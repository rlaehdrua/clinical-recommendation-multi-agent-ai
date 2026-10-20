"""실제 LLM 파일럿 실행 전 점검 (Phase 7). API 키 값을 출력하지 않고, 실제 API 클라이언트를 만들지 않으며, 네트워크 요청을 하지 않습니다.

기존 안전장치를 다시 만들지 않고 그대로 불러 점검합니다:
승인 게이트(llm_pilot.approval_problems·authorize·run_pilot), 계획 해시(run_llm_pilot.build_plan), Fake SDK 실행
(llm_pilot_exec.execute), 프로세스 보호(call_guard), 비식별 검사(assert_redacted), 사례 제한(PILOT_CASE_POOL).

    PYTHONPATH=src CTREC_FALLBACKS=0 python scripts/pilot_preflight.py --experiment A_pipeline
    PYTHONPATH=src CTREC_FALLBACKS=0 python scripts/pilot_preflight.py --experiment B_question_plan --json

결과 표시:
- PASS: 이 점검으로 확인함 (Fake SDK·가짜 HTTP 기준)
- LOCKED: 일부러 잠가 둔 상태 (승인 없음, PAID_EXECUTION_ENABLED = False) - 지금 단계의 정상 상태
- PENDING_USER: 사용자가 확인·승인해야 함 (단가 확인, Console 지출 한도 등)
- PENDING_RUNTIME: 실제 실행 시점·환경에서만 확인 가능 (모델 사용 가능 여부, 실제 응답의 사용량 형식 등)
- FAIL: 결함 - 이 상태로는 실행하면 안 됨
FAIL이 있으면 종료 코드 2. ready_to_execute는 PASS만 있을 때 True (지금은 LOCKED·PENDING 때문에 항상 False).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))

from bench_incremental import load_frozen  # noqa: E402
from decision_scenarios import load_trials  # noqa: E402
from run_llm_pilot import build_plan, load_selected  # noqa: E402

from ctrec import call_guard, config, llm, llm_pilot as lp, llm_pilot_exec as lpx, store_patch  # noqa: E402
from ctrec.llm_pilot_fake import FakeSDKClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
# Phase 5-4에서 승인 검토용으로 제안한 상한 (이 점검은 늘리지 않음: 계획 상한이 이보다 크면 FAIL)
PHASE5_CAPS = {"A_pipeline": {"max_cost_usd": 3.0, "max_calls": 72, "run_deadline_s": 1800.0},
               "B_question_plan": {"max_cost_usd": 2.0, "max_calls": 42, "run_deadline_s": 1800.0}}
FIRST_BATCH = ["M-SYN-DEMO-A-DEMO-LUNG-001-rescue-1", "S-NCT07369063-I9-biomarker",
               "V-DEMO-LUNG-001-NCT06908096-lab_platelets-shared"]
# 구조화 출력 스키마 제한 (공식 문서 structured-outputs, 2026-10-15 확인): 요청당 합계
SCHEMA_LIMITS = {"union_params": 16, "optional_params": 24}
_UNSUPPORTED = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength",
                "maxItems", "pattern", "patternProperties", "oneOf", "not", "if", "then", "else"}


def schema_problems() -> dict:
    """파일럿·웹이 쓰는 구조화 출력 스키마가 API 제한 안인지 (오프라인). $ref는 펼쳐서 셈 (보수적)."""
    from ctrec import schemas
    from ctrec.agents import screener
    out = {}
    for cls in (schemas.PatientProfile, schemas.MatcherOutput, schemas.QuestionSet, schemas.RecommendationDraft,
                screener.ScreeningOutput):
        s = llm.strict_schema(cls)
        defs = s.get("$defs", {})
        seen: dict = {"union": 0, "optional": 0, "bad": set(), "recursive": False}

        def walk(n, stack=()):
            if isinstance(n, dict):
                if "$ref" in n:
                    name = n["$ref"].split("/")[-1]
                    if name in stack:
                        seen["recursive"] = True
                        return
                    return walk(defs[name], stack + (name,))
                seen["union"] += int("anyOf" in n) + int(isinstance(n.get("type"), list))
                seen["bad"] |= set(n) & _UNSUPPORTED
                if n.get("minItems") not in (None, 0, 1):
                    seen["bad"].add("minItems>1")
                if n.get("type") == "object" and "properties" in n:
                    if n.get("additionalProperties") is not False:
                        seen["bad"].add("additionalProperties")
                    seen["optional"] += len(set(n["properties"]) - set(n.get("required", [])))
                for k, v in n.items():
                    if k != "$defs":
                        walk(v, stack)
            elif isinstance(n, list):
                for v in n:
                    walk(v, stack)
        walk(s)
        out[cls.__name__] = {"union_params": seen["union"], "optional_params": seen["optional"],
                             "unsupported": sorted(seen["bad"]), "recursive": seen["recursive"]}
    return out


class Report:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, area: str, status: str, message: str) -> None:
        self.items.append({"area": area, "status": status, "message": message})


def _sdk_check(r: Report) -> None:
    import anthropic
    v = anthropic.__version__
    major, minor = (int(x) for x in v.split(".")[:2])
    r.add("sdk_version", "PASS" if (major, minor) >= (1, 13) and major < 2 else "FAIL",
          f"anthropic {v} (요구: >=1.13,<2 - call_guard 훅과 usage 필드를 이 범위에서 확인)")


def _network_block_check(r: Report, plan: dict) -> None:
    """승인 없이 실제 실행을 시도해도 클라이언트를 만들지 않고, 배타 보호 중에는 직접 SDK 요청이 네트워크 전에 막히는지."""
    import anthropic
    import anthropic._client
    import httpx2
    made, sent = [], []
    real = anthropic._client.Anthropic  # 공개 이름(anthropic.Anthropic)을 바꿔도 가짜 HTTP용 실제 SDK 클래스를 씀
    try:
        anthropic.Anthropic = lambda *a, **k: made.append(1) or (_ for _ in ()).throw(RuntimeError("blocked"))
        try:
            lp.run_pilot(plan, None)
            refused = False
        except lp.PaidRunNotApproved:
            refused = True
    finally:
        anthropic.Anthropic = real
    r.add("gate_blocks_before_client", "PASS" if refused and not made else "FAIL",
          "승인 없이 run_pilot 호출 -> 거절, 실제 SDK 클라이언트 생성 0회" if refused and not made
          else "승인 없이 실행이 거절되지 않았거나 클라이언트가 만들어짐")
    # 가짜 HTTP 전송을 단 실제 SDK 객체: 배타 보호(파일럿 실행 중) 안에서 표시 없이 보내면 전송 전에 막혀야 함
    sdk = real(api_key="sk-ant-test-preflight-0000", base_url="http://preflight.invalid", max_retries=0,
               http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(
                   lambda req: sent.append(1) or httpx2.Response(500))))
    meter = lp.PilotMeter(lp.PilotLimits(**plan["limits"]), lp.PriceTable(**plan["price"]))
    before = call_guard.counters()["blocked"]
    with lp.metered(meter, FakeSDKClient(), exclusive=True):
        with contextlib.suppress(Exception):
            sdk.messages.create(model=config.MODEL, max_tokens=10, messages=[{"role": "user", "content": "x"}])
    blocked = call_guard.counters()["blocked"] - before
    r.add("unmetered_sdk_blocked", "PASS" if blocked == 1 and not sent else "FAIL",
          f"파일럿 실행 중 계측 밖 SDK 요청: 차단 {blocked}건, 전송 {len(sent)}건 (실제 SDK 객체 + 가짜 HTTP)")


def _fake_run(r: Report, plan: dict, price, pairs, trials) -> dict:
    fake = FakeSDKClient()
    res = lpx.execute(plan, price, lp.PilotLimits(**plan["limits"]), fake, pairs, trials, sleep=lambda s: None)
    ok = res["status"] == "succeeded" and res["frozen_store_intact"] and res["client"] == "fake_sdk"
    r.add("fake_rehearsal", "PASS" if ok else "FAIL",
          f"Fake SDK 실행 {res['status']} (사례 {res['case_status']}, 요청 {len(fake.calls)}회, 중단 이유 {res['stop_reason']}, "
          f"고정 기준표 무결 {res['frozen_store_intact']})")
    try:
        lp.assert_redacted(res, lp.forbidden_texts(pairs))
        r.add("output_redaction", "PASS", "결과에 원문·숨긴 정답·키·개인정보 형태 없음 (Fake 응답 기준)")
    except ValueError as e:
        r.add("output_redaction", "FAIL", str(e))
    # 실패 시 중단: 사용량 누락 응답 하나로 다음 요청부터 멈추는지
    bad = FakeSDKClient(faults=lambda i, stage: "no_usage" if i == 0 else None)
    res2 = lpx.execute(plan, price, lp.PilotLimits(**plan["limits"]), bad, pairs, trials, sleep=lambda s: None)
    r.add("stop_on_failure", "PASS" if res2["stop_reason"] == "usage_missing" and len(bad.calls) <= 1 + 4 else "FAIL",
          f"첫 응답 사용량 누락 -> 중단 이유 {res2['stop_reason']}, 그 뒤 요청 {max(0, len(bad.calls) - 1)}회 (병렬 진행분만)")
    return res


def check(experiment: str, cases: list[str]) -> tuple[Report, dict]:
    r = Report()
    key_set = bool(os.getenv("ANTHROPIC_API_KEY"))
    r.add("api_key", "PASS" if key_set else "PENDING_RUNTIME",
          "ANTHROPIC_API_KEY: " + ("설정됨 (값은 표시하지 않음)" if key_set else "없음 (실제 실행 환경에서 설정)"))
    r.add("model", "PASS" if config.MODEL == lp.PILOT_MODEL else "FAIL",
          f"설정 모델 {config.MODEL}, 파일럿 고정 모델 {lp.PILOT_MODEL}")
    r.add("model_available", "PENDING_RUNTIME",
          "공식 문서 2026-10-15: claude-sonnet-5 Active(legacy), 은퇴는 2027-06-30 이후. 이 키로 실제 호출 가능한지는 미확인")
    r.add("fallbacks", "PASS" if not config.USE_FALLBACKS and os.getenv("CTREC_FALLBACKS") == "0" else "FAIL",
          "CTREC_FALLBACKS=0 필요 (대체 모델 끔)")
    _sdk_check(r)
    sp = schema_problems()
    bad = {k: v for k, v in sp.items() if v["union_params"] > SCHEMA_LIMITS["union_params"]
           or v["optional_params"] > SCHEMA_LIMITS["optional_params"] or v["unsupported"] or v["recursive"]}
    r.add("structured_output_schemas", "FAIL" if bad else "PASS",
          f"구조화 출력 스키마 제한(합집합 ≤16, 선택 ≤24, 미지원 키워드 없음): "
          + ", ".join(f"{k} 합집합 {v['union_params']}" for k, v in sp.items()) + (f" / 문제: {bad}" if bad else ""))

    parsed = load_frozen()
    all_trials = load_trials()
    trials = {t: all_trials[t] for t in parsed}
    outside = [c for c in cases if c not in lp.PILOT_CASE_POOL]
    if outside:
        r.add("synthetic_cases", "FAIL", f"승인 후보 밖 사례 {outside} (게이트가 거절함)")
        return r, {}
    pairs = load_selected(parsed, cases)
    srcs = {gt.case_id: gt.synthetic for _, gt in pairs}
    r.add("synthetic_cases", "PASS" if all(srcs.values()) else "FAIL",
          f"사례 {len(cases)}개 모두 PILOT_CASE_POOL 안, 사례 파일 synthetic=true: {all(srcs.values())}")
    caps = PHASE5_CAPS[experiment]
    args = SimpleNamespace(experiment=experiment, price_input=None, price_output=None, price_cache_write=None,
                           price_cache_read=None, price_source=None, price_confirmed=False, **caps)
    plan, price = build_plan(args, pairs, trials)
    lim = plan["limits"]
    over = {k: (lim[k], v) for k, v in caps.items() if lim[k] > v}
    r.add("limits", "FAIL" if over else "PASS",
          f"비용 ${lim['max_cost_usd']}, 호출 {lim['max_calls']}회, 전체 {lim['run_deadline_s']:.0f}초, 요청당 "
          f"{lim['call_timeout_s']:.0f}초, 재시도 호출당 {lim['max_retries_per_call']}·전체 {lim['max_total_retries']}회 "
          f"(Phase 5 제안 상한 이하)" + (f" / 초과: {over}" if over else ""))
    r.add("plan_hash", "PASS", f"계획 해시 {plan['plan_hash'][:12]}… (단가 확인 전 해시: 확인하면 바뀜, 확인 문구 미생성)")
    r.add("price", "PENDING_USER", f"문서 단가 {price.input}/{price.output}/{price.cache_write}/{price.cache_read} "
          "USD/MTok (confirmed=False): 사용자가 공식 가격표로 확인하고 --price-confirmed + 출처 필요")
    problems = lp.approval_problems(plan, None)
    r.add("approval_gate", "LOCKED", f"승인 게이트: {'; '.join(problems)}")
    r.add("paid_switch", "LOCKED" if not lp.PAID_EXECUTION_ENABLED else "FAIL",
          f"PAID_EXECUTION_ENABLED = {lp.PAID_EXECUTION_ENABLED} (승인 후 별도 코드 변경으로만 켬)")
    _network_block_check(r, plan)
    frozen = store_patch.verify_frozen()
    r.add("frozen_store", "PASS" if frozen == [] else "FAIL", f"고정 기준표 무결성 문제 {len(frozen)}건")
    _fake_run(r, plan, price, pairs, trials)
    r.add("console_spend_limit", "PENDING_USER", "Anthropic Console 워크스페이스·키 지출 한도 (코드 밖, 이 저장소에서 확인 불가)")
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=30).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True,
                                    timeout=30).stdout.strip())
        r.add("code_version", "PENDING_USER" if not dirty else "PENDING_RUNTIME",
              f"실행 커밋 {head[:12]} (승인 기록에 함께 남길 것)" + (" / 작업 트리에 미커밋 변경 있음" if dirty else ""))
    except (OSError, subprocess.SubprocessError):
        r.add("code_version", "PENDING_RUNTIME", "git 확인 불가")
    return r, {"plan_hash": plan["plan_hash"], "estimate_usd": plan["estimate_usd"], "estimate_tokens": plan["estimate_tokens"],
               "limits": lim, "cases": cases}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--experiment", choices=sorted(PHASE5_CAPS), default="A_pipeline")
    ap.add_argument("--cases", nargs="+", default=FIRST_BATCH)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    r, info = check(a.experiment, a.cases)
    counts: dict = {}
    for it in r.items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    ready = set(counts) == {"PASS"}
    out = {"experiment": a.experiment, "ready_to_execute": ready, "counts": counts, "items": r.items, "plan": info,
           "paid_calls": 0}
    lp.assert_redacted(out, [os.environ[k] for k in ("ANTHROPIC_API_KEY",) if os.getenv(k)])
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print(f"파일럿 실행 전 점검 ({a.experiment}, 사례 {len(a.cases)}개) - 유료 호출 0회")
        for it in r.items:
            print(f"  [{it['status']:<15}] {it['area']}: {it['message']}")
        print(f"합계 {counts} / ready_to_execute={ready}")
    return 2 if counts.get("FAIL") else 0


if __name__ == "__main__":
    sys.exit(main())
