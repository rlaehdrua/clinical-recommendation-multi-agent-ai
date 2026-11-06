"""구조화 환자값의 원문 근거 확인: 프로필에 있다는 이유만으로 판정 근거가 되지 않음.

Session은 LLM이 만든 PatientProfile로 규칙 엔진을 돌립니다. 원문에 없는 값이 프로필에 들어오면 규칙 엔진은 그것을 정상
입력으로 처리하므로, 판정을 쓰기 전에 값이 처음 입력이나 연결된 확인 답변에 실제로 적혀 있는지 코드로 확인합니다.

반환 값: None(확인됨), "unlocated"(적혀 있지 않음), "hedged"(추측·모호한 답에만 있음), "conflict"(답변과 값이 충돌).

- 처음 입력의 값은 값 표현 전체로 찾습니다. 대명사와 단독 F/M/여/남은 성별의 근거가 아닙니다.
- 단독 값("62", "여")은 그 값을 직접 물은 문답에서만 인정합니다.
- 검사값은 인용 안의 수치가 그 검사명에 연결되어야 합니다. 인용이 길어도 옆 검사의 수치, 날짜, 참고 범위는 근거가 아닙니다.
- 연결된 수치도 역할을 봅니다(`_role`). 부정, 목표, 가정, 비교식의 기준값, 참고 범위, 다른 사람의 값은 측정값이 아닙니다.
  표현을 다 해석하지는 않습니다. 표지가 있으면 측정값으로 보지 않는 쪽으로 기웁니다.
- 문장 단위로 보므로 "62세인데 혈색소는 몰라요"의 나이는 인정되고 혈색소는 인정되지 않습니다.
- 값의 임상적 의미는 판단하지 않습니다. 적혀 있는지만 봅니다. 문서 검증 여부는 근거 수준(evidence.py) 쪽입니다.
"""

from __future__ import annotations

import re
from datetime import date
from functools import lru_cache

from . import evidence, evidence_directness as ed
from .tools.rule_engine import LAB_ALIASES, _norm, _norm_unit, canonical_lab

UNLOCATED, HEDGED, CONFLICT = "unlocated", "hedged", "conflict"

# 값이 어느 쪽 입력에 있는지 찾는 기본 표현 (matcher의 근거 출처 판별도 같은 표현을 씀)
FIELD_TEXT = {
    "ecog": lambda v: rf"(?:ecog|performance\s*status|\bps\b|수행\s*상태|활동도)[^0-9\n]{{0,20}}(?<!\d){re.escape(v)}(?!\d)",
    "age": lambda v: rf"(?<![\d.]){re.escape(v)}\s*(?:세|살|개월|주|일|years?|yrs?|months?|weeks?|days?|-year|yo\b|y/o)",
    "sex": lambda v: {"female": r"female|\bwoman\b|\bgirl\b|여성|여자|여아",
                      "male": r"(?<!fe)male\b|\bman\b|\bboy\b|남성|남자|남아"}.get(v, r"(?!)"),
}

_RANGE_TAIL = r"(?!\s*[-~–]\s*\d)"  # "ECOG 0-1"은 단일 값의 근거가 아님
_AGE_UNITS = {"years": r"세|살|years?|yrs?|-year|yo\b|y/o", "months": r"개월|달|months?|mos?\b|-month",
              "weeks": r"주|weeks?|wks?|-week", "days": r"일|days?|-day"}
_AGE_SUFFIX = {"years": r"세|살|years?\s*old|yrs?\s*old|-year-old|yo\b|y/o"}
_SEX_MARK = {"female": r"\d\s*/?\s*f\b|성별\s*[:：]?\s*여|\bsex\s*[:=]\s*f", "male": r"\d\s*/?\s*m\b|성별\s*[:：]?\s*남|\bsex\s*[:=]\s*m"}
_BIRTH = re.compile(r"((?:19|20)\d{2})\s*년?\s*(?:생\b|생(?=[ .,이입])|출생)|\bborn\s+(?:in\s+)?((?:19|20)\d{2})|"
                    r"(?:\bdob\b|생년월일)[^0-9\n]{0,6}((?:19|20)\d{2})", re.IGNORECASE)
_APPROX = re.compile(r"쯤|정도|일\s*거|대략|\b(?:about|around|approximately)\b|(?<![가-힣])약\s*\d", re.IGNORECASE)

