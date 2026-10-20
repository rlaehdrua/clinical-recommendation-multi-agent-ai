"""실제 LLM 파일럿 사례 구성과 평가 기준 (Phase 7). 데이터와 불러오기만 있고 LLM을 부르지 않습니다.

사례는 모두 합성입니다. 세 묶음으로 나눕니다.
- experiment_a_batch: Phase 5에서 승인 검토용으로 제안한 실험 A 첫 배치 3개 (그대로 재사용, 상한도 그대로)
- pilot_pool: Phase 5 승인 후보(llm_pilot.PILOT_CASE_POOL) 중 첫 배치 밖의 사례
- phase7_candidate: Phase 7에서 유형 공백을 메우려고 고른 후속 배치 후보. PILOT_CASE_POOL 밖이라 승인 게이트가 거절함
  (실행하려면 사용자 승인 뒤 풀에 넣는 별도 코드 변경 필요 - 이번 단계에서는 하지 않음)

유형 (사용자 지정 A~H):
A 명확한 조건 충족 / B 명확한 조건 불일치 / C 필수 정보 부족 / D 추가 질문 후 판정이 달라짐 / E 환자 정보 충돌 /
F 유전자 바이오마커 / G 검사 수치와 단위 / H 정보 부족으로 최종 판정 보류
"""

from __future__ import annotations

import json
from pathlib import Path

from . import config, llm_pilot as lp, question_eval as qe

ROOT = config.ROOT
PHASE7_CASES = ROOT / "eval" / "pilot" / "phase7_cases.json"
PHASE7_LABELS = ROOT / "eval" / "pilot" / "phase7_labels.csv"
_SETS = {
    "dev_101": (config.DATA_DIR / "patients" / "masked" / "masked_cases.json", qe.DEFAULT_LABELS),
    "validation": (config.DATA_DIR / "patients" / "masked_validation" / "validation_cases.json", qe.DEFAULT_LABELS),
    "supplement": (config.DATA_DIR / "patients" / "masked_validation" / "supplement_cases.json", qe.DEFAULT_LABELS),
    "phase7": (PHASE7_CASES, PHASE7_LABELS),
}

TYPES = {
    "A": "명확한 조건 충족", "B": "명확한 조건 불일치", "C": "필수 정보 부족", "D": "추가 질문 후 판정 변화",
    "E": "환자 정보 충돌", "F": "유전자 바이오마커", "G": "검사 수치와 단위", "H": "정보 부족으로 판정 보류",
}

EXPERIMENT_A_BATCH = ("M-SYN-DEMO-A-DEMO-LUNG-001-rescue-1", "S-NCT07369063-I9-biomarker",
                      "V-DEMO-LUNG-001-NCT06908096-lab_platelets-shared")

# (사례 ID, 사례 집합, 유형들, 확인하려는 모델 약점)
CASES = [
    ("M-SYN-DEMO-A-DEMO-LUNG-001-rescue-1", "dev_101", ("D", "C"),
     "가장 긴 서술형 원문 추출, 숨긴 충족 근거를 질문으로 되찾은 뒤 판정이 바뀌는지, 약물 이력 속 EGFR 언급의 출처"),
    ("S-NCT07369063-I9-biomarker", "supplement", ("F", "C"),
     "바이오마커 사실과 유전자 표적 기준 연결, 질문 대상 매핑"),
    ("V-DEMO-LUNG-001-NCT06908096-lab_platelets-shared", "validation", ("G", "C"),
     "두 시험이 같은 혈소판 기준 공유: 병렬 Matcher, 같은 정보를 한 번만 묻는지"),
    ("S-NCT05127980-E1-cohort", "supplement", ("C",), "코호트 전용 제외 기준: 열린 경로만 묻는지"),
    ("S-NCT06125405-E5-confirmed-NCT04063540", "supplement", ("B",),
     "확정 불충족 시험에 질문하지 않는지, 검증 상태 부당 승격 없음"),
    ("F-SYN-S005-02", "dev_101", ("G", "B"), "신장 eGFR 수치를 유전자 EGFR로 연결하지 않는지 (정답 두 시험 모두 불충족)"),
    ("F-SYN-DEMO-A", "dev_101", ("A",), "모든 정보가 있는 적합 사례를 근거 없이 보류·불충족으로 바꾸지 않는지 (E·G 대조 사례의 원본)"),
    ("F-SYN-DEMO-B", "dev_101", ("B",), "치료하지 않은 뇌전이(제외 기준)를 적합으로 표시하지 않는지"),
    ("P7-SYN-DEMO-A-conflict-ecog", "phase7", ("E",), "ECOG 1 vs 2 기록 충돌을 한쪽으로 임의 해결해 적합으로 확정하지 않는지"),
    ("P7-SYN-DEMO-A-conflict-plt", "phase7", ("E", "G"), "같은 날 혈소판 185 vs 85: 한 값을 임의로 골라 확정하지 않는지"),
    ("P7-SYN-DEMO-A-units-hb", "phase7", ("G",), "88 g/L(=8.8 g/dL)를 9.0 g/dL 기준과 숫자만 비교해 충족으로 보지 않는지"),
    ("V-NCT07516951-I7-prov-NCT07087314", "validation", ("H", "C"),
     "정보가 거의 없는 원문: 나이로 잠정 불충족인 시험을 확정 불충족으로 보지 않고, 절차 기준은 연구기관 확인으로 남기는지"),
]


