#!/usr/bin/env python3
"""김매실 지원사업 레이더 — 정부지원사업/공공조달 공고 수집기

기업마당 + K-Startup + 나라장터 API에서 공고를 수집하고,
키워드 필터를 거쳐 새 공고만 new_items.json 으로 출력한다.
(이미 본 공고는 seen_ids.json 에 기록되어 중복 제거)

사용법: python3 collect.py [--days 3]
"""
import html
import json
import re
import ssl
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"
SEEN_FILE = BASE_DIR / "seen_ids.json"
OUT_FILE = BASE_DIR / "new_items.json"
ITEMS_FILE = BASE_DIR / "items.json"   # 누적 저장소 (대시보드 원본)
DATA_JS = BASE_DIR / "data.js"         # index.html 이 읽는 데이터
ARCHIVE_FILE = BASE_DIR / "archive.json"  # 마감 지난 공고 보존 (연례 패턴 분석용)

# ── 김매실 프로필 기반 필터 ──────────────────────────────
# 지원사업(기업마당/K-Startup): 아래 키워드가 공고명·분야·대상에 하나라도 있으면 수집
GRANT_KEYWORDS = [
    "1인", "소상공인", "창업", "예비창업", "초기창업", "청년",
    "브랜드", "브랜딩", "마케팅", "콘텐츠", "SNS", "홍보",
    "이커머스", "온라인", "판로", "유통", "수출", "해외진출",
    "디자인", "패키지", "상세페이지", "라이브커머스",
    "AI", "인공지능", "디지털", "스마트",
]
# 지원사업 제외 키워드 (명백히 무관한 분야)
GRANT_EXCLUDE = ["농기계", "축산", "어업", "광업", "원자력", "조선", "방산"]

# 지역 정규화: 공고명 [태그]·기관명·지원지역 필드에서 추출
REGIONS = {
    "서울특별시": "서울", "부산광역시": "부산", "대구광역시": "대구", "인천광역시": "인천",
    "광주광역시": "광주", "대전광역시": "대전", "울산광역시": "울산", "세종특별자치시": "세종",
    "경기도": "경기", "강원특별자치도": "강원", "강원도": "강원",
    "충청북도": "충북", "충청남도": "충남", "전북특별자치도": "전북", "전라북도": "전북",
    "전라남도": "전남", "경상북도": "경북", "경상남도": "경남", "제주특별자치도": "제주",
    "서울": "서울", "부산": "부산", "대구": "대구", "인천": "인천", "광주": "광주",
    "대전": "대전", "울산": "울산", "세종": "세종", "경기": "경기", "강원": "강원",
    "충북": "충북", "충남": "충남", "전북": "전북", "전남": "전남", "경북": "경북",
    "경남": "경남", "제주": "제주", "전국": "전국",
}
_REGION_KEYS = sorted(REGIONS, key=len, reverse=True)

def detect_region(title, org, hint=""):
    """지원지역 힌트 → 공고명 [태그] → 기관명 순으로 지역 추출. 없으면 전국."""
    for k in _REGION_KEYS:
        if hint and k in hint:
            return REGIONS[k]
    m = re.match(r"^\s*[\[(（]([^\])）]{2,20})[\])）]", title or "")
    if m:
        for k in _REGION_KEYS:
            if k in m.group(1):
                return REGIONS[k]
    for k in _REGION_KEYS:
        if org and k in org:
            return REGIONS[k]
    return "전국"

# 소진공 공지사항 게시판 제외 키워드 (채용 등 정책자금과 무관한 내부 공지)
SEMAS_EXCLUDE = ["채용", "인사", "입찰공고", "계약", "조달"]

# 입찰(나라장터 용역): 1인 기업이 수행 가능한 용역 위주
BID_KEYWORDS = [
    "홍보", "콘텐츠", "마케팅", "SNS", "영상", "브랜딩", "브랜드",
    "디자인", "교육", "컨설팅", "멘토링", "강의", "제작",
    "에듀테크", "이러닝",
]
BID_MAX_PRICE = 300_000_000  # 추정가격 3억 이하만 (0이면 제한 없음)
# ─────────────────────────────────────────────────────


def load_key():
    import os
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("DATA_GO_KR_API_KEY="):
                return line.split("=", 1)[1].strip()
    if os.environ.get("DATA_GO_KR_API_KEY"):  # GitHub Actions 등 CI 환경
        return os.environ["DATA_GO_KR_API_KEY"]
    sys.exit(".env 또는 환경변수에서 DATA_GO_KR_API_KEY 를 찾을 수 없습니다")


