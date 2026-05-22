"""
시나리오 제안 자동 처리 스크립트.

GitHub Issue (라벨: scenario-proposal) 본문을 LLM 에게 보내 시나리오 JSON
초안을 만들고 `scenarios/{slug}.json` 으로 저장한다. 워크플로우에서 이
스크립트 실행 → 변경분으로 PR 자동 생성 → 관리자 머지 → 다음 크롤부터 적용.

환경변수:
    OPENAI_API_KEY    필수 (이 스크립트는 OpenAI 만 사용)
    GITHUB_OUTPUT     선택 — GitHub Actions 가 set 한 출력 파일. slug 를 기록.

사용법:
    python scripts/propose_scenario.py \
        --issue-body-file /tmp/issue_body.md \
        --issue-title "[시나리오] 환경" \
        --issue-author starg-lee \
        --issue-number 5

    python scripts/propose_scenario.py --issue-body-file body.md --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    KST = ZoneInfo("Asia/Seoul")
except Exception:
    KST = timezone(timedelta(hours=9))

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = ROOT / "scenarios"


SYSTEM_PROMPT = """\
당신은 한국 정부 입찰공고 트래커의 시나리오 빌더입니다.
사용자의 자연어 제안을 받아 다음 스키마의 시나리오 JSON 을 만드세요.

스키마 (반드시 이 형태):
{
  "slug": "<영문 소문자-하이픈, 30자 이내>",
  "title": "<한국어, 20자 이내 — 대시보드 탭에 표시>",
  "description": "<한국어, 60자 이내 한 줄 요약>",
  "keywords": ["키워드1", "키워드2", ...],
  "exclude_keywords": ["제외할 단어1", ...],
  "match_mode": "any",
  "case_sensitive": false,
  "relevance_filter": {
    "enabled": true,
    "provider": "openai",
    "model": "gpt-5-mini",
    "min_score": 3,
    "rate_limit_delay_seconds": 0.2,
    "context": "<한국어, 200~400자. 사용자 설명을 정제. 무관 분야도 명시.>"
  }
}

작성 규칙:
1. **keywords**: 공고 제목에 부분일치로 검색됨. 8~15개 권장.
   - 한국어 위주, 영문 약어(AI, LLM, ESG 등) 같이 사용.
   - 너무 짧은 단어("AI" 같은 2글자 미만) 는 피해. 다만 흔한 약어는 OK.
   - 합성어/부분 단어: 예) "재생에너지", "태양광", "에너지" (단독은 모호하면 제외)
2. **exclude_keywords**: 사용자가 "무관" 으로 명시한 분야의 시그널 단어. 5개 이내. 자주 오탐되는 게 아니면 비워둬.
3. **slug**: 사용자가 명시했으면 그대로. 없으면 description 에서 추론. 영문 소문자, 하이픈, 30자 이내.
4. **context**: 사용자 description 을 정제. 반드시 "관련 분야" 와 "무관 분야" 둘 다 명시. score_relevance.py 가 이 텍스트로 0-5점 평가하므로 가장 중요.
5. **provider**: 사용자가 제공한 값이 'anthropic' 또는 'openai' 면 그걸 사용. 없거나 다른 값이면 'openai'.
   - provider=openai 면 model="gpt-5-mini"
   - provider=anthropic 이면 model="claude-haiku-4-5-20251001"

