"""값이 적혀 있어도 환자 본인의 유효한 현재 측정값이 아니면 근거가 아님 (합성 데이터, LLM 호출 없음).

P0 후보에서 `Hb 12 g/dL 미측정` 등 11개 표현이 규칙 엔진, ContractSession, 문서 필수 기준, LLM 인용 경로로 확정 적격에 닿고
환자 사실로 저장되었습니다. 표현 하나마다 같은 낱말을 쓰는 정상 양성 대조군을 붙입니다. LLM 자리는 고정 대역이며,
프로필러가 값을 옮긴 최악의 경우입니다. 실제 LLM의 발생 빈도를 재지 않습니다. 표현 목록은 임상 검수를 받지 않았습니다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import test_p0_session_grounding as tsg  # noqa: E402
from test_expert_boundary import _run  # noqa: E402
from test_offline import _rule  # noqa: E402
from test_p0_session_grounding import (AGE, ECOG, HB, HB_HIGH, OPEN, PID, Q_HB, RECORD, SELF_OK, SEX, confirmed, hb, held, prof, run)  # noqa: E402,F401

from ctrec import profile_facts, value_grounding as vg  # noqa: E402
from ctrec.contract_session import ContractSession  # noqa: E402
from ctrec.fact_store import FactStore  # noqa: E402
from ctrec.schemas import LabValue, RuleMeta  # noqa: E402

U = "unlocated"
HOLD, OK = ("UNCERTAIN", "INFO_MISSING"), ("ELIGIBLE", "CONFIRMED_ELIGIBLE")
FIELD_OK = {"I1": RuleMeta(required_evidence="self_report_ok"), "I2": RuleMeta(required_evidence="self_report_ok")}
HB_NARRATIVE = _rule(rule_id="I3", text="Adequate hemoglobin (Hb >= 9 g/dL)", category="lab", structured_evaluable=False)

# 탐침에서 뚫린 표현 (구조 결함: 주어 없는 뒤 절, 줄 나눔, 완료로 잘못 읽은 '안 나옴'). 목록 밖 표현이 더 있다는 뜻이기도 함
LATER_CLAUSE = ["Hb 12 g/dL, 결과 대기", "Hb 12 g/dL, 미측정", "Hb 12 g/dL, 무효", "Hb 12 g/dL. 결과는 아직 안 나왔어요", "Hb 12 g/dL\n상태: 용혈", "Hb 12 g/dL\n위 결과 무효", "CBC 결과 안 나옴, Hb 12 g/dL",
                "Hb 12 g/dL로 입력했지만 실제 측정은 안 함", "Hb 12 g/dL 입력값, 실제 검사 안 함", "검사 결과 Hb 12 g/dL 였으나 취소됨"]
OTHER_WORDS = ["Hb 12 g/dL 보류", "Hb 12 g/dL TBD", "Hb 12 g/dL 미정", "Hb 12 g/dL 재채혈 요망", "Hb 12 g/dL not valid", "Hb 12 g/dL 신뢰 불가", "Hb 12 g/dL 펜딩", "Hb 12 g/dL 팬딩", "Hb 12 g/dL (시뮬레이션)",
               "Hb 12 g/dL (테스트)", "demo Hb 12 g/dL", "대조군 Hb 12 g/dL", "환자군 Hb 12 g/dL", "Hb 12 g/dL 차트 혼입", "wrong chart Hb 12 g/dL", "Hb 12 g/dL 검체 분실", "Hb 12 g/dL 검체 누락"]
STILL_MEASURED = ["Hb 12 g/dL 측정 오류 아님", "혈액검사 오류 수정 후 Hb 12 g/dL", "CBC 결과 대기 후 Hb 12 g/dL 확인", "Hb 12 g/dL 보험 심사 대기", "Hb 12 g/dL 동의서 대기", "Hb 12 g/dL로 항암 보류",
                  "Hb 12 g/dL, 수술 일정 미정", "CBC 결과 나옴, Hb 12 g/dL", "CBC: Hb 12 g/dL, 결과 대기 항목 없음", "Hb 12 g/dL, Plt 210 x10^9/L", "Hb 12 g/dL. 병리 결과 대기"]

# 표현, 부정 사례, 같은 낱말을 쓰는 정상 양성 대조군
ELEVEN = [
    ("미측정", "Hb 12 g/dL 미측정", "Hb 12 g/dL, CRP 미측정"),
    ("미시행", "Hb 12 g/dL 미시행", "Hb 12 g/dL이고 CRP는 미시행"),
    ("결과 대기", "Hb 12 g/dL 결과 대기", "Hb 12 g/dL, 병리 결과 대기"),
    ("pending", "Hb 12 g/dL pending", "Hb 12 g/dL and culture pending"),
    ("not done", "Hb 12 g/dL not done", "Hb 12 g/dL, CRP not done"),
    ("ordered", "Hb 12 g/dL ordered", "Hb 12 g/dL and CT ordered"),
    ("requested", "Hb 12 g/dL requested", "Hb 12 g/dL, MRI requested"),
    ("오류로 폐기", "Hb 12 g/dL 오류로 폐기", "이전 검체 오류로 폐기, 재검 Hb 12 g/dL"),
    ("검체 용혈로 무효", "Hb 12 g/dL 검체 용혈로 무효", "이전 검체 용혈로 무효, 재검 Hb 12 g/dL"),
    ("기관 기준", "기관 기준 Hb 12 g/dL", "Hb 12 g/dL (기관 기준 범위 내)"),
    ("기증자", "기증자 Hb 12 g/dL", "환자 Hb 12 g/dL, 기증자 Hb 13 g/dL"),
]
IDS = [x[0] for x in ELEVEN]

NOT_MEASURED = {
    "not_done": ["미측정 Hb 12 g/dL", "미시행: Hb 12 g/dL", "Hb 미측정 12 g/dL", "Hb 12 g/dL (미측정)", "Hb 12 g/dL (not done)", "Hb 12 g/dL 미실시", "Hb 12 g/dL 미검",
                 "Hb 12 g/dL not performed", "Hb 12 g/dL was not measured", "Hb 12 g/dL 검사 안 함", "Hb 12 g/dL 측정 안 함", "Hb 12 g/dL 검사 못 함", "Hb not done 12 g/dL",
                 "Hb | 12 | g/dL | 미측정", "Hb | 12 | g/dL | not done", "Hb 12 미측정", "HB 12 G/DL NOT DONE", "Hb 12 g/dL 측정 전", "Hb 12 g/dL 측정 불가",
                 "Hb 12 g/dL unable to measure"],
    "pending": ["Hb 12 g/dL 결과 대기 중", "Hb 12 g/dL 대기 중", "Hb 12 g/dL result pending", "pending Hb 12 g/dL", "Hb pending 12 g/dL", "Hb 12 g/dL (pending)", "결과 대기: Hb 12 g/dL",
                "Hb 12 g/dL awaiting result", "Hb 12 g/dL 결과 미수신", "Hb 12 g/dL 결과 안 나옴", "Hb 12 g/dL 결과 아직", "Hb | 12 | g/dL | pending", "Hb 12 g/dL 분석 중",
                "Hb 12 g/dL in progress", "CBC 결과 대기, Hb 12 g/dL", "CBC pending, Hb 12 g/dL", "Hb 12 g/dL 재검 대기", "Hb 12 g/dL pending review", "Hb 12 g/dL 이고 결과 대기",
                "Hb 12 g/dL and result pending"],
    "ordered": ["ordered Hb 12 g/dL", "requested Hb 12 g/dL", "Hb 12 g/dL 오더", "Hb 12 g/dL 처방", "Hb 12 g/dL 의뢰", "Hb 12 g/dL 요청", "Hb 12 g/dL 접수", "Hb 12 g/dL 채혈 예정",
                "Hb 12 g/dL scheduled", "Hb 12 g/dL 검사 신청", "Hb 12 g/dL was ordered", "Hb ordered 12 g/dL", "내일 Hb 12 g/dL 확인 예정", "Hb 12 g/dL 측정 예정",
                "Hb 12 g/dL 다음 주 예정", "채혈 예정 Hb 12 g/dL", "CBC 오더 Hb 12 g/dL"],
    "invalid": ["Hb 12 g/dL 폐기", "Hb 12 g/dL 무효", "Hb 12 g/dL 용혈", "Hb 12 g/dL hemolyzed", "Hb 12 g/dL invalid", "Hb 12 g/dL discarded", "Hb 12 g/dL rejected", "Hb 12 g/dL voided",
                "Hb 12 g/dL lab error", "Hb 12 g/dL clotted sample", "Hb 12 g/dL 검체 응고", "Hb 12 g/dL 검체 오염", "Hb 12 g/dL 부적합 검체", "Hb 12 g/dL 측정 오류", "용혈 검체 Hb 12 g/dL",
                "무효 Hb 12 g/dL", "hemolyzed sample Hb 12 g/dL", "오류로 폐기된 Hb 12 g/dL", "Hb 12 g/dL. 해당 검체 용혈로 무효", "Hb 12 g/dL. 위 결과는 오류로 폐기",
                "Hb 12 g/dL. That result was invalid", "Hb | 12 | g/dL | 무효", "Hb 12 g/dL cancelled", "Hb 12 g/dL 기기 오류", "Hb 12 g/dL insufficient sample", "Hb 12 g/dL 검체 바뀜",
                "Hb 12 g/dL mislabeled"],
    "institution": ["기관 기준: Hb 12 g/dL", "본원 기준 Hb 12 g/dL", "병원 기준 Hb 12 g/dL", "검사실 기준 Hb 12 g/dL", "우리 병원 기준 Hb 12 g/dL", "학회 기준 Hb 12 g/dL",
                    "가이드라인 기준 Hb 12 g/dL", "institutional Hb 12 g/dL", "institution's Hb 12 g/dL", "guideline Hb 12 g/dL", "Hb 12 g/dL 기관 기준", "Hb 12 g/dL (기관 기준)",
                    "Hb 12 g/dL per institutional standard", "Hb 기관 기준 12 g/dL", "Hb 12 g/dL는 참고치", "Hb 12 g/dL 기준치", "Hb 12 g/dL 정상 하한", "Hb 12 g/dL for reference"],
    "other": ["기증자의 Hb 12 g/dL", "Hb 12 g/dL (기증자)", "Hb 12 g/dL 기증자", "공여자 Hb 12 g/dL", "Hb 12 g/dL (donor)", "조혈모세포 기증자 Hb 12 g/dL", "타인 Hb 12 g/dL",
              "Hb 12 g/dL는 기증자 수치", "헌혈자 Hb 12 g/dL", "다른 사람 Hb 12 g/dL", "동명이인 Hb 12 g/dL", "혈액 제공자 Hb 12 g/dL", "Hb 12 g/dL (다른 환자 검체)"],
}

MEASURED = {
    "not_done": ["Hb 12 g/dL", "Hb 12 g/dL 측정", "Hb 12 g/dL 측정함", "Hb 12 g/dL 측정 완료", "Hb 12 g/dL 시행", "Hb 12 g/dL done", "Hb 12 g/dL performed", "Hb 12 g/dL and CRP not done",
                 "CRP 미시행, Hb 12 g/dL", "CRP not done, Hb 12 g/dL", "Hb 12 g/dL (CRP 미측정)", "Hb 12 g/dL, 혈소판 미측정", "Hb | 12 | g/dL | 정상", "Hb 12 g/dL 측정 후 항암",
                 "Hb 12 g/dL 검사 안내 받음", "Hb 12 g/dL 측정 완료, CT 전"],
    "pending": ["Hb 12 g/dL 결과 확인", "Hb 12 g/dL 결과 보고됨", "Hb 12 g/dL, culture pending", "Hb 12 g/dL; 병리 결과 대기 중", "Hb 12 g/dL (병리 pending)", "병리 결과 대기, Hb 12 g/dL",
                "Hb 12 g/dL final", "Hb 12 g/dL resulted", "Hb 12 g/dL, no pending labs", "Hb 12 g/dL 이고 병리 결과 대기", "Hb 12 g/dL pending surgery", "Hb 12 g/dL awaiting chemo",
                "pending surgery Hb 12 g/dL", "상담 대기 Hb 12 g/dL", "Hb 12 g/dL 대기 없음"],
    "ordered": ["Hb 12 g/dL 검사 완료", "Hb 12 g/dL 결과 회신", "CT ordered, Hb 12 g/dL", "철분제 처방, Hb 12 g/dL", "Hb 12 g/dL, 다음 주 항암 예정", "Hb 12 g/dL로 항암 예정", "CBC 결과 Hb 12 g/dL",
                "CBC 시행 Hb 12 g/dL", "Hb 12 g/dL 항암 처방", "다음 주 혈액검사 예정, Hb 12 g/dL", "Hb 12 g/dL, 재검 예정", "Hb 12 g/dL. 재검 예정", "수술 예정일 Hb 12 g/dL",
                "입원 예정 Hb 12 g/dL", "Hb 12 g/dL, 다음 달 CBC 오더", "Hb 12 g/dL 항암 대기", "Hb 12 g/dL 투약 예정", "등록 예정 Hb 12 g/dL", "CBC ordered on 3/1 and resulted: Hb 12 g/dL"],
    "invalid": ["용혈 없음, Hb 12 g/dL", "Hb 12 g/dL 용혈 없음", "no hemolysis, Hb 12 g/dL", "Hb 12 g/dL, no hemolysis", "Hb 12 g/dL 검체 적합", "용혈성 빈혈 병력, Hb 12 g/dL", "용혈성 빈혈 병력 Hb 12 g/dL",
                "재검 Hb 12 g/dL", "Hb 12 g/dL 유효", "Hb 12 g/dL valid", "Hb 12 g/dL 오류 없음", "hemolytic anemia history, Hb 12 g/dL", "hemolytic anemia Hb 12 g/dL", "Hb 12 g/dL 재검 결과",
                "지난주 Hb 결과 무효, 오늘 Hb 12 g/dL", "이전 Hb 8 g/dL 용혈로 무효. 재검 Hb 12 g/dL", "이전 CBC 무효, 재검 Hb 12 g/dL", "CBC 무효, 재검 Hb 12 g/dL", "Hb 8 g/dL 용혈로 무효, Hb 12 g/dL", "Hb 12 g/dL 무효 아님", "Hb 12 g/dL (not hemolyzed)",
                "Hb 12 g/dL no error"],
    "institution": ["오늘 기준 Hb 12 g/dL", "2026-03-01 기준 Hb 12 g/dL", "최근 기준 Hb 12 g/dL", "현재 기준 Hb 12 g/dL", "기관 검사 Hb 12 g/dL", "본원 검사 Hb 12 g/dL", "본원 Hb 12 g/dL",
                    "본원 검사실 Hb 12 g/dL", "외부 병원 Hb 12 g/dL", "타 병원 Hb 12 g/dL", "at our institution Hb 12 g/dL", "Hb 12 g/dL, 기관 기준 정상", "Hb 12 g/dL (기준치 12-16)",
                    "Hb 12 g/dL (참고치 12-16)", "Hb 12 g/dL (ref 12-16)", "Hb 12 g/dL (정상)"],
    "other": ["본인 Hb 12 g/dL", "환자 본인 Hb 12 g/dL", "기증자 Hb 13 g/dL, 환자 Hb 12 g/dL", "기증자 검사 정상, Hb 12 g/dL", "골수 기증자 없음, Hb 12 g/dL", "기증자 매칭 중, Hb 12 g/dL",
              "기증받은 신장 이식 후 Hb 12 g/dL", "donor transplant done, Hb 12 g/dL", "환자 Hb 12 g/dL, 기증자 정보 없음", "공여자 없음, Hb 12 g/dL", "참여자 Hb 12 g/dL"],
}

ECOG_NOT = ["ECOG 1 미측정", "ECOG 1 미시행", "ECOG 1 평가 예정", "ECOG 1 결과 대기", "ECOG 1 pending", "ECOG 1 not done", "ECOG 1 ordered", "ECOG 1 requested", "ECOG 1 오류로 폐기", "ECOG 1 무효",
            "기관 기준 ECOG 1", "기증자 ECOG 1", "donor ECOG 1", "ECOG 미측정 1", "ECOG 1 평가 불가", "ECOG 1, 미측정", "ECOG 1. 위 결과 무효"]
ECOG_YES = ["ECOG 1", "ECOG 1, CT 미시행", "ECOG 1, CT pending", "ECOG 1 평가 완료", "ECOG 1 측정", "CT ordered, ECOG 1", "ECOG 1이고 CT 결과 대기", "ECOG 1 and CT pending", "기증자 ECOG 0, 환자 ECOG 1",
            "ECOG 1, 검사 예정", "ECOG 1 수술 예정"]
AGE_NOT = ["기증자 62세", "62세 기증자", "기증자 나이 62세", "기증자의 나이는 62세", "나이 62세 오류로 폐기", "기관 기준 62세", "나이 62세 미확인", "donor age 62", "62-year-old donor", "62세 여성 기증자",
           "62-year-old female donor", "나이 62세 기증자", "기증자 1964년생", "age 62 pending"]
AGE_YES = ["62세", "62세, CT 미시행", "62세, 기증자 40세", "기증자 40세, 환자 62세", "나이 62세", "나이 62.", "나이 62. 폐암", "62세 남성", "62-year-old man", "62세, CT pending", "62세 남성 결과 대기",
           "62세 남성 CT 예정", "1964년생", "62세 오늘 내원"]
SEX_NOT = ["기증자 여성", "여성 기증자", "여성 공여자", "female donor", "donor is female"]
SEX_YES = ["여성", "여자", "여성, 기증자 남성", "여성 환자", "female patient", "기증자 남성, 환자 여성", "여자 환자"]
BARE_NOT = ["12 pending", "12 ordered", "12 requested", "12 미측정", "12 미시행", "12 결과 대기", "12 not done", "12 오류로 폐기", "12 무효", "12 g/dL pending", "12 g/dL 미측정", "12 g/dL 결과 대기",
            "12요 결과 대기", "기관 기준 12", "기증자 12", "12 invalid", "12 hemolyzed", "12 discarded", "12 cancelled", "12 yesterday"]
BARE_YES = ["12", "12 g/dL", "12입니다", "12요", "12 g/dL입니다", "12 g/dl"]


def flat(table):
    return [(c, t) for c, ts in table.items() for t in ts]


def wrap(text):
    return f"폐암 환자. {text}."


def lab(text, value=12.0, source=None):
    return vg.ground_lab("hemoglobin", value, text, source or wrap(text), unit="g/dL", known={"12"})


def _llm(monkeypatch, rule, quote):
    """규칙 엔진 없이 LLM이 원문을 인용해 충족이라고 답한 경우. 반환: (판정, PairStatus)."""
    x = _run(monkeypatch, "T1", [rule], {rule.rule_id: RuleMeta(required_evidence="self_report_ok")}, {rule.rule_id: ("yes", quote)}, wrap(quote))
    return x["match"].eligibility, x["pair"].status.value


def session_rule(run, text, **kw):
    return run(wrap(text), [HB], prof(labs=[hb(quote=text)]), meta=SELF_OK, **kw)


@pytest.fixture
def contract_run(run, monkeypatch):
    monkeypatch.setattr(tsg, "Session", ContractSession)
    return run


# ---------------------------------------------------------------------------
# 11개 표현: 경로별 부정 사례와 양성 대조군
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("_, negative, positive", ELEVEN, ids=IDS)
def test_session_holds_the_expression_and_keeps_the_control(run, _, negative, positive):
    o = session_rule(run, negative)
    held(o, "INFO_MISSING")
    assert "structured_unlocated" in o.a.validation_flags or "structured_conflict" in o.a.validation_flags
    confirmed(session_rule(run, positive))


@pytest.mark.parametrize("_, negative, positive", ELEVEN, ids=IDS)
def test_contract_session_holds_the_expression_and_keeps_the_control(contract_run, _, negative, positive):
    held(session_rule(contract_run, negative))
    confirmed(session_rule(contract_run, positive))


@pytest.mark.parametrize("_, negative, positive", ELEVEN, ids=IDS)
def test_expression_does_not_rule_a_patient_out(run, _, negative, positive):
    """부적격 쪽도 같음: 측정값이 아니면 불충족 근거도 아니고, 측정값이면 불충족은 유지됨."""
    held(run(wrap(negative), [HB_HIGH], prof(labs=[hb(quote=negative)]), meta=SELF_OK))
    assert run(wrap(positive), [HB_HIGH], prof(labs=[hb(quote=positive)]), meta=SELF_OK).match.eligibility == "INELIGIBLE"


@pytest.mark.parametrize("_, negative, positive", ELEVEN, ids=IDS)
def test_llm_quote_holds_the_expression_and_keeps_the_control(monkeypatch, _, negative, positive):
    assert _llm(monkeypatch, HB_NARRATIVE, negative) == HOLD
    assert _llm(monkeypatch, HB_NARRATIVE, positive) == OK


@pytest.mark.parametrize("_, negative, positive", ELEVEN, ids=IDS)
def test_record_only_rule_is_not_confirmed_by_the_expression(run, _, negative, positive):
    """문서 필수 기준: 측정값이 아닌 표현은 선언된 기록이어도 통과 근거가 아님."""
    o = run(wrap(negative), [HB], prof(labs=[hb(quote=negative)]), meta=RECORD, input_source="clinical_record")
    assert o.status != "CONFIRMED_ELIGIBLE" and o.a.passes is None


@pytest.mark.parametrize("_, negative, positive", ELEVEN, ids=IDS)
def test_server_attestation_does_not_turn_the_expression_into_a_value(run, monkeypatch, _, negative, positive):
    """문서 게이트가 있는 트리: 합성 서버 증명 대조군은 확정되고, 측정값이 아닌 표현은 증명 경로에도 사실로 올라오지 않음."""
    pytest.importorskip("ctrec.document_authority")
    from document_support import attest_declared
    real = tsg.pipeline.Session.match

    def match(self, *a, **k):
        attest_declared(self.fact_store, PID)
        return real(self, *a, **k)

    monkeypatch.setattr(tsg.pipeline.Session, "match", match)
    o = run(wrap(negative), [HB], prof(labs=[hb(quote=negative)]), meta=RECORD, input_source="clinical_record")
    assert o.status != "CONFIRMED_ELIGIBLE" and o.a.passes is None
    confirmed(run(wrap(positive), [HB], prof(labs=[hb(quote=positive)]), meta=RECORD, input_source="clinical_record"))


# ---------------------------------------------------------------------------
# 전체 표현: 값 근거 판정
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind, text", [*flat(NOT_MEASURED), *(("later", t) for t in LATER_CLAUSE), *(("word", t) for t in OTHER_WORDS)])
def test_not_a_measurement(kind, text):
    assert lab(text) in (U, vg.CONFLICT)


@pytest.mark.parametrize("kind, text", [*flat(MEASURED), *(("control", t) for t in STILL_MEASURED)])
def test_measurement_stays(kind, text):
    assert lab(text) is None


@pytest.mark.parametrize("text", ECOG_NOT)
def test_ecog_not_a_patient_value(text):
    assert vg.ground("ecog", 1, wrap(text)) == U


@pytest.mark.parametrize("text", ECOG_YES)
def test_ecog_stays(text):
    assert vg.ground("ecog", 1, wrap(text)) is None


@pytest.mark.parametrize("text", AGE_NOT)
def test_age_not_a_patient_value(text):
    assert vg.ground("age", 62, wrap(text), reference_date="2026-11-07") == U


@pytest.mark.parametrize("text", AGE_YES)
def test_age_stays(text):
    assert vg.ground("age", 62, wrap(text), reference_date="2026-11-07") is None


@pytest.mark.parametrize("text", SEX_NOT)
def test_sex_not_the_patients(text):
    assert vg.ground("sex", "female", wrap(text)) == U


@pytest.mark.parametrize("text", SEX_YES)
def test_sex_stays(text):
    assert vg.ground("sex", "female", wrap(text)) is None


@pytest.mark.parametrize("negative, positive", [("ECOG 1 미측정", "ECOG 1, CT 미시행"), ("기증자 ECOG 1", "기증자 ECOG 0, 환자 ECOG 1"), ("ECOG 1 pending", "ECOG 1 and CT pending")])
def test_ecog_in_session(run, negative, positive):
    held(run(wrap(negative), [ECOG], prof(ecog=1), meta=FIELD_OK), "INFO_MISSING")
    confirmed(run(wrap(positive), [ECOG], prof(ecog=1), meta=FIELD_OK))


@pytest.mark.parametrize("negative, positive", [("기증자 62세", "62세, 기증자 40세"), ("나이 62세 오류로 폐기", "나이 62세, CT 오류로 재촬영")])
def test_age_in_session(run, negative, positive):
    held(run(wrap(negative), [AGE], prof(age=62), meta=FIELD_OK), "INFO_MISSING")
    confirmed(run(wrap(positive), [AGE], prof(age=62), meta=FIELD_OK))


def test_donor_word_is_not_a_female_marker(run):
    held(run("폐암 환자. 공여자 정보 없음.", [SEX], prof(sex="female"), meta=FIELD_OK), "INFO_MISSING")


# ---------------------------------------------------------------------------
# N2 나이 회귀, fact_line
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [("나이 62.", None), ("나이 62. 폐암", None), ("Age: 62.", None), ("나이 62.5", U), ("나이 62.5세", U)])
def test_age_period_regression_stays_fixed(text, expected):
    assert vg.ground("age", 62, text) == expected


def test_age_period_in_session(run):
    confirmed(run("폐암 환자, 나이 62.", [AGE], prof(age=62), meta=FIELD_OK))


FACT_LINES = "\n".join(["나이 62세", "성별 female", "ECOG 1", "hemoglobin 12 g/dL", "platelets 210 x10^9/L", "absolute neutrophil count 2.1 x10^9/L", "creatinine clearance 60 mL/min",
                        "total bilirubin 0.8 mg/dL", "diagnosis: 비소세포폐암", "biomarker: EGFR 양성"])


@pytest.mark.parametrize("lab_name, value, unit", [("hemoglobin", 12.0, "g/dL"), ("platelets", 210.0, "x10^9/L"), ("absolute neutrophil count", 2.1, "x10^9/L"),
                                                   ("creatinine clearance", 60.0, "mL/min"), ("total bilirubin", 0.8, "mg/dL")])
def test_fact_line_labs_stay(lab_name, value, unit):
    line = f"{lab_name} {value:g} {unit}"
    assert vg.ground_lab(lab_name, value, line, FACT_LINES, unit=unit) is None


@pytest.mark.parametrize("field, value", [("age", 62), ("sex", "female"), ("ecog", 1)])
def test_fact_line_fields_stay(field, value):
    assert vg.ground(field, value, FACT_LINES) is None


@pytest.mark.parametrize("field, value, unit", [("lab:hemoglobin", 12.0, "g/dL"), ("lab:platelets", 210, "x10^9/L"), ("lab:absolute neutrophil count", 2.1, "x10^9/L"),
                                                 ("lab:creatinine clearance", 60.0, "mL/min"), ("age", 62, "years"), ("sex", "female", None), ("ecog", 1, None)])
def test_real_fact_line_stays_a_measurement(field, value, unit):
    """PR #20의 시뮬레이터 원문 한 줄이 정상 값으로 읽히는가. PR #20이 없는 트리에서는 건너뜀."""
    evaluator = pytest.importorskip("ctrec.sim.evaluator")
    from ctrec.sim.cohort import SimFact
    line = evaluator.fact_line(SimFact(field=field, value=value, unit=unit))
    if field.startswith("lab:"):
        assert vg.ground_lab(field[4:], float(value), line, line, unit=unit) is None
        assert not vg.not_measured(field, float(value), line, line, unit=unit)
    else:
        assert vg.ground(field, value, line) is None
        assert not vg.not_measured(field, value, None, line)


