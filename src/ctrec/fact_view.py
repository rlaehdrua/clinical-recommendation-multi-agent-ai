"""사실 저장소 -> Matcher 입력 계층과 사실 기반 근거 게이트 (Phase 1-3B/1-4).

비교용 경로에서만 씁니다. 실제 판정·질문·추천은 기존 PatientProfile 경로를 그대로 씁니다.

1) FactEvidenceView: 환자의 사실을 항목별로 묶은 근거 보기. 사실의 출처·인용·검증 상태·관찰 시점·충돌 관계를 그대로 담음.
   항목 상태
   - agreed         : 단일 값 항목의 사실이 모두 같은 값 (출처·시점이 다른 같은 값의 근거는 모두 보존)
   - contradictory / time_dependent / incomparable : 단일 값 항목의 값이 맞지 않음 -> 값을 고르지 않음
   - multi          : 여러 값 항목(진단·바이오마커 등). 같은 값끼리만 묶고, 값이 다른 쌍(not_compared)의 수를 기록
   to_profile()은 기존 PatientProfile 형태로 바꿔 Matcher·규칙 엔진에 그대로 넣을 수 있게 하되,
   맞지 않는 단일 값 항목은 비워 둠(최신 값·검증된 값을 자동으로 고르지 않음). 누락 값은 만들지 않음.

2) gate(): Matcher가 낸 기준별 판정을 사실 근거로 다시 확인. 문자열 위치가 아니라 사실의 출처·검증 상태로 판단.
   확정 판정(MET/NOT_MET)을 판정 불가로 되돌리는 방향으로만 바꾸며, 판정 불가를 확정으로 올리지 않음.
   - ungrounded          : LLM 판정의 인용이 기준과 관련된 사실과 이어지지 않음 -> 판정 불가 (미검증 정보 승격 차단)
   - ambiguous_evidence  : 인용이 서로 다른 여러 사실과 이어짐 -> 하나를 고르지 않고 판정 불가
     (연결: 사실 ID > 구조화 필드 참조 > 인용·값 정확히 일치 > MIN_LINK_CHARS 이상 포함. 짧은 조각 일치는 쓰지 않음)
   - conflict            : 근거 항목에 맞지 않는 사실이 있음 -> 판정 불가
   - biomarker_unresolved: 기준의 표적(BIOMARKER_TARGETS로 식별)과 관련된 바이오마커 사실 중 값이 다른 것이 있어
                           상반된 결과일 수 있음 -> 의사 확인. 다른 표적의 사실은 제외, 표적을 모르는 사실·기준은 보수적으로 포함
   - observation_window  : 기준의 기간 조건(time_window_days) 안에 관찰된 사실이 확인되지 않음 -> 판정 불가
   - self_report_only    : 진료 기록이 필요한 기준인데 근거가 자기 보고뿐 -> UNVERIFIED (답변 값 보존)
   - unknown_source      : 진료 기록이 필요한 기준인데 근거 출처를 모름 -> UNVERIFIED
   - declared_unverified : (require_document_verification=True일 때만) clinical_record 선언뿐이고 문서 검증 없음 -> UNVERIFIED
   진료 기록이 필요한 기준이 아니면 자기 보고·출처 불명 근거도 그대로 인정합니다(미검증 정보를 무조건 불일치로 보지 않음).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel

from .agents import matcher
from .fact_store import MULTI_VALUED_FIELDS, FactStore, PatientFact
from .schemas import CriterionAssessment, Fact, LabValue, PatientProfile, Rule, RuleMeta
from .tools.rule_engine import canonical_lab

EvidenceTier = Literal["verified", "declared_record", "self_report", "unknown"]
FieldState = Literal["agreed", "contradictory", "time_dependent", "incomparable", "multi"]
_TIER_RANK = {"verified": 3, "declared_record": 2, "self_report": 1, "unknown": 0}
_SCALARS = ("age", "sex", "ecog", "primary_diagnosis", "disease_stage")
_LIST_ATTRS = {"diagnosis": "diagnoses", "biomarker": "biomarkers", "medication": "medications",
               "treatment_history": "treatment_history", "other_fact": "other_facts"}


def tier(f: PatientFact) -> EvidenceTier:
    """사실 하나의 근거 수준. 문서 검증 이력이 있어야 verified (clinical_record 선언만으로는 declared_record)."""
    if f.verification_status == "document_verified":
        return "verified"
    if f.source_type == "clinical_record":
        return "declared_record"
    if f.source_type == "self_report":
        return "self_report"
    return "unknown"


class FactRef(BaseModel):
    fact_id: str
    field_name: str
    value: bool | int | float | str
    unit: str | None
    source_type: str
    verification_status: str
    tier: EvidenceTier
    observed_at: str | None
    observed_at_text: str | None
    quote: str | None


class FieldEvidence(BaseModel):
    field: str
    state: FieldState
    facts: list[FactRef]
    unresolved_pairs: int = 0  # 여러 값 항목에서 값이 다른(not_compared) 쌍의 수


def _ref(f: PatientFact) -> FactRef:
    return FactRef(fact_id=f.fact_id, field_name=f.field, value=f.value, unit=f.unit, source_type=f.source_type,
                   verification_status=f.verification_status, tier=tier(f), observed_at=f.observed_at,
                   observed_at_text=f.observed_at_text, quote=f.source_reference.quote)


def _field_state(facts: list[PatientFact]) -> tuple[FieldState, int]:
    if facts[0].field in MULTI_VALUED_FIELDS:
        pairs = sum(r == "not_compared" for f in facts for r in f.related_fact_ids.values()) // 2
        return "multi", pairs
    worst = max((f.conflict_status for f in facts), key=lambda s: ["none", "consistent", "not_compared", "incomparable",
                                                                   "time_dependent", "contradictory"].index(s))
    return ("agreed" if worst in ("none", "consistent") else worst), 0  # type: ignore[return-value]


class FactEvidenceView:
    """한 환자의 사실 근거 보기 (사실 저장소의 사본 정보, 저장소를 바꾸지 않음)."""

    def __init__(self, store: FactStore, patient_id: str):
        self.patient_id = patient_id
        self.fields: dict[str, FieldEvidence] = {}
        groups: dict[str, list[PatientFact]] = {}
        for f in store.facts(patient_id):
            groups.setdefault(f.field, []).append(f)
        for name, facts in groups.items():
            state, pairs = _field_state(facts)
            self.fields[name] = FieldEvidence(field=name, state=state, facts=[_ref(f) for f in facts],
                                              unresolved_pairs=pairs)

    def excluded(self) -> dict[str, str]:
        """값이 맞지 않아 보기에서 뺀 단일 값 항목 -> 상태."""
        return {n: e.state for n, e in self.fields.items() if e.state not in ("agreed", "multi")}

    def _agreed(self, name: str) -> FactRef | None:
        e = self.fields.get(name)
        if e is None or e.state != "agreed":
            return None
        # 값은 모두 같으므로 어떤 사실을 대표로 써도 값은 같음. 인용·날짜는 근거 수준이 높은 사실의 것을 보여 줌
        return max(e.facts, key=lambda r: (_TIER_RANK[r.tier], r.quote is not None))

    def to_profile(self) -> PatientProfile:
        """기존 Matcher 입력과 같은 PatientProfile. 맞지 않는 단일 값 항목은 비워 둠."""
        age, sex, ecog = self._agreed("age"), self._agreed("sex"), self._agreed("ecog")
        age_unit = (age.unit or "years") if age else None
        age_unit = age_unit if age_unit in ("years", "months", "weeks", "days") else None
        lists: dict[str, list[Fact]] = {attr: [] for attr in _LIST_ATTRS.values()}
        labs: list[LabValue] = []
        for name, e in self.fields.items():
            if name in _LIST_ATTRS:
                seen: list = []
                for r in e.facts:  # 같은 값은 한 번만 (값이 다른 항목은 모두 보존)
                    key = str(r.value).strip().casefold()
                    if key not in seen:
                        seen.append(key)
                        lists[_LIST_ATTRS[name]].append(Fact(description=str(r.value), date=r.observed_at,
                                                             source_quote=r.quote or ""))
            elif name.startswith("lab:"):
                r = self._agreed(name)
                if r is not None and isinstance(r.value, (int, float)) and not isinstance(r.value, bool):
                    labs.append(LabValue(name=name[4:], value=float(r.value), unit=r.unit, date=r.observed_at,
                                         source_quote=r.quote or ""))
        pd, ds = self._agreed("primary_diagnosis"), self._agreed("disease_stage")
        return PatientProfile(
            patient_id=self.patient_id,
            age=int(age.value) if age and age_unit == "years" else None,
            age_value=float(age.value) if age and age_unit else None, age_unit=age_unit,
            sex=sex.value if sex else "unknown",  # type: ignore[arg-type]
            primary_diagnosis=str(pd.value) if pd else None, disease_stage=str(ds.value) if ds else None,
            ecog=int(ecog.value) if ecog else None, labs=labs, **lists,
            missing_or_ambiguous=[f"{n}: 사실 값이 맞지 않음({s})" for n, s in sorted(self.excluded().items())],
        )


# ---------------------------------------------------------------------------
# 사실 기반 근거 게이트
# ---------------------------------------------------------------------------

def _engine_field(rule: Rule) -> str | None:
    f = (rule.field or "").strip().lower()
    if f in ("age", "sex", "ecog"):
        return f
    if f.startswith("lab:"):
        return "lab:" + canonical_lab(f[4:])
    return None


# ---------------------------------------------------------------------------
# 근거 연결 (LLM 판정 인용 -> 사실)
# ---------------------------------------------------------------------------

MIN_LINK_CHARS = 12  # 포함 관계로 이을 때 짧은 쪽에 필요한 최소 글자 수(공백 제외). 짧은 조각의 우연한 일치를 막음
_FACT_ID = re.compile(r"\bF-[0-9a-f]{12}\b")


def _norm(text) -> str:
    return " ".join(str(text).split()).casefold()


def _contains(longer: str, shorter: str) -> bool:
    return len(shorter.replace(" ", "")) >= MIN_LINK_CHARS and shorter in longer


class Link(BaseModel):
    status: Literal["linked", "ambiguous", "none", "invalid_ref"]
    fact_ids: list[str] = []
    fields: list[str] = []


def link_evidence(evidence: str, view: "FactEvidenceView") -> Link:
    """LLM 판정의 인용을 사실에 잇기. 추측하지 않고, 이어졌다고 검증 상태가 바뀌지 않음.

    우선순위: 1) 사실 ID(F-...)  2) 구조화 필드 참조('ecog: 1', 값이 사실과 같아야 함)
    3) 사실의 인용·값과 정확히 같음(공백·대소문자 무시)  4) 한쪽이 다른 쪽을 포함하고 짧은 쪽이 MIN_LINK_CHARS 이상.
    후보 사실이 둘 이상이면(항목·값·근거 수준이 모두 같은 경우 제외) 고르지 않고 ambiguous.
    """
    by_id = {r.fact_id: (name, r) for name, e in view.fields.items() for r in e.facts}
    ids = _FACT_ID.findall(evidence or "")
    if ids:
        if any(i not in by_id for i in ids):
            return Link(status="invalid_ref")
        return Link(status="linked", fact_ids=list(dict.fromkeys(ids)), fields=sorted({by_id[i][0] for i in ids}))
    refs = matcher._FIELD_REF.findall(evidence or "")
    if refs:
        fids, fields = [], set()
        for k, v in refs:
            e = view.fields.get(k.lower())
            matching = [r for r in (e.facts if e else []) if _norm(r.value) == _norm(v)]
            if not matching:
                return Link(status="none")
            fids += [r.fact_id for r in matching]
            fields.add(k.lower())
        return Link(status="linked", fact_ids=fids, fields=sorted(fields))
    ev = _norm(evidence or "")
    if not ev:
        return Link(status="none")
    exact, contained = [], []
    for name, e in view.fields.items():
        for r in e.facts:
            texts = [_norm(t) for t in (r.quote, r.value) if t is not None and str(t).strip()]
            if any(ev == t for t in texts):
                exact.append((name, r))
            elif any(_contains(ev, t) or _contains(t, ev) for t in texts):
                contained.append((name, r))
    candidates = exact or contained
    if not candidates:
        return Link(status="none")
    if len({(name, _norm(r.value), r.tier) for name, r in candidates}) > 1:
        return Link(status="ambiguous", fields=sorted({name for name, _ in candidates}))
    return Link(status="linked", fact_ids=[r.fact_id for _, r in candidates],
                fields=sorted({name for name, _ in candidates}))


# ---------------------------------------------------------------------------
# 바이오마커 표적 (명시적·소규모 목록, 공식 유전자·표지자 기호만. 의미 추론 없음)
# ---------------------------------------------------------------------------

# 표적 -> 단어 경계로 찾는 표기. 일반 영어 단어와 겹치는 기호(MET 등)는 넣지 않음 -> 표적을 모르는 것으로 처리
BIOMARKER_TARGETS = {
    "EGFR": [r"egfr"], "ALK": [r"alk"], "ROS1": [r"ros1"], "BRAF": [r"braf"], "KRAS": [r"kras"],
    "HER2": [r"her2", r"erbb2"], "PD-L1": [r"pd-?\s?l1"], "RET": [r"ret"], "NTRK": [r"ntrk[1-3]?"],
    "BRCA1": [r"brca1"], "BRCA2": [r"brca2"],
}
_TARGET_RE = {t: re.compile(r"(?<![a-z0-9])(?:" + "|".join(p) + r")(?![a-z0-9])", re.I)
              for t, p in BIOMARKER_TARGETS.items()}


def targets_in(text) -> set[str]:
    return {t for t, rx in _TARGET_RE.items() if text and rx.search(str(text))}


def _fact_targets(r: FactRef) -> set[str]:
    return targets_in(r.value) | targets_in(r.quote)


def relevant_biomarker_facts(rule: Rule, view: "FactEvidenceView") -> tuple[set[str], list[FactRef]]:
    """기준의 표적과 관련된 바이오마커 사실. 표적을 모르는 사실은 관련 없다고 단정하지 않고 포함.

    반환: (기준의 표적, 관련 사실). 기준의 표적을 알 수 없으면 모든 바이오마커 사실.
    """
    e = view.fields.get("biomarker")
    facts = list(e.facts) if e else []
    wanted = targets_in(rule.text)
    if not wanted:
        return wanted, facts
    return wanted, [r for r in facts if not _fact_targets(r) or _fact_targets(r) & wanted]


def _differing(facts: list[FactRef]) -> bool:
    """값이 다른 사실이 있는가 (같은 값이면 상반된 결과가 아님)."""
    return len({_norm(r.value) for r in facts}) > 1


def supporting_facts(a: CriterionAssessment, rule: Rule, view: FactEvidenceView
                     ) -> tuple[list[FactRef], list[str], str]:
    """판정을 뒷받침하는 사실, 근거 항목 중 값이 맞지 않는 항목, 연결 상태(linked/ambiguous/none/invalid_ref)."""
    if a.method == "rule_engine":  # 규칙 엔진은 구조화 필드 하나로 판정 -> 그 항목의 사실이 근거
        name = _engine_field(rule)
        e = view.fields.get(name or "")
        if e is None:
            return [], [], "none"
        if e.state not in ("agreed", "multi"):
            return [], [e.field], "linked"
        return list(e.facts), [], "linked"
    link = link_evidence(a.evidence or "", view)
    if link.status != "linked":
        return [], [], link.status
    conflicted = sorted(n for n in link.fields if view.fields[n].state not in ("agreed", "multi"))
    ids = set(link.fact_ids)
    facts = [r for n in link.fields for r in view.fields[n].facts if r.fact_id in ids]
    return facts, conflicted, "linked"


def _start_of(d: str) -> date | None:
    """관찰 시점의 가장 이른 날 (YYYY -> 1월 1일, YYYY-MM -> 1일). 기간 조건은 가장 이른 날로 보수적으로 확인."""
    try:
        parts = [int(x) for x in d.split("-")]
        return date(parts[0], parts[1] if len(parts) > 1 else 1, parts[2] if len(parts) > 2 else 1)
    except (ValueError, IndexError):
        return None


def _to_unknown(a: CriterionAssessment, note: str, **update) -> CriterionAssessment:
    return a.model_copy(update={"holds": "unknown", "passes": None, "applicable": True,
                                "reasoning": f"[사실 근거 확인: {note}] {a.reasoning}", **update})


def gate(a: CriterionAssessment, rule: Rule, meta: RuleMeta, view: FactEvidenceView, reference_date: str,
         require_document_verification: bool = False) -> tuple[CriterionAssessment, str | None]:
    """확정 판정을 사실 근거로 확인. 반환: (판정, 바꾼 이유 코드 또는 None). 판정 불가는 그대로 둠."""
    a = a.model_copy(update={"required_evidence": meta.required_evidence})
    if a.passes is None:
        return a, None
    biomarker_facts: list[FactRef] | None = None
    if rule.category == "biomarker":
        wanted, biomarker_facts = relevant_biomarker_facts(rule, view)
        if _differing(biomarker_facts):
            what = ", ".join(sorted(wanted)) if wanted else "표적 미상"
            return _to_unknown(a, f"관련 바이오마커 사실({what}) 중 값이 다른 것이 있어 상반된 결과일 수 있음",
                               underspecified=True, missing_info="바이오마커 결과 확인 필요(의사 확인)"), \
                "biomarker_unresolved"
    facts, conflicted, link = supporting_facts(a, rule, view)
    if conflicted:
        return _to_unknown(a, f"근거 항목의 사실 값이 맞지 않음({', '.join(conflicted)})",
                           missing_info="상충하는 정보 확인 필요"), "conflict"
    if link == "ambiguous":
        return _to_unknown(a, "판정 근거가 서로 다른 여러 사실과 이어져 하나로 정할 수 없음",
                           missing_info=a.missing_info or rule.text), "ambiguous_evidence"
    if biomarker_facts is not None:  # 다른 표적의 바이오마커 사실은 이 기준의 근거가 아님
        relevant = {r.fact_id for r in biomarker_facts}
        facts = [r for r in facts if r.field_name != "biomarker" or r.fact_id in relevant]
    if not facts:
        return _to_unknown(a, "판정 근거가 기준과 관련된 사실과 이어지지 않음",
                           missing_info=a.missing_info or rule.text), "ungrounded"
    if rule.time_window_days:
        ref = date.fromisoformat(reference_date)
        starts = [_start_of(r.observed_at) for r in facts if r.observed_at]
        if len(starts) < len(facts) or any(s is None or (ref - s).days > rule.time_window_days for s in starts):
            return _to_unknown(a, f"기준 기간({rule.time_window_days}일) 안의 관찰 시점이 확인되지 않음",
                               missing_info=f"최근 {rule.time_window_days}일 이내 결과 필요"), "observation_window"
    best = max((r.tier for r in facts), key=_TIER_RANK.__getitem__)
    a = a.model_copy(update={"evidence_source": {"verified": "record", "declared_record": "record",
                                                 "self_report": "self_report", "unknown": "unknown"}[best]})
    if meta.required_evidence != "clinical_record":
        return a, None
    if best == "verified" or (best == "declared_record" and not require_document_verification):
        return a, None
    reason = {"declared_record": "declared_unverified", "self_report": "self_report_only",
              "unknown": "unknown_source"}[best]
    said = "해당 없음" if not a.applicable else a.holds
    update = {"needs_verification": True,
              "missing_info": "진료 기록(검사 결과지, 병리·유전자 보고서 등)으로 확인 필요"}
    if best == "self_report":
        update.update(self_reported_holds=a.holds, self_reported_applicable=a.applicable)
    return _to_unknown(a, f"UNVERIFIED: 진료 기록이 필요한 기준인데 근거 수준이 {best} - 판정 '{said}'", **update), reason
