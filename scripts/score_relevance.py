"""
LLM 기반 공고 관련성 평가 스크립트 (시나리오 인식 버전).

build_index 실행 직전에 호출되어, data/*.json 의 모든 공고를
LLM(OpenAI 또는 Anthropic)에게 보내 0-5점 관련성 점수를 받아온다.
이미 평가된 공고는 data/_relevance/{scenario_slug}.json 에 캐시되어 재호출하지 않는다.

같은 공고도 시나리오마다 context 가 다르므로 점수가 다를 수 있다 → 시나리오별로 캐시 분리.

환경변수:
    OPENAI_API_KEY    (provider=openai 일 때 필요)
    ANTHROPIC_API_KEY (provider=anthropic 일 때 필요)

사용법:
    python scripts/score_relevance.py                     # 모든 시나리오, 미평가 공고만
    python scripts/score_relevance.py --scenario foo      # 특정 시나리오만
    python scripts/score_relevance.py --limit 20          # 최대 20건만 (테스트용)
    python scripts/score_relevance.py --rescore-all       # 캐시 무시하고 전부 재평가
    python scripts/score_relevance.py --dry-run           # API 호출 없이 평가 대상만 출력
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
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
LEGACY_CACHE_PATH = DATA_DIR / "_relevance_cache.json"

sys.path.insert(0, str(ROOT / "scripts"))
from scenarios import GlobalConfig, Scenario, load_scenarios  # noqa: E402

DEFAULT_INSTRUCTION = (
    "당신은 한국 정부 입찰공고의 관련성 평가자입니다. "
    "사용자가 관심을 둔 분야 설명과 공고 정보를 보고 0-5점으로 관련성을 평가하세요.\n"
    "  5: 매우 관련 / 4: 관련 / 3: 부분 관련 / 2: 약하게 관련 / 1-0: 거의 무관\n"
    "주의: 공고명에 AI/데이터 같은 키워드가 들어가도 본업이 토목·건축·청소·임대처럼 "
    "전혀 다른 분야면 0-1점입니다. 점수는 공고의 실질적 본업이 사용자 분야와 얼마나 "
    "겹치는지로 판단하세요.\n"
    "JSON 형식으로만 답하세요: {\"score\": <0-5 정수>, \"reason\": \"<한 줄 이유, 60자 이내>\"}"
)


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv(ROOT / ".env")


def now_kst() -> datetime:
    return datetime.now(tz=KST)


def cache_path_for(slug: str) -> Path:
    return RELEVANCE_DIR / f"{slug}.json"


def load_cache(slug: str) -> dict:
    path = cache_path_for(slug)
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_cache(slug: str, cache: dict) -> None:
    RELEVANCE_DIR.mkdir(parents=True, exist_ok=True)
    path = cache_path_for(slug)
    with path.open("w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2, sort_keys=True)


def migrate_legacy_cache_if_needed(slug: str) -> None:
    """기존 _relevance_cache.json 을 새 위치 (_relevance/{slug}.json) 로 이동.

    Phase 1 일회성 마이그레이션. 새 캐시 파일이 이미 있으면 건드리지 않는다.
    """
    new_path = cache_path_for(slug)
    if new_path.exists() or not LEGACY_CACHE_PATH.exists():
        return
    try:
        with LEGACY_CACHE_PATH.open(encoding="utf-8") as f:
            legacy = json.load(f)
    except (json.JSONDecodeError, OSError):
        return
    RELEVANCE_DIR.mkdir(parents=True, exist_ok=True)
    with new_path.open("w", encoding="utf-8") as f:
        json.dump(legacy, f, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        LEGACY_CACHE_PATH.unlink()
    except OSError:
        pass
    print(
        f"[migrate] {LEGACY_CACHE_PATH.relative_to(ROOT)} → "
        f"{new_path.relative_to(ROOT)} ({len(legacy)}건)"
    )


def collect_unique_items(keep_days: int) -> dict[str, dict]:
    """data/*.json 의 모든 공고를 키 단위로 모음 (최근 keep_days 만)."""
    if not DATA_DIR.exists():
        return {}
    cutoff = (now_kst() - timedelta(days=keep_days)).date()
    items: dict[str, dict] = {}
    for path in sorted(DATA_DIR.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            file_date = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_date < cutoff:
            continue
        try:
            with path.open(encoding="utf-8") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        for it in payload.get("items", []):
            key = f"{it.get('bidNtceNo')}-{it.get('bidNtceOrd')}"
            items[key] = it
    return items


def build_user_prompt(item: dict) -> str:
    return (
        f"공고명: {item.get('bidNtceNm','')}\n"
        f"공고기관: {item.get('ntceInsttNm','')}\n"
        f"수요기관: {item.get('dminsttNm','')}\n"
        f"용역구분: {item.get('srvceDivNm','')}"
    )


def parse_json_loose(text: str) -> dict | None:
    """LLM 응답에서 JSON 부분만 안전하게 추출."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        text = "\n".join(lines)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def score_with_openai(model: str, system: str, user: str) -> dict | None:
    from openai import OpenAI  # lazy import
    client = OpenAI()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        max_completion_tokens=2000,
    )
    return parse_json_loose(resp.choices[0].message.content or "")


def score_with_anthropic(model: str, system: str, user: str) -> dict | None:
    import anthropic  # lazy import
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=200,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    return parse_json_loose(text)


def score_one(provider: str, model: str, system: str, user: str) -> dict | None:
    if provider == "openai":
        return score_with_openai(model, system, user)
    if provider == "anthropic":
        return score_with_anthropic(model, system, user)
    raise ValueError(f"지원하지 않는 provider: {provider!r} (openai|anthropic)")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--scenario", type=str, default=None, help="특정 시나리오 slug 만 평가")
    p.add_argument("--limit", type=int, default=None, help="시나리오당 최대 평가 건수 (테스트용)")
    p.add_argument("--rescore-all", action="store_true", help="캐시 무시하고 전부 재평가")
    p.add_argument("--dry-run", action="store_true", help="API 호출 없이 평가 대상만 출력")
    return p.parse_args()


def run_for_scenario(
    sc: Scenario,
    items: dict[str, dict],
    args: argparse.Namespace,
) -> bool:
    """단일 시나리오에 대해 평가 수행. 평가가 정상 종료되면 True."""
    rf = sc.relevance_filter
    if not rf.enabled:
        print(f"[{sc.slug}] relevance_filter.enabled=false → 스킵")
        return True
    if not rf.context:
        print(f"[{sc.slug}] ERROR: relevance_filter.context 가 비어있습니다.", file=sys.stderr)
        return False

    env_key = "OPENAI_API_KEY" if rf.provider == "openai" else "ANTHROPIC_API_KEY"
    if not args.dry_run and not os.environ.get(env_key, "").strip():
        print(f"[{sc.slug}] ERROR: {env_key} 환경변수가 비어있습니다.", file=sys.stderr)
        return False

    migrate_legacy_cache_if_needed(sc.slug)
    cache = {} if args.rescore_all else load_cache(sc.slug)
    pending_keys = [k for k in items if k not in cache]

    print(f"[{sc.slug}] provider={rf.provider} / model={rf.model} / min_score={rf.min_score}")
    print(
        f"[{sc.slug}] 평가 대상: {len(items)}개 / 미평가: {len(pending_keys)}개 / 캐시: {len(cache)}개"
    )

    if args.dry_run:
        for k in pending_keys[:10]:
            print(f"  - {k}: {items[k].get('bidNtceNm','')[:80]}")
        if len(pending_keys) > 10:
            print(f"  ... (+{len(pending_keys) - 10}건)")
        return True

    if args.limit is not None:
        pending_keys = pending_keys[: args.limit]

    system = (
        f"{DEFAULT_INSTRUCTION}\n\n"
        f"=== 사용자 관심 분야 ===\n{rf.context}"
    )

    now_iso = now_kst().isoformat(timespec="seconds")
    success = 0
    fail = 0
    for i, key in enumerate(pending_keys, 1):
        item = items[key]
        try:
            result = score_one(rf.provider, rf.model, system, build_user_prompt(item))
        except Exception as e:  # 모든 API/네트워크 오류
            print(f"  [{sc.slug}][{i}/{len(pending_keys)}] {key} ERROR: {e}", file=sys.stderr)
            fail += 1
            time.sleep(rf.rate_limit_delay_seconds)
            continue
        if not result or not isinstance(result.get("score"), (int, float)):
            print(f"  [{sc.slug}][{i}/{len(pending_keys)}] {key} 파싱 실패: {result}", file=sys.stderr)
            fail += 1
            time.sleep(rf.rate_limit_delay_seconds)
            continue
        score = max(0, min(5, int(result["score"])))
        reason = str(result.get("reason", ""))[:200]
        cache[key] = {
            "score": score,
            "reason": reason,
            "scored_at": now_iso,
            "model": rf.model,
            "provider": rf.provider,
        }
        success += 1
        if i % 10 == 0:
            save_cache(sc.slug, cache)
            print(f"  [{sc.slug}][{i}/{len(pending_keys)}] 진행중 (success={success}, fail={fail})")
        time.sleep(rf.rate_limit_delay_seconds)

    # keep_days 지나 더 이상 데이터에 없는 공고 캐시 정리
    valid_keys = set(items.keys())
    pruned_keys = [k for k in list(cache.keys()) if k not in valid_keys]
    for k in pruned_keys:
        del cache[k]

    save_cache(sc.slug, cache)
    print(f"[{sc.slug}] 완료: 신규 {success}건 / 실패 {fail}건 / 만료 정리 {len(pruned_keys)}건")
    print(f"[{sc.slug}] 캐시 총 {len(cache)}건 → {cache_path_for(sc.slug).relative_to(ROOT)}")
    return fail == 0


def main() -> int:
    args = parse_args()
    cfg = GlobalConfig.load(CONFIG_PATH)
    scenarios = load_scenarios(ROOT, cfg.scenarios_dir)

    if args.scenario:
        scenarios = [s for s in scenarios if s.slug == args.scenario]
        if not scenarios:
            print(f"ERROR: '--scenario {args.scenario}' 에 해당하는 시나리오가 없습니다.", file=sys.stderr)
            return 2

    items = collect_unique_items(cfg.keep_days)
    print(f"전체 공고 (최근 {cfg.keep_days}일): {len(items)}개")
    print(f"평가할 시나리오 {len(scenarios)}개: {[s.slug for s in scenarios]}")
    print()

    all_ok = True
    for sc in scenarios:
        ok = run_for_scenario(sc, items, args)
        all_ok = all_ok and ok
        print()
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
