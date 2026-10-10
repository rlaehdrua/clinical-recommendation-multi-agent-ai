"""기준 파싱 에이전트: 선정/제외 기준 + 상세 설명 -> 구조화된 규칙(ParsedTrial)."""

from __future__ import annotations

import hashlib
import json
import re
import threading

from .. import config, llm
from ..schemas import ParsedTrial, Rule

SYSTEM = """당신은 임상시험 프로토콜을 분석하는 '기준 파싱 에이전트'입니다.
입력으로 받은 임상시험의 선정(Inclusion)·제외(Exclusion) 기준과 상세 설명을, 다른 에이전트가 환자 정보와 하나씩 대조할 수 있는 원자 규칙 목록으로 변환합니다.

규칙 작성 원칙:
- 한 규칙에는 하나의 판정 가능한 조건만 담습니다. "A and B"처럼 여러 조건이 묶인 기준은 나눕니다. "A or B"는 하나의 규칙으로 두고 text에 그대로 적습니다.
- text에는 원문 표현을 최대한 그대로 남겨 근거 추적이 가능하게 합니다.
- rule_id는 선정 기준 I1, I2, ..., 제외 기준 E1, E2, ... 순서로 매깁니다.
- 제외 기준의 규칙은 '제외 사유가 되는 상태' 자체를 서술합니다(예: "활동성 뇌전이가 있음"). 판정 시 이 상태가 성립하면 제외됩니다.
- CT.gov 구조화 필드(성별, 최소/최대 나이)가 주어지면 본문에 없더라도 규칙으로 포함합니다.
- "동의서 작성 가능", "추적관찰 순응" 같은 절차·행정적 기준은 category="consent_or_logistics"로 표시합니다.

규칙 엔진 필드(field/operator/value)는 아래 경우에만 채우고, 나머지는 null로 두며 structured_evaluable=false로 합니다.
- field="age": 나이(년). value는 숫자 문자열.
- field="sex": value는 "male" | "female" | "all", operator는 "==".
- field="ecog": ECOG 수행 상태 숫자. Karnofsky 등 다른 척도는 변환하지 말고 null로 둡니다.
- field="lab:<영문 소문자 표준 검사명>": 예) "lab:hemoglobin", "lab:platelets", "lab:absolute neutrophil count", "lab:creatinine", "lab:total bilirubin", "lab:ast", "lab:alt", "lab:hba1c". value는 숫자 문자열, unit은 원문 단위. "정상 상한의 1.5배(ULN)"처럼 기준값이 상대적이면 null로 둡니다.
structured_evaluable=true는 위 필드와 숫자/범주 값이 모두 명확할 때만 사용합니다.

underspecified=true는 "환자 정보가 완벽하게 주어져도 공개 원문만으로는 판정할 수 없는" 기준에만 표시합니다(드물게 사용).
- true인 예: "Use of certain medications"(약물 목록이 원문에 없음), "clinically significant laboratory abnormalities"(기준값 없음),
  "prohibited medications in this study"·"see §6.1 of the protocol"처럼 공개되지 않은 목록/절 참조,
  "other protocol-defined criteria may apply", 판단 근거 없이 "in the opinion of the investigator"에만 맡긴 기준
- false인 예(구체적인 질환·소견·약물·기간이 적혀 있어 환자 정보만 있으면 판정 가능):
  "Crohn's disease, ulcerative colitis", "vitreous hemorrhage", "recent onset macula-involving retinal detachment",
  "severe pancreatic disorders", "treated with immunosuppressants within 48 weeks", "unable to lie supine for 1 hour",
  "e.g. unstable diabetes"처럼 예시가 붙은 연구자 판단 기준
- 판정이 어렵거나 정보가 부족할 것 같다는 이유만으로 true로 하지 않습니다(그건 매칭 단계의 unknown으로 처리됩니다).

코호트(군)별 기준:
- 원문이 "eligible for the IM group" / "for the control group" / "Cohort A" / "Part 1"처럼 참여자 군마다 다른 기준을 두면,
  cohorts에 군 이름을 나열하고 각 규칙의 cohort에 해당 군 이름(cohorts 값과 정확히 동일)을 적습니다.
- "(control group only)"처럼 특정 군에만 적용된다고 적힌 제외 기준도 그 군의 cohort로 표시합니다.
- 모든 참여자에게 적용되는 기준은 cohort=null입니다. 군이 나뉘지 않은 시험은 cohorts=[]이고 모든 cohort=null입니다.
- 군 구분이 "무작위 배정 arm"일 뿐 기준이 같다면 코호트로 나누지 않습니다.
"""


def _render_trial(trial: dict) -> str:
    interventions = "; ".join(f"{i.get('type')}: {i.get('name')}" for i in trial.get("interventions", []))
    return f"""<trial>
trial_id: {trial['trial_id']}
title: {trial.get('title', '')}
conditions: {', '.join(trial.get('conditions', []))}
phases: {', '.join(trial.get('phases', []))}
interventions: {interventions}
structured_eligibility: sex={trial.get('sex')}, minimum_age={trial.get('minimum_age')}, maximum_age={trial.get('maximum_age')}, healthy_volunteers={trial.get('healthy_volunteers')}

<brief_summary>
{trial.get('brief_summary', '')}
</brief_summary>

<detailed_description>
{trial.get('detailed_description', '')}
</detailed_description>

<eligibility_criteria>
{trial.get('eligibility_criteria', '')}
</eligibility_criteria>
</trial>"""


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def parse_trial(trial: dict, *, use_cache: bool = True) -> ParsedTrial:
    rendered = _render_trial(trial)
    key = hashlib.sha256((config.MODEL + SYSTEM + rendered).encode()).hexdigest()[:16]
    cache_path = config.CACHE_DIR / "parsed" / f"{trial['trial_id']}_{key}.json"
    # 여러 환자를 동시에 처리할 때 같은 시험을 중복 파싱하지 않도록 시험별 잠금
    with _locks_guard:
        lock = _locks.setdefault(str(cache_path), threading.Lock())
    with lock:
        parsed = _parse_trial_locked(trial, rendered, cache_path, use_cache)
    # 캐시에는 LLM 원본을 두고, 불러올 때마다 검사 (검사 규칙이 바뀌어도 캐시를 버릴 필요 없음)
    parsed.integrity_issues = check_integrity(parsed, trial)
    return parsed