_TOPIC = {"age": re.compile(r"나이|연세|연령|몇\s*살|\bage\b|how old", re.IGNORECASE),
          "ecog": re.compile(r"ecog|활동|수행|performance", re.IGNORECASE)}
_SEX_QUESTION = re.compile(r"성별|남성.{0,6}여성|여성.{0,6}남성|남자.{0,6}여자|여자.{0,6}남자|\b(?:sex|gender)\b|male or female", re.IGNORECASE)
_BARE_SEX = {"f": "female", "m": "male", "여": "female", "남": "male"}
_LAB_KO = {"hemoglobin": ["혈색소", "헤모글로빈"], "platelets": ["혈소판"], "absolute neutrophil count": ["호중구"],
           "white blood cell count": ["백혈구"], "creatinine": ["크레아티닌"], "total bilirubin": ["빌리루빈"],
           "albumin": ["알부민"], "creatinine clearance": ["크레아티닌 청소율"], "lvef": ["박출률", "구혈률"],
           "potassium": ["칼륨"], "sodium": ["나트륨"], "calcium": ["칼슘"], "magnesium": ["마그네슘"], "glucose": ["혈당"]}
_NUMBER = r"\d+(?:\.\d+)?"

# ---------------------------------------------------------------------------- 수사 표현
_EN_ONES = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
_EN_TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
             "seventeen": 17, "eighteen": 18, "nineteen": 19}
_EN_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_EN_AGE = re.compile(rf"\b(?:({'|'.join(_EN_TENS)})(?:[\s-]({'|'.join(_EN_ONES)}))?|({'|'.join(_EN_TEENS)})|({'|'.join(_EN_ONES)}))"
                     r"(?=[\s-]*(?:years?|yrs?)\b)", re.IGNORECASE)
_KO_DIGIT = {"일": 1, "이": 2, "삼": 3, "사": 4, "오": 5, "육": 6, "칠": 7, "팔": 8, "구": 9}
_KO_AGE = re.compile(r"(?<![가-힣])([이삼사오육칠팔구]?)십([일이삼사오육칠팔구]?)(?=\s*(?:세|살))")
_KO_TENS = {"열": 10, "스물": 20, "스무": 20, "서른": 30, "마흔": 40, "쉰": 50, "예순": 60, "일흔": 70, "여든": 80, "아흔": 90}
_KO_ONES = {"하나": 1, "한": 1, "둘": 2, "두": 2, "셋": 3, "넷": 4, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9}
_KO_NATIVE = re.compile(rf"(?<![가-힣])({'|'.join(_KO_TENS)})\s*({'|'.join(_KO_ONES)}|세(?=\s*살))?(?=\s*(?:살|이에요|이요|입니다|이고|예요|요\b|$))")
_ECOG_WORD = re.compile(r"(ecog(?:\s*ps)?|performance\s*status|\bps\b|수행\s*상태|활동도)(?:\s*(?:of|score|is|:|는|은))?\s+(zero|one|two|three|four)\b",
                        re.IGNORECASE)
_ECOG_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4}


def _en_age(m: re.Match) -> str:
    tens, ones, teen, single = (g.lower() if g else None for g in m.groups())
    n = _EN_TEENS[teen] if teen else _EN_ONES[single] if single else _EN_TENS[tens] + _EN_ONES.get(ones or "", 0)
    return str(n)


def _ko_age(m: re.Match) -> str:
    return str(_KO_DIGIT.get(m.group(1), 1) * 10 + _KO_DIGIT.get(m.group(2), 0))


def _ko_native(m: re.Match) -> str:
    ones = 3 if m.group(2) == "세" else _KO_ONES.get(m.group(2) or "", 0)
    return f"{_KO_TENS[m.group(1)] + ones}살"


def normalize(field: str, text: str) -> str:
    """수사를 숫자로 바꾼 검색용 사본. 원문은 바꾸지 않음."""
    if field == "age":
        return _KO_NATIVE.sub(_ko_native, _KO_AGE.sub(_ko_age, _EN_AGE.sub(_en_age, text)))
    if field == "ecog":
        return _ECOG_WORD.sub(lambda m: f"{m.group(1)} {_ECOG_WORDS[m.group(2).lower()]}", text)
    return text


