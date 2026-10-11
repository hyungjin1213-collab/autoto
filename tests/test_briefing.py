import datetime as dt

import numpy as np
import pandas as pd
import pytest

from autoto import briefing

FCHART = """<?xml version="1.0" encoding="EUC-KR" ?>
<protocol><chartdata symbol="KOSPI" name="코스피" count="3" timeframe="day" precision="2" origintime="19900103">
<item data="20261007|2600.5|2620|2590|2610.25|400000" />
<item data="20261008|2610|2650|2600|2636.35|450000" />
</chartdata></protocol>"""

# m.stock.naver.com/api/stocks/industry 응답 형태 (2026-10 확인)
GROUPS = {"stockListSortType": "INDUSTRY", "groups": [
    {"no": 332, "name": "문구류", "totalCount": 1, "changeRate": "5.03", "riseCount": 1, "fallCount": 0, "steadyCount": 0},
    {"no": 278, "name": "반도체와반도체장비", "totalCount": 135, "changeRate": "3.25", "riseCount": 99, "fallCount": 26,
     "steadyCount": 10},
    {"no": 261, "name": "제약", "totalCount": 140, "changeRate": "-1.02", "riseCount": 30, "fallCount": 105,
     "steadyCount": 5},
    {"no": 300, "name": "은행", "totalCount": 10, "changeRate": "1.50", "riseCount": 8, "fallCount": 2, "steadyCount": 0},
], "totalCount": 79, "page": 1, "pageSize": 300, "marketStatus": "CLOSE"}


def stock(code, name, ratio, value):
    return {"stockType": "domestic", "itemCode": code, "stockName": name, "closePrice": "11,290",
            "fluctuationsRatio": ratio, "accumulatedTradingVolume": "387,121", "accumulatedTradingValue": value,
            "marketValue": "904"}


GROUP_STOCKS = {"stockListSortType": "INDUSTRY", "stocks": [
    stock("000660", "SK하이닉스", "5.26", "1,000,000"), stock("042700", "한미반도체", "7.30", "100,000"),
    stock("005930", "삼성전자", "2.00", "1,400,000"), stock("000990", "DB하이텍", "-1.00", "500")]}


def test_parse_fchart():
    df = briefing.parse_fchart(FCHART)
    assert list(df.index.strftime("%Y-%m-%d")) == ["2026-10-07", "2026-10-08"]
    assert df["Close"].iloc[-1] == 2636.35


def test_index_summary_change():
    got = briefing.index_summary(fetch=lambda url: FCHART)
    assert got["코스피"]["date"] == dt.date(2026, 10, 8)
    assert got["코스피"]["change"] == pytest.approx((2636.35 / 2610.25 - 1) * 100)


def test_parse_groups_and_stocks():
    g = briefing.parse_groups(GROUPS)
    assert g[1] == {"no": 278, "name": "반도체와반도체장비", "change": 3.25, "count": 135}
    st = briefing.parse_group_stocks(GROUP_STOCKS)
    assert st[0] == {"code": "000660", "name": "SK하이닉스", "change": 5.26, "amount": 1_000_000}


def test_leading_groups_skips_tiny_groups_and_orders_leaders_by_amount():
    urls = []

    # 4개 그룹을 2페이지로 나눠 응답
    page1 = {**GROUPS, "groups": GROUPS["groups"][:2], "totalCount": 4}
    page2 = {**GROUPS, "groups": GROUPS["groups"][2:], "totalCount": 4}

    def fetch(url):
        urls.append(url)
        if "/theme?page=1" in url:
            return page1
        if "/theme?page=2" in url:
            return page2
        return GROUP_STOCKS

    got = briefing.leading_groups("theme", fetch=fetch, top=2)
    assert [g["name"] for g in got] == ["반도체와반도체장비", "은행"]  # 1종목짜리 문구류 제외
    assert [x["name"] for x in got[0]["leaders"]] == ["삼성전자", "SK하이닉스", "한미반도체"]
    assert urls[:3] == ["https://m.stock.naver.com/api/stocks/theme?page=1&pageSize=50",
                        "https://m.stock.naver.com/api/stocks/theme?page=2&pageSize=50",
                        "https://m.stock.naver.com/api/stocks/theme/278?page=1&pageSize=50"]


def test_leading_groups_fails_on_empty_list():
    with pytest.raises(RuntimeError):
        briefing.leading_groups(fetch=lambda url: {"groups": [], "totalCount": 0})


def test_rsi_extremes():
    up = pd.Series(np.arange(1, 40, dtype=float))
    down = pd.Series(np.arange(40, 1, -1, dtype=float))
    assert briefing.rsi(up).iloc[-1] == pytest.approx(100)
    assert briefing.rsi(down).iloc[-1] == pytest.approx(0)


def daily(closes):
    idx = pd.bdate_range("2026-06-01", periods=len(closes))
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": 1000.0})


