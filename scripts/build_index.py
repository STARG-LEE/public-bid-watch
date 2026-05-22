"""
data/*.json 에 쌓인 일별 수집 결과를 시나리오별 대시보드 인덱스로 합친다.

산출물:
    docs/data/scenarios.json
        {"generated_at": ..., "scenarios": [{slug, title, description, stats}]}

    docs/data/index-{slug}.json   (시나리오마다 한 개)
        {
          "generated_at": ...,
          "scenario": {slug, title, description, keywords, match_mode, ...},
          "stats": {"open": N, "closing_soon": N, "closed": N, "total": N, "by_source": {...}},
          "closing_soon": [...],
          "open": [...],
          "closed": [...]
        }

config.keep_days 보다 오래된 data/ 파일은 스킵한다.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except Exception:
    KST = timezone(timedelta(hours=9))

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"
RELEVANCE_DIR = DATA_DIR / "_relevance"
OUT_DIR = ROOT / "docs" / "data"

sys.path.insert(0, str(ROOT / "scripts"))
from scenarios import GlobalConfig, Scenario, load_scenarios  # noqa: E402

SOON_THRESHOLD_DAYS = 7


def load_relevance_cache(slug: str) -> dict:
    path = RELEVANCE_DIR / f"{slug}.json"
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def parse_bid_dt(s: str) -> datetime | None:
    """나라장터 API의 날짜 문자열을 KST datetime으로 파싱.

    관측된 포맷: '2026-04-30 18:00:00', '202604301800', '2026-04-30 18:00'
    """
    if not s:
        return None
    s = s.strip()
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y%m%d%H%M%S",
        "%Y%m%d%H%M",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=KST)
        except ValueError:
            continue
    return None


def iter_daily_files(keep_days: int) -> dict[str, list[Path]]:
    """data/ 의 일별 파일을 source 별로 분리해 반환.

    g2b:     YYYY-MM-DD.json
    bizinfo: bizinfo-YYYY-MM-DD.json
    iris:    iris-YYYY-MM-DD.json
    nrf:     nrf-YYYY-MM-DD.json
    iitp:    iitp-YYYY-MM-DD.json
    """
    PREFIXES = {"bizinfo-": "bizinfo", "iris-": "iris", "nrf-": "nrf", "iitp-": "iitp"}
    out: dict[str, list[Path]] = {"g2b": [], "bizinfo": [], "iris": [], "nrf": [], "iitp": []}
    if not DATA_DIR.exists():
        return out
    cutoff = (datetime.now(tz=KST) - timedelta(days=keep_days)).date()
    for p in sorted(DATA_DIR.glob("*.json")):
        stem = p.stem
        if stem.startswith("_"):
            continue
        source = "g2b"
        date_str = stem
        for prefix, src in PREFIXES.items():
            if stem.startswith(prefix):
                source = src
                date_str = stem[len(prefix):]
                break
        try:
            file_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date >= cutoff:
            out[source].append(p)
    return out


def match_keywords(title: str, keywords: list[str], case_sensitive: bool) -> list[str]:
    """제목에 매치되는 모든 키워드를 원본 순서대로 반환."""
    if not title or not keywords:
        return []
    haystack = title if case_sensitive else title.lower()
    return [kw for kw in keywords if (kw if case_sensitive else kw.lower()) in haystack]


def has_excluded(text: str, exclude_keywords: list[str], case_sensitive: bool) -> bool:
    if not exclude_keywords:
        return False
    haystack = text if case_sensitive else text.lower()
    return any((kw if case_sensitive else kw.lower()) in haystack for kw in exclude_keywords)


def merge_g2b(
    files: list[Path],
    sc: Scenario,
    service_divs: list[str],
    relevance_cache: dict,
) -> list[dict]:
    """g2b 파일들에서 시나리오 키워드에 매치되는 항목만 모은다.

    필터 순서: service_divs → matched_keywords 존재 → match_mode=all → exclude_keywords.
    관련성 점수는 시나리오별 캐시에서 가져와 첨부 (필터는 호출자가 적용).
    """
    div_set = set(service_divs) if service_divs else None
    merged: dict[str, dict] = {}
    for path in files:
        try:
            with path.open(encoding="utf-8") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        collected_at = payload.get("date") or path.stem
        for item in payload.get("items", []):
            if div_set is not None:
                div = str(item.get("srvceDivNm") or "").strip()
                if div not in div_set:
                    continue
            title = str(item.get("bidNtceNm") or "")
            matched = match_keywords(title, sc.keywords, sc.case_sensitive)
            if sc.keywords and not matched:
                continue
            if sc.match_mode == "all" and sc.keywords and len(matched) != len(sc.keywords):
                continue
            if has_excluded(title, sc.exclude_keywords, sc.case_sensitive):
                continue

            bid_no = item.get("bidNtceNo")
            bid_ord = item.get("bidNtceOrd")
            key = f"g2b:{bid_no}-{bid_ord}"
            merged_item = dict(item)
            existing_seen = merged.get(key, {}).get("first_seen_date", collected_at)
            merged_item["first_seen_date"] = (
                existing_seen if existing_seen < collected_at else collected_at
            )
            merged_item["last_seen_date"] = collected_at
            merged_item["source"] = "g2b"
            merged_item["matched_keywords"] = matched
            score_entry = relevance_cache.get(f"{bid_no}-{bid_ord}")
            if score_entry:
                merged_item["_relevance_score"] = score_entry.get("score")
                merged_item["_relevance_reason"] = score_entry.get("reason", "")
            merged[key] = merged_item
    return list(merged.values())


def merge_simple_source(
    files: list[Path],
    sc: Scenario,
    source_label: str,
    key_prefix: str,
) -> list[dict]:
    """source_id 기반 단순 병합 (bizinfo/iris/nrf/iitp 공통).

    시나리오 키워드를 (title + summary) 에 매칭하고, 매치 안 된 항목은 제외.
    exclude_keywords / match_mode=all 도 동일 텍스트에 적용.
    """
    merged: dict[str, dict] = {}
    for path in files:
        try:
            with path.open(encoding="utf-8") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        collected_at = payload.get("date") or path.stem
        for item in payload.get("items", []):
            sid = item.get("source_id")
            if not sid:
                continue
            title = str(item.get("title") or item.get("bidNtceNm") or "")
            summary = str(item.get("summary") or "")
            text = title + " " + summary
            matched = match_keywords(text, sc.keywords, sc.case_sensitive)
            if sc.keywords and not matched:
                continue
            if sc.match_mode == "all" and sc.keywords and len(matched) != len(sc.keywords):
                continue
            if has_excluded(text, sc.exclude_keywords, sc.case_sensitive):
                continue

            key = f"{key_prefix}:{sid}"
            merged_item = dict(item)
            existing_seen = merged.get(key, {}).get("first_seen_date", collected_at)
            merged_item["first_seen_date"] = (
                existing_seen if existing_seen < collected_at else collected_at
            )
            merged_item["last_seen_date"] = collected_at
            merged_item["source"] = source_label
            merged_item["matched_keywords"] = matched
            merged[key] = merged_item
    return list(merged.values())


def dedup_by_bid_no(items: list[dict]) -> tuple[list[dict], int]:
    """같은 bidNtceNo 그룹에서 bidNtceOrd 가 가장 큰(최신 차수) 항목만 남긴다."""
    latest: dict[str, dict] = {}
    for it in items:
        bid_no = str(it.get("bidNtceNo") or "")
        if not bid_no:
            latest[f"_no_id_{id(it)}"] = it
            continue
        try:
            ord_n = int(it.get("bidNtceOrd") or 0)
        except (ValueError, TypeError):
            ord_n = 0
        prev = latest.get(bid_no)
        if prev is None:
            latest[bid_no] = it
            continue
        try:
            prev_ord = int(prev.get("bidNtceOrd") or 0)
        except (ValueError, TypeError):
            prev_ord = 0
        if ord_n > prev_ord:
            latest[bid_no] = it
    kept = list(latest.values())
    removed = len(items) - len(kept)
    return kept, removed


def classify(items: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    now = datetime.now(tz=KST)
    soon_cutoff = now + timedelta(days=SOON_THRESHOLD_DAYS)

    open_list: list[dict] = []
    soon_list: list[dict] = []
    closed_list: list[dict] = []

    for it in items:
        close_dt = parse_bid_dt(it.get("bidClseDt", ""))
        enriched = dict(it)
        if close_dt is not None:
            enriched["_bidClseDt_iso"] = close_dt.isoformat()
            remaining = close_dt - now
            enriched["_hours_remaining"] = round(remaining.total_seconds() / 3600, 1)
        else:
            enriched["_bidClseDt_iso"] = ""
            enriched["_hours_remaining"] = None

        if close_dt is None:
            open_list.append(enriched)
        elif close_dt < now:
            closed_list.append(enriched)
        elif close_dt <= soon_cutoff:
            soon_list.append(enriched)
        else:
            open_list.append(enriched)

    def key_open(x: dict) -> float:
        h = x.get("_hours_remaining")
        return h if h is not None else float("inf")

    open_list.sort(key=key_open)
    soon_list.sort(key=key_open)
    closed_list.sort(key=lambda x: x.get("_bidClseDt_iso") or "", reverse=True)
    return open_list, soon_list, closed_list


def count_source(items: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        s = it.get("source", "g2b")
        out[s] = out.get(s, 0) + 1
    return out


def build_scenario(
    sc: Scenario,
    files_by_src: dict[str, list[Path]],
    service_divs: list[str],
) -> dict:
    """단일 시나리오의 인덱스 payload 를 생성."""
    cache = load_relevance_cache(sc.slug)
    rf = sc.relevance_filter
    print(f"\n[{sc.slug}] 키워드 {len(sc.keywords)}개 / 캐시 {len(cache)}건 / min_score={rf.min_score if rf.enabled else '-'}")

    g2b_items = merge_g2b(files_by_src["g2b"], sc, service_divs, cache)
    biz_items = merge_simple_source(files_by_src["bizinfo"], sc, "bizinfo", "bizinfo")
    iris_items = merge_simple_source(files_by_src["iris"], sc, "iris", "iris")
    nrf_items = merge_simple_source(files_by_src["nrf"], sc, "nrf", "nrf")
    iitp_items = merge_simple_source(files_by_src["iitp"], sc, "iitp", "iitp")
    print(
        f"[{sc.slug}] 매치: g2b {len(g2b_items)} / bizinfo {len(biz_items)} / "
        f"iris {len(iris_items)} / nrf {len(nrf_items)} / iitp {len(iitp_items)}"
    )

    # 관련성 필터는 g2b 만 (다른 소스는 캐시 없음 → 통과)
    skipped_low = 0
    skipped_unscored = 0
    if rf.enabled and rf.min_score > 0:
        kept: list[dict] = []
        for it in g2b_items:
            score = it.get("_relevance_score")
            if score is None:
                kept.append(it)
                skipped_unscored += 1
                continue
            if score < rf.min_score:
                skipped_low += 1
                continue
            kept.append(it)
        g2b_items = kept
        print(
            f"[{sc.slug}] 관련성 필터 후 g2b: {len(g2b_items)}건 "
            f"(저점수 제외 {skipped_low}, 미평가 통과 {skipped_unscored})"
        )

    g2b_items, removed_dup = dedup_by_bid_no(g2b_items)
    if removed_dup > 0:
        print(f"[{sc.slug}] g2b 중복 차수 제거: {removed_dup}건")

    merged = g2b_items + biz_items + iris_items + nrf_items + iitp_items
    open_list, soon_list, closed_list = classify(merged)
    print(
        f"[{sc.slug}] 최종: open={len(open_list)} soon={len(soon_list)} closed={len(closed_list)} "
        f"total={len(merged)}"
    )

    stats = {
        "open": len(open_list),
        "closing_soon": len(soon_list),
        "closed": len(closed_list),
        "total": len(merged),
        "by_source": {
            "closing_soon": count_source(soon_list),
            "open": count_source(open_list),
            "closed": count_source(closed_list),
            "total": count_source(merged),
        },
    }

    payload = {
        "generated_at": datetime.now(tz=KST).isoformat(timespec="seconds"),
        "scenario": {
            "slug": sc.slug,
            "title": sc.title,
            "description": sc.description,
            "keywords": sc.keywords,
            "exclude_keywords": sc.exclude_keywords,
            "match_mode": sc.match_mode,
            "case_sensitive": sc.case_sensitive,
            "relevance_filter": {
                "enabled": rf.enabled,
                "min_score": rf.min_score if rf.enabled else None,
                "model": rf.model if rf.enabled else None,
                "provider": rf.provider if rf.enabled else None,
            },
        },
        "config": {
            "service_divs": service_divs,
            "sources": ["g2b", "bizinfo", "iris", "nrf", "iitp"],
        },
        "stats": stats,
        "closing_soon": soon_list,
        "open": open_list,
        "closed": closed_list,
    }
    return payload


def main() -> int:
    cfg = GlobalConfig.load(CONFIG_PATH)
    scenarios = load_scenarios(ROOT, cfg.scenarios_dir)
    files_by_src = iter_daily_files(cfg.keep_days)
    print(
        "파일 수: " + " / ".join(f"{k}={len(v)}" for k, v in files_by_src.items())
    )
    if cfg.service_divs:
        print(f"용역구분 화이트리스트(srvceDivNm): {cfg.service_divs}")
    print(f"시나리오 {len(scenarios)}개: {[s.slug for s in scenarios]}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    menu_entries: list[dict] = []
    for sc in scenarios:
        payload = build_scenario(sc, files_by_src, cfg.service_divs)
        out_path = OUT_DIR / f"index-{sc.slug}.json"
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"[{sc.slug}] 저장: {out_path.relative_to(ROOT)}")
        menu_entries.append({
            "slug": sc.slug,
            "title": sc.title,
            "description": sc.description,
            "keywords": sc.keywords,
            "stats": payload["stats"],
        })

    menu = {
        "generated_at": datetime.now(tz=KST).isoformat(timespec="seconds"),
        "default_slug": scenarios[0].slug if scenarios else None,
        "scenarios": menu_entries,
    }
    menu_path = OUT_DIR / "scenarios.json"
    with menu_path.open("w", encoding="utf-8") as f:
        json.dump(menu, f, ensure_ascii=False, indent=2)
    print(f"\n메뉴 저장: {menu_path.relative_to(ROOT)} ({len(menu_entries)}개 시나리오)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
