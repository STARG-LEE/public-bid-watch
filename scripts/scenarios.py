"""
시나리오 및 글로벌 설정 로딩 공통 모듈.

config.json 은 글로벌 설정 (API 엔드포인트, service_divs, keep_days 등) 만 담고,
시나리오별 keywords/relevance_filter 는 scenarios/*.json 으로 분리된다.

크롤은 모든 시나리오 키워드의 합집합으로 한 번만 수행하고
(API 호출 절약), 관련성 평가와 인덱스 빌드는 시나리오 단위로 수행된다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RelevanceFilter:
    enabled: bool = False
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    min_score: int = 0
    rate_limit_delay_seconds: float = 0.2
    context: str = ""


@dataclass
class Scenario:
    slug: str
    title: str
    description: str
    keywords: list[str]
    exclude_keywords: list[str]
    match_mode: str  # "any" | "all"
    case_sensitive: bool
    relevance_filter: RelevanceFilter

    @classmethod
    def from_dict(cls, data: dict, *, file_slug: str) -> "Scenario":
        slug = str(data.get("slug") or file_slug).strip()
        if not slug:
            raise ValueError(f"시나리오 slug 가 비어있습니다 (파일: {file_slug})")
        if slug != file_slug:
            raise ValueError(
                f"시나리오 slug({slug!r}) 와 파일명({file_slug!r}) 이 일치하지 않습니다."
            )
        rf_raw = data.get("relevance_filter") or {}
        rf = RelevanceFilter(
            enabled=bool(rf_raw.get("enabled", False)),
            provider=str(rf_raw.get("provider", "openai")),
            model=str(rf_raw.get("model", "gpt-4o-mini")),
            min_score=int(rf_raw.get("min_score", 0)),
            rate_limit_delay_seconds=float(rf_raw.get("rate_limit_delay_seconds", 0.2)),
            context=str(rf_raw.get("context", "") or "").strip(),
        )
        return cls(
            slug=slug,
            title=str(data.get("title") or slug),
            description=str(data.get("description") or ""),
            keywords=[k.strip() for k in (data.get("keywords") or []) if k and str(k).strip()],
            exclude_keywords=[k.strip() for k in (data.get("exclude_keywords") or []) if k and str(k).strip()],
            match_mode=str(data.get("match_mode") or "any"),
            case_sensitive=bool(data.get("case_sensitive", False)),
            relevance_filter=rf,
        )

    def relevance_cache_path(self, root: Path) -> Path:
        """시나리오별 LLM 점수 캐시 파일 경로."""
        return root / "data" / "_relevance" / f"{self.slug}.json"


@dataclass
class GlobalConfig:
    scenarios_dir: str
    service_divs: list[str]
    lookback_days: int
    keep_days: int
    api_endpoint: str
    api_operation: str
    api_num_of_rows: int
    api_max_pages: int
    raw: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "GlobalConfig":
        with path.open(encoding="utf-8") as f:
            raw = json.load(f)
        api = raw.get("api", {})
        return cls(
            scenarios_dir=str(raw.get("scenarios_dir") or "scenarios"),
            service_divs=[v.strip() for v in (raw.get("service_divs") or []) if v and str(v).strip()],
            lookback_days=int(raw.get("lookback_days", 1)),
            keep_days=int(raw.get("keep_days", 60)),
            api_endpoint=str(api.get("endpoint", "https://apis.data.go.kr/1230000/ad/BidPublicInfoService")),
            api_operation=str(api.get("operation", "getBidPblancListInfoServcPPSSrch")),
            api_num_of_rows=int(api.get("num_of_rows", 500)),
            api_max_pages=int(api.get("max_pages", 20)),
            raw=raw,
        )


def load_scenarios(root: Path, scenarios_dir: str = "scenarios") -> list[Scenario]:
    """scenarios/ 디렉토리에서 모든 시나리오를 로드.

    파일명(확장자 제외)이 slug 로 사용된다. `_` 로 시작하는 파일은 건너뜀
    (예: `_proposed/` 안의 초안).
    """
    base = root / scenarios_dir
    if not base.exists():
        raise FileNotFoundError(f"시나리오 디렉토리가 없습니다: {base}")
    out: list[Scenario] = []
    for path in sorted(base.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            with path.open(encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            raise ValueError(f"시나리오 파일 파싱 실패 ({path.name}): {e}") from e
        out.append(Scenario.from_dict(data, file_slug=path.stem))
    if not out:
        raise ValueError(f"활성 시나리오가 0개입니다: {base}")
    return out


def union_keywords(scenarios: list[Scenario]) -> list[str]:
    """모든 시나리오의 키워드 합집합 (순서 보존, 중복 제거).

    crawl.py 가 단일 API 호출 세트로 모든 시나리오를 커버하기 위해 사용.
    """
    seen: set[str] = set()
    out: list[str] = []
    for sc in scenarios:
        for kw in sc.keywords:
            if kw not in seen:
                seen.add(kw)
                out.append(kw)
    return out


def find_scenario(scenarios: list[Scenario], slug: str) -> Scenario | None:
    for sc in scenarios:
        if sc.slug == slug:
            return sc
    return None
