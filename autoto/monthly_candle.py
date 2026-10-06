"""월봉매매법: 월봉이 횡보하면서 매달 (고가-시가)/시가 >= 5% 가 꾸준히 나온 종목을
월초 시가에 사고, 매일 매수가 +5% 에 지정가 매도를 걸어두는 전략.

규칙 (기본값, 옵션으로 변경 가능):
  선별 - 직전 6개월 월봉 중 고가가 시가보다 5% 이상 오른 달의 비율(적중률)이 100%
       - 직전 6개월 월말 종가의 최고/최저 차이(횡보폭)가 25% 이하
       - 일평균 거래대금 1억원 이상, 주가 1,000원 이상
  매매 - 그달 첫 거래일 시가에 매수, 목표가 = 매수가 x 1.05 (호가단위 올림)
       - 어느 날이든 고가가 목표가에 닿으면 목표가에 매도
         (그날 시가가 이미 목표가 위면 시가에 체결)
       - 끝내 못 닿으면 그달 마지막 거래일 종가에 매도
       - 왕복 비용 0.25% (수수료 + 거래세) 차감

백테스트는 매달 그 이전 달까지의 데이터만으로 종목을 고르므로 미래 정보를 쓰지 않습니다.
같은 매매 규칙을 선별 없이 전 종목에 적용한 '기준선'과 비교해 선별이 효과가 있는지 봅니다.

Usage:
  python -m autoto.monthly_candle screen                 # 이번 달 후보
  python -m autoto.monthly_candle screen --month 2026-11
  python -m autoto.monthly_candle backtest --years 5
"""

import argparse
import sys
from dataclasses import dataclass, fields
from pathlib import Path

import pandas as pd

from autoto import prices

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output" / "monthly_candle"
CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "prices"


@dataclass
class Params:
    lookback: int = 6          # 선별에 쓰는 직전 개월 수
    target: float = 0.05       # 목표 수익률 (고가-시가)/시가
    min_hit: float = 1.0       # 직전 개월 중 목표 도달 비율의 최소값
    max_range: float = 0.25    # 횡보폭: 월말 종가 최고/최저 - 1 의 최대값
    min_amount: float = 1e8    # 일평균 거래대금 최소값 (원)
    min_price: float = 1000    # 직전 종가 최소값 (원)
    cost: float = 0.0025       # 왕복 거래비용 (수수료 + 거래세)


def tick_size(price):
    """KRX 호가단위 (2023년 이후 코스피·코스닥 공통)."""
    for limit, tick in ((2000, 1), (5000, 5), (20000, 10), (50000, 50), (200000, 100), (500000, 500)):
        if price < limit:
            return tick
    return 1000


