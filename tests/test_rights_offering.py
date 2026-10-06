import datetime as dt
import io
import json
import urllib.parse
import zipfile

from autoto import rights_offering as collect

DOC = """<DOCUMENT><BODY>
<P>1. 신주의 종류와 수</P>
<TABLE><TR><TD>보통주식</TD><TD>1,000,000</TD></TR></TABLE>
<P>【제3자배정 대상자별 선정경위, 거래내역, 배정내역 등】</P>
<TABLE BORDER="1">
<TR><TH>제3자배정 대상자</TH><TH>회사 또는 최대주주와의 관계</TH><TH>선정경위</TH>
<TH>증자결정 전후 6월이내 거래내역 및 계획</TH><TH>배정주식수(주)</TH><TH>비 고</TH></TR>
<TR><TD>(주)바이오투자</TD><TD>최대주주</TD><TD>경영안정</TD><TD>-</TD><TD>600,000</TD><TD>1년 보호예수</TD></TR>
<TR><TE>홍길동 &amp; 파트너스</TE><TE>-</TE><TE>투자유치</TE><TE>-</TE><TE>400,000</TE><TE>-</TE></TR>
<TR><TD>합계</TD><TD></TD><TD></TD><TD></TD><TD>1,000,000</TD><TD></TD></TR>
</TABLE></BODY></DOCUMENT>"""


def zipped(text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("20250102000001.xml", text.encode("utf-8"))
    return buf.getvalue()


def fake_fetch(url):
    parsed = urllib.parse.urlparse(url)
    endpoint = parsed.path.rsplit("/", 1)[-1]
    q = dict(urllib.parse.parse_qsl(parsed.query))
    if endpoint == "list.json":
        if q["bgn_de"] != "20250101":
            return json.dumps({"status": "013", "message": "no data"}).encode()
        return json.dumps({"status": "000", "total_page": 1, "list": [
            {"rcept_no": "20250102000001", "corp_code": "001", "corp_name": "에이바이오",
             "stock_code": "123456", "report_nm": "주요사항보고서(유상증자결정)", "rcept_dt": "20250102"},
            {"rcept_no": "20250103000002", "corp_code": "002", "corp_name": "비제약",
             "stock_code": "654321", "report_nm": "주요사항보고서(유상 증자결정)", "rcept_dt": "20250103"},
            {"rcept_no": "20250104000003", "corp_code": "003", "corp_name": "씨전자",
             "report_nm": "주요사항보고서(전환사채권발행결정)", "rcept_dt": "20250104"},
        ]}).encode()
    if endpoint == "piicDecsn.json":
        rows = {
            "001": [{"rcept_no": "20250102000001", "corp_code": "001", "corp_name": "에이바이오",
                     "corp_cls": "K", "ic_mthn": "제3자배정증자", "nstk_ostk_cnt": "1,000,000",
                     "nstk_estk_cnt": "-", "fv_ps": "500", "bfic_tisstk_ostk": "10,000,000",
                     "fdpp_op": "3,000,000,000", "fdpp_fclt": "-"}],
            "002": [{"rcept_no": "20250103000002", "corp_code": "002", "corp_name": "비제약",
                     "corp_cls": "Y", "ic_mthn": "주주배정후 실권주 일반공모"}],
        }[q["corp_code"]]
        return json.dumps({"status": "000", "list": rows}).encode()
    if endpoint == "company.json":
        return json.dumps({"status": "000", "ceo_nm": "김대표", "induty_code": "21210",
                           "stock_code": "123456", "adres": "서울", "hm_url": "", "est_dt": "20000101"}).encode()
    if endpoint == "document.xml":
        return zipped(DOC)
    raise AssertionError(url)


def test_windows_stay_within_three_months():
    wins = list(collect.date_windows(dt.date(2025, 1, 1), dt.date(2025, 12, 31)))
    assert wins[0][0] == dt.date(2025, 1, 1) and wins[-1][1] == dt.date(2025, 12, 31)
    for a, b in wins:
        assert (b - a).days <= 89
    for (_, b), (a, _) in zip(wins, wins[1:]):
        assert (a - b).days == 1


def test_parse_allottees():
    got = collect.parse_allottees(DOC)
    assert got == [
        {"name": "(주)바이오투자", "relation": "최대주주", "shares": 600000, "note": "1년 보호예수"},
        {"name": "홍길동 & 파트너스", "relation": "-", "shares": 400000, "note": "-"},
    ]


def test_collect_end_to_end():
    dart = collect.Dart("KEY", fetch=fake_fetch, pause=0)
    rows, allottees = collect.collect(dart, dt.date(2025, 1, 1), dt.date(2025, 3, 31))
    assert len(rows) == 1
    r = rows[0]
    assert r["회사명"] == "에이바이오" and r["시장"] == "코스닥"
    assert r["신주_보통주"] == 1000000 and r["신주_기타주"] is None
    assert r["조달금액_합계"] == 3000000000
    assert r["자금용도"] == "운영자금 3,000,000,000"
    assert r["배정대상자"] == "(주)바이오투자; 홍길동 & 파트너스"
    assert r["업종코드"] == "21210"
    assert [a["배정대상자"] for a in allottees] == ["(주)바이오투자", "홍길동 & 파트너스"]


def test_write_csv(tmp_path):
    collect.write_csv(tmp_path / "x.csv", [{"회사명": "에이"}])
    assert (tmp_path / "x.csv").read_bytes().startswith(b"\xef\xbb\xbf")
