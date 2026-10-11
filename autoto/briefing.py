"""시황 브리핑: 아침(장 전)과 장 마감 후에 텔레그램으로 요약을 보냅니다.

내용
  - 코스피·코스닥 지수
  - 주도 업종·테마: 네이버 증권 업종/테마 등락률 상위와 그 안의 거래대금 상위 종목
  - 회전율 급증 종목 (output/turnover/turnover_history.csv)
  - 과매도 후보: 거래대금 상위 종목 중 RSI(14) 이하 + 20일선 이격도
  - 거래량 바닥 시가 이탈 (전 종목): 최근 40거래일 안에 바닥권에서 평소 8배 거래량의 꽉 찬
    장대양봉(기준봉)이 나왔는데, 지금 주가가 그 기준봉의 시가 아래로 내려온 종목
  - 상장 이래 최대 거래량 (전 종목): 마지막 거래일 거래량이 그 종목 역사상 최대
  - 월봉매매 후보와 목표가 도달 여부 (output/monthly_candle/candidates.csv)
  - 새 제3자배정 유상증자 공시 (DART_API_KEY 가 있으면 최근 며칠만 직접 조회)

텔레그램 설정 (GitHub Settings -> Secrets and variables -> Actions):
  TELEGRAM_BOT_TOKEN  텔레그램 @BotFather 에서 /newbot 으로 받은 토큰
  TELEGRAM_CHAT_ID    봇에게 아무 메시지나 보낸 뒤
                      https://api.telegram.org/bot<토큰>/getUpdates 에서 "chat":{"id": ...} 값
토큰이 없으면 메시지를 화면에 출력하고 output/briefing/latest.txt 로만 저장합니다.

Usage:
  python -m autoto.briefing                 # 시간에 따라 아침/마감 자동 선택
  python -m autoto.briefing --session close --dry-run
"""

import argparse
import datetime as dt
from dataclasses import dataclass
import html
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd

from autoto import prices

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output" / "briefing"
TURNOVER_CSV = ROOT / "output" / "turnover" / "turnover_history.csv"
LISTING_CSV = ROOT / "output" / "turnover" / "turnover_today.csv"
CANDIDATES_CSV = ROOT / "output" / "monthly_candle" / "candidates.csv"
RIGHTS_CSV = ROOT / "output" / "rights_offering" / "third_party_allotments.csv"
HOMEPAGE = "https://hyungjin1213-collab.github.io/autoto/"

KST = dt.timezone(dt.timedelta(hours=9))
FCHART = "https://fchart.stock.naver.com/sise.nhn?timeframe=day&count={count}&requestType=0&symbol={symbol}"
# 네이버 증권(새 사이트)이 쓰는 JSON. kind = industry(업종) / theme(테마)
# pageSize 는 너무 크면 400 오류가 나서 50씩 여러 페이지로 받습니다.
GROUPS_URL = "https://m.stock.naver.com/api/stocks/{kind}?page={page}&pageSize=50"
GROUP_STOCKS_URL = "https://m.stock.naver.com/api/stocks/{kind}/{no}?page=1&pageSize=50"
TELEGRAM_LIMIT = 4000  # 텔레그램 한 메시지 최대 4096자


