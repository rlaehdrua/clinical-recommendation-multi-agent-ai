"""파싱 검증 에이전트 - 팀 결정 D1 Option D (Hybrid) + D6 Option C (상태 기반 underspecified 처리).

    결정적 검사 -> 위험 항목 탐지 -> 위험 항목만 LLM 의미 검증 -> 심각한 불일치는 의사 확인(UNSUPPORTED)

근거: 같은 시험을 같은 프롬프트로 3번 파싱하면 공통 규칙의 9.2%에서 규칙 엔진 필드가, 2.5%에서 underspecified가
바뀌었습니다(scripts/parse_consistency.py). 파서 출력 하나를 그대로 믿지 않고, 사전 컴파일(scripts/compile_trials.py)
때 한 번 검증해 고정 저장소(data/parsed/)에 저장합니다. 환자를 평가할 때는 다시 파싱하거나 검증하지 않습니다.

1) 결정적 검사 (LLM 없음)
   - 수치·부등호 무결성(criteria_parser.check_integrity), 규칙 엔진 필드·연산자 형식
   - 선정/제외 방향: 규칙 문장이 원문의 반대 절(선정 <-> 제외)에만 있는가
   - 코호트 이름이 cohorts 목록에 있는가
   - 원문 기준 문장 중 어떤 규칙에도 담기지 않은 문장(누락 후보)
2) 위험 항목 탐지
   - underspecified=true (D6: 파서 한 번의 판단을 믿지 않음)
   - 절차 '선정' 기준 (미확인이어도 적격을 막지 않으므로 잘못 분류되면 위험)
   - 검사 수치·ECOG 규칙 엔진 규칙 (LLM 없이 확정 판정)
   - 선정/제외 방향 불일치, 코호트 불일치
   - 다른 파싱 실행과 속성(underspecified, 선정/제외, 규칙 엔진 필드, 절차 분류)이 다른 규칙
   - category="other" (근거 수준을 유형으로 정할 수 없음, D2)
3) LLM 의미 검증: 위험 항목과 누락 후보만 원문과 대조
4) 적용 (코드)
   - 의미가 바뀐 규칙(조건·방향 오류) -> review: 자동 판정하지 않고 의사 확인 (UNSUPPORTED)
   - underspecified인데 원문은 구체적 -> 규칙 단위 재분류(reparsed_concrete), 원문이 정말 애매 -> vague_confirmed
     (반대로 underspecified가 아닌데 애매해 보이면 vague_suspected로 기록만 함. 켜면 시험이 ELIGIBLE이 될 수 없으므로)
   - 규칙 엔진 필드가 원문과 다름 -> 필드(단위 포함)를 비워 LLM이 원문으로 판정.
     단, 원문 범위('18-100 years')를 하한·상한 두 규칙으로 나눈 것이고 값·단위가 원문과 맞으면 지우지 않음
   - 누락된 기준 -> 원문 문장 그대로일 때만 규칙으로 추가
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field

from .. import config, evidence, llm
from ..schemas import ParsedTrial, RequiredEvidence, Rule, RuleCategory, RuleMeta
from .criteria_parser import _render_trial, demote_engine_fields

SYSTEM = """당신은 임상시험 기준 파싱 결과를 원문과 대조하는 '파싱 검증 에이전트'입니다.
다른 에이전트가 원문 선정(Inclusion)·제외(Exclusion) 기준을 원자 규칙으로 나눴습니다. 그중 오류 위험이 큰 규칙만 골라 드립니다.
각 규칙이 원문과 같은 뜻인지 확인합니다. 규칙을 새로 쓰지 말고, 아래 항목만 판정합니다.

- meaning_ok: 규칙의 text와 kind(inclusion/exclusion)가 원문 기준의 뜻을 바꾸지 않았는가.
  false인 예: 원문의 제외 기준을 선정 기준으로 둠, "A and B"를 A만 남김, 수치·기간·대상 조건을 빠뜨리거나 바꿈, 원문에 없는 조건을 추가.
  원문 일부를 원자 조건으로 나눈 것 자체는 정상입니다. 제외 기준 규칙은 '제외 사유가 되는 상태'를 서술하는 것이 정상입니다.