def fetch(url, params, post=False):
    qs = urllib.parse.urlencode(params, safe=":")
    ctx = ssl.create_default_context()
    headers = {"User-Agent": "Mozilla/5.0"}
    if post:
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        req = urllib.request.Request(url, data=qs.encode(), headers=headers)
    else:
        req = urllib.request.Request(f"{url}?{qs}", headers=headers)
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        return r.read().decode("utf-8", errors="replace")


def match_any(text, keywords):
    return any(k.lower() in text.lower() for k in keywords)


def parse_date(s):
    """YYYYMMDD / YYYY-MM-DD / 'YYYYMMDD ~ YYYYMMDD' → ISO date or None"""
    if not s:
        return None
    m = re.search(r"(\d{4})[.\-/]?(\d{2})[.\-/]?(\d{2})", s)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


MAX_PAGES = 30  # 폭주 방지용 안전장치 (사실상 전체 페이지를 다 돈다)


def collect_bizinfo(key):
    """기업마당: 전체 지원사업 공고 (페이지네이션으로 끝까지 수집)"""
    items = []
    page = 1
    while page <= MAX_PAGES:
        xml_text = fetch(
            "https://apis.data.go.kr/1421000/bizinfo/pblancBsnsService",
            {"serviceKey": key, "pageNo": page, "numOfRows": 200},
        )
        root = ET.fromstring(xml_text.strip())
        page_items = list(root.iter("item"))
        if not page_items:
            break
        for it in page_items:
            g = lambda tag: html.unescape((it.findtext(tag) or "").strip())
            period = g("reqstBeginEndDe")  # "2026-07-01 ~ 2026-07-31" 또는 "예산 소진시까지" 등
            parts = period.split("~") if period else []
            deadline = parse_date(parts[1]) if len(parts) > 1 else None
            blob = " ".join([g("pblancNm"), g("pldirSportRealmLclasCodeNm"), g("trgetNm"), g("hashtags")])
            if not match_any(blob, GRANT_KEYWORDS) or match_any(blob, GRANT_EXCLUDE):
                continue
            items.append({
                "id": f"bizinfo:{g('pblancId')}",
                "source": "기업마당",
                "title": g("pblancNm"),
                "field": g("pldirSportRealmLclasCodeNm"),
                "target": g("trgetNm"),
                "org": g("jrsdInsttNm") or g("excInsttNm"),
                "start": parse_date(parts[0]) if parts else None,
                "deadline": deadline,
                # 날짜가 아닌 접수기간 문구는 그대로 보존해 대시보드에 표시
                "deadline_note": None if deadline else (period or "상시"),
                "url": g("pblancUrl"),
                "region": detect_region(g("pblancNm"), g("excInsttNm") or g("jrsdInsttNm")),
            })
        if len(page_items) < 200:
            break
        page += 1
    return items


def collect_kstartup(key):
    """K-Startup: 창업지원사업 공고 (모집중, 페이지네이션으로 끝까지 수집)"""
    items = []
    page = 1
    while page <= MAX_PAGES:
        text = fetch(
            "https://apis.data.go.kr/B552735/kisedKstartupService01/getAnnouncementInformation01",
            {"serviceKey": key, "page": page, "perPage": 200, "returnType": "json",
             "cond[rcrt_prgs_yn::EQ]": "Y"},
        )
        data = json.loads(text).get("data", [])
        if not data:
            break
        for d in data:
            blob = " ".join(str(d.get(k) or "") for k in
                            ["biz_pbanc_nm", "supt_biz_clsfc", "aply_trgt", "supt_regin"])
            if not match_any(blob, GRANT_KEYWORDS) or match_any(blob, GRANT_EXCLUDE):
                continue
            items.append({
                "id": f"kstartup:{d.get('pbanc_sn')}",
                "source": "K-Startup",
                "title": d.get("biz_pbanc_nm") or "",
                "field": d.get("supt_biz_clsfc") or "",
                "target": (d.get("aply_trgt") or "")[:100],
                "org": d.get("pbanc_ntrp_nm") or "",
                "start": parse_date(d.get("pbanc_rcpt_bgng_dt")),
                "deadline": parse_date(d.get("pbanc_rcpt_end_dt")),
                "url": d.get("detl_pg_url") or "",
                "region": detect_region(d.get("biz_pbanc_nm") or "", d.get("pbanc_ntrp_nm") or "",
                                        hint=d.get("supt_regin") or ""),
            })
        if len(data) < 200:
            break
        page += 1
    return items


