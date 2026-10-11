import re, requests
H = {"User-Agent": "Mozilla/5.0"}
def get(u):
    try:
        r = requests.get(u, headers=H, timeout=20)
        return r.status_code, r.headers.get("content-type", ""), r.text
    except Exception as e:
        return None, "", repr(e)
cands = [
 "https://m.stock.naver.com/api/stocks/industry?page=1&pageSize=3",
 "https://m.stock.naver.com/api/stocks/industry/278?page=1&pageSize=3",
 "https://m.stock.naver.com/api/stocks/upjong?page=1&pageSize=3",
 "https://m.stock.naver.com/api/stocks/theme?page=1&pageSize=3",
 "https://api.stock.naver.com/stock/domestic/industry?page=1&pageSize=3",
 "https://api.stock.naver.com/stock/exchange/KOSPI/marketValue?page=1&pageSize=1",
 "https://finance.naver.com/sise/sise_group_detail.naver?type=upjong&no=278",
 "https://finance.naver.com/sise/sise_group.naver?type=theme",
]
for u in cands:
    s, ct, t = get(u)
    print("==", u, s, ct, len(t)); print(t[:600].replace("\n", " "))
s, ct, page = get("https://stock.naver.com/market/stock/kr/groups")
print("== groups page", s, len(page))
srcs = re.findall(r'<script[^>]+src="([^"]+)"', page)
print("scripts", len(srcs), srcs[:40])
seen = set()
for src in srcs:
    url = src if src.startswith("http") else "https://stock.naver.com" + src
    s, ct, js = get(url)
    for m in re.finditer(r'.{0,120}(?:upjong|industr|groups?/|/group)[^"\'`]{0,120}', js, re.I):
        frag = m.group(0)
        if ("api" in frag or "http" in frag or "/" in frag) and frag not in seen and len(seen) < 80:
            seen.add(frag); print("JS", src.split("/")[-1][:40], "::", frag[:240])
for m in re.finditer(r'["\'`](https?://[a-z.]*naver\.com/[^"\'`]*api[^"\'`]*|/api/[^"\'`]+)["\'`]', page):
    print("PAGEAPI", m.group(1))