def group_of(case_id: str) -> str:
    if case_id in EXPERIMENT_A_BATCH:
        return "experiment_a_batch"
    if case_id in lp.PILOT_CASE_POOL:
        return "pilot_pool"
    return "phase7_candidate"


def coverage() -> dict:
    """유형별 사례와 묶음. 비어 있는 유형이 있으면 missing에 나옴."""
    by_type: dict = {t: [] for t in TYPES}
    for cid, cset, types, why in CASES:
        for t in types:
            by_type[t].append({"case_id": cid, "set": cset, "group": group_of(cid), "checks": why})
    return {"types": TYPES, "by_type": by_type, "missing": [t for t, v in by_type.items() if not v],
            "experiment_a_batch_types": sorted({t for cid, _, ts, _ in CASES if cid in EXPERIMENT_A_BATCH for t in ts}),
            "not_in_experiment_a_batch": sorted(set(TYPES) - {t for cid, _, ts, _ in CASES
                                                              if cid in EXPERIMENT_A_BATCH for t in ts})}


def load_pairs(parsed: dict, case_ids: list[str] | None = None):
    """사례 ID 순서대로 (PatientCase, CaseGroundTruth). 합성 사례만 (synthetic=true가 아니면 거절)."""
    want = list(case_ids or [c[0] for c in CASES])
    where = {cid: cset for cid, cset, _, _ in CASES}
    out = []
    for cid in want:
        if cid not in where:
            raise ValueError(f"Phase 7 사례 목록에 없음: {cid}")
        path, labels = _SETS[where[cid]]
        raw = next((c for c in json.loads(Path(path).read_text(encoding="utf-8")) if c["patient_id"] == cid), None)
        if raw is None or raw.get("synthetic") is not True:
            raise ValueError(f"합성 사례가 아니거나 없음: {cid}")
        out += qe.load_cases(parsed, path, labels, only=[cid])
    return out


def phase7_expectations() -> dict:
    """새 합성 사례의 기대 판정·확인 규칙 (생성 스크립트가 사례 파일에 기록한 값)."""
    data = json.loads(PHASE7_CASES.read_text(encoding="utf-8"))
    return {c["patient_id"]: c["phase7"] for c in data}


# ---------------------------------------------------------------------------
# 평가 기준. quantitative: 결과 JSON(llm_pilot_exec.execute)에서 자동으로 세는 지표와 통과 조건.
# human_review: 사람이 원문·응답을 직접 보고 판단할 항목 (결과 파일에는 원문이 없으므로 승인된 검토자만 별도로 확인)
# ---------------------------------------------------------------------------

