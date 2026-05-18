"""
기업마당(bizinfo.go.kr) 사업공고 크롤러

OPEN API: https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do
bizinfo API 는 키워드 검색을 제공하지 않으므로 전체 공고를 한 번에 받아오고,
config.json 의 keywords 로 로컬 필터링한다.
결과는 data/bizinfo-YYYY-MM-DD.json 에 저장 (나라장터 파일과 분리).

환경변수:
    BIZINFO_API_KEY : bizinfo.go.kr OPEN API 인증키 (crtfcKey, 필수)

사용법:
    python scripts/crawl_bizinfo.py
    python scripts/crawl_bizinfo.py --date 2026-05-18
    python scripts/crawl_bizinfo.py --all   # 키워드 무관 전체 저장 (디버그)
"""
from __future__ import annotations

import argparse
import json
import os
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

BIZINFO_ENDPOINT = "https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(ROOT / ".env")


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def _fmt_date(s: str) -> str:
    """bizinfo 가 쓰는 'YYYYMMDD' 또는 'YYYY-MM-DD' 를 'YYYY-MM-DD HH:MM:SS' 로 정규화."""
    s = (s or "").strip()
    if not s:
        return ""
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) == 8:
        # 마감일은 보통 23:59 까지로 간주
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]} 23:59:00"
    if len(digits) >= 12:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]} {digits[8:10]}:{digits[10:12]}:00"
    # 이미 ISO 형태면 그대로
    return s


def parse_period(s: str) -> tuple[str, str]:
    """bizinfo reqstBeginEndDe (예: '20220727 ~ 20220930') 를 (시작, 마감) ISO 로 분리.

    상시·수시 같은 비표준은 빈 문자열로 둔다.
    """
    if not s:
        return "", ""
    s = s.strip()
    if "~" in s:
        a, b = s.split("~", 1)
        return _fmt_date(a), _fmt_date(b)
    return "", _fmt_date(s)


def normalize_item(raw: dict) -> dict:
    """bizinfo 응답을 공통 스키마로 정규화. 원본 필드도 유지."""
    pblanc_id = str(raw.get("pblancId") or "").strip()
    title = str(raw.get("pblancNm") or raw.get("pblancSj") or "").strip()
    # 공고 상세 URL — bizinfo 표준 패턴
    url = str(raw.get("pblancUrl") or "").strip()
    if url and url.startswith("/"):
        url = "https://www.bizinfo.go.kr" + url
    elif not url and pblanc_id:
        url = f"https://www.bizinfo.go.kr/web/lay1/bbs/S1T122C128/AS/74/{pblanc_id}.do"

    org = str(raw.get("jrsdInsttNm") or raw.get("author") or "").strip()
    exec_org = str(raw.get("excInsttNm") or "").strip()
    period = str(raw.get("reqstBeginEndDe") or raw.get("reqstDt") or "").strip()
    begin_dt, end_dt = parse_period(period)
    category = str(raw.get("pldirSportRealmLclasCodeNm") or raw.get("lcategory") or "").strip()
    summary = str(raw.get("bsnsSumryCn") or raw.get("description") or "").strip()
    target = str(raw.get("trgetNm") or "").strip()
    hashtags = str(raw.get("hashTags") or raw.get("hashtags") or "").strip()
    created = str(raw.get("creatPnttm") or raw.get("pubDate") or "").strip()

    return {
        "source": "bizinfo",
        "source_id": pblanc_id,
        "title": title,
        "org": org,
        "exec_org": exec_org,
        "url": url,
        "open_dt_raw": begin_dt,
        "close_dt_raw": end_dt,
        "period_raw": period,
        "category": category,
        "summary": summary,
        "target": target,
        "hashtags": hashtags,
        "created_at": created,
        # 호환: build_index.py 의 기존 필드 매핑 (g2b 와 동일 키로)
        "bidNtceNm": title,
        "bidNtceNo": pblanc_id,
        "bidNtceOrd": "",
        "bidClseDt": end_dt,
        "ntceInsttNm": org,
        "dminsttNm": exec_org if exec_org and exec_org != org else "",
        "bidNtceDtlUrl": url,
    }


