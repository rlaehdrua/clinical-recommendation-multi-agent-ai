"""명제(Proposition)와 명제 키, 코드로 승인된 함의 관계 (재설계 P2).

임상시험 기준과 확인 질문이 '같은 사실'을 가리키는지는 문장 유사도나 target 문자열이 아니라 명제 키로 판단합니다.
명제 = (개념 Concept, 한정어 qualifiers, 양상 Aspect, 시간 범위 TimeScope).

- Concept   : 아래 CONCEPTS 표에 등록된 개념만 씁니다. 상위 개념(parent)은 코드로 승인된 포함 관계입니다
              (예: rituximab ⊂ anti_cd20_antibody). 등록되지 않은 개념은 명제가 될 수 없습니다.
- qualifiers: 개념을 좁히는 한정어(예: surgery + major). 한정어가 많을수록 좁은 명제입니다.
              한정어 사이의 의미 관계는 정의하지 않습니다(다른 한정어 = 다른 명제).
- Aspect    : event(행위·노출이 일어남: 수술, 투약, 백신) / condition(질환·상태가 있음: 임신, 감염, 진단)
              / measurement(수치). 양상이 다르면 함의 관계가 없습니다(수술 '부작용'은 별개 개념).
- TimeScope : current(평가 시점에 진행 중) / window(N일, 기준 시점 anchor) / ever(평생).
              anchor = assessment(평가·답변 시점) | enrollment | screening | randomization | first_dose.
              환자 답변은 항상 assessment 기준입니다. 다른 anchor를 assessment로 근사할지는 IR 검수 사항입니다
              (condition_ir.CriterionIR.anchor_policy). 근사가 승인되지 않으면 평가기는 판정하지 않습니다.

함의 (narrower ⊑ broader, 같은 양상 안에서만)
- 개념: 좁은 쪽 개념이 넓은 쪽 개념과 같거나 그 하위 개념
- 한정어: 좁은 쪽 한정어 ⊇ 넓은 쪽 한정어
- 시간: current ⊑ window(N, a) ⊑ window(M, a) (N ≤ M, 같은 anchor) ⊑ ever. measurement는 같은 키만 같음
- 좁은 명제가 참이면 넓은 명제도 참, 넓은 명제가 거짓이면 좁은 명제도 거짓. 그 밖의 방향은 결론을 내지 않습니다.
  (평생 수술 '있음'은 최근 4주 수술에 대해 아무것도 말하지 않음, 최근 4주 '없음'은 평생에 대해 아무것도 말하지 않음)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ASPECTS = ("event", "condition", "measurement")
ANCHORS = ("assessment", "enrollment", "screening", "randomization", "first_dose")


class PropositionError(ValueError):
    """등록되지 않은 개념, 잘못된 키 등."""


@dataclass(frozen=True)
class Concept:
    concept_id: str
    label_ko: str
    label_en: str
    aspect: str
    parent: str | None = None


def _c(cid, ko, en, aspect, parent=None):
    return Concept(cid, ko, en, aspect, parent)


# 개념 표. 상위 개념은 '하위가 참이면 상위도 참'이 의학적으로 명백한 포함 관계만 둡니다(동의어·유사어를 묶지 않음).
_CONCEPT_LIST = [
    # 수술·시술
    _c("surgery", "수술", "surgery", "event"),
    _c("cholecystectomy", "담낭 절제술", "cholecystectomy", "event", "surgery"),
    _c("total_thyroidectomy", "갑상샘 전절제술", "total thyroidectomy", "event", "surgery"),
    _c("vitrectomy", "유리체 절제술", "vitrectomy", "event", "surgery"),
    _c("pancreatic_necrosectomy", "췌장 괴사 조직 제거술", "pancreatic necrosectomy", "event", "surgery"),
    # 항암·면역 치료
    _c("systemic_anticancer_therapy", "전신 항암 치료", "systemic anticancer therapy", "event"),
    _c("chemotherapy", "항암 화학요법", "chemotherapy", "event", "systemic_anticancer_therapy"),
    _c("cancer_immunotherapy", "항암 면역요법", "systemic cancer immunotherapy", "event", "systemic_anticancer_therapy"),
    _c("targeted_anticancer_therapy", "표적 항암 치료", "targeted anticancer therapy", "event",
       "systemic_anticancer_therapy"),
    _c("egfr_adc", "EGFR 표적 항체-약물 결합체", "EGFR-targeted antibody-drug conjugate", "event"),
    _c("cancer_vaccine", "암 백신", "cancer vaccine", "event"),
    _c("anti_cd38_therapy", "항CD38 치료", "anti-CD38 therapy", "event"),
    _c("anti_cd20_antibody", "항CD20 항체", "anti-CD20 antibody", "event"),
    _c("rituximab", "리툭시맙", "rituximab", "event", "anti_cd20_antibody"),
    _c("obinutuzumab", "오비누투주맙", "obinutuzumab", "event", "anti_cd20_antibody"),
    _c("tacrolimus", "타크로리무스", "tacrolimus", "event"),
    _c("cyclosporine", "사이클로스포린", "cyclosporine A", "event"),
    _c("mycophenolate_mofetil", "마이코페놀레이트 모페틸", "mycophenolate mofetil", "event"),
    _c("cyclophosphamide", "사이클로포스파마이드", "cyclophosphamide", "event"),
    _c("acth", "부신피질자극호르몬(ACTH)", "ACTH", "event"),
    _c("azathioprine", "아자티오프린", "azathioprine", "event"),
    _c("belimumab", "벨리무맙", "belimumab", "event"),
    _c("levamisole", "레바미솔", "levamisole", "event"),
    _c("radioactive_iodine_therapy", "방사성 요오드 치료", "radioactive iodine therapy", "event"),
    _c("ecmo", "체외막산소공급(ECMO)", "ECMO", "event"),
    _c("systemic_corticosteroid", "전신 스테로이드", "systemic corticosteroid", "event"),
    _c("amphotericin_b", "암포테리신 B", "amphotericin B", "event"),
    # 백신·시험 참여
    _c("vaccine", "백신 접종", "vaccination", "event"),
    _c("live_vaccine", "생백신 접종", "live or live-attenuated vaccine", "event", "vaccine"),
    _c("investigational_agent", "임상시험용 약", "investigational agent", "event"),
    _c("clinical_trial_participation", "다른 임상시험 참여", "participation in another clinical trial", "event"),
    _c("ebv_vaccine_trial_participation", "EBV 백신 임상시험 참여", "prior participation in an EBV vaccine trial",
       "event", "clinical_trial_participation"),
    _c("blood_product", "혈액 제제 투여", "blood products including immunoglobulin", "event"),
    # 생식
    _c("pregnancy", "임신", "pregnancy", "condition"),
    _c("lactation", "수유", "breastfeeding", "condition"),
    # 감염
    _c("hiv_infection", "HIV 감염", "HIV infection", "condition"),
    _c("active_tuberculosis", "활동성 결핵", "active tuberculosis", "condition"),
    _c("active_hepatitis_b", "활동성 B형 간염", "active hepatitis B", "condition"),
    _c("active_hepatitis_c", "활동성 C형 간염", "active hepatitis C", "condition"),
    _c("febrile_illness", "발열성 질환", "febrile illness", "condition"),
    _c("lower_respiratory_tract_infection", "하기도 감염", "lower respiratory tract infection", "condition"),
    # 병력
    _c("myocardial_infarction", "심근경색", "myocardial infarction", "condition"),
    _c("cerebral_infarction", "뇌경색", "cerebral infarction", "condition"),
    _c("guillain_barre_syndrome", "길랭-바레 증후군", "Guillain-Barre syndrome", "condition"),
    _c("angioedema", "혈관부종", "angioedema", "condition"),
    _c("idiopathic_urticaria", "원인 불명 두드러기", "idiopathic urticaria", "condition"),
    _c("asplenia", "비장이 없거나 기능하지 않음(비장 절제 포함)",
       "anatomic or functional asplenia, including removal of the spleen", "condition"),
    _c("coeliac_disease", "셀리악병", "coeliac disease", "condition"),
    _c("diabetes_type1", "제1형 당뇨병", "type 1 diabetes mellitus", "condition"),
    _c("diabetes_type2", "제2형 당뇨병", "type 2 diabetes mellitus", "condition"),
    _c("interstitial_lung_disease_steroid", "스테로이드 치료가 필요했던 간질성 폐질환",
       "interstitial lung disease requiring steroids", "condition"),
    _c("pancreatic_necrosis", "췌장 괴사", "pancreatic necrosis", "condition"),
    _c("pulmonary_mucormycosis", "폐 털곰팡이증", "pulmonary mucormycosis", "condition"),
    _c("retinal_detachment_family_history", "망막 박리 가족력", "family history of retinal detachment", "condition"),
    _c("nephrotic_family_history", "신증후군·만성 사구체신염·요독증 가족력",
       "family history of nephrotic syndrome, chronic glomerulonephritis or uremia", "condition"),
    _c("primary_immunodeficiency", "원발성 면역결핍", "primary immunodeficiency", "condition"),
    _c("kaposi_or_castleman", "카포시 육종 또는 다발성 캐슬만병", "Kaposi sarcoma or multicentric Castleman disease",
       "condition"),
    # 생활
    _c("smoking", "흡연", "smoking", "event"),
    _c("diabetes_gestational", "임신성 당뇨병", "gestational diabetes", "condition"),
]
CONCEPTS: dict[str, Concept] = {c.concept_id: c for c in _CONCEPT_LIST}

# 한정어 표: 개념을 좁히는 조건. 한정어 의미는 IR 검수 대상이며 코드가 서로 바꿔 쓰지 않습니다.
QUALIFIERS = {
    "major": "큰 수술(주요 수술)",
    "non_diagnostic": "진단 목적이 아닌",
    "systemic": "전신 투여",
    "for_detached_retina": "망막 박리 치료 목적",
    "successful": "성공적으로 치료됨",
    "over_5d": "투여 기간 5일 초과",
}


def ancestors(concept_id: str) -> list[str]:
    """자신부터 최상위까지 (자신 포함)."""
    if concept_id not in CONCEPTS:
        raise PropositionError(f"등록되지 않은 개념: {concept_id!r}")
    out = [concept_id]
    while CONCEPTS[out[-1]].parent:
        out.append(CONCEPTS[out[-1]].parent)  # type: ignore[arg-type]
    return out


@dataclass(frozen=True)
class TimeScope:
    kind: str  # current | window | ever
    days: int | None = None
    anchor: str = "assessment"

    def __post_init__(self):
        if self.kind not in ("current", "window", "ever"):
            raise PropositionError(f"잘못된 시간 범위: {self.kind!r}")
        if self.kind == "window":
            if not isinstance(self.days, int) or isinstance(self.days, bool) or self.days <= 0:
                raise PropositionError(f"기간은 양의 정수 일수: {self.days!r}")
            if self.anchor not in ANCHORS:
                raise PropositionError(f"잘못된 기준 시점: {self.anchor!r}")
        elif self.days is not None or self.anchor != "assessment":
            raise PropositionError(f"{self.kind} 범위에는 기간·기준 시점을 두지 않음")

    @property
    def token(self) -> str:
        return f"w{self.days}d-{self.anchor}" if self.kind == "window" else self.kind

    @classmethod
    def parse(cls, token: str) -> "TimeScope":
        if token in ("current", "ever"):
            return cls(token)
        m = re.fullmatch(r"w(\d+)d-([a-z_]+)", token)
        if not m:
            raise PropositionError(f"잘못된 시간 범위 표기: {token!r}")
        return cls("window", int(m.group(1)), m.group(2))

    def within(self, other: "TimeScope") -> bool:
        """self ⊑ other (self가 더 좁거나 같은 시간 범위)."""
        if self == other or other.kind == "ever":
            return True
        if self.kind == "current":  # 평가 시점에 진행 중 -> 평가 시점에서 끝나는 기간에는 포함 (다른 기준 시점은 모름)
            return other.kind == "window" and other.anchor == "assessment"
        if self.kind == "window" and other.kind == "window":
            return self.anchor == other.anchor and self.days <= other.days  # type: ignore[operator]
        return False


_KEY = re.compile(r"^([a-z0-9_]+)(?:\(([a-z0-9_.]+)\))?/(event|condition|measurement)/([a-z0-9_\-]+)$")


@dataclass(frozen=True)
class Proposition:
    concept: str
    aspect: str
    scope: TimeScope
    qualifiers: frozenset = frozenset()

    def __post_init__(self):
        if self.concept not in CONCEPTS:
            raise PropositionError(f"등록되지 않은 개념: {self.concept!r}")
        if self.aspect not in ASPECTS:
            raise PropositionError(f"잘못된 양상: {self.aspect!r}")
        if CONCEPTS[self.concept].aspect != self.aspect:
            raise PropositionError(f"{self.concept}의 양상은 {CONCEPTS[self.concept].aspect} (받은 값 {self.aspect})")
        bad = [q for q in self.qualifiers if q not in QUALIFIERS]
        if bad:
            raise PropositionError(f"등록되지 않은 한정어: {bad}")
        if self.aspect == "measurement" and self.scope.kind == "ever":
            raise PropositionError("수치 명제는 평생 범위를 쓰지 않음")

    @property
    def key(self) -> str:
        q = f"({'.'.join(sorted(self.qualifiers))})" if self.qualifiers else ""
        return f"{self.concept}{q}/{self.aspect}/{self.scope.token}"

    @classmethod
    def parse(cls, key: str) -> "Proposition":
        m = _KEY.match(key or "")
        if not m:
            raise PropositionError(f"잘못된 명제 키: {key!r}")
        concept, quals, aspect, scope = m.groups()
        return cls(concept, aspect, TimeScope.parse(scope), frozenset(quals.split(".")) if quals else frozenset())

    def narrower_or_equal(self, other: "Proposition") -> bool:
        """self ⊑ other: self가 참이면 other도 참 (코드로 승인된 함의만)."""
        if self.aspect != other.aspect:
            return False
        if self.aspect == "measurement":
            return self == other
        return (other.concept in ancestors(self.concept) and self.qualifiers >= other.qualifiers
                and self.scope.within(other.scope))

    def with_scope(self, scope: TimeScope) -> "Proposition":
        return Proposition(self.concept, self.aspect, scope, self.qualifiers)


def prop(concept: str, scope: str, *qualifiers: str) -> Proposition:
    """간편 생성: prop("surgery", "w28d-assessment", "major")."""
    if concept not in CONCEPTS:
        raise PropositionError(f"등록되지 않은 개념: {concept!r}")
    return Proposition(concept, CONCEPTS[concept].aspect, TimeScope.parse(scope), frozenset(qualifiers))


def normalize_key(key: str) -> str:
    """같은 명제의 다른 표기(한정어 순서 등)를 하나의 키로."""
    return Proposition.parse(key).key


def entails(fact: Proposition, fact_value: bool, target: Proposition) -> bool | None:
    """사실(fact = fact_value)에서 target의 참·거짓을 코드로 끌어낼 수 있으면 그 값, 아니면 None.

    좁은 명제가 참 -> 넓은 명제 참 / 넓은 명제가 거짓 -> 좁은 명제 거짓. 그 밖에는 None(결론 없음).
    """
    if fact_value is True and fact.narrower_or_equal(target):
        return True
    if fact_value is False and target.narrower_or_equal(fact):
        return False
    return None
