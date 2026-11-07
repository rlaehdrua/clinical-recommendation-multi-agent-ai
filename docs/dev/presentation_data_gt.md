# 발표 초안 — 데이터·GT 파트 (슬라이드 5장)

작성: 서연 · 2026-11-08 · 수치 기준: `integration/dcvc` `4f81460` + PR #43 평가셋
검증 수준: 데이터 검증 완료 / 임상 검수 미완료

---

## 슬라이드 1. 데이터 출처

- 프로토콜: ClinicalTrials.gov 실제 NSCLC 시험 8건
  - 출처: `data/nsclc/trials/*.json`
- 조회 시점: 2026-11-02
  - 출처: `data/nsclc/trials/*.json` `source.retrieved_at`
- 파싱 완료 6건, 파싱 미완료 2건 (API 크레딧)
  - 출처: `data/nsclc/review/protocol_status.csv`
- 기준 160개 (6건 합계)
  - 출처: `data/nsclc/review/criteria_verification.csv`
- 환자: 합성 80명, 실제 환자 정보 없음
  - 출처: `data/nsclc/eval/patients.json`
- 라이선스·이용 조건: 확인 필요

## 슬라이드 2. 축소 범위와 이유

- 범위 안 기준 72개 / 160개
  - 나이·성별·ECOG 19 (지역 조건부 1 포함)
  - 범주형 34 (연결 23, 미연결 11)
  - 임상 판단 기준 19
  - 출처: `data/nsclc/review/criteria_verification.csv`
- excluded 9개 (동의서·절차, 팀 결정 2)
- 범위 밖 79개 (LLM 판정 대상 등, 평가하지 않음)
- 원문 대조 결과: 일치 60 / 모호 11 / 불일치 1
  - 불일치 1: "≥22 years of age in the USA" 지역 조건 누락
  - 출처: `data/nsclc/review/overrides.json`
- 검사 수치 4개 기준 0건: 6개 시험 원문에 기준값 없음
  - 출처: `data/nsclc/eval/inputs/lab_reference.json`
- 이유: 슬롯으로 원문을 다 표현할 수 있는 기준만 정답 계산. 표현 못 하면 미연결

## 슬라이드 3. 독립 규칙 GT 설계

- 생성 / 검토 / 정답 분리
  - 생성: 생성기 → `data/nsclc/eval/generated/cohort.json`
  - 검토: 사람 검수표 → `data/nsclc/review/criteria_verification.csv`
  - 정답: GT 모듈 → `src/ctrec/cohort/gt.py`
- 엔진 코드 import 0
  - 출처: `tests/test_nsclc_eval_gt.py` (독립성 테스트 2건)
- 기준 상태 5종: 통과 / 불통과 / 해당 없음 / 전문가 판단 필요 / 미평가
- 쌍 라벨 4종: 확정 적격 / 전문가 판단 대기 적격 / 부적격 / 미평가
- 판정 규칙
  - 모든 OR 경로 불통과일 때만 부적격
  - 해당 없음·인구학 기준만 통과는 적격 근거 아님
  - 지역 조건 불명확 시 미평가
- 엔진 순환 GT와 일치 237 / 240쌍
  - 출처: `data/nsclc/eval/checks/circular_vs_independent_4f81460.json`

## 슬라이드 4. 라벨 분포와 실험 수치

- 평가셋 240쌍 (80명 × 3시험, 시드 20261107, 설정 해시 `a549da3b1171fd52`)
  - 출처: `data/nsclc/eval/manifest.json`

| 라벨 | 쌍 | 출처 |
|---|---|---|
| 확정 적격 | 0 (6개 시험 모두 판단 기준 보유) | `data/nsclc/eval/label_summary.json` |
| 전문가 판단 대기 적격 | 23 | 같음 |
| 부적격 | 130 | 같음 |
| 미평가 | 87 | 같음 |

- runner 실행 (seed 1, 정책 간 상대 비교 전용)

| 정책 · 예산 | 판단 기준 외 확인 완료 ∧ GT 판단 대기 | 확정 부적격 ∧ GT 부적격 | GT 부적격 아님 + 확정 부적격 | 확정 적격 | 출처 |
|---|---|---|---|---|---|
| 기준선(no_action) · 400분 | 0 / 23 | 0 / 130 | 0 | 0 | `data/nsclc/eval/checks/runner_400m_h10_4f81460.json` |
| full_dcvc · 400분 | 0 / 23 | 48 / 130 | 0 | 0 | 같음 |
| full_dcvc · 6000분 | 20 / 23 | 124 / 130 | 0 | 0 | `data/nsclc/eval/checks/runner_6000m_h80_4f81460.json` |

- 결함 발견 사례: 축소 저장소에서 코호트 전용 기준 제거 시 거짓 부적격 6쌍 → 수정 후 0쌍
  - 출처: `docs/dev/nsclc_eval_dataset.md` §7 D7

## 슬라이드 5. 주장 가능·불가, 한계

주장 가능
- 실제 프로토콜 기준 72개의 원문 대조 결과
- 엔진과 분리된 규칙 GT로 정책 결과 대조 가능
- 이 평가셋·이 조건에서 판단 기준 외 확인 완료 20 / 23쌍, 거짓 확정 0
- P0 사례 기대값 근거 공개 (상충·전해 들은 값 48건, branch `data/p0-eval-cases` `data/eval/p0_fact_cases/`)

주장 불가
- 임상 정확도, 임상 성능
- 전체 프로토콜 적격성 (범위 밖 79개 미평가)
- 확정 적격 관련 지표 (GT 0건)
- 검사 수치 기준 처리 (평가셋에 기준 0건)
- 실제 업무 시간 절감 (비용·응답 확률 가정값)

한계
- 임상 검수 미완료
  - 출처: 모든 검수표 `clinical_review = 미완료`
- 합성 환자 사전 분포는 설계값
  - 출처: `data/nsclc/eval/inputs/generator_config.json` `design_targets`
- 판단 대기 적격 비율 9.6%, 설계 목표 10% 미달
- NCT06216301 지역 조건: 현재 엔진에서 20세 거짓 부적격 2케이스
  - 출처: branch `data/p0-eval-cases` `tests/data/p0_3_region_cases.json`
- 기존 P0 실패 집합 미해결: 상충 수용 6 / 15, 전해 들은 값 오채택 3 / 10
  - 출처: branch `data/p0-eval-cases` `data/eval/p0_fact_cases/results_4f81460.json`
- 새 사례: 같은 항목 다른 값 5 / 5 채택
  - 출처: 같음