- raw_is_vague: 환자 정보가 완벽하게 주어져도 공개 원문만으로는 판정 기준이 정해지지 않는가.
  true: "Use of certain medications"(목록 없음), "clinically significant abnormalities"(기준값 없음), 공개되지 않은 프로토콜 절·목록 참조,
  판단 근거 없이 연구자 의견에만 맡긴 기준.
  false: 구체적인 질환·소견·약물·기간·수치가 적혀 있어 환자 정보만 있으면 판정 가능한 기준(예시가 붙은 연구자 판단 기준 포함).
  판정에 필요한 환자 정보가 부족할 것 같다는 이유로 true로 하지 않습니다.
- category: 이 기준의 올바른 분류. consent_or_logistics는 동의·방문·순응 같은 절차·행정 기준에만 씁니다(임상적 상태는 해당 없음).
- structured_ok: field/operator/value/unit이 원문과 같은 의미인가. same_sentence_rules는 같은 원문 문장을 나눈 다른 규칙입니다.
  범위('18-100 years')를 하한·상한 두 규칙으로 나눈 것처럼 함께 보면 원문과 같은 뜻이면 true입니다. 예: 원문 "ANC ≥ 1500/µL"인데 value가 1.5이고 unit이 없으면 false,
  "lab:egfr"가 신장 기능(eGFR)인지 EGFR 유전자 변이인지 원문과 다르면 false. 구조화 필드가 없으면 true.
- required_evidence: 이 기준을 판정할 때 필요한 근거.
  self_report_ok = 환자·보호자 답변으로 충분(나이, 복용 약, 과거 수술 등), clinical_record = 검사 결과지·병리 보고서 등 진료 기록이 필요.
- note: 판정 이유를 한국어 한 문장으로.

<coverage_gaps>에는 어떤 규칙에도 담기지 않은 것으로 보이는 원문 문장이 있습니다.
실제로 판정에 필요한 기준인데 규칙에 빠졌으면 missing에 넣습니다. text는 원문 문장을 그대로 복사하고(번역·요약 금지), 이미 다른 규칙에 담겼거나 기준이 아닌 문장(제목, 설명)은 넣지 않습니다.

