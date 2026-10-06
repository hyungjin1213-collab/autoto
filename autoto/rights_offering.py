"""Collect third-party allotment rights offerings (유상증자 제3자배정) from DART.

Flow:
  1. list.json      - 주요사항보고서 중 "유상증자결정" 공시 목록 (3개월 단위로 나눠 조회)
  2. piicDecsn.json - 회사별 유상증자 결정 상세, 증자방식이 제3자배정인 건만 남김
  3. company.json   - 해당 회사 기본정보 (대표자, 업종코드, 주소, 홈페이지 등)
  4. document.xml   - 공시 원문에서 제3자배정 대상자 표 추출

Usage:
  DART_API_KEY=... python -m autoto.rights_offering --days 365
  DART_API_KEY=... python -m autoto.rights_offering --start 20250101 --end 20250930
"""

import argparse
import csv
import datetime as dt
import html
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

API = "https://opendart.fss.or.kr/api"
OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output" / "rights_offering"
REQUEST_PAUSE = 0.15  # DART allows ~1,000 calls/min; stay well under it.

# list.json without corp_code only accepts windows of up to 3 months.
WINDOW_DAYS = 89

CORP_CLS = {"Y": "유가증권", "K": "코스닥", "N": "코넥스", "E": "기타"}

FUND_PURPOSES = {
    "fdpp_fclt": "시설자금",
    "fdpp_bsninh": "영업양수자금",
    "fdpp_op": "운영자금",
    "fdpp_dtrp": "채무상환자금",
    "fdpp_ocsa": "타법인증권취득자금",
    "fdpp_etc": "기타자금",
}


class DartError(RuntimeError):
    pass


def http_get(url):
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read()


class Dart:
    def __init__(self, api_key, fetch=http_get, pause=REQUEST_PAUSE):
        self.api_key = api_key
        self.fetch = fetch
        self.pause = pause

    def _url(self, endpoint, params):
        query = urllib.parse.urlencode({"crtfc_key": self.api_key, **params})
        return f"{API}/{endpoint}?{query}"

    def _get(self, endpoint, params):
        raw = self.fetch(self._url(endpoint, params))
        time.sleep(self.pause)
        return raw

    def json(self, endpoint, params):
        data = json.loads(self._get(endpoint, params).decode("utf-8"))
        status = data.get("status")
        if status == "013":  # 조회된 데이터가 없습니다
            return {"list": []}
        if status != "000":
            raise DartError(f"{endpoint} {params}: [{status}] {data.get('message')}")
        return data

    def document(self, rcept_no):
        raw = self._get("document.xml", {"rcept_no": rcept_no})
        if not raw.startswith(b"PK"):
            # Errors come back as XML/JSON instead of a zip.
            raise DartError(f"document.xml {rcept_no}: {raw[:200]!r}")
        parts = []
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for name in zf.namelist():
                parts.append(decode(zf.read(name)))
        return "\n".join(parts)


