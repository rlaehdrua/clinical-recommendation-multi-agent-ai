"""평가 위생 (WP1, 감사 P0-2): 케이스 ID를 통한 정답 누출 차단과 답변 시뮬레이터 범위 제한.

문제 (docs/audit/01 §6-1)
- masked 세트 케이스 ID에 정답 유형(rescue/cleanup)이, validation 세트 ID에 대상 기준 ID(-I9- 등)가 들어 있음
- 이 ID가 patient_profiler·orchestrator·matcher(profile.model_dump_json)·recommender 프롬프트로 들어감
- OracleAnswerer는 질문이 숨긴 기준 하나에만 EXACT로 맞아도 그 케이스의 숨긴 사실을 모두 돌려줌 (회수율 과대 추정)

이 모듈 (기존 파일을 바꾸지 않는 호환 래퍼)
- AliasMap        : 원래 ID -> 불투명 별칭 PA-<16 hex> (실행별 비밀 salt의 HMAC, 충돌 검사). 역변환은 평가기만
- LeakScanner     : 프롬프트 문자열에서 원래 ID·정답 유형 토큰·ID 모양 문자열을 찾음 (찾은 '종류'만 보고, 원문 없음)
- PromptLeakGuard : 평가 실행 동안 llm.create/structured/plain을 감싸서 보내기 **전에** 검사하고, 누출이면 차단
- ScopedOracleAnswerer + build_fact_scope : 질문이 EXACT로 겨냥한 기준에 대응하는 숨긴 사실만 공개
- load_cases_hygienic / run_method_hygienic : question_eval 경로에 위 장치를 연결
  (답변기는 question_eval.run_method(answerer_factory=...)로 주입, 모듈 속성을 바꾸지 않음)

정답·별칭 매핑은 HygienicCase / AliasMap 안에만 있고, 파이프라인에 넘기는 PatientCase에는 별칭만 들어갑니다.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import llm

ALIAS_PREFIX = "PA-"
ALIAS_RE = re.compile(r"^PA-[0-9a-f]{16}$")

# 정답 유형 토큰 (metrics.py:9-11의 rescue/cleanup 정의). 데이터 서술·숨긴 사실에는 0건 (2026-11-02 확인)
LABEL_TOKEN_RE = re.compile(r"\b(rescue2?|cleanup)\b", re.IGNORECASE)
# 평가 케이스 ID 모양: F-/M-/V- 접두 + 대문자·숫자 조각 (예: M-SYN-DEMO-A-..., V-DEMO-LUNG-001-I9-single)
CASE_ID_RE = re.compile(r"\b[FMV]-(?:SYN|DEMO|NCT)[A-Za-z0-9-]*")
# validation ID 안의 대상 기준 토큰 (-I9-single 등)
TARGET_RULE_IN_ID_RE = re.compile(r"-[IE]\d+-(?:single|cohort|shared|biomarker|confirmed|provisional|pair|multi)\b", re.IGNORECASE)


class PromptLeakError(RuntimeError):
    """프롬프트에 원래 ID·정답 토큰이 있어 보내지 않음. 메시지에는 종류만 넣고 원문은 넣지 않음."""


# ---------------------------------------------------------------------------
# 별칭
# ---------------------------------------------------------------------------

class AliasCollision(RuntimeError):
    pass


class AliasMap:
    """원래 ID -> 별칭. 같은 salt·같은 원래 ID면 같은 별칭 (재현 가능), salt는 정책·LLM 입력에 넘기지 않음."""

    def __init__(self, salt: str | None = None):
        self._salt = (salt or secrets.token_hex(16)).encode()
        self._fwd: dict[str, str] = {}
        self._rev: dict[str, str] = {}

    def alias(self, original: str) -> str:
        if not original:
            raise ValueError("빈 ID에는 별칭을 만들 수 없음")
        if original in self._fwd:
            return self._fwd[original]
        a = ALIAS_PREFIX + hmac.new(self._salt, original.encode(), hashlib.sha256).hexdigest()[:16]
        if a in self._rev and self._rev[a] != original:
            raise AliasCollision("서로 다른 ID가 같은 별칭으로 바뀜")
        self._fwd[original], self._rev[a] = a, original
        return a

    def resolve(self, alias: str) -> str:
        """평가기 전용 역변환 (정책·LLM 경로에서 부르지 않음)."""
        return self._rev[alias]

    def __len__(self) -> int:
        return len(self._fwd)

    def __repr__(self) -> str:  # 매핑·salt를 로그에 남기지 않음
        return f"AliasMap(n={len(self._fwd)})"

    def unalias_keys(self, pred: dict[tuple[str, str], str]) -> dict[tuple[str, str], str]:
        """{(별칭, 시험): 값} -> {(원래 ID, 시험): 값}. 모르는 별칭은 그대로 둠 (지표 계산에서 누락으로 잡힘)."""
        return {(self._rev.get(p, p), t): v for (p, t), v in pred.items()}


# ---------------------------------------------------------------------------
# 누출 검사
# ---------------------------------------------------------------------------

@dataclass
class LeakScanner:
    literals: set[str] = field(default_factory=set)  # 원래 케이스 ID, 원본 환자 ID 등
    check_label_tokens: bool = True
    check_id_shapes: bool = True

    def add_case(self, case_id: str, source_patient: str | None = None) -> None:
        self.literals.add(case_id)
        if source_patient:
            self.literals.add(source_patient)

    def scan(self, text: str) -> list[str]:
        """찾은 누출 종류 목록 (원문·위치는 넣지 않음)."""
        found = []
        if any(lit and lit in text for lit in self.literals):
            found.append("original_id")
        if self.check_label_tokens and LABEL_TOKEN_RE.search(text):
            found.append("label_token")
        if self.check_id_shapes and (CASE_ID_RE.search(text) or TARGET_RULE_IN_ID_RE.search(text)):
            found.append("case_id_shape")
        return found


def _prompt_texts(kwargs: dict) -> list[str]:
    out = [kwargs.get("system"), kwargs.get("user")]
    for m in kwargs.get("messages") or []:
        c = m.get("content") if isinstance(m, dict) else None
        out.append(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False, default=str) if c else None)
    sysv = kwargs.get("system")
    if isinstance(sysv, list):
        out.append(json.dumps(sysv, ensure_ascii=False, default=str))
    return [t for t in out if isinstance(t, str)]


class PromptLeakGuard:
    """평가 동안 llm.create/structured/plain을 감싸 보내기 전에 검사. 누출이면 PromptLeakError (보내지 않음).

    OfflineEnvironment 안에서 쓰면 대역(stub) 호출도 같은 방식으로 검사합니다. 끝나면 원래 함수로 되돌립니다.
    """

    def __init__(self, scanner: LeakScanner, raise_on_leak: bool = True):
        self.scanner = scanner
        self.raise_on_leak = raise_on_leak
        self.scanned = 0
        self.leaks: Counter = Counter()
        self._saved: list = []

    def _wrap(self, name, fn):
        def wrapped(*args, **kwargs):
            self.scanned += 1
            kinds = sorted({k for t in _prompt_texts(kwargs) for k in self.scanner.scan(t)})
            if kinds:
                for k in kinds:
                    self.leaks[k] += 1
                if self.raise_on_leak:
                    raise PromptLeakError(f"평가 프롬프트 누출 차단 ({name}): {', '.join(kinds)}")
            return fn(*args, **kwargs)
        wrapped.__wrapped__ = fn  # type: ignore[attr-defined]
        return wrapped

    def __enter__(self):
        for name in ("create", "structured", "plain"):
            fn = getattr(llm, name)
            self._saved.append((name, fn))
            setattr(llm, name, self._wrap(name, fn))
        return self

    def __exit__(self, *exc):
        for name, fn in reversed(self._saved):
            setattr(llm, name, fn)
        self._saved.clear()
        return False


def check_visible_case(raw: dict) -> list[str]:
    """평가 사례의 보이는 서술(text)에 숨긴 사실·ID·정답 토큰이 있는지 (종류만)."""
    text = raw.get("text", "")
    sc = LeakScanner()
    sc.add_case(raw.get("patient_id", ""), raw.get("source_patient"))
    issues = sc.scan(text)
    if any(h.get("text") and h["text"] in text for h in raw.get("hidden_facts", [])
           if h.get("fact_id") in set(raw.get("masked_fact_ids", []))):
        issues.append("hidden_fact_text")
    return issues


# ---------------------------------------------------------------------------
# 답변 시뮬레이터 범위 제한
# ---------------------------------------------------------------------------

_STOP = {"the", "a", "an", "of", "or", "and", "is", "are", "with", "for", "in", "on", "to", "no", "not", "any",
         "patient", "patients", "has", "have", "had", "was", "be", "by", "at", "as", "within", "prior", "history"}


def _tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9가-힣]+", s.casefold()) if len(t) > 1 and t not in _STOP}


@dataclass(frozen=True)
class FactScope:
    """숨긴 기준(규칙 키) -> 그 기준에 대응하는 숨긴 사실 ID. 대응을 정할 수 없는 사실은 unassigned (공개하지 않음)."""
    by_rule: dict[str, tuple[str, ...]]
    unassigned: tuple[str, ...] = ()
    method: str = "single_criterion"


def build_fact_scope(raw: dict, parsed: dict) -> FactScope:
    """숨긴 사실 -> 숨긴 기준 대응.

    - 숨긴 기준이 하나면 숨긴 사실은 모두 그 기준에 대응 (모호하지 않음)
    - 여러 개면 사실 문장과 기준 문장의 단어 겹침이 **유일하게 가장 큰** 기준에 대응. 동점·겹침 0이면 unassigned
      (공개하지 않음: 회수율을 과대가 아니라 과소 추정하는 쪽으로)
    """
    from .question_eval import map_criterion
    crits = list(raw.get("masked_criteria", []))
    trials = list(raw.get("candidate_trials", []))
    hidden = {h["fact_id"]: h["text"] for h in raw.get("hidden_facts", [])}
    masked = [i for i in raw.get("masked_fact_ids", []) if i in hidden]
    targets = [(c, map_criterion(c, trials, parsed)) for c in crits]
    by_rule: dict[str, list[str]] = {}
    if len(targets) == 1:
        for k in targets[0][1].rule_keys:
            by_rule.setdefault(k, []).extend(masked)
        return FactScope({k: tuple(v) for k, v in by_rule.items()}, (), "single_criterion")
    unassigned = []
    for fid in masked:
        ft = _tokens(hidden[fid])
        scores = sorted(((len(ft & _tokens(c)), i) for i, (c, _) in enumerate(targets)), reverse=True)
        if not scores or scores[0][0] == 0 or (len(scores) > 1 and scores[0][0] == scores[1][0]):
            unassigned.append(fid)
            continue
        for k in targets[scores[0][1]][1].rule_keys:
            by_rule.setdefault(k, []).append(fid)
    return FactScope({k: tuple(dict.fromkeys(v)) for k, v in by_rule.items()}, tuple(unassigned), "token_overlap")


class ScopedOracleAnswerer:
    """OracleAnswerer와 같은 인터페이스. 질문이 EXACT로 겨냥한 숨긴 기준의 사실만 공개 (다른 기준의 사실은 공개 안 함)."""

    def __init__(self, gt, scope: FactScope):
        self._gt = gt
        self._scope = scope
        self._texts = dict(gt._hidden_texts)
        self.log: list[dict] = []

    def answer(self, questions, patient_id):
        exact = self._gt.exact_keys()
        out = {}
        for q in questions:
            ids: list[str] = []
            for k in q.target:
                if k in exact:
                    ids.extend(i for i in self._scope.by_rule.get(k, ()) if i in self._texts and i not in ids)
            out[q.question_id] = " ".join(self._texts[i] for i in ids) if ids else "모름"
            self.log.append({"question_id": q.question_id, "revealed": bool(ids), "fact_ids": ids})
        return out


# ---------------------------------------------------------------------------
# question_eval 연결 (호환 래퍼)
# ---------------------------------------------------------------------------

@dataclass
class HygienicCase:
    case: object  # PatientCase (patient_id = 별칭)
    gt: object  # CaseGroundTruth (평가기 전용, case_id = 원래 ID)
    scope: FactScope
    visible_issues: list[str]


def load_cases_hygienic(parsed: dict, path: Path | None = None, labels_path: Path | None = None,
                        only: list[str] | None = None, alias_map: AliasMap | None = None
                        ) -> tuple[list[HygienicCase], AliasMap, LeakScanner]:
    """question_eval.load_cases와 같은 사례를 별칭으로 바꿔 돌려줌 (+ 사실 범위, 누출 검사기)."""
    from . import question_eval as qe
    path = Path(path or qe.DEFAULT_CASES)
    labels_path = Path(labels_path or qe.DEFAULT_LABELS)
    amap = alias_map if alias_map is not None else AliasMap()  # 빈 AliasMap은 len 0이라 `or`로 쓰면 버려짐
    raw = {r["patient_id"]: r for r in json.loads(path.read_text(encoding="utf-8"))}
    scanner = LeakScanner()
    out = []
    for case, gt in qe.load_cases(parsed, path, labels_path, only):
        r = raw[gt.case_id]
        scanner.add_case(gt.case_id, r.get("source_patient"))
        aliased = replace(case, patient_id=amap.alias(gt.case_id))
        out.append(HygienicCase(aliased, gt, build_fact_scope(r, parsed), check_visible_case(r)))
    return out, amap, scanner


def scoped_answerer_factory(scope: FactScope):
    """question_eval.run_method(answerer_factory=...)에 넘길 범위 제한 답변기 공장 (B-1: 모듈 속성 교체 없음)."""
    return lambda gt: ScopedOracleAnswerer(gt, scope)


def run_method_hygienic(method: str, hc: HygienicCase, trials: dict, rounds: int, budget: int,
                        scanner: LeakScanner, *, scoped_oracle: bool = True, raise_on_leak: bool = True, **kw):
    """question_eval.run_method를 별칭 사례 + 프롬프트 누출 차단 + (선택) 범위 제한 답변기로 실행.

    반환: (MethodRun, PromptLeakGuard). 누출이 한 번이라도 있으면 run.status="error", error_type="PromptLeakError"
    (누출된 프롬프트는 보내지 않음). OfflineEnvironment 안에서 불러야 함 (실제 API 차단).
    scoped_oracle=False면 기존 OracleAnswerer(숨긴 사실 일괄 공개)를 씀 - 비교용.
    """
    from . import question_eval as qe
    if "answerer_factory" in kw:
        raise TypeError("답변기는 scoped_oracle 인자로 정함 (answerer_factory 직접 지정 불가)")
    factory = scoped_answerer_factory(hc.scope) if scoped_oracle else None
    guard = PromptLeakGuard(scanner, raise_on_leak=raise_on_leak)
    with guard:
        run = qe.run_method(method, hc.case, hc.gt, trials, rounds, budget, answerer_factory=factory, **kw)
    if guard.leaks:  # 파이프라인이 시험 단위 예외를 잡아 NOT_EVALUATED로 남길 수 있으므로 실행 전체를 실패로 표시
        run.status, run.error_type = "error", "PromptLeakError"
    return run, guard