def collect_g2b(key, days):
    """나라장터: 최근 N일 등록된 용역 입찰공고 (페이지네이션으로 끝까지 수집)"""
    now = datetime.now()
    begin = (now - timedelta(days=days)).strftime("%Y%m%d0000")
    end = now.strftime("%Y%m%d2359")
    items = []
    page = 1
    while page <= MAX_PAGES:
        text = fetch(
            "https://apis.data.go.kr/1230000/ad/BidPublicInfoService/getBidPblancListInfoServcPPSSrch",
            {"serviceKey": key, "pageNo": page, "numOfRows": 500, "inqryDiv": 1,
             "inqryBgnDt": begin, "inqryEndDt": end, "type": "json"},
        )
        body = json.loads(text).get("response", {}).get("body", {})
        raw = body.get("items") or []
        if not raw:
            break
        for d in raw:
            name = d.get("bidNtceNm") or ""
            if not match_any(name, BID_KEYWORDS):
                continue
            try:
                price = int(float(d.get("presmptPrce") or 0))
            except ValueError:
                price = 0
            if BID_MAX_PRICE and price > BID_MAX_PRICE:
                continue
            items.append({
                "id": f"g2b:{d.get('bidNtceNo')}-{d.get('bidNtceOrd')}",
                "source": "나라장터",
                "title": name,
                "field": "용역 입찰" + (f" (추정 {price:,}원)" if price else ""),
                "target": d.get("cntrctCnclsMthdNm") or "",
                "org": d.get("ntceInsttNm") or d.get("dminsttNm") or "",
                "start": parse_date(d.get("bidNtceDt")),
                # 직찰 등 마감일이 비어있는 공고는 개찰일을 마감일로 사용
                "deadline": parse_date(d.get("bidClseDt")) or parse_date(d.get("opengDt")),
                "url": d.get("bidNtceDtlUrl") or "",
                "region": detect_region(name, d.get("ntceInsttNm") or d.get("dminsttNm") or ""),
            })
        if len(raw) < 500:
            break
        page += 1
    return items


def collect_gokams():
    """예술경영지원센터: 공모사업 접수중 공고 (자체 게시판, 인증키 불필요)"""
    text = fetch("https://www.gokams.or.kr/02_apply/introduction.aspx", {})
    row_re = re.compile(
        r"<tr><td>\d+</td><td><img[^>]*alt='([^']+)'[^>]*/></td>"
        r"<td class=\"left\"><a href=\"introduction_view\.aspx\?Idx=(\d+)[^\"]*\">(.*?)</a>.*?</td>"
        r"<td>([^<]*)</td>",
        re.DOTALL,
    )
    items = []
    for status, idx, title, deadline in row_re.findall(text):
        if status.strip() != "접수중":
            continue
        title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
        items.append({
            "id": f"gokams:{idx}",
            "source": "예술경영지원센터",
            "title": title,
            "field": "공모사업",
            "target": "",
            "org": "예술경영지원센터",
            "start": None,
            "deadline": parse_date(deadline.strip()),
            "url": f"https://www.gokams.or.kr/02_apply/introduction_view.aspx?Idx={idx}",
            "region": detect_region(title, "예술경영지원센터"),
        })
    return items


_KR_DATE = re.compile(r"(?:[’']?(\d{2,4})\s*[.\-/년]\s*)?(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*[일.]?")


def parse_kr_period(text, base_year):
    """본문에서 '신청/접수/모집기간 : ... ~ ...' 패턴을 찾아 마감일(ISO)을 반환. 없으면 None."""
    m = re.search(r"(?:신청|접수|모집)\s*기[간한]\s*[:：]?\s*(.{0,150})", text)
    if not m:
        return None
    seg = m.group(1)
    # ~ 뒤(마감 쪽)만 보되, ~가 없으면 구간 전체에서 마지막 날짜를 마감으로 본다
    tail = seg.split("~")[-1] if "~" in seg else seg
    dates = _KR_DATE.findall(tail)
    if not dates:
        return None
    y, mo, d = dates[0]
    mo, d = int(mo), int(d)
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    if y:
        year = int(y) + 2000 if len(y) == 2 else int(y)
    else:
        # 연도 생략 시: 시작일(~ 앞)의 연도 → 없으면 등록연도
        head_dates = _KR_DATE.findall(seg.split("~")[0]) if "~" in seg else []
        hy = next((h[0] for h in head_dates if h[0]), "")
        year = (int(hy) + 2000 if len(hy) == 2 else int(hy)) if hy else base_year
    return f"{year}-{mo:02d}-{d:02d}"


