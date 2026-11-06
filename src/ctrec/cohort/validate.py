"""생성된 코호트 검증과 구조 통계.

validate()는 문제 목록을 돌려줍니다 (빈 목록 = 통과). 검사 항목:
- 정답 재계산: oracle의 숨긴 값 + public의 공개 값으로 기준 정답을 다시 계산해 저장값과 같은지
- 쌍 라벨: 모든 OR 경로에 불통과 기준이 있으면 INELIGIBLE, 그 밖에 미연결·임상의 판단 기준이 있으면 UNLABELED,
  없으면 ELIGIBLE인지 (생성기 함수를 쓰지 않고 슬롯 사전·파싱된 기준의 cohort에서 다시 계산)
- 임상의 판단 기준: 슬롯 값이 known·hidden 어디에도 없고, truth·initial_holds가 unknown인지
- 정답 비노출: 숨긴 슬롯이 public.known에 없는지
- initial_holds가 공개 값만으로 다시 계산한 값(숨긴 슬롯·지난 검사는 미확인, 3값 논리)과 같은지
- 영 시나리오 조건: 환자당 시험 1개, 균일 비용, 소스 묶음 없음, 정원 제한 없음
"""

from __future__ import annotations

from collections import Counter

from ..schemas import ParsedTrial
from .spec import NULL_KNOBS, Cohort, SlotDictionary, is_expert_slot
from .truth import IndeterminateTruth, holds3, passes, rule_holds, rule_index


def validate(c: Cohort, sd: SlotDictionary, parsed: dict[str, ParsedTrial]) -> list[str]:
    issues: list[str] = []
    rules = rule_index(parsed)
    bindings = {b.key: b for b in sd.bindings}
    pub = c.public
    unbound = {t: [b.rule_id for b in sd.unbound(t)] for t in pub.trials}
    if pub.unbound_rules != unbound:
        issues.append("public.unbound_rules가 슬롯 사전의 미연결 기준과 다름")
    expert = {t: [b.rule_id for b in sd.usable(t) if b.needs_expert()] for t in pub.trials}
    if pub.expert_rules != expert:
        issues.append("public.expert_rules가 임상의 판단 기준 목록과 다름")
    for p in pub.patients:
        hidden = c.oracle.hidden.get(p.patient_id, {})
        if set(hidden) != set(p.unknown_slots):
            issues.append(f"{p.patient_id}: unknown_slots와 숨긴 값 목록이 다름")
        leaked = set(hidden) & set(p.known)
        if leaked:
            issues.append(f"{p.patient_id}: 숨긴 슬롯이 공개됨 {sorted(leaked)}")
        judged = sorted(s for s in (*p.known, *hidden) if is_expert_slot(s))
        if judged:
            issues.append(f"{p.patient_id}: 임상의 판단 슬롯에 값이 있음 {judged}")
        values = {**p.known, **hidden}
        for t in p.candidate_trials:
            pt = parsed.get(t)
            # OR 경로를 따로 다시 셈: 기준이 붙은 코호트, 없으면 경로 하나("*")
            cohort_of = {r.rule_id: r.cohort for r in pt.rules} if pt else {}
            paths = sorted({c for c in cohort_of.values() if c}) or ["*"]
            alive = set(paths)  # 아직 불통과 기준이 없는 경로
            for ref in pub.rules[t]:
                key = f"{p.patient_id}|{t}|{ref.rule_id}"
                b = bindings.get(f"{t}:{ref.rule_id}")
                if b is None:
                    issues.append(f"{key}: 슬롯 사전에 연결이 없음")
                    continue
                if b.needs_expert():
                    if c.oracle.truth.get(key) != "unknown" or pub.initial_holds.get(key) != "unknown":
                        issues.append(f"{key}: 임상의 판단 기준에 정답·초기 상태가 있음")
                    continue
                try:
                    h = rule_holds(b, rules.get(b.key), values, sd.slots, c.reference_date)
                except IndeterminateTruth as e:
                    issues.append(f"{key}: {e}")
                    continue
                if c.oracle.truth.get(key) != h:
                    issues.append(f"{key}: 정답 불일치 (저장 {c.oracle.truth.get(key)}, 재계산 {h})")
                expect_init = holds3(b, rules.get(b.key), p.known, sd.slots, c.reference_date, check_fresh=True)
                if pub.initial_holds.get(key) != expect_init:
                    issues.append(f"{key}: initial_holds {pub.initial_holds.get(key)} != {expect_init}")
                if passes(ref.kind, h) is not True:
                    rc = cohort_of.get(ref.rule_id)
                    alive -= set(paths) if rc is None else {rc}
            pk = f"{p.patient_id}|{t}"
            reasons = (["expert_review"] if expert[t] else []) + (["unbound_rules"] if unbound[t] else [])
            result = "fail" if not alive else "or_partial" if alive != set(paths) else "pass"
            # 모든 경로 불통과만 부적격 확정. 그 밖에 미연결·판단 기준이 있으면 라벨 없음
            want = "INELIGIBLE" if result == "fail" else "UNLABELED" if reasons else "ELIGIBLE"
            if c.oracle.pair_label.get(pk) != want:
                issues.append(f"{pk}: 쌍 라벨 불일치 (저장 {c.oracle.pair_label.get(pk)}, 재계산 {want})")
            if c.oracle.label_reasons.get(pk, []) != (reasons if want == "UNLABELED" else []):
                issues.append(f"{pk}: 라벨 없음 사유 불일치")
            if c.oracle.bound_result.get(pk) != result:
                issues.append(f"{pk}: bound_result 불일치 (저장 {c.oracle.bound_result.get(pk)}, 재계산 {result})")
    if c.null:
        k = c.knobs.model_dump()
        for name, v in NULL_KNOBS.items():
            if k[name] != v:
                issues.append(f"영 시나리오인데 {name}={k[name]!r}")
        if len({s.cost_min for s in pub.sources.values()}) > 1:
            issues.append("영 시나리오인데 소스 비용이 균일하지 않음")
        if any(len(s.reveals) != 1 for s in pub.sources.values()):
            issues.append("영 시나리오인데 소스가 여러 슬롯을 묶음")
        if any(len(p.candidate_trials) != 1 for p in pub.patients):
            issues.append("영 시나리오인데 환자당 후보 시험이 1개가 아님")
    return issues


