"""정보 가리기(controlled masking) 평가셋 생성.

정보가 모두 있는 합성 환자(공개 서술 + hidden_details)에서 판정을 가르는 사실만 골라 숨겨,
"질문으로 그 정보를 되찾아 올바른 판정을 회복하는가"를 측정하는 평가 사례를 자동으로 만듭니다.

1) 사실 분해: 환자 전체 기록을 번호 붙은 원자 사실 목록(F1, F2, ...)으로 나눕니다.
   - 사실 문장에 원문에 없는 숫자가 있으면 다시 요청하고, 그래도 있으면 그 환자를 건너뜁니다.
2) 결정 기준 주석: (환자, 시험) 쌍마다 판정을 가르는 기준과 그 근거 사실 ID를 표시합니다.
   - 정답 라벨(data/labels/synthetic_labels.csv)과 모순되는 주석은 버리고 검토 목록에 남깁니다.
   - 목록에 없는 사실 ID는 버립니다.
3) 변형 생성 (모집 중인 시험만. 모집 종료 시험에는 확인 질문을 하지 않으므로 제외)
   - full    : 모든 사실 공개 (정보가 다 있을 때의 기준선)
   - rescue  : 정답 ELIGIBLE 쌍에서 충족 기준 1개(또는 2개)의 근거 사실을 숨김 -> 서술만으로는 UNCERTAIN
   - cleanup : 정답 INELIGIBLE 쌍에서 위반 기준의 근거 사실을 모두 숨김     -> 서술만으로는 UNCERTAIN
   나이·성별·주 진단 같은 핵심 사실(core)은 숨기지 않습니다.
4) 누출 검사: 별도 LLM이 남은 사실만 보고 숨긴 기준을 추론할 수 있는지 확인합니다.
   추론 가능하면 단서 사실도 함께 숨기고 다시 검사하며(최대 2회), 그래도 새거나 핵심 사실이 단서면 그 변형을 버립니다.
5) 숨긴 사실은 oracle_facts / hidden_facts로만 저장되고 파이프라인 입력에서는 빠집니다(pipeline._HIDDEN_KEYS).
   평가 시 가상 환자(FactRevealAnswerer)는 질문이 직접 묻는 사실만 원문 그대로 공개합니다.

LLM 출력은 data/cache/benchmark/에 캐시되어, 다시 실행하면 API를 호출하지 않습니다.
주석은 LLM이 만든 것이므로 평가에 쓰기 전에 연구진이 표본을 검토해야 합니다.

사용:
    python scripts/build_masked_benchmark.py --workers 4
출력: data/patients/masked/masked_cases.json, data/labels/masked_labels.csv, data/patients/masked/build_report.json
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ctrec import config, llm
from ctrec.agents.criteria_parser import numbers_in
from ctrec.agents.recommender import is_open
from ctrec.pipeline import load_patients
from ctrec.tools import ctgov

CACHE = config.CACHE_DIR / "benchmark"
MAX_PRIMARY_FACTS = 3  # 결정 근거로 숨기는 사실 수 상한 (기준 1개당. 회수율은 이 사실들로 계산)
MAX_MASKED_FACTS = 8   # 누출을 막으려 함께 숨기는 단서 사실까지 포함한 상한
VERIFY_ROUNDS = 3      # 누출 검사 -> 단서 사실 추가로 숨기기 반복 횟수


# ---------------------------------------------------------------------------
# LLM 출력 스키마
# ---------------------------------------------------------------------------

class AtomicFact(BaseModel):
    fact_id: str = Field(description="F1, F2, ... 순서")
    text: str = Field(description="한 가지 사실만 담은 영어 문장. 수치·날짜·단위·부정 표현은 원문 그대로")
    core: bool = Field(description="나이, 성별, 주 진단(내원 사유)처럼 의뢰 시 항상 알려지는 사실이면 true")


class FactList(BaseModel):
    facts: list[AtomicFact]


class DecisiveCriterion(BaseModel):
    criterion: str = Field(description="시험 선정/제외 기준 원문에서 그대로 발췌한 구절")
    kind: Literal["inclusion", "exclusion"]
    patient_status: Literal["satisfied", "violated"] = Field(
        description="satisfied: 선정 기준 충족 또는 제외 사유 없음 / violated: 선정 기준 불충족 또는 제외 사유 해당")
    evidence_fact_ids: list[str] = Field(
        description="이 판정을 직접 확인하거나 추론할 수 있게 하는 사실 ID 전부. 이 사실들을 모두 지우면 판정할 수 없어야 함")


class PairAnnotation(BaseModel):
    criteria: list[DecisiveCriterion]
    notes: str


class CriterionCheck(BaseModel):
    criterion: str
    determinable: bool = Field(description="보이는 사실만으로 이 기준의 충족 여부를 확인하거나 합리적으로 추론할 수 있으면 true")
    revealing_fact_ids: list[str] = Field(description="determinable=true일 때 단서가 되는 사실 ID")


class MaskCheck(BaseModel):
    checks: list[CriterionCheck]


FACT_SYSTEM = """You split a synthetic patient record into atomic facts for an evaluation benchmark.
- Each fact is one short English sentence stating exactly one clinical fact (a finding, value, history item, medication, negative finding, plan, or logistics statement).
- Copy numbers, units, dates and negations exactly as written. Never add, infer, round or convert anything.
- The record may state the same information more than once (the additional record often repeats the main text, or says
  the same thing in other words such as "BCG-naive" and "never received BCG"). Write each piece of information ONCE.