def collect_semas():
    """소상공인시장진흥공단: 사업공고 게시판 (자체 게시판, 인증키 불필요)
    상세 페이지 본문에서 접수기간을 파싱해 실제 마감일을 얻는다. 없으면 상시(None)."""
    text = fetch("https://www.semas.or.kr/web/board/webBoardList.kmdc", {"pNm": "BOA0101", "bCd": 1, "page": 1})
    row_re = re.compile(
        r"<td>\d+</td>\s*<td class=\"left title\">\s*<a href=\"javascript:fncGoDetail\('(\d+)'\);\">\s*(.*?)\s*</a>"
        r".*?<td>(\d{4}-\d{2}-\d{2})</td>",
        re.DOTALL,
    )
    items = []
    for idx, title, reg_date in row_re.findall(text):
        title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
        if match_any(title, SEMAS_EXCLUDE):
            continue
        url = f"https://www.semas.or.kr/web/board/webBoardView.kmdc?pNm=BOA0101&bCd=1&b_idx={idx}"
        deadline = None
        try:
            detail = fetch("https://www.semas.or.kr/web/board/webBoardView.kmdc",
                           {"pNm": "BOA0101", "bCd": 1, "b_idx": idx})
            body = html.unescape(re.sub(r"<[^>]+>", " ", detail))
            deadline = parse_kr_period(body, int(reg_date[:4]))
        except Exception:
            pass  # 상세 파싱 실패 시 상시로 둔다
        items.append({
            "id": f"semas:{idx}",
            "source": "소상공인시장진흥공단",
            "title": title,
            "field": "사업공고",
            "target": "",
            "org": "소상공인시장진흥공단",
            "start": reg_date,
            "deadline": deadline,
            "url": url,
            "region": detect_region(title, "소상공인시장진흥공단"),
        })
    return items


def collect_bojo():
    """e나라도움·보탬e (보조금통합포털 bojo.go.kr): 접수중 공모사업.
    상세 화면이 팝업 방식이라 개별 딥링크가 없어 공모사업 목록 화면으로 링크한다."""
    items = []
    page = 1
    year = datetime.now().year
    while page <= MAX_PAGES:
        text = fetch("https://www.bojo.go.kr/da/retrieveTaskReqstList.do",
                     {"curPage": page, "perPage": 100, "searchBsnsYear": year,
                      "searchPssrpSttus": 1, "searchFilterYn": "N", "fileDownYn": "N",
                      "dateType": 0},
                     post=True)
        rows = json.loads(text).get("ntbdList", [])
        if not rows:
            break
        for d in rows:
            title = (d.get("sjCn") or "").strip()
            blob = " ".join(str(d.get(k) or "") for k in
                            ["sjCn", "bsnsSmry", "sportCn", "sportTrgetCn"])
            if not match_any(blob, GRANT_KEYWORDS) or match_any(blob, GRANT_EXCLUDE):
                continue
            org = (d.get("pssrpInsttNm") or "").strip()
            items.append({
                "id": f"bojo:{d.get('nttId')}",
                "source": "e나라도움",
                "title": title,
                "field": f"{d.get('pblancSeNm') or ''} 공모" + (" (지방)" if d.get("bsnsSe") == "2" else ""),
                "target": (d.get("sportTrgetCn") or "").strip()[:100],
                "org": org,
                "start": parse_date(d.get("rceptBeginDe")),
                "deadline": parse_date(d.get("rceptEndDe")),
                "url": "https://www.bojo.go.kr/bojo.do?menuId=USRM_10194",
                "region": detect_region(title, org),
            })
        if len(rows) < 100:
            break
        page += 1
    return items


def collect_iris():
    """IRIS 범부처통합연구지원시스템: 접수중 R&D 사업공고 (인증키 불필요)"""
    items = []
    page = 1
    while page <= MAX_PAGES:
        text = fetch("https://www.iris.go.kr/contents/retrieveBsnsAncmBtinSituList.do",
                     {"pageIndex": page, "ancmPrg": "ancmIng"}, post=True)
        d = json.loads(text)
        rows = d.get("listBsnsAncmBtinSitu", [])
        if not rows:
            break
        for r in rows:
            title = (r.get("ancmTl") or "").strip()
            if not title or "TEST" in title.upper():
                continue
            org = " · ".join(x for x in [r.get("blngGovdSeNm"), r.get("sorgnNm")] if x)
            items.append({
                "id": f"iris:{r.get('ancmId')}",
                "source": "IRIS",
                "title": title,
                "field": f"R&D {r.get('pbofrTpSeNmLst') or '공모'}",
                "target": "",
                "org": org,
                "start": parse_date(r.get("rcveStrDe")),
                "deadline": parse_date(r.get("rcveEndDe")),
                "url": f"https://www.iris.go.kr/contents/retrieveBsnsAncmView.do?ancmId={r.get('ancmId')}",
                "region": detect_region(title, org),
            })
        total_pages = d.get("paginationInfo", {}).get("totalPageCount", 1)
        if page >= total_pages:
            break
        page += 1
    return items


