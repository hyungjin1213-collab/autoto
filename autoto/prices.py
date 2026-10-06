"""Korean stock prices: the KRX listing and per-stock daily OHLCV.

Both come from FinanceDataReader (KRX for the listing, Naver for daily bars,
adjusted for splits). Daily bars can be cached on disk so reruns and the
backtest do not download every stock again.
"""

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


def listing(markets=("KOSPI", "KOSDAQ")):
    """Today's KRX listing: code, name, market, last price/volume, listed shares."""
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


def daily_many(codes, start, end=None, cache_dir=None):
    """{code: daily DataFrame}; stocks that fail to download are skipped."""
    result = {}
    for i, code in enumerate(codes, 1):
        try:
            df = daily(code, start, end, cache_dir)
        except Exception as e:  # one bad ticker should not stop a 2,500-stock run
            print(f"  {code} 시세 실패: {e}", file=sys.stderr)
            continue
        if not df.empty:
            result[code] = df
        if i % 200 == 0:
            print(f"  시세 {i}/{len(codes)}", file=sys.stderr)
    return result