- Cover every piece of information in the record, including negative statements ("No history of diabetes.") and consent/logistics statements.
- Mark core=true only for (a) age, (b) sex, (c) the bare name of the primary diagnosis. A core fact must contain nothing else:
  write e.g. "The patient has acute pancreatitis." as its own core fact and put timing, episode counts, severity, stage,
  test results or history into separate core=false facts. Expect at most 3 core facts.
- Number facts F1, F2, ... in reading order."""

ANNOTATE_SYSTEM = """You annotate which patient facts decide a clinical-trial eligibility outcome, for an evaluation benchmark.
Inputs: the trial's eligibility criteria, the patient's atomic facts (fact_id: text), and the reference label decided by researchers.

List decisive criteria:
- If the reference label is INELIGIBLE: list EVERY criterion the patient violates (patient_status="violated"), not just one.
- For both labels: also list up to 6 criteria the patient satisfies (patient_status="satisfied") whose status depends on specific
  non-core facts (lab values, treatment or medication history, comorbidities, negative findings, test results, procedures).
  Skip criteria decided only by age, sex or the primary diagnosis, and skip consent/logistics criteria.
- criterion: copy the relevant phrase verbatim from the eligibility criteria.
- evidence_fact_ids: EVERY fact from which this criterion's status could be established or reasonably inferred.
  Removing all listed facts must leave the criterion undeterminable from the remaining facts.