def main():
    days = 3
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    key = load_key()

    all_items, errors = [], []
    for name, fn in [("기업마당", lambda: collect_bizinfo(key)),
                     ("K-Startup", lambda: collect_kstartup(key)),
                     ("나라장터", lambda: collect_g2b(key, days)),
                     ("예술경영지원센터", collect_gokams),
                     ("소상공인시장진흥공단", collect_semas),
                     ("e나라도움", collect_bojo),
                     ("IRIS", collect_iris)]:
        try:
            got = fn()
            all_items += got
            print(f"[{name}] 필터 통과 {len(got)}건", file=sys.stderr)
        except Exception as e:
            errors.append(f"{name}: {e}")
            print(f"[{name}] 오류: {e}", file=sys.stderr)

    seen = set(json.loads(SEEN_FILE.read_text())) if SEEN_FILE.exists() else set()
    # 마감일 지난 공고 제외
    today = datetime.now().strftime("%Y-%m-%d")
    new = [i for i in all_items
           if i["id"] not in seen and (not i["deadline"] or i["deadline"] >= today)]

    SEEN_FILE.write_text(json.dumps(sorted(seen | {i["id"] for i in all_items}), ensure_ascii=False))
    OUT_FILE.write_text(json.dumps({"collected_at": datetime.now().isoformat(),
                                    "errors": errors, "new_items": new},
                                   ensure_ascii=False, indent=2))

    # ── 대시보드 데이터 갱신: 누적 저장 후 마감 지난 공고 정리 ──
    store = {}
    if ITEMS_FILE.exists():
        store = {i["id"]: i for i in json.loads(ITEMS_FILE.read_text())}
    today_s = datetime.now().strftime("%Y-%m-%d")
    for i in all_items:
        prev = store.get(i["id"], {})
        i["first_seen"] = prev.get("first_seen") or today_s
        i["last_seen"] = today_s
        store[i["id"]] = i
    cutoff = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    stale = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    # 마감일 있는 공고: 마감 3일 후 정리 / 상시(마감 미상) 공고: 소스에서 사라진 지 7일 후 정리
    items = [i for i in store.values()
             if (i["deadline"] >= cutoff if i["deadline"]
                 else (i.get("last_seen") or i.get("first_seen") or "9999") >= stale)]

    # ── 정리된 공고는 버리지 않고 아카이브에 보존 (연례 반복 공고 예측용) ──
    kept_ids = {i["id"] for i in items}
    removed = [i for i in store.values() if i["id"] not in kept_ids]
    if removed:
        archive = {}
        if ARCHIVE_FILE.exists():
            archive = {a["id"]: a for a in json.loads(ARCHIVE_FILE.read_text())}
        for i in removed:
            i["archived_at"] = today_s
            archive[i["id"]] = i
        arch_list = sorted(archive.values(), key=lambda a: a.get("deadline") or a["archived_at"])
        ARCHIVE_FILE.write_text(json.dumps(arch_list, ensure_ascii=False, indent=1))
        print(f"아카이브 이동 {len(removed)}건 (누적 {len(arch_list)}건)", file=sys.stderr)
    items.sort(key=lambda i: (i["deadline"] or "9999-99-99", i["source"]))
    ITEMS_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=1))
    DATA_JS.write_text("window.RADAR_DATA = " + json.dumps(
        {"collected_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
         "errors": errors, "items": items}, ensure_ascii=False) + ";")

    # 공유용 단일 파일(데이터 내장) 갱신
    index = BASE_DIR / "index.html"
    if index.exists():
        (BASE_DIR / "지원사업레이더.html").write_text(index.read_text().replace(
            '<script src="data.js"></script>', "<script>" + DATA_JS.read_text() + "</script>"))

    print(f"신규 공고 {len(new)}건 → {OUT_FILE}", file=sys.stderr)
    print(f"대시보드 데이터 {len(items)}건 → {DATA_JS}", file=sys.stderr)
    print(json.dumps({"new": len(new), "active": len(items), "errors": errors}, ensure_ascii=False))


if __name__ == "__main__":
    main()