CRITERIA = {
    "extraction": {
        "quantitative": [
            ("facts.unlocated_not_unknown", "== 0", "인용 위치를 원문에서 찾지 못한 사실을 출처 확인됨으로 표시하지 않음 (입력에 없는 사실 생성의 대리 지표)"),
            ("facts.answer_facts_not_self_report", "== 0", "답변에서 온 사실은 모두 본인·보호자 보고(self_report)"),
            ("facts.promoted_to_document_verified", "== 0", "문서 검증으로 부당 승격 없음 (검사 결과와 환자 진술 구분)"),
            ("extraction.agreed / extraction.checked", "== 1", "원문에서 정규식으로 확실히 읽히는 나이·성별·ECOG·주요 검사 이름 일치"),
            ("egfr.kidney_egfr_linked_to_EGFR", "== 0", "신장 eGFR을 유전자 EGFR로 연결하지 않음"),
        ],
        "human_review": [
            "원문에 없는 사실(특히 수치·날짜·약물)이 프로필에 생기지 않았는가",
            "수치와 단위를 원문 그대로 옮겼는가 (단위 생략·임의 환산 없음)",
            "검사 결과와 환자 진술을 구분했는가 (출처 표시)",
            "충돌하는 기록(E 유형)을 한쪽으로 임의 해결하지 않았는가 (둘 다 남기거나 불확실로 표시)",
        ],
    },
    "questions": {
        "quantitative": [
            ("B: duplicate_criteria, duplicate_info_keys", "== 0", "같은 기준·같은 정보를 두 번 묻지 않음"),
            ("B: unnecessary_targets", "== 0", "이미 충분하거나 확정 불충족인 시험에 묻지 않음"),
            ("B: unmapped_questions", "보고", "질문 대상이 실제 기준으로 해석되지 않는 질문 수"),
            ("B: hit_within_budget, first_hit_rank", "보고", "예산(3) 안에 숨긴 필수 기준을 물었는가, 몇 번째에"),
            ("A: questions", "<= question_budget", "라운드 질문 수가 예산 이내"),
        ],
        "human_review": [
            "환자·보호자가 이해할 수 있는 문장인가 (전문용어·내부 코드 없음)",
            "선정·제외를 가르는 필수 조건을 먼저 물었는가",
            "이미 원문에 있는 정보를 다시 묻지 않았는가",
        ],
    },
    "matcher_recommendation": {
        "quantitative": [
            ("matcher.not_evaluated, matcher.missing_after_retry", "== 0", "판정 누락 없음"),
            ("matcher.label_agreement / matcher.labelled_trials", "보고 (E·G·A·B 사례는 == 1 목표)",
             "정답 판정과 일치: A 적합 유지, B 불충족, E 확정 안 함(UNCERTAIN), G 단위 환산 후 불충족"),
            ("errors (TruncatedError, RefusalError)", "== 0", "출력 잘림·거절 없음"),
        ],
        "human_review": [
            "명확한 불일치를 적합으로 표시하지 않았는가",
            "정보 부족을 부적격으로 오판하지 않았는가 (보류·추가 정보 필요로 남김)",
            "근거 인용이 원문과 일치하는가 (조건별 근거 보기)",
            "추천 이유를 사용자가 이해할 수 있는가, 참여 확정처럼 쓰지 않았는가",
        ],
    },
    "web_experience": {
        "quantitative": [
            ("Edge E2E (Fake)", "통과", "질문 표시, 답변 반영 재분석, 진행 상태, 오류 종료, 비용 한도 차단 (tests/test_web_browser.py)"),
        ],
        "human_review": [
            "실제 모델 응답 길이·지연에서도 진행 표시와 대기 안내가 자연스러운가 (실제 웹 유료 호출은 별도 승인)",
        ],
    },
    "cost_safety": {
        "quantitative": [
            ("totals.cost_conservative_usd", "<= limits.max_cost_usd", "비용 상한 이내"),
            ("stop_reason", "None", "자동 중단 없음 (상한·응답 이상·보호 장치)"),
            ("case usage.cost_actual_usd", "<= 2 x 사례 base 추정", "사례당 실제 비용이 추정의 2배 이내"),
            ("by_stage.*.thinking_tokens", "보고", "사고 토큰이 max_tokens(16,000)에 근접하지 않음"),
            ("frozen_store_intact", "== true", "고정 기준표 무결"),
        ],
        "human_review": ["Console 사용량과 결과의 실제 비용 대조"],
    },
}
