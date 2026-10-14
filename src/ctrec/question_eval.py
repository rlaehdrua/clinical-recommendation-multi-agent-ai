"""기존 질문 생성 경로 vs Decision Loop 질문 계획 - 오프라인 비교 평가 (Phase 4-1).

**오프라인 구조 평가입니다.** 환자 정보 이해·Matcher·질문 생성의 LLM은 결정적 대역으로 바뀌므로,
결과는 실제 LLM 질문 성능·추천 정확도·비용을 뜻하지 않습니다. 같은 상태에서 두 질문 계획이 숨겨진 정보를
구조적으로 겨냥하는지, 중복·불필요 질문이 있는지를 비교합니다.

비교 대상
- baseline : 기존 Session.clarify -> question_generator.generate_questions 경로. 오프라인에서는 생성기 입력
             (판정 보류 시험의 compact 'unknown' 목록 + 확보 단계 계획)을 받은 순서대로 고르는 결정적 대역
             (BASELINE_SOURCE). 실제 LLM 생성기의 선택·묶음과 다를 수 있음.
- candidate: 같은 Session에서 Decision Loop 진단(decision_loop="diagnostic")이 낸 ASK_USER 계획을 순서대로 질문으로 바꿈
             (평가용 어댑터, 실제 추천 경로에는 연결하지 않음).
두 방식은 같은 환자 입력·같은 후보 시험·같은 질문 예산(라운드당 질문 수, 라운드 수)·같은 답변 시뮬레이터·같은
Session 재추출·재매칭 경로를 씁니다. 각 방식은 자기 질문의 답으로 다음 라운드 상태가 갈라집니다(라운드 1은 같은 상태).

정답 분리
- 평가 사례 파일의 숨긴 정보(oracle_facts, hidden_facts, masked_fact_ids, masked_criteria, clue_fact_ids)는
  CaseGroundTruth에만 담기고, 파이프라인 입력은 pipeline._case_from_dict(_HIDDEN_KEYS 제외)로 만듭니다.
- 답변 시뮬레이터(OracleAnswerer)만 정답을 보며, 질문 대상이 숨긴 기준에 정확히(EXACT) 대응할 때만 숨긴 사실 원문을
  답으로 줍니다. 추정(INFERRED) 대응·대응 실패는 '모름'. 답은 자기 보고로 기록되어 검증 상태로 승격되지 않습니다.
- 산출물에는 사례·시험·기준 ID, 숫자, 상태 코드만 넣고 환자 원문·숨긴 사실 원문은 넣지 않습니다.
"""

from __future__ import annotations

import contextvars
import csv
import hashlib
import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from . import config, llm
from .acquisition import is_open as acquisition_open
from .agents import matcher
from .decision_state import SessionView, build_state, info_key
from .pipeline import PatientCase, Session, _case_from_dict
from .schemas import (ClarifyingQuestion, Fact, LabValue, LLMAssessment, MatcherOutput, PatientProfile, QuestionSet,
                      RecommendationDraft)

BASELINE_SOURCE = "baseline_stub_first_unknown"
CANDIDATE_SOURCE = "decision_loop_plan"
ABLATION_SOURCE = "ablation_decision_policy_protocol_tiebreak"  # 평가 전용 변형 (Phase 3 정책을 바꾸지 않음)
METHODS = ("baseline", "candidate")
ABLATION = "ablation_protocol_tiebreak"
DEFAULT_CASES = config.DATA_DIR / "patients" / "masked" / "masked_cases.json"
DEFAULT_LABELS = config.DATA_DIR / "labels" / "masked_labels.csv"


# ---------------------------------------------------------------------------
# 정답 (평가기 전용)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GTTarget:
    criterion_ref: str  # 숨긴 기준 문장의 요약값 (원문은 넣지 않음)
    status: str  # EXACT / INFERRED / INFERRED_SPLIT / UNMAPPED
    rule_keys: tuple[str, ...]  # "<trial>:<rule>"


@dataclass(frozen=True)
class CaseGroundTruth:
    case_id: str
    case_type: str
    synthetic: bool
    candidate_trials: tuple[str, ...]
    masked_fact_ids: tuple[str, ...]
    targets: tuple[GTTarget, ...]
    labels: tuple[tuple[str, str], ...] = ()  # (trial, 정보가 모두 있을 때의 정답 판정)
    # 평가용 상태 구성(정답 아님): 환자 정보 추출 뒤 문서 검증된 것으로 표시할 항목 (확정 불충족 합성 사례용)
    setup_verified_fields: tuple[str, ...] = ()
    _hidden_texts: tuple[tuple[str, str], ...] = field(default=(), repr=False)  # (fact_id, 원문) - 답변 시뮬레이터만

    def exact_keys(self) -> set[str]:
        return {k for t in self.targets if t.status == "EXACT" for k in t.rule_keys}

    def inferred_keys(self) -> set[str]:
        return {k for t in self.targets if t.status in ("INFERRED", "INFERRED_SPLIT") for k in t.rule_keys}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9가-힣]+", " ", s.casefold()).strip()


def _ref(text: str) -> str:
    return "C-" + hashlib.sha256(_norm(text).encode()).hexdigest()[:10]