# ---------------------------------------------------------------------------
# 답변 경로
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("answer", BARE_NOT)
def test_bare_answer_with_a_status_is_not_a_value(run, answer):
    o = run("폐암 환자.", [OPEN, HB], prof(), after=prof(labs=[hb(quote=answer)]), answer=answer, meta=SELF_OK, question=Q_HB, target="T1:I3")
    a = next(a for a in o.match.assessments if a.rule_id == "I3")
    assert a.passes is None


@pytest.mark.parametrize("answer", BARE_YES)
def test_bare_answer_with_a_unit_stays(run, answer):
    o = run("폐암 환자.", [OPEN, HB], prof(), after=prof(labs=[hb(quote=answer)]), answer=answer, meta=SELF_OK, question=Q_HB, target="T1:I3")
    a = next(a for a in o.match.assessments if a.rule_id == "I3")
    assert a.passes is True


@pytest.mark.parametrize("answer", ["혈색소는 아직 결과 대기예요", "혈색소 검사는 안 했어요", "Hb 결과 무효래요", "아직 결과 대기예요", "그 검사는 무효예요"])
def test_answer_voids_the_value_in_the_record(run, answer):
    """기록의 수치를 답변이 무력화: 어느 쪽이 맞는지 모르므로 보류. 문답은 저 수치를 물은 질문에 연결됨."""
    o = run("폐암 환자. Hb 12 g/dL.", [OPEN, HB], prof(labs=[hb()]), answer=answer, meta=SELF_OK, question=Q_HB, target="T1:I3")
    a = next(a for a in o.match.assessments if a.rule_id == "I3")
    assert a.passes is None and "structured_conflict" in a.validation_flags


