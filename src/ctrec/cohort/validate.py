"""생성된 코호트 검증과 구조 통계.

validate()는 문제 목록을 돌려줍니다 (빈 목록 = 통과). 검사 항목:
- 정답 재계산: oracle의 숨긴 값 + public의 공개 값으로 기준 정답을 다시 계산해 저장값과 같은지
- 쌍 라벨: 정답 계산에 쓴 기준이 모두 통과할 때만 ELIGIBLE인지
- 정답 비노출: 숨긴 슬롯이 public.known에 없는지
- initial_holds가 공개 값만으로 다시 계산한 값(숨긴 슬롯·지난 검사는 미확인, 3값 논리)과 같은지
- 영 시나리오 조건: 환자당 시험 1개, 균일 비용, 소스 묶음 없음, 정원 제한 없음
"""

from __future__ import annotations

from collections import Counter

from ..schemas import ParsedTrial
from .spec import NULL_KNOBS, Cohort, SlotDictionary
from .truth import IndeterminateTruth, holds3, passes, rule_holds, rule_index


def validate(c: Cohort, sd: SlotDictionary, parsed: dict[str, ParsedTrial]) -> list[str]:
    issues: list[str] = []
    rules = rule_index(parsed)
    bindings = {b.key: b for b in sd.bindings}
    pub = c.public
    for p in pub.patients:
        hidden = c.oracle.hidden.get(p.patient_id, {})
        if set(hidden) != set(p.unknown_slots):
            issues.append(f"{p.patient_id}: unknown_slots와 숨긴 값 목록이 다름")
        leaked = set(hidden) & set(p.known)
        if leaked:
            issues.append(f"{p.patient_id}: 숨긴 슬롯이 공개됨 {sorted(leaked)}")
        values = {**p.known, **hidden}
        for t in p.candidate_trials:
            ok = True
            for ref in pub.rules[t]:
                key = f"{p.patient_id}|{t}|{ref.rule_id}"
                b = bindings.get(f"{t}:{ref.rule_id}")
                if b is None:
                    issues.append(f"{key}: 슬롯 사전에 연결이 없음")
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
                ok = ok and passes(ref.kind, h) is True
            want = "ELIGIBLE" if ok else "INELIGIBLE"
            if c.oracle.pair_label.get(f"{p.patient_id}|{t}") != want:
                issues.append(f"{p.patient_id}|{t}: 쌍 라벨 불일치")
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
        "shared_hidden_slot_rate": round(shared / total, 3) if total else None,
        "sources": len(pub.sources), "bound_rules": {t: len(r) for t, r in pub.rules.items()},
        "unbound_rules": {t: len(r) for t, r in pub.unbound_rules.items()},
    }