def test_oversold_filters_and_sorts_by_rsi():
    falling = daily(list(range(100, 60, -1)))                         # RSI 0
    mild = daily([100 + (i % 2) * 2 - i * 0.5 for i in range(40)])    # 완만한 하락
    rising = daily(list(range(60, 100)))
    data = {"A": rising, "B": mild, "C": falling}
    universe = pd.DataFrame({"Code": ["A", "B", "C"], "Name": ["상승", "완만", "급락"], "Market": "KOSPI"})
    rows, latest = briefing.oversold(universe, max_rsi=45, fetch_daily=lambda codes, start: data)
    assert [r["name"] for r in rows][0] == "급락"
    assert "상승" not in [r["name"] for r in rows]
    assert rows[0]["disparity"] < 100 and rows[0]["return_5d"] < 0
    assert latest == falling.index[-1].date()


def test_monthly_candidates_refreshes_target_hit(tmp_path):
    path = tmp_path / "c.csv"
    pd.DataFrame({"종목코드": ["000001", "000002"], "종목명": ["가", "나"], "적중률": [1.0, 1.0],
                  "일평균거래대금": [2e8, 1e8], "이번달시가": [1000, 2000], "매도목표가": [1050, 2100],
                  "이번달고가": [1000, 2000], "목표도달": [False, False]}).to_csv(path, index=False)
    data = {"000001": daily([1000, 1060, 1040]), "000002": daily([2000, 2050, 2020])}
    got = briefing.monthly_candidates(path, fetch_daily=lambda codes, start: data)
    hit = {r["종목명"]: r["목표도달"] for r in got}
    assert hit == {"가": True, "나": False}


def test_recent_allotments_filters_by_date(tmp_path):
    path = tmp_path / "r.csv"
    pd.DataFrame({"접수번호": ["20261008000001", "20261010000002"], "접수일": ["20261008", "20261010"],
                  "회사명": ["옛날", "새로"], "공시링크": ["u1", "u2"]}).to_csv(path, index=False)
    got = briefing.recent_allotments(dt.date(2026, 10, 9), path)
    assert [r["회사명"] for r in got] == ["새로"]


def test_previous_weekday_skips_weekend():
    assert briefing.previous_weekday(dt.date(2026, 10, 12)) == dt.date(2026, 10, 9)  # 월 -> 금
    assert briefing.previous_weekday(dt.date(2026, 10, 8)) == dt.date(2026, 10, 7)


def test_build_message_and_split():
    now = dt.datetime(2026, 10, 12, 16, 10, tzinfo=briefing.KST)
    text = briefing.build_message(
        "close", now,
        {"코스피": {"date": now.date(), "close": 2636.35, "change": 1.0}},
        [{"name": "반도체<장비>", "change": 3.25, "leaders": [{"name": "SK하이닉스", "change": 5.26}]}],
        [{"종목명": "덕우전자", "급증배수(5일/60일)": 10.7, "20일수익률": 0.35}],
        [{"name": "급락", "rsi": 18.2, "disparity": 85.0, "return_5d": -9.0}],
        [{"종목명": "이수화학", "이번달시가": 11220, "매도목표가": 11790, "목표도달": True}],
        [{"회사명": "엑스플러스", "조달금액_합계": 3e9, "배정대상자": "홍길동; (주)회사", "공시링크": "https://dart"}],
        errors=["주도 업종"], themes=[{"name": "리비안", "change": 3.51}])
    assert "장 마감 브리핑" in text and "2026-10-12 (월)" in text
    assert "반도체&lt;장비&gt; <b>+3.2%</b>" in text  # HTML 이스케이프
    assert "✅ 이수화학" in text and "목표가 도달 1" in text
    assert '<a href="https://dart">엑스플러스</a> 30억' in text
    assert "주도 업종 불러오기 실패" in text
    assert "🎯 <b>주도 테마</b>\n1. 리비안 <b>+3.5%</b>" in text
    parts = briefing.split_message(text, limit=200)
    assert all(len(p) <= 200 for p in parts) and "\n".join(parts) == text


def bars(n=130, base_at=None, base=None, last_close=98.0, low=100.0, vol=1000.0):
    """평평한 일봉 (시가=종가=100, 저가 low) + 선택적 기준봉 + 마지막날 종가."""
    idx = pd.bdate_range("2026-03-02", periods=n)
    df = pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": low, "Close": 100.0, "Volume": vol}, index=idx)
    if base_at is not None:
        i = n - 1 - base_at
        for k, val in base.items():
            df.iloc[i, df.columns.get_loc(k)] = val
        df.iloc[i + 1:, df.columns.get_loc("Close")] = base["Close"]  # 이후 기준봉 종가 근처 유지
        df.iloc[i + 1:, df.columns.get_loc("Open")] = base["Close"]
    df.iloc[-1, df.columns.get_loc("Close")] = last_close
    return df


GOOD = {"Open": 101.0, "High": 120.0, "Low": 99.0, "Close": 110.0, "Volume": 10_000.0}  # 고가+20%, 종가+10%, 10배


def test_bottom_break_detects_full_bodied_volume_candle_and_open_break():
    m = briefing.bottom_break(bars(base_at=10, base=GOOD))
    assert m["days_ago"] == 10 and m["base_open"] == 101.0
    assert m["base_gain"] == pytest.approx(10.0) and m["base_high_gain"] == pytest.approx(20.0)
    assert m["vol_mult"] == pytest.approx(10.0)
    assert m["below_open"] == pytest.approx((98 / 101 - 1) * 100)