def decode(raw):
    for enc in ("utf-8", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def date_windows(start, end):
    cur = start
    while cur <= end:
        stop = min(cur + dt.timedelta(days=WINDOW_DAYS), end)
        yield cur, stop
        cur = stop + dt.timedelta(days=1)


def ymd(d):
    return d.strftime("%Y%m%d")


def list_rights_offering_filings(dart, start, end):
    """All 유상증자결정 filings (incl. 정정) in the period, keyed by rcept_no."""
    filings = {}
    for bgn, stop in date_windows(start, end):
        page = 1
        while True:
            data = dart.json("list.json", {
                "bgn_de": ymd(bgn),
                "end_de": ymd(stop),
                "pblntf_detail_ty": "B001",  # 주요사항보고서
                "page_no": page,
                "page_count": 100,
            })
            for item in data.get("list", []):
                if "유상증자결정" in item.get("report_nm", "").replace(" ", ""):
                    filings[item["rcept_no"]] = item
            if page >= int(data.get("total_page") or 1):
                break
            page += 1
        print(f"  {ymd(bgn)}~{ymd(stop)}: 누적 {len(filings)}건", file=sys.stderr)
    return filings


def to_int(value):
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 공시 원문에서 제3자배정 대상자 표 추출

TABLE_RE = re.compile(r"<TABLE\b.*?</TABLE>", re.I | re.S)
ROW_RE = re.compile(r"<TR\b.*?</TR>", re.I | re.S)
CELL_RE = re.compile(r"<(TD|TH|TE|TU)\b[^>]*>(.*?)</\1>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")


def clean(text):
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def table_rows(table_html):
    rows = []
    for row in ROW_RE.findall(table_html):
        rows.append([clean(c[1]) for c in CELL_RE.findall(row)])
    return [r for r in rows if any(r)]


def find_col(header, *keywords):
    for i, h in enumerate(header):
        h = h.replace(" ", "")
        if all(k in h for k in keywords):
            return i
    return None


def parse_allottees(doc):
    """Return [{name, relation, shares, note}] from the 제3자배정 대상자 table.

    The filing has a section like
      【제3자배정 대상자별 선정경위, 거래내역, 배정내역 등】
    followed by a table whose first column is the allottee.
    """
    anchor = re.search(r"제\s*3\s*자\s*배정\s*대상자\s*별", doc)
    search_from = anchor.start() if anchor else 0

    for table in TABLE_RE.finditer(doc, search_from):
        rows = table_rows(table.group(0))
        if len(rows) < 2:
            continue
        header = rows[0]
        name_col = find_col(header, "대상자")
        shares_col = find_col(header, "배정주식")
        if name_col is None or shares_col is None:
            if anchor:
                continue
            break
        relation_col = find_col(header, "관계")
        note_col = find_col(header, "비고")
        result = []
        for row in rows[1:]:
            if len(row) <= max(name_col, shares_col):
                continue
            name = row[name_col]
            if not name or name in ("-", "합계", "합 계", "계"):
                continue
            result.append({
                "name": name,
                "relation": row[relation_col] if relation_col is not None and relation_col < len(row) else "",
                "shares": to_int(row[shares_col]),
                "note": row[note_col] if note_col is not None and note_col < len(row) else "",
            })
        return result
    return []


# ---------------------------------------------------------------------------


def collect(dart, start, end, with_allottees=True):
    filings = list_rights_offering_filings(dart, start, end)
    corp_codes = sorted({f["corp_code"] for f in filings.values()})
    print(f"유상증자결정 공시 {len(filings)}건 / 회사 {len(corp_codes)}곳", file=sys.stderr)

    decisions = []
    for i, corp_code in enumerate(corp_codes, 1):
        data = dart.json("piicDecsn.json", {
            "corp_code": corp_code,
            "bgn_de": ymd(start),
            "end_de": ymd(end),
        })
        for row in data.get("list", []):
            if "제3자" in (row.get("ic_mthn") or "").replace(" ", ""):
                decisions.append(row)
        if i % 50 == 0:
            print(f"  상세 조회 {i}/{len(corp_codes)}", file=sys.stderr)
    print(f"제3자배정 {len(decisions)}건", file=sys.stderr)

    companies = {}
    for corp_code in sorted({d["corp_code"] for d in decisions}):
        try:
            companies[corp_code] = dart.json("company.json", {"corp_code": corp_code})
        except DartError as e:
            print(f"  회사정보 실패: {e}", file=sys.stderr)
            companies[corp_code] = {}

    allottees = []
    rows = []
    for d in sorted(decisions, key=lambda r: r["rcept_no"], reverse=True):
        filing = filings.get(d["rcept_no"], {})
        company = companies.get(d["corp_code"], {})
        parsed = []
        if with_allottees:
            try:
                parsed = parse_allottees(dart.document(d["rcept_no"]))
            except (DartError, zipfile.BadZipFile) as e:
                print(f"  원문 실패: {e}", file=sys.stderr)
        for a in parsed:
            allottees.append({
                "접수번호": d["rcept_no"],
                "접수일": filing.get("rcept_dt", d["rcept_no"][:8]),
                "회사명": d.get("corp_name", ""),
                "배정대상자": a["name"],
                "회사와의관계": a["relation"],
                "배정주식수": a["shares"],
                "비고": a["note"],
            })

        purposes = {
            label: to_int(d.get(key))
            for key, label in FUND_PURPOSES.items()
            if to_int(d.get(key))
        }
        rows.append({
            "접수번호": d["rcept_no"],
            "접수일": filing.get("rcept_dt", d["rcept_no"][:8]),
            "보고서명": filing.get("report_nm", ""),
            "회사명": d.get("corp_name", ""),
            "종목코드": company.get("stock_code") or filing.get("stock_code", ""),
            "시장": CORP_CLS.get(d.get("corp_cls", ""), d.get("corp_cls", "")),
            "증자방식": d.get("ic_mthn", ""),
            "신주_보통주": to_int(d.get("nstk_ostk_cnt")),
            "신주_기타주": to_int(d.get("nstk_estk_cnt")),
            "증자전_보통주총수": to_int(d.get("bfic_tisstk_ostk")),
            "액면가": to_int(d.get("fv_ps")),
            "조달금액_합계": sum(purposes.values()) or None,
            "자금용도": ", ".join(f"{k} {v:,}" for k, v in purposes.items()),
            "배정대상자": "; ".join(a["name"] for a in parsed),
            "대표자": company.get("ceo_nm", ""),
            "업종코드": company.get("induty_code", ""),
            "설립일": company.get("est_dt", ""),
            "주소": company.get("adres", ""),
            "홈페이지": company.get("hm_url", ""),
            "공시링크": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={d['rcept_no']}",
        })
    return rows, allottees


def write_csv(path, rows, fieldnames=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = fieldnames or (list(rows[0].keys()) if rows else [])
    # utf-8-sig so Excel opens the Korean headers correctly.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", help="YYYYMMDD")
    p.add_argument("--end", help="YYYYMMDD (default: today)")
    p.add_argument("--days", type=int, default=365, help="--start 미지정 시 최근 N일 (default 365)")
    p.add_argument("--no-allottees", action="store_true", help="공시 원문(배정대상자) 파싱 생략")
    p.add_argument("--out", type=Path, default=OUTPUT_DIR)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    api_key = os.environ.get("DART_API_KEY", "").strip()
    if not api_key:
        sys.exit("DART_API_KEY 환경변수가 없습니다. https://opendart.fss.or.kr 에서 인증키를 발급받으세요.")

    end = dt.datetime.strptime(args.end, "%Y%m%d").date() if args.end else dt.date.today()
    start = (dt.datetime.strptime(args.start, "%Y%m%d").date() if args.start
             else end - dt.timedelta(days=args.days))
    print(f"기간: {ymd(start)} ~ {ymd(end)}", file=sys.stderr)

    rows, allottees = collect(Dart(api_key), start, end, with_allottees=not args.no_allottees)
    write_csv(args.out / "third_party_allotments.csv", rows)
    write_csv(args.out / "allottees.csv", allottees,
              ["접수번호", "접수일", "회사명", "배정대상자", "회사와의관계", "배정주식수", "비고"])
    print(f"저장: {args.out}/third_party_allotments.csv ({len(rows)}건), "
          f"allottees.csv ({len(allottees)}명)", file=sys.stderr)


if __name__ == "__main__":
    main()
