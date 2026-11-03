"""확인 질문 답변 사실의 질문별 출처 (WP3 §2.3, qa_pairs). LLM 호출 없음."""

from __future__ import annotations

import pytest

from ctrec.evidence_directness import eligibility_answers
from ctrec.fact_store import FactStore
from ctrec.profile_facts import ingest_profile
from ctrec.schemas import Fact, PatientProfile, QAPair

from test_profile_facts import ANSWER, RAW, _profile
from test_session_fact_shadow import ANSWER_QUOTE, _run, calls  # noqa: F401  (calls는 pytest 픽스처)

ANSWERED = "9월에 18만이라고 들었어요"  # test_profile_facts.ANSWER의 답


def _qa(qid="Q1", answer=ANSWERED, purpose="eligibility", rnd=1, session_id="S1", target=("T1:I2",)):
    return QAPair(round=rnd, question_id=qid, question="최근 혈소판 수치는?", answer=answer, purpose=purpose,
                  target=list(target), session_id=session_id)


def _answer_fact(quote, description="platelets reported by patient"):
    return _profile(other_facts=[Fact(description=description, date=None, source_quote=quote)])


def _snapshot(store, pid="P001"):
    return [f.model_dump(exclude={"fact_id", "recorded_at"}) for f in store.facts(pid)]


def _other(store, pid="P001"):
    return store.facts(pid, "other_fact")


# a) qa_pairs=None이면 이전과 같음
def test_without_qa_pairs_results_are_unchanged():
    raw = RAW + ANSWER
    old, new = FactStore(), FactStore()
    r_old = ingest_profile(old, _answer_fact(ANSWERED), raw_text=raw, input_source="clinical_record", session_id="S1")
    r_new = ingest_profile(new, _answer_fact(ANSWERED), raw_text=raw, input_source="clinical_record", session_id="S1",
                           qa_pairs=None)
    assert _snapshot(old) == _snapshot(new)
    assert (len(r_old.added), r_old.skipped, r_old.rejected) == (len(r_new.added), r_new.skipped, r_new.rejected)
    assert r_new.withheld == []
    assert _other(new)[0].source_reference.ref == "raw_text:answers"


# b) 연결이 유일하면 질문별 출처
def test_unique_answer_link_gets_per_question_ref():
    s = FactStore()
    r = ingest_profile(s, _answer_fact(ANSWERED), raw_text=RAW + ANSWER, input_source="clinical_record",
                       session_id="S1", qa_pairs=[_qa()])
    [f] = _other(s)
    assert (f.source_type, f.source_reference.kind, f.source_reference.ref, f.source_reference.quote) == \
        ("self_report", "answer", "q:r1:Q1", ANSWERED)
    assert r.withheld == []
    # 처음 입력에서 확인된 사실은 질문과 연결하지 않음
    [hb] = s.facts("P001", "lab:hemoglobin")
    assert hb.source_reference.ref == "raw_text:initial"


def test_other_session_answers_are_not_linked():
    s = FactStore()
    ingest_profile(s, _answer_fact(ANSWERED), raw_text=RAW + ANSWER, session_id="S1",
                   qa_pairs=[_qa(session_id="S2")])
    assert _other(s)[0].source_reference.ref == "raw_text:answers"


# c) 연결이 모호하면 raw_text:answers 유지
@pytest.mark.parametrize("pairs", [
    [_qa("Q1"), _qa("Q2", rnd=2)],  # 같은 답이 두 문답에 있음
    [_qa("Q1", answer=f"네, {ANSWERED}"), _qa("Q2", answer=f"{ANSWERED}. 지난달 기준")],  # 답은 달라도 인용이 둘 다에
    [],  # 문답 기록이 없음
    [_qa(answer="전혀 다른 답")],  # 인용이 어느 답에도 없음
])
def test_ambiguous_or_missing_link_keeps_raw_text_answers(pairs):
    s = FactStore()
    ingest_profile(s, _answer_fact(ANSWERED), raw_text=RAW + ANSWER, session_id="S1", qa_pairs=pairs)
    [f] = _other(s)
    assert f.source_reference.ref == "raw_text:answers" and f.source_type == "self_report"


def test_undeclared_purpose_is_not_linked_per_question():
    pair = QAPair(round=1, question_id="Q1", question="q", answer=ANSWERED, session_id="S1")  # purpose를 밝히지 않음
    s = FactStore()
    ingest_profile(s, _answer_fact(ANSWERED), raw_text=RAW + ANSWER, session_id="S1", qa_pairs=[pair])
    assert _other(s)[0].source_reference.ref == "raw_text:answers"


# d) "모름" 같은 답은 의료 사실 값이 되지 않음
@pytest.mark.parametrize("unknown", ["모름", "잘 모르겠어요", "모르겠습니다", "기억이 안 나요", "unknown", "답변 거부"])
def test_non_answers_are_not_stored_as_facts(unknown):
    raw = RAW + f"\n\n[추가 확인 정보 - 라운드 1]\nQ: EGFR 검사 결과를 아시나요?\nA: {unknown}"
    s = FactStore()
    r = ingest_profile(s, _answer_fact(unknown, description=f"EGFR status: {unknown}"), raw_text=raw,
                       session_id="S1", qa_pairs=[_qa(answer=unknown)])
    assert _other(s) == [] and r.withheld == [("other_fact", "non_answer")]
    assert {f.field for f in s.facts("P001")} >= {"age", "lab:hemoglobin"}  # 다른 사실은 그대로


# e) 의사 참고용 문답은 판정 근거(사실·evidence)에 섞이지 않음
def test_physician_reference_answers_are_not_facts_or_evidence():
    pair = _qa(purpose="physician_reference")
    s = FactStore()
    r = ingest_profile(s, _answer_fact(ANSWERED), raw_text=RAW + ANSWER, session_id="S1", qa_pairs=[pair])
    assert _other(s) == [] and r.withheld == [("other_fact", "physician_reference")]
    assert eligibility_answers("T1:I2", [pair]) == []  # 판정 근거 문답에서도 빠짐 (P14 D4)
    assert eligibility_answers("T1:I2", [_qa()]) != []


def test_mixed_purpose_link_is_ambiguous_not_dropped():
    s = FactStore()
    ingest_profile(s, _answer_fact(ANSWERED), raw_text=RAW + ANSWER, session_id="S1",
                   qa_pairs=[_qa("Q1", purpose="physician_reference"), _qa("Q2", rnd=2)])
    assert _other(s)[0].source_reference.ref == "raw_text:answers"


# Session 경로: pipeline._shadow_record가 qa_log를 넘김
def test_session_answer_fact_gets_question_ref(calls):  # noqa: F811
    s = _run()
    [q] = s.qa_log
    [egfr] = s.fact_store.facts("P001", "biomarker")
    assert egfr.source_reference.quote == ANSWER_QUOTE
    assert egfr.source_reference.ref == f"q:r{q.round}:{q.question_id}"