@pytest.mark.parametrize("change, why", [
    ({"Close": 103.0}, "고가 +20%에 종가 +3%: 봉이 차 있지 않음"),
    ({"Volume": 5_000.0}, "거래량 5배"),
    ({"High": 104.0, "Close": 104.0}, "종가 +4%: 장대양봉 아님"),
])
def test_bottom_break_rejects_weak_candles(change, why):
    assert briefing.bottom_break(bars(base_at=10, base={**GOOD, **change})) is None, why


def test_bottom_break_needs_open_break_lookback_and_bottom():
    assert briefing.bottom_break(bars(base_at=10, base=GOOD, last_close=102.0)) is None  # 시가 위
    assert briefing.bottom_break(bars(base_at=45, base=GOOD)) is None                     # 40거래일 밖
    high_up = bars(base_at=10, base=GOOD, low=80.0)  # 직전 저가 80 -> 기준봉 저가 99는 바닥이 아님
    assert briefing.bottom_break(high_up) is None
    assert briefing.bottom_break(bars(base_at=10, base=GOOD), briefing.BottomParams(vol_mult=12)) is None


def test_bottom_breaks_orders_by_recency():
    data = {"A": bars(base_at=20, base=GOOD), "B": bars(base_at=5, base=GOOD), "C": bars()}
    universe = pd.DataFrame({"Code": ["A", "B", "C"], "Name": ["에이", "비", "씨"], "Market": "KOSDAQ"})
    rows = briefing.bottom_breaks(universe, data)
    assert [r["name"] for r in rows] == ["비", "에이"]


def test_message_shows_bottom_section():
    now = dt.datetime(2026, 10, 12, 16, 10, tzinfo=briefing.KST)
    rows = [{"name": "가나다", "base_date": dt.date(2026, 10, 2), "base_gain": 12.0, "vol_mult": 9.5,
             "below_open": -3.1}]
    text = briefing.build_message("close", now, {}, [], [], [], [], [], bottoms=rows)
    assert "🧲 <b>과매도 (거래량 바닥 시가 이탈)</b>" in text
    assert "• 가나다 10/02 기준봉 +12.0%·거래량 10배 → 시가 대비 -3.1%" in text
    assert "🧲" not in briefing.build_message("close", now, {}, [], [], [], [], [])


def vol_series(vols, closes=None):
    idx = pd.bdate_range("2025-01-02", periods=len(vols))
    c = pd.Series(closes if closes is not None else [100.0] * len(vols), index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": vols}, index=idx, dtype=float)


def test_volume_record_only_when_last_day_beats_all_history():
    vols = [1000.0] * 150 + [5000.0] + [1000.0] * 20 + [6000.0]
    closes = [100.0] * 171 + [110.0]
    r = briefing.volume_record(vol_series(vols, closes))
    assert r["vs_prev_record"] == pytest.approx(1.2) and r["change"] == pytest.approx(10.0)
    assert r["prev_record_date"] == vol_series(vols).index[150].date()
    assert r["amount"] == 6000 * 110
    assert briefing.volume_record(vol_series(vols[:-1] + [5000.0])) is None   # 기록 타이는 제외
    assert briefing.volume_record(vol_series([1000.0] * 50 + [9000.0])) is None  # 상장 120거래일 미만


def test_fetch_full_trims_history_and_collects_records():
    full = {"A": vol_series([1000.0] * 300 + [9000.0]), "B": vol_series([1000.0] * 300)}

    def daily(code):
        if code == "X":
            raise ValueError("boom")
        return full[code]

    recent, records = briefing.fetch_full(["A", "B", "X"], keep=200, daily=daily)
    assert set(recent) == {"A", "B"} and len(recent["A"]) == 200
    assert set(records) == {"A"}


def test_record_volume_rows_keeps_latest_day_sorted_by_amount():
    d1, d0 = dt.date(2026, 10, 8), dt.date(2026, 10, 7)
    recs = {"A": {"date": d1, "amount": 1e9}, "B": {"date": d1, "amount": 5e9}, "C": {"date": d0, "amount": 9e9}}
    universe = pd.DataFrame({"Code": ["A", "B", "C"], "Name": ["에이", "비", "씨"], "Market": "KOSPI"})
    assert [r["name"] for r in briefing.record_volume_rows(universe, recs)] == ["비", "에이"]


def test_message_shows_record_section():
    now = dt.datetime(2026, 10, 12, 16, 10, tzinfo=briefing.KST)
    rows = [{"name": "가나다", "change": 15.0, "amount": 123e8, "prev_record_date": dt.date(2021, 3, 4),
             "vs_prev_record": 1.8}]
    text = briefing.build_message("close", now, {}, [], [], [], [], [], records=rows)
    assert "🚀 <b>상장 이래 최대 거래량</b>\n• 가나다 +15.0%, 거래대금 123억 (종전 최대 21.03.04의 1.8배)" in text
