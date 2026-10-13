"""질문 계획 오프라인 비교 평가 (Phase 4-1). 합성 masked case + 오프라인 대역, API 호출 없음."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from bench_incremental import load_frozen  # noqa: E402
from decision_scenarios import load_trials  # noqa: E402

from ctrec import config, llm, question_eval as qe, store_patch  # noqa: E402
from ctrec.schemas import ClarifyingQuestion, QAPair  # noqa: E402

PARSED = load_frozen()
ALL_TRIALS = load_trials()
TRIALS = {t: ALL_TRIALS[t] for t in PARSED}
SUBSET = ["M-SYN-DEMO-A-DEMO-LUNG-001-rescue-1", "M-SYN-DEMO-A-DEMO-LUNG-001-rescue-2",
          "M-SYN-DEMO-B-DEMO-LUNG-001-cleanup-1", "F-SYN-DEMO-A"]


@pytest.fixture(scope="module")
def subset_result():
    return qe.run_evaluation(PARSED, TRIALS, only=SUBSET, rounds=2, budget=3, ablation=True)


def _cases(only=None):
    return {g.case_id: (c, g) for c, g in qe.load_cases(PARSED, only=only)}


# ------------------------------------------------------------ 사례 로딩·유효성
def test_load_and_validate_101_cases():
    cases = qe.load_cases(PARSED)
    assert len(cases) == 101 and all(g.synthetic for _, g in cases)
    report = qe.validate_cases(parsed=PARSED)
    assert report["cases"] == 101 and report["issues"] == {}
    assert report["masked_criteria_mapping"] == {"EXACT": 72, "INFERRED": 8, "UNMAPPED": 10}


# ------------------------------------------------------------ 정답 분리·누출
def test_pipeline_input_has_no_ground_truth():
    raw = {c["patient_id"]: c for c in json.loads(qe.DEFAULT_CASES.read_text(encoding="utf-8"))}
    for case, gt in qe.load_cases(PARSED):
        r = raw[gt.case_id]
        for h in r["hidden_facts"]:
            assert h["text"] not in case.raw_text
        for key in ("oracle_facts", "hidden_facts", "masked_fact_ids", "masked_criteria", "clue_fact_ids"):
            assert key not in case.raw_text
        assert case.hidden_details is None and case.oracle_facts is None


def test_hidden_text_reaches_models_only_after_simulated_answer(monkeypatch):
    """모델 입력(프로필 대역·Matcher 대역·질문 생성기)에 숨긴 사실이 들어가는 것은 답변 시뮬레이터가 공개한 뒤뿐."""
    cases = _cases(SUBSET)
    hidden = {cid: [t for _, t in g._hidden_texts] for cid, (_, g) in cases.items()}
    ids = {cid: list(g.masked_fact_ids) for cid, (_, g) in cases.items()}
    revealed = {"any": False}
    seen = []
    real_answer = qe.OracleAnswerer.answer

    def answer(self, questions, pid):
        out = real_answer(self, questions, pid)
        revealed["any"] |= any(v != "모름" for v in out.values())
        return out
    monkeypatch.setattr(qe.OracleAnswerer, "answer", answer)
    real_profiler, real_structured = qe.stub_profiler, qe.StubLLM.structured

    def profiler(pid, raw_text, reference_date=None):  # 사례별: 그 사례의 숨긴 사실만 확인
        seen.append((revealed["any"], any(h in raw_text for h in hidden[pid])))
        return real_profiler(pid, raw_text, reference_date)

    def structured(self, model_cls, *, system, user, **k):
        cid = next(c for c in cases if f'"patient_id": "{c}"' in user or f"patient_id={c}" in user)
        seen.append((revealed["any"], any(h in user or h in system for h in hidden[cid])
                     or any(f'"{i}"' in user for i in ids[cid])))
        return real_structured(self, model_cls, system=system, user=user, **k)
    monkeypatch.setattr(qe, "stub_profiler", profiler)
    monkeypatch.setattr(qe.StubLLM, "structured", structured)
    qe.run_evaluation(PARSED, TRIALS, only=SUBSET, rounds=2, budget=3)
    assert seen and revealed["any"]
    assert all(after for after, leaked in seen if leaked)  # 누출로 보이는 입력은 모두 공개 이후


# ------------------------------------------------------------ 공정성: 같은 상태·같은 예산·분리
def test_same_snapshot_budget_and_separation(subset_result):
    r = subset_result
    assert r["round1_matcher_identical"] and r["cases_total"] == 4
    by = defaultdict(dict)
    for q in r["question_records"]:
        by[(q["case_id"], q["round_id"])].setdefault(q["method"], set()).add(q["fact_version"])
    for (cid, rnd), m in by.items():
        if rnd == 1 and len(m) > 1:
            assert len(set.union(*m.values())) == 1  # 라운드 1은 세 방식이 같은 사실 스냅샷
    per_round = defaultdict(int)
    for q in r["question_records"]:
        per_round[(q["case_id"], q["method"], q["round_id"])] += 1
    assert max(per_round.values()) <= 3  # 같은 라운드당 예산
    sources = {q["method"]: q["source"] for q in r["question_records"]}
    assert sources == {"baseline": qe.BASELINE_SOURCE, "candidate": qe.CANDIDATE_SOURCE,
                       qe.ABLATION: qe.ABLATION_SOURCE}


def test_budget_exhaustion():
    r = qe.run_evaluation(PARSED, TRIALS, only=SUBSET[:2], rounds=1, budget=1)
    counts = defaultdict(int)
    for q in r["question_records"]:
        counts[(q["case_id"], q["method"])] += 1
    assert counts and set(counts.values()) == {1}


# ------------------------------------------------------------ 매핑
def test_criterion_mapping_statuses():
    demo = PARSED["DEMO-LUNG-001"]
    text = demo.rules[0].text
    assert qe.map_criterion(text, ["DEMO-LUNG-001"], PARSED).status == "EXACT"
    long_rule = next(r for r in demo.rules if len(r.text) >= 25)
    assert qe.map_criterion(long_rule.text + " and other things", ["DEMO-LUNG-001"], PARSED).status in (
        "INFERRED", "AMBIGUOUS")
    assert qe.map_criterion("완전히 다른 합성 기준 문장입니다", ["DEMO-LUNG-001"], PARSED).status == "UNMAPPED"


def _view_for(trial_ids):
    from ctrec.decision_state import SessionView
    from ctrec.fact_changes import make_snapshot
    return SessionView(session_id="S", patient_id="P", snapshot=make_snapshot("P", []), matches={},
                       parsed={t: PARSED[t] for t in trial_ids}, trials={t: TRIALS[t] for t in trial_ids})


def test_egfr_biomarker_vs_kidney_egfr_not_confused():
    gt = qe.CaseGroundTruth(case_id="X", case_type="rescue", synthetic=True, candidate_trials=("DEMO-LUNG-001",),
                            masked_fact_ids=(), targets=(qe.GTTarget("C-x", "EXACT", ("DEMO-LUNG-001:I4",)),))
    view = _view_for(["DEMO-LUNG-001", "NCT06125405"])
    kidney = ClarifyingQuestion(question_id="Q1", question="q", answer_type="free_text", target=["NCT06125405:I5"],
                                purpose="eligibility", why="w")
    rec = qe._record(gt, "candidate", 1, 0, kidney, view, [])
    assert rec["normalized_target"] == ["lab:egfr"] and rec["mapping_status"] == "MISS"
    bio = kidney.model_copy(update={"target": ["DEMO-LUNG-001:I4"]})
    rec2 = qe._record(gt, "candidate", 1, 0, bio, view, [])
    assert rec2["normalized_target"] == ["biomarker:EGFR"] and rec2["mapping_status"] == "EXACT_HIT"


def test_duplicate_detection():
    gt = qe.CaseGroundTruth(case_id="X", case_type="full", synthetic=True, candidate_trials=("DEMO-LUNG-001",),
                            masked_fact_ids=(), targets=())
    view = _view_for(["DEMO-LUNG-001"])
    q = ClarifyingQuestion(question_id="Q1", question="q", answer_type="free_text", target=["DEMO-LUNG-001:I6"],
                           purpose="eligibility", why="w")
    prev = [QAPair(round=1, question_id="Q1", question="q", answer="ECOG 1", purpose="eligibility",
                   target=["DEMO-LUNG-001:I6"])]
    rec = qe._record(gt, "baseline", 2, 0, q, view, prev)
    assert rec["same_criterion_reasked"] == ["DEMO-LUNG-001:I6"] and rec["answered_reasked"] == ["DEMO-LUNG-001:I6"]
    other_ecog = dict(rec, target_criterion_ids=["DEMO-LUNG-001:I99"], same_criterion_reasked=[], answered_reasked=[],
                      relevance={"relevant": 1})  # 다른 기준이지만 같은 정보(ecog) -> 의미상 중복
    run = qe.MethodRun("baseline", questions=[dict(rec, relevance={"relevant": 1}), other_ecog])
    m = qe.case_metrics(gt, run, 3)
    assert m["duplicate_same_criterion"] == 1 and m["reasked_answered"] == 1 and m["duplicate_semantic"] == 1


# ------------------------------------------------------------ 불충족 분류 (확정 제외, 잠정 보존, OR 경로)
@pytest.fixture()
def stub_matcher(monkeypatch):
    import decision_fixtures as d
    from ctrec.agents import matcher
    monkeypatch.setattr(matcher.llm, "structured", d.unknown_llm)
    return d


def test_relevance_classification(stub_matcher):
    d = stub_matcher
    s = d.store(("age", 12, {"unit": "years"}))
    f = s.facts(d.P, "age")[0]
    s.set_verification(f.fact_id, "document_verified", reason="합성", by="t", document_reference="DOC")
    age_a = d.AGE.model_copy(update={"rule_id": "I7", "cohort": "A"})
    ecog_b = d.ECOG.model_copy(update={"rule_id": "I8", "cohort": "B"})
    v = d.view({"T1": d.trial("T1", [d.AGE, d.ECOG]), "T2": d.trial("T2", [d.DX, age_a, ecog_b], cohorts=["A", "B"])}, s)
    assert qe._classify(v, ["T1:I2"]) == {"confirmed_mismatch_only": 1}  # 확정 불충족 시험만을 위한 질문
    assert qe._classify(v, ["T2:I8"]) == {"relevant": 1}  # 다른 코호트 경로가 열려 있음
    assert qe._classify(v, ["T1:I1"]) == {"already_sufficient": 1}
    assert qe._classify(v, ["T9:I1"]) == {"not_candidate": 1}
    s2 = d.store(("age", 12, {"unit": "years"}))  # 문서 검증 전 -> 잠정 불충족은 불필요 질문으로 세지 않음
    v2 = d.view({"T1": d.trial("T1", [d.AGE, d.ECOG])}, s2)
    assert qe._classify(v2, ["T1:I2"]) == {"provisional_mismatch": 1}


# ------------------------------------------------------------ 답변 시뮬레이션
def test_oracle_answers_only_exact_hits():
    case, gt = _cases(["M-SYN-DEMO-A-DEMO-LUNG-001-rescue-1"])["M-SYN-DEMO-A-DEMO-LUNG-001-rescue-1"]
    exact = sorted(gt.exact_keys())[0]
    ans = qe.OracleAnswerer(gt)

    def q(qid, target):
        return ClarifyingQuestion(question_id=qid, question="q", answer_type="free_text", target=target,
                                  purpose="eligibility", why="w")
    out = ans.answer([q("Q1", [exact]), q("Q2", ["DEMO-LUNG-001:I1"])], case.patient_id)
    assert out["Q1"] != "모름" and out["Q2"] == "모름"
    assert [x["revealed"] for x in ans.log] == [True, False]
    inferred_gt = qe.CaseGroundTruth(case_id="X", case_type="rescue", synthetic=True,
                                     candidate_trials=gt.candidate_trials, masked_fact_ids=gt.masked_fact_ids,
                                     targets=(qe.GTTarget("C-x", "INFERRED", (exact,)),), _hidden_texts=gt._hidden_texts)
    assert qe.OracleAnswerer(inferred_gt).answer([q("Q1", [exact])], "X")["Q1"] == "모름"  # 추정 대응은 자동 공개 안 함


def test_resolution_and_unresolved_counted(subset_result):
    rows = {(r["case_id"], r["method"]): r for r in subset_result["case_rows"]}
    hit = rows[("M-SYN-DEMO-A-DEMO-LUNG-001-rescue-1", "baseline")]
    assert hit["revealed_answers"] >= 1 and hit["unanswered_questions"] == hit["questions"] - hit["revealed_answers"]
    full = rows[("F-SYN-DEMO-A", "candidate")]
    assert full["revealed_answers"] == 0 and full["has_exact_target"] is False  # 숨긴 정보가 없는 사례


# ------------------------------------------------------------ 실제 LLM 차단·비용·승인 게이트
def test_real_llm_blocked_and_restored():
    original = (llm.client, llm.create, llm.plain, config.MAX_QUESTIONS_PER_ROUND)
    with qe.OfflineEnvironment(2):
        for fn in (llm.client, lambda: llm.create(system="s", messages=[]), lambda: llm.plain(system="s", user="u")):
            with pytest.raises(RuntimeError, match="차단"):
                fn()
        assert config.MAX_QUESTIONS_PER_ROUND == 2
    assert (llm.client, llm.create, llm.plain, config.MAX_QUESTIONS_PER_ROUND) == original


def test_cost_estimate_and_paid_gate(monkeypatch):
    est = qe.estimate_cost(101, 1.4, 2, cap_usd=1.0)
    assert est["prices_confirmed"] is False and est["tokens_confirmed"] is False and est["within_cap"] is False
    assert est["calls_per_case_per_method"]["patient_profiler"] == 3 and "참고값" in est["reference_note"]
    with pytest.raises(qe.PaidRunNotApproved, match="승인 플래그"):
        qe.require_paid_approval(False, est)
    monkeypatch.setenv("CTREC_ALLOW_PAID_EVAL", "1")
    with pytest.raises(qe.PaidRunNotApproved, match="비용 상한"):
        qe.require_paid_approval(True, est)
    ok = qe.estimate_cost(1, 1, 1, cap_usd=10_000, prices={"default": {"input": 1, "output": 1, "confirmed": True}})
    with pytest.raises(qe.PaidRunNotApproved, match="Phase 4-1"):
        qe.require_paid_approval(True, ok)  # 모든 조건이 맞아도 이번 단계에는 실행 경로 없음


# ------------------------------------------------------------ 취소·시간 초과는 실패로 남김
def test_cancel_and_timeout_reported_not_dropped():
    r = qe.run_evaluation(PARSED, TRIALS, only=SUBSET[:2], rounds=1, budget=2, cancel=lambda: True)
    assert r["methods"]["baseline"]["failed"] == 2 and r["methods"]["baseline"]["ok"] == 0
    assert {c["status"] for c in r["methods"]["candidate"]["failed_cases"]} == {"cancelled"}
    r2 = qe.run_evaluation(PARSED, TRIALS, only=SUBSET[:1], rounds=1, budget=2, case_timeout_s=0)
    assert r2["methods"]["baseline"]["failed_cases"][0]["status"] == "timeout"


# ------------------------------------------------------------ 저장·개인정보·기존 결과 불변
def test_outputs_have_no_text_and_default_writes_nothing(tmp_path, monkeypatch, subset_result):
    monkeypatch.chdir(tmp_path)
    assert list(tmp_path.iterdir()) == []  # run_evaluation 자체는 파일을 쓰지 않음 (subset_result는 이미 실행됨)
    paths = qe.save_outputs(subset_result, tmp_path / "out")
    blob = "".join(p.read_text(encoding="utf-8") for p in paths)
    raw = json.loads(qe.DEFAULT_CASES.read_text(encoding="utf-8"))
    for c in raw:
        if c["patient_id"] in SUBSET:
            for h in c["hidden_facts"] + c["oracle_facts"]:
                assert h["text"] not in blob
            for crit in c["masked_criteria"]:
                assert crit not in blob


def test_matcher_and_store_unchanged(subset_result):
    assert subset_result["round1_matcher_identical"]
    for m in subset_result["methods"].values():
        assert set(m["safety"].values()) <= {0}
    assert store_patch.verify_frozen() == []
