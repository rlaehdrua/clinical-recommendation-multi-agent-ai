"""Decision Loop Inspector (Phase 10-A): 이미 기록된 실행 결과를 읽기 전용 정적 HTML로 보여 줍니다.

새 판단을 하지 않습니다. 판정·질문·변화는 모두 실행이 남긴 기록(result.json, trace.jsonl, 사실 저장소 파일)을 그대로
옮기며, 기록이 없는 항목은 '기록 없음'으로 표시합니다(질문 선정 이유·LLM 추론을 만들어 내지 않음).

입력 계약 (실행 폴더, 버전 1) - CLI 실행(`outputs/<patient_id>/`)과 같은 형식이라 실제 API 실행 결과도 그대로 연결됩니다.
- result.json         (필수) Session.result()
- trace.jsonl         (선택) Tracer 기록. 라운드별 판정·질문·단계·오류
- facts/<id>.json     (선택) FactStore.save() 형식. Session.fact_store_dir로 남김
- metering.json       (선택) 웹 run.metering 형식(WebRunMeter.summary + RunBudget.summary) - 숫자·코드만 읽음
- run_meta.json       (선택) {"llm_backend": "offline_stub" | "real" | ..., "synthetic": bool, ...} 실행 출처 표시

입력 파일은 실행 폴더 안의 실제 파일만 읽고(링크로 밖을 가리키면 거부), 출력 HTML은 새 파일로만 만듭니다(덮어쓰지 않음).

개인정보 원칙 (허용 목록): 환자 원문·인용(quote/evidence)·LLM 추론(reasoning)·자유 서술(missing_info, 추림 사유)·
예외 원문·토큰·키는 view에 넣지 않습니다. 기본(strict)은 환자 ID를 해시로 바꾸고 사실 값·질문 문구·답변을 뺍니다.
synthetic_details=True는 run_meta.json에 synthetic: true가 있는 합성 실행에서만 허용되며 사실 값·질문 문구·답변을 보입니다.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config

SCHEMA = "ctrec.inspector/v1"
DECLINED = {"모름", "확인 불가", "모름 · 확인 불가"}


class InspectorError(ValueError):
    pass


@dataclass
class RunRecord:
    """Inspector 입력. 파일(load_run)이나 메모리의 Session(record_from_session)에서 만듦."""
    result: dict
    trace: list[dict] | None = None
    facts: list[dict] | None = None
    metering: dict | None = None
    meta: dict | None = None
    issues: list[str] | None = None  # 읽는 중 발견한 형식 문제 (예: trace의 깨진 줄). 원문은 넣지 않음
    # 사실 기록의 출처: "file" = 실행 폴더 facts/ 파일 (run_meta 기록과 대조), "session" = 메모리 Session (이번 실행 그 자체)
    facts_origin: str | None = None
    meta_status: str | None = None  # "absent" / "ok" / "corrupt" (None이면 meta 유무로 판단)


# ---------------------------------------------------------------------------
# 입력
# ---------------------------------------------------------------------------

def _read_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError) as e:  # 손상된 파일: 내용은 오류 메시지에 넣지 않음
        raise InspectorError(f"{p.name}: JSON으로 읽을 수 없음 ({type(e).__name__})") from None


# 사실 파일 이름으로 쓰는 환자 ID: FactStore와 같은 규칙 (경로 구분자·'..'·절대경로가 들어갈 수 없음)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")


def _inside(run_dir: Path, *parts: str) -> Path | None:
    """실행 폴더 안의 입력 파일. 없으면 None. 링크 등으로 실제 위치가 실행 폴더 밖이면 거부."""
    p = run_dir.joinpath(*parts)
    if not p.exists():
        return None
    real = p.resolve()
    if not real.is_relative_to(run_dir) or not real.is_file():
        raise InspectorError(f"실행 폴더 밖이거나 파일이 아닌 입력은 읽지 않음: {'/'.join(parts)}")
    return real


def _optional(run_dir: Path, issues: list[str], *parts: str) -> Path | None:
    """선택 입력 파일: 실행 폴더 밖을 가리키거나(링크·정션) 파일이 아니면 읽지 않고 형식 문제로 남김 (실행 전체는 계속 표시)."""
    try:
        return _inside(run_dir, *parts)
    except InspectorError:
        issues.append(f"{'/'.join(parts[:1]) if parts[0] == 'facts' else parts[0]}: 실행 폴더 밖을 가리키거나 파일이 아님 - 읽지 않음")
        return None


def _optional_json(path: Path | None, name: str, issues: list[str]) -> tuple[Any, bool]:
    """반환: (내용 또는 None, 손상 여부). 손상된 선택 파일은 읽지 않고 형식 문제로 남김."""
    if path is None:
        return None, False
    try:
        return _read_json(path), False
    except InspectorError:
        issues.append(f"{name}: JSON으로 읽을 수 없음 - 읽지 않음")
        return None, True


def load_run(run_dir: Path) -> RunRecord:
    run_dir = Path(run_dir).resolve()
    rp = _inside(run_dir, "result.json")  # 필수 입력: 밖을 가리키거나 손상되면 실행 전체를 거부
    if rp is None:
        raise InspectorError(f"result.json이 없음: {run_dir}")
    result = _read_json(rp)
    trace, issues = None, []
    tp = _optional(run_dir, issues, "trace.jsonl")
    if tp is not None:
        trace, bad = [], 0
        for ln in tp.read_text(encoding="utf-8", errors="replace").splitlines():
            if ln.strip():
                try:
                    trace.append(json.loads(ln))
                except ValueError:
                    bad += 1
        if bad:  # 실행 중단 등으로 잘린 줄: 버리고 형식 문제로 남김
            issues.append(f"trace.jsonl: JSON으로 읽을 수 없는 줄 {bad}건 - 읽지 않음")
    pid = result.get("patient_id")
    if not isinstance(pid, str) or not _SAFE_ID.fullmatch(pid) or ".." in pid:
        raise InspectorError("result.json의 patient_id가 파일 이름으로 쓸 수 없는 형식")
    facts = None
    data, _ = _optional_json(_optional(run_dir, issues, "facts", f"{pid}.json"), "facts/", issues)
    if data is not None:
        facts = data.get("facts", []) if isinstance(data, dict) else None
        if facts is None:
            issues.append("facts/: 객체가 아님 - 읽지 않음")
    metering, _ = _optional_json(_optional(run_dir, issues, "metering.json"), "metering.json", issues)
    xp = _optional(run_dir, issues, "run_meta.json")
    meta, corrupt = _optional_json(xp, "run_meta.json", issues)
    meta_status = "corrupt" if corrupt or (xp is None and (run_dir / "run_meta.json").exists()) else         ("ok" if meta is not None else "absent")
    return RunRecord(result=result, trace=trace, facts=facts, metering=metering, meta=meta, issues=issues,
                     facts_origin="file" if facts is not None else None, meta_status=meta_status)


def record_from_session(session, trace: list[dict] | None = None, metering: dict | None = None,
                        meta: dict | None = None) -> RunRecord:
    """메모리의 Session에서 바로 (나중에 웹·파일럿 실행 결과를 연결할 때). 사실은 Session.fact_store에서 읽음."""
    facts = [f.model_dump() for f in session.fact_store.facts(session.case.patient_id)]
    return RunRecord(result=session.result(), trace=trace, facts=facts, metering=metering, meta=meta,
                     facts_origin="session")


# ---------------------------------------------------------------------------
# 정제 (허용 목록)
# ---------------------------------------------------------------------------

_ERR = re.compile(r"\b([A-Z][A-Za-z0-9_]*(?:Error|Exception|Cancelled|Blocked|Pending|Stop|Timeout|Refusal))\b")


def error_type(text: Any) -> str:
    """예외 원문에서 종류 이름만 (원문·메시지는 버림)."""
    m = _ERR.search(str(text or ""))
    return m.group(1) if m else "Error"


def _stage(text: Any) -> str | None:
    s = str(text or "")
    for stage in ("기준 파싱 실패", "적격성 판정 실패"):
        if s.startswith(stage):
            return stage
    return None


def _num(d: dict | None, keys) -> dict:
    d = d or {}
    return {k: d[k] for k in keys if isinstance(d.get(k), (int, float)) and not isinstance(d.get(k), bool)}


def _code(v: Any) -> str | None:
    """짧은 코드 값만 (자유 문장은 버림)."""
    return v if isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_.:\-]{1,64}", v) else None


# 계약 판정 출처의 식별자 형식 (propositions._KEY, evaluator 출처의 '<계약 ID>@v<버전>', IR digest 앞 12자리)
_PROP_KEY = re.compile(r"[a-z0-9_]{1,48}(?:\([a-z0-9_.]{1,32}\))?/(?:event|condition|measurement)/[a-z0-9_\-]{1,32}")
_CONTRACT_REF = re.compile(r"[A-Za-z0-9_.\-]{1,64}@v\d{1,6}")
_DIGEST12 = re.compile(r"[0-9a-f]{12}")


def _ident(v: Any, pattern: re.Pattern | None = None) -> str | None:
    """식별자 하나: 형식이 맞으면 그대로, 아니면 '<text>' (없으면 None). 명제 키·계약 참조는 '/'·'@'가 있어 _code와 따로 봄."""
    if v is None:
        return None
    ok = isinstance(v, str) and (pattern.fullmatch(v) if pattern else _code(v))
    return v if ok else "<text>"


def _idents(v: Any, name: str, anomalies: list[str], pattern: re.Pattern | None = None) -> list[str] | None:
    """식별자 목록. 목록이 아니면 읽지 않고 형식 문제로 남김."""
    if v is None:
        return None
    if not isinstance(v, list):
        anomalies.append(f"{name}: 목록이 아님 ({type(v).__name__}) - 읽지 않음")
        return None
    return [_ident(x, pattern) for x in v]


# 기록 출처별 필드 (키 -> 화면 이름). 실제 정의: web_metering.WebRunMeter.summary, web.budget.RunBudget.summary,
# decision_state.BudgetUsage. 숫자·참거짓·짧은 코드만 옮기며 자유 문장(note·cost_note)과 중첩 객체는 읽지 않음
METER_FIELDS = {
    "mode": "계측 모드", "status": "계측 상태", "status_reason": "상태 사유", "calls": "호출 수(실패 포함)",
    "input_tokens": "입력 토큰", "output_tokens": "출력 토큰", "cache_write_tokens": "캐시 쓰기 토큰",
    "cache_read_tokens": "캐시 읽기 토큰", "thinking_tokens": "사고 토큰(출력에 포함)", "retries": "재시도",
    "usage_missing": "사용량 누락 응답", "maybe_billed_failures": "청구됐을 수 있는 실패",
    "unattributed_calls_during_run": "계측 밖 호출", "usage_confirmed": "사용량 확정", "requested_model": "요청 모델",
    "cost_estimable": "비용 산정 가능", "cost_usd": "비용(USD)", "cost_cap_enforced": "비용 상한 적용",
    "elapsed_s": "경과(초)",
}
BUDGET_FIELDS = {
    "budget_mode": "예산 모드", "budget_status": "예산 상태", "cap_enforced": "상한 적용",
    "estimated_cost_usd": "추정 합계(USD)", "confirmed_cost_usd": "확정(USD)", "reserved_cost_usd": "예약 중(USD)",
    "unknown_cost_usd": "사용량 미확인분(USD)", "unknown_requests": "사용량 미확인 요청", "calls_used": "사용한 호출",
    "calls_remaining": "남은 호출", "blocked_unmetered_calls": "차단된 미계측 호출", "last_block_reason": "마지막 차단 사유",
    "price_source": "단가 출처",
}
LOOP_USAGE_FIELDS = {
    "iterations": "반복", "questions": "질문 계획 수", "tool_calls": "도구 계획 수", "llm_calls": "LLM 호출(루프 내부)",
    "cost_units": "추상 비용 단위(USD 아님)", "seconds": "계획 시간(초)",
}


def _pick(d: dict | None, fields: dict) -> dict:
    """기록된 키만 (없는 키는 0으로 채우지 않음). 값은 숫자·참거짓·코드 문자열만."""
    out = {}
    for k in fields:
        v = (d or {}).get(k)
        if isinstance(v, (bool, int, float)) or _code(v):
            out[k] = v
    return out


_CRITERION_KEYS = ("rule_id", "kind", "criterion_text", "category", "method", "holds", "passes", "applicable",
                   "cohort", "underspecified", "needs_verification", "self_reported_holds", "evidence_source",
                   "required_evidence", "gap", "evidence_directness", "validation_flags", "llm_judgment")


# 라운드 사이 변화로 표시하는 기록 필드. 판정(verdict)과 근거·확인 상태(evidence)를 나눠 셈
VERDICT_KEYS = ("passes", "holds")
EVIDENCE_KEYS = ("self_reported_holds", "needs_verification", "evidence_source", "gap")
_CHANGE_KEYS = VERDICT_KEYS + EVIDENCE_KEYS
# 근거 직접성 진단 (schemas.CriterionAssessment: matcher._enforce_directness·qa_guard·evaluator가 남긴 값).
# 판정·근거 상태 변화와 섞지 않고 따로 셈. 이전 형식 기록에는 없음 (없으면 '기록 없음', 'none'과 다름)
DIRECTNESS_KEYS = ("evidence_directness", "validation_flags", "llm_judgment")
_LLM_JUDGMENT_KEYS = ("holds", "applicable", "evidence_type", "confidence")  # matcher가 남기는 키만 (그 밖의 키는 읽지 않음)
_TRIAL_KEYS = ("eligibility", "match_status")


def _scalar(v: Any) -> Any:
    """비교·표시용 기록 값: 참거짓·숫자·None·코드만 (자유 문장은 '<text>'로 가림)."""
    return v if v is None or isinstance(v, (bool, int, float)) or _code(v) else "<text>"


def _directness_value(k: str, v: Any) -> Any:
    """직접성 진단 필드의 표시·비교 값: 코드·참거짓만 (자유 문장·형식이 다른 값은 '<text>'). 근거·인용 문장은 읽지 않음."""
    if v is None:
        return None
    if k == "evidence_directness":
        return _scalar(v)
    if k == "validation_flags":
        return [_ident(x) for x in v] if isinstance(v, list) else "<text>"
    if not isinstance(v, dict):  # llm_judgment
        return "<text>"
    return {j: _scalar(v[j]) for j in _LLM_JUDGMENT_KEYS if j in v}


def _round(v: Any) -> int | None:
    """기록된 라운드 번호. 0 이상의 정수만 (없거나 형식이 다르면 None - 추정하지 않음)."""
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _dicts(v: Any, name: str, anomalies: list[str]) -> list[dict]:
    """목록 기록에서 객체만. 형식이 다르면 버리고 형식 문제로 남김 (원문은 넣지 않음)."""
    if v is None:
        return []
    if not isinstance(v, list):
        anomalies.append(f"{name}: 목록이 아님 ({type(v).__name__}) - 읽지 않음")
        return []
    bad = sum(not isinstance(x, dict) for x in v)
    if bad:
        anomalies.append(f"{name}: 객체가 아닌 항목 {bad}건 - 읽지 않음")
    return [x for x in v if isinstance(x, dict)]


def _answer_status(q: dict | None) -> str:
    """qa_log 답변 상태. 값이 없거나 비면 '답변함'으로 세지 않음."""
    if q is None:
        return "미기록"
    a = q.get("answer")
    if a is None:
        return "답변 값 없음"
    if not isinstance(a, str):
        return "형식 오류"
    if not a.strip():
        return "빈 답변"
    return "모름·확인 불가" if a.strip() in DECLINED else "답변함"


def _pair_changes(tid: str, prev: dict, cur: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """한 시험의 연속된 두 평가 기록 비교 -> (시험 변화, 기준 변화, 비교 불가). 한쪽에만 있는 기준·필드는 비교 불가."""
    span = {"from_round": prev["round"], "to_round": cur["round"], "same_round": prev["round"] == cur["round"]}
    a, b = prev["rec"], cur["rec"]
    trial, crit, inc = [], [], []
    for k in _TRIAL_KEYS:
        if k in a and k in b and a[k] != b[k]:
            trial.append({"trial_id": tid, "field": k, "before": _scalar(a[k]), "after": _scalar(b[k]), **span})
        elif (k in a) != (k in b):
            inc.append({"trial_id": tid, "rule_id": None, "field": k, "reason": "필드가 한쪽 기록에만 있음", **span})
    before = {x.get("rule_id"): x for x in prev["assessments"] if _code(x.get("rule_id"))}
    after = {x.get("rule_id"): x for x in cur["assessments"] if _code(x.get("rule_id"))}
    for rid in sorted(set(before) - set(after)):
        inc.append({"trial_id": tid, "rule_id": rid, "field": None, "reason": "다음 기록에 기준 없음", **span})
    for rid in sorted(set(after) - set(before)):
        inc.append({"trial_id": tid, "rule_id": rid, "field": None, "reason": "이 기록에 새로 등장", **span})
    for rid in sorted(set(before) & set(after)):
        x, y = before[rid], after[rid]
        for k in _CHANGE_KEYS:
            if k in x and k in y:
                if x[k] != y[k]:
                    crit.append({"trial_id": tid, "rule_id": rid, "kind": _code(y.get("kind")), "field": k,
                                 "group": "verdict" if k in VERDICT_KEYS else "evidence",
                                 "before": _scalar(x[k]), "after": _scalar(y[k]),
                                 "passes_after": y.get("passes") if "passes" in y else "<missing>", **span})
            elif (k in x) != (k in y):
                inc.append({"trial_id": tid, "rule_id": rid, "field": k, "reason": "필드가 한쪽 기록에만 있음", **span})
        for k in DIRECTNESS_KEYS:  # 양쪽 다 없으면(이전 형식·trace에 미기록) 비교하지 않음 - 변화 없음으로 세지 않고 따로 알림
            if k in x and k in y:
                p, q = _directness_value(k, x[k]), _directness_value(k, y[k])
                if p != q:
                    crit.append({"trial_id": tid, "rule_id": rid, "kind": _code(y.get("kind")), "field": k,
                                 "group": "directness", "before": p, "after": q,
                                 "passes_after": y.get("passes") if "passes" in y else "<missing>", **span})
            elif (k in x) != (k in y):
                inc.append({"trial_id": tid, "rule_id": rid, "field": k, "reason": "필드가 한쪽 기록에만 있음", **span})
    return trial, crit, inc


def _criterion(a: dict) -> dict:
    c = {k: a.get(k) for k in _CRITERION_KEYS if k in a}
    for k in DIRECTNESS_KEYS:
        if k in c:
            c[k] = _directness_value(k, c[k])
    return c


def _trial_titles(ids) -> dict[str, str]:
    """공개 시험 제목 (data/trials, 읽기만)."""
    out = {}
    for tid in ids:
        p = config.TRIALS_DIR / f"{tid}.json"
        if re.fullmatch(r"[A-Za-z0-9_\-]+", tid) and p.is_file():
            try:
                rec = _read_json(p)
                out[tid] = rec.get("title") or (rec.get("protocolSection", {}).get("identificationModule", {})
                                                 .get("briefTitle"))
            except (OSError, ValueError):
                pass
    return {k: v for k, v in out.items() if isinstance(v, str)}


ELIGIBILITY_VALUES = {"ELIGIBLE", "INELIGIBLE", "UNCERTAIN", "NOT_EVALUATED"}
FACT_STORE_VALUES = (None, "run_dir", "external")  # 백엔드 CLI run_meta.records.fact_store 계약


def _fact_provenance(rec: RunRecord, meta_status: str, anomalies: list[str], gaps: list[str]) -> tuple[bool, dict]:
    """사실 기록을 이번 실행의 사실로 표시할지 판단. 반환: (표시 여부, {status, recorded}).

    status: verified(run_meta 기록·파일 존재 일치) / unverified(이전 형식: records 기록 없음 - 표시하되 미검증으로 표시) /
            hidden(출처 불일치·확인 불가 - 표시하지 않음) / none(사실 기록 없음)"""
    present = rec.facts is not None
    if rec.facts_origin == "session":
        return present, {"status": "verified" if present else "none", "recorded": "session"}
    if meta_status == "corrupt" or (rec.meta is not None and not isinstance(rec.meta, dict)):
        if present:
            anomalies.append("run_meta.json을 읽을 수 없어 facts/ 파일의 출처를 확인할 수 없음 - 사실을 표시하지 않음")
            return False, {"status": "hidden", "recorded": "unreadable"}
        return False, {"status": "none", "recorded": "unreadable"}
    meta = rec.meta if isinstance(rec.meta, dict) else None
    if meta is None or "records" not in meta:  # 이전 형식 (run_meta 없음, 또는 records 항목 없음)
        if present:
            gaps.append("사실 출처 미검증: run_meta.json에 records 기록이 없는 이전 형식 - 이번 실행의 사실인지 확인할 수 없음")
            return True, {"status": "unverified", "recorded": "absent"}
        gaps.append("사실 저장소 파일(facts/) 없음: 사실 목록 대신 병행 기록 건수만 표시")
        return False, {"status": "none", "recorded": "absent"}
    records = meta.get("records")
    fs = records.get("fact_store", ...) if isinstance(records, dict) else ...
    if fs is ... or fs not in FACT_STORE_VALUES:
        anomalies.append("run_meta.records.fact_store 형식이 계약과 다름: 사실 출처를 확인할 수 없음"
                         + (" - 사실을 표시하지 않음" if present else ""))
        return False, {"status": "hidden" if present else "none", "recorded": "invalid"}
    if fs == "run_dir":
        if present:
            return True, {"status": "verified", "recorded": "run_dir"}
        anomalies.append("run_meta는 실행 폴더에 사실을 저장했다고 기록했으나 facts/ 파일을 읽지 못함 (없거나 거부됨)")
        return False, {"status": "none", "recorded": "run_dir"}
    if fs == "external":
        gaps.append("사실은 외부 경로에 저장됨(run_meta): Inspector는 실행 폴더 밖을 읽지 않음")
        if present:
            anomalies.append("run_meta는 외부 경로 저장인데 실행 폴더에 facts/ 파일이 있음 - 이전 실행 기록일 수 있어 표시하지 않음")
            return False, {"status": "hidden", "recorded": "external"}
        return False, {"status": "none", "recorded": "external"}
    # fs is None: 이번 실행은 사실을 저장하지 않음
    if present:
        anomalies.append("run_meta는 사실 저장 안 함(fact_store=null)인데 실행 폴더에 facts/ 파일이 있음 - 이전 실행 기록일 수 있어 "
                         "표시하지 않음")
        return False, {"status": "hidden", "recorded": None}
    gaps.append("이번 실행은 사실을 저장하지 않음(run_meta.records.fact_store=null): 병행 기록 건수만 표시")
    return False, {"status": "none", "recorded": None}


def _meta_view(meta: dict) -> dict:
    """run_meta 허용 필드 (백엔드 CLI 계약). 설정값과 관찰 기록을 나눠 옮기며 자유 문장·경로는 읽지 않음."""
    called = meta.get("called_models")
    rid = meta.get("run_id")
    if rid is None and isinstance(meta.get("records"), dict):  # 문서상 records.run_id로 적힌 경우도 허용
        rid = meta["records"].get("run_id")
    obs, n = meta.get("llm_calls_observed"), meta.get("llm_responses_with_usage")
    return {
        "format_version": meta.get("format_version") if isinstance(meta.get("format_version"), int) else None,
        "written_by": _code(meta.get("written_by")), "run_id": _code(rid),
        "mode": _code(meta.get("mode")), "candidates": _code(meta.get("candidates")),
        "requested_model": _code(meta.get("requested_model")),
        # 확인된 호출 모델만 목록으로. "UNKNOWN"·누락은 None (호출 모델로 표시하지 않음)
        "called_models": [c for c in called if _code(c) and c != "UNKNOWN"] or None if isinstance(called, list)
        else (called if _code(called) and called != "UNKNOWN" else None),
        "called_models_recorded": "called_models" in meta,
        "llm_calls_observed": obs if isinstance(obs, bool) else None,  # null = 확인 못 함 (호출 없음이 아님)
        "llm_responses_with_usage": n if isinstance(n, int) and not isinstance(n, bool) else None,
        "has_call_fields": any(k in meta for k in ("llm_calls_observed", "llm_responses_with_usage", "called_models")),
    }


def build_view(rec: RunRecord, *, synthetic_details: bool = False) -> dict:
    anomalies: list[str] = list(rec.issues or [])  # 기록 간 불일치·형식 문제 (gaps는 기록 누락)
    if not isinstance(rec.result, dict):
        raise InspectorError("result.json의 최상위가 객체가 아님")
    if rec.meta is not None and not isinstance(rec.meta, dict):
        anomalies.append("run_meta.json: 객체가 아님 - 읽지 않음")
    meta = rec.meta if isinstance(rec.meta, dict) else {}
    if synthetic_details and meta.get("synthetic") is not True:
        raise InspectorError("synthetic_details는 run_meta.json에 synthetic: true가 있는 합성 실행에서만 허용")
    r = rec.result
    trace = _dicts(rec.trace, "trace.jsonl", anomalies)
    pid = str(r.get("patient_id") or "")
    gaps: list[str] = []

    # --- 실행 정보
    backend = _code(meta.get("llm_backend")) or "unknown"
    run = {
        "patient": pid if synthetic_details else "patient-" + hashlib.sha256(pid.encode()).hexdigest()[:8],
        "model": _code(r.get("model")), "input_source": _code(r.get("input_source")),
        "llm_backend": backend, "synthetic": meta.get("synthetic") is True,
        "case_id": _code(meta.get("case_id")) if synthetic_details else None,
        "mode": "synthetic_details" if synthetic_details else "strict",
    }
    meta_status = rec.meta_status or ("ok" if rec.meta is not None else "absent")
    if meta_status == "absent":
        gaps.append("run_meta.json 없음: 실제 API 실행인지 대역 실행인지 기록되지 않음")
    elif meta_status == "corrupt":
        anomalies.append("run_meta.json을 읽을 수 없음: 실행 출처·사실 저장 여부를 확인할 수 없음")
    run["meta"] = _meta_view(meta) if isinstance(rec.meta, dict) else None

    # --- 사실: 출처가 확인된 이번 실행의 사실만 표시 (run_meta.records.fact_store와 실제 파일을 교차 확인)
    show_facts, provenance = _fact_provenance(rec, meta_status, anomalies, gaps)
    run["fact_provenance"] = provenance
    facts = None
    if show_facts:
        facts = []
        for f in _dicts(rec.facts, "facts/", anomalies):
            ref = f.get("source_reference") if isinstance(f.get("source_reference"), dict) else {}
            hist = f.get("verification_history")
            row = {"field": _code(f.get("field")) or ("<text>" if f.get("field") is not None else None),
                   "unit": _scalar(f.get("unit")), "source_type": _code(f.get("source_type")),
                   "verification_status": _code(f.get("verification_status")),
                   "conflict_status": _code(f.get("conflict_status")), "version": _round(f.get("version")),
                   "source_kind": _code(ref.get("kind")), "source_ref": _code(ref.get("ref")),
                   "verification_events": len(hist) if isinstance(hist, list) else None}
            if synthetic_details:
                row["value"] = f.get("value")
            facts.append(row)
    fact_shadow = [{"round": _round(e.get("round")), "status": _code(e.get("status")),
                    **_num(e, ("added", "duplicates", "skipped", "rejected")),
                    "error_type": _code(e.get("error_type"))} for e in _dicts(r.get("fact_shadow"), "fact_shadow", anomalies)]

    # --- 시험 평가 기록: trace의 matcher done을 시험별로 trace 순서대로 (라운드 번호가 없으면 배정하지 않음)
    # ContractSession의 contract applied(판정 포함)는 같은 라운드 matcher done 뒤의 경계·평가기 적용 결과 -> 그 라운드의
    # 판정 기록을 대신함 (새 라운드로 세지 않음). 판정이 없는 contract applied(이전 형식)는 읽지 않음
    evals: dict[str, list[dict]] = {}
    no_round = bad_tid = 0
    for t in trace:
        if t.get("agent") == "contract" and t.get("event") == "applied" and isinstance(t.get("assessments"), list):
            tid, rnd = t.get("trial_id"), _round(t.get("round"))
            prev = evals.get(tid) if _code(tid) else None
            if prev and prev[-1]["round"] == rnd and not prev[-1].get("contract_applied"):
                prev[-1].update(rec=t, assessments=[x for x in t["assessments"] if isinstance(x, dict)],
                                contract_applied=True)
            else:
                anomalies.append(f"{_code(tid) or '<text>'}: 계약 적용 기록 앞에 같은 라운드 판정 기록이 없음 - 읽지 않음")
            continue
        if t.get("agent") != "matcher" or t.get("event") != "done":
            continue
        tid, rnd = t.get("trial_id"), _round(t.get("round"))
        if not _code(tid):
            bad_tid += 1
            continue
        if rnd is None:
            no_round += 1
            continue
        rows = t.get("assessments")
        evals.setdefault(tid, []).append({"round": rnd, "rec": t, "assessments": [x for x in rows if isinstance(x, dict)]
                                          if isinstance(rows, list) else []})
    if no_round:
        anomalies.append(f"라운드 번호가 없거나 형식이 다른 판정 기록 {no_round}건: 라운드에 배정하지 않음")
    if bad_tid:
        anomalies.append(f"시험 ID 형식이 다른 판정 기록 {bad_tid}건: 읽지 않음")
    for tid, ev in evals.items():
        if any(b["round"] < a["round"] for a, b in zip(ev, ev[1:])):
            anomalies.append(f"{tid}: trace 순서와 라운드 번호 순서가 맞지 않음 (trace 순서로 비교)")
        dup = len(ev) - len({e["round"] for e in ev})
        if dup:
            gaps.append(f"{tid}: 같은 라운드 재평가 기록 {dup}건 - trace 순서로 비교 (평가 차수 ID는 기록되지 않음)")
    if not trace:
        gaps.append("trace.jsonl 없음: 라운드별 기준 판정·질문·실행 단계를 볼 수 없음 (최종 판정만)")
    elif not evals:
        gaps.append("trace에 판정 기록(matcher done)이 없음: 라운드별 판정을 볼 수 없음")

    matches_raw = r.get("matches")
    if matches_raw is not None and not isinstance(matches_raw, dict):
        anomalies.append(f"matches: 객체가 아님 ({type(matches_raw).__name__}) - 읽지 않음")
        matches_raw = {}
    matches = {k: v for k, v in (matches_raw or {}).items() if isinstance(v, dict) and _code(k)}
    if len(matches) != len(matches_raw or {}):
        anomalies.append(f"matches: 형식이 다른 항목 {len(matches_raw) - len(matches)}건 - 읽지 않음")
    titles = _trial_titles(list(matches))
    trials, all_changes, trial_changes, incomparable, pairs = [], [], [], [], []
    for tid in list(matches) + [t for t in evals if t not in matches]:
        ev = evals.get(tid, [])
        for p, n in zip(ev, ev[1:]):
            tc, cc, ic = _pair_changes(tid, p, n)
            trial_changes += tc
            all_changes += cc
            incomparable += ic
            pairs.append({"trial_id": tid, "from_round": p["round"], "to_round": n["round"]})
        if tid not in matches:
            anomalies.append(f"{tid}: trace에는 판정 기록이 있으나 result.json matches에 없음")
            continue
        m = matches[tid]
        elig = m.get("eligibility")
        if elig is not None and elig not in ELIGIBILITY_VALUES:
            anomalies.append(f"{_code(tid)}: 예상하지 못한 판정 값")
        assess = _dicts(m.get("assessments"), f"{tid}.assessments", anomalies)
        if ev:  # 최종 판정 정합성: result.json과 trace 마지막 기록 (trace 순서)
            last = ev[-1]
            if "eligibility" in last["rec"] and last["rec"].get("eligibility") != elig:
                anomalies.append(f"{tid}: 최종 판정 불일치 - result.json {_scalar(elig)} / trace 마지막 기록(라운드 "
                                 f"{last['round']}) {_scalar(last['rec'].get('eligibility'))}")
            lastc = {x.get("rule_id"): x for x in last["assessments"]}
            diff = sorted(a.get("rule_id") for a in assess if _code(a.get("rule_id")) and a.get("rule_id") in lastc
                          and any(k in a and k in lastc[a["rule_id"]] and a[k] != lastc[a["rule_id"]][k]
                                  for k in VERDICT_KEYS))
            if diff:
                anomalies.append(f"{tid}: 기준 판정이 result.json과 trace 마지막 기록에서 다름 {diff}")
        elif trace:
            gaps.append(f"{tid}: trace에 판정 기록 없음 (최종 판정만)")
        crit_changes = [c for c in all_changes if c["trial_id"] == tid]
        criteria = []
        for a in assess:
            c = _criterion(a)
            c["changes"] = [x for x in crit_changes if x["rule_id"] == a.get("rule_id")]
            if "gap" not in c:  # result.json에는 해소 경로가 없음: 이 기준이 마지막으로 기록된 평가의 값만 (없으면 비움)
                last = next((e for e in reversed(ev) for x in e["assessments"] if x.get("rule_id") == a.get("rule_id")),
                            None)
                x = next((x for x in last["assessments"] if x.get("rule_id") == a.get("rule_id")), {}) if last else {}
                c["gap"] = _code(x.get("gap"))
                c["gap_round"] = last["round"] if last and "gap" in x else None
            criteria.append(c)
        err = m.get("evaluation_error")
        trials.append({
            "trial_id": tid, "title": titles.get(tid), "eligibility": _scalar(elig),
            "match_status": _scalar(m.get("match_status")), **_num(m, ("n_pass", "n_fail", "n_unknown")),
            "matched_cohort": _code(m.get("matched_cohort")),
            "error": {"stage": _stage(err), "type": error_type(err)} if err else None,
            "rounds": [{"round": e["round"], "eligibility": _scalar(e["rec"].get("eligibility")),
                        "match_status": _scalar(e["rec"].get("match_status")),
                        **_num(e["rec"], ("n_pass", "n_fail", "n_unknown"))} for e in ev],
            "criteria": criteria,
        })

    sc = r.get("screening")
    if sc is not None and not isinstance(sc, dict):
        anomalies.append("screening: 객체가 아님 - 읽지 않음")
        sc = None
    screening = None if not sc else {
        "pool_size": sc.get("pool_size") if isinstance(sc.get("pool_size"), int) else None,
        "selected": [_code(s.get("trial_id")) for s in _dicts(sc.get("selected"), "screening.selected", anomalies)],
        "excluded": [_code(s.get("trial_id")) for s in _dicts(sc.get("excluded"), "screening.excluded", anomalies)],
        "over_limit": [_code(s.get("trial_id")) for s in _dicts(sc.get("over_limit"), "screening.over_limit", anomalies)],
        "kept_by_default": [x for x in sc.get("kept_by_default") or [] if _code(x)]
        if isinstance(sc.get("kept_by_default"), list) else [],
        "hard_filtered": [{"trial_id": _code(h.get("trial_id")), "kind": _code(h.get("kind"))}
                          for h in _dicts(sc.get("hard_filtered"), "screening.hard_filtered", anomalies)],
    }

    # --- 질문·답변: trace 생성 기록과 qa_log를 (라운드, 질문 ID)로 맞춤. 한쪽에만 있으면 그대로 표시하고 불일치로 남김
    qa_raw = r.get("qa_log")
    qa_log = None if qa_raw is None else _dicts(qa_raw, "qa_log", anomalies)
    if qa_raw is not None and not isinstance(qa_raw, list):
        qa_log = None
    if qa_log is None:
        gaps.append("result.json에 qa_log 없음: 질문·답변 기록을 알 수 없음")
    qa_by_key = {(_round(q.get("round")), q.get("question_id")): q for q in qa_log or []}
    asked: list[dict] = []
    gen_by_round: dict[int, int] = {}
    gen_events = 0
    for t in trace:
        if t.get("agent") == "question_generator" and t.get("event") == "questions":
            gen_events += 1
            rnd = _round(t.get("round"))
            qs = _dicts(t.get("questions"), "trace 질문 기록", anomalies)
            if rnd is None:
                anomalies.append(f"라운드 번호가 없는 질문 생성 기록 {len(qs)}건: 라운드에 배정하지 않음")
            else:
                gen_by_round[rnd] = gen_by_round.get(rnd, 0) + len(qs)
            asked += [{**q, "round": rnd, "_src": "trace"} for q in qs]
    trace_keys = {(q["round"], q.get("question_id")) for q in asked}
    qa_only = [{**q, "round": _round(q.get("round")), "_src": "qa_log"} for q in qa_log or []
               if (_round(q.get("round")), q.get("question_id")) not in trace_keys]
    if trace and qa_log is not None:
        t_only = [k for k in trace_keys if k not in qa_by_key]
        if t_only or (qa_only and gen_events):
            anomalies.append(f"질문 기록 불일치: trace에만 {len(t_only)}건, qa_log에만 {len(qa_only)}건 "
                             "(라운드·질문 ID 기준, 연결하지 않고 각각 표시)")
        elif qa_only and not gen_events:
            gaps.append("trace에 질문 생성 기록이 없음: 질문은 qa_log 기록만 표시")
    diags_raw = r.get("decision_diagnostic")
    diags = None if diags_raw is None or not isinstance(diags_raw, list) else _dicts(diags_raw, "decision_diagnostic", anomalies)
    if diags_raw is not None and diags is None:
        anomalies.append("decision_diagnostic: 목록이 아님 - 읽지 않음")
    questions = []
    for q in asked + qa_only:
        rnd, qid = q["round"], q.get("question_id")
        targets = [k for k in q.get("target") or [] if _code(k)] if isinstance(q.get("target"), list) else []
        a = qa_by_key.get((rnd, qid)) if q["_src"] == "trace" else q
        source = ("trace+qa_log" if a is not None else "trace만") if q["_src"] == "trace" else \
            ("qa_log만" if trace else "qa_log")
        row = {"round": rnd, "question_id": _code(qid), "purpose": _code(q.get("purpose")),
               "answer_type": _code(q.get("answer_type")), "targets": targets,
               "answer_status": _answer_status(a), "source": source}
        plan = None
        if diags is not None and rnd is not None:  # 이 질문 직전 진단(라운드 rnd-1)에서 대상 기준이 겹치는 ASK_USER 계획
            prev = [d for d in diags if _round(d.get("round")) == rnd - 1 and d.get("status") == "ok"]
            if prev:
                plan = [{"info_key": _code(x.get("info_key")), "tier": _scalar(x.get("tier")),
                         "priority": _scalar(x.get("priority")), "value_score": _scalar(x.get("value_score")),
                         "reason_code": _code(x.get("reason_code"))}
                        for d in prev for x in _dicts(d.get("actions"), "decision actions", anomalies)
                        if x.get("action_type") == "ASK_USER" and isinstance(x.get("target_criterion_ids"), list)
                        and set(x["target_criterion_ids"]) & set(targets)]
        row["overlapping_plan"] = plan
        if synthetic_details:
            row["question"] = q.get("question")
            row["why"] = q.get("why")
            row["answer"] = a.get("answer") if a else None
        questions.append(row)
    if diags is None:
        gaps.append("Decision Loop 진단 기록 없음(decision_loop=off): 질문 후보의 우선순위·가치 점수를 볼 수 없음")
    if questions:
        gaps.append("질문 생성기가 어떤 계획을 따랐는지의 직접 연결 기록은 없음: 대상 기준이 겹치는 계획만 표시")
        gaps.append("qa_log의 '모름'은 사용자가 모름이라고 답한 경우와 답변이 없어 기본값이 들어간 경우를 구분하지 않음")

    decision = None if diags is None else [{
        "round": _round(d.get("round")), "status": _code(d.get("status")), "stop_reason": _code(d.get("stop_reason")),
        "error_type": _code(d.get("error_type")),
        "usage": _pick(d.get("usage") if isinstance(d.get("usage"), dict) else {}, LOOP_USAGE_FIELDS),
        "actions": [{"action_type": _code(x.get("action_type")), "reason_code": _code(x.get("reason_code")),
                     "reason": x.get("human_readable_reason") if isinstance(x.get("human_readable_reason"), str) else None,
                     "tier": _scalar(x.get("tier")), "priority": _scalar(x.get("priority")),
                     "value_score": _scalar(x.get("value_score")), "estimated_cost": _scalar(x.get("estimated_cost")),
                     "targets": [k for k in x.get("target_criterion_ids") or [] if _code(k)]
                     if isinstance(x.get("target_criterion_ids"), list) else [],
                     "status": _code(x.get("status"))} for x in _dicts(d.get("actions"), "decision actions", [])],
    } for d in diags]

    # --- 판정 변화 (match_history): 그 시점 Session의 시험별 판정 (재평가 여부와 무관)
    history = [{"step": _code(str(h.get("step", "")).replace("(", ":").replace(")", "").replace("=", "")),
                "eligibility": {k: _scalar(x) for k, x in h["eligibility"].items() if _code(k)}
                if isinstance(h.get("eligibility"), dict) else {}}
               for h in _dicts(r.get("eligibility_history"), "eligibility_history", anomalies)]
    dep_raw = r.get("dependency_diagnostic")
    dep = None if dep_raw is None or not isinstance(dep_raw, list) else _dicts(dep_raw, "dependency_diagnostic", anomalies)
    if dep_raw is not None and dep is None:
        anomalies.append("dependency_diagnostic: 목록이 아님 - 읽지 않음")

    def _chg(d):
        return d.get("changes") if isinstance(d.get("changes"), dict) else {}
    fact_changes = None if dep is None else [{
        "round": _round(d.get("round")), "status": _code(d.get("status")), "mode": _code(d.get("mode")),
        "changed_fields": [f for f in _chg(d).get("changed_fields") or [] if _code(f)]
        if isinstance(_chg(d).get("changed_fields"), list) else [],
        "has_changes": "changes" in d,
        "version_before": _code(_chg(d).get("version_before")), "version_after": _code(_chg(d).get("version_after")),
    } for d in dep]
    if dep is None:
        gaps.append("의존 그래프 진단 기록 없음: 라운드별 사실 변화 요약 없음")

    # --- 단계·오류
    steps = [{"t": t.get("t") if isinstance(t.get("t"), (int, float)) else None,
              "agent": _code(t.get("agent")), "event": _code(t.get("event")), "round": _round(t.get("round")),
              "trial_id": _code(t.get("trial_id")), "eligibility": _code(t.get("eligibility")),
              "stop_reason": _code(t.get("stop_reason")),
              "error_type": error_type(t.get("message")) if t.get("event") == "error" else None}
             for t in trace]
    errors = None if "errors" not in r else \
        [{"item": _code(e.get("item")), "type": error_type(e.get("error"))} for e in _dicts(r.get("errors"), "errors", anomalies)]
    if errors is None:
        gaps.append("result.json에 errors 항목 없음: 실행 오류 여부를 알 수 없음")

    # --- 호출량·비용 (기록된 값만, 계산하지 않음)
    usage_raw = r.get("usage")
    if usage_raw is not None and not isinstance(usage_raw, dict):
        anomalies.append("usage: 객체가 아님 - 읽지 않음")
    usage = _num(usage_raw if isinstance(usage_raw, dict) else {},
                 ("calls", "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    if "usage" not in r:
        gaps.append("result.json에 usage 항목 없음: API 호출량이 기록되지 않음 (0과 다름)")
    elif not usage:
        gaps.append("result.json의 usage가 비어 있음: API 호출량이 기록되지 않음 (0으로 기록된 것과 다름)"
                    + (" · run_meta.json에는 오프라인 대역 실행으로 기록됨" if backend == "offline_stub" else ""))
    metering = None
    if rec.metering is not None and not isinstance(rec.metering, dict):
        anomalies.append("metering.json: 객체가 아님 - 읽지 않음")
    elif rec.metering:
        # metering.json = 웹 run.metering: WebRunMeter.summary() + _budget_fields() (enforce면 RunBudget.summary()).
        # 두 출처를 따로 두고 합산하지 않음. 기록에 없는 키는 빼고(0으로 채우지 않음), 'UNKNOWN'은 그대로
        metering = {"web": _pick(rec.metering, METER_FIELDS), "budget": _pick(rec.metering, BUDGET_FIELDS)}
        if not any(k.endswith("_cost_usd") for k in metering["budget"]):
            gaps.append(f"metering.json에 USD 비용 값 없음 (cost_usd={metering['web'].get('cost_usd', '기록 없음')}, "
                        "예산 원장 금액은 enforce 모드에서만 기록됨)")
    if rec.metering is None:
        gaps.append("metering.json 없음: USD 비용 기록 없음 (Inspector는 비용을 추정하지 않음)")

    rec_ = r.get("recommendation") if isinstance(r.get("recommendation"), dict) else {}
    recommendation = {k: [_code(x.get("trial_id")) for x in _dicts(rec_.get(k), f"recommendation.{k}", anomalies)]
                      for k in ("recommended", "ranked", "excluded")} if rec_ else None

    # --- Decision Loop 타임라인 (라운드 = 판정 기록의 round. 기록끼리 묶기만 하고 새로 계산하지 않음)
    eval_rounds = sorted({e["round"] for ev in evals.values() for e in ev})
    timeline = None
    if trace:
        timeline = []
        for rnd in eval_rounds:
            qs = None if qa_log is None else [q for q in qa_log if _round(q.get("round")) == rnd]
            st = [_answer_status(q) for q in qs or []]
            deps = [d for d in fact_changes or [] if d["round"] == rnd]
            shadows = [e for e in fact_shadow if e["round"] == rnd]
            dgs = [d for d in decision or [] if d["round"] == rnd]
            into = [c for c in all_changes if c["to_round"] == rnd]
            inc = [c for c in incomparable if c["to_round"] == rnd]
            timeline.append({
                "round": rnd,
                # 이 라운드의 시험별 마지막 판정 기록 (trace 순서). 기록이 없으면 None
                "trials": {tid: next((_code(e["rec"].get("eligibility")) for e in reversed(ev) if e["round"] == rnd), None)
                           for tid, ev in evals.items()},
                "trial_evals": {tid: [_code(e["rec"].get("eligibility")) for e in ev if e["round"] == rnd]
                                for tid, ev in evals.items()},
                "questions_generated": gen_by_round.get(rnd) if trace else None,
                "qa": None if qs is None else {
                    "count": len(qs), "answered": st.count("답변함"), "declined": st.count("모름·확인 불가"),
                    "missing": len(st) - st.count("답변함") - st.count("모름·확인 불가")},
                # 진단 기록이 정확히 1건일 때만 그 값 (여러 건이면 목록으로만, 합치지 않음)
                "fact_changed_fields": deps[0]["changed_fields"] if len(deps) == 1 else None,
                "fact_change_entries": deps,
                "fact_shadow": {k: shadows[0].get(k) for k in ("status", "added", "duplicates")} if len(shadows) == 1 else None,
                "fact_shadow_entries": len(shadows),
                "verdict_changes": sorted({f"{c['trial_id']}:{c['rule_id']}" for c in into if c["group"] == "verdict"}),
                "evidence_changes": sorted({f"{c['trial_id']}:{c['rule_id']}" for c in into if c["group"] == "evidence"}),
                "directness_changes": sorted({f"{c['trial_id']}:{c['rule_id']}" for c in into if c["group"] == "directness"}),
                "incomparable": sorted({f"{c['trial_id']}:{c['rule_id'] or '(시험)'}" for c in inc}),
                "comparisons": [p for p in pairs if p["to_round"] == rnd],
                "trial_changes": [c for c in trial_changes if c["to_round"] == rnd],
                "compared_with": max((x for x in eval_rounds if x < rnd), default=None),
                "decision": {"status": dgs[0]["status"], "stop_reason": dgs[0]["stop_reason"]} if len(dgs) == 1 else None,
                "decisions": [{"status": d["status"], "stop_reason": d["stop_reason"]} for d in dgs],
            })
    unlinked_q = [] if qa_log is None or not trace else \
        sorted({_round(q.get("round")) for q in qa_log if _round(q.get("round")) not in eval_rounds}, key=str)

    # --- 주요 변화 요약 (연속된 평가 기록끼리 비교한 계산값)
    stable = []
    for tid, ev in evals.items():
        seq = [_code(e["rec"].get("eligibility")) for e in ev]
        if len(seq) > 1 and len(set(seq)) == 1 and seq[0] is not None:
            stable.append({"trial_id": tid, "eligibility": seq[0], "rounds": [e["round"] for e in ev]})

    def n_crit(rows):
        return len({(c["trial_id"], c["rule_id"]) for c in rows})
    verdict = [c for c in all_changes if c["group"] == "verdict"]
    evidence = [c for c in all_changes if c["group"] == "evidence"]
    directness = [c for c in all_changes if c["group"] == "directness"]
    # 직접성 필드가 trace 판정 기록에 있는가 (pipeline.trace_match가 싣지 않으면 라운드 사이 비교 자체가 불가능)
    directness_in_trace = any(k in x for ev in evals.values() for e in ev for x in e["assessments"] for k in DIRECTNESS_KEYS)
    directness_in_result = any(k in c for t in trials for c in t["criteria"] for k in DIRECTNESS_KEYS)
    if pairs and not directness_in_trace:
        gaps.append("trace 판정 기록에 근거 직접성 필드(evidence_directness·validation_flags·llm_judgment)가 없음: "
                    "라운드 사이 직접성 변화는 비교할 수 없음 (변화 없음이 아님) - 최종 값(result.json)만 표시")
    if trials and not directness_in_result:
        gaps.append("result.json 기준 판정에 근거 직접성 필드 없음 (이전 형식 결과): 직접성 진단을 표시하지 않음")
    changes = {"trial": trial_changes, "verdict": verdict, "evidence": evidence, "directness": directness,
               "incomparable": incomparable, "stable_trials": stable, "comparable": bool(pairs),
               "directness_comparable": bool(pairs) and directness_in_trace,
               "n_criteria": {"verdict": n_crit(verdict), "evidence": n_crit(evidence), "directness": n_crit(directness),
                              "incomparable": n_crit([c for c in incomparable if c["rule_id"]])}}
    # 최종 직접성 분포 (result.json 기준 판정의 기록 값 그대로 셈. 필드가 없는 기준은 따로)
    final_dir: dict[str, int] = {}
    for t in trials:
        for c in t["criteria"]:
            d = c.get("evidence_directness", "<missing>")
            final_dir[str(d)] = final_dir.get(str(d), 0) + 1

    # --- 실행 개요
    if not rec.meta or not isinstance(rec.meta, dict):
        kind = "확인 불가"
    elif meta.get("synthetic") is True:
        kind = "합성"
    elif meta.get("synthetic") is False and backend == "real":
        kind = "실제"
    else:
        kind = "확인 불가"
    recs = meta.get("records") if isinstance(meta.get("records"), dict) else {}
    for key, present in (("decision_diagnostic", diags is not None), ("dependency_diagnostic", dep is not None)):
        if isinstance(recs.get(key), bool) and recs[key] != present:
            anomalies.append(f"run_meta.records.{key}={str(recs[key]).lower()}인데 result.json에는 "
                             f"{'있음' if present else '없음'}")
    sources = [("result.json", True), ("trace.jsonl", bool(trace)), ("사실 저장소(facts/)", facts is not None),
               ("Decision Loop 진단", diags is not None), ("의존 그래프 진단", dep is not None),
               ("호출량(usage)", bool(usage)), ("계측(metering.json)", rec.metering is not None),
               ("실행 출처(run_meta.json)", rec.meta is not None)]
    overview = {
        "run_kind": kind, "llm_backend": backend, "model_config": run["model"],
        "n_candidates": len(matches), "pool_size": (screening or {}).get("pool_size"),
        "n_rounds": len(eval_rounds) if evals else None, "rounds": eval_rounds if evals else None,
        "n_questions": None if qa_log is None else len(qa_log),
        "final": [{"trial_id": t["trial_id"], "eligibility": t["eligibility"], "match_status": t["match_status"]}
                  for t in trials],
        "n_errors": None if errors is None else len(errors),
        "n_not_evaluated": sum(t["eligibility"] == "NOT_EVALUATED" for t in trials),
        "n_anomalies": len(anomalies),
        "sources": [{"name": n, "present": p,
                     # 파일은 있지만 출처 불일치로 표시하지 않은 사실 기록은 '없음'이 아니라 따로 표시
                     "note": "있음 · 출처 불일치로 표시 안 함" if n.startswith("사실 저장소") and provenance["status"] == "hidden"
                     else ("있음 · 출처 미검증" if n.startswith("사실 저장소") and provenance["status"] == "unverified" else None)}
                    for n, p in sources],
        "n_changes": {k: len(changes[k]) for k in ("trial", "verdict", "evidence", "incomparable")}
        if changes["comparable"] else None,
        # 직접성 변화는 판정·근거 상태 변화 수치(n_changes)와 따로: None = 비교할 기록 없음 (0건과 다름)
        "n_directness_changes": len(directness) if changes["directness_comparable"] else None,
        "directness_final": dict(sorted(final_dir.items())) if directness_in_result else None,
    }

    # 계약 판정 출처 (ContractSession result["contract"]): 기준 ID·명제 키·계약·질문·사실 ID·IR 버전·출처·평가 방법.
    # 없으면 None (기존 Session 기록과 호환: v1 스키마에 선택 필드로 추가)
    contract = r.get("contract") if isinstance(r.get("contract"), dict) else None
    prov = contract.get("provenance") if contract is not None else None
    if prov is not None and not isinstance(prov, dict):
        anomalies.append(f"contract.provenance: 객체가 아님 ({type(prov).__name__}) - 읽지 않음")
        prov = None
    # 식별자만 형식 검사 후 옮김 (P9: 명제 키·'계약@v버전'이 _scalar에서 '<text>'로 가려지던 문제, 목록 필드 무검사 문제)
    contract_view = None if contract is None else {
        "session_type": _scalar(contract.get("session_type")), "profile_source": _scalar(contract.get("profile_source")),
        "criteria": [
            {"criterion_id": _ident(k),
             "proposition_keys": _idents(p.get("propositions"), "contract.provenance.propositions", anomalies, _PROP_KEY),
             "contract_ids": _idents(p.get("contracts"), "contract.provenance.contracts", anomalies, _CONTRACT_REF),
             "question_ids": _idents(p.get("questions"), "contract.provenance.questions", anomalies),
             "fact_ids": _idents(p.get("fact_ids"), "contract.provenance.fact_ids", anomalies),
             "ir_version": _scalar(p.get("ir_version")),
             "ir_digest": _ident(p["ir_digest"][:12] if isinstance(p.get("ir_digest"), str) else p.get("ir_digest"),
                                 _DIGEST12),
             "completeness": _scalar(p.get("completeness")),
             "sources": _idents(p.get("sources"), "contract.provenance.sources", anomalies),
             "method": _scalar(p.get("method")), "outcome": _scalar(p.get("outcome")), "round": _round(p.get("round"))}
            for k, p in sorted((prov or {}).items(), key=lambda kv: str(kv[0])) if isinstance(p, dict)],
        # 명제 사실의 출처 (어느 계약·버전·라운드 질문의 답에서 왔는가). 명제 값은 환자 답이므로 답변과 같은 원칙:
        # strict에서는 빼고 synthetic_details에서만 보임
        "facts": [{**{k: _scalar(f.get(k)) for k in ("fact_id", "contract_id", "contract_version",
                                                     "question", "as_of", "source", "superseded")},
                   "proposition_key": _ident(f.get("proposition_key"), _PROP_KEY),
                   "value": _scalar(f.get("value")) if synthetic_details else None}
                  for f in _dicts(contract.get("facts"), "contract.facts", anomalies)],
    }
    overview["n_anomalies"] = len(anomalies)  # 계약 기록의 형식 문제까지 포함 (위 overview는 그 전에 셈)
    return {"schema": SCHEMA, "contract": contract_view,
            "run": run, "overview": overview, "timeline": timeline, "unlinked_question_rounds": unlinked_q,
            "changes": changes, "facts": facts, "fact_shadow": fact_shadow, "screening": screening,
            "trials": trials, "questions": questions, "decision": decision, "history": history,
            "fact_changes": fact_changes, "steps": steps, "errors": errors or [], "usage": usage, "metering": metering,
            "recommendation": recommendation, "gaps": gaps, "anomalies": anomalies}


# ---------------------------------------------------------------------------
# HTML (스크립트 없음, 모든 값 이스케이프)
# ---------------------------------------------------------------------------

def _e(v: Any) -> str:
    if v is None:
        return '<span class="na">–</span>'
    if isinstance(v, bool):
        return "예" if v else "아니요"
    if isinstance(v, (list, tuple)):
        return ", ".join(_e(x) for x in v) if v else '<span class="na">–</span>'
    if isinstance(v, float):
        return f"{v:.3f}".rstrip("0").rstrip(".")  # 표시만 반올림 (view 값은 기록 그대로)
    return html.escape(str(v))


def _table(headers: list[str], rows: list[list[Any]], raw_cols: set[int] = frozenset()) -> str:
    if not rows:
        return '<p class="na">기록 없음</p>'
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c if i in raw_cols else _e(c)}</td>" for i, c in enumerate(r)) + "</tr>"
                   for r in rows)
    return f'<div class="tw"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _badge(v: Any) -> str:
    s = str(v or "")
    cls = {"ELIGIBLE": "ok", "CRITERIA_MATCH": "ok", "INELIGIBLE": "bad", "CRITERIA_MISMATCH": "bad",
           "NOT_EVALUATED": "err", "True": "ok", "False": "bad"}.get(s, "mid")
    return f'<span class="b {cls}">{html.escape(s) if s else "–"}</span>'


def _passes(p: Any) -> str:
    if p is None:
        return '<span class="b mid">판정 불가</span>'
    return f'<span class="b {"ok" if p else "bad"}">{"충족" if p else "불충족"}</span>'


_ELIG = {"ELIGIBLE": "적격", "INELIGIBLE": "부적격", "UNCERTAIN": "판정 보류", "NOT_EVALUATED": "평가 실패"}
_GAP = {"ask": "확인 질문", "verify": "문서 확인", "physician": "의사 확인"}
_FIELD = {"passes": "기준 판정", "holds": "해당 여부", "self_reported_holds": "답변상 해당 여부",
          "needs_verification": "문서 확인 필요", "evidence_source": "근거 출처", "gap": "해소 경로",
          "eligibility": "시험 판정", "match_status": "외부 상태",
          "evidence_directness": "근거 직접성", "validation_flags": "코드 검증 사유", "llm_judgment": "코드 검증 전 LLM 판정"}
# schemas.EvidenceDirectness 값의 읽는 이름 (기록 값은 코드 그대로 병기). 목록에 없는 값은 이름 없이 코드만
_DIRECTNESS = {"direct": "직접 근거", "structured": "구조화 값(규칙 엔진)", "contract": "계약 평가기", "none": "확정 판정 없음",
               "unchecked": "직접성 검사 대상 밖", "hedged": "추측·불확실 표현", "indirect": "간접 추론",
               "conflicting": "기록 상충", "question_only": "질문 문장에만 있음", "unlocated": "인용 위치 확인 불가",
               "unlinked": "답변-질문 연결 확인 불가", "object_mismatch": "대상 불일치", "time_unverified": "기간 확인 불가"}
_STOP = {"awaiting_user_answers": "사용자 답변 대기", "awaiting_external_input": "외부 입력(문서 등) 대기"}
CHANGED, SAME, NONE, NA = "▲ 변경", "＝ 변화 없음", "○ 기록 없음", "△ 비교 대상 없음"
NARRATIVE_FIELDS = {"other_fact"}
_BACKEND = {"offline_stub": "오프라인 대역 · 실제 모델 판정 아님", "real": "실제 API 실행으로 기록됨", "unknown": "기록 없음",
            "cli_sdk_path": "CLI의 SDK 경로 · 실제 호출 여부를 뜻하지 않음 (아래 호출 기록으로 판단)"}
# 내부 상태 verified = run_meta.json의 자기 선언(records.fact_store)과 실제 파일 존재가 맞는다는 뜻일 뿐,
# 파일이 그 실행에서 만들어졌다는 것을 독립적으로 증명하지는 않음 (표시 문구도 그 범위로 한정)
_PROVENANCE = {"verified": "실행 메타데이터와 일치 (run_meta.json의 기록과 파일 존재가 맞음 · 파일 생성 출처를 독립적으로 증명한 것은 아님)",
               "unverified": "출처 미검증: 이전 형식 기록 (run_meta에 records 없음)",
               "hidden": "출처 불일치·확인 불가로 표시하지 않음 (D6 참고)", "none": "사실 기록 없음"}


def _calls_card(m: dict | None) -> str:
    if m is None or not m["has_call_fields"]:
        return f'{_mark(NONE)} run_meta에 호출 기록 항목 없음'
    if m["llm_calls_observed"] is True:
        s = f'호출 관찰됨 · 사용량 기록 응답 {_e(m["llm_responses_with_usage"])}건'
    else:
        s = '확인 못 함 <span class="mut">(사용량 기록 없음 — 0회라는 뜻 아님)</span>'
    called = m["called_models"]
    s += "<br>실제 호출 모델: " + (f'<code>{_e(called)}</code>' if called else '<span class="mut">기록 없음 (UNKNOWN)</span>')
    return s


def _criteria_change_badge(criteria: list[dict]) -> str:
    """시험 요약 줄: 바뀐 기준 수를 기준 판정 / 근거·확인 상태로 나눠 표시 (같은 기준은 한 번만 셈)."""
    parts = []
    for group, label in (("verdict", "기준 판정이 변경된 기준"), ("evidence", "근거·확인 상태가 변경된 기준"),
                         ("directness", "근거 직접성 진단이 변경된 기준")):
        n = sum(any(x["group"] == group for x in c["changes"]) for c in criteria)
        if n:
            parts.append(f"{_mark(CHANGED)} {label} {n}개")
    return (" " + " · ".join(parts)) if parts else ""  # 표시 구분만 (서술형 사실을 해석·구조화하지 않음)


def _val(field: str, v: Any) -> str:
    """기록 값 + 사람이 읽을 이름 (코드는 그대로 병기)."""
    if v == "<missing>":
        return '<span class="na">기록 없음</span>'
    if v == "<text>":
        return '<span class="na">(서술 값, 표시 안 함)</span>'
    if field == "passes":
        return _passes(v)
    if field in ("eligibility",) and isinstance(v, str):
        return f'{_badge(v)} <span class="mut">{html.escape(_ELIG.get(v, ""))}</span>'
    if field == "gap" and isinstance(v, str):
        return f'<code>{html.escape(v)}</code> <span class="mut">{html.escape(_GAP.get(v, ""))}</span>'
    if field == "evidence_directness" and isinstance(v, str):
        return f'<code>{html.escape(v)}</code> <span class="mut">{html.escape(_DIRECTNESS.get(v, ""))}</span>'
    if field == "validation_flags" and isinstance(v, list):
        return " ".join(f"<code>{html.escape(x)}</code>" for x in v) if v else '<span class="mut">없음</span>'
    if field == "llm_judgment" and isinstance(v, dict):
        return " · ".join(f'{html.escape(k)}=<code>{_e(x)}</code>' for k, x in v.items()) or '<span class="na">–</span>'
    return _e(v)


def _directness_cell(c: dict, field: str) -> str:
    """기준 상세 표의 직접성 칸. 필드가 없는 이전 형식 기록은 '기록 없음' (값 none/빈 목록과 구분)."""
    if field not in c:
        return '<span class="na">기록 없음</span>'
    s = _val(field, c[field])
    j = c.get("llm_judgment") if field == "llm_judgment" else None
    # 기록된 두 값(LLM 원래 해당 여부, 최종 해당 여부)이 다르면 표시만 함 - 다시 판정하지 않음
    if isinstance(j, dict) and "holds" in j and "holds" in c and j["holds"] != _scalar(c["holds"]):
        s += f' <span class="chg">▲ 최종 해당 여부 <code>{_e(_scalar(c["holds"]))}</code>와 다름</span>'
    return s


def _elig(v: Any) -> str:
    return _val("eligibility", v) if v else f'<span class="na">{NONE}</span>'


def _mark(text: str) -> str:
    cls = {CHANGED: "chg", SAME: "same", NONE: "na", NA: "na"}.get(text, "mut")
    return f'<span class="{cls}">{html.escape(text)}</span>'


_CSS = """
:root{--bg:#fbfaf8;--fg:#1d1d1f;--mut:#6b6b70;--line:#e3e1dc;--card:#fff;--ok:#1f7a4d;--okb:#e5f4ec;--bad:#a8322d;
--badb:#fbe9e7;--mid:#7a5b00;--midb:#fbf3d9;--err:#5b3fa0;--errb:#efe9fb;--acc:#2f5fa7;--accb:#e8eef8}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#17171a;--fg:#ececef;--mut:#a0a0a8;--line:#33333a;
--card:#1f1f23;--ok:#7fd3a6;--okb:#173326;--bad:#f2a49e;--badb:#3a1d1b;--mid:#efd27a;--midb:#352c12;--err:#c6b2f5;
--errb:#2a2140;--acc:#8fb4ee;--accb:#1c2738}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,
"Segoe UI","Malgun Gothic",sans-serif}main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:18px;margin:36px 0 8px;padding-top:10px;border-top:1px solid var(--line)}
h3{font-size:15px;margin:18px 0 6px}.mut,.na{color:var(--mut)}.warn{background:var(--midb);color:var(--mid);
padding:8px 12px;border-radius:6px;font-weight:600}.note{color:var(--mut);font-size:13px;margin:4px 0 8px}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px}
.kv div{background:var(--card);border:1px solid var(--line);border-radius:6px;padding:8px 10px;min-width:0}
.kv b{display:block;font-size:12px;color:var(--mut);font-weight:500}.kv .big{font-size:18px;font-weight:600}
.src{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0;padding:0;list-style:none}.src li{border:1px solid var(--line);
border-radius:12px;padding:1px 8px;font-size:12px;background:var(--card)}
.tw{overflow-x:auto;max-width:100%}table{border-collapse:collapse;width:100%;background:var(--card);font-size:13px}
th,td{border:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top;overflow-wrap:break-word;
word-break:keep-all}
th{background:var(--bg);font-weight:600}details{background:var(--card);border:1px solid var(--line);border-radius:6px;
margin:8px 0;padding:6px 10px}summary{cursor:pointer;font-weight:600}.b{display:inline-block;padding:0 6px;
border-radius:4px;font-size:12px;background:var(--midb);color:var(--mid)}.b.ok{background:var(--okb);color:var(--ok)}
.b.bad{background:var(--badb);color:var(--bad)}.b.err{background:var(--errb);color:var(--err)}
.chg{color:var(--acc);font-weight:700}.same{color:var(--ok)}ul{margin:4px 0;padding-left:20px}code{font-size:12px}
ol.tl{list-style:none;margin:8px 0;padding:0;border-left:3px solid var(--line)}
ol.tl>li{position:relative;margin:0 0 12px 14px;background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:10px 12px}ol.tl>li:before{content:"";position:absolute;left:-23px;top:14px;width:12px;height:12px;
border-radius:50%;background:var(--acc)}ol.tl h3{margin:0 0 6px}
dl.st{display:grid;grid-template-columns:minmax(120px,190px) 1fr;gap:4px 12px;margin:0}dl.st dt{color:var(--mut)}
dl.st dd{margin:0;min-width:0;overflow-wrap:break-word}.hl{background:var(--accb);border-radius:6px;padding:8px 12px}
@media (max-width:560px){dl.st{grid-template-columns:1fr}dl.st dd{margin-bottom:6px}ol.tl>li{margin-left:10px}}
"""


def _kv_table(d: dict, labels: dict) -> str:
    return _table(["항목", "기록 키", "값"], [[labels[k], k, v] for k, v in d.items()])


def _gap(c: dict) -> str:
    if c.get("gap") is None:
        return _e(None)
    rnd = f' <span class="mut">(라운드 {_e(c["gap_round"])} 기록)</span>' if c.get("gap_round") is not None else ""
    return _val("gap", c["gap"]) + rnd


def _change_rows(rows: list[dict], with_verdict: bool = False) -> str:
    hdr = ["시험", "기준", "항목", "이전 값", "이후 값", "비교 라운드"]
    if with_verdict:
        hdr.append("이후 라운드의 기준 판정(기록)")
    out = []
    for c in rows:
        row = [_e(c["trial_id"]), _e(c.get("rule_id")) if "rule_id" in c else _e(None),
               f'{html.escape(_FIELD.get(c["field"], c["field"]))} <code>{html.escape(c["field"])}</code>',
               _val(c["field"], c["before"]), _val(c["field"], c["after"]),
               f'{_mark(CHANGED)} {_span(c)}']
        if with_verdict:
            row.append(_val("passes", c["passes_after"]))
        out.append(row)
    return _table(hdr, out, raw_cols=set(range(len(hdr))))


def _span(c: dict) -> str:
    if c.get("same_round"):
        return f'라운드 {_e(c["from_round"])} 안 재평가'
    return f'라운드 {_e(c["from_round"])} → {_e(c["to_round"])}'


def _directness_card(o: dict) -> str:
    n, dist = o["n_directness_changes"], o["directness_final"]
    s = (f'라운드 사이 변경 {n}건' if n is not None
         else f'{_mark(NONE)} 라운드 사이 비교 기록 없음 <span class="mut">(0건이라는 뜻 아님)</span>')
    if dist is None:
        return s + f'<br>{_mark(NONE)} 최종 값 기록 없음 <span class="mut">(이전 형식 결과)</span>'
    return s + "<br>최종: " + " · ".join(
        f'<code>{html.escape(k)}</code> {c}' if k != "<missing>" else f'기록 없음 {c}' for k, c in dist.items())


def _overview(v: dict) -> str:
    o, run = v["overview"], v["run"]
    out = []
    if o["run_kind"] == "합성" and o["llm_backend"] == "offline_stub":
        out.append('<p class="warn">⚠ 오프라인 합성 실행이며 실제 AI 판정 결과가 아닙니다.</p>')
    elif o["run_kind"] == "합성":
        out.append(f'<p class="warn">⚠ 합성 데이터 실행입니다 (기록된 백엔드: {_e(o["llm_backend"])}).</p>')
    elif o["run_kind"] == "확인 불가":
        out.append('<p class="warn">⚠ 실행 출처를 확인할 수 없습니다 (run_meta.json이 없거나 실제·합성 여부가 기록되지 않음).</p>')
    if o["n_anomalies"]:
        out.append(f'<p class="warn">⚠ 기록 간 불일치·형식 문제 {o["n_anomalies"]}건 — 아래 값은 기록 그대로이며 서로 맞지 않는 부분이 '
                   '있습니다. 목록은 D6에 있습니다.</p>')
    if o["n_errors"] or o["n_not_evaluated"]:
        out.append(f'<p class="warn">⚠ 실행 오류 {_e(o["n_errors"])}건 · 평가 실패(NOT_EVALUATED) 시험 '
                   f'{o["n_not_evaluated"]}개가 기록된 실행입니다.</p>')
    if run["mode"] == "synthetic_details":
        out.append('<p class="warn">합성 데이터 상세 표시: 사실 값·질문 문구·답변을 포함합니다. 실제 환자 실행에는 쓸 수 없습니다.</p>')
    final = " ".join(f'{_e(f["trial_id"])} {_elig(f["eligibility"])}' for f in o["final"]) or _e(None)
    nch, ncr = o["n_changes"], v["changes"]["n_criteria"]
    ch = (f'시험 판정 {nch["trial"]}건 · 기준 판정 {nch["verdict"]}건 (기준 {ncr["verdict"]}개) · '
          f'근거·확인 상태 {nch["evidence"]}건 (기준 {ncr["evidence"]}개)'
          + (f' · 비교 불가 {nch["incomparable"]}건' if nch["incomparable"] else "") if nch is not None
          else html.escape(NA + " (연속된 평가 기록이 없음)"))
    backend = o["llm_backend"]
    err = (_mark(NONE) if o["n_errors"] is None else
           f'오류 {o["n_errors"]}건 · 평가 실패 {o["n_not_evaluated"]}개')
    cards = [
        ("실행 백엔드 (run_meta 기록)", f'<span class="big"><code>{_e(backend)}</code></span><br>'
         f'<span class="mut">{html.escape(_BACKEND.get(backend, "기록된 값 그대로"))}</span>'),
        ("실행 유형", f'<span class="big">{html.escape(o["run_kind"])}</span>'
         + ('<br><span class="mut">synthetic=null: 합성·실제 여부 미기록 (실제 환자 실행이라는 뜻 아님)</span>'
            if isinstance(run.get("meta"), dict) and o["run_kind"] == "확인 불가" else "")),
        ("모델 호출 기록 (run_meta)", _calls_card(run.get("meta"))),
        ("후보 임상시험 수", f'<span class="big">{_e(o["n_candidates"])}</span>'
         + (f' <span class="mut">(추림 풀 {_e(o["pool_size"])})</span>' if o["pool_size"] is not None else "")),
        ("평가 라운드 수", (f'<span class="big">{_e(o["n_rounds"])}</span> <span class="mut">(라운드 {_e(o["rounds"])})</span>'
                        if o["n_rounds"] is not None else _mark(NONE))),
        ("기록된 질문 수 (qa_log)", f'<span class="big">{_e(o["n_questions"])}</span>' if o["n_questions"] is not None
         else _mark(NONE)),
        ("최종 시험 판정 (result.json)", final),
        ("실행 오류 (result.json)", err),
        ("기록 간 불일치·형식 문제", f'<span class="big">{o["n_anomalies"]}</span>'
         + (f' {_mark(CHANGED)}' if o["n_anomalies"] else "")),
        ("연속 평가 기록 비교(계산)", ch),
        ("근거 직접성 진단 (판정 변화와 별도)", _directness_card(o)),
    ]
    out.append('<div class="kv">' + "".join(f"<div><b>{html.escape(k)}</b>{val}</div>" for k, val in cards) + "</div>")
    out.append('<ul class="src" aria-label="기록 완전성">' + "".join(
        f'<li>{"⚠" if s.get("note") and not s["present"] else ("✓" if s["present"] else "✗")} {html.escape(s["name"])} '
        f'{html.escape(s.get("note") or ("있음" if s["present"] else "없음"))}</li>'
        for s in o["sources"]) + "</ul>")
    out.append(f'<p class="note">모델 설정값(참고) <code>{_e(o["model_config"])}</code> — 설정 파일의 값이며, '
               '이 실행에서 모델이 호출되었는지나 어떤 모델이 호출되었는지를 뜻하지 않습니다. '
               '호출 기록은 D5(기록된 호출량과 비용)에서만 확인합니다.</p>')
    out.append(f'<p class="note">환자 {_e(run["patient"])} · 표시 모드 {_e(run["mode"])} · 최초 입력 출처 '
               f'{_e(run["input_source"])}{" · 사례 " + _e(run["case_id"]) if run.get("case_id") else ""}</p>')
    return "\n".join(out)


def _qa_line(x: dict) -> str:
    r, qa, gen = x["round"], x["qa"], x["questions_generated"]
    if qa is None:
        return _mark(NONE) + " qa_log 없음"
    if qa["count"] == 0:
        if gen:
            return f'{_mark(CHANGED)} 불일치: trace에 질문 생성 기록 {gen}개, qa_log에 이 라운드 질문·답변 기록 없음'
        return "해당 없음 (질문은 라운드 1부터)" if r == 0 else "이 평가 전 기록된 질문 없음"
    s = f'질문 {qa["count"]}개 → 답변 {qa["answered"]} · 모름/확인 불가 {qa["declined"]}'
    if qa["missing"]:
        s += f' · 답변 값 없음·빈 답변·형식 오류 {qa["missing"]}'
    if gen is None:
        s += ' <span class="mut">(trace에 이 라운드 생성 기록 없음)</span>'
    elif gen != qa["count"]:
        s += f' {_mark(CHANGED)} 생성 기록 {gen}개와 qa_log {qa["count"]}개가 다름'
    else:
        s += f' <span class="mut">(라운드 {r - 1} 평가 뒤 생성, 생성 기록 {gen}개)</span>'
    return s


def _fact_line(v: dict, x: dict) -> str:
    entries = x["fact_change_entries"]
    if v["fact_changes"] is None:
        s = _mark(NONE) + " 의존 그래프 진단 없음"
    elif not entries:
        s = _mark(NONE) + " 이 라운드 진단 기록 없음"
    elif len(entries) > 1:
        s = f'진단 기록 {len(entries)}건 (합치지 않음): ' + " / ".join(
            f'<code>{_e(d["changed_fields"]) if d["has_changes"] else "변화 정보 없음"}</code>' for d in entries)
    else:
        d = entries[0]
        if x["compared_with"] is None and not d["has_changes"]:
            s = "첫 평가 (비교 대상 없음)"
        elif not d["has_changes"]:
            s = f'{_mark(NONE)} 변화 정보 없음 (진단 상태 {_e(d["status"])})'
        elif d["changed_fields"]:
            s = f'{_mark(CHANGED)} 바뀐 항목: <code>{_e(d["changed_fields"])}</code>'
        else:
            s = f'{_mark(SAME)} 바뀐 항목 없음'
    if x["fact_shadow"]:
        fs = x["fact_shadow"]
        s += f' <span class="mut">· 사실 저장소 기록 {_e(fs.get("status"))}: 추가 {_e(fs.get("added"))}, 중복 {_e(fs.get("duplicates"))}</span>'
    elif x["fact_shadow_entries"] > 1:
        s += f' <span class="mut">· 사실 저장소 기록 {x["fact_shadow_entries"]}건 (D3 참고)</span>'
    return s


def _trial_line(x: dict) -> str:
    parts = []
    for tid, seq in x["trial_evals"].items():
        if not seq:
            parts.append(f'{_e(tid)} {_mark(NONE)} <span class="mut">(이 라운드 판정 기록 없음)</span>')
            continue
        s = f'{_e(tid)} ' + " → ".join(_elig(e) for e in seq)
        if len(seq) > 1:
            s += f' <span class="mut">(같은 라운드 재평가 {len(seq)}건, trace 순서)</span>'
        chg = [c for c in x["trial_changes"] if c["trial_id"] == tid]
        if chg:
            s += " " + "; ".join(f'{_mark(CHANGED)} {_FIELD[c["field"]]} {_e(c["before"])} → {_e(c["after"])}' for c in chg)
        elif any(p["trial_id"] == tid for p in x["comparisons"]):
            s += f" {_mark(SAME)}"
        else:
            s += ' <span class="mut">(첫 기록)</span>'
        parts.append(s)
    return "<br>".join(parts) or _mark(NONE)


def _criteria_line(x: dict) -> str:
    if not x["comparisons"]:
        return _mark(NA) + " (이 라운드로 이어지는 평가 기록 비교 없음)"
    bits = []
    for label, ids in (("기준 판정", x["verdict_changes"]), ("근거·확인 상태", x["evidence_changes"]),
                       ("근거 직접성", x["directness_changes"])):
        if ids:
            bits.append(f'{_mark(CHANGED)} {label} 바뀐 기준 {len(ids)}개 <code>{_e(ids)}</code>')
    if x["incomparable"]:
        bits.append(f'{_mark(NA)} 비교 불가 {len(x["incomparable"])}개 <code>{_e(x["incomparable"])}</code>')
    if not bits:
        tids = sorted({p["trial_id"] for p in x["comparisons"]})
        return f'{_mark(SAME)} 비교한 시험 {_e(tids)}의 기준 판정·근거 상태'
    return " · ".join(bits)


def _timeline(v: dict) -> str:
    tl = v["timeline"]
    if tl is None:
        return f'<p>{_mark(NONE)} trace.jsonl이 없어 라운드별 흐름을 만들 수 없습니다.</p>'
    if not tl:
        return f'<p>{_mark(NONE)} trace에 라운드 번호가 있는 판정 기록이 없어 라운드별 흐름을 만들 수 없습니다.</p>'
    out = ['<p class="note">라운드 r의 질문은 라운드 r−1 평가 뒤에 생성·답변되고, 그 답변을 넣어 다시 추출한 사실로 라운드 r 평가를 합니다. '
           '같은 라운드 안의 질문·사실·판정 변화를 나란히 보일 뿐, 어떤 답변이 어떤 변화를 일으켰는지는 기록되지 않았습니다. '
           '비교는 시험별로 연속된 판정 기록(trace 순서)끼리 합니다.</p>', '<ol class="tl">']
    for x in tl:
        r = x["round"]
        title = f"라운드 {r} — 최초 입력으로 평가" if x["compared_with"] is None and r == 0 else f"라운드 {r} — 답변 반영 후 재평가"
        ds = x["decisions"]
        d_s = _mark(NONE) if not ds else " / ".join(
            f'<code>{_e(d["stop_reason"])}</code> <span class="mut">{html.escape(_STOP.get(d["stop_reason"] or "", ""))}'
            f' · 상태 {_e(d["status"])}</span>' for d in ds) + (f' <span class="mut">(진단 {len(ds)}건)</span>' if len(ds) > 1 else "")
        out.append(f'<li><h3>{html.escape(title)}</h3><dl class="st">'
                   f'<dt>질문·답변 (QA 라운드 {r})</dt><dd>{_qa_line(x)}</dd>'
                   f'<dt>사실 반영</dt><dd>{_fact_line(v, x)}</dd>'
                   f'<dt>이 라운드 시험 판정</dt><dd>{_trial_line(x)}</dd>'
                   f'<dt>기준 변화 (직전 평가 기록 대비)</dt><dd>{_criteria_line(x)}</dd>'
                   f'<dt>평가 직후 Decision Loop 진단</dt><dd>{d_s}</dd></dl></li>')
    out.append("</ol>")
    if v["unlinked_question_rounds"]:
        out.append(f'<p class="note">{_mark(NA)} 판정 기록이 없는 라운드의 질문: QA 라운드 {_e(v["unlinked_question_rounds"])} '
                   "(라운드에 배정하지 않음, D2 참고)</p>")
    return "\n".join(out)


def _changes(v: dict) -> str:
    ch = v["changes"]
    out = ['<p class="note">시험별로 연속된 판정 기록(trace 순서)끼리 비교한 계산 결과입니다. 변화의 원인(어떤 답변 때문인지)은 '
           '기록되지 않아 표시하지 않습니다. 한쪽 기록에만 있는 기준·필드는 변화 없음이 아니라 비교 불가로 따로 둡니다.</p>']
    if not ch["comparable"]:
        return out[0] + f"<p>{_mark(NA)} 비교할 연속 평가 기록이 없습니다.</p>"
    out.append("<h3>① 시험 수준 판정 변화</h3>")
    if ch["trial"]:
        out.append(_change_rows(ch["trial"]))
    else:
        out.append(f"<p>{_mark(SAME)} 비교한 기록 사이 시험 판정 변화 없음</p>")
    if ch["stable_trials"]:
        out.append("<ul>" + "".join(f'<li>{_e(s["trial_id"])}: 라운드 {_e(s["rounds"])} 기록 모두 {_elig(s["eligibility"])}</li>'
                                    for s in ch["stable_trials"]) + "</ul>")
    n = ch["n_criteria"]
    out.append(f"<h3>② 기준 수준 판정 변화 <span class=\"mut\">(passes · holds) — 바뀐 기준 {n['verdict']}개, "
               f"변경 항목 {len(ch['verdict'])}건</span></h3>")
    out.append(_change_rows(ch["verdict"]) if ch["verdict"] else f"<p>{_mark(SAME)} 비교한 기준의 판정 변화 없음</p>")
    out.append(f"<h3>③ 기준의 근거·확인 상태 변화 <span class=\"mut\">(답변상 해당 여부 · 문서 확인 필요 · 근거 출처 · 해소 경로) — "
               f"바뀐 기준 {n['evidence']}개, 변경 항목 {len(ch['evidence'])}건</span></h3>")
    out.append(_change_rows(ch["evidence"], with_verdict=True) if ch["evidence"]
               else f"<p>{_mark(SAME)} 비교한 기준의 근거·확인 상태 변화 없음</p>")
    if ch["incomparable"]:
        out.append(f"<h3>④ 비교 불가 <span class=\"mut\">— {len(ch['incomparable'])}건</span></h3>")
        out.append(_table(["시험", "기준", "항목", "사유", "비교 기록"],
                          [[_e(c["trial_id"]), _e(c["rule_id"]), _e(c["field"]), html.escape(c["reason"]), _span(c)]
                           for c in ch["incomparable"]], raw_cols={0, 1, 2, 3, 4}))
    out.append(f"<h3>⑤ 근거 직접성 진단 변화 <span class=\"mut\">(근거 직접성 · 코드 검증 사유 · 코드 검증 전 LLM 판정) — "
               + (f"바뀐 기준 {n['directness']}개, 변경 항목 {len(ch['directness'])}건" if ch["directness_comparable"]
                  else "비교할 기록 없음") + "</span></h3>")
    out.append('<p class="note">위 ②·③ 수치에 포함하지 않습니다. 코드 검증(근거 직접성 검사)이 남긴 진단 기록이며, '
               'Inspector가 다시 검사한 결과가 아닙니다.</p>')
    if not ch["directness_comparable"]:
        out.append(f"<p>{_mark(NONE)} trace 판정 기록에 직접성 필드가 없어 라운드 사이 변화를 비교할 수 없습니다 "
                   "(변화 없음이 아님). 최종 값은 D1 기준 표에 있습니다.</p>")
    else:
        out.append(_change_rows(ch["directness"], with_verdict=True) if ch["directness"]
                   else f"<p>{_mark(SAME)} 비교한 기준의 근거 직접성 진단 변화 없음</p>")
    return "\n".join(out)


def render_html(view: dict) -> str:
    v, run = view, view["run"]
    out: list[str] = []
    w = out.append
    w(f"<h1>Decision Loop Inspector</h1><p class=\"mut\">기록된 실행 결과를 그대로 보여 주는 읽기 전용 화면입니다. "
      f"새로 판정하지 않으며, 기록이 없는 항목은 '기록 없음'으로 둡니다. 형식 {_e(v['schema'])}</p>")
    w("<h2>A. 실행 개요</h2>")
    w(_overview(v))
    w("<h2>B. Decision Loop 타임라인</h2>")
    w(_timeline(v))
    w("<h2>C. 주요 변화 요약</h2>")
    w(_changes(v))

    w("<h2>D. 상세 기록</h2>")
    # D1. 후보·기준
    w("<h3>D1. 후보 임상시험과 기준별 판정</h3>")
    s = v["screening"]
    if s:
        w(_table(["풀 크기", "선택", "추림 제외", "상한 초과", "기본 유지", "코드 확정 제외(종류)"],
                 [[s["pool_size"], s["selected"], s["excluded"], s["over_limit"], s["kept_by_default"],
                   [f"{h['trial_id']}({h['kind']})" for h in s["hard_filtered"]]]]))
        w('<p class="note">추림 사유 문장은 LLM 서술이라 표시하지 않습니다.</p>')
    else:
        w('<p class="note">후보 추림 기록 없음 (추림을 하지 않았거나 기록되지 않음)</p>')
    w('<p class="note">근거 직접성·코드 검증 사유·코드 검증 전 LLM 판정: result.json에 기록된 최종 값 (코드 검증이 남긴 진단이며 '
      'Inspector가 다시 검사하지 않음). 근거 문장·인용은 표시하지 않습니다. "기록 없음"은 이 필드가 없는 이전 형식 결과입니다.</p>')
    for t in v["trials"]:
        title = f" · {_e(t['title'])}" if t["title"] else ""
        badge = _criteria_change_badge(t["criteria"])
        w(f"<details><summary>{_e(t['trial_id'])}{title} — {_badge(t['eligibility'])} {_badge(t['match_status'])} "
          f"<span class=\"mut\">충족 {_e(t.get('n_pass'))} / 불충족 {_e(t.get('n_fail'))} / 판정 불가 "
          f"{_e(t.get('n_unknown'))}</span>{badge}</summary>")
        if t["error"]:
            w(f'<p class="warn">평가 실패: {_e(t["error"]["stage"])} ({_e(t["error"]["type"])})</p>')
        if t["rounds"]:
            w(_table(["라운드", "판정", "외부 상태", "충족", "불충족", "판정 불가"],
                     [[x["round"], x["eligibility"], x["match_status"], x.get("n_pass"), x.get("n_fail"),
                       x.get("n_unknown")] for x in t["rounds"]]))
        rows = []
        for c in t["criteria"]:
            chs = "".join(f'<li>라운드 {_e(x["from_round"])}→{_e(x["to_round"])}: {html.escape(_FIELD[x["field"]])} '
                          f'{_val(x["field"], x["before"])} → {_val(x["field"], x["after"])}</li>' for x in c["changes"])
            rows.append([html.escape(str(c.get("rule_id"))), _e(c.get("kind")), _e(c.get("criterion_text")),
                         _e(c.get("method")), _passes(c.get("passes")),
                         f'{_mark(CHANGED)}<ul>{chs}</ul>' if chs else "",
                         _gap(c), _e(c.get("evidence_source")), _e(c.get("required_evidence")),
                         _e(c.get("needs_verification")), _directness_cell(c, "evidence_directness"),
                         _directness_cell(c, "validation_flags"), _directness_cell(c, "llm_judgment"),
                         _e(c.get("category"))])
        w(_table(["기준", "구분", "기준 원문(공개 시험 정보)", "판정 방법", "최종", "라운드별 변화", "해소 경로", "근거 출처",
                  "필요 근거", "문서 확인 필요", "근거 직접성", "코드 검증 사유", "코드 검증 전 LLM 판정", "분류"],
                 rows, raw_cols=set(range(14))))
        w("</details>")
    if v.get("contract"):  # 계약 판정 출처 (ContractSession 기록에만 있음)
        cv = v["contract"]
        w("<h3>계약 판정 출처 (결정적 평가기)</h3>")
        w(f'<p class="note">Session: {_e(cv["session_type"])} · 프로필: {_e(cv["profile_source"])} · '
          '결과 decided만 결정적 확정이며, undecided·partial_ir_not_used는 기존 판정을 그대로 둔 것</p>')
        if cv["criteria"]:
            w(_table(["기준", "명제 키", "질문 계약", "질문", "사실 ID", "IR 버전", "IR digest", "완전성", "출처", "평가 방법",
                      "결과", "라운드"],
                     [[_e(c["criterion_id"]), _e(", ".join(c["proposition_keys"] or [])),
                       _e(", ".join(c["contract_ids"] or [])), _e(", ".join(c["question_ids"] or [])),
                       _e(", ".join(c["fact_ids"] or [])), _e(c["ir_version"]), _e(c["ir_digest"]), _e(c["completeness"]),
                       _e(", ".join(c["sources"] or [])), _e(c["method"]), _e(c["outcome"]), _e(c["round"])]
                      for c in cv["criteria"]], raw_cols=set(range(12))))
        else:
            w('<p class="note">활성(승인된) IR이 없어 결정적 평가가 실행되지 않았습니다.</p>')
        if cv["facts"]:
            w(_table(["사실 ID", "명제 키", "값", "질문 계약", "질문", "답변 기준일", "출처", "정정됨"],
                     [[_e(f["fact_id"]), _e(f["proposition_key"]),
                       _e(f["value"]) if run["mode"] == "synthetic_details" else "(strict: 숨김)",
                       _e(f"{f['contract_id']}@v{f['contract_version']}"), _e(f["question"]), _e(f["as_of"]),
                       _e(f["source"]), _e(f["superseded"])] for f in cv["facts"]], raw_cols=set(range(8))))
    tids = sorted({t for h in v["history"] for t in h["eligibility"]})
    if v["history"]:
        w('<p class="note">매칭 단계별 시험 판정 (result.json eligibility_history: 그 시점 Session에 있던 판정이며, '
          '그 단계에서 다시 평가했는지는 뜻하지 않음)</p>')
        rows = []
        for i, h in enumerate(v["history"]):
            prev = v["history"][i - 1]["eligibility"] if i else {}
            rows.append([_e(h["step"])] + [
                (f'{_mark(CHANGED)} {_badge(h["eligibility"].get(t))}' if i and prev.get(t) != h["eligibility"].get(t)
                 else _badge(h["eligibility"].get(t))) for t in tids])
        w(_table(["단계"] + tids, rows, raw_cols=set(range(len(tids) + 1))))
    if v["recommendation"]:
        rc = v["recommendation"]
        w(_table(["추천(가장 앞 그룹)", "표시 순서", "제외"], [[rc["recommended"], rc["ranked"], rc["excluded"]]]))

    # D2. 질문
    w("<h3>D2. 질문·답변과 Decision Loop 계획</h3>")
    rows = []
    for q in v["questions"]:
        plan = q["overlapping_plan"]
        plan_s = '<span class="na">진단 기록 없음</span>' if plan is None else (
            "<ul>" + "".join(f"<li>{_e(p['info_key'])} · 단계 {_e(p['tier'])} · 우선순위 {_e(p['priority'])} · "
                             f"가치 {_e(p['value_score'])} · {_e(p['reason_code'])}</li>" for p in plan) + "</ul>"
            if plan else '<span class="na">겹치는 계획 없음</span>')
        row = [_e(q["round"]), _e(q["question_id"]), _e(q["purpose"]), _e(q["answer_type"]), _e(q["targets"]),
               _e(q["answer_status"]), _e(q["source"]), plan_s]
        if run["mode"] == "synthetic_details":
            row[2:2] = [_e(q.get("question")), _e(q.get("why")), _e(q.get("answer"))]
        rows.append(row)
    hdr = ["QA 라운드", "ID", "목적", "답 형식", "대상 기준", "답변 상태", "기록 출처", "직전 Decision Loop 계획 중 대상이 겹치는 것"]
    if run["mode"] == "synthetic_details":
        hdr[2:2] = ["질문", "생성기가 기록한 이유", "답변"]
    if v["questions"]:
        w(_table(hdr, rows, raw_cols=set(range(len(hdr)))))
        w('<p class="note">\'겹치는 계획\'은 대상 기준이 같은 계획을 나란히 보인 것이며, 질문이 그 계획 때문에 만들어졌다는 기록은 아닙니다.</p>')
    else:
        w('<p class="note">기록된 질문 없음</p>')
    if v["decision"] is not None:
        for d in v["decision"]:
            w(f"<details><summary>Decision Loop 진단 · 라운드 {_e(d['round'])} 평가 직후 · {_e(d['status'])} · 종료 사유 "
              f"{_e(d['stop_reason'])}{' · 오류 ' + _e(d['error_type']) if d['error_type'] else ''}</summary>")
            if d["usage"]:
                w(_kv_table(d["usage"], LOOP_USAGE_FIELDS))
            w(_table(["행동", "이유 코드", "설명(코드 템플릿)", "단계", "우선순위", "가치 점수", "추상 비용", "대상 기준", "상태"],
                     [[a["action_type"], a["reason_code"], a["reason"], a["tier"], a["priority"], a["value_score"],
                       a["estimated_cost"], a["targets"], a["status"]] for a in d["actions"]]))
            w("</details>")

    # D3. 사실 (기본 접힘)
    w("<h3>D3. 환자 Fact Store</h3>")
    pv = run.get("fact_provenance") or {}
    label = ("이번 실행의 메모리 Session에서 읽음" if pv.get("recorded") == "session"
             else _PROVENANCE.get(pv.get("status"), "기록 없음"))
    w(f'<p class="note">사실 기록 출처: {html.escape(label)} '
      f'<span class="mut">(run_meta.records.fact_store: {_e(pv.get("recorded"))})</span></p>')
    if v["facts"] is not None:
        structured = [f for f in v["facts"] if f["field"] not in NARRATIVE_FIELDS]
        narrative = [f for f in v["facts"] if f["field"] in NARRATIVE_FIELDS]
        hdr = ["항목", "단위", "출처 유형", "검증 상태", "충돌", "버전", "출처", "출처 위치", "검증 이력"]
        if run["mode"] == "synthetic_details":
            hdr.insert(1, "값")

        def rows_of(fs):
            out_ = []
            for f in fs:
                row = [f["field"], f["unit"], f["source_type"], f["verification_status"], f["conflict_status"],
                       f["version"], f["source_kind"], f["source_ref"], f["verification_events"]]
                if run["mode"] == "synthetic_details":
                    row.insert(1, f.get("value"))
                out_.append(row)
            return out_
        w(f"<details><summary>사실 {len(v['facts'])}개 — 구조화 {len(structured)} · 서술형(other_fact) {len(narrative)} "
          f"<span class=\"mut\">(펼치기)</span></summary>")
        w("<h3>구조화된 사실</h3>" + _table(hdr, rows_of(structured)))
        w('<h3>서술형 사실 <span class="mut">(other_fact: 표시 구분만, 내용을 해석하지 않음)</span></h3>'
          + _table(hdr, rows_of(narrative)))
        w("</details>")
    else:
        w(f'<p>{_mark(NONE)} 사실 저장소 파일 없음</p>')
    w('<p class="note">사실 저장소 병행 기록 (라운드별 건수)</p>')
    w(_table(["라운드", "상태", "추가", "중복", "건너뜀", "거부", "오류 종류"],
             [[e["round"], e["status"], e.get("added"), e.get("duplicates"), e.get("skipped"), e.get("rejected"),
               e["error_type"]] for e in v["fact_shadow"]]))
    if v["fact_changes"] is not None:
        w('<p class="note">라운드별 사실 변화 (의존 그래프 진단)</p>')
        w(_table(["라운드", "상태", "재평가 방식", "바뀐 항목", "사실 버전 전", "후"],
                 [[d["round"], d["status"], d["mode"], d["changed_fields"], d["version_before"], d["version_after"]]
                  for d in v["fact_changes"]]))

    # D4. 단계·오류
    w("<h3>D4. 실행 단계와 오류</h3>")
    w(_table(["오류 대상", "종류"], [[e["item"], e["type"]] for e in v["errors"]]) if v["errors"]
      else ('<p class="note">result.json에 기록된 실행 오류 없음</p>' if v["overview"]["n_errors"] is not None
            else f'<p>{_mark(NONE)} result.json에 errors 항목 없음 (오류 여부를 알 수 없음)</p>'))
    w(f"<details><summary>실행 단계 {len(v['steps'])}건</summary>")
    w(_table(["시각(초)", "에이전트", "이벤트", "라운드", "시험", "판정", "종료 사유", "오류 종류"],
             [[s_["t"], s_["agent"], s_["event"], s_["round"], s_["trial_id"], s_["eligibility"], s_["stop_reason"],
               s_["error_type"]] for s_ in v["steps"]]))
    w("</details>")

    # D5. 호출량·비용
    w("<h3>D5. 기록된 호출량과 비용</h3>")
    w(_table(list(v["usage"]), [list(v["usage"].values())]) if v["usage"] else f'<p>{_mark(NONE)} API 호출량 기록 없음 (0과 다름)</p>')
    if v["metering"]:
        w('<p class="note">웹 계측 기록 (web_metering)</p>')
        w(_kv_table(v["metering"]["web"], METER_FIELDS))
        w('<p class="note">예산 원장 기록 (enforce 모드 RunBudget)</p>')
        w(_kv_table(v["metering"]["budget"], BUDGET_FIELDS))
        w('<p class="note">두 기록은 출처가 달라 합산하지 않습니다. 기록에 없는 항목은 표시하지 않으며 0으로 채우지 않습니다.</p>')
    w('<p class="note">Decision Loop의 \'추상 비용\'은 계획용 단위이며 USD가 아닙니다. Inspector는 비용을 추정하지 않습니다.</p>')

    w("<h3>D6. 기록 간 불일치·형식 문제</h3>")
    w("<ul>" + "".join(f"<li>{_mark(CHANGED)} {_e(a)}</li>" for a in v["anomalies"]) + "</ul>" if v["anomalies"]
      else '<p class="note">발견된 불일치·형식 문제 없음 (Inspector가 검사하는 항목 기준)</p>')
    w("<h3>D7. 기록에 없는 데이터</h3><ul>" + "".join(f"<li>{_e(g)}</li>" for g in v["gaps"]) + "</ul>")
    body = "\n".join(out)
    return ("<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
            "<meta name=\"referrer\" content=\"no-referrer\">"
            f"<title>Decision Loop Inspector</title><style>{_CSS}</style></head><body><main>{body}</main></body></html>")


def write_html(view: dict, out: Path) -> Path:
    """새 파일만 만듦 (기존 HTML·링크는 덮어쓰지 않음). data/·src/ 아래는 거부."""
    if Path(out).is_symlink():
        raise InspectorError("링크 경로에는 쓰지 않음")
    out = Path(out).resolve()
    if out.suffix.lower() != ".html":
        raise InspectorError("출력 파일은 .html이어야 함")
    for protected in (config.DATA_DIR.resolve(), (config.ROOT / "src").resolve()):
        if out.is_relative_to(protected):
            raise InspectorError(f"보호된 위치에는 쓰지 않음: {protected}")
    if out.exists():
        raise InspectorError(f"이미 있는 파일은 덮어쓰지 않음: {out.name} (다른 --out 경로를 지정하세요)")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x", encoding="utf-8") as f:  # 배타 생성: 확인과 쓰기 사이에 생긴 파일도 덮어쓰지 않음
        f.write(render_html(view))
    return out
