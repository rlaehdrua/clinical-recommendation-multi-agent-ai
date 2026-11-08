"""P1-3 (2026-11-08 최종 감사): 임상의 판단 표현이 있는데 underspecified·후보표 분류가 없던 저장 기준을 AI가 확정하던 문제.

재현 (수정 전): data/parsed/NCT06468397.json E1 "in the opinion of the investigator, would limit study participation"
(category=comorbidity, self_report_ok, underspecified=false) -> LLM이 'no'를 내면 ELIGIBLE / CRITERIA_MATCH.
수정: expert_boundary의 분류 신호 3(AUDIT_EXPERT_RULES)·4(판단 주체 문구)를 더함. 저장 파일·해시는 바꾸지 않음.
API 호출 없음, 값은 모두 합성.
"""

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_expert_boundary import AGE, _assert_expert_review, _run, _verdict  # noqa: E402

from ctrec import expert_boundary as xb, store_patch  # noqa: E402
from ctrec.engine import contract_bridge  # noqa: E402
from ctrec.schemas import ParsedTrial, RuleMeta  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STORES = [ROOT / "data/parsed", ROOT / "data/nsclc/parsed", ROOT / "data/nsclc/eval/inputs/trial_store"]


def _load(path: Path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    parsed = ParsedTrial(**raw["parsed"])
    meta = {k: RuleMeta(**v) for k, v in (raw.get("rule_meta") or {}).items()}
    return parsed, meta


def _stored(trial_id: str, rule_id: str):
    for store in STORES:
        p = store / f"{trial_id}.json"
        if p.exists():
            parsed, meta = _load(p)
            rule = next(r for r in parsed.rules if r.rule_id == rule_id)
            return rule, {k: v for k, v in meta.items() if k == rule_id}
    raise FileNotFoundError(trial_id)


def _all_stored_rules():
    seen = {}
    for store in STORES:
        for f in sorted(store.glob("*.json")):
            if f.name == "FROZEN.json":
                continue
            parsed, _ = _load(f)
            for r in parsed.rules:
                seen.setdefault(f"{parsed.trial_id}:{r.rule_id}", (parsed.trial_id, r))
    return seen


# ---------------------------------------------------------------------------
# 목록 고정: 원문 앵커와 출처
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", sorted(xb.AUDIT_EXPERT_RULES))
def test_audit_rules_point_at_their_text_and_were_unclassified(key):
    """재파싱으로 ID가 밀리면 실패. 플래그·후보표에 이미 있으면 목록에 둘 필요가 없음."""
    tid, rid = key.split(":")
    rule, _ = _stored(tid, rid)
    assert xb.AUDIT_EXPERT_RULES[key] in rule.text.lower(), key
    assert not rule.underspecified and key not in xb.EXPERT_RULES
    assert xb.clinician_reason(tid, rule) in (xb.REASON_AUDIT, xb.REASON_WORDING)
    src = xb.AUDIT_EXPERT_RULE_SOURCES[key]
    assert src["review_status"] == "unreviewed" and src["source"] == xb.AUDIT_SOURCE


def test_audit_list_needs_its_own_trial():
    rule, _ = _stored("NCT06468397", "E1")
    assert xb.clinician_reason("NCT06468397", rule) is not None
    other = rule.model_copy(update={"text": "History of unstable diabetes."})  # 같은 ID, 다른 시험·원문
    assert xb.clinician_reason("NCT99999999", other) is None


def test_frozen_store_is_unchanged():
    """목록·문구는 런타임 분류만 바꿈: 잠긴 기준표 26개 해시는 그대로."""
    assert store_patch.verify_frozen() == []


# ---------------------------------------------------------------------------
# 판단 주체 문구 (실패-안전 장치)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "History of conditions that, in the opinion of the investigator, would limit study participation",
    "Suspicion of IM as per judgement of the treating clinician",
    "Any condition that in the Investigator's judgment makes the patient unsuitable",
    "Other significant medical conditions deemed by the investigator to interfere with participation",
    "As judged by the investigator, any severe or uncontrolled systemic disease",
    "Lack of improvement, at the discretion of Investigator",
    "Any medical condition that, in the judgement of the investigator, is a contraindication",
    "The investigator considers the patient unable to comply",
])
def test_judgment_wording_is_caught(text):
    assert xb.JUDGMENT_WORDING.search(text)


@pytest.mark.parametrize("text", [
    "Clinically significant cardiac arrhythmias",              # 판단 주체 없음: 목록·검수로만
    "Measurable disease per RECIST v1.1 as assessed by the investigator",  # 측정 기준 지정 (보류 항목)
    "Age 18 years or older",
    "Platelets >= 100 x 10^9/L",
    "Informed consent signed by the participant and the investigator",
    "Prior treatment prescribed by the subject's primary physician",
])
def test_concrete_wording_is_not_caught(text):
    assert not xb.JUDGMENT_WORDING.search(text)


