"""LLM 확정 판정의 근거 직접성 검증 (코드 검증, LLM 자기평가와 별개).

matcher._enforce_quote는 '인용이 환자 원문에 있는가'만 봅니다. 인용이 원문에 있다는 것(출처)과 그 인용이 기준의
조건을, 기준이 말하는 대상에 대해 직접 말한다는 것(직접성)은 다릅니다.
예: "조영 CT를 받은 적 있음"은 원문에 있지만 "조영제 금기 없음"을 말하지 않음(간접 추론),
    "페니실린 알레르기 없음"은 직접 기록이지만 "조영제 알레르기 없음"의 근거가 아님(대상 불일치).

원칙
- 이 모듈은 판정을 올리지 않습니다. 기준이 요구하는 필요조건이 근거에 없을 때만 확정 판정을 unknown으로 되돌립니다.
  검사를 모두 통과해도 '검증된 직접 근거'가 아니라 '코드가 확인할 수 있는 필요조건을 어기지 않음'입니다.
- 표현 목록(개념어·대상어·결과어)은 후보 탐지에만 씁니다. 단어가 있다는 것만으로 근거를 인정하지 않습니다.
- 의학적 동등성·임상적 대체 가능성을 정의하지 않습니다. 대상 비교는 이름(표기)과 포함 관계(조영제 ⊃ 요오드·가돌리늄
  조영제)만 쓰며, 확인할 수 없으면 unknown입니다.
- 기준 하나의 판정과 그 판정이 인용한 원문 절(clause)만 봅니다. 환자 정보 전체를 바꾸지 않습니다.

원문 구조: 처음 입력(record) / 확인 질문(Q:) / 환자 답변(A:, 바로 앞 질문과 연결). 질문 문장은 환자 진술이 아닙니다.

검사 순서 (첫 번째로 걸린 것이 결과)
1. unlocated      : 인용 위치를 원문 줄 단위로 확인하지 못함 (여러 줄에 걸친 인용은 줄마다 나눠 확인)
2. question_only  : 인용이 질문 문장에만 있음
3. unlinked       : 근거가 짧은 답변(예/아니요 등)뿐인데 질문 ID·대상 기준·답변 연결(qa_log)이 확인되지 않음
4. conflicting    : 같은 대상에 대한 원문 진술의 극성(있음/없음)이 서로 다름 -> 기록 확인(UNVERIFIED)
5. hedged         : 근거 절에 추측·불확실·모름 표현. '의심·가능성'은 부정 범위 안이면("의심 소견 없음") 추측이 아님
   (해당 없음)    : 대상 집단 정의가 있는 개념군(임신·수유)은 서로 다른 상태를 묶은 근거는 hedged, 대상 집단 밖임을 정하는
                    사실(폐경·자궁 적출 등)을 긍정·과거형으로 말하지 않으면 indirect (P14 D1). 아래 6~8은 적용하지 않음
6. indirect       : 개념군 기준인데 개념어 없음 / 극성이 판정과 반대 / 검사 결과를 요구하는데 검사 언급 없음 /
                    치료 결과(실패·진행 등)를 요구하는데 결과 언급 없음
7. object_mismatch: 기준의 대상(물질·특정 시술 약어)과 근거의 대상이 맞지 않음
8. time_unverified: 기간 조건(N일 이내 등)이 있는데 근거 날짜가 기간 안임을 확인할 수 없음
통과: direct(개념군 기준) / unchecked(그 밖의 기준). 구조화 필드 근거는 check_structured.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

# 언제나 추측·불확실을 뜻하는 표현
_EPISTEMIC = re.compile(
    r"것\s*같|듯\s*(?:해|합|하|싶)|아마|글쎄|모르(?!핀)|모릅|모른|모를|몰라|몰랐|모름|기억이?\s*(?:안|나지|없)|"
    r"확실하지|확실치|불확실|미확인|불명|확인(?:되지|이?\s*안|\s*못)|추정|무슨|뭔가|을\s*거(?:예요|에요|야|다|같)|\?|"
    r"\b(?:probabl[ey]|maybe|perhaps|likely|unlikely|presum(?:ed|ably)|questionable|uncertain|unsure|not\s+sure|"
    r"unknown|unclear|may\s+have|might|could\s+be|rule\s+out|r/o|cannot\s+(?:be\s+)?exclude[d]?|equivocal|"
    r"some\s+kind\s+of|i\s+think|i\s+guess|i\s+believe|not\s+known)\b",
    re.IGNORECASE)
# 가능성·의심을 나타내는 명사: 부정 범위 안에 있으면 추측이 아니라 부정 진술 ("의심 소견 없음", "no suspicious lesion")
_SOFT = re.compile(r"의심|가능성|\b(?:suspect\w*|suspicio\w*|possib\w*)", re.IGNORECASE)
_SOFT_NEGATED_AFTER = re.compile(r"^[^,;.!?]{0,10}?(?:없|않|아니)")  # 한국어: 명사 뒤 같은 절의 부정 서술어
_SOFT_NEGATED_BEFORE = re.compile(r"\b(?:no|not|without|negative\s+for|denies|denied)\b[^,;.!?]{0,30}$", re.IGNORECASE)
# 부정 표현. 한국어는 어절(붙여 쓴 한글 덩어리) 단위로 판별해, 같은 글자열이 다른 낱말의 일부인 경우를 구분함
# - 영어 부정어와 띄어 쓴 '안'·'못': 낱말 경계로만
_NEG_WORD = re.compile(r"\b(?:no|not|none|denies|denied|negative|without|never)\b|(?<![가-힣])(?:안|못)(?=\s)",
                       re.IGNORECASE)
# - 문법 부정 형태소 없(다)·않(다)·아니(다): 붙여 쓴 기록("이상없음", "임신아님")이 흔해 어절 안 어디서나 부정으로 봄
_NEG_MORPHEME = re.compile(r"없|않|아니|아닙|아님|아닌")
# - 단, '없'과 굳어 다른 뜻이 된 부사·형용사(사전 등재어: 끊임없이·틀림없다·변함없이 ...)는 임상 개념의 부재를 말하지 않음
_LEXICAL_EOPS = re.compile(r"(?:끊임|틀림|어김|빠짐|거침|변함|하염|꾸밈|쉴새|영락|다름)없")
# - 한자어 부정 명사 否認(부인)·陰性(음성): 같은 글자열이 다른 한자어(婦人: 산부인과·부인과·부인암·부인께서,
#   音聲: 음성 녹음·음성인식)의 일부일 수 있어, 뒤가 어절 끝이거나 그 명사에 붙는 서술·조사 어미일 때만 부정으로 봄
_NEG_NOUN = {"부인": re.compile(r"(?:하[가-힣]*|함|했[가-힣]*|한|해[가-힣]*|됨|되[가-힣]*)?"),
             "음성": re.compile(r"(?:이[가-힣]*|임|입[가-힣]*|인|였[가-힣]*|으로[가-힣]*|을|은|도|"
                               r"반응[가-힣]*|판정[가-힣]*|소견[가-힣]*)?")}
_HANGUL_RUN = re.compile(r"[가-힣]+")
# 대상어 비교에서 뺄 낱말: 낱말 전체가 부정어인 것 (이전 규칙 그대로)
_NEG_WHOLE = re.compile(r"no|not|none|denies|denied|negative|without|never|없|않|아니|아닙|아님|아닌|음성|부인", re.IGNORECASE)


def _negation_token(token: str) -> bool:
    """한글 어절 하나가 부정 표현을 담고 있는가 (_negated 참고)."""
    if _NEG_MORPHEME.search(_LEXICAL_EOPS.sub(" ", token)):
        return True
    for noun, ending in _NEG_NOUN.items():
        for m in re.finditer(noun, token):
            if ending.fullmatch(token[m.end():]):
                return True
    return False


def _negated(text: str) -> bool:
    """부정 표현이 있는가. 낱말 안의 같은 글자열(산부인과의 '부인', 음성 녹음의 '음성', 끊임없이의 '없')은 부정이 아님.
    의미 판정이 아니라 표기 판별입니다: 부정이 무엇을 부정하는지(개념·대상)는 다른 검사(_objects·_covers)가 봅니다."""
    return bool(_NEG_WORD.search(text)) or any(_negation_token(t) for t in _HANGUL_RUN.findall(text))
# 기준 문장 자체가 '없음'을 요구하는가 ("No rituximab ...", "without prior ...", "~ 없음"). 'negative'(검사 결과값)는 제외
_CRITERION_NEGATION = re.compile(
    r"^\s*(?:no|not|none|without|never|absence\s+of|free\s+of)\b|"
    r"\b(?:with\s+no|without|has\s+not|have\s+not|has\s+never|have\s+never|did\s+not|does\s+not|no\s+(?:prior|previous|"
    r"known|history|evidence|use))\b|(?:없음|없는|없을|않은|않음|아닌|아님)\s*\.?\s*$", re.IGNORECASE)
# P9: 있음·없음 서술 뒤의 대등 연결 어미(-고·-(으)며)도 절 경계 ("페니실린 알레르기는 없고 조영제 알레르기 있음"이
# 한 절로 묶여 '없'이 조영제 진술의 극성이 되던 문제). '보고'·'사고' 같은 명사의 '고'는 앞 글자 조건으로 제외
_CLAUSE_BOUNDARY = re.compile(r"[,;]|(?<=[.!?。])\s|\s(?:but|however|although|though|whereas)\s|"
                              r"(?:는데|한데|인데|던데|지만|으나)(?=\s|$)|"
                              r"(?:(?<=없)|(?<=있)|(?<=않)|(?<=아니))(?:고|으며|며)(?=\s)", re.IGNORECASE)
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?。])\s")
_TOKEN = re.compile(r"[a-z][a-z0-9]*|[가-힣]+", re.IGNORECASE)
# 대상 비교에서 빼는 일반 단어 (기능어·극성어·서술어·기록 표지). 한국어는 접두로 비교(조사가 붙은 형태)
_STOP = {"no", "not", "none", "known", "any", "the", "to", "or", "and", "with", "without", "of", "has", "have", "had",
         "history", "patient", "he", "she", "is", "was", "are", "were", "a", "an", "in", "on", "for", "by", "at",
         "denies", "denied", "negative", "never", "record", "records", "documented", "reported", "previous", "prior",
         "past", "current", "medical", "status", "noted", "findings", "finding", "evidence", "test", "result", "results",
         "적", "후", "전", "때", "이상", "정도", "했어요", "했습니다", "했음"}
# 한국어 접두 비교 (조사·어미가 붙은 형태). '전'·'후'처럼 다른 낱말(전이·전립선)의 앞부분이 되는 한 글자는 넣지 않음
_STOP_KO = ("기록", "의무기록", "환자", "있", "없", "받", "여부", "확인", "과거", "현재", "최근", "이전", "검사", "결과",
            "소견", "투여", "시행", "상태", "당시")
_YES = {"예", "네", "yes", "y", "맞아요", "맞습니다", "그렇습니다", "있음", "있어요", "있습니다"}
_NO = {"아니요", "아니오", "아뇨", "no", "n", "없음", "없어요", "없습니다"}
_OUTCOME = re.compile(r"fail\w*|progress\w*|refractor\w*|resist\w*|relaps\w*|recur\w*|실패|진행|불응|내성|재발|악화",
                      re.IGNORECASE)
_ACRONYM = re.compile(r"\b[A-Z][A-Z0-9]{1,}(?:-[A-Z0-9]+)?\b")
_GENERIC_ACRONYMS = {"CT", "MRI", "MR", "IV", "PET", "US", "ECOG", "RECIST", "ULN", "BMI", "ICU", "II", "III", "VI",
                     "OR", "AND", "NOT", "NO", "WHO", "NCI", "CTCAE", "FDA"}
_SPECIFIC_CATEGORIES = {"procedure", "treatment_history"}
_WINDOW = [(re.compile(r"within\s+(?:the\s+)?(?:last\s+|past\s+)?(\d+)\s*(day|week|month|year)s?", re.I), None),
           (re.compile(r"(\d+)\s*(일|주|개월|달|년)\s*(?:이내|안에)"), None)]
_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365, "일": 1, "주": 7, "개월": 30, "달": 30, "년": 365}
_DATE = re.compile(r"(?<!\d)(\d{4})\s*(?:[-./]|년)\s*(\d{1,2})(?:\s*(?:[-./]|월)\s*(\d{1,2}))?")


@dataclass(frozen=True)
class ObjectTerm:
    name: str
    pattern: re.Pattern
    parent: str | None = None


@dataclass(frozen=True)
class Concept:
    name: str
    criterion: re.Pattern  # 기준 문장이 이 개념군에 속하는가 (후보)
    direct: re.Pattern  # 근거 절이 이 개념을 말하는가 (후보, 충분조건 아님)
    objects: tuple[ObjectTerm, ...] = ()  # 대상 이름 (구체적인 것부터)
    requires_test: re.Pattern | None = None  # 기준이 검사 결과를 요구하면 근거에도 검사 언급이 필요
    # '해당 없음'(대상 집단 아님)을 정하는 사실 (후보). 있으면 개념어만으로는 '해당 없음'을 인정하지 않음 (P14 D1)
    population_exclusion: re.Pattern | None = None


_CONTRAST_SUFFIX = r"(?:[\s-]*(?:based\s*)?(?:contrast(?:\s+(?:media|agents?))?|조영제))?"
CONCEPTS = (
    Concept("allergy_contraindication",
            re.compile(r"allerg|hypersensitiv|contraindicat|intoleran|anaphyla|adverse\s+(?:drug\s+)?reaction|"
                       r"알레르기|알러지|과민|금기|부작용|아나필락시", re.I),
            re.compile(r"allerg|hypersensitiv|contraindicat|intoleran|anaphyla|adverse|reaction|tolerat|urticaria|"
                       r"rash|shock|알레르기|알러지|과민|금기|부작용|아나필락시|쇼크|반응|두드러기|발진", re.I),
            (ObjectTerm("iodinated_contrast", re.compile(r"(?:iodinat\w*|iodine|요오드|아이오딘)" + _CONTRAST_SUFFIX, re.I),
                        "contrast"),
             ObjectTerm("gadolinium_contrast", re.compile(r"(?:gadolin\w*|가돌리늄)" + _CONTRAST_SUFFIX, re.I), "contrast"),
             ObjectTerm("contrast", re.compile(r"contrast|조영제|조영", re.I)))),
    Concept("pregnancy_lactation",
            re.compile(r"pregnan|breast-?feed|lactat|nursing|childbearing|임신|수유|가임", re.I),
            re.compile(r"pregnan|hcg|breast-?feed|lactat|nursing|menopaus|hysterectom|oophorectom|"
                       r"임신|수유|폐경|자궁\s*적출|난소\s*절제", re.I),
            (ObjectTerm("pregnancy", re.compile(r"pregnan\w*|임신|hcg", re.I)),
             ObjectTerm("lactation", re.compile(r"breast-?feed\w*|lactat\w*|nursing|수유", re.I))),
            re.compile(r"\btest|hcg|검사", re.I),
            # 가임 대상 집단 밖: 폐경·자궁 적출·양측 난소(난관) 절제. 폐경 '전'·폐경 이행기·난관결찰·"임신 가능성 없음"(자기 평가)은 아님
            re.compile(r"(?<!pre)(?<!peri)(?<!pre-)(?<!peri-)menopaus|hysterectom|"
                       r"bilateral\s+(?:salpingo-?)?oophorectom|bilateral\s+salpingectom|"
                       r"(?:폐경|완경)(?!기?\s*(?:전|이행|주변|여부))|자궁\s*(?:적출|절제)|"
                       r"양측\s*(?:난관\s*)?(?:난소|난관)\s*(?:절제|적출)", re.I)),
)
# '해당 없음' 근거가 서로 다른 상태를 묶음 ("폐경되었거나 임신 가능성이 없음"): 어느 상태인지 모름
_DISJUNCTION = re.compile(r"거나|또는|혹은|\b(?:or|either)\b", re.I)  # '/'는 약어(s/p, w/o)에 흔해 넣지 않음
# 앞으로 일어날 일 ("자궁 적출 예정"): 지금 대상 집단 밖이라는 근거가 아님
_PLANNED = re.compile(r"예정|계획|하려고|할\s*것|\b(?:scheduled|planned|planning|will|plans?\s+to)\b", re.I)
_PARENT = {o.name: o.parent for c in CONCEPTS for o in c.objects}


def _ancestors(name: str) -> set[str]:
    out = {name}
    while _PARENT.get(name):
        name = _PARENT[name]
        out.add(name)
    return out


@dataclass
class Check:
    directness: str
    flags: list[str] = field(default_factory=list)
    concept: str | None = None

    @property
    def ok(self) -> bool:
        return self.directness in ("direct", "unchecked", "structured")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


# ---------------------------------------------------------------------------- 표현 판별
def hedged(clause: str) -> bool:
    """추측·불확실 표현이 있는가. '의심·가능성'은 같은 절 안의 부정 범위 밖에 있을 때만 추측."""
    if _EPISTEMIC.search(clause):
        return True
    for m in _SOFT.finditer(clause):
        if _SOFT_NEGATED_AFTER.search(clause[m.end():]) or _SOFT_NEGATED_BEFORE.search(clause[:m.start()]):
            continue
        return True
    return False


def polarity(clause: str) -> str:
    if hedged(clause):
        return "uncertain"
    return "negative" if _negated(clause) else "positive"


def _flip(p: str) -> str:
    return {"positive": "negative", "negative": "positive"}.get(p, p)


# ---------------------------------------------------------------------------- 원문 구조
@dataclass
class Segment:
    role: str  # record | question | answer | header
    text: str  # 정규화된 줄 전체 (Q:/A: 포함)
    body_start: int  # 내용 시작 위치 (Q:/A: 다음)
    start: int  # 이어 붙인 원문에서의 시작 위치
    question: "Segment | None" = None  # 답변이면 바로 앞 질문
    round: int | None = None  # 확인 질문 라운드 ("[추가 확인 정보 - 라운드 N]" 머리말 아래). 처음 입력은 None

    @property
    def body(self) -> str:
        return self.text[self.body_start:]


_ROUND = re.compile(r"라운드\s*(\d+)")


def parse_source(source_text: str) -> tuple[str, list[Segment]]:
    """줄 단위 구조. 이어 붙인 문자열은 matcher._norm_text(원문)와 같은 공백 규칙(줄바꿈 = 공백 1칸)."""
    segs: list[Segment] = []
    joined = ""
    last_q: Segment | None = None
    round_no: int | None = None
    for line in source_text.split("\n"):
        t = _norm(line)
        if not t:
            continue
        if t.startswith("[추가 확인 정보"):
            role, body = "header", len(t)
            last_q = None
            m = _ROUND.search(t)
            round_no = int(m.group(1)) if m else None
        elif t[:2] in ("q:", "a:"):
            role = "question" if t[0] == "q" else "answer"
            body = len(t) - len(t[2:].lstrip())
        else:
            role, body = "record", 0
        if joined:
            joined += " "
        seg = Segment(role, t, body, len(joined), last_q if role == "answer" else None,
                      round_no if role in ("question", "answer") else None)
        if role == "question":
            last_q = seg
        segs.append(seg)
        joined += t
    return joined, segs


@dataclass
class Hit:
    seg: Segment
    lo: int  # 줄 내용(body) 안의 위치
    hi: int


def _fragments(evidence_text: str) -> list[str]:
    from .agents.matcher import _MIN_QUOTE_CHARS, _QUOTE_SPLIT  # 인용 조각 기준은 _enforce_quote와 같게
    out = []
    for frag in _QUOTE_SPLIT.split(evidence_text):
        frag = _norm(frag).strip(" .,:'`()[]")
        if len(frag.replace(" ", "")) >= _MIN_QUOTE_CHARS:
            out.append(frag)
    return out


def locate(evidence_text: str, source_text: str) -> tuple[bool, list[Hit]]:
    """인용 조각의 원문 위치를 줄별로 나눔. 반환: (한 조각이라도 위치를 찾았는가, 줄별 위치)."""
    joined, segs = parse_source(source_text)
    found, hits = False, []
    for frag in _fragments(evidence_text):
        for m in re.finditer(re.escape(frag), joined):
            found = True
            for seg in segs:
                lo = max(m.start(), seg.start) - seg.start
                hi = min(m.end(), seg.start + len(seg.text)) - seg.start
                lo = max(lo, seg.body_start)
                if lo < hi:
                    hits.append(Hit(seg, lo - seg.body_start, hi - seg.body_start))
    return found, hits


def _clause(body: str, lo: int, hi: int) -> str:
    """body[lo:hi]를 포함하는 절. 인용 끝이 절 경계 어미('~는데')로 끝나면 거기서 끊음."""
    a, b = 0, len(body)
    for m in _CLAUSE_BOUNDARY.finditer(body):
        s, e = m.span()
        if e <= lo:
            a = max(a, e)
        elif s <= lo < e:
            a = max(a, lo)
        if s >= hi:
            b = min(b, s)
        elif s < hi <= e:
            b = min(b, hi)
    return body[a:b].strip(" ,;")


def _sentence(body: str, lo: int) -> str:
    start = 0
    for m in _SENTENCE_BOUNDARY.finditer(body):
        if m.end() <= lo:
            start = m.end()
        else:
            return body[start:m.start()]
    return body[start:]


def _short(answer: str) -> bool:
    from .agents.matcher import _MIN_QUOTE_CHARS
    return len(answer.replace(" ", "").strip(".!")) < _MIN_QUOTE_CHARS


def _linked(seg: Segment, key: str | None, qa_log) -> bool:
    """답변 줄이 qa_log의 한 기록(라운드·질문 ID·질문 문장·답·대상 기준 <trial>:<rule>)과 연결되는가.

    question_id는 라운드마다 Q1부터 다시 매겨지므로 라운드까지 맞아야 같은 기록으로 봄. qa_log는 호출 쪽에서
    이 세션의 기록만 남긴 것(check의 session_id 필터)이어야 함. (fix/qa-evidence-link의 구조 연결 검사)
    """
    if not (key and qa_log and seg.question):
        return False
    q, a = seg.question.body, seg.body
    return any(_norm(p.question) == q and _norm(p.answer) == a and p.question_id
               and (seg.round is None or p.round == seg.round) for p in eligibility_answers(key, qa_log))


def non_answer(answer: str | None) -> bool:
    """사실을 말하지 않은 답(모름·거부·확인 불가·미응답·공백) 전체. 판별은 question_contract.non_answer 하나이고, 이 이름은
    fix/qa-evidence-link의 API(evidence_directness.non_answer)를 그대로 쓰는 호출 쪽을 위해 둠."""
    from .question_contract import non_answer as _non_answer  # 순환 import 방지 (question_contract -> decision_state)
    return _non_answer(answer)


def eligibility_answers(key: str | None, qa_log) -> list:
    """이 기준(<trial>:<rule>)을 겨냥한 적격성 확인 문답만. 의사 참고용(physician_reference) 문답은 판정 근거가 아님
    (판정을 바꾸지 않는 참고 정보 수집용). 세션 필터는 호출 쪽(check의 session_id)이 먼저 적용."""
    if not (key and qa_log):
        return []
    return [p for p in qa_log if key in (p.target or []) and getattr(p, "purpose", "eligibility") == "eligibility"]


def answers_revised(key: str | None, qa_log) -> bool:
    """같은 기준을 겨냥한 같은 질문(문장 동일)에 서로 다른 답이 있는가 (답 수정). 모름·거부·미응답은 비교하지 않음.

    fix/qa-evidence-link의 answers_conflict 중 '질문 문장이 같으면 같은 사실' 규칙만 남긴 구조 검사입니다. 질문 주제·
    기간이 같은지 문자열로 추측하는 규칙(_same_topic·기간 포함 관계)은 옮기지 않음 - 다른 질문 사이의 관계는 질문 계약의
    명제 키가 정합니다(question_contract). 표기만 다른 같은 답("예"/"네")도 수정으로 봄(확정하지 않는 쪽, 보수적).
    """
    seen: dict[str, str] = {}
    for p in eligibility_answers(key, qa_log):
        if non_answer(p.answer):
            continue
        q, a = _norm(p.question), _norm(p.answer).strip(" .!")
        if seen.setdefault(q, a) != a:
            return True
    return False


@dataclass
class _Evidence:
    """검사에 쓰는 근거 단위 하나 (원문 절, 또는 연결된 질문 + 짧은 답변)."""
    clause: str  # 추측 검사 대상 (환자 진술·기록 부분만)
    context: str  # 개념어·대상·결과어 탐지 범위 (짧은 답변이면 질문 포함)
    sentence: str  # 날짜·대상 보충 범위
    polarity: str
    question: str = ""  # 답변이면 연결된 질문 (대상 범위 보충용, 진술로 쓰지 않음)
    linked: bool = False  # qa_log로 연결된 짧은 답변 (질문 + 답으로 극성을 정할 수 있음)
    non_answer: bool = False  # 답 전체가 모름·거부·미응답 (확정 근거가 될 수 없음)


def _evidence_units(hits: list[Hit], key: str | None, qa_log) -> tuple[list[_Evidence], list[str]]:
    units, problems = [], []
    seen = set()
    for h in hits:
        if h.seg.role not in ("record", "answer"):
            continue
        body = h.seg.body
        ident = (h.seg.start, h.lo, h.hi)
        if ident in seen:
            continue
        seen.add(ident)
        if h.seg.role == "answer" and non_answer(body):  # "답변하지 않겠습니다"의 '않'을 부재 진술로 읽지 않음
            q = h.seg.question.body if h.seg.question else ""
            units.append(_Evidence(body, body, body, "uncertain", q, non_answer=True))
            continue
        if h.seg.role == "answer" and _short(body):
            if not _linked(h.seg, key, qa_log):
                problems.append("answer_not_linked")
                continue
            q = h.seg.question.body
            ans = body.strip(" .!")
            if hedged(ans):
                pol = "uncertain"
            elif ans in _YES:
                pol = "negative" if _negated(q) else "positive"
            elif ans in _NO:
                pol = _flip("negative" if _negated(q) else "positive")
            else:
                pol = polarity(ans)
            units.append(_Evidence(ans, f"{q} {ans}", ans, pol, q, linked=True))
            continue
        c = _clause(body, h.lo, h.hi)
        q = h.seg.question.body if h.seg.role == "answer" and h.seg.question else ""
        units.append(_Evidence(c, c, _sentence(body, h.lo), polarity(c), q))
    return units, problems


# ---------------------------------------------------------------------------- 대상
def _objects(text: str, concept: Concept | None) -> tuple[set[str], set[str]]:
    """(대상 이름 집합, 그 밖의 한정어 토큰). 구체적인 대상 이름을 먼저 찾아 지우고 일반 이름을 찾음."""
    found: set[str] = set()
    rest = text.lower()
    if concept:
        for o in concept.objects:  # 구체적인 것부터 정의돼 있음
            if o.pattern.search(rest):
                found.add(o.name)
                rest = o.pattern.sub(" ", rest)
    words = set()
    for t in _TOKEN.findall(rest):
        if len(t) < 2 or t in _STOP or t.startswith(_STOP_KO) or _NEG_WHOLE.fullmatch(t) or hedged(t):
            continue
        if concept and (concept.direct.search(t) or concept.criterion.search(t)):
            continue
        words.add(t)
    return found, words


def _covers(holds_absence: bool, ev: tuple[set[str], set[str]], crit: tuple[set[str], set[str]]) -> bool:
    """근거의 대상이 기준의 대상을 덮는가 (이름·포함 관계만).

    없음(부재) 판정: 기준의 대상마다 근거가 같은 대상이나 그 상위 대상(조영제 ⊃ 요오드 조영제)의 부재를 말해야 함.
                    대상을 밝히지 않은 부재("알레르기 없음")는 모든 대상의 부재로 봄. 다른 대상(페니실린)은 덮지 않음.
    있음(존재) 판정: 근거가 기준의 대상과 같거나 그 하위 대상의 존재를 말해야 함. 대상 없는 존재는 덮지 않음.
    """
    ev_objs, ev_words = ev
    crit_objs, crit_words = crit
    if holds_absence:
        if ev_objs:
            return bool(crit_objs) and all(any(e in _ancestors(c) for e in ev_objs) for c in crit_objs)
        return not ev_words
    if crit_objs:
        return any(c in _ancestors(e) for e in ev_objs for c in crit_objs)
    if crit_words:
        return bool(crit_words & ev_words)
    return True


def _related(unit_objs: tuple[set[str], set[str]], other: str, concept: Concept | None) -> bool:
    objs, words = unit_objs
    o_objs, _ = _objects(other, concept)
    if concept and objs:
        return concept.direct.search(other) is not None and any(
            a in _ancestors(b) or b in _ancestors(a) for a in objs for b in o_objs)
    if not words:
        return False
    return all(w in other for w in words)


def _conflicts(units: list[_Evidence], segs: list[Segment], concept: Concept | None) -> bool:
    """같은 대상에 대한 원문 진술(질문 제외) 중 '있음'과 '없음'이 함께 있는가. 추측 진술은 상충으로 세지 않음."""
    clauses = [c.strip(" ,;") for s in segs if s.role in ("record", "answer") for c in _CLAUSE_BOUNDARY.split(s.body)]
    clauses = [c for c in clauses if c]
    for u in units:
        objs = _objects(u.context if u.question and u.clause != u.context else u.clause, concept)
        if not (objs[0] or objs[1]):
            objs = _objects(u.sentence, concept)
        pols = {u.polarity}
        for c in clauses:
            if c != u.clause and _related(objs, c, concept):
                pols.add(polarity(c))
        if {"positive", "negative"} <= pols:
            return True
    return False


# ---------------------------------------------------------------------------- 기간
def window_days(criterion_text: str, time_window_days: int | None) -> int | None:
    if time_window_days:
        return time_window_days
    for pat, _ in _WINDOW:
        m = pat.search(criterion_text)
        if m:
            return int(m.group(1)) * _UNIT_DAYS[m.group(2).lower()]
    return None


def _within(text: str, days: int, reference_date: str | None) -> bool:
    """근거의 날짜가 모두 [기준일 - days, 기준일] 안인가. 날짜가 없거나 연도만 있거나 해석할 수 없으면 False."""
    try:
        ref = date.fromisoformat(reference_date or "")
    except ValueError:
        return False
    found = _DATE.findall(text)
    if not found:
        return False
    for y, mth, d in found:
        try:
            y, mth = int(y), int(mth)
            first = date(y, mth, int(d)) if d else date(y, mth, 1)
            last = date(y, mth, int(d)) if d else date(y, mth, calendar.monthrange(y, mth)[1])
        except ValueError:
            return False
        if first < ref - timedelta(days=days) or last > ref:
            return False
    return True


# ---------------------------------------------------------------------------- 검사
def check(criterion_text: str, evidence_text: str, source_text: str | None, *, holds: str = "yes",
          applicable: bool = True, category: str = "other", time_window_days: int | None = None,
          reference_date: str | None = None, key: str | None = None, qa_log=None,
          session_id: str | None = None) -> Check:
    """확정 판정 근거의 필요조건 검사. 인용이 원문에 있다는 것은 _enforce_quote가 이미 확인했다고 가정.

    key: "<trial_id>:<rule_id>" (짧은 답변 연결 확인용), qa_log: Session.qa_log (없으면 짧은 답변은 근거로 쓰지 않음)
    session_id: 주면 그 세션에서 만든 qa_log 기록만 씀(다른 환자·세션 기록 혼입 방지, session_id 없는 기록도 제외)
    """
    if source_text is None:
        return Check("unchecked", ["no_source_text"])
    if qa_log and session_id is not None:
        qa_log = [p for p in qa_log if getattr(p, "session_id", None) == session_id]
    found, hits = locate(evidence_text, source_text)
    if not found:
        return Check("unlocated", ["quote_not_located"])
    if not any(h.seg.role in ("record", "answer") for h in hits):
        return Check("question_only", ["quote_only_in_question"])
    units, problems = _evidence_units(hits, key, qa_log)
    if not units:
        return Check("unlinked", problems or ["quote_not_located"])
    concept = next((k for k in CONCEPTS if k.criterion.search(criterion_text)), None)
    cname = concept.name if concept else None
    _, segs = parse_source(source_text)
    if _conflicts(units, segs, concept):
        return Check("conflicting", ["conflicting_evidence"], cname)
    if answers_revised(key, qa_log):  # 같은 질문에 답을 바꿈: 어느 답으로도 확정하지 않고 기록 확인
        return Check("conflicting", ["conflicting_answers"], cname)
    if any(u.non_answer for u in units):
        return Check("hedged", ["non_answer"], cname)  # 모름·거부·미응답은 어느 쪽 근거도 아님
    if any(u.polarity == "uncertain" or hedged(u.clause) for u in units):
        return Check("hedged", ["hedged_quote"], cname)
    if not applicable:  # '해당 없음': 대상 집단이 아님을 말하는 근거. 기준 본문의 대상·기간·검사 요건은 적용하지 않음
        if concept and concept.population_exclusion:
            # P14 D1: 개념어("임신 가능성")가 있다는 것만으로 대상 집단 밖이라고 보지 않음. 대상 집단 밖임을 정하는 사실을
            # 근거가 다른 상태와 묶지 않고, 긍정으로(부정형 질문의 예/아니요 제외), 이미 일어난 일로 말해야 함
            if any(_DISJUNCTION.search(u.context) for u in units):
                return Check("hedged", ["combined_states"], cname)
            if not any(concept.population_exclusion.search(u.context) and u.polarity == "positive"
                       and not _PLANNED.search(u.context) and not (u.linked and _negated(u.question)) for u in units):
                return Check("indirect", ["population_exclusion_not_stated"], cname)
            return Check("direct", [], cname)
        if concept and not any(concept.direct.search(u.context) for u in units):
            return Check("indirect", ["indirect_evidence"], cname)
        return Check("direct" if concept else "unchecked", [], cname)
    # 판정이 말하는 것이 사건·상태의 '있음'인가 '없음'인가. 부정형 기준("No rituximab ... within 6 months")에서는
    # holds=yes가 '없음' 주장이므로 뒤집음. 극성 대응은 부정형 기준에서 건너뜀(보고서의 한계)
    negated_criterion = bool(_CRITERION_NEGATION.search(criterion_text))
    absence = (holds == "no") != negated_criterion
    # 극성 대응은 기준에 부정어·결과값('negative', '음성' 등)이 하나라도 있으면 건너뜀: "Negative pregnancy test"처럼
    # 근거의 '음성'이 충족을 뜻하는 기준을 코드가 구분할 수 없음 (보수적으로 막지 않고 LLM 판정을 둠 - 보고서의 한계)
    polarity_checkable = not _negated(criterion_text)
    # 연결된 짧은 답변(예/아니요)은 질문과 답으로 극성이 정해지므로 개념군 밖 기준에도 판정과 맞는지 확인
    # (질문이 기준과 반대 방향으로 물었으면 맞지 않는 것으로 보여 보류됨 - 보수적)
    if polarity_checkable and any(
            u.linked and u.polarity != ("negative" if absence else "positive") for u in units):
        return Check("indirect", ["polarity_mismatch"], cname)
    if concept:
        if not any(concept.direct.search(u.context) for u in units):
            return Check("indirect", ["indirect_evidence"], cname)
        if concept.requires_test and concept.requires_test.search(criterion_text) \
                and not any(concept.requires_test.search(u.context) for u in units):
            return Check("indirect", ["test_not_stated"], cname)
        if polarity_checkable:
            want = "negative" if absence else "positive"
            if any(u.polarity != want for u in units):
                return Check("indirect", ["polarity_mismatch"], cname)
        crit = _objects(criterion_text, concept)
        ev_objs: set[str] = set()
        ev_words: set[str] = set()
        for u in units:
            o, w = _objects(u.context, concept)
            if not (o or w):
                o, w = _objects(u.sentence, concept)
            if not (o or w) and u.question:
                o, w = _objects(u.question, concept)  # 대상을 밝히지 않은 답변은 연결된 질문의 대상 범위로 읽음
            ev_objs |= o
            ev_words |= w
        if not _covers(absence, (ev_objs, ev_words), crit):
            return Check("object_mismatch", ["object_not_matched"], cname)
    if not absence and category in _SPECIFIC_CATEGORIES:
        ids = {a.lower() for a in _ACRONYM.findall(criterion_text) if a not in _GENERIC_ACRONYMS}
        if ids and not any(i in u.context for i in ids for u in units):
            return Check("object_mismatch", ["specific_term_missing"], cname)
    if not absence and _OUTCOME.search(criterion_text) and not any(_OUTCOME.search(u.context) for u in units):
        return Check("indirect", ["outcome_not_stated"], cname)
    days = window_days(criterion_text, time_window_days)
    if days and not absence and not all(_within(u.sentence, days, reference_date) for u in units):
        return Check("time_unverified", ["time_window_unverified"], cname)
    return Check("direct" if concept else "unchecked", [], cname)


_FIELD_REF = re.compile(r"\b(sex|age|ecog)\b\s*[:=]", re.IGNORECASE)


def check_structured(criterion_text: str, evidence_text: str) -> Check:
    """근거가 프로필 구조화 필드(나이·성별·ECOG)뿐인 확정 판정.

    개념군 기준에는 구조화 필드가 직접 근거가 되지 않음 (예: 나이만으로 임신 가능성 없음, 성별만으로 알레르기 없음).
    예외: 임신·수유 기준에서 성별(남성)은 대상 집단을 직접 정함.
    """
    concept = next((k for k in CONCEPTS if k.criterion.search(criterion_text)), None)
    if concept is None:
        return Check("structured")
    refs = {m.lower() for m in _FIELD_REF.findall(evidence_text)}
    if concept.name == "pregnancy_lactation" and "sex" in refs:
        return Check("structured", [], concept.name)
    return Check("indirect", ["indirect_evidence"], concept.name)