# ---------------------------------------------------------------------------- 수치의 역할
_NOT_A_RESULT = re.compile(r"\b(?:target|goal|aim|desired|threshold|cut-?off|trigger|maintain|keep|transfus\w*|if|unless|should|in\s+case|provided|"
                           r"assuming|suppose|would|once|until|expected|predicted)\b|목표|유지|역치|수혈(?!\s*없이)|라면|다면|이면|"
                           r"[일인]\s*(?:경우|때)|예상|예정", re.IGNORECASE)
_REFERENCE_BEFORE = re.compile(r"\b(?:reference|ref|normal|range|uln|lln)\b|정상|참고|기준\s*(?:범위|치|값)", re.IGNORECASE)
_COMPARE_BEFORE = re.compile(r"[<>≤≥]=?\s*$")
_COMPARE_AFTER = re.compile(r"\s*[^\s,;()]*\s*(?:이상|이하|미만|초과|or\s+(?:more|less|above|below|greater|higher|lower)|and\s+(?:above|below))", re.IGNORECASE)
_NEGATED_BEFORE = re.compile(r"\b(?:not|never|isn'?t|wasn'?t|no\s+longer|rather\s+than|instead\s+of)\b[^,;]{0,12}$", re.IGNORECASE)
_NEGATED_AFTER = re.compile(r"[^,;]{0,12}?(?:아님|아니|아닌|아닙|않|없(?:음|었|다|습|어))")
_DENIED_AFTER = re.compile(r"\s*(?:가|이|은|는|라는\s*뜻은)?\s*(?:아님|아니|아닌|아닙)")  # 나이·성별·ECOG: 값 바로 뒤의 부정만
_DENIED_BEFORE = re.compile(r"\bnot\s+(?:an?\s+)?$", re.IGNORECASE)
_PAST = re.compile(r"\b(?:previous|prior|former|old|last\s+(?:week|month|year))\b|과거|이전|지난|\d+\s*(?:일|주|개월|달|년)\s*전", re.IGNORECASE)
_OTHER_PERSON = r"아버지|어머니|부친|모친|남편|아내|배우자|형제|자매|보호자|파트너|공여자|\b(?:donor|father|mother|spouse|husband|wife|partner|sibling|brother|sister)\b"
_ABOUT_OTHER = re.compile(_OTHER_PERSON, re.IGNORECASE)
_NEXT_TO_OTHER = re.compile(rf"\s*(?:{_OTHER_PERSON})", re.IGNORECASE)  # 값 바로 뒤: "남성 보호자", "62세 아버지"


def _clause_around(text: str, lo: int, hi: int) -> tuple[str, str]:
    """수치 앞뒤의 같은 절 (줄을 넘지 않음)."""
    start, end = text.rfind("\n", 0, lo) + 1, text.find("\n", hi)
    end = len(text) if end < 0 else end
    for m in ed._CLAUSE_BOUNDARY.finditer(text, start, end):
        if m.end() <= lo:
            start = m.end()
        elif m.start() >= hi:
            end = m.start()
            break
    return text[start:lo], text[hi:end]


def _role(text: str, lo: int, hi: int) -> str | None:
    """검사 수치의 역할: 'measured', 'past', 측정값이 아니면 None."""
    before, after = _clause_around(text, lo, hi)
    if _NOT_A_RESULT.search(before + " " + after) or _ABOUT_OTHER.search(before + " " + after) or _REFERENCE_BEFORE.search(before) \
            or _COMPARE_BEFORE.search(before) or _COMPARE_AFTER.match(after) or _NEGATED_BEFORE.search(before) or _NEGATED_AFTER.match(after):
        return None
    return "past" if _PAST.search(before + " " + after) else "measured"


def _about_patient(text: str, lo: int, hi: int) -> bool:
    """나이·성별·ECOG 값이 환자에 대한 긍정 진술인가. 같은 절의 다른 내용은 보지 않고 값에 붙은 표지만 봅니다."""
    before, after = _clause_around(text, lo, hi)
    return not (_DENIED_AFTER.match(after) or _DENIED_BEFORE.search(before) or _NEXT_TO_OTHER.match(after))


# ---------------------------------------------------------------------------- 값 표현
def _text(value) -> str:
    return f"{value:g}" if isinstance(value, (int, float)) else str(value).strip().lower()


