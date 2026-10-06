"""회전율 분석: 거래량 / 상장주식수.

1. 코스피·코스닥 전 종목의 최근 거래일 회전율 순위 (KRX 목록 한 번 조회)
2. 회전율 상위 N 종목(또는 --codes)은 일봉을 받아 5/20/60일 평균, 20일 누적,
   급증배수(5일 평균 / 60일 평균)를 계산합니다.

상장주식수는 오늘 기준이므로, 기간 중 증자·감자가 있었다면 과거 회전율은 근사치입니다.

Usage:
  python -m autoto.turnover --top 100
  python -m autoto.turnover --codes 005930 000660
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

from autoto import prices

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output" / "turnover"
CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "prices"


def today_turnover(listing):
    """목록의 당일 거래량으로 계산한 회전율(%) 순위."""
    df = listing[listing["Stocks"] > 0].copy()
    df["turnover"] = df["Volume"] / df["Stocks"] * 100
    return df.sort_values("turnover", ascending=False).reset_index(drop=True)


def turnover_metrics(daily, shares):
    """일봉과 상장주식수로 회전율(%) 지표 계산."""
    t = daily["Volume"] / shares * 100
    avg60 = t.tail(60).mean()
    avg5 = t.tail(5).mean()
    return {
        "date": daily.index[-1].date(),
        "close": float(daily["Close"].iloc[-1]),
        "turnover_1d": float(t.iloc[-1]),
        "turnover_5d": float(avg5),
        "turnover_20d": float(t.tail(20).mean()),
        "turnover_60d": float(avg60),
        "turnover_sum_20d": float(t.tail(20).sum()),
        "spike": float(avg5 / avg60) if avg60 else None,
        "return_20d": float(daily["Close"].iloc[-1] / daily["Close"].iloc[-21] - 1) if len(daily) > 20 else None,
    }


COLUMNS = {
    "code": "종목코드", "name": "종목명", "market": "시장", "date": "기준일", "close": "종가",
    "turnover_1d": "회전율_당일", "turnover_5d": "회전율_5일평균", "turnover_20d": "회전율_20일평균",
    "turnover_60d": "회전율_60일평균", "turnover_sum_20d": "회전율_20일누적",
    "spike": "급증배수(5일/60일)", "return_20d": "20일수익률",
}


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--top", type=int, default=100, help="일봉 지표를 계산할 회전율 상위 종목 수")
    ap.add_argument("--codes", nargs="*", help="종목코드 직접 지정")
    ap.add_argument("--days", type=int, default=120, help="일봉 조회 기간(달력일)")
    ap.add_argument("--out", type=Path, default=OUTPUT_DIR)
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    listing = prices.listing()
    ranked = today_turnover(listing)
    args.out.mkdir(parents=True, exist_ok=True)
    snapshot = ranked[["Code", "Name", "Market", "Close", "Volume", "Amount", "Stocks", "turnover"]]
    snapshot.round(4).rename(columns={
        "Code": "종목코드", "Name": "종목명", "Market": "시장", "Close": "종가", "Volume": "거래량",
        "Amount": "거래대금", "Stocks": "상장주식수", "turnover": "회전율"}).to_csv(
        args.out / "turnover_today.csv", index=False, encoding="utf-8-sig")

    info = ranked.set_index("Code")
    codes = args.codes or list(ranked["Code"].head(args.top))
    start = pd.Timestamp.today() - pd.Timedelta(days=args.days)
    data = prices.daily_many(codes, start, cache_dir=CACHE_DIR)
    rows = []
    for code, daily in data.items():
        if code not in info.index:
            continue
        row = info.loc[code]
        rows.append({"code": code, "name": row["Name"], "market": row["Market"],
                     **turnover_metrics(daily, row["Stocks"])})
    df = pd.DataFrame(rows, columns=list(COLUMNS))
    df = df.sort_values("spike", ascending=False)
    df.round(4).rename(columns=COLUMNS).to_csv(args.out / "turnover_history.csv", index=False, encoding="utf-8-sig")
    print(f"회전율: 전체 {len(ranked)}종목, 상세 {len(df)}종목 -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
