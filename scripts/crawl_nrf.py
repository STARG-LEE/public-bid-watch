"""
한국연구재단(NRF) 사업공고 + 공지사항 크롤러

대상 페이지 (서버사이드 렌더링 HTML, 인증 불필요):
    신규사업공모: https://www.nrf.re.kr/biz/notice/list?menu_no=362&bizNotGubn=guide
    공지사항(사업): https://www.nrf.re.kr/biz/notice/list?menu_no=364&bizNotGubn=notice

각 페이지에서 .public-notice-block 단위로 게시글 추출.
한 페이지에 보통 9~10건. IRIS 가 다 못 잡는 NRF 의 비-R&D / 일반공고 (학술지 평가,
사업관리 변경, 모집공고 등) 를 보완하는 목적.

매일 크롤링으로 신규 항목 누적 → data/nrf-YYYY-MM-DD.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
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

NRF_HOST = "https://www.nrf.re.kr"
# (URL, 게시판 라벨)
SOURCES = [
    (f"{NRF_HOST}/biz/notice/list?menu_no=362&bizNotGubn=guide", "신규사업공모"),
    (f"{NRF_HOST}/biz/notice/list?menu_no=364&bizNotGubn=notice", "공지사항"),
]


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def fetch_html(url: str, timeout: int = 25) -> str:
    r = requests.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
            "Accept-Language": "ko-KR,ko;q=0.9",
        },
        timeout=timeout,
    )
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def _strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def _norm_dt(s: str) -> str:
    """'2026-05-13 17:00' 또는 '2026-05-13' 을 'YYYY-MM-DD HH:MM:00' 으로."""
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


def parse_blocks(html: str, board_label: str) -> list[dict]:
    """`.public-notice-block` 단위로 파싱."""
    items: list[dict] = []
    # 블록 시작 다음 블록(같은 클래스) 또는 페이지네이션 직전까지
    pattern = re.compile(
        r'<div class="public-notice-block">(.+?)(?=<div class="public-notice-block">|<div class="board-pagination|<div class="board_no_search)',
        re.S,
    )
    for m in pattern.finditer(html):
        block = re.sub(r"<!--.*?-->", "", m.group(1), flags=re.S)
        # 제목 + postNo + bizNo
        t = re.search(
            r'class="title-name[^"]*"[^>]*data-post_no="(\d+)"(?:[^>]*data-biz_no="(\d+)")?[^>]*>([^<]+)</a>',
            block,
        )
        if not t:
            continue
        post_no, biz_no, title = t.group(1), (t.group(2) or ""), t.group(3).strip()

        # 상태 (접수중/접수마감 + D-N)
        state_block = re.search(r'<div class="pnb-state">(.+?)</div>\s*</div>', block, re.S)
        status_text, dday = "", ""
        if state_block:
            sb = state_block.group(1)
            texts = [_strip_tags(x) for x in re.findall(r"<span[^>]*>(.+?)</span>", sb, re.S)]
            texts = [t for t in texts if t]
            for t in texts:
                if t.startswith("D-") or t.startswith("D+") or t == "D-DAY":
                    dday = t
                elif "접수" in t:
                    status_text = t

        # 사업명 (bread crumb)
        biz_cat = ""
        m_bc = re.search(r'class="bread-crumb-text"[^>]*>([^<]+)</span>', block)
        if m_bc:
            biz_cat = m_bc.group(1).strip()

        # 접수일자 (info li)
        period_text = ""
        m_pi = re.search(r"<b>접수일자</b>\s*:\s*([^<]+)</span>", block)
        if m_pi:
            period_text = m_pi.group(1).strip()
        open_dt, close_dt = "", ""
        if "~" in period_text:
            a, b = [x.strip() for x in period_text.split("~", 1)]
            open_dt, close_dt = _norm_dt(a), _norm_dt(b)
        elif period_text:
            close_dt = _norm_dt(period_text)

        # 상세 URL: NRF 의 view 페이지 패턴
        # bizNotGubn 은 게시판마다 다른데 board_label 로 결정
        gubn = "guide" if board_label == "신규사업공모" else "notice"
        url = f"{NRF_HOST}/biz/notice/view?menu_no=362&postNo={post_no}&bizNotGubn={gubn}"
        if biz_no:
            url += f"&bizNo={biz_no}"

        items.append({
            "source": "nrf",
            "source_id": f"{gubn}-{post_no}",
            "title": title,
            "org": "한국연구재단",
            "exec_org": "",
            "url": url,
            "open_dt_raw": open_dt,
            "close_dt_raw": close_dt,
            "period_raw": period_text,
            "category": biz_cat,
            "board": board_label,
            "status": status_text,
            "dday": dday,
            # build_index.py 호환 필드
            "bidNtceNm": title,
            "bidNtceNo": post_no,
            "bidNtceOrd": "",
            "bidClseDt": close_dt,
            "ntceInsttNm": "한국연구재단",
            "dminsttNm": "",
            "bidNtceDtlUrl": url,
        })
    return items


def match_keywords(title: str, keywords: list[str], case_sensitive: bool) -> list[str]:
    if not title or not keywords:
        return []
    hay = title if case_sensitive else title.lower()
    return [kw for kw in keywords if (kw if case_sensitive else kw.lower()) in hay]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="저장 날짜 (YYYY-MM-DD)")
    parser.add_argument("--all", action="store_true", help="키워드 필터 건너뛰기")
    args = parser.parse_args()

    cfg = load_config()
    keywords = [k.strip() for k in cfg.get("keywords", []) if k and k.strip()]
    exclude = [k.strip() for k in cfg.get("exclude_keywords", []) if k and k.strip()]
    case_sensitive = bool(cfg.get("case_sensitive", False))

    date_str = args.date or datetime.now(tz=KST).date().isoformat()
    print(f"=== NRF 수집 시작 ({date_str}) ===")

    seen: dict[str, dict] = {}
    for url, label in SOURCES:
        print(f"\n[{label}] {url}")
        try:
            html = fetch_html(url)
        except requests.RequestException as e:
            print(f"  오류: {e}", file=sys.stderr)
            continue
        items = parse_blocks(html, label)
        print(f"  파싱: {len(items)}건")

        for it in items:
            sid = it["source_id"]
            matched = match_keywords(it["title"], keywords, case_sensitive)
            if not args.all and keywords and not matched:
                continue
            hay = it["title"].lower() if not case_sensitive else it["title"]
            if any((x if case_sensitive else x.lower()) in hay for x in exclude):
                continue
            it["matched_keywords"] = matched
            seen[sid] = it

    out_dir = DATA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"nrf-{date_str}.json"
    payload = {
        "source": "nrf",
        "date": date_str,
        "generated_at": datetime.now(tz=KST).isoformat(timespec="seconds"),
        "items": list(seen.values()),
    }
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"\n총 통과: {len(seen)}건  → {out_path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