def _parse_trial_locked(trial: dict, rendered: str, cache_path, use_cache: bool) -> ParsedTrial:
    if use_cache and cache_path.exists():
        return ParsedTrial.model_validate_json(cache_path.read_text(encoding="utf-8"))

    parsed = llm.structured(
        ParsedTrial,
        system=SYSTEM,
        user=rendered + "\n\n위 임상시험의 기준을 원자 규칙으로 구조화하세요.",
        effort=config.EFFORT["criteria_parser"],
    )
    parsed.trial_id = trial["trial_id"]
    _renumber(parsed)

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(parsed.model_dump_json(indent=2), encoding="utf-8")
    return parsed


def _renumber(parsed: ParsedTrial) -> None:
    """rule_id 중복/누락을 방지하기 위해 kind별로 다시 번호를 매김."""
    counters = {"inclusion": 0, "exclusion": 0}
    for rule in parsed.rules:
        counters[rule.kind] += 1
        rule.rule_id = f"{'I' if rule.kind == 'inclusion' else 'E'}{counters[rule.kind]}"


# ---------------------------------------------------------------------------
# 기계적 무결성 검사: 규칙 엔진이 쓸 값(field/operator/value)이 원문과 맞는지 코드로 확인
# ---------------------------------------------------------------------------

_NUM = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?|\.\d+")
_RANGE = re.compile(r"\d\s*(?:-|–|~|to)\s*[<>≤≥]?\s*\d", re.I)
_UP = re.compile(r"≥|>|\bat least\b|\bor older\b|\bor more\b|\bor greater\b|\bminimum\b|\bno less than\b"
                 r"|\bgreater than\b|\bmore than\b|\bolder than\b|\bexceed(?:s|ing)?\b|\babove\b|\bover\b|이상|초과", re.I)
_DOWN = re.compile(r"≤|<|\bat most\b|\bor younger\b|\bor less\b|\bmaximum\b|\bno more than\b|\bup to\b"
                   r"|\bless than\b|\byounger than\b|\bunder\b|\bbelow\b|이하|미만", re.I)
_OP_DIRECTION = {">=": "up", ">": "up", "<=": "down", "<": "down"}


def numbers_in(text: str) -> set[float]:
    return {float(n.replace(",", "")) for n in _NUM.findall(text or "")}


def _integrity_problem(rule: Rule, source_numbers: set[float]) -> str | None:
    """규칙 엔진용 값이 원문과 어긋나면 사유를 반환."""
    if rule.field in (None, "sex") or rule.value is None:
        return None
    try:
        value = float(rule.value)
    except ValueError:
        return f"비교값 '{rule.value}'이 숫자가 아님"
    # 수치 일치: 비교값이 기준 문장(또는 CT.gov 구조화 나이 필드)에 그대로 있어야 함. 단위 환산값은 인정하지 않음
    if value not in numbers_in(rule.text) | source_numbers:
        return f"비교값 {rule.value}이 원문에 없음"
    # 부등호 방향: 문장에 한 방향 표현만 있을 때 operator 방향과 비교 (범위 표현 '18-65', '18 to <65'는 건너뜀)
    direction = _OP_DIRECTION.get(rule.operator or "")
    if direction and not _RANGE.search(rule.text):
        up, down = bool(_UP.search(rule.text)), bool(_DOWN.search(rule.text))
        if up != down:
            stated = "up" if up else "down"
            if stated != direction:
                return f"부등호 '{rule.operator}'가 원문 방향({'이상/초과' if up else '이하/미만'})과 반대"
    return None


def check_integrity(parsed: ParsedTrial, trial: dict) -> list[str]:
    """원문과 어긋난 규칙은 규칙 엔진 대상에서 빼고(LLM이 원문으로 판정) 사유 목록을 반환.

    LLM 파서가 'ANC >= 1.5'를 'ANC >= 1.0'으로 바꾸면 스키마는 맞아도 의미가 틀립니다.
    이런 규칙을 규칙 엔진이 그대로 판정하지 않도록, 원문에 없는 수치나 반대 방향 부등호는 구조화 필드를 비웁니다.
    """
    source_numbers = numbers_in(f"{trial.get('minimum_age') or ''} {trial.get('maximum_age') or ''}")
    issues = []
    for rule in parsed.rules:
        problem = _integrity_problem(rule, source_numbers)
        if problem is None:
            continue
        issues.append(f"{rule.rule_id}: {problem} ({rule.field} {rule.operator} {rule.value}) -> LLM 판정으로 전환")
        rule.field = rule.operator = rule.value = None
        rule.structured_evaluable = False
    if issues:
        parsed.parsing_notes += "\n[코드 검증] " + " / ".join(issues)
    return issues


def rules_json(parsed: ParsedTrial) -> str:
    return json.dumps([r.model_dump() for r in parsed.rules], ensure_ascii=False, indent=1)