<rules_to_check>의 모든 rule_id에 대해 정확히 하나씩 checks를 반환합니다.
"""


class RuleCheck(BaseModel):
    rule_id: str
    meaning_ok: bool
    raw_is_vague: bool
    category: RuleCategory
    structured_ok: bool
    required_evidence: RequiredEvidence
    note: str = Field(description="한국어 한 문장")


class MissingCriterion(BaseModel):
    kind: Literal["inclusion", "exclusion"]
    text: str = Field(description="원문 문장 그대로")
    category: RuleCategory


class ValidationOutput(BaseModel):
    checks: list[RuleCheck]
    missing: list[MissingCriterion]


class ValidationReport(BaseModel):
    """사전 컴파일 기록 (data/parsed/<trial_id>.json의 validation)."""
    deterministic: dict[str, list[str]] = {}  # rule_id -> 결정적 검사에서 나온 문제
    risk: dict[str, list[str]] = {}           # rule_id -> 위험 사유 (LLM 검증 대상)
    coverage_gaps: list[str] = []
    llm_checked: list[str] = []
    changes: list[str] = []                   # 코드가 실제로 바꾼 내용
    review: dict[str, str] = {}               # rule_id -> 자동 판정하지 않는 사유
    added_rules: list[str] = []
    evidence_conflicts: list[str] = []
    notes: list[str] = []                     # 적용하지 않은 검증 의견
    llm_output: dict | None = None            # 검증 LLM 원본 응답 (재현·감사용)


# ---------------------------------------------------------------------------
# 1) 결정적 검사
# ---------------------------------------------------------------------------

_STOP = {"the", "and", "with", "for", "who", "are", "has", "have", "had", "been", "any", "not", "or", "of", "to",
         "in", "on", "at", "by", "be", "is", "as", "an", "a", "than", "that", "this", "from", "within", "their",
         "patients", "patient", "participants", "participant", "subjects", "subject", "must", "will", "should"}
_WORD = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
_EXCL_HEADER = re.compile(r"exclusion\s+criteria", re.I)
_INCL_HEADER = re.compile(r"inclusion\s+criteria", re.I)
_SUPPORTED_OPS = {">=", "<=", ">", "<", "==", "!="}


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if len(w) > 1 and w not in _STOP}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def split_sections(criteria: str) -> tuple[str, str]:
    """원문 기준을 (선정 절, 제외 절)로 나눔. 제외 절 머리말이 없으면 전부 선정 절."""
    m = _EXCL_HEADER.search(criteria or "")
    if not m:
        return criteria or "", ""
    return criteria[:m.start()], criteria[m.start():]


def criterion_lines(criteria: str) -> list[tuple[str, str]]:
    """원문 기준 문장 목록 [(kind, 문장)]. 머리말·짧은 줄은 뺌."""
    out = []
    inc, exc = split_sections(criteria)
    for kind, block in (("inclusion", inc), ("exclusion", exc)):
        for line in block.splitlines():
            line = re.sub(r"^\s*(?:[-*•·]|\d+[.)]|[a-z][.)])\s*", "", line).strip()
            if len(_tokens(line)) < 3 or _INCL_HEADER.fullmatch(line.rstrip(":")) or _EXCL_HEADER.fullmatch(line.rstrip(":")):
                continue
            out.append((kind, line))
    return out


def coverage_gaps(parsed: ParsedTrial, criteria: str, threshold: float = 0.5) -> list[str]:
    """어떤 규칙에도 담기지 않은 원문 문장.

    문장 단어 중 '그 문장과 많이 겹치는 규칙들'의 단어로 덮이는 비율이 threshold 미만이면 누락 후보로 봅니다.
    """
    rule_tokens = [(r.kind, _tokens(r.text)) for r in parsed.rules]
    gaps = []
    for kind, line in criterion_lines(criteria):
        lt = _tokens(line)
        covered: set[str] = set()
        for rk, rt in rule_tokens:
            if rk != kind or not rt:
                continue
            shared = lt & rt
            if len(shared) >= 0.5 * len(rt) or len(shared) >= 0.5 * len(lt):
                covered |= shared
        if len(covered) < threshold * len(lt):
            gaps.append(f"[{kind}] {line}")
    return gaps


def _section_mismatch(rule: Rule, inc: str, exc: str) -> bool:
    """규칙 문장이 원문의 반대 절에만 있는가 (CT.gov 구조화 나이·성별 규칙은 본문에 없으므로 제외)."""
    if rule.field in ("age", "sex") or not exc:
        return False
    rt = _tokens(rule.text)
    if len(rt) < 3:
        return False
    in_inc = len(rt & _tokens(inc)) / len(rt)
    in_exc = len(rt & _tokens(exc)) / len(rt)
    own, other = (in_inc, in_exc) if rule.kind == "inclusion" else (in_exc, in_inc)
    return other >= 0.9 and own < 0.5


def deterministic_check(parsed: ParsedTrial, trial: dict) -> dict[str, list[str]]:
    """반환: rule_id -> 문제 목록. 규칙 엔진에 쓸 수 없는 구조화 필드는 이 자리에서 비웁니다."""
    issues: dict[str, list[str]] = {}

    def add(rid: str, msg: str) -> None:
        issues.setdefault(rid, []).append(msg)

    for line in parsed.integrity_issues:  # check_integrity가 이미 필드를 비운 규칙
        add(line.split(":", 1)[0], "무결성: " + line.split(":", 1)[-1].strip())
    inc, exc = split_sections(trial.get("eligibility_criteria") or "")
    for r in parsed.rules:
        if r.field is not None:
            f = r.field.strip().lower()
            bad = None
            if not (f in ("age", "sex", "ecog") or f.startswith("lab:")):
                bad = f"알 수 없는 규칙 엔진 필드 '{r.field}'"
            elif r.operator not in _SUPPORTED_OPS:
                bad = f"규칙 엔진이 처리하지 않는 연산자 '{r.operator}'"
            elif f == "sex" and (r.value or "").lower() not in ("male", "female", "all"):
                bad = f"성별 값 '{r.value}'"
            if bad:
                add(r.rule_id, bad + " -> LLM 판정으로 전환")
                demote_engine_fields(r)
        if _section_mismatch(r, inc, exc):
            add(r.rule_id, f"선정/제외 방향: {r.kind} 규칙인데 문장이 원문의 반대 절에만 있음")
        if r.cohort is not None and r.cohort not in parsed.cohorts:
            add(r.rule_id, f"코호트 '{r.cohort}'가 cohorts 목록에 없음")
    return issues


# ---------------------------------------------------------------------------
# 2) 위험 항목 탐지
# ---------------------------------------------------------------------------

def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _attrs(r: Rule) -> dict:
    return {"underspecified": r.underspecified, "kind": r.kind,
            "engine": (r.field, r.operator, r.value), "logistics": r.category == "consent_or_logistics"}


def detect_risk(parsed: ParsedTrial, det: dict[str, list[str]],
                other_runs: list[ParsedTrial] | None = None) -> dict[str, list[str]]:
    risk: dict[str, list[str]] = {}

    def add(rid: str, msg: str) -> None:
        risk.setdefault(rid, []).append(msg)

    # 같은 문장을 여러 규칙으로 나눈 경우(범위의 하한·상한 등)가 있으므로 문장마다 규칙 목록으로 비교
    others: list[dict[str, list[Rule]]] = []
    for p in other_runs or []:
        by_text: dict[str, list[Rule]] = {}
        for x in p.rules:
            by_text.setdefault(_key(x.text), []).append(x)
        others.append(by_text)
    for r in parsed.rules:
        if r.underspecified:
            add(r.rule_id, "underspecified (파서 한 번의 판단)")
        if r.category == "consent_or_logistics" and r.kind == "inclusion":
            add(r.rule_id, "절차 선정 기준 (미확인이어도 적격을 막지 않음)")
        if r.field and (r.field.lower().startswith("lab:") or r.field.lower() == "ecog"):
            add(r.rule_id, "규칙 엔진 확정 판정 (검사·ECOG)")
        if r.category == "other":
            add(r.rule_id, "category=other (근거 수준을 유형으로 정할 수 없음)")
        for msg in det.get(r.rule_id, []):
            if msg.startswith(("선정/제외 방향", "코호트")):
                add(r.rule_id, msg)
        mine = _attrs(r)
        for run in others:
            alts = run.get(_key(r.text))
            if not alts:
                continue
            diffs = [[k for k, v in _attrs(alt).items() if v != mine[k]] for alt in alts]
            if all(diffs):  # 같은 문장의 규칙 중 속성이 같은 것이 하나도 없음
                add(r.rule_id, f"다른 파싱 실행과 다름({', '.join(min(diffs, key=len))})")
                break
    return risk


# ---------------------------------------------------------------------------
# 3) LLM 의미 검증 + 4) 적용
# ---------------------------------------------------------------------------

def _rule_view(r: Rule, reasons: list[str], rules: list[Rule] = ()) -> dict:
    view = {"rule_id": r.rule_id, "kind": r.kind, "text": r.text, "category": r.category,
            "field": r.field, "operator": r.operator, "value": r.value, "unit": r.unit,
            "underspecified": r.underspecified, "cohort": r.cohort, "risk": reasons}
    # 같은 원문 문장을 나눈 다른 규칙: 한쪽만 보고 '상한 누락' 등으로 오판하지 않도록 함께 보여 줌
    same = [{"rule_id": o.rule_id, "field": o.field, "operator": o.operator, "value": o.value, "unit": o.unit}
            for o in rules if o is not r and _norm(o.text) == _norm(r.text)]
    if same:
        view["same_sentence_rules"] = same
    return view


_RANGE_BOUNDS = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(?:-|–|~|\bto\b)\s*(\d[\d,]*(?:\.\d+)?)", re.I)


def _num(s: str | None) -> float | None:
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def _range_partner(r: Rule, rules: list[Rule]) -> Rule | None:
    """r이 원문 범위 'lo-hi'를 하한(>= lo)·상한(<= hi) 두 규칙으로 나눈 것 중 하나이고 값·단위가 원문과 맞으면 짝 규칙.

    검증 LLM이 한쪽 규칙만 보고 '상한(하한) 누락'으로 판정해 올바른 필드를 지우는 것을 막습니다(코드로 확인될 때만).
    """
    if not r.field or not r.unit or r.operator not in (">=", "<="):
        return None
    lower = r.operator == ">="
    value = _num(r.value)
    unit = re.sub(r"\s+", "", r.unit.lower()).rstrip("s")
    for m in _RANGE_BOUNDS.finditer(r.text):
        lo, hi = _num(m.group(1)), _num(m.group(2))
        if lo is None or hi is None or lo >= hi or value != (lo if lower else hi):
            continue
        if unit not in re.sub(r"\s+", "", r.text[m.end():].lower())[:20]:  # 단위가 범위 바로 뒤에 적혀 있어야 함
            continue
        want_op, want_value = ("<=", hi) if lower else (">=", lo)
        for o in rules:
            if (o is not r and _norm(o.text) == _norm(r.text) and o.field == r.field and o.unit == r.unit
                    and o.operator == want_op and _num(o.value) == want_value):
                return o
    return None


def _llm_validate(parsed: ParsedTrial, trial: dict, risk: dict[str, list[str]], gaps: list[str]) -> ValidationOutput:
    rules = [_rule_view(r, risk[r.rule_id], parsed.rules) for r in parsed.rules if r.rule_id in risk]
    user = f"""{_render_trial(trial)}

