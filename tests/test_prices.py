import pandas as pd
from autoto import prices

PAGE = """<table class="type_2"><thead><tr>
<th>N</th><th>종목명</th><th>현재가</th><th>전일비</th><th>등락률</th><th>액면가</th><th>시가총액</th>
<th>상장주식수</th><th>외국인비율</th><th>거래량</th><th>PER</th><th>ROE</th><th>토론실</th></tr></thead>
<tbody><tr><td colspan="10" class="blank_08"></td></tr>
<tr><td class="no">1</td><td><a href="/item/main.naver?code=005930" class="tltle">삼성전자</a></td>
<td class="number">71,000</td><td class="number"><span>상승 500</span></td><td class="number">+0.71%</td>
<td class="number">100</td><td class="number">4,238,500</td><td class="number">5,969,783</td>
<td class="number">55.20</td><td class="number">12,345,678</td><td class="number">15.1</td><td class="number">8.0</td>
<td><a href="/item/board.naver?code=005930"><img></a></td></tr>
<tr><td class="no">2</td><td><a href="/item/main.naver?code=069500" class="tltle">KODEX 200</a></td>
<td class="number">35,000</td><td class="number">0</td><td class="number">0.00%</td>
<td class="number">0</td><td class="number">70,000</td><td class="number">200,000</td>
<td class="number">0</td><td class="number">1,000</td><td class="number">N/A</td><td class="number">N/A</td><td></td></tr>
</tbody></table>"""


def test_parse_market_sum():
    rows = prices.parse_market_sum(PAGE, "KOSPI")
    assert [r["Code"] for r in rows] == ["005930", "069500"]
    r = rows[0]
    assert r["Name"] == "삼성전자" and r["Close"] == 71000 and r["Volume"] == 12345678
    assert r["Stocks"] == 5_969_783_000 and r["Marcap"] == 4_238_500e8


def test_naver_listing_paginates_and_drops_etfs():
    calls = []

    def fetch(url):
        calls.append(url)
        return PAGE if url.endswith("page=1") else PAGE  # 2쪽부터는 같은 종목이 반복됨

    df = prices.naver_listing(["KOSPI"], fetch=fetch)
    assert df["Code"].tolist() == ["005930"]
    assert len(calls) == 2
    assert df.loc[0, "Amount"] == 71000 * 12345678


def test_daily_many_keeps_order_and_skips_failures(monkeypatch):
    import pandas as pd

    def fake_daily(code, start, end=None, cache_dir=None):
        if code == "BAD":
            raise ValueError("boom")
        idx = pd.bdate_range("2025-01-01", periods=2)
        return pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1.0}, index=idx)

    monkeypatch.setattr(prices, "daily", fake_daily)
    got = prices.daily_many(["C", "BAD", "A", "B"], "2025-01-01")
    assert list(got) == ["C", "A", "B"]


def test_daily_cache_refetches_when_earlier_start_requested(tmp_path, monkeypatch):
    import pandas as pd

    calls = []

    class FakeFdr:
        @staticmethod
        def DataReader(code, start, end=None):
            calls.append(pd.Timestamp(start))
            idx = pd.bdate_range(start, "2026-09-30")
            return pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1.0}, index=idx)

    monkeypatch.setattr(prices, "_fdr", lambda: FakeFdr)
    short = prices.daily("000001", "2026-02-01", cache_dir=tmp_path)
    again = prices.daily("000001", "2026-05-01", cache_dir=tmp_path)    # 캐시 범위 안 -> 재사용
    longer = prices.daily("000001", "2021-01-01", cache_dir=tmp_path)   # 캐시보다 이르다 -> 다시 받기
    assert calls == [pd.Timestamp("2026-02-01"), pd.Timestamp("2021-01-01")]
    assert again.index[0] == pd.Timestamp("2026-05-01")
    assert longer.index[0] == pd.Timestamp("2021-01-01") and len(longer) > len(short)


def test_naver_quotes_pages_through_industries_and_stocks():
    def stock(code, close, vol, value, cap):
        return {"itemCode": code, "stockName": code, "closePrice": close, "accumulatedTradingVolume": vol,
                "accumulatedTradingValue": value, "marketValue": cap}

    pages = {
        "https://m.stock.naver.com/api/stocks/industry?page=1&pageSize=50":
            {"groups": [{"no": 1}, {"no": 2}], "totalCount": 2},
        "https://m.stock.naver.com/api/stocks/industry/1?page=1&pageSize=50":
            {"stocks": [stock("005930", "71,000", "12,345,678", "876,543", "4,238,500")]},
        "https://m.stock.naver.com/api/stocks/industry/2?page=1&pageSize=50":
            {"stocks": [stock("000660", "200,000", "1,000", "200", "1,456,000")]},
    }
    q = prices.naver_quotes(fetch=lambda url: pages[url])
    assert q.loc["005930", "Close"] == 71000 and q.loc["005930", "Volume"] == 12345678
    assert q.loc["005930", "Amount"] == 876543e6 and q.loc["005930", "Marcap"] == 4238500e8
    assert q.loc["000660", "Close"] == 200000


def test_fill_quotes_fills_prices_and_missing_shares():
    listing = pd.DataFrame({"Code": ["A", "B"], "Name": ["에이", "비"], "Market": "KOSPI", "Close": [None, None],
                            "Volume": [None, None], "Amount": [None, None], "Marcap": [0.0, 0.0],
                            "Stocks": [1000.0, 0.0]})
    quotes = pd.DataFrame({"Close": [10.0, 20.0], "Volume": [5.0, 6.0], "Amount": [50.0, 120.0],
                           "Marcap": [1e4, 4e4]}, index=["A", "B"])
    got = prices.fill_quotes(listing, quotes).set_index("Code")
    assert got.loc["A", "Close"] == 10 and got.loc["B", "Amount"] == 120
    assert got.loc["A", "Stocks"] == 1000 and got.loc["B", "Stocks"] == 2000  # 4e4 / 20