def test_answer_voids_the_ecog_in_the_record(run):
    """나이·ECOG도 같음: 그 값을 물은 문답에 아직 없다고 답하면 기록의 값으로 확정하지 않음."""
    o = run("폐암 환자. ECOG 1.", [OPEN, ECOG], prof(ecog=1), answer="아직 평가 미시행", meta=FIELD_OK, question="ECOG 활동도는 어떻게 되나요?", target="T1:I2")
    a = next(a for a in o.match.assessments if a.rule_id == "I2")
    assert a.passes is None and "structured_conflict" in a.validation_flags


def test_answer_about_another_test_keeps_the_ecog(run):
    o = run("폐암 환자. ECOG 1.", [OPEN, ECOG], prof(ecog=1), answer="CT는 아직 결과 대기예요", meta=FIELD_OK, question="다른 검사도 받으셨나요?", target="T1:I9")
    a = next(a for a in o.match.assessments if a.rule_id == "I2")
    assert a.passes is True


@pytest.mark.parametrize("answer", ["네 맞아요", "CRP는 아직 결과 대기예요", "CT는 무효예요"])
def test_answer_about_something_else_keeps_the_value(run, answer):
    o = run("폐암 환자. Hb 12 g/dL.", [OPEN, HB], prof(labs=[hb()]), answer=answer, meta=SELF_OK, question="다른 검사도 받으셨나요?", target="T1:I9")
    a = next(a for a in o.match.assessments if a.rule_id == "I3")
    assert a.passes is True