- Use only fact_ids that appear in the input. Do not invent facts.
- If you believe the reference label is wrong, still annotate and explain in notes."""


VERIFY_SYSTEM = """You audit a masked patient record for an evaluation benchmark. Some facts were deliberately removed.
For each listed trial criterion, decide whether the patient's status on it (met or not met) can be determined OR reasonably
inferred from the visible facts alone. Be strict and count indirect clues: e.g. "specimen had uninvolved detrusor muscle"
reveals non-muscle-invasive disease; "never received systemic cancer treatment" reveals no prior chemotherapy;
"second hospitalization" reveals at least two episodes. If determinable, list every visible fact_id that gives it away.
Return each criterion exactly once."""


# ---------------------------------------------------------------------------
# 캐시된 LLM 호출
# ---------------------------------------------------------------------------

def _cached(kind: str, key_parts: list[str], model_cls, *, system: str, user: str, effort: str):
    key = hashlib.sha256("\x00".join([config.MODEL, system, user, *key_parts]).encode()).hexdigest()[:16]
    path = CACHE / f"{kind}_{key}.json"
    if path.exists():
        return model_cls.model_validate_json(path.read_text(encoding="utf-8"))
    out = llm.structured(model_cls, system=system, user=user, effort=effort)
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(out.model_dump_json(indent=1), encoding="utf-8")
    return out


def full_record(case) -> str:
    return f"{case.raw_text}\n\n[Additional record]\n{case.hidden_details or ''}".strip()


def split_facts(case) -> tuple[list[AtomicFact] | None, list[str]]:
    """사실 분해 + 수치 무결성 검사. 원문에 없는 숫자가 있으면 1회 재요청."""
    record = full_record(case)
    source = numbers_in(record)
    for attempt in range(2):
        facts = _cached("facts", [case.patient_id, str(attempt)], FactList, system=FACT_SYSTEM,
                        user=f"<record>\n{record}\n</record>", effort="medium").facts
        for i, f in enumerate(facts, 1):
            f.fact_id = f"F{i}"  # 번호 중복·누락 방지
        bad = [f"{f.fact_id}: {sorted(numbers_in(f.text) - source)}" for f in facts if numbers_in(f.text) - source]
        if not bad:
            return facts, []
    return None, [f"{case.patient_id}: 원문에 없는 숫자가 들어간 사실 {bad}"]


def annotate(case, facts: list[AtomicFact], trial: dict, label: str, rationale: str) -> PairAnnotation:
    fact_lines = "\n".join(f"{f.fact_id}{' [core]' if f.core else ''}: {f.text}" for f in facts)
    user = f"""<trial id="{trial['trial_id']}">
{trial['eligibility_criteria']}
structured: sex={trial.get('sex')}, minimum_age={trial.get('minimum_age')}, maximum_age={trial.get('maximum_age')}
</trial>

<patient_facts>
{fact_lines}
</patient_facts>

