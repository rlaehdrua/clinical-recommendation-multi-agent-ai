"""검사값의 검체 종류: 검사명이 같아도 검체가 다르면 같은 검사 사실이 아님.

수치·단위·시점·주체·유효성과 별개의 축입니다. 값과 기준을 비교한 결과는 셋 중 하나입니다.

- `match`    : 값의 검체가 기준이 요구하는 검체와 같음
- `mismatch` : 값의 검체가 다름. 이 기준의 값이 아니므로 근거에서 빼고 충돌 계산에도 넣지 않음
- `unclear`  : 값의 검체를 알 수 없고 이 검사는 검체를 추정할 수 없음. 값은 남기되 확정 근거로 쓰지 않음

값의 검체 (`of_value`)
1. 검사명에 붙은 표시: "Urine creatinine 140", "creatinine (CSF) 30", "혈청 알부민 3.5"
2. 수치 뒤의 같은 절: "creatinine 140 mg/dL (spot urine ...)"
3. 같은 구간의 머리글: "CSF analysis: glucose 60, protein 45", "Renal: Cr 1.0"
4. 표시가 없을 때의 문맥: 혈액 채취·혈액 묶음 검사가 있고 혈액이 아닌 검체 언급이 없을 때만 혈액. 그 밖에는 모름.

기준의 검체 (`required`): 기준 항목 이름과 기준 문장에 적힌 검체. 적혀 있지 않으면 혈액.

`MULTI_SPECIMEN`은 같은 이름·같은 단위로 혈액과 다른 체액을 모두 재는 검사입니다. 이 검사는 검체를 알 수 없으면 `unclear`입니다.
그 밖의 검사(Hb, 혈소판 등)는 표시가 없으면 혈액 검사로 읽고, 다른 검체가 명시될 때만 `mismatch`입니다.
serum, plasma, whole blood는 구별하지 않고 혈액으로 봅니다. 이 목록과 표현은 임상 검수를 받지 않았습니다.
"""

from __future__ import annotations

import re

BLOOD, URINE, CSF, OTHER_FLUID = "blood", "urine", "csf", "other_fluid"
UNCLEAR = "unclear"  # 사실 저장소: 원문을 확인했지만 검체를 알 수 없음. None은 확인한 적 없음(구조화 입력)
MATCH, MISMATCH = "match", "mismatch"
NO_SPECIMEN = frozenset({"creatinine clearance", "egfr (renal)", "lvef"})  # 계산값·영상값: 한 검체의 측정값이 아님 ("24h urine CrCl")
LABEL = {URINE: "urine", CSF: "csf", OTHER_FLUID: "body fluid", BLOOD: "blood"}  # 다른 검체 값의 사실 항목 이름에 붙이는 말

MULTI_SPECIMEN = frozenset({"creatinine", "albumin", "protein", "total protein", "glucose", "ldh", "lactate dehydrogenase", "amylase"})

_NOT_SAMPLE = r"(?!\s+(?:pressure|culture|cell|cells|transfusion|group|type|loss|products?))"  # "blood pressure"는 검체가 아님
_TERMS = {
    URINE: r"urine(?!\s+(?:output|retention|culture))|urinary(?!\s+(?:tract|retention|incontinence|symptoms?|output))|urinalysis|\bua\b|u/a|\bupcr\b|\buacr\b|\b24\s*-?\s*h(?:ou)?r?s?\s+collection\b|\bspot\s+(?:sample|collection)\b|소변|(?<![가-힣])뇨(?=\s|[가-힣]{0,2}검사)|(?<![가-힣])요(?=\s*(?:크레아티닌|단백|알부민|검사|화학))",
    CSF: r"\bcsf\b|cerebrospinal|spinal\s+fluid|lumbar\s+puncture|\blp\b|뇌척수액|척수액|요추\s*천자",
    OTHER_FLUID: r"pleural|thoracentesis|ascit\w*|peritoneal|paracentesis|dialysate|drain(?:age)?\s+fluid|effusion|bronchoalveolar|\bbal\b|synovial|pericardial|\bsaliva\b|\bsputum\b|\bstool\b|흉수|복수(?![가-힣]*(?:의|개))|늑막|객담|대변",
    BLOOD: rf"serum|plasma|whole\s+blood|(?<!white\s)(?<!red\s)\bblood\b{_NOT_SAMPLE}|\bs-?cr\b|혈청|혈장|전혈|혈액",
}
_SPECIMEN = {k: re.compile(v, re.IGNORECASE) for k, v in _TERMS.items()}
_ANY = re.compile("|".join(f"(?P<{k}>{v})" for k, v in _TERMS.items()), re.IGNORECASE)
_BLOOD_CONTEXT = re.compile(rf"(?<!white\s)(?<!red\s)\bbloods?\b{_NOT_SAMPLE}|\b(?:re)?draw(?:n|s)?\b|\bvenipuncture\b|\bphlebotomy\b|\bcbc\b|\bbmp\b|\bcmp\b|\bchem(?:istry|\s*\d+)?\b|\b[lkr]fts?\b|"
                            r"\b(?:renal|liver|metabolic)\s+(?:panel|function|profile)\b|^\s*renal\s*[:：]|채혈|피\s*검사|혈액\s*검사|생화학|간\s*기능|신장?\s*기능", re.IGNORECASE | re.MULTILINE)
