"""현황판(/cohort)과 시뮬레이터의 쌍 상태 차이 추출 (이슈 #23 후속, 합성 데이터, LLM 호출 없음).

비교하는 세 시각
- board        : web.cohort_board.board. 코호트 public만 읽음 (initial_holds, 근거 검사 없음)
- sim_truth    : 정답 사실 전부를 문서 검증된 기록으로 넣은 OfflineEvaluator (runner.rule_labels와 같은 입력).
                 이슈 #23의 170·43쌍이 쓰는 '시뮬레이터' 쪽이며 board보다 정보가 많음
- sim_public   : 공개 사실만 넣은 OfflineEvaluator (runner.run의 첫 상태와 같은 입력). board와 같은 정보량

추출 대상
- board_CI_sim_IM: board CONFIRMED_INELIGIBLE, sim_truth INFO_MISSING
- board_IM_sim_CI: board INFO_MISSING, sim_truth CONFIRMED_INELIGIBLE (반대 방향)

mechanism은 기록된 값에서 기계적으로 정한 코드입니다. 어느 쪽이 맞는지 판정한 분류가 아니며, 오판 여부는 담지 않습니다.
생성기 GT·판정 코드는 바꾸지 않습니다. Matcher 결과를 읽으려고 실행 중에만 matcher.match_trial을 기록용으로 감쌉니다.

    PYTHONPATH=src python scripts/board_sim_diff.py                  # 기본 출력 폴더에 씀
    PYTHONPATH=src python scripts/board_sim_diff.py --check          # 다시 계산해 저장된 파일과 바이트 비교 (다르면 exit 1)
    PYTHONPATH=src python scripts/board_sim_diff.py --out outputs/x  # 다른 폴더에 씀
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import io
import json
import sys
from collections import Counter
from pathlib import Path

from ctrec import config
from ctrec.agents import matcher
from ctrec.cohort import generate as G
from ctrec.engine.cohort_state import CohortState
from ctrec.sim import from_generator as FG
from ctrec.sim.evaluator import OfflineEvaluator, make_fact
from ctrec.web import cohort_board as cb

DEFAULT_OUT = config.ROOT / "docs" / "review" / "board_sim_diff"
SCRIPT_VERSION = 1
BOARD_CI_SIM_IM = "board_CI_sim_IM"
BOARD_IM_SIM_CI = "board_IM_sim_CI"
GROUPS = {("CONFIRMED_INELIGIBLE", "INFO_MISSING"): BOARD_CI_SIM_IM,
          ("INFO_MISSING", "CONFIRMED_INELIGIBLE"): BOARD_IM_SIM_CI}
INPUT_FILES = ("data/cohort/generator_config.json", "data/cohort/slot_dictionary.draft.json",
               "data/cohort/source_catalog.json", "data/cohort/source_map.json")

PAIR_COLUMNS = ["group", "pair_id", "cohort_id", "patient_id", "trial_id", "board_status", "sim_truth_status",
                "sim_public_status", "board_failing_rules", "sim_failing_rules", "driving_rules", "pair_mechanism",
                "board_gaps", "sim_gaps", "n_rules_differ", "n_unbound_rules", "trial_cohorts", "board_path",
                "sim_matched_cohort", "sim_cohort_results", "generator_pair_label"]
CRITERION_COLUMNS = ["group", "pair_id", "cohort_id", "patient_id", "trial_id", "rule_id", "kind", "category",
                     "time_window_days", "required_evidence", "rule_text",
                     "differs", "drives_status", "mechanism",
                     "board_bound", "slots", "slot_visibility", "generator_sources", "engine_sources",
                     "binding_status", "binding_used_by_judge", "binding_truth", "binding_also_slots", "binding_combine",
                     "board_holds", "board_passes", "board_gap", "board_applied",
                     "sim_holds", "sim_passes", "sim_raw_judge_holds", "sim_raw_passes", "sim_method", "sim_state",
                     "sim_gap", "sim_requirement", "sim_evidence_source", "sim_evidence_directness",
                     "sim_validation_flags", "sim_needs_verification", "sim_basis_tier", "sim_evidence_refs",
                     "sim_public_holds", "sim_public_gap", "sim_public_validation_flags",
                     "trial_cohorts", "rule_cohort", "board_path", "sim_matched_cohort", "generator_rule_truth"]


@contextlib.contextmanager
def recording_matches(sink: dict, tag: list):
    """Matcher 결과만 기록 (판정은 그대로). tag[0] = 지금 평가 중인 (시각, 환자)."""
    original = matcher.match_trial

    def wrapped(profile, parsed, trial, **kw):
        result = original(profile, parsed, trial, **kw)
        sink[(*tag[0], parsed.trial_id)] = result
        return result
    matcher.match_trial = wrapped
    try:
        yield
    finally:
        matcher.match_trial = original


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple, set, frozenset)):
        return ";".join(str(x) for x in v)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False, sort_keys=True)
    return str(v)


def _passes_raw(kind: str, holds: str | None) -> bool | None:
    return None if holds is None else cb._passes(kind, holds)


def evaluate(gc, sim) -> tuple[dict, dict, dict, dict]:
    """(시각별 쌍 상태, Matcher 결과, 시각별 기준 상태, 별칭 -> 환자). 입력은 rule_labels·runner.run의 첫 상태와 같음."""
    ev = OfflineEvaluator(sim.trials, sim.reference_date, sim.judge)
    aliases = sim.aliases()
    matches: dict = {}
    tag: list = [None]
    truth, public = {}, {}
    with recording_matches(matches, tag):
        for p in sim.patients:
            alias = aliases[p.key]
            tag[0] = ("truth", p.key)
            facts = [make_fact(alias, f, seq=0, origin=f"truth{i}", source_type="clinical_record", verified=True,
                               ref_kind="document", ref=None) for i, f in enumerate(sim.oracle.truth[p.key])]
            truth[alias] = ev.evaluate(alias, facts, p.trials)[0]
            tag[0] = ("public", p.key)
            facts = [make_fact(alias, f, seq=0, origin=f"public{i}", source_type=f.source_type, verified=f.verified,
                               ref_kind="input_text", ref=None) for i, f in enumerate(p.public)]
            public[alias] = ev.evaluate(alias, facts, p.trials)[0]
    rev = {a: k for k, a in aliases.items()}
    pairs = {view: {(rev[p.pair.patient_alias], p.pair.trial_id): p
                    for p in CohortState.from_decision_states(states).pairs()}
             for view, states in (("truth", truth), ("public", public))}
    states = {"truth": truth, "public": public}
    return pairs, matches, states, aliases


def mechanism(row: dict) -> str:
    """기준 한 줄의 차이가 생긴 코드 경로. 값이 같으면 빈 문자열. 맞고 틀림을 뜻하지 않음."""
    if not row["differs"]:
        return ""
    if not row["board_bound"]:
        return "board_rule_unbound"
    flags = row["sim_validation_flags"]
    if row["sim_holds"] == "unknown":
        return f"sim_gate:{flags}" if flags else "sim_unknown_no_flag"
    if row["board_holds"] == "unknown":
        if "hidden" in row["slot_visibility"]:
            return "info_asymmetry:hidden_slot_in_truth_view"
        if row["board_gap"] == "stale":
            return "info_asymmetry:stale_lab_in_board"
        return "board_unknown_other"
    return "value_differs"


def criterion_rows(gc, sd, smap, sim, group, key, pairs, matches, states, aliases) -> list[dict]:
    pid, tid = key
    public = gc.public
    pkey = f"{pid}|{tid}"
    patient = next(p for p in public.patients if p.patient_id == pid)
    board = cb.pair_board(gc.model_dump(mode="json")["public"], patient.model_dump(mode="json"), tid,
                          cb._expert_slots(gc.model_dump(mode="json")["public"]))
    board_rules = {x["rule_id"]: x for x in board["rules"]}
    refs = {r.rule_id: r for r in public.rules[tid]}
    hidden = gc.oracle.hidden.get(pid, {})
    trial = next(t for t in sim.trials if t.trial_id == tid)
    bindings = {b.key: b for b in sd.bindings}
    match_t, match_p = matches[("truth", pid, tid)], matches[("public", pid, tid)]
    assess_t = {a.rule_id: a for a in [*match_t.assessments, *match_t.other_cohort_assessments]}
    assess_p = {a.rule_id: a for a in [*match_p.assessments, *match_p.other_cohort_assessments]}
    state_t = {c.rule_id: c for c in states["truth"][aliases[pid]].trials[tid].criteria}
    rows = []
    for rule in trial.parsed.rules:
        rid = rule.rule_id
        ref, bd, a, ap, c = refs.get(rid), board_rules.get(rid), assess_t.get(rid), assess_p.get(rid), state_t.get(rid)
        b = bindings.get(f"{tid}:{rid}")
        slots = ref.slots() if ref else (b.slots_used() if b else [])
        vis = [f"{s}={'public' if s in patient.known else 'hidden' if s in hidden else 'absent'}" for s in slots]
        gen = sorted({es.base_source for es in public.sources.values() if set(es.reveals) & set(slots)})
        eng = sorted({smap.sources[g] for g in gen})
        holds = public.initial_holds.get(f"{pkey}|{rid}") if ref else None
        board_pass = None if holds is None else (None if bd["gap"] == "physician_review" else cb._passes(ref.kind, holds))
        raw = (a.llm_judgment or {}).get("holds") if a else None
        row = {"group": group, "pair_id": f"{gc.cohort_id}|{pkey}", "cohort_id": gc.cohort_id, "patient_id": pid,
               "trial_id": tid, "rule_id": rid, "kind": rule.kind, "category": rule.category,
               "time_window_days": rule.time_window_days,
               "required_evidence": a.required_evidence if a else (ref.required_evidence if ref else None),
               "rule_text": " ".join(rule.text.split()), "board_bound": ref is not None, "slots": slots,
               "slot_visibility": vis, "generator_sources": gen, "engine_sources": eng,
               "binding_status": b.status if b else "none", "binding_used_by_judge": f"{tid}:{rid}" in sim.judge.bindings,
               "binding_truth": b.truth if b else None, "binding_also_slots": [x.slot for x in b.also] if b else [],
               "binding_combine": b.combine if b and b.also else None,
               "board_holds": holds, "board_passes": board_pass, "board_gap": bd["gap"] if bd else None,
               "board_applied": bd["applied"] if bd else None,
               "sim_holds": a.holds if a else None, "sim_passes": a.passes if a else None, "sim_raw_judge_holds": raw,
               "sim_raw_passes": _passes_raw(rule.kind, raw), "sim_method": a.method if a else None,
               "sim_state": c.status if c else None, "sim_gap": c.gap if c else None,
               "sim_requirement": c.requirement if c else None,
               "sim_evidence_source": a.evidence_source if a else None,
               "sim_evidence_directness": a.evidence_directness if a else None,
               "sim_validation_flags": a.validation_flags if a else [],
               "sim_needs_verification": a.needs_verification if a else None,
               "sim_basis_tier": c.basis_tier if c else None, "sim_evidence_refs": len(c.evidence_refs) if c else 0,
               "sim_public_holds": ap.holds if ap else None,
               "sim_public_gap": next((x.gap for x in states["public"][aliases[pid]].trials[tid].criteria
                                       if x.rule_id == rid), None),
               "sim_public_validation_flags": ap.validation_flags if ap else [],
               "trial_cohorts": trial.parsed.cohorts, "rule_cohort": rule.cohort, "board_path": board["path"],
               "sim_matched_cohort": match_t.matched_cohort,
               "generator_rule_truth": gc.oracle.truth.get(f"{pkey}|{rid}")}
        row["sim_validation_flags"] = _text(row["sim_validation_flags"])
        row["slot_visibility"] = _text(vis)
        row["differs"] = row["board_bound"] and row["board_passes"] != row["sim_passes"]
        row["mechanism"] = mechanism(row)
        board_fail, sim_fail = row["board_passes"] is False, row["sim_passes"] is False
        row["drives_status"] = (board_fail and not sim_fail) if group == BOARD_CI_SIM_IM else (sim_fail and not board_fail)
        rows.append(row)
    return rows


def pair_row(gc, group, key, pairs, matches, crit) -> dict:
    pid, tid = key
    pkey = f"{pid}|{tid}"
    d = gc.model_dump(mode="json")["public"]
    patient = next(p for p in d["patients"] if p["patient_id"] == pid)
    b = cb.pair_board(d, patient, tid, cb._expert_slots(d))
    m = matches[("truth", pid, tid)]
    driving = [r for r in crit if r["drives_status"]]
    return {"group": group, "pair_id": f"{gc.cohort_id}|{pkey}", "cohort_id": gc.cohort_id, "patient_id": pid,
            "trial_id": tid, "board_status": b["status"], "sim_truth_status": pairs["truth"][key].status.value,
            "sim_public_status": pairs["public"][key].status.value,
            "board_failing_rules": [r["rule_id"] for r in crit if r["board_passes"] is False],
            "sim_failing_rules": [r["rule_id"] for r in crit if r["sim_passes"] is False],
            "driving_rules": [r["rule_id"] for r in driving],
            "pair_mechanism": sorted({r["mechanism"] for r in driving}),
            "board_gaps": sorted({x["gap"] for x in b["rules"]} - {"none"}),
            "sim_gaps": sorted(pairs["truth"][key].gaps), "n_rules_differ": sum(r["differs"] for r in crit),
            "n_unbound_rules": b["unbound_rules"], "trial_cohorts": crit[0]["trial_cohorts"],
            "board_path": b["path"], "sim_matched_cohort": m.matched_cohort, "sim_cohort_results": m.cohort_results,
            "generator_pair_label": gc.oracle.pair_label.get(pkey)}


def compute(limit: int | None = None) -> dict:
    cfg, sd, cat, parsed = G.load_inputs(config.DATA_DIR / "cohort" / "generator_config.json")
    smap = FG.load_source_map(config.ROOT / "data" / "cohort" / "source_map.json")
    cohorts = G.generate_all(cfg, sd, cat, parsed)
    cohorts = cohorts[:limit] if limit else cohorts
    trials = FG.load_trials(cohorts[0].public.trials)
    tri, pair_rows, crit_rows = Counter(), [], []
    agree_other_rule = 0
    for gc in cohorts:
        sim = FG.to_sim_cohort(gc, sd, smap, trials)
        board = cb.board(gc.model_dump(mode="json"))["pairs"]
        pairs, matches, states, aliases = evaluate(gc, sim)
        for key in sorted(pairs["truth"]):
            pid, tid = key
            bs, ts = board[f"{pid}|{tid}"]["status"], pairs["truth"][key].status.value
            tri[(bs, ts, pairs["public"][key].status.value)] += 1
            group = GROUPS.get((bs, ts))
            if group is None:
                if bs == ts == "CONFIRMED_INELIGIBLE":  # 상태는 같지만 불통과 기준이 겹치지 않는 쌍 (집계만)
                    rows = criterion_rows(gc, sd, smap, sim, "", key, pairs, matches, states, aliases)
                    agree_other_rule += not ({r["rule_id"] for r in rows if r["board_passes"] is False}
                                             & {r["rule_id"] for r in rows if r["sim_passes"] is False})
                continue
            rows = criterion_rows(gc, sd, smap, sim, group, key, pairs, matches, states, aliases)
            crit_rows += rows
            pair_rows.append(pair_row(gc, group, key, pairs, matches, rows))
    return {"pairs": sorted(pair_rows, key=lambda r: (r["group"], r["pair_id"])),
            "criteria": sorted(crit_rows, key=lambda r: (r["group"], r["pair_id"], r["rule_id"])), "cross_tab": tri,
            "cohorts": [c.cohort_id for c in cohorts], "config_hash": sorted({c.config_hash for c in cohorts}),
            "store_hash": sim.store_hash(), "agree_on_status_with_no_common_failing_rule": agree_other_rule}


def _csv(rows: list[dict], columns: list[str]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(columns)
    for r in rows:
        w.writerow([_text(r.get(c)) for c in columns])
    return buf.getvalue()


def render(res: dict) -> dict[str, str]:
    """파일명 -> 내용. 시각·경로·커밋을 넣지 않음 (같은 입력 -> 같은 바이트)."""
    files = {"board_sim_pairs.csv": _csv(res["pairs"], PAIR_COLUMNS),
             "board_sim_criteria.csv": _csv(res["criteria"], CRITERION_COLUMNS)}
    for group, name in ((BOARD_CI_SIM_IM, "pair_ids_board_CI_sim_IM.txt"), (BOARD_IM_SIM_CI, "pair_ids_board_IM_sim_CI.txt")):
        files[name] = "".join(r["pair_id"] + "\n" for r in res["pairs"] if r["group"] == group)
    driving = [r for r in res["criteria"] if r["drives_status"]]
    inputs = {f: hashlib.sha256((config.ROOT / f).read_bytes()).hexdigest() for f in INPUT_FILES}
    summary = {
        "script_version": SCRIPT_VERSION, "cohorts": len(res["cohorts"]), "generator_config_hash": res["config_hash"],
        "trial_store_hash": res["store_hash"], "input_sha256": inputs,
        "pairs_total": sum(res["cross_tab"].values()),
        "cross_tab_board_simtruth_simpublic": [{"board": k[0], "sim_truth": k[1], "sim_public": k[2], "pairs": v}
                                               for k, v in sorted(res["cross_tab"].items())],
        "groups": dict(sorted(Counter(r["group"] for r in res["pairs"]).items())),
        "driving_rule_rows_by_group_and_mechanism": [
            {"group": k[0], "mechanism": k[1], "rows": v}
            for k, v in sorted(Counter((r["group"], r["mechanism"]) for r in driving).items())],
        "driving_rule_rows_by_trial_rule": [
            {"group": k[0], "trial_id": k[1], "rule_id": k[2], "slots": k[3], "rows": v}
            for k, v in sorted(Counter((r["group"], r["trial_id"], r["rule_id"], _text(r["slots"])) for r in driving).items())],
        "sim_public_status_by_group": [{"group": k[0], "sim_public": k[1], "pairs": v}
                                       for k, v in sorted(Counter((r["group"], r["sim_public_status"])
                                                                  for r in res["pairs"]).items())],
        "generator_pair_label_by_group": [{"group": k[0], "label": k[1], "pairs": v}
                                          for k, v in sorted(Counter((r["group"], r["generator_pair_label"])
                                                                     for r in res["pairs"]).items())],
        "agree_CI_with_no_common_failing_rule": res["agree_on_status_with_no_common_failing_rule"],
        "note": "mechanism은 코드 경로이며 어느 쪽이 맞는지의 판정이 아님"}
    files["summary.json"] = json.dumps(summary, ensure_ascii=False, indent=1, sort_keys=False) + "\n"
    return files


def same_file(path: Path, text: str) -> bool:
    """저장된 파일과 같은가. git 줄바꿈 변환(autocrlf)으로 생긴 CRLF는 차이로 보지 않음."""
    return path.exists() and path.read_bytes().replace(b"\r\n", b"\n") == text.encode("utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--check", action="store_true", help="파일을 쓰지 않고 저장된 파일과 비교")
    args = ap.parse_args(argv)
    res = compute()
    files = render(res)
    if args.check:
        bad = [n for n, text in files.items() if not same_file(args.out / n, text)]
        print("차이 없음" if not bad else f"다름: {bad}")
        return 1 if bad else 0
    args.out.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (args.out / name).write_bytes(text.encode("utf-8"))
    print(json.dumps({"groups": dict(Counter(r["group"] for r in res["pairs"])), "criteria_rows": len(res["criteria"]),
                      "out": str(args.out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