<reference_label>{label}</reference_label>
<reference_rationale>{rationale}</reference_rationale>"""
    return _cached("annot", [case.patient_id, trial["trial_id"]], PairAnnotation,
                   system=ANNOTATE_SYSTEM, user=user, effort="high")


def verify_mask(src: str, tid: str, facts: list[AtomicFact], masked: set[str],
                criteria: list[str]) -> tuple[set[str] | None, str | None]:
    """누출 검사: 남은 사실만 보고 숨긴 기준을 추론할 수 있으면 단서 사실도 숨김. 실패하면 (None, 사유)."""
    core = {f.fact_id for f in facts if f.core}
    masked = set(masked)
    for _ in range(VERIFY_ROUNDS):
        visible = [f for f in facts if f.fact_id not in masked]
        user = ("<visible_facts>\n" + "\n".join(f"{f.fact_id}: {f.text}" for f in visible)
                + "\n</visible_facts>\n\n<criteria>\n" + "\n".join(f"- {c}" for c in criteria) + "\n</criteria>")
        check = _cached("verify", [src, tid, ",".join(sorted(masked))], MaskCheck,
                        system=VERIFY_SYSTEM, user=user, effort="high")
        visible_ids = {f.fact_id for f in visible}
        leaks = {i for c in check.checks if c.determinable for i in c.revealing_fact_ids if i in visible_ids}
        if not any(c.determinable for c in check.checks):
            return masked, None
        if not leaks:
            return None, "남은 사실로 추론 가능하다고 판정됐지만 단서 사실을 특정하지 못함"
        if leaks & core:
            return None, f"핵심 사실이 단서임 ({sorted(leaks & core)})"
        masked |= leaks
        if len(masked) > MAX_MASKED_FACTS:
            return None, f"단서까지 숨기면 {len(masked)}개로 너무 많음"
    return None, f"누출 검사 {VERIFY_ROUNDS}회 후에도 추론 가능"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


# ---------------------------------------------------------------------------
# 변형 생성
# ---------------------------------------------------------------------------

def render(facts: list[AtomicFact]) -> str:
    return "\n".join(f"- {f.text}" for f in facts)


def _sorted_ids(ids) -> list[str]:
    return sorted(ids, key=lambda x: int(x[1:]))


def make_case(pid: str, src: str, facts: list[AtomicFact], masked: set[str], trials: list[str],
              case_type: str, masked_criteria: list[str], primary: set[str] | None = None) -> dict:
    primary = masked if primary is None else primary
    return {
        "patient_id": pid,
        "text": render([f for f in facts if f.fact_id not in masked]),
        # 아래 필드는 파이프라인에 노출되지 않음 (가상 환자·평가용)
        "oracle_facts": [f.model_dump() for f in facts],
        "hidden_facts": [f.model_dump() for f in facts if f.fact_id in masked],
        "masked_fact_ids": _sorted_ids(primary),             # 결정 근거 사실 (회수율 계산 대상)
        "clue_fact_ids": _sorted_ids(masked - primary),       # 누출 방지로 함께 숨긴 단서 사실
        "masked_criteria": masked_criteria,
        "case_type": case_type,
        "source_patient": src,
        "candidate_trials": trials,
        "synthetic": True,
    }


def variants_for_pair(src: str, tid: str, label: str, facts: list[AtomicFact], ann: PairAnnotation,
                      max_single: int) -> tuple[list[tuple[str, set[str], list[str]]], str | None]:
    """반환: [(case_type, 숨길 사실 ID, 숨긴 기준)], 건너뛴 사유."""
    ids = {f.fact_id for f in facts}
    core = {f.fact_id for f in facts if f.core}

    def evidence(c: DecisiveCriterion) -> set[str]:
        return {i for i in c.evidence_fact_ids if i in ids}  # 화이트리스트

    violated = [c for c in ann.criteria if c.patient_status == "violated"]
    if label == "INELIGIBLE":
        if not violated:
            return [], "정답 INELIGIBLE인데 위반 기준을 찾지 못함"
        masked = set().union(*(evidence(c) for c in violated))
        if not masked or masked & core:
            return [], "위반 근거가 핵심 사실(나이·성별·주 진단)이거나 비어 있음"
        if len(masked) > MAX_PRIMARY_FACTS * 2:
            return [], f"숨길 사실이 {len(masked)}개로 너무 많음"
        return [("cleanup", masked, [c.criterion for c in violated])], None

    if violated:
        return [], "정답 ELIGIBLE인데 위반 기준이 주석됨 (라벨 검토 필요)"
    usable = [c for c in ann.criteria
              if evidence(c) and not (evidence(c) & core) and len(evidence(c)) <= MAX_PRIMARY_FACTS]
    out, seen = [], set()
    for c in usable:
        key = frozenset(evidence(c))
        if key in seen:
            continue
        seen.add(key)
        out.append(("rescue", set(key), [c.criterion]))
        if len(out) >= max_single:
            break
    if len(out) >= 2:  # 기준 2개를 동시에 숨긴 변형 (질문 여러 개가 필요한 사례)
        out.append(("rescue2", out[0][1] | out[1][1], out[0][2] + out[1][2]))
    return out, (None if out else "숨길 수 있는 충족 기준 근거가 없음")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--patients-dir", default="data/patients/synthetic")
    ap.add_argument("--trials-dir", default="data/trials")
    ap.add_argument("--labels", default="data/labels/synthetic_labels.csv")
    ap.add_argument("--out-cases", default="data/patients/masked/masked_cases.json")
    ap.add_argument("--out-labels", default="data/labels/masked_labels.csv")
    ap.add_argument("--max-single", type=int, default=2, help="ELIGIBLE 쌍마다 기준 1개를 숨긴 변형 최대 개수")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--only", help="쉼표로 구분한 원본 환자 ID만 처리 (품질 확인용)")
    args = ap.parse_args()

    trials = {ctgov.load_local(p)["trial_id"]: ctgov.load_local(p) for p in Path(args.trials_dir).glob("*.json")}
    cases = {c.patient_id: c for p in sorted(Path(args.patients_dir).glob("*.json")) for c in load_patients(p)}
    if args.only:
        keep = set(args.only.split(","))
        cases = {pid: c for pid, c in cases.items() if pid in keep}
    with open(args.labels, encoding="utf-8") as f:
        gold = [r for r in csv.DictReader(f) if r["patient_id"] in cases and r["trial_id"] in trials]
    pairs = [r for r in gold if is_open(trials[r["trial_id"]])]
    warnings: list[str] = []

    # 1) 사실 분해
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        split = dict(zip(cases, pool.map(lambda c: split_facts(c), cases.values())))
    facts = {}
    for pid, (fl, warn) in split.items():
        warnings += warn
        if fl:
            facts[pid] = fl
    print(f"사실 분해: 환자 {len(facts)}/{len(cases)}명, 평균 {sum(map(len, facts.values())) / max(1, len(facts)):.1f}개 사실")

    # 2) 결정 기준 주석
    todo = [r for r in pairs if r["patient_id"] in facts]

    def work(r):
        return r, annotate(cases[r["patient_id"]], facts[r["patient_id"]], trials[r["trial_id"]],
                           r["label"], r.get("rationale", ""))

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        annotated = list(pool.map(work, todo))

    # 3) 변형 생성
    out_cases, out_labels, stats = [], [], Counter()
    open_by_patient = defaultdict(list)
    for r in pairs:
        open_by_patient[r["patient_id"]].append(r)
    for src, rows in sorted(open_by_patient.items()):
        if src not in facts:
            continue
        pid = f"F-{src}"
        out_cases.append(make_case(pid, src, facts[src], set(), [r["trial_id"] for r in rows], "full", []))
        for r in rows:
            out_labels.append({"patient_id": pid, "trial_id": r["trial_id"], "label": r["label"],
                               "label_text_only": r["label"], "case_type": "full", "source_patient": src,
                               "masked_fact_ids": "", "clue_fact_ids": "", "masked_criteria": "", "rationale": r.get("rationale", "")})
            stats["full"] += 1

    for r, ann in annotated:
        src, tid, label = r["patient_id"], r["trial_id"], r["label"]
        unverified = [c.criterion[:60] for c in ann.criteria if _norm(c.criterion) not in _norm(trials[tid]["eligibility_criteria"])]
        if unverified:
            warnings.append(f"{src}/{tid}: 원문에서 찾지 못한 기준 발췌 {unverified}")
        variants, skip = variants_for_pair(src, tid, label, facts[src], ann, args.max_single)
        if skip:
            warnings.append(f"{src}/{tid} ({label}) 건너뜀: {skip}")
            stats[f"skipped_{label}"] += 1
        seen = set()
        for n, (case_type, primary, crits) in enumerate(variants, 1):
            masked, leak = verify_mask(src, tid, facts[src], primary, crits)
            if masked is None:
                warnings.append(f"{src}/{tid} {case_type}-{n} 제외 (누출): {leak}")
                stats["dropped_leak"] += 1
                continue
            if frozenset(masked) in seen:
                continue
            seen.add(frozenset(masked))
            pid = f"M-{src}-{tid}-{case_type}-{n}"
            out_cases.append(make_case(pid, src, facts[src], masked, [tid], case_type, crits, primary))
            out_labels.append({"patient_id": pid, "trial_id": tid, "label": label, "label_text_only": "UNCERTAIN",
                               "case_type": case_type, "source_patient": src,
                               "masked_fact_ids": ";".join(_sorted_ids(primary)),
                               "clue_fact_ids": ";".join(_sorted_ids(masked - primary)),
                               "masked_criteria": " | ".join(crits), "rationale": r.get("rationale", "")})
            stats[case_type] += 1

    Path(args.out_cases).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_cases).write_text(json.dumps(out_cases, ensure_ascii=False, indent=1), encoding="utf-8")
    with open(args.out_labels, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out_labels[0]))
        w.writeheader()
        w.writerows(out_labels)
    report = {"stats": dict(stats), "patients": len(out_cases), "label_rows": len(out_labels),
              "open_pairs": len(pairs), "warnings": warnings}
    (Path(args.out_cases).parent / "build_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"생성: 환자 {len(out_cases)}명, 라벨 {len(out_labels)}쌍 {dict(stats)}")
    print(f"경고 {len(warnings)}건 -> {Path(args.out_cases).parent / 'build_report.json'}")
    print(f"-> {args.out_cases}\n-> {args.out_labels}")


if __name__ == "__main__":
    main()
