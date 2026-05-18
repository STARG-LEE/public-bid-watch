"""
정보통신기획평가원(IITP) 입찰공고 크롤러

대상: 알림·공지 > 입찰공고 (cms_menu_seq=38, board_seq=8)
    1525건 (현재). IITP 가 직접 발주하는 ICT R&D / AI / SW 시스템 등 용역.
    IRIS(R&D 사업공고) 와는 다른 IITP 자체 조달 건.

API (사용자 DevTools 캡쳐로 발견):
    POST https://www.iitp.kr/board-svc/api/bbs/A/list.do
    Headers: X-CSRF-TOKEN (meta 에서 추출), Content-Type: application/json
    Body:  {"cms_menu_seq":"38","cpage":N,"rows":"10","keyword":"","condition":"","sort":"latest"}

응답 list[] 핵심 필드:
    article_seq   글 시퀀스
    board_seq     게시판 seq (38→8)
    title         제목 (예: "[중앙조달 입찰공고] ...")
    reg_dt        등록일 "YYYY-MM-DD"
    notice_yn     상단 고정 공지 여부
    attach_cnt    첨부파일 수
    view_cnt      조회수

매일 최신 N페이지 (디폴트 5페이지=50건) 받아서 키워드 로컬 필터.
결과: data/iitp-YYYY-MM-DD.json
"""
from __future__ import annotations

import argparse
import json
import re
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

IITP_HOST = "https://www.iitp.kr"
NOTICE_PAGE = f"{IITP_HOST}/kr/1/notice/notice.it"  # CSRF + 세션 쿠키 확보용
LIST_API = f"{IITP_HOST}/board-svc/api/bbs/A/list.do"

# (cms_menu_seq, board_seq, board_label)
# 입찰공고만. 필요 시 ('31', 5, '보도자료') 등 추가 가능.
BOARDS = [
    ("38", 8, "입찰공고"),
]

DEFAULT_MAX_PAGES = 5
PAGE_SIZE = "10"  # API 가 사실상 10 고정


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def get_csrf(session: requests.Session) -> str:
    r = session.get(NOTICE_PAGE, timeout=20)
    r.raise_for_status()
    m = re.search(r'<meta name="_csrf" content="([^"]+)"', r.text)
    if not m:
        raise RuntimeError("CSRF 토큰을 찾지 못함")
    return m.group(1)


def fetch_page(session: requests.Session, csrf: str, cms_menu_seq: str, page: int) -> dict:
    headers = {
        "Content-Type": "application/json",
        "X-CSRF-TOKEN": csrf,
        "Referer": f"{IITP_HOST}/",
        "Accept": "application/json, text/plain, */*",
    }
    body = {
        "cms_menu_seq": cms_menu_seq,
        "cpage": page,
        "rows": PAGE_SIZE,
        "keyword": "",
        "condition": "",
        "sort": "latest",
    }
    r = session.post(LIST_API, headers=headers, json=body, timeout=25)
    r.raise_for_status()
    return r.json()


