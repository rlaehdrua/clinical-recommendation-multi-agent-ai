"""시나리오별 합성 코호트 생성 (LLM 없음, 시드가 같으면 결과도 같음).

순서: 후보 시험 배정 -> 슬롯 값 샘플링(사전 분포) -> 기준별 정답 계산(truth.py) -> 가시성(범주별 결측률) ->
유효 소스(비용 이질성·묶음 knob 적용) -> public / oracle 분리.

- 정답은 슬롯 사전에서 auto·reviewed 연결만으로 계산합니다. 검수 전 기준(unbound)은 정답에서 빠지며
  충족으로 간주하지 않습니다: 미연결 기준이 남은 쌍은 적격 라벨을 만들지 않고 UNLABELED(사유 unbound_rules)이며,
  public.unbound_rules에 적습니다. (cohort-gen-1은 assume_pass로 충족 처리 -> 현황판 오확정의 원인이었음)
- 임상의 판단 기준(spec.is_expert_slot 슬롯을 쓰는 연결)은 값을 뽑지 않고 정답도 만들지 않습니다(truth·initial_holds
  = unknown). 공개 결측률과 무관하게 값이 공개되지 않으며, 그 쌍은 UNLABELED(사유 expert_review)입니다.
- 예외: 연결 기준의 불통과가 모든 OR 경로(코호트)에서 성립하면 남은 기준과 관계없이 INELIGIBLE (label_pair).
  일부 경로만 불통과면 확정하지 않습니다. oracle.bound_result에 pass / or_partial / fail을 남깁니다.
- 규칙 엔진이 판정하지 못하는 값이 나오면 그 환자를 다시 샘플링합니다(max_resample). 그래도 안 되면 오류입니다.
- 기간 조건이 있는 검사 기준은 lab_age_days(마지막 검사가 며칠 전인지, 항상 공개)로 유효 여부를 봅니다.
  유효 기간이 지났으면 값이 공개돼 있어도 initial_holds는 미확인입니다 (정답은 값이 그대로라고 가정).
- 영 시나리오(null)는 NULL_KNOBS를 강제합니다: 환자당 시험 1개, 균일 비용, 소스 묶음 없음, 정원 제한 없음.
  구조를 줄인 대조 조건일 뿐 정책 간 동등 성능을 보장하지 않습니다. 시뮬레이터 정확성은 작은 사례의 완전탐색으로 확인합니다.
"""

from __future__ import annotations

import hashlib
import json
from fnmatch import fnmatch
from pathlib import Path
from random import Random

from .. import config as app_config
from .. import trial_store
from ..schemas import Holds, ParsedTrial
from .spec import (CORE_SOURCE, LAB_AGE, REASON_EXPERT, REASON_UNBOUND, Cohort, EffectiveSource, GeneratorConfig, Knobs,
                   OraclePart, Prior, PublicPart, PublicPatient, RuleRef, Scenario, SlotDictionary, SourceCatalog)
from .truth import IndeterminateTruth, holds3, passes, rule_holds, rule_index

GENERATOR_VERSION = "cohort-gen-2"  # 2: 미연결 기준 충족 간주 폐지, 임상의 판단 기준 정답 미생성


class GenerationError(RuntimeError):
    pass


def _read(path: str | Path) -> dict:
    p = Path(path)
    if not p.is_absolute():
        p = app_config.ROOT / p
    return json.loads(p.read_text(encoding="utf-8"))


def load_parsed(trial_ids: list[str]) -> dict[str, ParsedTrial]:
    out = {}
    for tid in trial_ids:
        rec = trial_store.read_record(tid)
        if rec is None:
            raise GenerationError(f"{tid}: data/parsed에 컴파일된 기준이 없음 (scripts/compile_trials.py)")
        out[tid] = ParsedTrial(**rec["parsed"])
    return out