# ---------------------------------------------------------------------------
# 증거 권한: 자기 보고 허용 기준과 문서 필수 기준
# ---------------------------------------------------------------------------

def test_valid_answer_confirms_only_a_self_report_rule(run):
    """측정값이어도 권한은 그대로: 자기 보고 허용 기준은 통과, 문서 필수 기준은 답변만으로 통과하지 않음."""
    kw = dict(after=prof(labs=[hb(quote="12 g/dL")]), answer="12 g/dL", question=Q_HB, target="T1:I3")
    allowed = run("폐암 환자.", [OPEN, HB], prof(), meta=SELF_OK, **kw)
    assert next(a for a in allowed.match.assessments if a.rule_id == "I3").passes is True
    record = run("폐암 환자.", [OPEN, HB], prof(), meta=RECORD, **kw)
    a = next(a for a in record.match.assessments if a.rule_id == "I3")
    assert a.passes is None and a.needs_verification and a.self_reported_holds == "yes"


def test_status_answer_is_not_kept_as_a_self_reported_value(run):
    """문서 필수 기준에서 무효 답변을 '답변상 충족'으로 남기지 않음: 문서 확인으로 넘겨도 답변 값으로 꾸미지 않음."""
    kw = dict(after=prof(labs=[hb(quote="12 pending")]), answer="12 pending", question=Q_HB, target="T1:I3")
    o = run("폐암 환자.", [OPEN, HB], prof(), meta=RECORD, **kw)
    a = next(a for a in o.match.assessments if a.rule_id == "I3")
    assert a.passes is None and a.self_reported_holds is None


