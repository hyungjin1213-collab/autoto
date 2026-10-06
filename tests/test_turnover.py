import pandas as pd
import pytest

from autoto import turnover


def test_today_turnover_ranks_and_skips_missing_shares():
    listing = pd.DataFrame({
        "Code": ["A", "B", "C"], "Name": ["에이", "비", "씨"], "Market": ["KOSPI", "KOSDAQ", "KOSDAQ"],
        "Close": [1, 1, 1], "Volume": [1_000, 50_000, 10], "Amount": [0, 0, 0], "Marcap": [0, 0, 0],
        "Stocks": [100_000, 1_000_000, 0],
    })
    got = turnover.today_turnover(listing)
    assert got["Code"].tolist() == ["B", "A"]
    assert got["turnover"].tolist() == pytest.approx([5.0, 1.0])


def test_turnover_metrics():
    idx = pd.bdate_range("2025-01-01", periods=60)
    volume = [1_000] * 55 + [10_000] * 5
    daily = pd.DataFrame({"Open": 100.0, "High": 100.0, "Low": 100.0,
                          "Close": [100.0] * 39 + [110.0] * 21, "Volume": volume}, index=idx)
    m = turnover.turnover_metrics(daily, shares=100_000)
    assert m["turnover_1d"] == pytest.approx(10.0)
    assert m["turnover_5d"] == pytest.approx(10.0)
    assert m["turnover_60d"] == pytest.approx((55 * 1 + 5 * 10) / 60)
    assert m["turnover_sum_20d"] == pytest.approx(15 * 1 + 5 * 10)
    assert m["spike"] == pytest.approx(10.0 / (105 / 60))
    assert m["return_20d"] == pytest.approx(0.0)  # day -21 is already 110
