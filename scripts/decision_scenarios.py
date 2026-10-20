"""Decision Loop 진단 시나리오 (Phase 3). 고정 기준표 26개 시험·471개 기준(읽기만) + 합성 환자. API 호출 없음.

기준선 판정은 실제 Matcher(match_trial)에 LLM 대역(LLM 판정 기준은 모두 '판정 불가')을 넣어 만듭니다.
따라서 LLM 기준의 실제 판정 품질·비용과 무관하며, 결과를 실제 LLM 비용 절감·정확도로 해석하면 안 됩니다.

    PYTHONPATH=src python scripts/decision_scenarios.py [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bench_incremental import load_frozen  # noqa: E402

from ctrec import config, fact_changes  # noqa: E402
from ctrec.agents import matcher  # noqa: E402
from ctrec.decision_loop import LoopConfig, LoopContext, run_loop  # noqa: E402
from ctrec.decision_state import Budget, SessionView  # noqa: E402
from ctrec.fact_store import FactStore  # noqa: E402
from ctrec.fact_view import FactEvidenceView  # noqa: E402
from ctrec.schemas import LLMAssessment, MatcherOutput, QAPair  # noqa: E402
from ctrec.tools import ctgov  # noqa: E402

PID = "SYN-DECISION"
REF = "2026-10-12"
REC = "clinical_record"


class StubLLM:
    """Matcher LLM 대역: 요청받은 LLM 판정 기준을 모두 '판정 불가(근거 없음)'로. 호출 수만 셈."""

    def __init__(self):
        self.calls = 0

    def __call__(self, model_cls, *, system, user, **k):
        self.calls += 1
        rules = json.loads(user.split("<rules_to_assess>")[1].split("</rules_to_assess>")[0])
        return MatcherOutput(assessments=[LLMAssessment(
            rule_id=r["rule_id"], applicable=True, holds="unknown", evidence_type="absent", confidence="low",
            evidence="근거 없음", reasoning="stub", missing_info="확인 필요") for r in rules])


class SimChannel:
    def __init__(self, answers: dict):
        self.answers, self.asked = answers, []

    def ask(self, action, state):
        self.asked.append(action.info_key)
        a = self.answers.get(action.info_key)
        if a == "declined":
            return {"facts": [], "declined": True}
        return {"facts": list(a or []), "declined": False}


def load_trials() -> dict[str, dict]:
    out = {}
    for p in sorted(config.TRIALS_DIR.glob("*.json")):
        rec = json.loads(p.read_text(encoding="utf-8"))
        if "trial_id" in rec:
            out[rec["trial_id"]] = ctgov.load_local(p)
    return out


def _store(facts) -> FactStore:
    s = FactStore()
    for field, value, kw in facts:
        s.add(PID, field, value, **{"source_type": REC, **kw})
    return s


BASE = [("age", 63, {"unit": "years"}), ("sex", "male", {}),
        ("primary_diagnosis", "non-small cell lung cancer", {}), ("disease_stage", "IV", {})]
LABS = [("lab:hemoglobin", 12.1, {"unit": "g/dL", "observed_at": "2026-09-20"}),
        ("lab:platelets", 210, {"unit": "10^9/L", "observed_at": "2026-09-20"}),
        ("lab:absolute neutrophil count", 3.1, {"unit": "10^9/L", "observed_at": "2026-09-20"}),
        ("lab:creatinine", 0.9, {"unit": "mg/dL", "observed_at": "2026-09-20"})]


def _view(facts, parsed, trials, stub, qa=None, source_text=None, input_source=REC):
    s = _store(facts)
    profile = FactEvidenceView(s, PID).to_profile()
    t0 = time.perf_counter()
    matches = {tid: matcher.match_trial(profile, p, trials[tid], reference_date=REF, source_text=source_text,
                                        input_source=input_source) for tid, p in parsed.items()}
    baseline_ms = (time.perf_counter() - t0) * 1000
    snap = fact_changes.take_snapshot(s, PID)
    v = SessionView(session_id="S-scenario", patient_id=PID, snapshot=snap, matches=matches, parsed=parsed,
                    trials=trials, qa_log=list(qa or []), reference_date=REF,
                    matched_versions={t: snap.version for t in matches})
    return v, baseline_ms


def _run(v, channel=None, budget=None, stop_check=lambda: None):
    ctx = LoopContext(view=v, matched_snapshots={t: v.snapshot for t in v.matches})
    t0 = time.perf_counter()
    r = run_loop(ctx, channel, LoopConfig(budget=budget or Budget()), stop_check=stop_check)
    return r, (time.perf_counter() - t0) * 1000


def scenarios(parsed, trials, stub):
    first_asks: list = []

    def s1():
        v, b = _view(BASE, parsed, trials, stub)
        r, ms = _run(v)
        first_asks.extend(a for a in r.actions if a.action_type == "ASK_USER")
        return "1 필수 정보가 거의 없는 환자", v, r, ms, b

    def s2():
        v, b = _view(BASE + LABS, parsed, trials, stub)
        r, ms = _run(v, SimChannel({"ecog": [("ecog", 1, {})]}))  # 질문 답(자기 보고)으로 ECOG 추가
        return "2 일부 검사 결과 + ECOG 답변 시뮬레이션", v, r, ms, b

    def s3():
        v, b = _view(BASE + LABS + [("ecog", 1, {}), ("ecog", 3, {})], parsed, trials, stub)
        r, ms = _run(v)
        return "3 ECOG 정보 충돌", v, r, ms, b

    def s4():
        v, b = _view(BASE + LABS + [("ecog", 1, {})], parsed, trials, stub)
        r, ms = _run(v)
        return "4 바이오마커 정보 부족", v, r, ms, b

    def s5():
        targets = [k for a in first_asks[:3] for k in a.target_criterion_ids]
        qa = [QAPair(round=1, question_id="Q1", question="합성 질문", answer="모름", purpose="eligibility",
                     target=targets)]
        v, b = _view(BASE, parsed, trials, stub, qa=qa)
        r, ms = _run(v)
        return "5 이미 질문했으나 '모름'", v, r, ms, b

    def s6():
        v, b = _view(BASE + LABS + [("ecog", 1, {})], parsed, trials, stub,
                     source_text="합성 환자 기록 요약 (출처 미상)", input_source="unknown")
        r, ms = _run(v)
        return "6 문서 검증이 필요한 환자 (출처 미상 입력)", v, r, ms, b

    def s7():
        v, b = _view(BASE + LABS, parsed, trials, stub)
        r, ms = _run(v, budget=Budget(max_questions=1))
        return "7 여러 시험에 공통으로 필요한 정보 (질문 1개 예산)", v, r, ms, b

    def s8():
        v, b = _view([("age", 8, {"unit": "years"}), ("sex", "female", {})], parsed, trials, stub)
        r, ms = _run(v)
        return "8 8세 환자 (실제 기준표: 21개 나이 불충족·근거 문서 미검증, 5개 나이 조건 충족)", v, r, ms, b

    def s9():
        v, b = _view(BASE + LABS, parsed, trials, stub)
        r, ms = _run(v, SimChannel({}))  # 답은 오지만 정보가 늘지 않음
        return "9 반복 질문 방지 (정보 변화 없음)", v, r, ms, b

    def s10():
        v, b = _view(BASE, parsed, trials, stub)
        n = {"i": 0}

        def cancel_after_two():
            n["i"] += 1
            return "cancelled" if n["i"] > 2 else None
        r, ms = _run(v, stop_check=cancel_after_two)
        rb, msb = _run(_view(BASE, parsed, trials, stub)[0], budget=Budget(max_cost_units=3))
        return "10 취소(2회 뒤) / 비용 예산 3", v, (r, rb), ms + msb, b

    return [s1, s2, s3, s4, s5, s6, s7, s8, s9, s10]


def _row(name, v, r, ms, baseline_ms):
    def one(res):
        return {"stop_reason": res.stop_reason, "iterations": res.iterations,
                "actions": [f"{a.action_type}:{a.reason_code}:{a.info_key}" for a in res.actions][:8],
                "n_actions": len(res.actions), "questions": res.usage.questions, "tool_calls": res.usage.tool_calls,
                "duplicates_blocked": res.stats["duplicates_blocked"], "candidates_total": res.stats["candidates_total"],
                "reevaluated": res.stats["reevaluated"], "rule_engine_reused": res.stats["rule_engine_reused"],
                "llm_stale": res.stats["llm_stale"], "state_build_ms": res.stats["state_build_ms"],
                "decision_llm_calls": res.usage.llm_calls, "gaps": res.final_state["gaps"]}
    out = {"scenario": name, "baseline_matcher_ms": round(baseline_ms, 1), "loop_ms": round(ms, 1)}
    if isinstance(r, tuple):
        out.update(cancel=one(r[0]), budget=one(r[1]))
    else:
        out.update(one(r))
    out["baseline_unchanged"] = True
    return out


def run_all() -> list[dict]:
    parsed = load_frozen()
    all_trials = load_trials()
    trials = {t: all_trials[t] for t in parsed}
    stub = StubLLM()
    original = matcher.llm.structured
    matcher.llm.structured = stub
    rows = []
    try:
        for fn in scenarios(parsed, trials, stub):
            calls_before = stub.calls
            name, v, r, ms, b = fn()
            snapshot_before = {t: m.model_dump() for t, m in v.matches.items()}
            row = _row(name, v, r, ms, b)
            row["baseline_stub_llm_calls"] = stub.calls - calls_before
            row["baseline_unchanged"] = snapshot_before == {t: m.model_dump() for t, m in v.matches.items()}
            rows.append(row)
    finally:
        matcher.llm.structured = original
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rows = run_all()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return
    for r in rows:
        print(f"\n## {r['scenario']}  (기준선 {r['baseline_matcher_ms']} ms, 루프 {r['loop_ms']} ms, "
              f"기준선 LLM 대역 {r['baseline_stub_llm_calls']}회)")
        for part in ([r] if "stop_reason" in r else [r["cancel"], r["budget"]]):
            print(f"  종료 {part['stop_reason']} · 반복 {part['iterations']} · 질문 {part['questions']} · 도구 "
                  f"{part['tool_calls']} · 후보 {part['candidates_total']} · 중복 차단 {part['duplicates_blocked']} · "
                  f"재계산 {part['reevaluated']} · 규칙엔진 재사용 {part['rule_engine_reused']} · LLM stale "
                  f"{part['llm_stale']} · 상태 구성 {part['state_build_ms']} ms · 결정 LLM {part['decision_llm_calls']}")
            for a in part["actions"]:
                print(f"    - {a}")


if __name__ == "__main__":
    main()