# ---------------------------------------------------------------------------
# LLM 인용
# ---------------------------------------------------------------------------

BIOMARKER = _rule(rule_id="I3", text="EGFR mutation positive", category="biomarker", structured_evaluable=False)
ORGAN = _rule(rule_id="I3", text="Received a transfusion before enrollment", category="comorbidity", structured_evaluable=False)
ECOG_NARRATIVE = _rule(rule_id="I3", text="Good performance status", category="performance_status", structured_evaluable=False)


@pytest.mark.parametrize("rule, negative, positive", [
    (BIOMARKER, "EGFR L858R 결과 대기", "EGFR L858R 양성, PD-L1 결과 대기"),
    (BIOMARKER, "EGFR L858R pending", "EGFR L858R 양성 and PD-L1 pending"),
    (BIOMARKER, "EGFR 유전자 검사 결과 대기", "EGFR 유전자 검사 양성"),
    (BIOMARKER, "부친 EGFR L858R 양성", "EGFR L858R 양성"),
    (ECOG_NARRATIVE, "ECOG 1 미측정", "ECOG 1, CT 미측정"),
    (ECOG_NARRATIVE, "기증자 ECOG 1", "기증자 ECOG 0, 환자 ECOG 1"),
])
def test_llm_quote_in_other_measured_categories(monkeypatch, rule, negative, positive):
    assert _llm(monkeypatch, rule, negative) == HOLD
    assert _llm(monkeypatch, rule, positive) == OK


