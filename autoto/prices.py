"""Korean stock prices: the stock listing and per-stock daily OHLCV.

The listing comes from Naver Finance's market-cap pages (KRX's data site
refuses connections from overseas servers such as GitHub Actions), with
FinanceDataReader's KRX listing as a fallback. Daily bars come from
FinanceDataReader (Naver, adjusted for splits) and can be cached on disk so
reruns and the backtest do not download every stock again.
"""

import html
import re
import sys
import time
from pathlib import Path

import pandas as pd

LISTING_COLUMNS = ["Code", "Name", "Market", "Close", "Volume", "Amount", "Marcap", "Stocks"]
DAILY_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]
CACHE_MAX_AGE = 12 * 3600  # seconds; a cached file older than this is downloaded again


def _fdr():
    import FinanceDataReader as fdr  # imported lazily so tests do not need it
    return fdr


NAVER_MARKET_SUM = "https://finance.naver.com/sise/sise_market_sum.naver?sosok={sosok}&page={page}"
NAVER_SOSOK = {"KOSPI": 0, "KOSDAQ": 1}
ROW_RE = re.compile(r"<tr\b.*?</tr>", re.I | re.S)
CELL_RE = re.compile(r"<t([dh])\b[^>]*>(.*?)</t\1>", re.I | re.S)
CODE_RE = re.compile(r"code=(\w{6})")
TAG_RE = re.compile(r"<[^>]+>")


def _text(cell):
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", cell))).strip()


def _num(text):
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def parse_market_sum(page_html, market):
    """네이버 시가총액 페이지 한 장 -> [{Code, Name, Market, Close, Volume, Marcap, Stocks, Par}]."""
    header, rows = None, []
    for tr in ROW_RE.findall(page_html):
        cells = CELL_RE.findall(tr)
        if cells and all(kind.lower() == "h" for kind, _ in cells):
            header = [_text(c) for _, c in cells]
            continue
        code = CODE_RE.search(tr)
        if not code or not header:
            continue
        values = dict(zip(header, (_text(c) for _, c in cells)))
        rows.append({
            "Code": code.group(1),
            "Name": values.get("종목명", ""),
            "Market": market,
            "Close": _num(values.get("현재가", "")),
            "Volume": _num(values.get("거래량", "")),
            "Marcap": (_num(values.get("시가총액", "")) or 0) * 1e8,       # 억원
            "Stocks": (_num(values.get("상장주식수", "")) or 0) * 1000,     # 천주
            "Par": _num(values.get("액면가", "")),
        })
    return rows


def naver_listing(markets=("KOSPI", "KOSDAQ"), fetch=None, max_pages=100):
    if fetch is None:
        import requests

        def fetch(url):
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            r.raise_for_status()
            return r.content.decode("euc-kr", errors="replace")

    rows, seen = [], set()
    for market in markets:
        for page in range(1, max_pages + 1):
            new = [r for r in parse_market_sum(fetch(NAVER_MARKET_SUM.format(sosok=NAVER_SOSOK[market], page=page)), market)
                   if r["Code"] not in seen]
            if not new:  # 마지막 페이지를 지나면 같은 종목이 반복되거나 비어 있습니다
                break
            seen.update(r["Code"] for r in new)
            rows.extend(new)
            time.sleep(0.2)
    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("네이버 시가총액 페이지에서 종목을 읽지 못했습니다")
    df = df[df["Par"].fillna(0) > 0]  # 액면가 0 = ETF/ETN
    df["Amount"] = df["Close"] * df["Volume"]  # 거래대금 근사치 (종가 x 거래량)
    return df[LISTING_COLUMNS].reset_index(drop=True)


def listing(markets=("KOSPI", "KOSDAQ")):
    """Today's listing: code, name, market, last price/volume, listed shares."""
    try:
        return naver_listing(markets)
    except Exception as e:
        print(f"네이버 종목 목록 실패, KRX로 재시도: {e}", file=sys.stderr)
    df = _fdr().StockListing("KRX")
    df = df[df["Market"].fillna("").str.startswith(tuple(markets))]
    return df[LISTING_COLUMNS].reset_index(drop=True)


def daily(code, start, end=None, cache_dir=None):
    """Daily Open/High/Low/Close/Volume indexed by date. Halted days are dropped."""
    path = Path(cache_dir) / f"{code}.csv" if cache_dir else None
    if path and path.exists() and time.time() - path.stat().st_mtime < CACHE_MAX_AGE:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
    else:
        df = _fdr().DataReader(code, start, end)[DAILY_COLUMNS]
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(path)
    df = df.loc[pd.Timestamp(start):pd.Timestamp(end) if end else None]
    return df[(df["Volume"] > 0) & (df["Open"] > 0)]


def daily_many(codes, start, end=None, cache_dir=None, workers=8):
    """{code: daily DataFrame}; stocks that fail to download are skipped.

    Downloads run in parallel threads: each request mostly waits on the network.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    result = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(daily, code, start, end, cache_dir): code for code in codes}
        for i, future in enumerate(as_completed(futures), 1):
            code = futures[future]
            try:
                df = future.result()
            except Exception as e:  # one bad ticker should not stop a 2,500-stock run
                print(f"  {code} 시세 실패: {e}", file=sys.stderr)
                continue
            if not df.empty:
                result[code] = df
            if i % 200 == 0:
                print(f"  시세 {i}/{len(codes)}", file=sys.stderr)
    return {code: result[code] for code in codes if code in result}  # 입력 순서 유지
