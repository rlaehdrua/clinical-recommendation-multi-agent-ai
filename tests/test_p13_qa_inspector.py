"""P13 통합: P12 확인 답변 재평가 결과를 Inspector(근거 직접성 표시, af6b6c3)가 기록 그대로 보여 주는지.

- 재평가로 확정된 기준: Inspector의 기준 상세가 result.json과 같은 판정·근거 직접성(direct)·사유(answer_verified)·
  코드 검증 전 LLM 판정을 보여 줌. Inspector는 다시 판정하지 않음
- 검증하지 못한 기준: answer_unverified:<사유>가 그대로 보임 (사유 코드가 '<text>'로 가려지지 않음)
- strict: 근거(문답 원문)·답변 문장 비노출 / 판정 상태는 같은 값
오프라인 결정적 대역 (실제 LLM 호출 없음).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from ctrec import inspector, pipeline, qa_guard
from ctrec.agents import matcher
from ctrec.schemas import LLMAssessment, MatcherOutput
from test_contract_p5_session import _Answerer, _profile_from, _session, _trials, patched  # noqa: F401
from test_offline import _rule

META = {"format_version": 1, "llm_backend": "offline_stub", "synthetic": True, "written_by": "test"}
PREG_Q = "현재 임신 중이거나 수유 중인가요?"
ADC_Q = "EGFR을 표적으로 하는 항체-약물 결합체(ADC) 치료를 받은 적이 있나요?"
TKI_Q = "EGFR 표적 치료제(EGFR TKI, 예: 오시머티닙)를 복용한 적이 있나요?"


def _run(monkeypatch, rules, questions, answers, judgments):
    """judgments: {rule_id: (holds, 의역 근거)} - 답변 뒤에만 모델이 판정 (실제 실행에서 본 양상)."""
    monkeypatch.delenv(qa_guard.POLICY_ENV, raising=False)
    monkeypatch.setattr(pipeline.patient_profiler, "profile_patient", _profile_from(None))
    trials = _trials(monkeypatch, {"T1": rules})
    state = {"done": False}

    def fake(model, system, user, **k):
        out = []
        for r in rules:
            h, ev = judgments.get(r.rule_id, ("unknown", "근거 없음")) if state["done"] else ("unknown", "근거 없음")
            out.append(LLMAssessment(rule_id=r.rule_id, applicable=True, holds=h,
                                     evidence_type="explicit" if h != "unknown" else "absent", confidence="high",
                                     evidence=ev, reasoning="mock", missing_info=None))
        return MatcherOutput(assessments=out)

    class Ans(_Answerer):
        def answer(self, qs, patient_id):
            state["done"] = True
            return super().answer(qs, patient_id)

    monkeypatch.setattr(matcher.llm, "structured", fake)
    return trials, questions, Ans(answers)


def _session_view(monkeypatch, patched, rules, questions, answers, judgments, synthetic=False):
    trials, qs, ans = _run(monkeypatch, rules, questions, answers, judgments)
    patched["llm_questions"] = qs
    s = _session(pipeline.Session, trials, ans)
    s.run_fixed()
    rec = inspector.record_from_session(s, trace=s.tracer.records, meta=META)
    v = inspector.build_view(rec, synthetic_details=synthetic)
    return s, v, inspector.render_html(v)


def _q(qid, q, rid):
    return dict(question_id=qid, question=q, answer_type="yes_no", target=[f"T1:{rid}"], purpose="eligibility", why=rid)


PREG = _rule(rule_id="E4", kind="exclusion", text="Pregnant or breastfeeding women", category="reproductive",
             structured_evaluable=False)
ADC = _rule(rule_id="E2", kind="exclusion", text="Prior treatment with any EGFR-targeted antibody-drug conjugate",
            category="treatment_history", structured_evaluable=False)


def test_inspector_shows_reevaluated_verdict_as_recorded(monkeypatch, patched):
    s, v, page = _session_view(monkeypatch, patched, [PREG, ADC], [_q("Q1", PREG_Q, "E4"), _q("Q2", ADC_Q, "E2")],
                               {PREG_Q: "아니요", ADC_Q: "아니요"},
                               {"E4": ("no", "임신·수유 아님"), "E2": ("no", "EGFR ADC 치료 받은 적 없음")})
    rec = {a.rule_id: a for a in s.matches["T1"].assessments}
    crit = {c["rule_id"]: c for c in v["trials"][0]["criteria"]}
    # P12: E4는 검증된 답으로 확정, E2(개념군 밖)는 미확정 + 사유
    assert rec["E4"].passes is True and rec["E4"].validation_flags == ["answer_verified"]
    assert rec["E2"].passes is None and "answer_unverified:criterion_not_verifiable" in rec["E2"].validation_flags
    # Inspector: 기록 값 그대로 (판정·직접성·사유·LLM 원래 판정), 다시 판정하지 않음
    for rid in ("E4", "E2"):
        assert crit[rid]["passes"] == rec[rid].passes and crit[rid]["holds"] == rec[rid].holds
        assert crit[rid]["evidence_directness"] == rec[rid].evidence_directness
        assert crit[rid]["validation_flags"] == rec[rid].validation_flags  # 사유 코드가 가려지지 않음
        assert crit[rid]["llm_judgment"]["holds"] == "no"
    assert "<code>answer_verified</code>" in page and "<code>answer_unverified:criterion_not_verifiable</code>" in page
    assert page.count("최종 해당 여부") == 1  # E2만: LLM 원래 판정(no)과 최종(unknown)이 다름을 표시
    # strict: 문답 원문 근거·답변 문장·질문 문장 없음
    for text in (PREG_Q, ADC_Q, f"Q: {PREG_Q} A: 아니요"):
        assert text not in page


def test_egfr_tki_answer_is_not_used_for_egfr_adc_criterion(monkeypatch, patched):
    """EGFR TKI 질문에 '아니요' -> EGFR ADC 기준을 정하지 않음 (낱말 EGFR이 같아도 다른 치료)."""
    s, v, _ = _session_view(monkeypatch, patched, [ADC], [_q("Q1", TKI_Q, "E2")], {TKI_Q: "아니요"},
                            {"E2": ("no", "EGFR 치료 받은 적 없음")})
    a = s.matches["T1"].assessments[0]
    assert a.passes is None and "answer_unverified:criterion_not_verifiable" in a.validation_flags


def test_synthetic_view_keeps_same_verdicts(monkeypatch, patched):
    args = ([PREG], [_q("Q1", PREG_Q, "E4")], {PREG_Q: "아니요"}, {"E4": ("no", "임신·수유 아님")})
    _, strict_v, _ = _session_view(monkeypatch, patched, *args)
    _, syn_v, _ = _session_view(monkeypatch, patched, *args, synthetic=True)
    key = ("passes", "holds", "evidence_directness", "validation_flags")
    assert [{k: c[k] for k in key} for c in strict_v["trials"][0]["criteria"]] == \
        [{k: c[k] for k in key} for c in syn_v["trials"][0]["criteria"]]
    assert strict_v["trials"][0]["eligibility"] == syn_v["trials"][0]["eligibility"]


@pytest.mark.parametrize("flag", ["answer_unverified:directness:conflicting", "answer_unverified:time_scope_mismatch"])
def test_p12_reason_codes_are_valid_inspector_codes(flag):
    assert inspector._ident(flag) == flag