JSON 외에는 어떤 텍스트도 출력하지 마세요. 코드 블록 마커도 금지.
"""


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


def slugify(s: str) -> str:
    """LLM 응답이 이상하면 fallback 으로 쓰는 안전 slug 화."""
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9가-힣\s-]+", "", s)
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-{2,}", "-", s)
    return s[:30].strip("-") or "scenario"


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


def call_openai(model: str, system: str, user: str) -> dict | None:
    from openai import OpenAI  # lazy import
    client = OpenAI()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        max_completion_tokens=4000,
    )
    return parse_json_loose(resp.choices[0].message.content or "")


def validate_scenario(data: dict, existing_slugs: set[str]) -> tuple[bool, str]:
    """LLM 출력이 정상 시나리오 스키마인지 검증."""
    required = ["slug", "title", "description", "keywords", "match_mode", "relevance_filter"]
    for k in required:
        if k not in data:
            return False, f"필드 누락: {k}"

    slug = data["slug"]
    if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,29}", slug):
        return False, f"slug 형식 오류 (영문 소문자/숫자/하이픈, 30자 이내): {slug!r}"
    if slug in existing_slugs:
        return False, f"이미 존재하는 slug: {slug!r}"

    kws = data.get("keywords") or []
    if not isinstance(kws, list) or len(kws) < 3:
        return False, f"keywords 가 너무 적습니다 (최소 3개): {kws}"
    if any(not isinstance(k, str) or not k.strip() for k in kws):
        return False, "keywords 에 빈 문자열이 있습니다"

    rf = data.get("relevance_filter") or {}
    if not isinstance(rf, dict):
        return False, "relevance_filter 가 객체가 아닙니다"
    if not rf.get("context"):
        return False, "relevance_filter.context 가 비어있습니다"
    if rf.get("provider") not in ("openai", "anthropic"):
        return False, f"provider 값이 잘못됨: {rf.get('provider')!r}"

    return True, ""


def build_user_prompt(title: str, body: str, author: str) -> str:
    return (
        f"이슈 제목: {title}\n"
        f"제안자: {author}\n\n"
        f"--- 이슈 본문 ---\n{body}\n"
    )


def existing_slugs() -> set[str]:
    if not SCENARIOS_DIR.exists():
        return set()
    return {p.stem for p in SCENARIOS_DIR.glob("*.json") if not p.name.startswith("_")}


def write_github_output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{key}={value}\n")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--issue-body-file", type=Path, required=True, help="이슈 본문 텍스트 파일")
    p.add_argument("--issue-title", type=str, default="", help="이슈 제목")
    p.add_argument("--issue-author", type=str, default="", help="이슈 작성자 username")
    p.add_argument("--issue-number", type=int, default=0, help="이슈 번호 (메타데이터용)")
    p.add_argument("--model", type=str, default="gpt-5-mini", help="이 스크립트가 사용할 OpenAI 모델")
    p.add_argument("--dry-run", action="store_true", help="API 호출하지만 파일 안 씀")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if not os.environ.get("OPENAI_API_KEY", "").strip():
        print("ERROR: OPENAI_API_KEY 환경변수가 비어있습니다.", file=sys.stderr)
        return 2

    if not args.issue_body_file.exists():
        print(f"ERROR: 이슈 본문 파일이 없습니다: {args.issue_body_file}", file=sys.stderr)
        return 2

    body = args.issue_body_file.read_text(encoding="utf-8")
    if len(body.strip()) < 20:
        print("ERROR: 이슈 본문이 너무 짧습니다 (20자 이상 필요).", file=sys.stderr)
        return 2

    print(f"이슈 #{args.issue_number} ({args.issue_author}) — '{args.issue_title}'")
    print(f"본문 {len(body)}자")

    user = build_user_prompt(args.issue_title, body, args.issue_author)
    try:
        data = call_openai(args.model, SYSTEM_PROMPT, user)
    except Exception as e:
        print(f"ERROR: LLM 호출 실패 — {e}", file=sys.stderr)
        return 3

    if not data:
        print("ERROR: LLM 응답을 JSON 으로 파싱하지 못했습니다.", file=sys.stderr)
        return 3

    # provider/model 강제 정규화 (LLM 이 가끔 model 을 잘못 채움)
    rf = data.get("relevance_filter") or {}
    provider = rf.get("provider", "openai")
    if provider == "openai":
        rf["model"] = rf.get("model") or "gpt-5-mini"
    elif provider == "anthropic":
        rf["model"] = rf.get("model") or "claude-haiku-4-5-20251001"
    rf.setdefault("enabled", True)
    rf.setdefault("min_score", 3)
    rf.setdefault("rate_limit_delay_seconds", 0.2)
    data["relevance_filter"] = rf
    data.setdefault("match_mode", "any")
    data.setdefault("case_sensitive", False)
    data.setdefault("exclude_keywords", [])

    ok, reason = validate_scenario(data, existing_slugs())
    if not ok:
        print(f"ERROR: 시나리오 검증 실패 — {reason}", file=sys.stderr)
        print("LLM 출력:", json.dumps(data, ensure_ascii=False, indent=2), file=sys.stderr)
        return 4

    slug = data["slug"]
    out_path = SCENARIOS_DIR / f"{slug}.json"
    print(f"\n생성될 시나리오: {slug}")
    print(f"  제목: {data['title']}")
    print(f"  설명: {data['description']}")
    print(f"  키워드 {len(data['keywords'])}개: {data['keywords']}")
    print(f"  제외 키워드: {data.get('exclude_keywords') or '(없음)'}")
    print(f"  provider: {rf['provider']} / model: {rf['model']} / min_score: {rf['min_score']}")
    print(f"  context: {rf['context'][:120]}...")

    if args.dry_run:
        print("\n--dry-run: 파일 안 씀")
        return 0

    SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\n저장: {out_path.relative_to(ROOT)}")

    write_github_output("slug", slug)
    write_github_output("title", data["title"])
    write_github_output("description", data["description"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
