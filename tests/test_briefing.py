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

SECTORS = """<table class="type_1" summary="업종별 시세 리스트">
<tr><th>업종명</th><th>전일대비</th><th colspan="4">전일대비 등락현황</th><th>등락그래프</th></tr>
<tr><th>전체</th><th>상승</th><th>보합</th><th>하락</th></tr>
<tr>
<td style="padding-left:10px;"><a href="/sise/sise_group_detail.naver?type=upjong&no=278">반도체와반도체장비</a></td>
<td class="number"><span class="tah p11 red01">+3.25%</span></td>
<td class="number">135</td><td class="number">99</td><td class="number">10</td><td class="number">26</td>
<td><div class="graph_bar"></div></td></tr>
<tr>
<td style="padding-left:10px;"><a href="/sise/sise_group_detail.naver?type=upjong&amp;no=999">우주항공</a></td>
<td class="number"><span class="tah p11 red01">+9.10%</span></td>
<td class="number">3</td><td class="number">3</td><td class="number">0</td><td class="number">0</td>
<td></td></tr>
<tr>
<td style="padding-left:10px;"><a href="/sise/sise_group_detail.naver?type=upjong&no=261">제약</a></td>
<td class="number"><span class="tah p11 nv01">-1.02%</span></td>
<td class="number">140</td><td class="number">30</td><td class="number">5</td><td class="number">105</td>
<td></td></tr>
<tr>
<td style="padding-left:10px;"><a href="/sise/sise_group_detail.naver?type=upjong&no=300">은행</a></td>
<td class="number"><span class="tah p11 red01">+1.50%</span></td>
<td class="number">10</td><td class="number">8</td><td class="number">0</td><td class="number">2</td>
<td></td></tr>
</table>"""

DETAIL = """<table class="type_5" summary="업종별 시세 리스트">
<thead><tr><th>종목명</th><th>현재가</th><th>전일비</th><th>등락률</th><th>매수호가</th><th>매도호가</th>
<th>거래량</th><th>거래대금</th><th>전일거래량</th><th>토론실</th></tr></thead>
<tbody>
<tr><td class="name"><div class="name_area"><a href="/item/main.naver?code=000660">SK하이닉스</a></div></td>
<td class="number">200,000</td><td class="number"><span>상승 10,000</span></td>
<td class="number"><span class="tah p11 red01">+5.26%</span></td><td class="number">199,900</td><td class="number">200,000</td>
<td class="number">5,000,000</td><td class="number">1,000,000</td><td class="number">4,000,000</td><td></td></tr>
<tr><td class="name"><div class="name_area"><a href="/item/main.naver?code=042700">한미반도체 *</a></div></td>
<td class="number">100,000</td><td class="number">0</td>
<td class="number"><span class="tah p11 red01">+7.30%</span></td><td class="number">1</td><td class="number">1</td>
<td class="number">1,000,000</td><td class="number">100,000</td><td class="number">1</td><td></td></tr>
<tr><td class="name"><div class="name_area"><a href="/item/main.naver?code=005930">삼성전자</a></div></td>
<td class="number">70,000</td><td class="number">0</td>
<td class="number"><span class="tah p11 red01">+2.00%</span></td><td class="number">1</td><td class="number">1</td>
<td class="number">20,000,000</td><td class="number">1,400,000</td><td class="number">1</td><td></td></tr>
<tr><td class="name"><div class="name_area"><a href="/item/main.naver?code=000990">DB하이텍</a></div></td>
<td class="number">50,000</td><td class="number">0</td>
<td class="number"><span class="tah p11 nv01">-1.00%</span></td><td class="number">1</td><td class="number">1</td>
<td class="number">10,000</td><td class="number">500</td><td class="number">1</td><td></td></tr>
</tbody></table>"""


def test_parse_fchart():
    df = briefing.parse_fchart(FCHART)
    assert list(df.index.strftime("%Y-%m-%d")) == ["2026-10-07", "2026-10-08"]
    assert df["Close"].iloc[-1] == 2636.35


def test_index_summary_change():
    got = briefing.index_summary(fetch=lambda url: FCHART)
    assert got["코스피"]["date"] == dt.date(2026, 10, 8)
    assert got["코스피"]["change"] == pytest.approx((2636.35 / 2610.25 - 1) * 100)


def test_parse_sectors():
    s = briefing.parse_sectors(SECTORS)
    assert [x["name"] for x in s] == ["반도체와반도체장비", "우주항공", "제약", "은행"]
    assert s[0] == {"no": "278", "name": "반도체와반도체장비", "change": 3.25, "count": 135}
    assert s[1]["no"] == "999" and s[2]["change"] == -1.02


def test_parse_sector_stocks():
    rows = briefing.parse_sector_stocks(DETAIL)
    assert [r["name"] for r in rows] == ["SK하이닉스", "한미반도체", "삼성전자", "DB하이텍"]  # 코스닥 * 제거
    assert rows[0]["change"] == 5.26 and rows[0]["amount"] == 1_000_000


def test_leading_sectors_skips_tiny_sectors_and_orders_leaders_by_amount():
    pages = {briefing.SECTORS_URL: SECTORS}

    def fetch(url):
        return pages.get(url, DETAIL)

    got = briefing.leading_sectors(fetch=fetch, top=2)
    assert [s["name"] for s in got] == ["반도체와반도체장비", "은행"]  # 우주항공(3종목) 제외
    assert [x["name"] for x in got[0]["leaders"]] == ["삼성전자", "SK하이닉스", "한미반도체"]


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
        errors=["주도 섹터"])
    assert "장 마감 브리핑" in text and "2026-10-12 (월)" in text
    assert "반도체&lt;장비&gt; <b>+3.2%</b>" in text  # HTML 이스케이프
    assert "✅ 이수화학" in text and "목표가 도달 1" in text
    assert '<a href="https://dart">엑스플러스</a> 30억' in text
    assert "주도 섹터 불러오기 실패" in text
    parts = briefing.split_message(text, limit=200)
    assert all(len(p) <= 200 for p in parts) and "\n".join(parts) == text
