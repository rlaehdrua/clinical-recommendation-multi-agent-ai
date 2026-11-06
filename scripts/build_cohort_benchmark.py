"""후보 A용 합성 코호트 벤치마크 생성 (LLM 호출 없음).

    # 1) 슬롯 사전 초안: 구조화 기준만 자동 연결, 나머지는 needs_review (사람 검수 대상)
    python scripts/build_cohort_benchmark.py draft-slots --trials NCT06125405 NCT06530004 NCT07087314
    # 2) 비구조화 기준의 LLM 초안 넣기 (status=draft, 정답에는 안 쓰임) + 초안 사전 분포를 생성 설정에 추가
    python scripts/build_cohort_benchmark.py apply-draft --draft data/cohort/llm_draft_nephrotic_peds.json
    # 3) 사람 검수: 시트를 만들어 '검수 결과' 칸을 채운 뒤 반영 ('확인'만 reviewed)
    python scripts/build_cohort_benchmark.py review-sheet --out data/cohort/review_sheet_nephrotic_peds.csv
    python scripts/build_cohort_benchmark.py apply-review --sheet data/cohort/review_sheet_nephrotic_peds.csv
    # (참고) 초안이 모두 검수 통과했다고 가정했을 때의 구조 통계만 미리 보기 (아무것도 저장하지 않음)
    python scripts/build_cohort_benchmark.py preview-drafts
    # 4) 코호트 생성 + 검증 + 구조 통계
    python scripts/build_cohort_benchmark.py generate --config data/cohort/generator_config.json

- draft-slots는 --out 파일이 있으면 그 안의 reviewed·excluded·draft 연결과 사람이 추가한 슬롯을 보존합니다.
- generate는 검증에 실패한 코호트가 하나라도 있으면 아무것도 쓰지 않고 종료 코드 1로 끝납니다.
  출력: <out>/<config_hash>/<cohort_id>.json, report.json (data/cache/ 아래라 git에 올라가지 않음)
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from ctrec import config
from ctrec.cohort import generate as gen
from ctrec.cohort import slots as slot_draft
from ctrec.cohort.spec import GeneratorConfig, Prior, SlotDictionary
from ctrec.cohort.validate import stats, validate

DEFAULT_SLOTS = config.DATA_DIR / "cohort" / "slot_dictionary.draft.json"
DEFAULT_OUT = config.CACHE_DIR / "cohort"
DEFAULT_CONFIG = config.DATA_DIR / "cohort" / "generator_config.json"
KO_FILE = config.DATA_DIR / "i18n" / "trials_ko.json"


def _load_sd(path) -> SlotDictionary:
    return SlotDictionary(**json.loads(Path(path).read_text(encoding="utf-8")))


def _save_json(path, obj) -> None:
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def cmd_draft(args) -> int:
    out = Path(args.out)
    existing = SlotDictionary(**json.loads(out.read_text(encoding="utf-8"))) if out.exists() else None
    d = slot_draft.draft(gen.load_parsed(args.trials), existing)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(d.model_dump(mode="json"), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(slot_draft.summary(d), ensure_ascii=False, indent=1))
    print(f"saved {out}")
    return 0


def cmd_apply_draft(args) -> int:
    draft = json.loads(Path(args.draft).read_text(encoding="utf-8"))
    sd = _load_sd(args.slots)
    new, report = slot_draft.apply_draft(sd, draft, gen.load_parsed(draft["meta"]["trials"]))
    _save_json(args.slots, new.model_dump(mode="json"))
    # 초안 사전 분포: 같은 슬롯이 이미 있으면 덮어쓰지 않음
    cfg_raw = json.loads(Path(args.config).read_text(encoding="utf-8"))
    priors = cfg_raw["clusters"][draft["meta"]["cluster"]]["priors"]
    added = []
    for sid, raw in draft.get("priors", {}).items():
        if sid not in priors:
            Prior(**raw)  # 확률 합 등 검증
            priors[sid] = {**raw, "basis": raw.get("basis", "가정 (LLM 초안, 검수 필요)")}
            added.append(sid)
    GeneratorConfig(**cfg_raw)
    _save_json(args.config, cfg_raw)
    print(json.dumps({"applied": len(report["applied"]), "skipped": report["skipped"],
                      "not_in_dictionary": report["not_in_dictionary"], "problems": report["problems"],
                      "priors_added": len(added)}, ensure_ascii=False, indent=1))
    print(json.dumps(slot_draft.summary(new)["by_status"], ensure_ascii=False))
    return 1 if report["problems"] or report["not_in_dictionary"] else 0


def cmd_review_sheet(args) -> int:
    sd = _load_sd(args.slots)
    ko = json.loads(KO_FILE.read_text(encoding="utf-8")) if KO_FILE.exists() else {}
    rows = slot_draft.review_rows(sd, gen.load_parsed(sd.built_from), ko)
    with open(args.out, "w", encoding="utf-8-sig", newline="") as f:  # utf-8-sig: 엑셀에서 한글이 깨지지 않게
        w = csv.writer(f)
        w.writerow(slot_draft.REVIEW_HEADER)
        w.writerows(rows)
    print(f"saved {args.out} ({len(rows)} rows)")
    return 0


def cmd_apply_review(args) -> int:
    sd = _load_sd(args.slots)
    with open(args.sheet, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    new, report = slot_draft.apply_review(sd, rows, gen.load_parsed(sd.built_from))
    _save_json(args.slots, new.model_dump(mode="json"))
    print(json.dumps({k: (len(v) if k != "rejected" else v) for k, v in report.items()}, ensure_ascii=False, indent=1))
    return 1 if report["rejected"] else 0


def cmd_preview(args) -> int:
    """초안이 모두 검수 통과했다고 가정한 구조 통계 (메모리에서만, 저장 안 함). 실제 실험에 쓰면 안 됨."""
    cfg, sd, cat, parsed = gen.load_inputs(args.config)
    promoted = SlotDictionary(built_from=sd.built_from, slots=sd.slots, bindings=[
        b.model_copy(update={"status": "reviewed"}) if b.status == "draft" else b for b in sd.bindings])
    for c in gen.generate_all(cfg, promoted, cat, parsed):
        problems = validate(c, promoted, parsed)
        s = stats(c)
        print(f"{s['cohort_id']:<28} pairs={s['pairs']:<3} open={s['open_pairs']:<3} eligible={s['eligible_rate']} "
              f"shared_hidden={s['shared_hidden_slot_rate']} sources={s['sources']} unknown/pair={s['unknown_rules_per_pair']}"
              f"{'  PROBLEMS=' + str(len(problems)) if problems else ''}")
    print("(미리 보기: 초안을 검수 통과로 가정. 아무것도 저장하지 않음)")
    return 0


def cmd_generate(args) -> int:
    cfg, sd, cat, parsed = gen.load_inputs(args.config)
    cohorts = gen.generate_all(cfg, sd, cat, parsed)
    problems = {c.cohort_id: validate(c, sd, parsed) for c in cohorts}
    problems = {k: v for k, v in problems.items() if v}
    if problems:
        for cid, issues in problems.items():
            print(f"[FAIL] {cid}: {len(issues)}개 문제, 예: {issues[:3]}", file=sys.stderr)
        return 1
    chash = cohorts[0].config_hash if cohorts else gen.config_hash(cfg, sd, cat)
    out = Path(args.out) / chash
    out.mkdir(parents=True, exist_ok=True)
    for c in cohorts:
        (out / f"{c.cohort_id}.json").write_text(json.dumps(c.model_dump(mode="json"), ensure_ascii=False) + "\n",
                                                 encoding="utf-8")
    report = {"config": str(args.config), "config_hash": chash, "generator": gen.GENERATOR_VERSION,
              "cohorts": [stats(c) for c in cohorts]}
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for s in report["cohorts"]:
        print(f"{s['cohort_id']:<28} {s['split']:<4} pairs={s['pairs']:<3} open={s['open_pairs']:<3} "
              f"unknown/pair={s['unknown_rules_per_pair']} eligible={s['eligible_rate']} "
              f"shared_hidden={s['shared_hidden_slot_rate']} sources={s['sources']}")
    print(f"saved {len(cohorts)} cohorts -> {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("draft-slots")
    d.add_argument("--trials", nargs="+", required=True)
    d.add_argument("--out", default=str(DEFAULT_SLOTS))
    a = sub.add_parser("apply-draft")
    a.add_argument("--draft", required=True)
    a.add_argument("--slots", default=str(DEFAULT_SLOTS))
    a.add_argument("--config", default=str(DEFAULT_CONFIG))
    r = sub.add_parser("review-sheet")
    r.add_argument("--slots", default=str(DEFAULT_SLOTS))
    r.add_argument("--out", required=True)
    v = sub.add_parser("apply-review")
    v.add_argument("--slots", default=str(DEFAULT_SLOTS))
    v.add_argument("--sheet", required=True)
    p = sub.add_parser("preview-drafts")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    g = sub.add_parser("generate")
    g.add_argument("--config", default=str(DEFAULT_CONFIG))
    g.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()
    return {"draft-slots": cmd_draft, "apply-draft": cmd_apply_draft, "review-sheet": cmd_review_sheet,
            "apply-review": cmd_apply_review, "preview-drafts": cmd_preview, "generate": cmd_generate}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