def map_criterion(text: str, trials: list[str], parsed: dict) -> GTTarget:
    """숨긴 기준 문장 -> 고정 기준표의 원자 규칙. 문장이 같으면 EXACT, 한쪽이 다른 쪽을 포함하는 규칙이 하나면 INFERRED,
    규칙 문장들이 기준 문장 안에 나뉘어 있으면 INFERRED_SPLIT, 없으면 UNMAPPED (여러 후보는 고르지 않음)."""
    n = _norm(text)
    exact, contain, split = [], [], []
    for tid in trials:
        for r in parsed[tid].rules:
            rn = _norm(r.text)
            key = f"{tid}:{r.rule_id}"
            if rn == n:
                exact.append(key)
            elif len(min(rn, n, key=len)) >= 20 and (rn in n or n in rn):
                contain.append(key)
            elif len(rn) >= 15 and rn in n:
                split.append(key)
    if exact:
        return GTTarget(_ref(text), "EXACT", tuple(exact))
    if len(contain) == 1:
        return GTTarget(_ref(text), "INFERRED", tuple(contain))
    if contain:
        return GTTarget(_ref(text), "AMBIGUOUS", tuple(contain))
    if split:
        return GTTarget(_ref(text), "INFERRED_SPLIT", tuple(split))
    return GTTarget(_ref(text), "UNMAPPED", ())


