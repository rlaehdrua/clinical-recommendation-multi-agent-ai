"""P9: 기본 정책(legacy)에서 서비스 기본 동작이 바뀌지 않는다는 것을 코드 경로별로 확인.

보호 파일 변경(통합 후보 브랜치 한정)은 모두 정책 스위치 뒤에 있거나 기록용 필드만 더함:
- patient_profiler (01 v2): Profiler 프롬프트의 '확인 답변 충돌' 줄은 contract 정책에서만 바뀜. legacy 프롬프트는
  기준 커밋 65eaa0f와 글자 단위로 같음 (LLM 캐시 키·비용 추정 포함 그대로)
- matcher (03 v2): 계약 경계는 contract 정책에서만
- pipeline (02): QAPair.session_id 기록 (model_dump 제외, 판정에 쓰지 않음)
E9(evidence_directness) 수정은 legacy에도 적용되는 의도된 안전성 수정입니다(test_p9_e9_regression.py).
"""

from __future__ import annotations

import hashlib

import pytest

from ctrec import qa_guard
from ctrec.agents import patient_profiler
from ctrec.schemas import QAPair

# 65eaa0f src/ctrec/agents/patient_profiler.py의 SYSTEM 문자열 sha256
BASE_PROFILER_SYSTEM_SHA256 = "fae9705787c74d4788ab46bc19c6e4d008868aaf33866947a3e4560c69268241"


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("value", [None, "", "legacy"])
def test_legacy_profiler_prompt_is_byte_identical_to_base(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(qa_guard.POLICY_ENV, raising=False)
    else:
        monkeypatch.setenv(qa_guard.POLICY_ENV, value)
    assert qa_guard.policy() == "legacy"
    assert _sha(patient_profiler.system_prompt()) == _sha(patient_profiler.SYSTEM) == BASE_PROFILER_SYSTEM_SHA256


@pytest.mark.parametrize("value", ["contract", "strict", "CONTRACT"])
def test_contract_profiler_prompt_does_not_prefer_latest_answer(monkeypatch, value):
    monkeypatch.setenv(qa_guard.POLICY_ENV, value)  # legacy가 아닌 값은 모두 contract (fail-closed)
    s = patient_profiler.system_prompt()
    assert patient_profiler._QA_CONFLICT_CONTRACT in s and patient_profiler._QA_CONFLICT_LEGACY not in s
    assert s.count("\n") == patient_profiler.SYSTEM.count("\n")  # 그 한 줄만 바뀜


def test_profiler_uses_policy_prompt(monkeypatch):
    seen = []

    def fake(model, *, system, user, effort):
        seen.append(system)
        return model.model_construct(patient_id="", age=None)

    monkeypatch.setattr(patient_profiler.llm, "structured", fake)
    monkeypatch.delenv(qa_guard.POLICY_ENV, raising=False)
    patient_profiler.profile_patient("P1", "45세 여성.", "2026-10-25")
    monkeypatch.setenv(qa_guard.POLICY_ENV, "contract")
    patient_profiler.profile_patient("P1", "45세 여성.", "2026-10-25")
    assert seen == [patient_profiler.SYSTEM, patient_profiler.system_prompt()] and seen[0] != seen[1]


def test_session_id_is_not_serialized():
    p = QAPair(round=1, question_id="Q1", question="q", answer="a", session_id="s1")
    assert "session_id" not in p.model_dump() and p.session_id == "s1"