def fetch_all(key: str, timeout: int = 60) -> list[dict]:
    """전체 지원사업 공고를 한 번에 가져온다 (searchCnt=0 → 전체)."""
    params = {
        "crtfcKey": key,
        "dataType": "json",
        "searchCnt": 0,
    }
    try:
        r = requests.get(BIZINFO_ENDPOINT, params=params, timeout=timeout)
    except requests.RequestException as e:
        print(f"[HTTP 오류] {e}", file=sys.stderr)
        return []
    if not r.ok:
        print(f"[HTTP {r.status_code}] {r.text[:300]}", file=sys.stderr)
        return []
    try:
        data = r.json()
    except ValueError:
        print(f"[JSON 파싱 실패] {r.text[:300]}", file=sys.stderr)
        return []
    if isinstance(data, dict) and data.get("reqErr"):
        print(f"[API 에러] {data['reqErr']}", file=sys.stderr)
        return []
    # 응답 wrapper 후보 (RSS 변환 결과 등)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key_name in ("jsonArray", "result", "list", "items", "data", "item"):
            val = data.get(key_name)
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                # 한 건만 응답한 경우
                return [val]
        # channel.item 같은 RSS-스타일
        ch = data.get("channel") or data.get("rss")
        if isinstance(ch, dict):
            it = ch.get("item")
            if isinstance(it, list):
                return it
            if isinstance(it, dict):
                return [it]
    return []


def match_title(title: str, summary: str, keywords: list[str], case_sensitive: bool) -> list[str]:
    if not keywords:
        return []
    hay = (title or "") + " " + (summary or "")
    if not case_sensitive:
        hay = hay.lower()
    return [kw for kw in keywords if (kw if case_sensitive else kw.lower()) in hay]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="저장할 날짜 (YYYY-MM-DD, 기본=오늘 KST)")
    parser.add_argument("--all", action="store_true", help="키워드 무시하고 전체 저장 (디버그)")
    args = parser.parse_args()

    cfg = load_config()
    keywords = [k.strip() for k in cfg.get("keywords", []) if k and k.strip()]
    exclude = [k.strip() for k in cfg.get("exclude_keywords", []) if k and k.strip()]
    case_sensitive = bool(cfg.get("case_sensitive", False))

    key = os.environ.get("BIZINFO_API_KEY", "").strip()
    if not key:
        print("ERROR: BIZINFO_API_KEY 환경변수가 비어있습니다. .env 또는 GitHub Secret 에 설정하세요.", file=sys.stderr)
        return 2

    date_str = args.date or datetime.now(tz=KST).date().isoformat()
    print(f"=== 기업마당 수집 시작 ({date_str}) ===")
    print(f"키워드 ({len(keywords)}): {keywords}")
    if args.all:
        print("--all 모드: 키워드 필터 건너뜀")

    raw_items = fetch_all(key)
    print(f"수신: {len(raw_items)}건")

    seen: dict[str, dict] = {}
    filtered_out = 0
    for raw in raw_items:
        norm = normalize_item(raw)
        sid = norm["source_id"] or norm["title"]
        if not sid:
            continue

        matched = match_title(norm["title"], norm["summary"], keywords, case_sensitive)
        if not args.all and keywords and not matched:
            filtered_out += 1
            continue

        # 제외 키워드 (제목 + 사업개요)
        hay = norm["title"] + " " + norm["summary"]
        if not case_sensitive:
            hay = hay.lower()
        if any((x if case_sensitive else x.lower()) in hay for x in exclude):
            filtered_out += 1
            continue

        norm["matched_keywords"] = matched
        seen[sid] = norm

    out_dir = DATA_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"bizinfo-{date_str}.json"
    payload = {
        "source": "bizinfo",
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
