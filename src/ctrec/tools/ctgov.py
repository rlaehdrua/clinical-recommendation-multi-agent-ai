"""ClinicalTrials.gov API v2 클라이언트 (공개 데이터).

- 문서: https://clinicaltrials.gov/data-api/api
- 이용 약관: https://clinicaltrials.gov/about-site/terms-conditions
가져온 원본은 data/trials/<NCT ID>.json 으로 캐시해 재현성을 확보합니다.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from .. import config

API = "https://clinicaltrials.gov/api/v2/studies"
TIMEOUT = 30


def _get(d: dict, *path: str, default=None):
    for key in path:
        if not isinstance(d, dict) or key not in d:
            return default
        d = d[key]
    return d


def to_trial_record(study: dict[str, Any]) -> dict[str, Any]:
    """CT.gov study JSON -> 파이프라인 내부 TrialRecord(dict)."""
    ps = study.get("protocolSection", {})
    nct_id = _get(ps, "identificationModule", "nctId")
    return {
        "trial_id": nct_id,
        "title": _get(ps, "identificationModule", "briefTitle", default=""),
        "official_title": _get(ps, "identificationModule", "officialTitle", default=""),
        "overall_status": _get(ps, "statusModule", "overallStatus", default="UNKNOWN"),
        "phases": _get(ps, "designModule", "phases", default=[]),
        "study_type": _get(ps, "designModule", "studyType", default=""),
        "conditions": _get(ps, "conditionsModule", "conditions", default=[]),
        "keywords": _get(ps, "conditionsModule", "keywords", default=[]),
        "interventions": [
            {"type": i.get("type"), "name": i.get("name"), "description": i.get("description", "")}
            for i in _get(ps, "armsInterventionsModule", "interventions", default=[])
        ],
        "brief_summary": _get(ps, "descriptionModule", "briefSummary", default=""),
        "detailed_description": _get(ps, "descriptionModule", "detailedDescription", default=""),
        "eligibility_criteria": _get(ps, "eligibilityModule", "eligibilityCriteria", default=""),
        "sex": _get(ps, "eligibilityModule", "sex", default="ALL"),
        "minimum_age": _get(ps, "eligibilityModule", "minimumAge"),
        "maximum_age": _get(ps, "eligibilityModule", "maximumAge"),
        "healthy_volunteers": _get(ps, "eligibilityModule", "healthyVolunteers"),
        "locations": [
            {"facility": loc.get("facility"), "city": loc.get("city"), "country": loc.get("country")}
            for loc in _get(ps, "contactsLocationsModule", "locations", default=[])[:20]
        ],
        "source": {
            "name": "ClinicalTrials.gov (U.S. National Library of Medicine)",
            "url": f"https://clinicaltrials.gov/study/{nct_id}",
            "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        "synthetic": False,
    }


def fetch_study(nct_id: str, *, use_cache: bool = True) -> dict[str, Any]:
    path = config.TRIALS_DIR / f"{nct_id}.json"
    if use_cache and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    resp = requests.get(f"{API}/{nct_id}", params={"format": "json"}, timeout=TIMEOUT)
    resp.raise_for_status()
    record = to_trial_record(resp.json())
    save_record(record)
    return record


def search_studies(
    condition: str,
    *,
    intervention: str | None = None,
    term: str | None = None,
    statuses: tuple[str, ...] = ("RECRUITING", "NOT_YET_RECRUITING"),
    max_results: int = 10,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "format": "json",
        "query.cond": condition,
        "pageSize": min(max_results, 100),
    }
    if intervention:
        params["query.intr"] = intervention
    if term:
        params["query.term"] = term
    if statuses:
        params["filter.overallStatus"] = ",".join(statuses)
    resp = requests.get(API, params=params, timeout=TIMEOUT)
    resp.raise_for_status()
    records = [to_trial_record(s) for s in resp.json().get("studies", [])]
    for r in records:
        save_record(r)
    return records[:max_results]


def save_record(record: dict[str, Any]) -> Path:
    config.TRIALS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.TRIALS_DIR / f"{record['trial_id']}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_local(path: Path) -> dict[str, Any]:
    """직접 입력한 시험 파일(JSON). 최소 필드: trial_id, title, eligibility_criteria."""
    record = json.loads(path.read_text(encoding="utf-8"))
    for key in ("trial_id", "eligibility_criteria"):
        if key not in record:
            raise ValueError(f"{path}: '{key}' 필드가 필요합니다")
    record.setdefault("title", record["trial_id"])
    for key, default in [
        ("brief_summary", ""), ("detailed_description", ""), ("conditions", []),
        ("interventions", []), ("phases", []), ("overall_status", "UNKNOWN"),
        ("sex", "ALL"), ("minimum_age", None), ("maximum_age", None), ("locations", []),
        ("source", {"name": "local file", "url": str(path)}), ("synthetic", False),
    ]:
        record.setdefault(key, default)
    return record