def stats(c: Cohort) -> dict:
    """후보 A 실험에 필요한 구조가 실제로 생겼는지 보는 통계."""
    pub = c.public
    pairs = [(p, t) for p in pub.patients for t in p.candidate_trials]
    unknown_per_pair = Counter()
    open_pairs = 0
    for p, t in pairs:
        n = sum(pub.initial_holds[f"{p.patient_id}|{t}|{r.rule_id}"] == "unknown" for r in pub.rules[t])
        unknown_per_pair[n] += 1
        open_pairs += n > 0
    # 숨긴 슬롯 하나가 그 환자의 후보 시험 2개 이상에 걸리는 비율 (확인 한 번으로 여러 쌍에 영향)
    shared = total = 0
    for p in pub.patients:
        for s in p.unknown_slots:
            total += 1
            shared += sum(any(s in r.slots() for r in pub.rules[t]) for t in p.candidate_trials) >= 2
    labels = Counter(c.oracle.pair_label.values())
    return {
        "cohort_id": c.cohort_id, "split": c.split, "null": c.null, "patients": len(pub.patients), "pairs": len(pairs),
        "open_pairs": open_pairs, "unknown_rules_per_pair": dict(sorted(unknown_per_pair.items())),
        "eligible_rate": round(labels["ELIGIBLE"] / len(pairs), 3) if pairs else None,
        "unlabeled_rate": round(labels["UNLABELED"] / len(pairs), 3) if pairs else None,
        "shared_hidden_slot_rate": round(shared / total, 3) if total else None,
        "sources": len(pub.sources), "bound_rules": {t: len(r) for t, r in pub.rules.items()},
        "unbound_rules": {t: len(r) for t, r in pub.unbound_rules.items()},
    }
