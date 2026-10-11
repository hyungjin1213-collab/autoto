"""시황 브리핑: 아침(장 전)과 장 마감 후에 텔레그램으로 요약을 보냅니다.

내용
  - 코스피·코스닥 지수
  - 주도 섹터: 네이버 업종 등락률 상위와 업종별 거래대금 상위 종목
  - 회전율 급증 종목 (output/turnover/turnover_history.csv)
  - 과매도 후보: 거래대금 상위 종목 중 RSI(14) 이하 + 20일선 이격도
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
SECTORS_URL = "https://finance.naver.com/sise/sise_group.naver?type=upjong"
SECTOR_DETAIL_URL = "https://finance.naver.com/sise/sise_group_detail.naver?type=upjong&no={no}"
SECTOR_RE = re.compile(r"sise_group_detail\.naver\?type=upjong&(?:amp;)?no=(\d+)[^>]*>(.*?)</a>", re.S)
PCT_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?%$")
TELEGRAM_LIMIT = 4000  # 텔레그램 한 메시지 최대 4096자


def http_get(url, encoding="euc-kr", tries=3):
    import requests

    for attempt in range(tries):
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            r.raise_for_status()
            return r.content.decode(encoding, errors="replace")
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


# ---------------------------------------------------------------- 주도 섹터
def parse_sectors(page_html):
    """업종 목록 -> [{no, name, change, count}]. count = 업종 종목 수."""
    sectors = []
    for tr in prices.ROW_RE.findall(page_html):
        m = SECTOR_RE.search(tr)
        if not m:
            continue
        cells = [prices._text(c) for _, c in prices.CELL_RE.findall(tr)]
        change = next((pct(c) for c in cells if PCT_RE.match(c.replace(" ", ""))), None)
        counts = [int(c) for c in cells if c.isdigit()]
        sectors.append({"no": m.group(1), "name": prices._text(m.group(2)), "change": change,
                        "count": counts[0] if counts else 0})
    return sectors


def parse_sector_stocks(page_html):
    """업종 상세 -> [{code, name, change, amount}] (헤더 이름으로 열을 찾습니다)."""
    header, rows = None, []
    for tr in prices.ROW_RE.findall(page_html):
        cells = prices.CELL_RE.findall(tr)
        if cells and all(kind.lower() == "h" for kind, _ in cells):
            header = [prices._text(c).replace(" ", "") for _, c in cells]
            continue
        code = prices.CODE_RE.search(tr)
        if not code or not header:
            continue
        texts = [prices._text(c) for _, c in cells]
        values = dict(zip(header, texts))
        change = pct(values.get("등락률")) if "등락률" in values else next(
            (pct(t) for t in texts if PCT_RE.match(t.replace(" ", ""))), None)
        rows.append({"code": code.group(1), "name": values.get("종목명", texts[0]).rstrip(" *"),
                     "change": change, "amount": prices._num(values.get("거래대금", "")) or 0})
    return rows


def leading_sectors(fetch=http_get, top=5, stocks_per_sector=3, min_count=5):
    sectors = [s for s in parse_sectors(fetch(SECTORS_URL))
               if s["change"] is not None and s["count"] >= min_count]
    sectors.sort(key=lambda s: s["change"], reverse=True)
    for s in sectors[:top]:
        try:
            stocks = parse_sector_stocks(fetch(SECTOR_DETAIL_URL.format(no=s["no"])))
        except Exception as e:
            print(f"  업종 상세 실패 {s['name']}: {e}", file=sys.stderr)
            stocks = []
        s["leaders"] = sorted(stocks, key=lambda x: x["amount"], reverse=True)[:stocks_per_sector]
    return sectors[:top]


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


def oversold(universe, max_rsi=30, top=10, fetch_daily=None):
    """universe: Code/Name/Market 열. RSI(14) <= max_rsi 를 RSI 낮은 순으로."""
    start = pd.Timestamp.today().normalize() - pd.Timedelta(days=120)
    data = fetch_daily(list(universe["Code"]), start) if fetch_daily else \
        prices.daily_many(list(universe["Code"]), start)  # 캐시를 쓰지 않아야 오늘 종가가 들어갑니다
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


def liquid_universe(n=300, min_price=1000):
    """거래대금 상위 n 종목. 오늘 아침 회전율 결과가 있으면 그 목록을 씁니다."""
    if LISTING_CSV.exists():
        df = pd.read_csv(LISTING_CSV, dtype={"종목코드": str}).rename(columns={
            "종목코드": "Code", "종목명": "Name", "시장": "Market", "종가": "Close", "거래대금": "Amount"})
    else:
        df = prices.listing()
    df = df[df["Close"] >= min_price]
    return df.sort_values("Amount", ascending=False).head(n)[["Code", "Name", "Market"]]


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
def build_message(session, now, indices, sectors, spikes, oversold_rows, candidates, allotments, errors=()):
    title = "🌅 장 전 브리핑" if session == "morning" else "🌆 장 마감 브리핑"
    lines = [f"<b>{title}</b>  {now:%Y-%m-%d} ({'월화수목금토일'[now.weekday()]})"]
    if indices:
        lines.append(" · ".join(f"{k} {v['close']:,.2f} ({fmt_pct(v['change'], 2)})" for k, v in indices.items()))

    lines += ["", "🔥 <b>주도 섹터</b>"]
    for i, s in enumerate(sectors, 1):
        leaders = ", ".join(f"{esc(x['name'])} {fmt_pct(x['change'])}" for x in s.get("leaders", []))
        lines.append(f"{i}. {esc(s['name'])} <b>{fmt_pct(s['change'])}</b>" + (f"\n    {leaders}" if leaders else ""))
    if not sectors:
        lines.append("데이터 없음")

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
    ap.add_argument("--universe", type=int, default=300, help="과매도 검사 대상: 거래대금 상위 N")
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

    sectors = attempt("주도 섹터", leading_sectors, [])
    spikes = attempt("거래 급증", turnover_spikes, [])
    oversold_rows, _ = attempt("과매도", lambda: oversold(liquid_universe(args.universe), args.max_rsi), ([], None))
    candidates = attempt("월봉 후보", monthly_candidates, [])
    since = previous_weekday(today) if session == "morning" else today
    rights_csv = args.rights_csv or RIGHTS_CSV
    if rights_csv.exists():
        allotments = attempt("유상증자", lambda: recent_allotments(since, rights_csv), [])
    else:
        print(f"유상증자 결과 없음: {rights_csv} (DART_API_KEY 확인)", file=sys.stderr)
        allotments = []
        errors.append("유상증자")

    text = build_message(session, now, indices, sectors, spikes, oversold_rows, candidates, allotments, errors)
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
