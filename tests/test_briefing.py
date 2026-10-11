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