_NEXT_ITEM = re.compile(r"[,;\n]|\.(?:\s|$)|\b(?:and|or|with|plus)\b|및|그리고", re.IGNORECASE)
_NAME_BREAK = re.compile(r"[,;:：\n.]")
_NAME_REACH = 24  # 검사명 앞의 수식어를 찾는 글자 수
_RULED_OUT_BEFORE = re.compile(r"\b(?:no|without|denies|negative\s+for)\s+(?:\w+\s+){0,2}$", re.IGNORECASE)  # "no pleural effusion"은 검체가 아니라 소견
_RULED_OUT_AFTER = re.compile(r"\s*(?:은|는|이|가)?\s*(?:없|음성)")


def in_name(name: str) -> str | None:
    """검사명 자체에 적힌 검체: "urine protein"."""
    m = _ANY.search(name or "")
    return m.lastgroup if m else None


def _last(text: str) -> str | None:
    found = list(_ANY.finditer(text))
    return found[-1].lastgroup if found else None


def _attached(text: str, name_lo: int, name_hi: int, lo: int) -> str | None:
    """검사명에 붙은 표시: 이름 안, 이름 앞의 수식어, 이름과 수치 사이."""
    before = text[max(0, name_lo - _NAME_REACH):name_lo]
    cut = max((m.end() for m in _NAME_BREAK.finditer(before)), default=0)
    return in_name(text[name_lo:name_hi]) or _last(before[cut:]) or (in_name(text[name_hi:lo]) if lo >= name_hi else None)


def _after_value(text: str, hi: int) -> str | None:
    """수치 뒤 같은 절의 표시. 괄호 안은 쉼표를 넘어 봄: "140 (spot, urine)". """
    m = re.compile(r"\s*[^\s,;()]*\s*\(([^)]*)\)").match(text, hi)
    if m and in_name(m.group(1)):
        return in_name(m.group(1))
    end = _NEXT_ITEM.search(text, hi)
    return in_name(text[hi:end.start() if end else len(text)])


def _header(text: str, lo: int) -> str | None:
    """같은 구간의 머리글: "CSF analysis: glucose 60, protein 45". 바로 앞 줄이 머리글이면 그 줄도 봄."""
    start = max((m.end() for m in re.finditer(r"[;\n]|\.\s", text[:lo])), default=0)
    colon = max(text.rfind(":", start, lo), text.rfind("：", start, lo))
    if colon >= 0:
        return _last(text[start:colon])
    end = text.rfind("\n", 0, lo)
    prev = text[text.rfind("\n", 0, max(end, 0)) + 1:end].rstrip() if end > 0 else ""
    return _last(prev) if prev.endswith((":", "：")) else None


def of_value(text: str, name_lo: int, name_hi: int, lo: int, hi: int) -> str | None:
    """이 수치의 검체. 명시되거나 문맥으로 확인되지 않으면 None."""
    explicit = _attached(text, name_lo, name_hi, lo) or _after_value(text, hi) or _header(text, lo)
    if explicit:
        return explicit
    mentioned = {m.lastgroup for m in _ANY.finditer(text) if not _RULED_OUT_BEFORE.search(text[:m.start()]) and not _RULED_OUT_AFTER.match(text, m.end())}
    if mentioned - {BLOOD}:
        return None  # 다른 검체가 언급된 본문에서 표시 없는 값은 어느 쪽인지 알 수 없음
    return BLOOD if mentioned or _BLOOD_CONTEXT.search(text) else None


def required(key: str, rule_text: str | None = None) -> str:
    """기준이 요구하는 검체. 기준 항목 이름, 그다음 기준 문장에서 검사명에 붙은 표시. 없으면 혈액."""
    named = in_name(key)
    if named:
        return named
    text = rule_text or ""
    for m in re.finditer(rf"(?<![a-z가-힣]){re.escape(key)}(?![a-z])", text, re.IGNORECASE):
        note = re.compile(r"\s*\(([^)]*)\)").match(text, m.end())
        cue = _attached(text, m.start(), m.end(), m.end()) or (in_name(note.group(1)) if note else None)
        if cue:
            return cue
    return BLOOD


def verdict(key: str, specimen: str | None, wanted: str = BLOOD) -> str:
    """값의 검체와 기준의 검체 비교."""
    if specimen == wanted or key in NO_SPECIMEN:
        return MATCH
    if specimen is not None and specimen != UNCLEAR:
        return MISMATCH
    return UNCLEAR if key in MULTI_SPECIMEN or wanted != BLOOD else MATCH
