"""
IRIS(범부처통합연구지원시스템 www.iris.go.kr) 사업공고 크롤러

API (인증키 없음, JSON 반환):
    POST https://www.iris.go.kr/contents/retrieveBsnsAncmBtinSituList.do
    Headers: X-Requested-With: XMLHttpRequest, Referer 필요
    Body: pageIndex=N&pageUnit=10  (pageUnit 은 서버에서 10 고정)

응답 핵심 필드:
    ancmId       공고ID
    ancmTl       공고명
    sorgnNm      주관기관명 (한국연구재단/IITP/우주항공청 등)
    blngGovdSeNm 소관부처명 (과학기술정보통신부 등)
    rcveStrDe    접수시작일 ("2026.05.15")
    rcveEndDe    접수마감일 ("2026.05.21")
    dDay         남은 일수 (서버 계산)
    rcveStt      진행상태 ("진행중" 등)
    ancmNo       공고번호 (공고명 외 식별자)
    pbofrTpSeNmLst 공모유형 (지정공모/자유공모 등)

전체 진행중 공고를 페이지네이션으로 받아서 키워드 로컬 필터링.
data/iris-YYYY-MM-DD.json 에 저장.

환경변수: 없음 (공개 API)

사용법:
    python scripts/crawl_iris.py
    python scripts/crawl_iris.py --date 2026-05-18
    python scripts/crawl_iris.py --all   # 키워드 무시
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except Exception:
    KST = timezone(timedelta(hours=9))

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"

IRIS_LIST_URL = "https://www.iris.go.kr/contents/retrieveBsnsAncmBtinSituList.do"
IRIS_DETAIL_URL = "https://www.iris.go.kr/contents/retrieveBsnsAncmView.do?ancmId={}"
IRIS_REFERER = "https://www.iris.go.kr/contents/retrieveBsnsAncmBtinSituListView.do"
MAX_PAGES = 50  # 안전 가드


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def _norm_date(s: str) -> str:
    """IRIS 가 쓰는 'YYYY.MM.DD' 또는 'YYYY-MM-DD' 를 'YYYY-MM-DD 23:59:00' 으로."""
    if not s:
        return ""
    s = s.strip().replace(".", "-")
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]} 23:59:00"
    return s


def fetch_page(session: requests.Session, page: int, timeout: int = 30) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": IRIS_REFERER,
    }
    data = {"pageIndex": page, "pageUnit": 10}
    r = session.post(IRIS_LIST_URL, headers=headers, data=data, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_all() -> list[dict]:
    session = requests.Session()
    items: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        try:
            payload = fetch_page(session, page)
        except (requests.RequestException, ValueError) as e:
            print(f"  [page {page}] 오류: {e}", file=sys.stderr)
            break
        chunk = payload.get("listBsnsAncmBtinSitu") or []
        items.extend(chunk)
        pg = payload.get("paginationInfo") or {}
        total_pages = int(pg.get("totalPageCount") or 1)
        if page >= total_pages or not chunk:
            break
        time.sleep(0.25)  # 서버 배려
    return items


def normalize_item(raw: dict) -> dict:
    ancm_id = str(raw.get("ancmId") or "").strip()
    title = str(raw.get("ancmTl") or "").strip()
    org = str(raw.get("sorgnNm") or "").strip()
    govd = str(raw.get("blngGovdSeNm") or "").strip()
    open_dt = _norm_date(str(raw.get("rcveStrDe") or ""))
    close_dt = _norm_date(str(raw.get("rcveEndDe") or ""))
    ancm_no = str(raw.get("ancmNo") or "").strip()
    pbofr = str(raw.get("pbofrTpSeNmLst") or "").strip()
    rcve_stt = str(raw.get("rcveSttSeNmLst") or raw.get("rcveStt") or "").strip()
    url = IRIS_DETAIL_URL.format(ancm_id) if ancm_id else ""

    return {
        "source": "iris",
        "source_id": ancm_id,
        "title": title,
        "org": org,            # 주관기관 (NRF/IITP/등)
        "exec_org": govd,      # 소관부처 (과기정통부 등)
        "url": url,
        "open_dt_raw": open_dt,
        "close_dt_raw": close_dt,
        "period_raw": f"{raw.get('rcveStrDe', '')} ~ {raw.get('rcveEndDe', '')}",
        "category": pbofr,
        "rcve_status": rcve_stt,
        "ancm_no": ancm_no,
        # build_index.py 호환 필드
        "bidNtceNm": title,
        "bidNtceNo": ancm_id,
        "bidNtceOrd": "",
        "bidClseDt": close_dt,
        "ntceInsttNm": org,
        "dminsttNm": govd if govd and govd != org else "",
        "bidNtceDtlUrl": url,
    }


def match_keywords(title: str, keywords: list[str], case_sensitive: bool) -> list[str]:
    if not title or not keywords:
        return []
    hay = title if case_sensitive else title.lower()
    return [kw for kw in keywords if (kw if case_sensitive else kw.lower()) in hay]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="저장 날짜 (YYYY-MM-DD, 기본=오늘 KST)")
    parser.add_argument("--all", action="store_true", help="키워드 필터 건너뛰기")
    args = parser.parse_args()

    cfg = load_config()
    keywords = [k.strip() for k in cfg.get("keywords", []) if k and k.strip()]
    exclude = [k.strip() for k in cfg.get("exclude_keywords", []) if k and k.strip()]
    case_sensitive = bool(cfg.get("case_sensitive", False))

    date_str = args.date or datetime.now(tz=KST).date().isoformat()
    print(f"=== IRIS 수집 시작 ({date_str}) ===")
    print(f"키워드 ({len(keywords)}): {keywords}")

    raw_items = fetch_all()
    print(f"수신: {len(raw_items)}건")

    seen: dict[str, dict] = {}
    filtered_out = 0
    for raw in raw_items:
        norm = normalize_item(raw)
        sid = norm["source_id"]
        if not sid:
            continue

        matched = match_keywords(norm["title"], keywords, case_sensitive)
        if not args.all and keywords and not matched:
            filtered_out += 1
            continue

        # 제외 키워드
        hay = norm["title"]
        if not case_sensitive:
            hay = hay.lower()
        if any((x if case_sensitive else x.lower()) in hay for x in exclude):
            filtered_out += 1
            continue

        norm["matched_keywords"] = matched
        seen[sid] = norm

    out_dir = DATA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"iris-{date_str}.json"
    payload = {
        "source": "iris",
        "date": date_str,
        "generated_at": datetime.now(tz=KST).isoformat(timespec="seconds"),
        "keyword_count": len(keywords),
        "items": list(seen.values()),
    }
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"필터 통과: {len(seen)}건  (제외 {filtered_out}건)")
    print(f"저장: {out_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
