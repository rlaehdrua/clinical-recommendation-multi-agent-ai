"""정보 가리기 평가셋: 정답 격리, 사실 단위 가상 환자, 회수 지표, 변형 생성 규칙 (API 호출 없음)."""

import importlib.util
import json
import sys
from pathlib import Path

from ctrec.agents import answerers
from ctrec.agents.answerers import FactRevealAnswerer, _Reveal, _Reveals
from ctrec.metrics import fact_acquisition_metrics
from ctrec.pipeline import load_patients
from ctrec.schemas import ClarifyingQuestion

FACTS = [{"fact_id": "F1", "text": "The patient is 60 years old.", "core": True},
         {"fact_id": "F2", "text": "Platelets are 150 x10^9/L.", "core": False},
         {"fact_id": "F3", "text": "No prior chemotherapy.", "core": False},
         {"fact_id": "F4", "text": "No brain metastases on MRI.", "core": False}]


def _q(qid, purpose="eligibility"):
    return ClarifyingQuestion(question_id=qid, question=f"질문 {qid}", answer_type="free_text", target=[],
                              purpose=purpose, why="")


def test_hidden_facts_never_reach_pipeline(tmp_path: Path):
    case = {"patient_id": "M-1", "text": "- The patient is 60 years old.", "oracle_facts": FACTS,
            "hidden_facts": FACTS[1:], "masked_fact_ids": ["F2"], "clue_fact_ids": [], "case_type": "rescue",
            "masked_criteria": ["Platelets >= 100"], "source_patient": "S", "candidate_trials": ["T"]}
    p = tmp_path / "cases.json"
    p.write_text(json.dumps([case]), encoding="utf-8")
    c = load_patients(p)[0]
    assert c.raw_text == "- The patient is 60 years old."
    assert "Platelets" not in c.raw_text and c.oracle_facts == FACTS and c.candidate_trials == ["T"]


def test_fact_reveal_answers_verbatim_with_whitelist_and_cap(monkeypatch):
    def fake(_cls, **_):
        return _Reveals(reveals=[_Reveal(question_id="Q1", fact_ids=["F2", "F99", "F3", "F4"]),
                                 _Reveal(question_id="Q2", fact_ids=[])])
    monkeypatch.setattr(answerers.llm, "structured", fake)
    a = FactRevealAnswerer({"P": FACTS}, max_facts_per_answer=2)
    out = a.answer([_q("Q1"), _q("Q2")], "P")
    # 지어낸 ID(F99)는 버리고, 질문당 최대 2개, 답변은 사실 원문 그대로
    assert out["Q1"] == "Platelets are 150 x10^9/L. No prior chemotherapy."
    assert out["Q2"].startswith("모름")
    assert [r["fact_ids"] for r in a.revealed("P")] == [["F2", "F3"], []]


def test_fact_acquisition_metrics():
    rows = [{"patient_id": "M-1", "masked_fact_ids": "F2;F3"}, {"patient_id": "F-1", "masked_fact_ids": ""}]
    reveals = {"M-1": [{"purpose": "eligibility", "fact_ids": ["F2"]},
                       {"purpose": "eligibility", "fact_ids": ["F4"]},
                       {"purpose": "physician_reference", "fact_ids": []}],
               "F-1": []}
    m = fact_acquisition_metrics(rows, reveals)
    assert m["masked_patients"] == 1
    assert m["masked_fact_recovery"] == 0.5 and m["full_recovery"] == 0.0
    assert m["useful_question_rate"] == 0.5 and m["questions_per_masked_fact"] == 2.0


def _builder():
    path = Path(__file__).resolve().parents[1] / "scripts" / "build_masked_benchmark.py"
    spec = importlib.util.spec_from_file_location("build_masked_benchmark", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # pydantic이 모듈 안의 타입 이름을 찾을 수 있도록 등록
    spec.loader.exec_module(mod)
    return mod


def test_variant_rules():
    b = _builder()
    facts = [b.AtomicFact(**f) for f in FACTS]

    def crit(status, ids, text="c"):
        return b.DecisiveCriterion(criterion=text, kind="inclusion", patient_status=status, evidence_fact_ids=ids)

    # 부적격: 위반 근거 전부 숨김 (cleanup)
    ann = b.PairAnnotation(criteria=[crit("violated", ["F3"]), crit("violated", ["F4", "F77"])], notes="")
    v, skip = b.variants_for_pair("S", "T", "INELIGIBLE", facts, ann, 2)
    assert skip is None and v[0][0] == "cleanup" and v[0][1] == {"F3", "F4"}
    # 핵심 사실(나이)이 근거면 숨기지 않음
    v, skip = b.variants_for_pair("S", "T", "INELIGIBLE", facts, b.PairAnnotation(criteria=[crit("violated", ["F1"])], notes=""), 2)
    assert v == [] and skip
    # 적격: 기준별 1개씩 + 2개 동시 변형, 라벨과 모순된 주석은 건너뜀
    ann = b.PairAnnotation(criteria=[crit("satisfied", ["F2"]), crit("satisfied", ["F3"])], notes="")
    v, _ = b.variants_for_pair("S", "T", "ELIGIBLE", facts, ann, 2)
    assert [t for t, _, _ in v] == ["rescue", "rescue", "rescue2"] and v[2][1] == {"F2", "F3"}
    v, skip = b.variants_for_pair("S", "T", "ELIGIBLE", facts, b.PairAnnotation(criteria=[crit("violated", ["F2"])], notes=""), 2)
    assert v == [] and "라벨" in skip


def test_leak_check_hides_clues_or_drops(monkeypatch):
    b = _builder()
    facts = [b.AtomicFact(**f) for f in FACTS]
    calls = []

    def fake(kind, key, cls, **_):
        calls.append(key[2])
        leak = key[2] == "F3"  # 처음엔 F4가 단서, F4까지 숨기면 통과
        return b.MaskCheck(checks=[b.CriterionCheck(criterion="c", determinable=leak,
                                                    revealing_fact_ids=["F4"] if leak else [])])
    monkeypatch.setattr(b, "_cached", fake)
    masked, why = b.verify_mask("S", "T", facts, {"F3"}, ["c"])
    assert masked == {"F3", "F4"} and why is None and calls == ["F3", "F3,F4"]

    monkeypatch.setattr(b, "_cached", lambda *a, **k: b.MaskCheck(
        checks=[b.CriterionCheck(criterion="c", determinable=True, revealing_fact_ids=["F1"])]))
    masked, why = b.verify_mask("S", "T", facts, {"F3"}, ["c"])
    assert masked is None and "핵심" in why