def test_wording_only_hits_on_stored_rules_are_pinned():
    """저장 규칙에서 문구로만 새로 걸리는 기준을 고정: 저장소가 바뀌면 사람이 다시 보게 함."""
    rules = _all_stored_rules()
    hits = {k for k, (tid, r) in rules.items() if xb.JUDGMENT_WORDING.search(r.text)}
    wording_only = {k for k in hits if not rules[k][1].underspecified and k not in xb.EXPERT_RULES}
    assert wording_only == {"NCT06468397:E1", "NCT05127980:E1"}
    assert wording_only <= set(xb.AUDIT_EXPERT_RULES)
    assert len(hits) == 12


def test_unregistered_new_trial_with_explicit_wording_is_held():
    """목록 밖 새 시험(LLM 경로 즉석 파싱)도 판단 주체 문구가 있으면 막음."""
    rule, _ = _stored("NCT06468397", "E1")
    fresh = rule.model_copy(update={"rule_id": "E9"})
    assert xb.clinician_reason("NCT00000001", fresh) == xb.REASON_WORDING


# ---------------------------------------------------------------------------
# 두 경로: LLM 경로(기본 Session = CLI·웹 /api/runs, matcher.match_trial)와 오프라인 엔진 분류(contract_bridge)
# ---------------------------------------------------------------------------

CASES = [
    ("NCT06468397", "E1", "참여를 제한할 질환 없음", "조절되지 않는 당뇨"),
    ("NCT05127980", "E1", "IM 의심 소견 없음", "IM 의심 소견 있음"),
    ("NCT07027514", "E14", "심장 질환 없음", "임상적으로 의미 있는 부정맥 있음"),
    ("NCT07416058", "E17", "심혈관 질환 없음", "임상적으로 의미 있는 심혈관 질환 있음"),
]


@pytest.mark.parametrize("tid, rid, good, bad", CASES)
@pytest.mark.parametrize("case", ["A", "B", "D"])
def test_default_session_does_not_confirm_unflagged_clinician_rule(monkeypatch, tid, rid, good, bad, case):
    """A: LLM 통과 판정 / B: LLM 불충족 판정 / D: 진료 기록 문서 사실이 있어도 -> 모두 EXPERT_REVIEW (UNCERTAIN, NEEDS_REVIEW)."""
    rule, meta = _stored(tid, rid)
    rule = rule.model_copy(update={"cohort": None})  # 코호트 분기 없이 이 기준 하나의 경계만 봄
    v = _verdict(rule, good, bad, ok=case != "B")
    text = {"A": f"62세 남성. {good}.", "B": f"62세 남성. {bad}.", "D": f"[퇴원요약 2026-09-01] 62세 남성. {good}."}[case]
    x = _run(monkeypatch, tid, [AGE, rule], meta, {rid: v}, text, documents=case == "D")
    _assert_expert_review(x, rid)
    assert x["match"].eligibility != "ELIGIBLE" and x["external"] != "CRITERIA_MATCH"


def test_audit_repro_eligible_is_now_needs_review(monkeypatch):
    """감사 재현 그대로: E1을 LLM이 'no'로 판정 -> 수정 전 ELIGIBLE / CRITERIA_MATCH, 지금은 NEEDS_REVIEW."""
    rule, meta = _stored("NCT06468397", "E1")
    x = _run(monkeypatch, "NCT06468397", [AGE, rule], meta, {"E1": ("no", "No significant comorbidities")},
             "64-year-old male. No significant comorbidities.", age=64)
    assert (x["match"].eligibility, x["external"]) == ("UNCERTAIN", "NEEDS_REVIEW")
    e1 = next(a for a in x["match"].assessments if a.rule_id == "E1")
    assert e1.passes is None and xb.FLAG in e1.validation_flags


@pytest.mark.parametrize("tid, rid, good, bad", CASES)
def test_offline_engine_classifies_unflagged_clinician_rule_as_expert(tid, rid, good, bad):
    rule, _ = _stored(tid, rid)
    assert contract_bridge.provisional_adjudication(rule, tid) == "expert"


def test_ordinary_rule_still_decides(monkeypatch):
    """회귀: 분류 밖 기준은 그대로 확정 (나이 불충족은 부적격 확정 경로 유지)."""
    x = _run(monkeypatch, "NCT06468397", [AGE], {}, {}, "16세 남성.", age=16)
    assert x["match"].eligibility == "INELIGIBLE"
    assert re.match(r"CRITERIA_MISMATCH|NEEDS_REVIEW|INSUFFICIENT_INFORMATION", x["external"])