def _value_pattern(field: str, v: str, unit: str | None) -> re.Pattern:
    if field == "age":
        num = rf"(?<![\d.]){re.escape(v)}(?![\d.])"
        pat = rf"{num}\s*(?:{_AGE_UNITS.get(unit or 'years', _AGE_UNITS['years'])})"
        if (unit or "years") == "years":
            pat += rf"|(?:나이|연령|\bage[d]?\b)[^0-9\n]{{0,10}}{num}|{num}\s*/?\s*[fm]\b"
    elif field == "ecog":
        pat = FIELD_TEXT["ecog"](v) + _RANGE_TAIL
    else:
        pat = FIELD_TEXT["sex"](v) + "|" + _SEX_MARK.get(v, r"(?!)")
    return re.compile(pat, re.IGNORECASE)


def _mentions(field: str, text: str) -> list[tuple[str, int, int]]:
    """본문에 적힌 이 필드의 값들 (충돌 확인용)."""
    if field == "age":
        pat = re.compile(rf"(?<![\d.])(\d{{1,3}})\s*(?:{_AGE_SUFFIX['years']})", re.IGNORECASE)
        found = [(m.group(1), m.start(), m.end()) for m in pat.finditer(text)]
    elif field == "ecog":
        pat = re.compile(r"(?:ecog|performance\s*status|\bps\b|수행\s*상태|활동도)[^0-9\n]{0,20}(?<!\d)(\d)(?!\d)" + _RANGE_TAIL, re.IGNORECASE)
        found = [(m.group(1), m.start(), m.end()) for m in pat.finditer(text)]
    else:
        found = [(s, m.start(), m.end()) for s in ("female", "male") for m in _value_pattern("sex", s, None).finditer(text)]
    return [x for x in found if _about_patient(text, x[1], x[2])]


def _uncertain(body: str, lo: int, hi: int) -> bool:
    """값을 말한 문장에 추측·불확실 표현이 있는가. 값 뒤의 다른 절("...인데 혈색소는 몰라요")은 보지 않음."""
    sentence = ed._sentence(body, lo)
    i = sentence.find(body[lo:hi])
    head = sentence if i < 0 else sentence[:i + hi - lo]
    clause = ed._clause(body, lo, hi)
    return ed.hedged(head) or ed.hedged(clause) or bool(_APPROX.search(head + " " + clause))


def _usable_answers(source_text: str, qa) -> list[ed.Segment]:
    """사실을 말한 답변 줄. 모름·거부와 의사 참고용 문답의 답은 판정 근거가 아님."""
    from .question_contract import non_answer
    _, segs = ed.parse_source(source_text)
    out = []
    for s in segs:
        if s.role != "answer" or non_answer(s.body):
            continue
        if qa is not None and s.round is not None and ed.answer_purpose(s, qa) == "physician_reference":
            continue
        out.append(s)
    return out


def _qa_in_source(qa, usable) -> list:
    """이 원문에 실제로 있는 답의 문답만. 다른 세션이나 지워진 문답은 근거가 아님."""
    bodies = {s.body for s in usable}
    return [q for q in qa or [] if ed._norm(q.answer or "") in bodies]


def _bare(answer: str) -> str:
    return re.sub(r"(?:이에요|예요|입니다|이요|요)$", "", (answer or "").strip().strip(" .!~").strip()).strip().lower()


def _bare_value(field: str, q, key: str | None) -> str | None:
    """그 값을 직접 물은 문답의 단독 답에서 값을 꺼냄. 없거나 모호하면 None."""
    from .question_contract import non_answer
    answer = normalize(field, q.answer or "")
    if non_answer(answer) or ed.hedged(answer) or _APPROX.search(answer) or ed.declared_purpose(q) == "physician_reference":
        return None
    if field == "sex":
        return _BARE_SEX.get(_bare(answer)) if _SEX_QUESTION.search(q.question or "") else None
    if not (key in (q.target or []) or _TOPIC[field].search(q.question or "")):
        return None
    m = re.fullmatch(rf"(?:저는\s*|제\s*\S+\s*)?({_NUMBER})", _bare(answer))
    return m.group(1) if m else None