<rules_to_check>
{json.dumps(rules, ensure_ascii=False, indent=1)}
</rules_to_check>

<coverage_gaps>
{chr(10).join(gaps) or "(없음)"}
</coverage_gaps>

위 규칙을 원문과 대조해 판정하세요."""
    return llm.structured(ValidationOutput, system=SYSTEM, user=user, effort=config.EFFORT["parser_validator"])


def _next_id(parsed: ParsedTrial, kind: str) -> str:
    prefix = "I" if kind == "inclusion" else "E"
    nums = [int(r.rule_id[1:]) for r in parsed.rules if r.rule_id[:1] == prefix and r.rule_id[1:].isdigit()]
    return f"{prefix}{max(nums, default=0) + 1}"


def apply_checks(parsed: ParsedTrial, trial: dict, risk: dict[str, list[str]],
                 out: ValidationOutput, report: ValidationReport) -> dict[str, RuleMeta]:
    """LLM 검증 결과를 코드 규칙으로 적용. 반환: rule_id -> RuleMeta."""
    by_id = {r.rule_id: r for r in parsed.rules}
    meta = {r.rule_id: RuleMeta() for r in parsed.rules}
    proposals: dict[str, RequiredEvidence] = {}
    checks: dict[str, RuleCheck] = {}
    for c in out.checks:
        if c.rule_id in risk and c.rule_id not in checks:  # 화이트리스트 + 중복 제거
            checks[c.rule_id] = c
    report.llm_checked = sorted(checks)

    for rid, reasons in risk.items():
        r, c = by_id[rid], checks.get(rid)
        if c is None:
            # 검증 응답이 빠진 규칙: 방향 불일치처럼 심각한 문제만 보수적으로 의사 확인
            if any(x.startswith("선정/제외 방향") for x in reasons):
                meta[rid].review = "선정/제외 방향이 원문과 다르고 의미 검증을 받지 못함"
            continue
        proposals[rid] = c.required_evidence
        if not c.meaning_ok:
            meta[rid].review = f"파싱 결과가 원문과 뜻이 다름: {c.note}"
        # D6: underspecified는 원문이 정말 애매한지로 다시 정함
        if r.underspecified and not c.raw_is_vague:
            r.underspecified = False
            meta[rid].underspecified_state = "reparsed_concrete"
            report.changes.append(f"{rid}: underspecified -> false (원문 기준이 구체적, 규칙 단위 재분류)")
        elif r.underspecified:
            meta[rid].underspecified_state = "vague_confirmed"
        elif c.raw_is_vague:
            # 반대 방향(false -> true)은 자동 적용하지 않음: underspecified 기준은 ELIGIBLE을 막고 확인 질문 대상에서도
            # 빠지므로, 검증 LLM 한 번의 판단으로 시험을 적격 불가로 만들지 않도록 기록만 남김
            meta[rid].underspecified_state = "vague_suspected"
            report.notes.append(f"{rid}: 원문이 애매할 수 있음(적용 안 함) - {c.note}")
        if r.field and not c.structured_ok:
            partner = _range_partner(r, parsed.rules)
            if partner is not None:
                report.notes.append(f"{rid}: 규칙 엔진 필드 유지 ({r.field} {r.operator} {r.value} {r.unit}) - 원문 범위를 나눈 "
                                    f"규칙이고 반대쪽 경계는 {partner.rule_id}에 있음(검증 판단 미적용: {c.note})")
            else:
                report.changes.append(f"{rid}: 규칙 엔진 필드 제거 ({r.field} {r.operator} {r.value} {r.unit or ''}) - {c.note}")
                demote_engine_fields(r)
        if c.category != r.category:
            if c.category == "consent_or_logistics":
                pass  # 새로 '절차'로 바꾸면 미확인 기준이 적격을 막지 않게 되므로 받아들이지 않음
            else:
                report.changes.append(f"{rid}: category {r.category} -> {c.category}")
                r.category = c.category

    # 누락 기준 추가: 원문 문장 그대로일 때만
    source = _norm(trial.get("eligibility_criteria") or "")
    for m in out.missing:
        text = m.text.strip()
        if len(text) < 15 or _norm(text) not in source:
            continue
        if any(_norm(text) == _norm(r.text) for r in parsed.rules):
            continue
        rid = _next_id(parsed, m.kind)
        rule = Rule(rule_id=rid, kind=m.kind, text=text, category=m.category, field=None, operator=None,
                    value=None, unit=None, time_window_days=None, structured_evaluable=False,
                    underspecified=False, cohort=None)
        parsed.rules.append(rule)
        meta[rid] = RuleMeta(added_by_validator=True)
        report.added_rules.append(f"{rid}: {text[:120]}")

    for r in parsed.rules:
        level, conflict = evidence.required_evidence(r, proposals.get(r.rule_id))
        meta[r.rule_id].required_evidence = level
        if conflict:
            report.evidence_conflicts.append(f"{r.rule_id}: {conflict}")
    report.review = {rid: m.review for rid, m in meta.items() if m.review}
    return meta


def validate(parsed: ParsedTrial, trial: dict, other_runs: list[ParsedTrial] | None = None,
             *, use_llm: bool = True) -> tuple[ParsedTrial, dict[str, RuleMeta], ValidationReport]:
    """파싱 결과 검증. parsed를 제자리에서 고치고 (parsed, 규칙 메타, 기록)을 반환."""
    report = ValidationReport()
    report.deterministic = deterministic_check(parsed, trial)
    report.coverage_gaps = coverage_gaps(parsed, trial.get("eligibility_criteria") or "")
    report.risk = detect_risk(parsed, report.deterministic, other_runs)
    if use_llm and (report.risk or report.coverage_gaps):
        out = _llm_validate(parsed, trial, report.risk, report.coverage_gaps)
        report.llm_output = out.model_dump()
    else:
        out = ValidationOutput(checks=[], missing=[])
    meta = apply_checks(parsed, trial, report.risk, out, report)
    parsed.rule_meta = meta
    return parsed, meta, report