def test_llm_rule_that_does_not_ask_for_a_measurement_is_untouched(monkeypatch):
    """측정값을 묻지 않는 기준은 이 검사의 대상이 아님: 수혈 사실의 근거로 인용된 '수혈 후 Hb 12'."""
    assert _llm(monkeypatch, ORGAN, "수혈 후 Hb 12 g/dL") == OK


# ---------------------------------------------------------------------------
# 사실 저장
# ---------------------------------------------------------------------------

def ingest(profile, text, **kw):
    store = FactStore()
    result = profile_facts.ingest_profile(store, profile, raw_text=text, input_source="self_report", session_id="S1", **kw)
    return store, result


@pytest.mark.parametrize("_, negative, positive", ELEVEN, ids=IDS)
def test_expression_does_not_become_a_patient_fact(_, negative, positive):
    store, r = ingest(prof(labs=[hb(quote=negative)]), wrap(negative))
    assert store.facts(PID) == [] and r.withheld == [("lab:hemoglobin", "not_measured")]
    store, r = ingest(prof(labs=[hb(quote=positive)]), wrap(positive))
    assert [f.value for f in store.facts(PID)] == [12.0] and r.withheld == []


@pytest.mark.parametrize("profile, text, field", [(prof(ecog=1), "ECOG 1 미측정.", "ecog"), (prof(age=62), "기증자 62세.", "age"), (prof(sex="female"), "기증자 여성.", "sex")])
def test_field_of_someone_else_does_not_become_a_fact(profile, text, field):
    store, r = ingest(profile, text)
    assert store.facts(PID) == [] and r.withheld == [(field, "not_measured")]


@pytest.mark.parametrize("profile, text", [(prof(ecog=1), "ECOG 1."), (prof(age=62), "62세."), (prof(sex="female"), "여성."), (prof(labs=[hb()]), "Hb 12 g/dL.")])
def test_measured_fact_is_stored(profile, text):
    store, r = ingest(profile, text)
    assert len(store.facts(PID)) == 1 and r.withheld == []


@pytest.mark.parametrize("profile, text", [(prof(ecog=1), "폐암 환자."), (prof(labs=[LabValue(name="hemoglobin", value=12.0, unit="g/dL", date=None, source_quote="다른 문장")]), "폐암 환자.")])
def test_value_absent_from_the_text_keeps_the_existing_path(profile, text):
    """적혀 있지 않은 값은 이 검사의 대상이 아님: 지금처럼 출처 unknown으로 저장되고 판정에서 보류됨."""
    store, r = ingest(profile, text)
    assert len(store.facts(PID)) == 1 and r.withheld == []


def test_held_value_leaves_no_fact_for_a_later_overlay(run):
    o = session_rule(run, "Hb 12 g/dL 결과 대기")
    assert o.session.fact_store.facts(PID, "lab:hemoglobin") == []
