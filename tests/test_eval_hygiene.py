"""WP1 평가 위생: 별칭, 프롬프트 누출 0, oracle 응답 범위, 누락 예측 분모 (실제 API 호출 없음)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from bench_incremental import load_frozen  # noqa: E402
from decision_scenarios import load_trials  # noqa: E402

from ctrec import config, llm, question_eval as qe  # noqa: E402
from ctrec.cohort_metrics import cohort_baseline_gate, cohort_eligibility_metrics  # noqa: E402
from ctrec.engine.contracts import ALIAS_PATTERN, PairKey  # noqa: E402
from ctrec.eval_hygiene import (AliasCollision, AliasMap, FactScope, LeakScanner, PromptLeakError,  # noqa: E402
                                PromptLeakGuard, ScopedOracleAnswerer, build_fact_scope, check_visible_case,
                                load_cases_hygienic, run_method_hygienic)
from ctrec.metrics import eligibility_metrics  # noqa: E402
from ctrec.schemas import ClarifyingQuestion  # noqa: E402

PARSED = load_frozen()
ALL_TRIALS = load_trials()
TRIALS = {t: ALL_TRIALS[t] for t in PARSED}
MASKED = json.loads(qe.DEFAULT_CASES.read_text(encoding="utf-8"))
VALID_DIR = config.DATA_DIR / "patients" / "masked_validation"
VALIDATION = [c for f in ("validation_cases.json", "supplement_cases.json")
              for c in json.loads((VALID_DIR / f).read_text(encoding="utf-8"))]
SUBSET = ["M-SYN-DEMO-A-DEMO-LUNG-001-rescue-1", "M-SYN-DEMO-A-DEMO-LUNG-001-rescue2-3",
          "M-SYN-DEMO-B-DEMO-LUNG-001-cleanup-1", "F-SYN-DEMO-A"]


def _q(qid, *targets):
    return ClarifyingQuestion(question_id=qid, question="합성 질문", answer_type="free_text", target=list(targets),
                              purpose="eligibility", why="")


# ------------------------------------------------------------ 별칭
def test_alias_consistent_opaque_and_contract_compatible():
    amap = AliasMap(salt="s1")
    ids = [c["patient_id"] for c in MASKED + VALIDATION]
    aliases = [amap.alias(i) for i in ids]
    assert aliases == [amap.alias(i) for i in ids]  # 같은 원본 -> 같은 별칭
    assert len(set(aliases)) == len(set(ids))  # 다른 환자 -> 충돌 없음
    for orig, a in zip(ids, aliases):
        assert re.match(ALIAS_PATTERN, a) and orig not in a
        assert not re.search(r"rescue|cleanup|-[IE]\d+-", a)
        PairKey(patient_alias=a, trial_id="T1")  # 계약 형식 통과
        assert amap.resolve(a) == orig
    assert AliasMap(salt="s1").alias(ids[0]) == aliases[0]  # 같은 salt면 재현 가능
    assert AliasMap(salt="s2").alias(ids[0]) != aliases[0]  # salt가 다르면 다름 (ID 해시만으로 추측 불가)
    assert "s1" not in repr(amap) and ids[0] not in repr(amap)


def test_alias_collision_detected(monkeypatch):
    amap = AliasMap(salt="x")
    a = amap.alias("A")
    monkeypatch.setattr("ctrec.eval_hygiene.hmac.new", lambda *a_, **k: type("H", (), {"hexdigest": lambda s: a[3:]})())
    with pytest.raises(AliasCollision):
        amap.alias("B")
    with pytest.raises(ValueError):
        amap.alias("")


def test_unalias_predictions():
    amap = AliasMap(salt="x")
    a = amap.alias("M-1")
    assert amap.unalias_keys({(a, "T1"): "ELIGIBLE", ("PA-unknown", "T2"): "INELIGIBLE"}) == {
        ("M-1", "T1"): "ELIGIBLE", ("PA-unknown", "T2"): "INELIGIBLE"}


# ------------------------------------------------------------ 누출 검사기
def test_scanner_flags_every_dataset_id_and_not_aliases():
    sc = LeakScanner()
    amap = AliasMap(salt="z")
    for c in MASKED + VALIDATION:
        assert sc.scan(f"patient_id: {c['patient_id']}"), c["patient_id"]  # ID 모양만으로도 잡힘
        assert sc.scan(f'{{"patient_id": "{amap.alias(c["patient_id"])}"}}') == []
    assert "label_token" in sc.scan("case is a rescue")
    sc.add_case("CUSTOM-ID-7")
    assert sc.scan("x CUSTOM-ID-7 y") == ["original_id"]


def test_visible_texts_of_all_cases_are_clean():
    """보이는 서술에는 원래 ID·정답 토큰·숨긴 사실 원문이 없음 (누출 경로는 ID 필드뿐임을 확인)."""
    bad = {c["patient_id"]: check_visible_case(c) for c in MASKED + VALIDATION}
    assert {k: v for k, v in bad.items() if v} == {}


# ------------------------------------------------------------ 실행 경로 연결 (오프라인 대역)
def test_original_ids_reach_prompts_without_hygiene():
    """기존 경로는 케이스 ID가 Matcher·추천 프롬프트로 들어감 (감사 P0-2 재현). 차단 없이 계측만."""
    cases = {g.case_id: (c, g) for c, g in qe.load_cases(PARSED, only=SUBSET[:1])}
    case, gt = cases[SUBSET[0]]
    sc = LeakScanner()
    sc.add_case(gt.case_id)
    with qe.OfflineEnvironment(3), PromptLeakGuard(sc, raise_on_leak=False) as guard:
        run = qe.run_method("candidate", case, gt, TRIALS, 1, 3)
    assert run.status == "ok"
    assert guard.scanned > 0 and guard.leaks["original_id"] > 0


@pytest.mark.parametrize("method", ["baseline", "candidate"])
def test_hygienic_run_has_zero_prompt_leaks(method):
    hcs, amap, scanner = load_cases_hygienic(PARSED, only=SUBSET, alias_map=AliasMap(salt="t"))
    assert len(hcs) == len(SUBSET)
    with qe.OfflineEnvironment(3):
        for hc in hcs:
            assert hc.case.patient_id.startswith("PA-") and hc.gt.case_id in SUBSET
            assert hc.visible_issues == []
            run, guard = run_method_hygienic(method, hc, TRIALS, 2, 3, scanner)
            assert run.status == "ok", (hc.gt.case_id, run.error_type)
            assert guard.scanned > 0 and sum(guard.leaks.values()) == 0
            assert amap.resolve(hc.case.patient_id) == hc.gt.case_id


def test_guard_blocks_before_sending(monkeypatch):
    sent = []
    monkeypatch.setattr(llm, "structured", lambda *a, **k: sent.append(k) or "ok")
    sc = LeakScanner()
    sc.add_case("M-SYN-X-rescue-1")
    with PromptLeakGuard(sc):
        with pytest.raises(PromptLeakError) as e:
            llm.structured(object, system="s", user="patient_id: M-SYN-X-rescue-1")
        assert "M-SYN-X" not in str(e.value)  # 오류 메시지에 원문 없음
        assert llm.structured(object, system="s", user="patient_id: PA-0123456789abcdef") == "ok"
    assert len(sent) == 1
    assert not hasattr(llm.structured, "__wrapped__")  # 끝나면 원래 함수로 되돌림


def test_hygienic_run_reports_leak_as_error_status():
    hcs, _, scanner = load_cases_hygienic(PARSED, only=SUBSET[:1], alias_map=AliasMap(salt="t"))
    hc = hcs[0]
    from dataclasses import replace
    leaky = replace(hc, case=replace(hc.case, patient_id=hc.gt.case_id))  # 별칭을 빼면 차단되어야 함
    with qe.OfflineEnvironment(3):
        run, guard = run_method_hygienic("baseline", leaky, TRIALS, 1, 3, scanner)
    assert run.status == "error" and run.error_type == "PromptLeakError"
    assert guard.leaks["original_id"] >= 1


# ------------------------------------------------------------ oracle 응답 범위
class _GT:
    def __init__(self, exact, hidden):
        self._exact, self._hidden_texts = set(exact), tuple(hidden)

    def exact_keys(self):
        return self._exact


def test_legacy_oracle_reveals_all_facts_for_single_hit():
    """기존 OracleAnswerer 동작 확인: 기준 하나만 맞아도 숨긴 사실 전부 (회수율 과대 추정 원인)."""
    gt = _GT({"T:I1", "T:I2"}, [("F1", "EGFR exon 19 deletion."), ("F2", "ECOG is 1.")])
    out = qe.OracleAnswerer(gt).answer([_q("Q1", "T:I1")], "P")
    assert "EGFR" in out["Q1"] and "ECOG" in out["Q1"]


def test_scoped_oracle_reveals_only_facts_of_targeted_criterion():
    gt = _GT({"T:I1", "T:I2"}, [("F1", "EGFR exon 19 deletion."), ("F2", "ECOG is 1."), ("F3", "Unmapped fact.")])
    scope = FactScope({"T:I1": ("F1",), "T:I2": ("F2",)}, ("F3",), "token_overlap")
    a = ScopedOracleAnswerer(gt, scope)
    out = a.answer([_q("Q1", "T:I1"), _q("Q2", "T:I2", "T:I1"), _q("Q3", "T:I9"), _q("Q4")], "P")
    assert out["Q1"] == "EGFR exon 19 deletion."
    assert out["Q2"] == "ECOG is 1. EGFR exon 19 deletion."
    assert out["Q3"] == "모름" and out["Q4"] == "모름"
    assert all("Unmapped" not in v for v in out.values())  # 대응을 정하지 못한 사실은 공개하지 않음
    assert [x["fact_ids"] for x in a.log] == [["F1"], ["F2", "F1"], [], []]


def test_scoped_oracle_ignores_inferred_targets():
    gt = _GT({"T:I1"}, [("F1", "x")])
    a = ScopedOracleAnswerer(gt, FactScope({"T:I1": ("F1",), "T:I2": ("F1",)}))
    assert a.answer([_q("Q1", "T:I2")], "P")["Q1"] == "모름"  # EXACT가 아닌 대상은 공개 안 함


def test_fact_scope_on_real_cases():
    multi = 0
    for raw in MASKED:
        scope = build_fact_scope(raw, PARSED)
        assigned = [i for ids in scope.by_rule.values() for i in ids]
        assert set(assigned) | set(scope.unassigned) <= set(raw["masked_fact_ids"])
        if len(raw["masked_criteria"]) > 1:
            multi += 1
            assert scope.method == "token_overlap"
    assert multi == 17
    r = next(c for c in MASKED if c["patient_id"] == "M-SYN-DEMO-A-DEMO-LUNG-001-rescue2-3")
    s = build_fact_scope(r, PARSED)
    egfr = [k for k, v in s.by_rule.items() if "F6" in v]
    ecog = [k for k, v in s.by_rule.items() if "F12" in v]
    assert egfr and ecog and set(egfr).isdisjoint(ecog)  # EGFR 사실과 ECOG 사실이 다른 기준에 대응


def test_scoped_oracle_lowers_or_keeps_recovery_vs_legacy():
    """같은 실행에서 범위 제한 답변기의 공개 사실은 기존 답변기의 부분집합."""
    hcs, _, scanner = load_cases_hygienic(PARSED, only=["M-SYN-DEMO-A-DEMO-LUNG-001-rescue2-3"],
                                          alias_map=AliasMap(salt="t"))
    hc = hcs[0]
    with qe.OfflineEnvironment(3):
        scoped, _ = run_method_hygienic("candidate", hc, TRIALS, 2, 3, scanner, scoped_oracle=True)
        legacy, _ = run_method_hygienic("candidate", hc, TRIALS, 2, 3, scanner, scoped_oracle=False)
    assert scoped.status == legacy.status == "ok"
    assert scoped.revealed <= legacy.revealed
    assert qe.OracleAnswerer.__name__ == "OracleAnswerer"  # 교체가 원래대로 돌아옴


# ------------------------------------------------------------ 누락 예측 분모
GOLD = {("P1", "T1"): "ELIGIBLE", ("P1", "T2"): "INELIGIBLE", ("P2", "T1"): "ELIGIBLE", ("P2", "T2"): "INELIGIBLE"}


def test_missing_predictions_counted_in_denominator():
    pred = {("P1", "T1"): "ELIGIBLE", ("P1", "T2"): "INELIGIBLE", ("X", "T9"): "ELIGIBLE"}  # P2 실행 실패
    legacy = eligibility_metrics(GOLD, pred)
    assert legacy["accuracy"] == 1.0 and legacy["pairs"] == 2  # 기존: 누락이 빠져 100%
    m = cohort_eligibility_metrics(GOLD, pred)
    assert m["pairs"] == 4 and m["accuracy"] == 0.5 and m["coverage"] == 0.5
    assert m["missing_predictions"] == 2 and m["missing_rate"] == 0.5 and m["patients_with_missing"] == 1
    assert m["extra_predictions"] == 1
    assert m["false_removal"] == 0.0  # 누락은 부적격 예측으로 세지 않음 (NOT_EVALUATED)
    assert m["confirmed_correct_pairs"] == 1 and m["gold_eligible_pairs"] == 2


def test_confirmed_pairs_and_gate():
    pred = dict(GOLD)
    pred[("P1", "T2")] = "ELIGIBLE"  # 오확정
    m = cohort_eligibility_metrics(GOLD, pred, confirmed={("P1", "T1"), ("P1", "T2")})
    assert m["confirmed_correct_pairs"] == 1 and m["confirmed_wrong_pairs"] == 1
    base = cohort_eligibility_metrics(GOLD, GOLD)
    ok, msg = cohort_baseline_gate(m, base)
    assert not ok and "불합격" in msg
    assert cohort_baseline_gate(base, base)[0]
    assert not cohort_baseline_gate(eligibility_metrics(GOLD, GOLD), base)[0]  # 기존 지표와 섞지 않음
    smaller = cohort_eligibility_metrics({k: v for k, v in list(GOLD.items())[:2]}, GOLD)
    assert not cohort_baseline_gate(smaller, base)[0]


def test_empty_gold():
    m = cohort_eligibility_metrics({}, {("P", "T"): "ELIGIBLE"})
    assert m["pairs"] == 0 and m["accuracy"] is None and m["missing_rate"] is None and m["extra_predictions"] == 1