def tick_up(price):
    """price 이상인 가장 가까운 유효 호가."""
    tick = tick_size(price)
    return -(-price // tick) * tick


def monthly_bars(daily):
    """일봉 -> 월봉. Amount 는 그달 일평균 거래대금(종가 x 거래량)."""
    month = daily.index.to_period("M")
    bars = daily.groupby(month).agg(
        Open=("Open", "first"), High=("High", "max"), Low=("Low", "min"),
        Close=("Close", "last"), Days=("Close", "size"))
    bars["Amount"] = (daily["Close"] * daily["Volume"]).groupby(month).sum() / bars["Days"]
    return bars


def lookback_window(bars, month, lookback):
    """month 직전 lookback 개월의 월봉. 한 달이라도 비어 있으면 None."""
    want = pd.period_range(end=month - 1, periods=lookback, freq="M")
    if not want.isin(bars.index).all():
        return None
    return bars.loc[want]


def window_stats(window, target):
    upside = window["High"] / window["Open"] - 1
    return {
        "hit_rate": float((upside >= target).mean()),
        "min_upside": float(upside.min()),
        "median_upside": float(upside.median()),
        "range": float(window["Close"].max() / window["Close"].min() - 1),
        "drift": float(window["Close"].iloc[-1] / window["Open"].iloc[0] - 1),
        "avg_amount": float(window["Amount"].mean()),
        "last_close": float(window["Close"].iloc[-1]),
    }


def is_eligible(stats, p):
    """선별 전 기본 조건 (유동성·가격). 기준선도 이 조건을 통과한 종목만 씁니다."""
    return stats["avg_amount"] >= p.min_amount and stats["last_close"] >= p.min_price


def is_selected(stats, p):
    return (is_eligible(stats, p)
            and stats["hit_rate"] >= p.min_hit - 1e-9
            and stats["range"] <= p.max_range)


def simulate_month(days, target, cost):
    """한 달치 일봉으로 '월초 시가 매수, 매일 +target 지정가 매도' 를 실행."""
    entry = float(days["Open"].iloc[0])
    goal = tick_up(entry * (1 + target))
    reached = days.index[days["High"] >= goal]
    if len(reached):
        day = reached[0]
        exit_price = max(goal, float(days.at[day, "Open"]))  # 갭상승이면 시가에 체결
        hit = True
    else:
        day = days.index[-1]
        exit_price = float(days.at[day, "Close"])
        hit = False
    return {
        "entry": entry,
        "goal": goal,
        "exit": exit_price,
        "exit_date": day.date(),
        "hit": hit,
        "days_held": int(days.index.get_loc(day)) + 1,
        "ret": exit_price / entry - 1 - cost,
    }


def backtest(daily_by_code, p, start=None, end=None, today=None):
    """종목·월별 거래 내역. selected=True 가 전략 선별 종목, 나머지는 기준선."""
    current = pd.Period(today or pd.Timestamp.today(), "M")
    start = pd.Period(start, "M") if start else None
    end = pd.Period(end, "M") if end else current - 1
    end = min(end, current - 1)  # 진행 중인 달은 결과가 확정되지 않았으므로 제외

    rows = []
    for code, daily in daily_by_code.items():
        bars = monthly_bars(daily)
        for month, days in daily.groupby(daily.index.to_period("M")):
            if month > end or (start and month < start):
                continue
            window = lookback_window(bars, month, p.lookback)
            if window is None:
                continue
            stats = window_stats(window, p.target)
            if not is_eligible(stats, p):
                continue
            rows.append({"code": code, "month": str(month), "selected": is_selected(stats, p),
                         **stats, **simulate_month(days, p.target, p.cost)})
    return pd.DataFrame(rows)


def max_drawdown(returns):
    equity = (1 + returns).cumprod()
    return float((equity / equity.cummax() - 1).min()) if len(equity) else 0.0


def group_summary(trades):
    if trades.empty:
        return {"거래수": 0}
    miss = trades[~trades["hit"]]
    return {
        "거래수": len(trades),
        "목표도달률": trades["hit"].mean(),
        "평균수익률": trades["ret"].mean(),
        "중앙수익률": trades["ret"].median(),
        "수익거래비율": (trades["ret"] > 0).mean(),
        "미도달시_평균수익률": miss["ret"].mean() if len(miss) else None,
        "평균보유일": trades["days_held"].mean(),
    }


def summarize(trades):
    """(요약 표, 월별 수익률 표). 월별 수익률은 선별 종목 동일가중, 후보가 없던 달은 0(현금)."""
    if trades.empty:
        return pd.DataFrame(), pd.DataFrame()
    months = sorted(trades["month"].unique())
    picked = trades[trades["selected"]]
    monthly = pd.DataFrame({
        "전략": picked.groupby("month")["ret"].mean().reindex(months, fill_value=0.0),
        "기준선": trades.groupby("month")["ret"].mean().reindex(months),
        "선별종목수": picked.groupby("month").size().reindex(months, fill_value=0),
    })
    monthly.index.name = "month"

    summary = pd.DataFrame({"전략(선별)": group_summary(picked), "기준선(전종목)": group_summary(trades)},
                           dtype=object)
    for col, name in (("전략", "전략(선별)"), ("기준선", "기준선(전종목)")):
        r = monthly[col]
        summary.loc["월평균수익률", name] = r.mean()
        summary.loc["누적수익률", name] = (1 + r).prod() - 1
        summary.loc["최대낙폭", name] = max_drawdown(r)
    summary.loc["기간", :] = f"{months[0]} ~ {months[-1]} ({len(months)}개월)"
    summary.loc["후보있던달", "전략(선별)"] = int((monthly["선별종목수"] > 0).sum())
    return summary, monthly


def screen(daily_by_code, p, month):
    """month 에 매수할 후보. 그달 데이터가 이미 있으면 실제 시가와 목표가도 채웁니다."""
    month = pd.Period(month, "M")
    rows = []
    for code, daily in daily_by_code.items():
        window = lookback_window(monthly_bars(daily), month, p.lookback)
        if window is None:
            continue
        stats = window_stats(window, p.target)
        if not is_selected(stats, p):
            continue
        this_month = daily[daily.index.to_period("M") == month]
        row = {"code": code, **stats}
        if len(this_month):
            entry = float(this_month["Open"].iloc[0])
            row.update(entry=entry, goal=tick_up(entry * (1 + p.target)),
                       month_high=float(this_month["High"].max()))
            row["reached"] = row["month_high"] >= row["goal"]
        rows.append(row)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values(["hit_rate", "median_upside"], ascending=False).reset_index(drop=True)


CANDIDATE_COLUMNS = {
    "code": "종목코드", "name": "종목명", "market": "시장",
    "hit_rate": "적중률", "min_upside": "최소상승폭", "median_upside": "중앙상승폭",
    "range": "횡보폭", "drift": "기간등락률", "avg_amount": "일평균거래대금", "last_close": "직전월종가",
    "entry": "이번달시가", "goal": "매도목표가", "month_high": "이번달고가", "reached": "목표도달",
}


# ---------------------------------------------------------------------------


def load_universe(codes):
    """(codes, {code: (name, market)}). --codes 가 없으면 코스피·코스닥 전 종목."""
    try:
        listing = prices.listing()
        names = {r.Code: (r.Name, r.Market) for r in listing.itertuples()}
    except Exception as e:
        if not codes:
            sys.exit(f"KRX 종목 목록을 받지 못했습니다: {e}\n--codes 로 종목을 직접 지정하세요.")
        print(f"종목 목록 실패, 이름 없이 진행: {e}", file=sys.stderr)
        names = {}
    return (codes or list(names)), names


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["screen", "backtest"])
    ap.add_argument("--month", help="screen: 매수할 달 YYYY-MM (기본: 이번 달)")
    ap.add_argument("--years", type=float, default=5, help="backtest: 최근 몇 년 (기본 5)")
    ap.add_argument("--codes", nargs="*", help="종목코드 직접 지정 (기본: 코스피·코스닥 전체)")
    ap.add_argument("--out", type=Path, default=OUTPUT_DIR)
    ap.add_argument("--no-cache", action="store_true")
    for f in fields(Params):
        ap.add_argument(f"--{f.name.replace('_', '-')}", type=type(f.default), default=f.default)
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    p = Params(**{f.name: getattr(args, f.name) for f in fields(Params)})
    cache = None if args.no_cache else CACHE_DIR
    codes, names = load_universe(args.codes)
    args.out.mkdir(parents=True, exist_ok=True)
    print(f"종목 {len(codes)}개, 조건 {p}", file=sys.stderr)

    if args.command == "screen":
        month = pd.Period(args.month or pd.Timestamp.today(), "M")
        start = (month - p.lookback - 1).start_time
        data = prices.daily_many(codes, start, cache_dir=cache)
        df = screen(data, p, month)
        if not df.empty:
            df["name"] = df["code"].map(lambda c: names.get(c, ("", ""))[0])
            df["market"] = df["code"].map(lambda c: names.get(c, ("", ""))[1])
        df = df.reindex(columns=[c for c in CANDIDATE_COLUMNS if c in df.columns] or list(CANDIDATE_COLUMNS))
        df.round(4).rename(columns=CANDIDATE_COLUMNS).to_csv(args.out / "candidates.csv", index=False, encoding="utf-8-sig")
        print(f"{month} 후보 {len(df)}종목 -> {args.out / 'candidates.csv'}", file=sys.stderr)
        return

    end = pd.Timestamp.today()
    start = end - pd.DateOffset(months=int(args.years * 12) + p.lookback + 1)
    data = prices.daily_many(codes, start, cache_dir=cache)
    first = pd.Period(end, "M") - int(args.years * 12)
    trades = backtest(data, p, start=first)
    if trades.empty:
        sys.exit("백테스트할 거래가 없습니다.")
    trades.insert(1, "name", trades["code"].map(lambda c: names.get(c, ("", ""))[0]))
    summary, monthly = summarize(trades)
    trades.round(4).to_csv(args.out / "backtest_trades.csv", index=False, encoding="utf-8-sig")
    monthly.round(4).to_csv(args.out / "backtest_monthly.csv", encoding="utf-8-sig")
    summary.to_csv(args.out / "backtest_summary.csv", encoding="utf-8-sig")
    print(summary.to_string())


if __name__ == "__main__":
    main()