def load_cases(parsed: dict, path: Path = DEFAULT_CASES, labels_path: Path = DEFAULT_LABELS,
               only: list[str] | None = None) -> list[tuple[PatientCase, CaseGroundTruth]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    labels: dict[str, list[tuple[str, str]]] = {}
    if Path(labels_path).exists():
        for row in csv.DictReader(Path(labels_path).open(encoding="utf-8")):
            labels.setdefault(row["patient_id"], []).append((row["trial_id"], row["label"]))
    out = []
    for raw in data:
        if only and raw["patient_id"] not in only:
            continue
        case = _case_from_dict(raw, raw["patient_id"])  # 숨긴 정보 키는 파이프라인 입력에서 빠짐
        case.candidate_trials = list(raw["candidate_trials"])
        case.oracle_facts = case.hidden_details = None  # 정답은 CaseGroundTruth에만 (평가 실행 객체에 남기지 않음)
        hidden = {h["fact_id"]: h["text"] for h in raw.get("hidden_facts", [])}
        gt = CaseGroundTruth(
            case_id=raw["patient_id"], case_type=raw["case_type"], synthetic=bool(raw.get("synthetic")),
            candidate_trials=tuple(raw["candidate_trials"]), masked_fact_ids=tuple(raw.get("masked_fact_ids", [])),
            targets=tuple(map_criterion(t, raw["candidate_trials"], parsed) for t in raw.get("masked_criteria", [])),
            labels=tuple(labels.get(raw["patient_id"], [])),
            setup_verified_fields=tuple(raw.get("setup_verified_fields", [])),
            _hidden_texts=tuple((i, hidden[i]) for i in raw.get("masked_fact_ids", []) if i in hidden))
        out.append((case, gt))
    return out


def validate_cases(path: Path = DEFAULT_CASES, parsed: dict | None = None) -> dict:
    """사례 파일 점검 (값·원문 없이 건수만)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    issues = Counter()
    for c in data:
        visible = c.get("text", "")
        if not c.get("synthetic"):
            issues["not_marked_synthetic"] += 1
        if any(h["text"] in visible for h in c.get("hidden_facts", [])):
            issues["hidden_text_in_visible"] += 1
        if re.search(r"\bF\d+\b", visible):
            issues["fact_id_in_visible"] += 1
        hidden_ids = {h["fact_id"] for h in c.get("hidden_facts", [])}
        if not set(c.get("masked_fact_ids", [])) <= hidden_ids:
            issues["masked_id_not_hidden"] += 1
        if parsed is not None and any(t not in parsed for t in c["candidate_trials"]):
            issues["trial_not_in_store"] += 1
    mapping = Counter()
    if parsed is not None:
        for c in data:
            for t in c.get("masked_criteria", []):
                mapping[map_criterion(t, c["candidate_trials"], parsed).status] += 1
    return {"cases": len(data), "case_types": dict(Counter(c["case_type"] for c in data)),
            "issues": dict(issues), "masked_criteria_mapping": dict(mapping)}


# ---------------------------------------------------------------------------
# 오프라인 대역 (실제 API 차단)
# ---------------------------------------------------------------------------

_LAB_PATTERNS = [
    ("hemoglobin", r"(?:hemoglobin|haemoglobin|\bhb\b)[^0-9\n]{0,20}(\d+(?:\.\d+)?)\s*(g/dl)"),
    ("platelets", r"(?:platelets?|\bplt\b)[^0-9\n]{0,20}(\d+(?:\.\d+)?)\s*((?:x|×)\s?10\^?9/l)"),
    ("absolute neutrophil count", r"(?:\banc\b|absolute neutrophil count)[^0-9\n]{0,20}(\d+(?:\.\d+)?)\s*((?:x|×)\s?10\^?9/l)"),
    ("creatinine", r"(?<!clearance )creatinine[^0-9\n]{0,20}(\d+(?:\.\d+)?)\s*(mg/dl)"),
]


def stub_profiler(patient_id: str, raw_text: str, reference_date: str | None = None) -> PatientProfile:
    """결정적 환자 정보 대역: 문장 단위 사실 + 나이·성별·ECOG·일부 검사 수치만 정규식으로. 추론 없음."""
    lines = [ln.strip(" -\t") for ln in raw_text.splitlines()]
    lines = [ln[2:].strip() if ln.startswith("A:") else ln for ln in lines]
    lines = [ln for ln in lines if ln and not ln.startswith(("Q:", "[추가 확인 정보"))]
    text = "\n".join(lines).casefold()
    age = re.search(r"(\d+(?:\.\d+)?)[- ]?(year|month|week|day)s?[- ]old", text)
    sex = "female" if re.search(r"\b(female|woman|girl)\b", text) else "male" if re.search(r"\b(male|man|boy)\b", text) \
        else "unknown"
    ecog = re.search(r"ecog(?: performance status)?(?: score)?(?: of| is|:)?\s*([0-5])\b", text)
    labs = []
    for name, pat in _LAB_PATTERNS:
        for ln in lines:
            m = re.search(pat, ln.casefold())
            if m:
                labs.append(LabValue(name=name, value=float(m.group(1)), unit=m.group(2), date=None, source_quote=ln))
                break
    unit = {"year": "years", "month": "months", "week": "weeks", "day": "days"}[age.group(2)] if age else None
    return PatientProfile(
        patient_id=patient_id, age=int(float(age.group(1))) if age and unit == "years" else None,
        age_value=float(age.group(1)) if age else None, age_unit=unit, sex=sex, primary_diagnosis=None,
        disease_stage=None, diagnoses=[], biomarkers=[], labs=labs, medications=[], treatment_history=[],
        ecog=int(ecog.group(1)) if ecog else None,
        other_facts=[Fact(description=ln, date=None, source_quote=ln) for ln in lines], missing_or_ambiguous=[])


class StubLLM:
    """Matcher·추천 LLM 대역. LLM 판정 기준은 모두 '판정 불가(근거 없음)'. 그 밖의 요청은 거절. 호출 수를 셈."""

    def __init__(self):
        self.calls = Counter()

    def structured(self, model_cls, *, system, user, **k):
        llm.guard()
        self.calls[model_cls.__name__] += 1
        if model_cls is MatcherOutput:
            rules = json.loads(user.split("<rules_to_assess>")[1].split("</rules_to_assess>")[0])
            return MatcherOutput(assessments=[LLMAssessment(
                rule_id=r["rule_id"], applicable=True, holds="unknown", evidence_type="absent", confidence="low",
                evidence="근거 없음", reasoning="offline stub", missing_info="확인 필요") for r in rules])
        if model_cls is RecommendationDraft:
            pid = user.split("patient_id=")[1].split(" ")[0]
            return RecommendationDraft(patient_id=pid, ranked=[], excluded=[], overall_comment="offline stub")
        raise RuntimeError(f"offline stub: 지원하지 않는 LLM 요청 {model_cls.__name__}")


def _blocked(*a, **k):
    raise RuntimeError("오프라인 평가: 실제 LLM API 호출이 차단되어 있습니다 (유료 실행은 별도 승인 필요)")


class OfflineEnvironment:
    """평가 동안만 대역을 설치하고 끝나면 되돌림. 실제 API 클라이언트·create·plain은 차단."""

    def __init__(self, question_budget: int):
        self.stub = StubLLM()
        self.budget = question_budget
        self._saved: list = []

    def _set(self, obj, name, value):
        self._saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def __enter__(self):
        from . import pipeline
        self._set(llm, "client", _blocked)
        self._set(llm, "create", _blocked)
        self._set(llm, "plain", _blocked)
        self._set(llm, "structured", self.stub.structured)
        self._set(pipeline.patient_profiler, "profile_patient", stub_profiler)
        self._set(config, "WRITE_REPORT", False)
        self._set(config, "QUESTION_POLICY", "batch")
        self._set(config, "MAX_QUESTIONS_PER_ROUND", self.budget)
        return self

    def __exit__(self, *exc):
        for obj, name, value in reversed(self._saved):
            setattr(obj, name, value)
        return False


# ---------------------------------------------------------------------------
# 질문 생성 (두 방식) - 같은 Session 경로에서 generate_questions만 바뀜
# ---------------------------------------------------------------------------

def _asked_keys(qa_log) -> set[str]:
    return {k for q in qa_log for k in q.target}


def baseline_generator(profile, matches, qa_log, n, acquisition_plan=None) -> QuestionSet:
    """기존 생성기 입력(compact 'unknown' 목록, 확보 단계 계획)을 받은 순서대로 고르는 결정적 대역. 질문 하나 = 기준 하나."""
    asked = _asked_keys(qa_log)
    qs = []
    for m in matches:
        for u in matcher.compact(m)["unknown"]:
            key = f"{m.trial_id}:{u['rule_id']}"
            if key in asked or (acquisition_plan is not None and key not in acquisition_plan):
                continue
            qs.append(ClarifyingQuestion(question_id=f"Q{len(qs) + 1}", question=f"[baseline] {key}",
                                         answer_type="free_text", target=[key], purpose="eligibility",
                                         why=BASELINE_SOURCE))
            asked.add(key)
            if len(qs) >= n:
                return QuestionSet(questions=qs)
    return QuestionSet(questions=qs)


def candidate_generator(session_ref: dict):
    """같은 Session의 최신 Decision Loop 진단에서 ASK_USER 계획을 순서대로 질문으로 (평가용 어댑터)."""
    def generate(profile, matches, qa_log, n, acquisition_plan=None) -> QuestionSet:
        s = session_ref["session"]
        diag = s.decision_diagnostics[-1] if s.decision_diagnostics else {}
        asked = _asked_keys(qa_log)
        qs = []
        for a in diag.get("actions", []):
            if a["action_type"] != "ASK_USER":
                continue
            keys = [k for k in a["target_criterion_ids"] if k not in asked and acquisition_open(s.missing_facts, k)]
            if not keys:
                continue
            qs.append(ClarifyingQuestion(question_id=f"Q{len(qs) + 1}", question=f"[decision] {a['info_key']}",
                                         answer_type="free_text", target=keys, purpose="eligibility",
                                         why=CANDIDATE_SOURCE))
            asked |= set(keys)
            if len(qs) >= n:
                break
        return QuestionSet(questions=qs)
    return generate


def ablation_generator(session_ref: dict):
    """평가 전용 변형: Decision Policy의 행동 후보·단계·가치 점수는 그대로 두고, 같은 순위 안의 동점만 프로토콜 순서
    (후보 시험 순서 -> 기준표의 규칙 순서)로 정함. 기존 정책은 정보 키 문자열 순서로 동점을 정해 제외 기준(E*)이 먼저 옴.
    채택된 정책이 아니라 원인 확인용 비교입니다."""
    from .decision_policy import candidates

    def generate(profile, matches, qa_log, n, acquisition_plan=None) -> QuestionSet:
        s = session_ref["session"]
        view = SessionView.from_session(s, {t: "current" for t in s.matches})
        view.matched_versions = {t: view.snapshot.version for t in s.matches}
        st = build_state(view)
        order = {}
        for ti, tid in enumerate(s.case.candidate_trials or list(s.trials)):
            for ri, r in enumerate(s.parsed[tid].rules if tid in s.parsed else []):
                order[f"{tid}:{r.rule_id}"] = (ti, ri)
        asks = [a for a in candidates(st)[0] if a.action_type == "ASK_USER"]
        asks.sort(key=lambda a: (a.tier, -a.value_score,
                                 min((order.get(k, (99, 999)) for k in a.target_criterion_ids), default=(99, 999))))
        asked, qs = _asked_keys(qa_log), []
        for a in asks:
            keys = [k for k in a.target_criterion_ids if k not in asked and acquisition_open(s.missing_facts, k)]
            if not keys:
                continue
            qs.append(ClarifyingQuestion(question_id=f"Q{len(qs) + 1}", question=f"[ablation] {a.info_key}",
                                         answer_type="free_text", target=keys, purpose="eligibility",
                                         why=ABLATION_SOURCE))
            asked |= set(keys)
            if len(qs) >= n:
                break
        return QuestionSet(questions=qs)
    return generate


class OracleAnswerer:
    """답변 시뮬레이터 (정답 접근은 여기만). 질문 대상이 숨긴 기준에 EXACT로 대응할 때만 숨긴 사실 원문을 답으로."""

    def __init__(self, gt: CaseGroundTruth):
        self._gt = gt
        self.log: list[dict] = []

    def answer(self, questions, patient_id):
        exact = self._gt.exact_keys()
        texts = [t for _, t in self._gt._hidden_texts]
        out = {}
        for q in questions:
            hit = bool(set(q.target) & exact)
            out[q.question_id] = " ".join(texts) if hit and texts else "모름"
            self.log.append({"question_id": q.question_id, "revealed": hit and bool(texts)})
        return out


# ---------------------------------------------------------------------------
# 실행과 지표
# ---------------------------------------------------------------------------

class _NullTracer:
    def log(self, *a, **k):
        pass


def _classify(view: SessionView, keys: list[str]) -> dict:
    """질문 시점의 Decision State로 질문 대상 분류 (평가기 계측, 두 방식에 똑같이 적용)."""
    st = build_state(view)
    by = {c.key: c for c in st.criteria()}
    cls = Counter()
    for k in keys:
        c = by.get(k)
        tid = k.split(":")[0]
        if c is None or tid not in st.trials:
            cls["not_candidate"] += 1
            continue
        t = st.trials[tid]
        if c.gap == "none":
            cls["already_sufficient"] += 1
        elif t.mismatch == "confirmed" or (t.mismatch == "none" and c.cohort and c.cohort not in t.open_paths
                                           and "all" not in t.open_paths):
            cls["confirmed_mismatch_only"] += 1
        elif t.mismatch == "provisional":
            cls["provisional_mismatch"] += 1
        else:
            cls["relevant"] += 1
    return dict(cls)


@dataclass
class MethodRun:
    method: str
    status: str = "ok"
    error_type: str | None = None
    questions: list[dict] = field(default_factory=list)
    final_eligibility: dict[str, str] = field(default_factory=dict)
    round0_matches: dict = field(default_factory=dict)
    final_gt_states: dict[str, str] = field(default_factory=dict)
    decision_finals: list[str] = field(default_factory=list)
    diagnostics: list[dict] = field(default_factory=list)  # Decision Loop 진단 기록 (ID·코드만)
    candidate_log: list = field(default_factory=list)  # tie_order recorder (후보 목록)
    safety: Counter = field(default_factory=Counter)


def run_method(method: str, case: PatientCase, gt: CaseGroundTruth, trials: dict, rounds: int, budget: int,
               deadline: float | None = None, cancel: "callable | None" = None,
               factories: dict | None = None, tie_policy: str | None = None,
               record_candidates: bool = False) -> MethodRun:
    from . import pipeline
    from .decision_loop import DecisionTracker, LoopConfig
    from .decision_state import Budget

    run = MethodRun(method)
    c = PatientCase(patient_id=case.patient_id, raw_text=case.raw_text, input_source=case.input_source,
                    candidate_trials=list(case.candidate_trials or []))
    ref: dict = {}
    if tie_policy is not None:  # 실제 Decision Loop 경로 + 동점 순서만 주입 (Phase 4-3)
        gen = candidate_generator(ref)
    elif factories and method in factories:  # Phase 4-2 방식(상태를 평가기가 다시 만듦) - 비교용으로만 보존
        gen = factories[method](ref)
    else:
        gen = (baseline_generator if method == "baseline" else ablation_generator(ref) if method == ABLATION
               else candidate_generator(ref))
    answerer = OracleAnswerer(gt)
    s = Session(case=c, trials={t: trials[t] for t in gt.candidate_trials}, answerer=answerer, tracer=_NullTracer(),
                max_rounds=rounds, question_budget=None,
                decision_loop="diagnostic" if method == "candidate" or tie_policy is not None else "off")
    if method == "candidate" or tie_policy is not None:
        s._decision_tracker = DecisionTracker(LoopConfig(budget=Budget(max_questions=budget)))
    ref["session"] = s

    def generate(profile, matches, qa_log, n, acquisition_plan=None):
        if not run.round0_matches:
            run.round0_matches = {t: m.model_dump() for t, m in s.matches.items()}
        view = SessionView.from_session(s)
        qs = gen(profile, matches, qa_log, n, acquisition_plan)
        for i, q in enumerate(qs.questions):
            run.questions.append(_record(gt, method, s.rounds, i, q, view, qa_log))
        st = build_state(view)  # 안전 불변식 (질문 계획과 무관하게 매 라운드 확인)
        run.safety["met_with_conflicting_basis"] += sum(c.status == "MET" and c.conflict_on_decided for c in st.criteria())
        run.safety["document_verified_facts"] += sum(  # 평가용 상태 구성으로 표시한 항목은 제외 (승격이 아님)
            f.verification_status == "document_verified" and f.field not in gt.setup_verified_fields
            for f in view.snapshot.facts)
        run.safety["mismatch_basis_not_not_met"] += sum(
            1 for t in st.trials.values() for k in t.mismatch_basis
            if next(c for c in t.criteria if c.key == k).status != "NOT_MET")
        run.safety["not_evaluated_in_mismatch"] += sum(t in st.trials for t in st.not_evaluated_trials)
        return qs

    def guard():
        if cancel and cancel():
            return "cancelled"
        if deadline and time.time() > deadline:
            return "timeout"
        return None

    if gt.setup_verified_fields:  # 평가용 상태 구성: 추출된 해당 항목 사실을 문서 검증으로 표시 (확정 불충족 사례)
        real_profile = s.profile_patient

        def profile_and_verify():
            out = real_profile()
            for f in s.fact_store.facts(s.case.patient_id):
                if f.field in gt.setup_verified_fields and f.verification_status != "document_verified":
                    s.fact_store.set_verification(f.fact_id, "document_verified", reason="합성 평가 상태",
                                                  by="question_eval", document_reference="SYNTHETIC")
            return out
        s.profile_patient = profile_and_verify
    from contextlib import nullcontext
    from .question_priority import tie_order
    recorder = [] if record_candidates else None
    try:
        def go():  # 질문 생성기 교체·취소 확인은 이 실행의 컨텍스트에서만 (다른 실행에 보이지 않음)
            llm.set_guard(guard)
            with pipeline.question_generator.use_generator(generate), \
                    (tie_order(tie_policy, ref, recorder) if (tie_policy is not None or recorder is not None)
                     else nullcontext()):
                s.run_fixed()
        contextvars.copy_context().run(go)
        if guard():
            raise llm.RunCancelled(guard())
    except llm.RunCancelled as e:
        run.status, run.error_type = e.args[0] if e.args else "cancelled", "RunCancelled"
    except Exception as e:  # noqa: BLE001 - 실패는 결과에서 빼지 않고 상태로 남김 (메시지에는 원문이 있을 수 있어 종류만)
        run.status, run.error_type = "error", type(e).__name__
    if not run.round0_matches:
        run.round0_matches = {t: m.model_dump() for t, m in s.matches.items()}
    run.final_eligibility = {t: m.eligibility for t, m in s.matches.items()}
    for k in gt.exact_keys() | gt.inferred_keys():
        tid, rid = k.split(":", 1)
        m = s.matches.get(tid)
        a = next((x for x in (m.assessments + m.other_cohort_assessments if m else []) if x.rule_id == rid), None)
        run.final_gt_states[k] = "UNKNOWN" if a is None or a.passes is None else ("MET" if a.passes else "NOT_MET")
    run.decision_finals = [d.get("stop_reason") or d.get("status") for d in s.decision_diagnostics]
    run.diagnostics = s.decision_diagnostics
    run.candidate_log = recorder or []
    if any(e.get("status") == "error" for e in s.decision_diagnostics):
        run.safety["decision_diagnostic_errors"] += 1
    run.revealed = sum(x["revealed"] for x in answerer.log)  # type: ignore[attr-defined]
    answers = {(q.round, q.question_id): q.answer for q in s.qa_log}
    for r in run.questions:
        ans = answers.get((r["round_id"], r["question_id"]))
        r["answered"] = ans is not None and ans != "모름"
    return run


def _record(gt: CaseGroundTruth, method: str, round_id: int, i: int, q: ClarifyingQuestion, view: SessionView,
            qa_log) -> dict:
    keys = list(q.target)
    exact, inferred = gt.exact_keys(), gt.inferred_keys()
    parsed = view.parsed
    norm_targets = []
    for k in keys:
        tid, rid = k.split(":", 1)
        rule = next((r for r in parsed[tid].rules if r.rule_id == rid), None) if tid in parsed else None
        norm_targets.append(info_key(rule, tid)[0] if rule else "unresolved")
    if set(keys) & exact:
        status, conf = "EXACT_HIT", 1.0
    elif set(keys) & inferred:
        status, conf = "INFERRED_HIT", 0.5
    elif "unresolved" in norm_targets:
        status, conf = "UNMAPPED", 0.0
    else:
        status, conf = "MISS", 1.0
    prev = {k for qq in qa_log for k in qq.target}
    prev_answered = {k for qq in qa_log if qq.answer not in ("모름", "") for k in qq.target}
    return {"case_id": gt.case_id, "method": method, "round_id": round_id, "question_id": q.question_id,
            "question_ref": f"{gt.case_id}:{method}:{round_id}:{q.question_id}",
            "fact_version": view.snapshot.version, "target_fact_ids": [], "target_criterion_ids": keys,
            "target_trial_ids": sorted({k.split(':')[0] for k in keys}), "question_type": q.purpose, "priority": i + 1,
            "source": {"baseline": BASELINE_SOURCE, "candidate": CANDIDATE_SOURCE,
                       ABLATION: ABLATION_SOURCE}.get(method, f"policy:{method}"),
            "normalized_target": sorted(set(norm_targets)), "mapping_status": status, "mapping_confidence": conf,
            "relevance": _classify(view, keys), "same_criterion_reasked": sorted(set(keys) & prev),
            "answered_reasked": sorted(set(keys) & prev_answered)}


def _ratio(num: float, den: float) -> dict:
    return {"numerator": num, "denominator": den, "value": round(num / den, 4) if den else None}


def case_metrics(gt: CaseGroundTruth, run: MethodRun, budget: int) -> dict:
    qs = run.questions
    exact, inferred = gt.exact_keys(), gt.inferred_keys()
    first_exact = next((i + 1 for i, q in enumerate(qs) if q["mapping_status"] == "EXACT_HIT"), None)
    first_any = next((i + 1 for i, q in enumerate(qs) if q["mapping_status"] in ("EXACT_HIT", "INFERRED_HIT")), None)
    keys_seen, semantic_seen, sem_dup = set(), set(), 0
    for q in qs:
        sem = tuple(q["normalized_target"])
        if sem in semantic_seen and not set(q["target_criterion_ids"]) & keys_seen:
            sem_dup += 1
        semantic_seen.add(sem)
        keys_seen |= set(q["target_criterion_ids"])
    irrelevant = sum(1 for q in qs if not q["relevance"].get("relevant") and not q["relevance"].get("provisional_mismatch"))
    by_cls = Counter()
    for q in qs:
        for k, v in q["relevance"].items():
            by_cls[k] += v
    labels = dict(gt.labels)
    return {
        "case_id": gt.case_id, "case_type": gt.case_type, "method": run.method, "status": run.status,
        "has_exact_target": bool(exact), "has_inferred_target": bool(inferred),
        "questions": len(qs), "rounds_with_questions": len({q["round_id"] for q in qs}),
        "exact_hit": first_exact is not None, "any_hit": first_any is not None,
        "exact_hit_within_budget": first_exact is not None and first_exact <= budget,
        "first_exact_hit_rank": first_exact, "first_any_hit_rank": first_any,
        "duplicate_same_criterion": sum(bool(q["same_criterion_reasked"]) for q in qs),
        "duplicate_semantic": sem_dup, "reasked_answered": sum(bool(q["answered_reasked"]) for q in qs),
        "irrelevant_questions": irrelevant, "provisional_mismatch_targets": by_cls.get("provisional_mismatch", 0),
        "target_classes": dict(by_cls), "unanswered_questions": sum(not q.get("answered") for q in qs),
        "revealed_answers": getattr(run, "revealed", 0),
        "gt_resolved_after": sum(v != "UNKNOWN" for v in run.final_gt_states.values()),
        "gt_targets_total": len(run.final_gt_states),
        "final_label_agreement": sum(labels.get(t) == e for t, e in run.final_eligibility.items() if t in labels),
        "label_pairs": sum(t in labels for t in run.final_eligibility),
        "decision_finals": run.decision_finals, "safety": dict(run.safety),
        "mapping_unmapped_or_ambiguous": sum(q["mapping_status"] in ("UNMAPPED", "AMBIGUOUS") for q in qs),
    }


def aggregate(rows: list[dict], method: str) -> dict:
    rs = [r for r in rows if r["method"] == method]
    ok = [r for r in rs if r["status"] == "ok"]
    with_exact = [r for r in ok if r["has_exact_target"]]
    with_any = [r for r in ok if r["has_exact_target"] or r["has_inferred_target"]]
    nq = sum(r["questions"] for r in ok)
    ranks = [r["first_exact_hit_rank"] for r in with_exact if r["first_exact_hit_rank"]]
    safety = Counter()
    for r in ok:
        safety.update(r["safety"])
    return {
        "cases": len(rs), "ok": len(ok), "failed": len(rs) - len(ok),
        "failed_cases": [{"case_id": r["case_id"], "status": r["status"]} for r in rs if r["status"] != "ok"],
        "coverage_exact": _ratio(sum(r["exact_hit"] for r in with_exact), len(with_exact)),
        "coverage_exact_within_budget": _ratio(sum(r["exact_hit_within_budget"] for r in with_exact), len(with_exact)),
        "coverage_exact_or_inferred": _ratio(sum(r["any_hit"] for r in with_any), len(with_any)),
        "questions_total": nq, "questions_per_case": round(nq / len(ok), 3) if ok else None,
        "median_questions_to_first_exact_hit": sorted(ranks)[len(ranks) // 2] if ranks else None,
        "duplicate_same_criterion_rate": _ratio(sum(r["duplicate_same_criterion"] for r in ok), nq),
        "duplicate_semantic_rate": _ratio(sum(r["duplicate_semantic"] for r in ok), nq),
        "reasked_answered": sum(r["reasked_answered"] for r in ok),
        "irrelevant_rate": _ratio(sum(r["irrelevant_questions"] for r in ok), nq),
        "provisional_mismatch_targets": sum(r["provisional_mismatch_targets"] for r in ok),
        "unanswered_rate": _ratio(sum(r["unanswered_questions"] for r in ok), nq),
        "revealed_answers": sum(r["revealed_answers"] for r in ok),
        "gt_resolved_after": _ratio(sum(r["gt_resolved_after"] for r in ok), sum(r["gt_targets_total"] for r in ok)),
        "final_label_agreement": _ratio(sum(r["final_label_agreement"] for r in ok), sum(r["label_pairs"] for r in ok)),
        "mapping_unmapped_or_ambiguous": sum(r["mapping_unmapped_or_ambiguous"] for r in ok),
        "safety": dict(safety),
    }


def run_evaluation(parsed: dict, trials: dict, *, only: list[str] | None = None, rounds: int = 2, budget: int = 3,
                   case_timeout_s: float = 30.0, cancel=None, cases_path: Path = DEFAULT_CASES,
                   labels_path: Path = DEFAULT_LABELS, ablation: bool = False, methods: tuple | None = None,
                   factories: dict | None = None, tie_policies: dict | None = None) -> dict:
    """오프라인 평가 실행. 결과는 메모리 dict (파일 저장은 save_outputs로 명시적으로)."""
    cases = load_cases(parsed, cases_path, labels_path, only)
    all_methods = tuple(methods) if methods else METHODS + ((ABLATION,) if ablation else ())
    rows, records, matcher_mismatch, t0 = [], [], [], time.time()
    with OfflineEnvironment(budget) as env:
        for case, gt in cases:
            runs = {}
            for method in all_methods:
                runs[method] = run_method(method, case, gt, trials, rounds, budget,
                                          deadline=time.time() + case_timeout_s, cancel=cancel, factories=factories,
                                          tie_policy=(tie_policies or {}).get(method))
                rows.append(case_metrics(gt, runs[method], budget))
                records.extend({k: v for k, v in q.items() if k != "relevance"} | {"relevance": q["relevance"]}
                               for q in runs[method].questions)
            if len({json.dumps(r.round0_matches, sort_keys=True, default=str) for r in runs.values()}) > 1:
                matcher_mismatch.append(gt.case_id)  # 라운드 1(같은 상태)의 Matcher 판정이 방식마다 달라지면 안 됨
        stub_calls = dict(env.stub.calls)
    per_case = {}
    for r in rows:
        per_case.setdefault(r["case_id"], {})[r["method"]] = r
    if not {"baseline", "candidate"} <= set(all_methods):
        diffs = []
    else:
        diffs = [{"case_id": cid, "case_type": d["baseline"]["case_type"],
              "baseline_first_exact": d["baseline"]["first_exact_hit_rank"],
              "candidate_first_exact": d["candidate"]["first_exact_hit_rank"],
              "baseline_questions": d["baseline"]["questions"], "candidate_questions": d["candidate"]["questions"],
              "baseline_irrelevant": d["baseline"]["irrelevant_questions"],
              "candidate_irrelevant": d["candidate"]["irrelevant_questions"]}
                 for cid, d in per_case.items() if "baseline" in d and "candidate" in d]
    agg = {m: aggregate(rows, m) for m in all_methods}
    return {
        "mode": "offline_stub", "rounds": rounds, "question_budget_per_round": budget,
        "cases_total": len(cases), "seconds": round(time.time() - t0, 2), "stub_llm_calls": stub_calls,
        "methods": agg,
        "difference_candidate_minus_baseline": {
            k: round(agg["candidate"][k]["value"] - agg["baseline"][k]["value"], 4)
            for k in ("coverage_exact", "coverage_exact_within_budget", "coverage_exact_or_inferred",
                      "duplicate_same_criterion_rate", "duplicate_semantic_rate", "irrelevant_rate")
            if "candidate" in agg and "baseline" in agg
            and agg["candidate"][k]["value"] is not None and agg["baseline"][k]["value"] is not None},
        "round1_matcher_identical": not matcher_mismatch, "round1_matcher_mismatch_cases": matcher_mismatch,
        "case_rows": rows, "case_diffs": diffs, "question_records": records,
        "limitations": LIMITATIONS,
    }


LIMITATIONS = [
    "LLM(환자 정보 이해·Matcher·질문 생성)은 오프라인 대역: 실제 LLM 질문 성능·추천 정확도·비용을 뜻하지 않음",
    "baseline은 기존 생성기의 입력 목록을 순서대로 고르는 대역이며, 실제 LLM 생성기의 질문 묶음·우선순위와 다름",
    "Matcher 대역은 LLM 판정 기준을 모두 '판정 불가'로 두어 불확실 기준이 실제보다 많음 -> 두 방식 모두 같은 조건",
    "숨긴 기준 중 고정 기준표 규칙에 정확히 대응하지 않는 기준(INFERRED·UNMAPPED)은 정확 지표에서 제외하고 따로 보고",
    "답변 시뮬레이터는 EXACT 대응 질문에만 숨긴 사실을 공개 (질문 문장 품질은 평가하지 않음)",
    "라운드 1은 두 방식이 같은 상태, 라운드 2부터는 각자 받은 답에 따라 상태가 갈라짐",
]


def save_outputs(result: dict, out_dir: Path) -> list[Path]:
    """요약 JSON과 사례별 CSV 저장 (ID·숫자·상태 코드만). 명시적으로 부를 때만 파일을 씀."""
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {k: v for k, v in result.items() if k not in ("case_rows", "question_records")}
    p1 = out_dir / "summary.json"
    p1.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    p2 = out_dir / "cases.csv"
    cols = ["case_id", "case_type", "method", "status", "questions", "exact_hit", "first_exact_hit_rank", "any_hit",
            "duplicate_same_criterion", "duplicate_semantic", "irrelevant_questions", "provisional_mismatch_targets",
            "unanswered_questions", "revealed_answers", "gt_resolved_after", "gt_targets_total",
            "final_label_agreement", "label_pairs"]
    with p2.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(result["case_rows"])
    return [p1, p2]


# ---------------------------------------------------------------------------
# 유료 평가 dry-run 비용 추정과 승인 게이트
# ---------------------------------------------------------------------------

# 가정값: 실제 측정·확인 전. 모델 단가는 반드시 실행 전 공식 가격표로 확인해 바꿔야 함 (confirmed=False)
ASSUMED_PRICES_PER_MTOK = {"default": {"input": 3.0, "output": 15.0, "confirmed": False}}
ASSUMED_TOKENS = {  # 호출 1회당 (입력, 출력) 토큰 - 가정
    "patient_profiler": (3000, 1500), "matcher": (6000, 2500), "question_generator": (5000, 800),
    "simulated_patient": (2500, 300), "recommender": (6000, 1500),
}
REFERENCE_NOTE = "README의 환자당 약 $0.2–0.7은 과거 참고값이며 이번 추정의 측정값이 아님"


def estimate_cost(n_cases: int, avg_trials: float, rounds: int, *, retry_rate: float = 0.1,
                  prices: dict | None = None, tokens: dict | None = None, cap_usd: float | None = None,
                  methods: int = 2, disabled: tuple = ()) -> dict:
    """유료 평가 비용 추정 (dry-run). 단계별 호출 수 = 실제 고정 순서(run_fixed) 기준의 상한."""
    prices = prices or ASSUMED_PRICES_PER_MTOK
    tokens = tokens or ASSUMED_TOKENS
    price = prices.get(config.MODEL, prices["default"])
    per_case = {
        "patient_profiler": 1 + rounds,  # 처음 + 라운드마다 재추출
        "matcher": avg_trials * (1 + rounds) * (1 + config.MATCHER_MISSING_RETRIES * retry_rate),
        "question_generator": rounds,
        "simulated_patient": rounds,
        "recommender": 1,
    }
    for k in disabled:  # 이 실행에서 LLM을 쓰지 않는 단계 (예: 질문 계획 어댑터, 결정적 답변 시뮬레이터)
        per_case[k] = 0
    calls = {k: v * n_cases * methods for k, v in per_case.items()}
    inp = sum(calls[k] * tokens[k][0] for k in calls)
    out = sum(calls[k] * tokens[k][1] for k in calls)
    base = inp / 1e6 * price["input"] + out / 1e6 * price["output"]
    total = base * (1 + retry_rate)
    return {
        "model": config.MODEL, "cases": n_cases, "methods": methods, "rounds": rounds, "avg_trials": avg_trials,
        "calls_per_case_per_method": {k: round(v, 2) for k, v in per_case.items()},
        "calls_total": {k: round(v, 1) for k, v in calls.items()}, "input_tokens": int(inp), "output_tokens": int(out),
        "estimated_usd": round(total, 2), "retry_overhead_rate": retry_rate,
        "price_per_mtok": price, "prices_confirmed": bool(price.get("confirmed")), "tokens_confirmed": False,
        "cap_usd": cap_usd, "within_cap": None if cap_usd is None else total <= cap_usd, "disabled_stages": list(disabled),
        "reference_note": REFERENCE_NOTE,
    }


class PaidRunNotApproved(RuntimeError):
    pass


def require_paid_approval(approve_flag: bool, estimate: dict) -> None:
    """유료 실행 게이트: 명시 플래그 + 환경 변수 CTREC_ALLOW_PAID_EVAL=1 + 비용 상한 + 확인된 단가가 모두 있어야 함.
    이번 단계(Phase 4-1)에서는 통과하더라도 유료 실행 경로를 제공하지 않음."""
    if not approve_flag or os.getenv("CTREC_ALLOW_PAID_EVAL") != "1":
        raise PaidRunNotApproved("유료 평가 승인 플래그(--approve-paid-run)와 CTREC_ALLOW_PAID_EVAL=1이 모두 필요합니다")
    if estimate.get("cap_usd") is None or not estimate.get("within_cap"):
        raise PaidRunNotApproved("비용 상한(--max-cost-usd) 안의 추정치가 필요합니다")
    if not estimate.get("prices_confirmed"):
        raise PaidRunNotApproved("모델 단가가 확인되지 않았습니다 (가정값으로는 실행하지 않음)")
    raise PaidRunNotApproved("Phase 4-1에서는 유료 실행 경로를 제공하지 않습니다")
