# P0 사례 채점 - offline_stub(value_grounding.ground*/evidence_directness.check) (60d7921)

건수이며 정확도가 아닙니다. 임상 검수 미완료.

| 세트 | 유형 | 사례 | 안전 처리 | 오채택 | 과잉 보류 | 상충 탐지 | 결과 없음 |
|---|---|---|---|---|---|---|---|
| holdout6_failure_cases | holdout6_conflict | 15 | 14 | 1 | 0 | 12/15 | 0 |
| holdout6_failure_cases | holdout6_hearsay | 10 | 10 | 0 | 0 | - | 0 |
| narrative_conflict_cases | conflict_doc_vs_self | 4 | 0 | 4 | 0 | 0/4 | 0 |
| narrative_conflict_cases | conflict_negation_reversed | 8 | 4 | 4 | 0 | 3/8 | 0 |
| narrative_conflict_cases | control_document | 8 | 8 | 0 | 0 | - | 0 |
| value_cases | conflict_doc_vs_self | 4 | 4 | 0 | 0 | - | 0 |
| value_cases | conflict_same_item | 8 | 1 | 7 | 0 | - | 0 |
| value_cases | conflict_timepoint_old | 8 | 4 | 4 | 0 | - | 0 |
| value_cases | control_dated_lab | 12 | 11 | 0 | 1 | - | 0 |
| value_cases | control_document | 4 | 3 | 0 | 1 | - | 0 |
| value_cases | control_self_certain | 10 | 9 | 0 | 1 | - | 0 |
| value_cases | control_timepoint_latest | 4 | 3 | 0 | 1 | - | 0 |
| value_cases | hearsay_clinician | 8 | 8 | 0 | 0 | - | 0 |
| value_cases | hearsay_family | 8 | 8 | 0 | 0 | - | 0 |
| value_cases | hearsay_memory | 8 | 8 | 0 | 0 | - | 0 |
| value_cases | hearsay_other_person | 8 | 6 | 2 | 0 | - | 0 |
| **합계** | | 127 | 101 | 22 | 4 | 15/27 | 0 |
