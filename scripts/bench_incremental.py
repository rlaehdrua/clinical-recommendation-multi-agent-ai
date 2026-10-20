"""전체 평가 vs 부분 재평가 측정 (Phase 2). 합성 환자 1명 + 고정 기준표(data/parsed, 읽기만). API 호출 없음.

LLM 판정은 결정적 대역(항상 판정 불가로 답하고 호출 수만 셈)으로 대신합니다. 시간은 이 대역 기준이므로
실제 LLM 비용·지연 절감을 뜻하지 않습니다. 정확성(전체·부분 동등성)은 측정과 따로 확인해 함께 적으며,
LLM 기준의 '같음'은 양쪽이 같은 대역 출력을 썼다는 뜻일 뿐 실제 LLM 재판정과의 동등성이 아닙니다.
LLM 기준은 입력 항목을 검증할 수 없어 사실 내용이 바뀌면 항상 다시 판정합니다(대역 호출 수에 반영).

    PYTHONPATH=src python scripts/bench_incremental.py [--repeat 5] [--json]
"""

from __future__ import annotations

import argparse
import json
import statistics
import time

from ctrec import fact_changes, incremental, trial_store
from ctrec.fact_store import FactStore
from ctrec.schemas import CriterionAssessment, ParsedTrial, RuleMeta

PID = "SYN-BENCH"
REC = "clinical_record"


def load_frozen() -> dict[str, ParsedTrial]:
    """고정 기준표를 읽기만 함 (해시 확인·패치 적용 없음, 파일을 쓰지 않음)."""
    out = {}
    for tid in sorted(trial_store.frozen_manifest().get("files", {})):
        tid = tid.removesuffix(".json")
        rec = trial_store.read_record(tid)
        if rec is None:
            continue
        p = ParsedTrial.model_validate(rec["parsed"])
        p.rule_meta = {rid: RuleMeta.model_validate(m) for rid, m in (rec.get("rule_meta") or {}).items()}
        out[tid] = p
    return out


def synthetic_store() -> FactStore:
    s = FactStore()
    for field, value, kw in [
        ("age", 58, {"unit": "years"}), ("sex", "female", {}), ("ecog", 1, {}),
        ("primary_diagnosis", "non-small cell lung cancer", {}), ("disease_stage", "IV", {}),
        ("diagnosis", "hypertension", {}), ("biomarker", "EGFR exon 19 deletion positive", {}),
        ("medication", "amlodipine", {"source_type": "self_report"}),
        ("lab:hemoglobin", 11.5, {"unit": "g/dL", "observed_at": "2026-09-20"}),
        ("lab:platelets", 210, {"unit": "10^9/L", "observed_at": "2026-09-20"}),
        ("lab:absolute neutrophil count", 3.1, {"unit": "10^9/L", "observed_at": "2026-09-20"}),
        ("lab:creatinine", 0.9, {"unit": "mg/dL", "observed_at": "2026-09-20"}),
    ]:
        s.add(PID, field, value, **{"source_type": REC, **kw})
    return s


def _verify_hb(s: FactStore):
    f = s.facts(PID, "lab:hemoglobin")[0]
    s.set_verification(f.fact_id, "document_verified", reason="합성 결과지", by="bench", document_reference="SYN-DOC")


SCENARIOS = {
    "duplicate_reentry": lambda s: s.add(PID, "ecog", 1, source_type=REC),
    "ecog_1_to_2": lambda s: s.add(PID, "ecog", 2, source_type=REC),
    "hb_new_result": lambda s: s.add(PID, "lab:hemoglobin", 8.4, unit="g/dL", observed_at="2026-10-08", source_type=REC),
    "pdl1_added": lambda s: s.add(PID, "biomarker", "PD-L1 TPS 70%", source_type=REC),
    "hb_verified": _verify_hb,
    "medication_added": lambda s: s.add(PID, "medication", "warfarin", source_type="self_report"),
    "multi_field": lambda s: (s.add(PID, "age", 58, unit="years", source_type="self_report"),
                              s.add(PID, "lab:platelets", 95, unit="10^9/L", observed_at="2026-10-08", source_type=REC),
                              s.add(PID, "biomarker", "EGFR negative", source_type=REC)),
}


class CountingAssessor:
    """LLM 대역: 항상 판정 불가(근거 없음). 호출 수만 셈."""

    def __init__(self):
        self.calls = 0

    def __call__(self, tid, rule, profile, view):
        self.calls += 1
        return CriterionAssessment(
            rule_id=rule.rule_id, kind=rule.kind, criterion_text=rule.text, holds="unknown", passes=None,
            confidence="low", evidence="근거 없음", reasoning="bench", missing_info=rule.text, method="llm",
            category=rule.category, underspecified=rule.underspecified, cohort=rule.cohort), "bench_stub"


def _time(fn, repeat: int) -> tuple[float, object]:
    times, out = [], None
    for _ in range(repeat):
        t = time.perf_counter()
        out = fn()
        times.append((time.perf_counter() - t) * 1000)
    return statistics.median(times), out


def run(repeat: int = 5, parsed: dict[str, ParsedTrial] | None = None, scenarios: dict | None = None) -> list[dict]:
    parsed = parsed if parsed is not None else load_frozen()
    policy = incremental.EvaluationPolicy(reference_date="2026-10-12")
    rows = []
    for name, change in (scenarios or SCENARIOS).items():
        s = synthetic_store()
        before = fact_changes.take_snapshot(s, PID)
        st0 = incremental.evaluate_full(before, parsed, policy, CountingAssessor())
        change(s)
        after = fact_changes.take_snapshot(s, PID)
        full_llm, inc_llm = CountingAssessor(), CountingAssessor()
        full_ms, full = _time(lambda: incremental.evaluate_full(after, parsed, policy, full_llm), repeat)
        inc_ms, r = _time(lambda: incremental.update(st0, before, after, parsed, policy, inc_llm), repeat)
        eq = incremental.compare_states(r.state, full)  # 정확성 확인 (시간 측정과 별도)
        st = r.stats
        rows.append({"scenario": name, "trials": len(parsed), "total_criteria": st["total_criteria"],
                     "changed_fields": st["changed_fields"], "affected_criteria": st["affected_criteria"],
                     "reevaluated": st["reevaluated"], "reused": st["reused"],
                     "fallback_criteria": st["fallback_criteria"], "unavailable": st["unavailable"],
                     "validity_failures": st["validity_failures"], "reuse_blocked": st["reuse_blocked"],
                     "llm_stub_calls_full": full_llm.calls // repeat, "llm_stub_calls_incremental": inc_llm.calls // repeat,
                     "full_ms": round(full_ms, 2), "incremental_ms": round(inc_ms, 2),
                     "equivalence": eq["status"]})
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rows = run(args.repeat)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    cols = list(rows[0])
    print(" | ".join(cols))
    for r in rows:
        print(" | ".join(str(r[c]) for c in cols))


if __name__ == "__main__":
    main()
