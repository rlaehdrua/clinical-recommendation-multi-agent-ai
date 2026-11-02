"""실험 실행 전 검사 (0.2, I-9). 정보원 카탈로그 해시 기록과 비용 근거에 따른 주장 범위.

- require_catalog_hash: ExperimentSpec에 정보원 카탈로그 해시가 있고 실제 카탈로그와 같아야 실행 가능.
  (같은 조건 비교(M10)는 카탈로그가 같다는 것을 해시로 보여야 함)
- with_catalog_hash: 카탈로그 해시를 채운 새 ExperimentSpec (검증을 다시 거침)
- catalog_claims: 가정값(cost_basis="assumed") 정보원이 하나라도 있으면 분 단위 결과는 정책 간 상대 비교에만 쓰고
  실제 병원 업무시간 절감을 주장하지 않음.
WP6 실행기(서연)는 실행 시작 전에 require_catalog_hash를 부르고, 결과 manifest에 catalog_claims를 남기면 됩니다.
"""

from __future__ import annotations

from .bundles import SourceCatalog
from .contracts import DataVersion, ExperimentSpec


class ExperimentSpecError(ValueError):
    pass


def require_catalog_hash(spec: ExperimentSpec, catalog: SourceCatalog) -> str:
    h = spec.data_version.source_catalog_hash
    if not h:
        raise ExperimentSpecError("source_catalog_hash_missing")
    if h != catalog.digest():
        raise ExperimentSpecError("source_catalog_hash_mismatch")
    return h


def with_catalog_hash(spec: ExperimentSpec, catalog: SourceCatalog) -> ExperimentSpec:
    data = spec.model_dump()
    data["data_version"] = DataVersion(**{**spec.data_version.model_dump(), "source_catalog_hash": catalog.digest()})
    return ExperimentSpec.model_validate(data)


def catalog_claims(catalog: SourceCatalog) -> dict:
    assumed = sorted(s for s, src in catalog.sources.items() if src.cost_basis == "assumed"
                     and (src.request_cost.coordinator_minutes or src.request_cost.clinician_minutes
                          or src.per_item_minutes))
    return {"catalog_hash": catalog.digest(), "assumed_cost_sources": assumed,
            "absolute_time_claims_allowed": not assumed,
            "allowed_claims": "relative_policy_comparison_only" if assumed else "absolute_with_cost_references"}


__all__ = ["ExperimentSpecError", "require_catalog_hash", "with_catalog_hash", "catalog_claims"]
