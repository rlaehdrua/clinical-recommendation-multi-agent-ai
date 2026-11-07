# P0-3 제안: NCT06216301 국가 조건부 나이 기준 (선우 전달용)

작성: 서연 (데이터) · 2026-11-08 · 기준 `integration/dcvc` `4f81460`

> 데이터 검증 완료 / 임상 검수 미완료. 엔진 코드는 바꾸지 않았습니다. 패치는 제안만 하고 적용하지 않았습니다.

## 1. 원문과 파싱

ClinicalTrials.gov NCT06216301 (2026-11-02 조회), Inclusion Criteria:

```
* ≥22 years of age in the USA
  ≥18 years of age outside of the USA.
```

| 기준 | 파싱본 (`data/nsclc/parsed/NCT06216301.json`) | 원문 뜻 |
|---|---|---|
| I1 | `age >= 22 years`, 지역 조건 없음 | USA 환자만 22세 이상 |
| I2 | `age >= 18 years`, 지역 조건 없음 | USA 밖 환자는 18세 이상 |

- 엔진 `PatientProfile`에는 국가 항목이 없습니다. 국가는 자유 사실(other_fact)로만 들어가고, 엔진은 읽지 않습니다.
- 검수표 `data/nsclc/review/overrides.json`(PR #43)의 지역 조건 수정 제안은 **엔진 경로에서 읽히지 않습니다.** 코드에서 그 파일을 읽는 곳은 0곳입니다(`grep`).

## 2. 통합본 실행 결과 (4f81460, OfflineEvaluator, 사실 전부 문서 검증)

| 케이스 | 원문 기대 | 현재 엔진 | 제안 적용 |
|---|---|---|---|
| 20세 · 국가 미상 | 확인 필요 (USA면 불충족, 밖이면 충족) | **CONFIRMED_INELIGIBLE** ✗ | INFO_MISSING |
| 20세 · USA 밖 | 나이로 부적격 아님 (I1 해당 없음, I2 충족) | **CONFIRMED_INELIGIBLE** ✗ (거짓 부적격) | INFO_MISSING |
| 20세 · USA | 부적격 (I1 불충족) | CONFIRMED_INELIGIBLE | INFO_MISSING (국가를 문서로 확인할 수 없어 보류) |
| 17세 · 모든 국가 | 부적격 (어느 지역 하한에도 못 미침) | CONFIRMED_INELIGIBLE | INFO_MISSING (보수적 부작용, §4) |
| 22·25세 · 모든 국가 | 나이로 부적격 아님 | INFO_MISSING (다른 기준 미확인) | 같음 |

- 회귀 케이스: `tests/data/p0_3_region_cases.json` (12케이스, 원문 인용·기대 처리·금지 상태·실측값)
- 테스트: `tests/test_p0_3_region_cases.py`
  - 현재 금지 상태가 나오는 2케이스는 strict xfail입니다(`known_defect: true`).
  - 같은 결함을 다루는 기존 strict xfail `tests/test_p_common_engine.py::test_nsclc_us_only_age_rule_does_not_confirm_ineligible`과 같은 원인입니다.

## 3. 엔진이 읽게 하는 최소 변경 (제안 패치 `docs/proposals/p0_3_region_condition.patch`)

`src/ctrec/tools/rule_engine.py` `evaluate()`의 나이 분기 한 곳만 바꿉니다.

```python
_REGION_QUALIFIER = re.compile(r"\b(?:in|outside(?:\s+of)?)\s+(?:the\s+)?(?:USA|U\.S\.(?:A\.)?|US|United States)\b")
...
    if field == "age":
        a = _evaluate_age(rule, patient)
        if a is not None and a.holds == "no" and _REGION_QUALIFIER.search(rule.text or ""):
            return None  # 지역 조건부 나이 기준 불충족: 국가 확인 전에는 판정하지 않음 (충족은 그대로)
        return a
```

- None을 돌려주면 Matcher는 규칙 엔진 확정을 하지 않고, 판정 불가(확인 필요)로 남깁니다.
- **충족은 그대로** 둡니다(지역과 무관하게 충족). 그래서 22세 이상 환자의 판정은 바뀌지 않습니다.
- 같은 패치에 테스트 두 곳의 정리가 들어 있습니다.
  - `test_p_common_engine.py`: strict xfail 표시 제거
  - `p0_3_region_cases.json`: `known_defect` → false
- 패치 적용 검증
  - `tests/test_p0_3_region_cases.py`와 `tests/test_p_common_engine.py`: 45 passed
  - 전체 테스트: §6

`overrides.json`을 읽는 방식(대안 B)을 쓰지 않은 이유
- 파싱 저장소·시험별 경로에 의존하는 새 입력이 엔진에 생깁니다.
- 웹·시뮬레이터 두 경로에 모두 연결해야 합니다.
- 규칙 문장에 지역 문구가 남아 있으므로 문장 검사가 변경이 가장 작습니다.

## 4. 한계와 결정이 필요한 것

1. **보수적 부작용:** 17세처럼 모든 지역에서 불충족인 환자도 확정하지 않습니다. 원칙(지역 조건이 불명확하면 확정 부적격 금지)에는 맞지만 확인 행동이 늘어납니다.
   - 정밀하게 하려면 같은 시험의 지역 조건부 나이 기준을 묶어 "모든 지역에서 불충족"일 때만 확정하는 처리가 필요합니다. 이것은 Matcher 변경입니다.
2. **USA 환자 20세:** 원문상 부적격이지만, 국가 항목이 없어 엔진이 확정할 근거가 없습니다. 국가를 확인할 정보원·사실 항목을 둘지는 엔진·정보원 카탈로그 결정입니다.
3. **문구 목록:** 미국 표현만 넣었습니다. 다른 국가·지역 한정 문구는 NSCLC 6개 시험에서 0건이었습니다(검수표 전수 조사). 범위를 넓힐지는 결정이 필요합니다.
4. **근본 해결:** 파서가 지역 조건을 필드(예: `applies_if: country`)로 내도록 바꾸는 것입니다. 파서·스키마 변경이라 이 제안 범위 밖입니다.

## 5. 데이터·GT 쪽 처리 (이미 반영, PR #43)

- 독립 GT(`src/ctrec/cohort/gt.py`)는 I1을 `region_conditional`로 둡니다. 22세 이상이면 PASS, 미만이면 NOT_EVALUATED입니다.
- 평가셋 생성기는 I1에 정답을 만들지 않습니다(미연결).

## 6. 전체 테스트 (패치 적용, 4f81460 위)

| 대상 | passed | failed | skipped | xfailed |
|---|---|---|---|---|
| 4f81460 그대로 | 5,652 | 0 | 9 | 5 |
| 4f81460 + 이 패치의 엔진·테스트 변경 | 5,653 | 0 | 9 | 4 |

- 기존 strict xfail 1건(`test_nsclc_us_only_age_rule_does_not_confirm_ineligible`)이 정상 통과로 바뀌었습니다. 그 밖의 변화는 없습니다.
- 이 측정에는 `tests/test_p0_3_region_cases.py`가 없습니다(별도 브랜치). 그 파일은 패치 적용 시 13건 모두 통과합니다(위 §3의 45 passed).