def ground(field: str, value, source_text: str | None, *, key: str | None = None, qa=None, unit: str | None = None,
           reference_date: str | None = None) -> str | None:
    """나이·성별·ECOG 값의 원문 근거. 값이나 원문이 없으면 확인할 대상이 없으므로 None."""
    if source_text is None or value is None:
        return None
    v = _text(value)
    pat = _value_pattern(field, v, unit)
    initial = normalize(field, evidence.split_source(source_text)[0])
    in_record = any(_about_patient(initial, m.start(), m.end()) for m in pat.finditer(initial)) \
        or (field == "age" and _from_birth_year(v, initial, reference_date))
    stated, hedged = False, False
    answered: set[str] = set()
    usable = _usable_answers(source_text, qa)
    for seg in usable:
        body = normalize(field, seg.body)
        for m in pat.finditer(body):
            if not _about_patient(body, m.start(), m.end()):
                continue
            if _uncertain(body, m.start(), m.end()):
                hedged = True
            else:
                stated = True
        answered |= {x for x, lo, hi in _mentions(field, body) if not _uncertain(body, lo, hi)}
    for q in _qa_in_source(qa, usable):
        bare = _bare_value(field, q, key)
        if bare is not None:
            answered.add(bare)
            stated = stated or bare == v
    recorded = {x for x, _, _ in _mentions(field, initial)}
    if len(answered) > 1 or (recorded and answered - recorded):
        return CONFLICT
    if in_record or stated:
        return None
    return HEDGED if hedged else UNLOCATED


def _from_birth_year(v: str, text: str, reference_date: str | None) -> bool:
    """출생 연도에서 계산되는 나이인가 (생일 전후 1살 차이는 둘 다 인정)."""
    if not v.isdigit():
        return False
    try:
        year = date.fromisoformat(reference_date).year if reference_date else date.today().year
    except ValueError:
        return False
    return any(year - int(y) - int(v) in (0, 1) for m in _BIRTH.finditer(text) for y in m.groups() if y)


def _lab_named(lab: str, sentence: str) -> bool:
    canon = canonical_lab(lab)
    names = [*LAB_ALIASES.get(canon, ()), *_LAB_KO.get(canon, ())]
    return not names or any(re.search(rf"(?<![a-z가-힣]){re.escape(n)}(?![a-z])", sentence, re.IGNORECASE) for n in names)


_LAB_CANON = {n.lower(): canon for table in (LAB_ALIASES, _LAB_KO) for canon, names in table.items() for n in (canon, *names)}


@lru_cache(maxsize=256)
def _lab_names(extra: str) -> re.Pattern:
    """알려진 검사명과 이 검사의 이름. 목록에 없는 검사는 자기 이름으로만 수치에 연결됩니다."""
    names = sorted({*_LAB_CANON, extra}, key=len, reverse=True)
    return re.compile(rf"(?<![a-z가-힣])(?:{'|'.join(map(re.escape, names))})(?![a-z])", re.IGNORECASE)


def _lab_key(lab: str) -> str:
    return _LAB_CANON.get(_norm(lab)) or canonical_lab(lab)


_LAB_NUMBER = re.compile(r"(?<![\d.,])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![\d,]*\d)")
_NOT_A_VALUE = re.compile(r"\d{4}[-./]\d{1,2}(?:[-./]\d{1,2})?|\d{1,2}월(?:\s*\d{1,2}일)?|(?<![\d.])\d{1,2}/\d{1,2}(?![\d/])|"  # 날짜
                          r"\d+(?:\.\d+)?\s*[-~–]\s*\d+(?:\.\d+)?|10\s*[\^*]\s*\d+|10e\d+", re.IGNORECASE)  # 범위, 단위의 10^9
_LINK_BREAK = re.compile(r"[,;\n]")
_NAME_TO_VALUE, _VALUE_TO_NAME = 25, 12  # 검사명과 수치 사이 글자 수
_UNIT_AFTER = re.compile(r"\s*([^\s,;()]*[/%][^\s,;()]*)")


def _sentence_spans(body: str):
    start = 0
    for m in ed._SENTENCE_BOUNDARY.finditer(body):
        yield start, m.start()
        start = m.end()
    yield start, len(body)


