"""실제 LLM 파일럿 실행기 (Phase 5-1). 승인 게이트를 지난 실제 SDK 클라이언트 또는 오프라인 Fake SDK만 받습니다.

흐름: (승인 표가 있을 때만) 실제 SDK(max_retries=0) -> MeteredClient(예약·정산·재시도 상한·취소) -> llm.client 자리
-> 실험 A(프로파일러·Matcher·재추출·재판정·추천, Decision Loop 질문 계획, 결정적 정답 시뮬레이터) 또는
실험 B(프로파일러·Matcher·기존 LLM 질문 생성기, 같은 상태에서 Decision Loop 계획과 비교, 답변 반영 없음).

결과에는 ID·코드·건수·토큰·비용만 넣고, 환자 원문·숨긴 정답·API 키·LLM 응답 문장은 넣지 않습니다(assert_redacted).
결과 파일 저장은 기본 꺼짐(save_to=None). 실제 유료 실행은 llm_pilot.run_pilot -> authorize를 지나야 하며,
PAID_EXECUTION_ENABLED = False인 동안 승인 표가 만들어지지 않으므로 이 모듈은 Fake SDK로만 돌 수 있습니다.
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from . import config, llm_pilot as lp, store_patch
from .llm_pilot_fake import FakeSDKClient

# 사례를 '중단'으로 보는 상태 코드 (meter 중단 이유 + 취소·시간 제한)
_STOP_CODES = {"cancelled", "timeout", "call_cap", "cost_cap", "input_token_cap", "output_token_cap", "retry_cap",
               "consecutive_failures", "usage_missing", "model_mismatch", "fallback_detected", "fallback_requested",
               "model_price_mismatch", "sdk_retries_enabled", "retry_exhausted"}


def _is_real_sdk(inner) -> bool:
    return type(inner).__module__.split(".")[0] == "anthropic"


def client_kind(inner, ticket, plan: dict, approval) -> str:
    """승인 표가 유효하면 실제 SDK, 아니면 오프라인 Fake SDK만 허용."""
    if ticket is not None and lp.ticket_valid(ticket, plan, approval):
        return "sdk"  # pragma: no cover - PAID_EXECUTION_ENABLED가 False라 표가 유효할 수 없음
    if isinstance(inner, FakeSDKClient) and not _is_real_sdk(inner):
        return "fake_sdk"
    raise lp.PaidRunNotApproved("승인 표 없이는 오프라인 Fake SDK로만 실행할 수 있음 (실제 클라이언트 거부)")


@contextlib.contextmanager
def live_environment(budget: int):
    """실험 설정: 보고서 없음, 질문 정책·라운드당 질문 수는 오프라인 평가와 같게, 대체 모델·즉석 파싱 끔. 끝나면 복구."""
    values = {"WRITE_REPORT": False, "QUESTION_POLICY": "batch", "MAX_QUESTIONS_PER_ROUND": budget,
              "USE_FALLBACKS": False, "LAZY_COMPILE": False}
    saved = {k: getattr(config, k) for k in values}
    for k, v in values.items():
        setattr(config, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(config, k, v)


def _outcome(exp: str, metrics: dict, stopped_now: str | None) -> str:
    status = metrics.get("status")
    if stopped_now or status in _STOP_CODES:
        return "aborted"
    if status != "ok":
        return "failed"
    if exp == "A_pipeline" and (metrics["errors"] or metrics["matcher"]["not_evaluated"]):
        return "failed"
    if exp == "B_question_plan" and not metrics.get("comparison_valid"):
        return "failed"
    return "succeeded"


def summarize_a(rows: list[dict]) -> dict:
    ms = [r["metrics"] for r in rows if r.get("metrics")]
    total = Counter()
    for m in ms:
        f, x, mt, e = m["facts"], m["extraction"], m["matcher"], m["egfr"]
        total.update(facts=f["total"], answer_facts=f["answer_facts"],
                     answer_facts_not_self_report=f["answer_facts_not_self_report"],
                     promoted_to_document_verified=f["promoted_to_document_verified"],
                     unlocated_not_unknown=f["unlocated_not_unknown"], extraction_checked=x["checked"],
                     extraction_agreed=x["agreed"], matcher_retried_rules=mt["retried_rules"],
                     matcher_missing_after_retry=mt["missing_after_retry"], matcher_not_evaluated=mt["not_evaluated"],
                     label_agreement=mt["label_agreement"], labelled_trials=mt["labelled_trials"],
                     kidney_egfr_facts=e.get("kidney_egfr_facts", 0),
                     kidney_egfr_linked_to_EGFR=e.get("kidney_egfr_linked_to_EGFR", 0),
                     kidney_egfr_in_biomarker_field=e.get("kidney_egfr_in_biomarker_field", 0))
    return {"cases": len(rows), "status": dict(Counter(r["status"] for r in rows)), **dict(total)}


def execute(plan: dict, price: lp.PriceTable, limits: lp.PilotLimits, inner, pairs, trials: dict, *,
            ticket=None, approval=None, cancel: Callable[[], bool] | None = None,
            clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
            save_to: Path | None = None) -> dict:
    """계획대로 실험 하나를 사례 순서대로 실행. 상한·중단에 걸리면 남은 사례는 건너뜀(skipped)."""
    kind = client_kind(inner, ticket, plan, approval)
    exp = plan.get("experiment")
    if exp not in lp.EXPERIMENTS:
        raise ValueError("실험은 A_pipeline 또는 B_question_plan")
    if [gt.case_id for _, gt in pairs] != list(plan.get("case_ids") or []):
        raise ValueError("계획의 사례 목록과 입력 사례가 다름")
    if price.model != plan.get("model") or plan.get("model") != config.MODEL:
        raise ValueError("계획 모델·단가 모델·설정 모델이 다름")
    budget, rounds = plan["question_budget"], plan["rounds"]
    frozen_before = store_patch.verify_frozen()
    meter = lp.PilotMeter(limits, price, cancel=cancel, clock=clock)
    run_id = uuid.uuid4().hex[:12]
    rows = []
    with live_environment(budget), lp.metered(meter, inner, sleep):
        for case, gt in pairs:
            row: dict = {"case_id": gt.case_id, "case_type": gt.case_type}
            with contextlib.suppress(lp.PilotStop):
                meter.check()  # 취소·시간 제한은 사례 사이에도 확인 (실험 코드의 guard가 먼저 잡은 경우 포함)
            if meter.stopped:
                row.update(status="skipped", stop_reason=meter.stopped)
                rows.append(row)
                continue
            before, t0 = meter.snapshot(), clock()
            stopped_before = meter.stopped
            if exp == "A_pipeline":
                m = lp.run_experiment_a(case, gt, trials, rounds, budget, cancel=cancel)
            else:
                m = lp.compare_plans(case, gt, trials, budget)
            stopped_now = meter.stopped if meter.stopped != stopped_before else (
                m.get("status") if m.get("status") in _STOP_CODES else None)
            row.update(status=_outcome(exp, m, stopped_now), detail_status=m.get("status"),
                       error_type=m.get("error_type"), stop_reason=stopped_now,
                       usage=meter.usage_since(before), elapsed_s=round(clock() - t0, 3),
                       metrics={k: v for k, v in m.items() if k not in ("case_id", "case_type")})
            rows.append(row)
    totals = meter.report()
    statuses = Counter(r["status"] for r in rows)
    result = {
        "run_id": run_id, "plan_hash": lp.plan_hash(plan), "experiment": exp, "model": plan.get("model"),
        "client": kind, "paid": kind == "sdk",
        "price": {"confirmed": price.confirmed, "source": price.source},
        "cost_kind": ("actual_usage_x_confirmed_price" if kind == "sdk" and price.confirmed
                      else "estimate_fake_usage_x_assumed_price"),
        "status": "aborted" if meter.stopped else ("succeeded" if statuses == {"succeeded": len(rows)} else "partial"),
        "stop_reason": meter.stopped, "case_status": dict(statuses), "cases": rows, "totals": totals,
        "summary": summarize_a(rows) if exp == "A_pipeline" else lp.summarize_b(
            [dict(r["metrics"], status=r["detail_status"]) for r in rows if r.get("metrics")], budget),
        "elapsed_s": totals["elapsed_s"],
        "frozen_store_intact": frozen_before == [] and store_patch.verify_frozen() == [],
        "patient_data_saved": False, "redacted": True, "limits": asdict(limits),
    }
    lp.assert_redacted(result, lp.forbidden_texts(pairs))
    if save_to is not None:  # 기본 꺼짐. 비식별 검사를 지난 결과만
        Path(save_to).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


def simulate(plan: dict, pairs, trials: dict, scenario: str = "base", **fake_kw) -> dict:
    """dry-run: 실제 실행 경로(MeteredClient·실험 코드)를 Fake SDK로 돌려 사례별 호출 수와 추정 비용 확인. 유료 호출 없음."""
    fake = FakeSDKClient(chars_per_token=lp.CHARS_PER_TOKEN[scenario], output_tokens=lp.OUTPUT_TOKENS[scenario],
                         **fake_kw)
    limits = lp.PilotLimits(max_cost_usd=1e6, max_calls=10 ** 6, max_input_tokens=10 ** 12,
                            max_output_tokens=10 ** 12, max_total_retries=10 ** 6, max_consecutive_failures=10 ** 6)
    price = lp.PriceTable(**{**asdict(lp.ASSUMED_PRICE), "model": config.MODEL})
    return execute({**plan, "model": config.MODEL}, price, limits, fake, pairs, trials, sleep=lambda s: None)