def load_inputs(config_path: str | Path) -> tuple[GeneratorConfig, SlotDictionary, SourceCatalog, dict[str, ParsedTrial]]:
    cfg = GeneratorConfig(**_read(config_path))
    sd = SlotDictionary(**_read(cfg.slot_dictionary))
    cat = SourceCatalog(**_read(cfg.source_catalog))
    trials = sorted({t for c in cfg.clusters.values() for t in c.trials})
    return cfg, sd, cat, load_parsed(trials)


def config_hash(cfg: GeneratorConfig, sd: SlotDictionary, cat: SourceCatalog) -> str:
    blob = json.dumps({"generator": GENERATOR_VERSION, "config": cfg.model_dump(mode="json"),
                       "slots": sd.model_dump(mode="json"), "catalog": cat.model_dump(mode="json")},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def derive_seed(base_seed: int, scenario_id: str, replicate: int) -> int:
    # 내장 hash()는 실행마다 달라지므로 쓰지 않음
    return int(hashlib.sha256(f"{base_seed}|{scenario_id}|{replicate}".encode()).hexdigest()[:12], 16)


def sample(prior: Prior, rng: Random) -> float | str:
    if prior.dist == "categorical":
        keys = list(prior.probs)
        return rng.choices(keys, weights=[prior.probs[k] for k in keys])[0]
    v = rng.gauss(prior.a, prior.b) if prior.dist == "normal" else rng.uniform(prior.a, prior.b)
    if prior.lo is not None:
        v = max(prior.lo, v)
    if prior.hi is not None:
        v = min(prior.hi, v)
    return int(round(v)) if prior.decimals == 0 else round(v, prior.decimals)


def effective_sources(cat: SourceCatalog, slots_used: list[str], sd: SlotDictionary, knobs: Knobs) -> dict[str, EffectiveSource]:
    checkable = [s for s in slots_used if sd.slots[s].source != CORE_SOURCE]
    base: dict[str, list[str]] = {}
    for sid, spec in cat.sources.items():
        rev = [s for s in checkable if any(fnmatch(s, pat) for pat in spec.reveals)]
        if rev:
            base[sid] = rev
    for s in checkable:
        if not any(s in rev for rev in base.values()):
            raise GenerationError(f"슬롯 {s}을 드러내는 소스가 카탈로그에 없음")
    if not base:
        return {}
    mean = sum(cat.sources[s].cost_min for s in base) / len(base)
    out = {}
    for sid, rev in base.items():
        spec = cat.sources[sid]
        cost = round(mean + knobs.cost_heterogeneity * (spec.cost_min - mean), 3)
        groups = [(sid, rev)] if knobs.grouping == "source" else [(f"{sid}:{s}", [s]) for s in rev]
        for gid, slots in groups:
            out[gid] = EffectiveSource(source_id=gid, base_source=sid, evidence=spec.evidence, cost_min=cost, reveals=slots)
    return out


def _check_priors(cfg: GeneratorConfig, sd: SlotDictionary, cluster: str, slots_used: list[str]) -> None:
    priors = cfg.clusters[cluster].priors
    for s in slots_used:
        if s not in priors:
            raise GenerationError(f"{cluster}: 슬롯 {s}의 사전 분포가 없음")
        spec, prior = sd.slots[s], priors[s]
        if spec.kind == "numeric" and spec.unit and prior.unit != spec.unit:
            raise GenerationError(f"{cluster}: 슬롯 {s} 단위 불일치 (사전 분포 {prior.unit!r}, 슬롯 {spec.unit!r})")
        if spec.kind == "categorical" and prior.dist != "categorical":
            raise GenerationError(f"{cluster}: 범주형 슬롯 {s}에 수치 사전 분포")
        if prior.dist == "categorical" and spec.values and set(prior.probs) - set(spec.values):
            raise GenerationError(f"{cluster}: 슬롯 {s}의 값 목록에 없는 사전 분포 값 {sorted(set(prior.probs) - set(spec.values))}")


def _needed(b) -> list[str]:
    return b.slots_used() + ([LAB_AGE] if b.time_window_days is not None else [])


def generate_cohort(cfg: GeneratorConfig, sd: SlotDictionary, cat: SourceCatalog, parsed: dict[str, ParsedTrial],
                    scenario: Scenario, replicate: int, chash: str) -> Cohort:
    knobs = scenario.effective_knobs()
    cluster = cfg.clusters[scenario.cluster]
    trials = list(cluster.trials)
    rules = rule_index(parsed)
    bound = {t: sd.usable(t) for t in trials}
    valued = {t: [b for b in bound[t] if not b.needs_expert()] for t in trials}  # 값·정답을 만드는 기준
    expert = {t: [b.rule_id for b in bound[t] if b.needs_expert()] for t in trials}
    unbound = {t: [b.rule_id for b in sd.unbound(t)] for t in trials}
    paths = {t: trial_paths(parsed.get(t)) for t in trials}
    slots_used = sorted({s for bs in bound.values() for b in bs for s in _needed(b)})  # 소스 구성용 (판단 슬롯 포함)
    _check_priors(cfg, sd, scenario.cluster, sorted({s for bs in valued.values() for b in bs for s in _needed(b)}))

    seed = derive_seed(cfg.base_seed, scenario.scenario_id, replicate)
    rng = Random(seed)
    cohort_id = f"{scenario.scenario_id}-r{replicate:02d}"
    patients, hidden_all, truth, labels, initial = [], {}, {}, {}, {}
    reasons_all, bound_result = {}, {}

    for i in range(scenario.n_patients):
        pid = f"P{i + 1:03d}"
        chosen = set(rng.sample(trials, k=min(knobs.trials_per_patient, len(trials))))
        cand = [t for t in trials if t in chosen]  # 시험 순서는 설정 순서로 고정
        # 임상의 판단 슬롯은 값을 뽑지 않음 -> public.known에도 oracle.hidden에도 없음 (공개 여부와 무관하게 판단 대기)
        needed = sorted({s for t in cand for b in valued[t] for s in _needed(b)})
        for attempt in range(cfg.max_resample + 1):
            values = {s: sample(cluster.priors[s], rng) for s in needed}
            try:
                pt = {(t, b.rule_id): rule_holds(b, rules.get(b.key), values, sd.slots, cfg.reference_date)
                      for t in cand for b in valued[t]}
                break
            except IndeterminateTruth as e:
                if attempt == cfg.max_resample:
                    raise GenerationError(f"{cohort_id}/{pid}: {e}") from e

        hidden = {s for s in needed if sd.slots[s].source != CORE_SOURCE
                  and rng.random() < knobs.missing_rate.get(sd.slots[s].category, knobs.missing_rate.get("default", 0.0))}
        known = {s: values[s] for s in needed if s not in hidden}
        patients.append(PublicPatient(patient_id=pid, candidate_trials=cand, known=known, unknown_slots=sorted(hidden)))
        hidden_all[pid] = {s: values[s] for s in sorted(hidden)}

        for t in cand:
            failing: list[str | None] = []  # 불통과 기준의 코호트 (None = 모든 경로 공통)
            for b in bound[t]:
                key = f"{pid}|{t}|{b.rule_id}"
                if b.needs_expert():  # 정답을 만들지 않음 (임상의 판단 대기)
                    truth[key] = initial[key] = "unknown"
                    continue
                h: Holds = pt[(t, b.rule_id)]
                truth[key] = h
                # 정책이 아는 값만으로 본 상태: 숨긴 슬롯·유효 기간이 지난 검사는 미확인, 묶인 조건은 3값 논리
                initial[key] = holds3(b, rules.get(b.key), known, sd.slots, cfg.reference_date, check_fresh=True)
                if passes(b.kind, h) is not True:
                    failing.append(rules[b.key].cohort if b.key in rules else None)
            pk = f"{pid}|{t}"
            reasons = pair_label_reasons(expert[t], unbound[t])
            labels[pk], bound_result[pk] = label_pair(failing, paths[t], reasons)
            if labels[pk] == "UNLABELED":
                reasons_all[pk] = reasons

    public = PublicPart(
        trials=trials,
        rules={t: [RuleRef(rule_id=b.rule_id, kind=b.kind, slot=b.slot, extra_slots=[c.slot for c in b.also],
                           time_window_days=b.time_window_days, required_evidence=b.required_evidence)
                   for b in bound[t]] for t in trials},
        unbound_rules=unbound,
        expert_rules=expert,
        enrollment_remaining={t: knobs.enrollment_remaining for t in trials},
        sources=effective_sources(cat, slots_used, sd, knobs),
        budgets_min=cfg.budgets_min, patients=patients, initial_holds=initial,
    )
    return Cohort(cohort_id=cohort_id, config_hash=chash, scenario_id=scenario.scenario_id, replicate=replicate,
                  split="dev" if replicate < scenario.dev_replicates else "test", seed=seed, cluster=scenario.cluster,
                  reference_date=cfg.reference_date, null=scenario.null, knobs=knobs, public=public,
                  oracle=OraclePart(hidden=hidden_all, truth=truth, pair_label=labels, label_reasons=reasons_all,
                                    bound_result=bound_result))


def pair_label_reasons(expert_rules: list[str], unbound_rules: list[str]) -> list[str]:
    """쌍에 적격 라벨을 만들지 않는 사유 (빈 목록 = 라벨을 만듦)."""
    return ([REASON_EXPERT] if expert_rules else []) + ([REASON_UNBOUND] if unbound_rules else [])


def trial_paths(parsed: ParsedTrial | None) -> list[str | None]:
    """OR 경로(코호트). 기준이 하나라도 붙은 코호트만 경로이며(Matcher._select_cohort와 같은 뜻), 없으면 [None] 하나."""
    if parsed is None:
        return [None]
    used = [c for c in parsed.cohorts if any(r.cohort == c for r in parsed.rules)]
    used += sorted({r.cohort for r in parsed.rules if r.cohort and r.cohort not in used})
    return used or [None]


def label_pair(failing: list[str | None], paths: list[str | None], reasons: list[str]) -> tuple[str, str]:
    """(쌍 라벨, bound_result). failing = 정답이 불통과인 (판단 기준 제외) 연결 기준의 코호트 목록.

    경로 p에 적용되는 기준 = 공통(None) + 코호트 p 전용. 경로가 불통과 = 그 경로의 기준 하나 이상 불통과.
    - 모든 경로가 불통과 -> INELIGIBLE (미연결·판단 기준이 남아도: 남은 기준이 무엇이든 어느 경로로도 적격이 안 됨)
    - 사유(미연결·판단 기준)가 있으면 그 밖은 UNLABELED. 한 경로라도 불통과가 아니면 부적격 확정 금지
    - 사유가 없으면 ELIGIBLE (불통과가 없는 경로가 있고, 그 경로의 기준은 정답이 모두 있음)
    bound_result: pass = 불통과 없음 / or_partial = 일부 경로만 불통과 / fail = 모든 경로 불통과 (확정 라벨 아님)
    """
    dead = {p for p in paths if any(c is None or c == p for c in failing)}
    result = "fail" if dead == set(paths) else "or_partial" if dead else "pass"
    if result == "fail":
        return "INELIGIBLE", result
    return ("UNLABELED" if reasons else "ELIGIBLE"), result


def generate_all(cfg: GeneratorConfig, sd: SlotDictionary, cat: SourceCatalog,
                 parsed: dict[str, ParsedTrial]) -> list[Cohort]:
    chash = config_hash(cfg, sd, cat)
    return [generate_cohort(cfg, sd, cat, parsed, sc, r, chash) for sc in cfg.scenarios for r in range(sc.replicates)]