def _norm_dt(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    m = re.search(r"(\d{4})[.\-/](\d{2})[.\-/](\d{2})(?:[\s\-]+(\d{2}):(\d{2}))?", s)
    if not m:
        return s
    y, mo, d = m.group(1), m.group(2), m.group(3)
    hh = m.group(4) or "23"
    mm = m.group(5) or "59"
    return f"{y}-{mo}-{d} {hh}:{mm}:00"


def normalize_item(raw: dict, board_seq: int, board_label: str, cms_menu_seq: str) -> dict:
    article_seq = str(raw.get("article_seq") or "")
    title = str(raw.get("title") or "").strip()
    reg_dt = _norm_dt(str(raw.get("reg_dt") or ""))
    # 상세 URL — list 페이지 URL 패턴 추정: /web/lay1/bbs/S1T12C{seq}/A/{board_seq}/view.do?article_seq=
    url = (
        f"{IITP_HOST}/web/lay1/bbs/S1T12C{cms_menu_seq}/A/{board_seq}/view.do"
        f"?article_seq={article_seq}"
    )

    return {
        "source": "iitp",
        "source_id": f"{board_seq}-{article_seq}",
        "title": title,
        "org": "정보통신기획평가원",
        "exec_org": "",
        "url": url,
        "open_dt_raw": "",
        "close_dt_raw": "",   # 입찰공고에서 마감일은 첨부 PDF 안에 — 게시일만 알 수 있음
        "period_raw": "",
        "category": board_label,
        "board": board_label,
        "reg_dt": reg_dt,
        "view_cnt": raw.get("view_cnt") or 0,
        "attach_cnt": raw.get("attach_cnt") or 0,
        # build_index.py 호환 필드 (마감일 없음 → open_list 분류됨)
        "bidNtceNm": title,
        "bidNtceNo": article_seq,
        "bidNtceOrd": "",
        "bidClseDt": "",
        "ntceInsttNm": "정보통신기획평가원",
        "dminsttNm": "",
        "bidNtceDtlUrl": url,
    }


def match_keywords(title: str, keywords: list[str], case_sensitive: bool) -> list[str]:
    if not title or not keywords:
        return []
    hay = title if case_sensitive else title.lower()
    return [kw for kw in keywords if (kw if case_sensitive else kw.lower()) in hay]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="저장 날짜 (YYYY-MM-DD)")
    parser.add_argument("--pages", type=int, default=DEFAULT_MAX_PAGES, help="게시판별 최대 페이지")
    parser.add_argument("--all", action="store_true", help="키워드 필터 건너뛰기")
    args = parser.parse_args()

    cfg = load_config()
    keywords = [k.strip() for k in cfg.get("keywords", []) if k and k.strip()]
    exclude = [k.strip() for k in cfg.get("exclude_keywords", []) if k and k.strip()]
    case_sensitive = bool(cfg.get("case_sensitive", False))

    date_str = args.date or datetime.now(tz=KST).date().isoformat()
    print(f"=== IITP 수집 시작 ({date_str}) ===")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    })
    try:
        csrf = get_csrf(session)
        print(f"CSRF: {csrf[:8]}…")
    except (requests.RequestException, RuntimeError) as e:
        print(f"CSRF 획득 실패: {e}", file=sys.stderr)
        return 2

    seen: dict[str, dict] = {}
    for cms_menu_seq, board_seq, label in BOARDS:
        print(f"\n[{label}] cms_menu_seq={cms_menu_seq}")
        received_total = 0
        for page in range(1, args.pages + 1):
            try:
                d = fetch_page(session, csrf, cms_menu_seq, page)
            except (requests.RequestException, ValueError) as e:
                print(f"  page {page} 오류: {e}", file=sys.stderr)
                break
            items = d.get("list") or []
            if not items:
                break
            received_total += len(items)
            for raw in items:
                norm = normalize_item(raw, board_seq, label, cms_menu_seq)
                sid = norm["source_id"]
                matched = match_keywords(norm["title"], keywords, case_sensitive)
                if not args.all and keywords and not matched:
                    continue
                hay = norm["title"].lower() if not case_sensitive else norm["title"]
                if any((x if case_sensitive else x.lower()) in hay for x in exclude):
                    continue
                norm["matched_keywords"] = matched
                seen[sid] = norm
            pg = d.get("pagination") or {}
            total_pages = int(pg.get("totalpage") or 1)
            if page >= total_pages:
                break
            time.sleep(0.25)
        print(f"  수신 {received_total}건, 키워드 매치 누적: {len(seen)}건")

    out_dir = DATA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"iitp-{date_str}.json"
    payload = {
        "source": "iitp",
        "date": date_str,
        "generated_at": datetime.now(tz=KST).isoformat(timespec="seconds"),
        "items": list(seen.values()),
    }
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n저장: {out_path.relative_to(ROOT)} ({len(seen)}건)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