def _linked_numbers(sentence: str, key: str) -> list[tuple[int, int, str, str | None]]:
    """문장의 수치와 그 수치의 검사명 (시작, 끝, 수치, 표준 검사명 | None). 검사명마다 자기 뒤의 첫 수치 하나만 가짐.

    뒤에 수치가 없는 검사명만 바로 앞의 수치를 가짐 ("12 g/dL hemoglobin"). 참고 범위·날짜·단위의 숫자는 수치가 아님."""
    text = _NOT_A_VALUE.sub(lambda m: " " * len(m.group()), sentence)
    names = [(m.start(), m.end(), _LAB_CANON.get(m.group().lower(), key)) for m in _lab_names(key).finditer(text)]
    numbers = [(m.start(), m.end(), m.group().replace(",", "")) for m in _LAB_NUMBER.finditer(text)]
    owner: dict[int, str] = {}
    waiting = []
    for i, (lo, hi, canon) in enumerate(names):
        limit = names[i + 1][0] if i + 1 < len(names) else len(text)
        after = next((n for n, x in enumerate(numbers) if hi <= x[0] < limit), None)
        if after is not None and numbers[after][0] - hi <= _NAME_TO_VALUE and not _LINK_BREAK.search(text[hi:numbers[after][0]]):
            owner[after] = canon
        elif after is None:
            waiting.append(i)
    for i in waiting:
        lo, _, canon = names[i]
        floor = names[i - 1][1] if i else 0
        before = next((n for n in range(len(numbers) - 1, -1, -1) if floor <= numbers[n][1] <= lo), None)
        if before is not None and before not in owner and lo - numbers[before][1] <= _VALUE_TO_NAME \
                and not _LINK_BREAK.search(text[numbers[before][1]:lo]):
            owner[before] = canon
    return [(x[0], x[1], x[2], owner.get(n)) for n, x in enumerate(numbers)]


def _lab_numbers(lab: str, body: str) -> list[tuple[int, int, str, str]]:
    """본문에서 이 검사에 연결된 환자 수치 (시작, 끝, 수치, 역할). 측정값이 아닌 수치는 뺍니다."""
    key = _lab_key(lab)
    out = []
    for start, end in _sentence_spans(body):
        sentence = body[start:end]
        for lo, hi, number, owner in _linked_numbers(sentence, key):
            role = _role(sentence, lo, hi) if owner == key else None
            if role:
                out.append((start + lo, start + hi, number, role))
    return out


def _current_values(numbers: list[tuple[int, int, str, str]]) -> set[str]:
    """현재 값으로 볼 수치. 과거로 표시된 값은 현재 값이 없을 때만 씁니다."""
    measured = {_text(float(x[2])) for x in numbers if x[3] == "measured"}
    return measured or {_text(float(x[2])) for x in numbers}


def _same_unit(body: str, hi: int, unit: str | None) -> bool:
    """수치 바로 뒤의 단위가 프로필 단위와 같은가. 어느 한쪽에 단위가 없으면 비교할 수 없으므로 막지 않음."""
    m = _UNIT_AFTER.match(body, hi)
    return not (unit and m) or _norm_unit(m.group(1).rstrip(".")) == _norm_unit(unit)


def _quote_hits(quote: str | None, source_text: str) -> list:
    """인용의 위치. 일반 위치 검사는 짧은 인용을 건너뛰고 표의 "|"에서 인용을 나누므로 인용 전체도 그대로 찾음 (수치 연결은 호출 쪽에서 확인)."""
    if not quote:
        return []
    found, hits = ed.locate(quote, source_text)
    text = ed._norm(quote).strip(" .,:'`()[]")
    _, segs = ed.parse_source(source_text)
    whole = [ed.Hit(s, m.start(), m.end()) for s in segs for m in re.finditer(re.escape(text), s.body)] if text else []
    return (hits if found else []) + whole


