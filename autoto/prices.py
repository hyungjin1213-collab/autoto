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
    if df["Close"].notna().mean() < 0.5:
        # 2026-10-12부터 시가총액 페이지가 현재가·거래량을 '-' 로만 보여줍니다 (새 사이트 이전 중).
        # 종목코드·상장주식수는 그대로 쓰고 가격은 업종별 JSON에서 채웁니다.
        print("시가총액 페이지에 가격이 없어 업종별 시세로 채웁니다", file=sys.stderr)
        df = fill_quotes(df, naver_quotes())
    return df[LISTING_COLUMNS].reset_index(drop=True)


NAVER_INDUSTRIES = "https://m.stock.naver.com/api/stocks/industry?page={page}&pageSize=50"
NAVER_INDUSTRY_STOCKS = "https://m.stock.naver.com/api/stocks/industry/{no}?page={page}&pageSize=50"


def _json(url):
    import json
    import requests

    for attempt in range(3):
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            r.raise_for_status()
            return json.loads(r.content.decode("utf-8"))
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))


def _paged(url, key, fetch, max_pages=40):
    items = []
    for page in range(1, max_pages + 1):
        data = fetch(url.format(page=page))
        got = data.get(key) or []
        items += got
        total = int(data.get("totalCount") or 0)
        if not got or len(got) < 50 or (total and len(items) >= total):
            break
    return items


def naver_quotes(fetch=_json):
    """업종별 종목 JSON으로 전 종목 현재가·거래량·거래대금·시가총액 (Code 인덱스)."""
    rows = {}
    for g in _paged(NAVER_INDUSTRIES, "groups", fetch):
        url = NAVER_INDUSTRY_STOCKS.replace("{no}", str(g["no"]))
        for x in _paged(url, "stocks", fetch):
            num = lambda k: _num(str(x.get(k, "")))  # noqa: E731
            rows[x["itemCode"]] = {"Close": num("closePrice"), "Volume": num("accumulatedTradingVolume"),
                                   "Amount": (num("accumulatedTradingValue") or 0) * 1e6,   # 백만원
                                   "Marcap": (num("marketValue") or 0) * 1e8}             # 억원
        time.sleep(0.05)
    if not rows:
        raise RuntimeError("네이버 업종별 시세를 읽지 못했습니다")
    return pd.DataFrame.from_dict(rows, orient="index")


def fill_quotes(listing_df, quotes):
    """목록의 비어 있는 가격 열을 quotes 로 채웁니다. 상장주식수가 없으면 시가총액/종가로."""
    df = listing_df.set_index("Code")
    q = quotes.reindex(df.index)
    for col in ("Close", "Volume", "Amount", "Marcap"):
        if col in q:
            df[col] = q[col].where(q[col].notna(), df.get(col))
    if "Stocks" in df:
        missing = df["Stocks"].fillna(0) <= 0
        df.loc[missing, "Stocks"] = (df["Marcap"] / df["Close"]).where(missing)
    return df.reset_index()


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
    start = pd.Timestamp(start)
    path = Path(cache_dir) / f"{code}.csv" if cache_dir else None
    # 캐시 파일 옆 .start 에 그 파일이 몇 일부터 받은 것인지 적어둡니다.
    # 더 이른 날짜를 요청하면(예: 8개월치 캐시로 5년 백테스트) 다시 받습니다.
    marker = path.with_suffix(".start") if path else None
    fresh = (path and path.exists() and marker.exists()
             and time.time() - path.stat().st_mtime < CACHE_MAX_AGE
             and pd.Timestamp(marker.read_text().strip()) <= start)
    if fresh:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
    else:
        df = _fdr().DataReader(code, start, end)[DAILY_COLUMNS]
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(path)
            marker.write_text(start.strftime("%Y-%m-%d"))
    df = df.loc[start:pd.Timestamp(end) if end else None]
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