def http_get(url, encoding="euc-kr", tries=3):
    import requests

    for attempt in range(tries):
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            r.raise_for_status()
            declared = re.search(r"charset=([\w-]+)", r.headers.get("content-type", ""))
            return r.content.decode(declared.group(1) if declared else encoding, errors="replace")
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def pct(text):
    """'+3.25%' -> 3.25"""
    text = (text or "").replace(",", "").replace("%", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def fmt_pct(x, digits=1):
    return "-" if x is None or pd.isna(x) else f"{x:+.{digits}f}%"


def won(x):
    return "-" if x is None or pd.isna(x) else f"{x:,.0f}"


def esc(text):
    return html.escape(str(text), quote=False)


# ---------------------------------------------------------------- 지수
def parse_fchart(xml):
    """네이버 차트 XML -> Date 인덱스의 Open/High/Low/Close/Volume."""
    rows = [item.split("|") for item in re.findall(r'<item data="([^"]+)"', xml)]
    df = pd.DataFrame(rows, columns=["Date", "Open", "High", "Low", "Close", "Volume"])
    df["Date"] = pd.to_datetime(df["Date"], format="%Y%m%d")
    return df.set_index("Date").astype(float)


def index_summary(fetch=http_get):
    out = {}
    for name, symbol in (("코스피", "KOSPI"), ("코스닥", "KOSDAQ")):
        df = parse_fchart(fetch(FCHART.format(count=5, symbol=symbol)))
        if len(df) >= 2:
            last, prev = df["Close"].iloc[-1], df["Close"].iloc[-2]
            out[name] = {"date": df.index[-1].date(), "close": last, "change": (last / prev - 1) * 100}
    return out


# ---------------------------------------------------------------- 주도 업종·테마
def parse_groups(data):
    """업종/테마 목록 JSON -> [{no, name, change, count}]."""
    return [{"no": g["no"], "name": g["name"], "change": pct(g.get("changeRate")),
             "count": int(g.get("totalCount") or 0)} for g in data.get("groups", [])]


def parse_group_stocks(data):
    """업종/테마 종목 JSON -> [{code, name, change, amount}]. amount = 거래대금(백만원)."""
    return [{"code": x.get("itemCode"), "name": x.get("stockName", ""), "change": pct(x.get("fluctuationsRatio")),
             "amount": prices._num(str(x.get("accumulatedTradingValue", ""))) or 0} for x in data.get("stocks", [])]


def fetch_json(url):
    import json
    return json.loads(http_get(url, encoding="utf-8"))


def leading_groups(kind="industry", fetch=fetch_json, top=5, stocks_per_group=3, min_count=5):
    """등락률 상위 업종(또는 테마)과 각 그룹의 거래대금 상위 종목."""
    groups, page = [], 1
    while page <= 20:
        data = fetch(GROUPS_URL.format(kind=kind, page=page))
        groups += parse_groups(data)
        if not data.get("groups") or len(groups) >= int(data.get("totalCount") or 0):
            break
        page += 1
    groups = [g for g in groups if g["change"] is not None and g["count"] >= min_count]
    if not groups:
        raise RuntimeError(f"네이버 {kind} 목록이 비어 있습니다")
    groups.sort(key=lambda g: g["change"], reverse=True)
    for g in groups[:top]:
        try:
            stocks = parse_group_stocks(fetch(GROUP_STOCKS_URL.format(kind=kind, no=g["no"])))
        except Exception as e:
            print(f"  {g['name']} 종목 실패: {e}", file=sys.stderr)
            stocks = []
        g["leaders"] = sorted(stocks, key=lambda x: x["amount"], reverse=True)[:stocks_per_group]
    return groups[:top]


# ---------------------------------------------------------------- 과매도
def rsi(close, period=14):
    """Wilder RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = gain / loss
    return 100 - 100 / (1 + rs)


def oversold_metrics(daily):
    c = daily["Close"]
    if len(c) < 25:
        return None
    return {"date": c.index[-1].date(), "close": float(c.iloc[-1]), "rsi": float(rsi(c).iloc[-1]),
            "disparity": float(c.iloc[-1] / c.tail(20).mean() * 100),
            "return_5d": float((c.iloc[-1] / c.iloc[-6] - 1) * 100)}


def fetch_recent(codes, days=200, fetch_daily=None):
    """최근 일봉. 캐시를 쓰지 않아야 오늘 종가가 들어갑니다."""
    start = pd.Timestamp.today().normalize() - pd.Timedelta(days=days)
    return fetch_daily(list(codes), start) if fetch_daily else prices.daily_many(list(codes), start)


def volume_record(daily, min_history=120):
    """마지막 거래일 거래량이 상장(데이터 시작) 이래 최대면 정보 dict.

    상장한 지 얼마 안 된 종목은 첫날들이 당연히 기록이라 min_history 거래일 미만이면 제외합니다.
    """
    vol = daily["Volume"]
    if len(vol) < min_history:
        return None
    last, prev_max = float(vol.iloc[-1]), float(vol.iloc[:-1].max())
    if last <= prev_max:
        return None
    c = daily["Close"]
    return {"date": daily.index[-1].date(), "volume": last, "vs_prev_record": last / prev_max,
            "prev_record_date": vol.iloc[:-1].idxmax().date(), "since": daily.index[0].date(),
            "change": float((c.iloc[-1] / c.iloc[-2] - 1) * 100), "amount": last * float(c.iloc[-1])}


def fetch_full(codes, keep=200, workers=8, daily=None):
    """종목별 상장 이래 일봉을 받아 (최근 keep 거래일 일봉 dict, 거래량 기록 dict) 로 줄여서 반환.

    네이버는 기간과 관계없이 전체 기록을 한 번에 주므로 전체를 받아도 다운로드 양은 같습니다.
    메모리를 아끼려고 종목마다 받자마자 필요한 부분만 남깁니다.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    get = daily or (lambda code: prices.daily(code, "1980-01-01"))
    recent, records = {}, {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(get, code): code for code in codes}
        for i, future in enumerate(as_completed(futures), 1):
            code = futures[future]
            try:
                df = future.result()
            except Exception as e:
                print(f"  {code} 시세 실패: {e}", file=sys.stderr)
                continue
            if df is None or df.empty:
                continue
            recent[code] = df.tail(keep)
            rec = volume_record(df)
            if rec:
                records[code] = rec
            if i % 500 == 0:
                print(f"  시세 {i}/{len(codes)}", file=sys.stderr)
    return recent, records


def record_volume_rows(universe, records, top=10):
    names = universe.set_index("Code")["Name"]
    rows = [{"code": c, "name": names[c], **r} for c, r in records.items() if c in names.index]
    rows.sort(key=lambda r: r["amount"], reverse=True)
    latest = max((r["date"] for r in rows), default=None)
    return [r for r in rows if r["date"] == latest][:top]


def oversold(universe, max_rsi=30, top=10, fetch_daily=None, data=None):
    """universe: Code/Name/Market 열. RSI(14) <= max_rsi 를 RSI 낮은 순으로."""
    if data is None:
        data = fetch_recent(universe["Code"], 120, fetch_daily)
    data = {c: data[c] for c in universe["Code"] if c in data}
    if not data:
        raise RuntimeError("시세를 하나도 받지 못했습니다")
    names = universe.set_index("Code")
    rows = []
    for code, df in data.items():
        m = oversold_metrics(df)
        if m and m["rsi"] <= max_rsi:
            rows.append({"code": code, "name": names.loc[code, "Name"], **m})
    rows.sort(key=lambda r: r["rsi"])
    latest = max((m["date"] for m in rows), default=None)
    return rows[:top], latest


@dataclass
class BottomParams:
    lookback: int = 40        # 기준봉을 찾는 기간 (오늘 제외 최근 N거래일)
    vol_mult: float = 8.0     # 기준봉 거래량 >= 직전 vol_avg일 평균 x vol_mult
    vol_avg: int = 20
    min_gain: float = 0.05    # 기준봉 종가 상승률(전일 종가 대비) 하한
    min_fill: float = 0.5     # 종가 상승폭 / 고가 상승폭 하한 (고가 +20%, 종가 +10% = 0.5)
    low_window: int = 60      # 바닥 판단: 기준봉 저가가 직전 low_window일 최저가의
    near_low: float = 0.10    #   (1 + near_low) 배 이내


def find_bottom_candle(daily, p=BottomParams()):
    """최근 p.lookback 거래일(오늘 제외) 중 조건을 만족하는 가장 최근 기준봉의 위치(iloc). 없으면 None."""
    o, h, l, c, v = (daily[k].to_numpy(dtype=float) for k in ("Open", "High", "Low", "Close", "Volume"))
    n = len(c)
    first = max(n - 1 - p.lookback, max(p.vol_avg, p.low_window, 1))
    for i in range(n - 2, first - 1, -1):
        prev = c[i - 1]
        avg_vol = v[i - p.vol_avg:i].mean()
        if not (prev > 0 and avg_vol > 0 and v[i] >= p.vol_mult * avg_vol and c[i] > o[i]):
            continue
        gain, high_gain = c[i] / prev - 1, h[i] / prev - 1
        if gain < p.min_gain or high_gain <= 0 or gain / high_gain < p.min_fill:
            continue
        if l[i] > l[i - p.low_window:i].min() * (1 + p.near_low):
            continue
        return i
    return None


def bottom_break(daily, p=BottomParams()):
    """기준봉이 있고 현재 종가가 그 시가 아래면 정보 dict, 아니면 None."""
    if len(daily) < max(p.vol_avg, p.low_window) + 2:
        return None
    i = find_bottom_candle(daily, p)
    if i is None:
        return None
    base, close = daily.iloc[i], float(daily["Close"].iloc[-1])
    if close >= base["Open"]:
        return None
    prev = daily["Close"].iloc[i - 1]
    return {"base_date": daily.index[i].date(), "days_ago": len(daily) - 1 - i,
            "base_open": float(base["Open"]), "base_gain": float((base["Close"] / prev - 1) * 100),
            "base_high_gain": float((base["High"] / prev - 1) * 100),
            "vol_mult": float(base["Volume"] / daily["Volume"].iloc[i - p.vol_avg:i].mean()),
            "close": close, "below_open": float((close / base["Open"] - 1) * 100)}


def bottom_breaks(universe, data, p=BottomParams(), top=10):
    """기준봉 시가를 이탈한 종목, 기준봉이 최근인 순."""
    names = universe.set_index("Code")["Name"]
    rows = []
    for code in universe["Code"]:
        if code in data:
            m = bottom_break(data[code], p)
            if m:
                rows.append({"code": code, "name": names[code], **m})
    rows.sort(key=lambda r: r["days_ago"])
    return rows[:top]


def liquid_universe(n=300, min_price=1000):  # n=None 이면 전 종목
    """거래대금 상위 n 종목. 오늘 아침 회전율 결과가 있으면 그 목록을 씁니다."""
    if LISTING_CSV.exists():
        df = pd.read_csv(LISTING_CSV, dtype={"종목코드": str}).rename(columns={
            "종목코드": "Code", "종목명": "Name", "시장": "Market", "종가": "Close", "거래대금": "Amount"})
    else:
        df = prices.listing()
    df = df[df["Close"] >= min_price]
    df = df.sort_values("Amount", ascending=False)
    return (df if n is None else df.head(n))[["Code", "Name", "Market"]]


# ---------------------------------------------------------------- 월봉 후보 / 회전율 / 유상증자
def monthly_candidates(path=CANDIDATES_CSV, refresh=True, fetch_daily=None):
    """후보 목록. refresh 면 후보 종목만 일봉을 새로 받아 이번 달 고가와 목표가 도달 여부를 갱신."""
    if not path.exists():
        return []
    df = pd.read_csv(path, dtype={"종목코드": str})
    if df.empty:
        return []
    if refresh:
        month_start = pd.Timestamp.today().normalize().replace(day=1)
        get = fetch_daily or (lambda codes, start: prices.daily_many(codes, start))
        data = get(list(df["종목코드"]), month_start)
        highs = {c: float(d["High"].max()) for c, d in data.items() if not d.empty}
        df["이번달고가"] = df["종목코드"].map(highs).fillna(df.get("이번달고가"))
        df["목표도달"] = df["이번달고가"] >= df["매도목표가"]
    sort = [c for c in ("적중률", "일평균거래대금") if c in df]
    return df.sort_values(sort, ascending=False).to_dict("records") if sort else df.to_dict("records")


def turnover_spikes(path=TURNOVER_CSV, top=5, min_spike=3):
    if not path.exists():
        return []
    df = pd.read_csv(path, dtype={"종목코드": str})
    col = "급증배수(5일/60일)"
    df = df[df[col] >= min_spike].sort_values(col, ascending=False)
    return df.head(top).to_dict("records")


def recent_allotments(since, path=RIGHTS_CSV):
    """접수일이 since(date) 이후인 제3자배정 공시."""
    df = pd.read_csv(path, dtype={"접수일": str, "종목코드": str})
    df = df[pd.to_datetime(df["접수일"], format="%Y%m%d").dt.date >= since]
    return df.sort_values("접수번호", ascending=False).to_dict("records")


def previous_weekday(day):
    day -= dt.timedelta(days=1)
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day


# ---------------------------------------------------------------- 메시지
def group_lines(groups):
    lines = []
    for i, g in enumerate(groups, 1):
        leaders = ", ".join(f"{esc(x['name'])} {fmt_pct(x['change'])}" for x in g.get("leaders", []))
        lines.append(f"{i}. {esc(g['name'])} <b>{fmt_pct(g['change'])}</b>" + (f"\n    {leaders}" if leaders else ""))
    return lines or ["데이터 없음"]


def build_message(session, now, indices, sectors, spikes, oversold_rows, candidates, allotments, errors=(),
                  themes=(), bottoms=None, records=None):
    title = "🌅 장 전 브리핑" if session == "morning" else "🌆 장 마감 브리핑"
    lines = [f"<b>{title}</b>  {now:%Y-%m-%d} ({'월화수목금토일'[now.weekday()]})"]
    if indices:
        lines.append(" · ".join(f"{k} {v['close']:,.2f} ({fmt_pct(v['change'], 2)})" for k, v in indices.items()))

    lines += ["", "🔥 <b>주도 업종</b>"] + group_lines(sectors)
    lines += ["", "🎯 <b>주도 테마</b>"] + group_lines(themes)

    lines += ["", "📈 <b>거래 급증</b> (회전율 5일/60일 평균)"]
    for r in spikes:
        lines.append(f"• {esc(r['종목명'])} {r['급증배수(5일/60일)']:.1f}배, 20일 {fmt_pct(r['20일수익률'] * 100)}")
    if not spikes:
        lines.append("해당 없음")

    lines += ["", "📉 <b>과매도 후보</b> (거래대금 상위, RSI14 ≤ 30)"]
    for r in oversold_rows:
        lines.append(f"• {esc(r['name'])} RSI {r['rsi']:.0f}, 20일선 {r['disparity']:.0f}%, 5일 {fmt_pct(r['return_5d'])}")
    if not oversold_rows:
        lines.append("해당 없음")

    if records is not None:
        lines += ["", "🚀 <b>상장 이래 최대 거래량</b>"]
        for r in records:
            lines.append(f"• {esc(r['name'])} {fmt_pct(r['change'])}, 거래대금 {r['amount'] / 1e8:,.0f}억 "
                         f"(종전 최대 {r['prev_record_date']:%y.%m.%d}의 {r['vs_prev_record']:.1f}배)")
        if not records:
            lines.append("해당 없음")

    if bottoms is not None:
        lines += ["", "🧲 <b>과매도 (거래량 바닥 시가 이탈)</b>", "<i>전 종목 · 40일 내 바닥권 8배 거래량 장대양봉 → 그 시가 아래</i>"]
        for r in bottoms:
            lines.append(f"• {esc(r['name'])} {r['base_date']:%m/%d} 기준봉 {fmt_pct(r['base_gain'])}·거래량 "
                         f"{r['vol_mult']:.0f}배 → 시가 대비 {fmt_pct(r['below_open'])}")
        if not bottoms:
            lines.append("해당 없음")

    hit = sum(bool(c.get("목표도달")) for c in candidates)
    lines += ["", f"🗓 <b>월봉매매 후보</b> {now.month}월 {len(candidates)}종목 · 목표가 도달 {hit}"]
    for c in candidates[:10]:
        mark = "✅" if c.get("목표도달") else "⏳"
        lines.append(f"{mark} {esc(c['종목명'])} 시가 {won(c.get('이번달시가'))} → 목표 {won(c.get('매도목표가'))}")
    if len(candidates) > 10:
        lines.append(f"…외 {len(candidates) - 10}종목")

    lines += ["", f"📝 <b>제3자배정 유상증자</b> 새 공시 {len(allotments)}건"]
    for a in allotments[:8]:
        amount = a.get("조달금액_합계")
        amount = f" {amount / 1e8:,.0f}억" if amount and not pd.isna(amount) else ""
        who = esc(str(a.get("배정대상자") or "")[:60])
        lines.append(f"• <a href=\"{a['공시링크']}\">{esc(a['회사명'])}</a>{amount} → {who}")

    if errors:
        lines += ["", "⚠️ " + ", ".join(errors) + " 불러오기 실패"]
    lines += ["", f"<a href=\"{HOMEPAGE}\">홈페이지에서 자세히 보기</a>"]
    return "\n".join(lines)


def split_message(text, limit=TELEGRAM_LIMIT):
    """줄 단위로 끊어 limit 이하 조각들로."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    return chunks + [cur] if cur else chunks


def send_telegram(token, chat_id, text):
    import requests

    for chunk in split_message(text):
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=30, data={
            "chat_id": chat_id, "text": chunk, "parse_mode": "HTML", "disable_web_page_preview": "true"})
        if not r.ok:
            raise RuntimeError(f"텔레그램 전송 실패 {r.status_code}: {r.text[:200]}")


# ---------------------------------------------------------------- 실행
def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", choices=["auto", "morning", "close"], default="auto",
                    help="auto: 한국시간 12시 전이면 아침, 이후면 마감")
    ap.add_argument("--rights-csv", type=Path, help="제3자배정 CSV (기본: output/rights_offering)")
    ap.add_argument("--universe", type=int, default=300, help="RSI 과매도 검사 대상: 거래대금 상위 N")
    ap.add_argument("--vol-mult", type=float, default=8.0, help="기준봉 거래량 배수")
    ap.add_argument("--max-rsi", type=float, default=30)
    ap.add_argument("--dry-run", action="store_true", help="텔레그램으로 보내지 않고 출력만")
    ap.add_argument("--force", action="store_true", help="휴장일에도 보내기 (수동 실행 시 자동)")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    now = dt.datetime.now(KST)
    session = args.session if args.session != "auto" else ("morning" if now.hour < 12 else "close")
    today = now.date()
    errors = []

    def attempt(label, fn, default):
        try:
            return fn()
        except Exception as e:
            print(f"{label} 실패: {type(e).__name__}: {e}", file=sys.stderr)
            errors.append(label)
            return default

    indices = attempt("지수", index_summary, {})
    last_day = max((v["date"] for v in indices.values()), default=None)
    if session == "close" and last_day and last_day < today and not args.force:
        print(f"오늘({today})은 휴장일로 보입니다 (마지막 거래일 {last_day}). 보내지 않습니다.", file=sys.stderr)
        return

    sectors = attempt("주도 업종", lambda: leading_groups("industry"), [])
    themes = attempt("주도 테마", lambda: leading_groups("theme"), [])
    spikes = attempt("거래 급증", turnover_spikes, [])
    everyone = attempt("종목 목록", lambda: liquid_universe(None, min_price=0), None)  # 거래대금 순 전 종목
    data, recs = attempt("시세", lambda: fetch_full(everyone["Code"]), ({}, {})) if everyone is not None else ({}, {})
    rsi_universe = everyone[everyone["Code"].isin(liquid_universe(args.universe)["Code"])] \
        if everyone is not None else None
    oversold_rows, _ = attempt("과매도", lambda: oversold(rsi_universe, args.max_rsi, data=data), ([], None))
    bottoms = attempt("거래량 바닥", lambda: bottom_breaks(everyone, data, BottomParams(vol_mult=args.vol_mult)), [])
    records = attempt("최대 거래량", lambda: record_volume_rows(everyone, recs), [])
    candidates = attempt("월봉 후보", monthly_candidates, [])
    since = previous_weekday(today) if session == "morning" else today
    rights_csv = args.rights_csv or RIGHTS_CSV
    if rights_csv.exists():
        allotments = attempt("유상증자", lambda: recent_allotments(since, rights_csv), [])
    else:
        print(f"유상증자 결과 없음: {rights_csv} (DART_API_KEY 확인)", file=sys.stderr)
        allotments = []
        errors.append("유상증자")

    text = build_message(session, now, indices, sectors, spikes, oversold_rows, candidates, allotments, errors,
                         themes, bottoms, records)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "latest.txt").write_text(text, encoding="utf-8")

    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if args.dry_run or not (token and chat_id):
        if not args.dry_run:
            print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 없어 출력만 합니다.", file=sys.stderr)
        print(text)
        return
    send_telegram(token, chat_id, text)
    print(f"텔레그램 전송 완료 ({len(text)}자)", file=sys.stderr)


if __name__ == "__main__":
    main()
