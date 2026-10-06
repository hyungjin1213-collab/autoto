import datetime as dt

import pandas as pd
import pytest

from autoto import monthly_candle as mc


def make_daily(upsides, start="2025-01", price=10000, volume=100_000):
    """Flat stock whose month m peaks at price*(1+upsides[m]) on its 3rd trading day."""
    frames = []
    for i, up in enumerate(upsides):
        month = pd.Period(start, "M") + i
        days = pd.bdate_range(month.start_time, month.end_time)
        high = [price * 1.01] * len(days)
        high[2] = price * (1 + up)
        frames.append(pd.DataFrame({"Open": price, "High": high, "Low": price * 0.99,
                                    "Close": price, "Volume": volume}, index=days))
    return pd.concat(frames).astype(float)


def test_tick_up():
    assert mc.tick_up(1999.2) == 2000
    assert mc.tick_up(2000) == 2000
    assert mc.tick_up(10500) == 10500
    assert mc.tick_up(10501) == 10510
    assert mc.tick_up(52345) == 52400
    assert mc.tick_up(600001) == 601000


def test_monthly_bars():
    bars = mc.monthly_bars(make_daily([0.06, 0.02]))
    assert list(bars.index.astype(str)) == ["2025-01", "2025-02"]
    assert bars["High"].tolist() == [10600, 10200]
    assert bars["Open"].tolist() == [10000, 10000]
    assert bars["Amount"].iloc[0] == 10000 * 100_000


def days(rows):
    idx = pd.bdate_range("2025-03-03", periods=len(rows))
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx).astype(float)


def test_simulate_hits_target_intraday():
    t = mc.simulate_month(days([(10000, 10300, 9900, 10100), (10100, 10600, 10000, 10400),
                                (10400, 10400, 9000, 9000)]), 0.05, 0.0)
    assert t["hit"] and t["goal"] == 10500 and t["exit"] == 10500
    assert t["exit_date"] == dt.date(2025, 3, 4) and t["days_held"] == 2
    assert t["ret"] == pytest.approx(0.05)


def test_simulate_gap_up_fills_at_open():
    t = mc.simulate_month(days([(10000, 10100, 9900, 10000), (11000, 11200, 10900, 11000)]), 0.05, 0.0)
    assert t["hit"] and t["exit"] == 11000


def test_simulate_miss_sells_at_month_end_close():
    t = mc.simulate_month(days([(10000, 10300, 9900, 10100), (10100, 10200, 9500, 9600)]), 0.05, 0.0025)
    assert not t["hit"] and t["exit"] == 9600
    assert t["ret"] == pytest.approx(9600 / 10000 - 1 - 0.0025)


def test_backtest_uses_only_past_months():
    p = mc.Params(lookback=3, cost=0.0)
    # A hits 6% every month except 2025-05; B never reaches 5%.
    a = make_daily([0.06, 0.06, 0.06, 0.06, 0.02, 0.06, 0.06, 0.06, 0.06])
    b = make_daily([0.02] * 9)
    trades = mc.backtest({"A": a, "B": b}, p, today="2025-12-15")
    sel = trades[trades["code"] == "A"].set_index("month")["selected"]
    # 2025-05 is still picked (its own bad month is not known yet),
    # the next 3 months see the miss in their lookback, 2025-09 is clean again.
    assert sel.to_dict() == {"2025-04": True, "2025-05": True, "2025-06": False,
                             "2025-07": False, "2025-08": False, "2025-09": True}
    assert not trades[trades["code"] == "B"]["selected"].any()
    a_may = trades[(trades["code"] == "A") & (trades["month"] == "2025-05")].iloc[0]
    assert not a_may["hit"] and a_may["ret"] == pytest.approx(0.0)


def test_backtest_skips_unfinished_month_and_illiquid():
    p = mc.Params(lookback=2)
    trades = mc.backtest({"A": make_daily([0.06] * 4)}, p, today="2025-04-10")
    assert trades["month"].tolist() == ["2025-03"]
    thin = make_daily([0.06] * 4, volume=100)  # 100만원/일 < 1억
    assert mc.backtest({"T": thin}, p, today="2025-12-01").empty


def test_backtest_requires_full_lookback():
    a = make_daily([0.06] * 5)
    a = a[a.index.to_period("M") != pd.Period("2025-02", "M")]  # a month with no trading
    trades = mc.backtest({"A": a}, mc.Params(lookback=2), today="2025-12-01")
    assert trades["month"].tolist() == ["2025-05"]


def test_summarize():
    p = mc.Params(lookback=2, cost=0.0)
    trades = mc.backtest({"A": make_daily([0.06] * 5), "B": make_daily([0.02] * 5)}, p, today="2025-12-01")
    summary, monthly = mc.summarize(trades)
    assert monthly["전략"].tolist() == pytest.approx([0.05, 0.05, 0.05])
    assert monthly["기준선"].tolist() == pytest.approx([0.025, 0.025, 0.025])
    assert summary.loc["거래수", "전략(선별)"] == 3
    assert summary.loc["목표도달률", "전략(선별)"] == 1.0
    assert summary.loc["누적수익률", "전략(선별)"] == pytest.approx(1.05 ** 3 - 1)
    assert summary.loc["후보있던달", "전략(선별)"] == 3


def test_screen_current_month():
    p = mc.Params(lookback=3)
    data = {"A": make_daily([0.06, 0.06, 0.06, 0.06]), "B": make_daily([0.06, 0.02, 0.06, 0.06]),
            "S": make_daily([0.06] * 3, price=1000) }  # 거래대금 부족
    data["S"]["Volume"] = 10
    got = mc.screen(data, p, "2025-04")
    assert got["code"].tolist() == ["A"]
    row = got.iloc[0]
    assert row["entry"] == 10000 and row["goal"] == 10500 and bool(row["reached"])
    # Next month: no data yet, so no entry price columns but A still qualifies.
    nxt = mc.screen(data, p, "2025-05")
    assert nxt["code"].tolist() == ["A"] and pd.isna(nxt.iloc[0].get("entry", float("nan")))


def test_sideways_filter():
    rising = make_daily([0.06] * 4)
    rising["Close"] = rising["Open"] = rising["Open"] * pd.Series(
        [1.0 + 0.15 * i for i in range(4)], index=pd.period_range("2025-01", periods=4, freq="M")
    ).reindex(rising.index.to_period("M")).to_numpy()
    rising["High"] = rising["Open"] * 1.06
    got = mc.screen({"R": rising}, mc.Params(lookback=3), "2025-05")
    assert got.empty  # 종가가 3개월간 1.15 -> 1.45 (26%) 움직여 횡보 아님