def ground_lab(lab: str, value, quote: str | None, source_text: str | None, *, key: str | None = None, qa=None,
               unit: str | None = None, known: set[str] | None = None) -> str | None:
    """검사값의 원문 근거. 인용 안의 수치가 이 검사에 연결되고 프로필 값·단위와 같아야 함.

    known: 프로필에 있는 이 검사의 값 전체. 기록의 다른 값이 빠져 있으면 어느 값이 현재인지 알 수 없으므로 충돌로 봅니다."""
    if source_text is None or value is None:
        return None
    usable = {id(s): s for s in _usable_answers(source_text, qa)}
    in_record = _lab_numbers(lab, evidence.split_source(source_text)[0])
    in_answers = [x for seg in usable.values() for x in _lab_numbers(lab, seg.body) if not _uncertain(seg.body, x[0], x[1])]
    recorded, answered = _current_values(in_record), _current_values(in_answers)
    has_current = any(x[3] == "measured" for x in (*in_record, *in_answers))
    if len(answered) > 1 or (recorded and answered - recorded) or (known is not None and recorded - known):
        return CONFLICT
    hedged = False
    for h in _quote_hits(quote, source_text):
        if h.seg.role != "record" and not (h.seg.role == "answer" and any(s.text == h.seg.text for s in usable.values())):
            continue
        body = h.seg.body
        for lo, hi, number, role in _lab_numbers(lab, body):
            if not (h.lo <= lo and hi <= h.hi) or float(number) != float(value) or not _same_unit(body, hi, unit):
                continue
            if role == "past" and has_current:
                continue
            if _uncertain(body, lo, hi) if h.seg.role == "answer" else ed.hedged(ed._clause(body, lo, hi)):
                hedged = True
            else:
                return None
    for q in _qa_in_source(qa, usable.values()):
        bare = _bare_lab(lab, q, key)
        if bare is not None and float(bare) == float(value):
            return None
    return HEDGED if hedged else UNLOCATED


def _bare_lab(lab: str, q, key: str | None) -> str | None:
    from .question_contract import non_answer
    answer = (q.answer or "").replace(",", "")
    if non_answer(answer) or ed.hedged(answer) or _APPROX.search(answer) or ed.declared_purpose(q) == "physician_reference":
        return None
    if not (key in (q.target or []) or _lab_named(lab, q.question or "") and _has_lab_name(lab, q.question or "")):
        return None
    m = re.fullmatch(rf"({_NUMBER})\s*(?:[a-zµ/%^0-9*]+)?", _bare(answer))
    return m.group(1) if m else None


def _has_lab_name(lab: str, text: str) -> bool:
    canon = canonical_lab(lab)
    return bool([*LAB_ALIASES.get(canon, ()), *_LAB_KO.get(canon, ())]) and _lab_named(lab, text)


# ---------------------------------------------------------------------------- 판정 근거 확인
_LLM_FIELD = re.compile(r'["\']?\b(sex|age|ecog)\b["\']?\s*[:=]\s*["\']?([a-z0-9.]+)', re.IGNORECASE)
_LAB_EVIDENCE = re.compile(r'^(.+?) (-?\d+(?:\.\d+)?(?:e[+-]?\d+)?) (.*?) ?\(', re.IGNORECASE)


def structured_problem(a, rule, profile, source_text: str | None, *, key: str | None = None, qa=None,
                       reference_date: str | None = None) -> str | None:
    """판정에 쓴 구조화 값의 근거 문제. 확정 판정만 봅니다 (판정 불가 항목은 이미 보류).

    규칙 엔진 판정은 프로필 값을, LLM 판정은 근거로 내세운 프로필 필드("age: 62")가 원문에 없을 때 그 값을 확인합니다."""
    if source_text is None or a.passes is None:
        return None
    kw = {"key": key, "qa": qa}
    if a.method == "llm":
        if ed.locate(a.evidence, source_text)[0]:
            return None
        for name, value in _LLM_FIELD.findall(a.evidence):
            problem = ground(name.lower(), value, source_text, reference_date=reference_date, **kw)
            if problem:
                return problem
        return None
    if a.method != "rule_engine" or rule is None or profile is None:
        return None
    field = _norm(rule.field or "")
    if field == "age":
        unit = profile.age_unit if profile.age_value is not None and profile.age_unit else "years"
        value = profile.age_value if profile.age_value is not None and profile.age_unit else profile.age
        return ground("age", value, source_text, unit=unit, reference_date=reference_date, **kw)
    if field == "ecog":
        return ground("ecog", profile.ecog, source_text, **kw)
    if field == "sex":
        if _norm(rule.value or "") in ("all", "any") or profile.sex not in ("male", "female"):
            return None
        return ground("sex", profile.sex, source_text, **kw)
    if field.startswith("lab:"):
        m = _LAB_EVIDENCE.match(a.evidence)
        quote = a.evidence.split(' - "', 1)[-1].rstrip('"') if ' - "' in a.evidence else ""
        if not m:
            return UNLOCATED
        known = {_text(x.value) for x in profile.labs if x.value is not None and canonical_lab(x.name) == canonical_lab(m.group(1))}
        return ground_lab(m.group(1), float(m.group(2)), quote, source_text, unit=m.group(3).strip() or None, known=known, **kw)
    return None
