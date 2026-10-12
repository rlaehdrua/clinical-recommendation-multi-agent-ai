"""잠긴 기준표(data/parsed/)의 규칙 단위 패치: 제안 -> 팀 승인 -> 후보 저장소(candidate store)에만 적용 -> diff·검사.

원본 고정 저장소는 읽기 전용입니다. 이 모듈은 data/parsed/와 FROZEN.json을 쓰지 않습니다.

1) 제안(propose): 저장된 기준표와 원문을 대조해 규칙 단위 패치 제안을 만듭니다(LLM 없음). 제안은 승인이 아닙니다.
   - restore_engine_fields: 검증 단계에서 지워진 나이 경계 복구. 지워진 값이 기록에 있고, 원문 범위의 짝 규칙과
     CT.gov 구조화 나이가 같은 값을 가리키며, 무결성 검사를 통과할 때만 제안
   - clear_orphan_unit: 규칙 엔진 필드 없이 단위만 남은 규칙의 단위 제거 (다른 필드는 만들지 않음, 판정 의미 변화 없음)
   - flag_review: 원문 수치·단위가 의심스러운 기준(예: 혈소판 기준이 10^12/L)을 의사 확인 항목으로 표시.
     값은 고치지 않고, 검수 전까지 자동 확정 판정에 쓰지 않음
2) 승인(approve): 사람이 승인 파일에 기록합니다. 승인은 패치 내용의 해시(digest)에 묶여, 승인 뒤 패치를 고치면 무효입니다.
3) 적용(apply): 원본 무결성(FROZEN.json 해시)을 확인한 뒤 원본을 복사한 후보 저장소에만, 승인된 패치만 적용합니다.
   하나라도 거부되면 아무것도 쓰지 않습니다. 기존 값이 다르거나, 시험·규칙이 없거나, 이미 적용한 패치면 거부합니다.
4) diff·check: 원본과 후보의 규칙 단위 차이, 후보 기준표의 오프라인 검사(후보 파일 해시, 로드, 규칙 엔진 필드 형식,
   수치 무결성, 단위만 남은 규칙). 후보 파일 해시는 CANDIDATE.json에 기록되어 적용 뒤 손으로 고친 파일을 잡아냄.

후보를 공식 기준표로 바꾸는 일(data/parsed/ 교체와 다시 잠금)은 팀 승인 후 사람이 합니다.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import config, trial_store
from .agents import criteria_parser, parser_validator
from .schemas import ParsedTrial, Rule
from .tools import rule_engine

PATCH_FORMAT = 1
CANDIDATE_FILE = "CANDIDATE.json"
# 패치로 바꿀 수 있는 키 (원문 문장·선정/제외·분류 등은 패치 대상이 아님)
RULE_KEYS = {"field", "operator", "value", "unit", "structured_evaluable"}
META_KEYS = {"review"}
KINDS = {"restore_engine_fields", "clear_orphan_unit", "flag_review"}
_REMOVED = re.compile(r"^(\w+): 규칙 엔진 필드 제거 \((\S+) (\S+) (\S+) (\S+)\)")


class PatchError(ValueError):
    """패치를 적용할 수 없음 (아무것도 쓰지 않음)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def patch_digest(patch: dict) -> str:
    """승인이 가리키는 패치 내용의 해시 (제안 쪽 status 표시는 제외)."""
    body = {k: v for k, v in patch.items() if k != "status"}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------------------
# 원본 무결성
# ---------------------------------------------------------------------------

def verify_frozen(store_dir: Path | None = None) -> list[str]:
    """원본 고정 저장소가 FROZEN.json과 같은지. 반환: 문제 목록 (비어 있으면 정상)."""
    store = store_dir or config.PARSED_DIR
    manifest = trial_store.frozen_manifest(store)
    if not manifest.get("files"):
        return [f"{store / trial_store.FREEZE_FILE}이 없거나 비어 있음"]
    problems = []
    for tid, digest in manifest["files"].items():
        path = trial_store.path_for(tid, store)
        if not path.exists():
            problems.append(f"{tid}: 파일 없음")
        elif trial_store.file_hash(path) != digest:
            problems.append(f"{tid}: 잠긴 기준표와 다름")
    extra = {p.stem for p in store.glob("*.json") if p.name != trial_store.FREEZE_FILE} - set(manifest["files"])
    problems += [f"{tid}: 잠금 목록에 없는 파일" for tid in sorted(extra)]
    return problems


# ---------------------------------------------------------------------------
# 1) 제안
# ---------------------------------------------------------------------------

def _rule_values(rule: dict, keys) -> dict:
    return {k: rule.get(k) for k in keys}


def _ctgov_age(trial: dict, key: str) -> tuple[str, str] | None:
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([A-Za-z]+)\s*$", trial.get(key) or "")
    return (m.group(1), m.group(2).lower()) if m else None


def propose(store_dir: Path | None = None, trials_dir: Path | None = None) -> dict:
    """저장된 기준표와 원문을 대조해 패치 제안을 만듦. 모든 제안은 status=proposed (승인 아님)."""
    store = store_dir or config.PARSED_DIR
    trials_dir = trials_dir or config.TRIALS_DIR
    patches = []
    for tid in sorted(trial_store.frozen_manifest(store).get("files", {})):
        rec = _read(trial_store.path_for(tid, store))
        trial = _read(trials_dir / f"{tid}.json")
        rules = rec["parsed"]["rules"]
        removed = {m.group(1): m.groups()[1:] for c in rec["validation"].get("changes", []) if (m := _REMOVED.match(c))}
        restored: set[str] = set()
        for rule in rules:
            rid = rule["rule_id"]
            if rid in removed and rule["field"] is None and removed[rid][0] == "age":
                p = _restore_age_proposal(tid, rule, rules, removed[rid], trial)
                if p:
                    patches.append(p)
                    restored.add(rid)
        for rule in rules:
            rid = rule["rule_id"]
            if rule["field"] is None and rule.get("unit") and rid not in restored:
                patches.append({
                    "patch_id": f"{tid}-{rid}-clear-orphan-unit", "trial_id": tid, "rule_id": rid,
                    "kind": "clear_orphan_unit",
                    "before": {"rule": _rule_values(rule, ["field", "unit"])},
                    "after": {"rule": {"field": None, "unit": None}},
                    "rationale": "규칙 엔진 필드 없이 단위만 남은 구조 정리. 다른 구조화 필드는 만들지 않으며 판정 의미는 바뀌지 않음"
                                 "(필드가 없어 규칙 엔진이 쓰지 않고 LLM이 원문으로 판정).",
                    "evidence": {"source_text": rule["text"]},
                    "status": "proposed",
                })
            field = rule.get("field") or ""
            meta = rec.get("rule_meta", {}).get(rid, {})
            if (field.startswith("lab:") and not meta.get("review")
                    and rule_engine._nonstandard_count_rule_unit(rule_engine.canonical_lab(field[4:]), rule.get("unit"))):
                patches.append({
                    "patch_id": f"{tid}-{rid}-flag-review", "trial_id": tid, "rule_id": rid, "kind": "flag_review",
                    "before": {"meta": {"review": None}},
                    "after": {"meta": {"review": f"원문 기준값·단위 확인 필요: '{rule['text']}' - {field[4:]} 기준이 표준 보고 단위가 "
                                                  f"아닌 {rule.get('unit')}로 적혀 있음(오기 의심). 자동 정정하지 않음"}},
                    "rationale": "원문 수치·단위가 의심스러우나 원문을 정정할 근거가 없음. 값은 그대로 두고 검수 전까지 자동 확정 판정에 "
                                 "쓰지 않도록 의사 확인 항목으로 표시.",
                    "evidence": {"source_text": rule["text"], "structured": _rule_values(rule, ["field", "operator", "value", "unit"])},
                    "status": "proposed",
                })
    return {"format": PATCH_FORMAT, "created_at": _now(),
            "created_by": "scripts/review_store.py propose (자동 생성, 팀 검수 전)",
            "base_frozen_at": trial_store.frozen_manifest(store).get("frozen_at"), "patches": patches}


def _restore_age_proposal(tid: str, rule: dict, rules: list[dict], removed: tuple, trial: dict) -> dict | None:
    """지워진 나이 경계를 복구할 근거가 모두 맞을 때만 제안."""
    field, op, value, unit = removed
    candidate = Rule.model_validate({**rule, "field": field, "operator": op, "value": value, "unit": unit,
                                     "structured_evaluable": True})
    partner = parser_validator._range_partner(candidate, [Rule.model_validate(r) for r in rules])
    ctgov = _ctgov_age(trial, "minimum_age" if op in (">=", ">") else "maximum_age")
    numbers = criteria_parser.numbers_in(f"{trial.get('minimum_age') or ''} {trial.get('maximum_age') or ''}")
    if (partner is None or ctgov is None or float(ctgov[0]) != float(value)
            or ctgov[1].rstrip("s") != unit.lower().rstrip("s")
            or criteria_parser._integrity_problem(candidate, numbers) is not None):
        return None
    return {
        "patch_id": f"{tid}-{rule['rule_id']}-restore-age", "trial_id": tid, "rule_id": rule["rule_id"],
        "kind": "restore_engine_fields",
        "before": {"rule": _rule_values(rule, ["field", "operator", "value", "unit", "structured_evaluable"])},
        "after": {"rule": {"field": field, "operator": op, "value": value, "unit": unit, "structured_evaluable": True}},
        "rationale": f"검증 단계가 범위의 한쪽만 보고 지운 나이 경계 복구(Phase 0-2 원인). 원문 범위의 반대쪽 경계는 "
                     f"{partner.rule_id}에 있고, CT.gov 구조화 나이와 같은 값.",
        "evidence": {"source_text": rule["text"], "ctgov": f"{'minimum_age' if op in ('>=', '>') else 'maximum_age'}="
                                                           f"{trial.get('minimum_age' if op in ('>=', '>') else 'maximum_age')}",
                     "range_partner": f"{partner.rule_id}: {partner.field} {partner.operator} {partner.value} {partner.unit}",
                     "removed_by_validator": f"{field} {op} {value} {unit}"},
        "status": "proposed",
    }


# ---------------------------------------------------------------------------
# 2) 승인 (사람이 실행)
# ---------------------------------------------------------------------------

def record_decision(approvals_path: Path, patch: dict, decision: str, reviewer: str, note: str = "") -> dict:
    """검수 결정을 승인 파일에 기록. 패치 내용 해시에 묶임."""
    if decision not in ("approved", "rejected"):
        raise PatchError(f"decision은 approved 또는 rejected: {decision!r}")
    if not reviewer.strip():
        raise PatchError("검수자(reviewer)를 적어야 합니다")
    data = _read(approvals_path) if approvals_path.exists() else {"format": PATCH_FORMAT, "decisions": []}
    entry = {"patch_id": patch["patch_id"], "patch_digest": patch_digest(patch), "decision": decision,
             "reviewer": reviewer.strip(), "reviewed_at": _now(), "note": note}
    data["decisions"].append(entry)
    _write(approvals_path, data)
    return entry


def review_status(patches: dict, approvals: dict | None) -> dict[str, str]:
    """패치별 실제 검수 상태: proposed / approved / rejected / stale(승인 뒤 패치 내용이 바뀜)."""
    latest: dict[str, dict] = {}
    for d in (approvals or {}).get("decisions", []):
        latest[d["patch_id"]] = d  # 마지막 결정이 유효
    status = {}
    for p in patches["patches"]:
        d = latest.get(p["patch_id"])
        if d is None:
            status[p["patch_id"]] = "proposed"
        elif d.get("patch_digest") != patch_digest(p):
            status[p["patch_id"]] = "stale"
        elif d.get("decision") == "approved" and (d.get("reviewer") or "").strip():
            status[p["patch_id"]] = "approved"
        else:
            status[p["patch_id"]] = "rejected"
    return status


# ---------------------------------------------------------------------------
# 3) 후보 저장소에 적용
# ---------------------------------------------------------------------------

def _validate_shape(p: dict) -> None:
    for key in ("patch_id", "trial_id", "rule_id", "kind", "before", "after", "rationale"):
        if not p.get(key):
            raise PatchError(f"{p.get('patch_id')}: '{key}' 없음")
    if p["kind"] not in KINDS:
        raise PatchError(f"{p['patch_id']}: 알 수 없는 kind {p['kind']!r}")
    for side in ("before", "after"):
        unknown = set(p[side]) - {"rule", "meta"}
        if unknown:
            raise PatchError(f"{p['patch_id']}: {side}에 알 수 없는 영역 {sorted(unknown)}")
    bad = (set(p["before"].get("rule", {})) | set(p["after"].get("rule", {}))) - RULE_KEYS
    if bad:
        raise PatchError(f"{p['patch_id']}: 패치로 바꿀 수 없는 규칙 키 {sorted(bad)}")
    bad =(set(p["before"].get("meta", {})) | set(p["after"].get("meta", {}))) - META_KEYS
    if bad:
        raise PatchError(f"{p['patch_id']}: 패치로 바꿀 수 없는 메타 키 {sorted(bad)}")
    for area in ("rule", "meta"):
        if set(p["after"].get(area, {})) - set(p["before"].get(area, {})):
            raise PatchError(f"{p['patch_id']}: after의 키는 모두 before에 기존 값이 있어야 함")


def apply_patches(patches: dict, approvals: dict | None, out_dir: Path, store_dir: Path | None = None) -> dict:
    """승인된 패치만 원본을 복사한 후보 저장소에 적용. 하나라도 문제가 있으면 아무것도 쓰지 않고 PatchError.

    out_dir가 없으면 원본을 복사해 만들고, 있으면(이전 적용 결과) 그 위에 이어서 적용합니다.
    반환: 후보 저장소 기록(CANDIDATE.json 내용).
    """
    store = (store_dir or config.PARSED_DIR).resolve()
    out = out_dir.resolve()
    if out == store or store in out.parents:
        raise PatchError(f"후보 저장소는 원본 저장소({store}) 밖이어야 합니다")
    problems = verify_frozen(store)
    if problems:
        raise PatchError("원본 고정 저장소가 FROZEN.json과 다름: " + "; ".join(problems))
    status = review_status(patches, approvals)
    approvals_by_id = {d["patch_id"]: d for d in (approvals or {}).get("decisions", [])}

    existing = _read(out / CANDIDATE_FILE) if (out / CANDIDATE_FILE).exists() else None
    if out.exists() and existing is None and any(out.iterdir()):
        raise PatchError(f"{out}는 후보 저장소가 아닌 비어 있지 않은 디렉터리입니다")
    if existing and existing.get("base_files") != trial_store.frozen_manifest(store)["files"]:
        raise PatchError("기존 후보 저장소가 현재 원본과 다른 잠금 버전에서 만들어짐 - 새 디렉터리를 쓰세요")
    if existing:
        tampered = verify_candidate(out)
        if tampered:
            raise PatchError("기존 후보 저장소가 기록과 다름: " + "; ".join(tampered))
    applied_ids = {a["patch_id"] for a in (existing or {}).get("applied", [])}

    # 메모리에서 모두 검사·적용한 뒤 마지막에 씀 (전부 적용 또는 전부 거부)
    src = out if existing else store
    records: dict[str, dict] = {}
    seen_ids: set[str] = set()
    touched: set[tuple] = set()
    applied = []
    for p in patches.get("patches", []):
        _validate_shape(p)
        pid = p["patch_id"]
        if pid in seen_ids:
            raise PatchError(f"{pid}: 같은 patch_id가 두 번 있음")
        seen_ids.add(pid)
        if status[pid] != "approved":
            continue  # 승인되지 않은 패치는 적용하지 않음 (아래에서 요청 목록과 대조)
        if pid in applied_ids:
            raise PatchError(f"{pid}: 이미 이 후보 저장소에 적용된 패치")
        tid, rid = p["trial_id"], p["rule_id"]
        if tid not in records:
            path = trial_store.path_for(tid, src)
            if not path.exists():
                raise PatchError(f"{pid}: 시험 {tid}가 기준표에 없음")
            records[tid] = _read(path)
        rec = records[tid]
        rule = next((r for r in rec["parsed"]["rules"] if r["rule_id"] == rid), None)
        if rule is None:
            raise PatchError(f"{pid}: {tid}에 규칙 {rid}가 없음")
        meta = rec.setdefault("rule_meta", {}).setdefault(rid, {})
        for area, target in (("rule", rule), ("meta", meta)):
            for key, want in p["before"].get(area, {}).items():
                if (tid, rid, area, key) in touched:
                    raise PatchError(f"{pid}: {tid} {rid}의 {key}를 다른 패치가 이미 바꿈 (중복 패치)")
                if target.get(key) != want:
                    raise PatchError(f"{pid}: {tid} {rid}의 {area}.{key} 기존 값이 다름 "
                                     f"(제안 {want!r}, 현재 {target.get(key)!r})")
            for key, value in p["after"].get(area, {}).items():
                target[key] = value
                touched.add((tid, rid, area, key))
        Rule.model_validate(rule)  # 스키마 위반이면 여기서 실패
        d = approvals_by_id[pid]
        entry = {"patch_id": pid, "patch_digest": patch_digest(p), "reviewer": d["reviewer"],
                 "reviewed_at": d["reviewed_at"], "applied_at": _now()}
        rec.setdefault("review_patches", []).append(entry)
        applied.append(entry)

    if not existing:
        out.mkdir(parents=True, exist_ok=True)
        for tid in trial_store.frozen_manifest(store)["files"]:
            shutil.copyfile(trial_store.path_for(tid, store), trial_store.path_for(tid, out))
    for tid, rec in records.items():
        _write(trial_store.path_for(tid, out), rec)
    manifest = existing or {"format": PATCH_FORMAT, "created_at": _now(), "base_store": str(store),
                            "base_frozen_at": trial_store.frozen_manifest(store).get("frozen_at"),
                            "base_files": trial_store.frozen_manifest(store)["files"], "applied": []}
    manifest["applied"] += applied
    manifest["skipped_not_approved"] = sorted(pid for pid, s in status.items() if s != "approved")
    # 후보 파일 해시: 적용 뒤 손으로 고친 후보를 검사(check)·이어서 적용할 때 잡아냄
    manifest["files"] = {tid: trial_store.file_hash(trial_store.path_for(tid, out)) for tid in manifest["base_files"]}
    _write(out / CANDIDATE_FILE, manifest)
    return manifest


def verify_candidate(candidate_dir: Path) -> list[str]:
    """후보 저장소 파일이 CANDIDATE.json에 기록된 해시와 같은지. 반환: 문제 목록."""
    path = candidate_dir / CANDIDATE_FILE
    if not path.exists():
        return [f"{path}이 없음 (후보 저장소가 아님)"]
    files = _read(path).get("files") or {}
    if not files:
        return ["CANDIDATE.json에 파일 해시가 없음"]
    problems = []
    for tid, digest in files.items():
        p = trial_store.path_for(tid, candidate_dir)
        if not p.exists():
            problems.append(f"{tid}: 후보 파일 없음")
        elif trial_store.file_hash(p) != digest:
            problems.append(f"{tid}: 후보 파일이 적용 기록 이후 바뀜")
    extra = {p.stem for p in candidate_dir.glob("*.json") if p.name != CANDIDATE_FILE} - set(files)
    problems += [f"{tid}: 기록에 없는 후보 파일" for tid in sorted(extra)]
    return problems


# ---------------------------------------------------------------------------
# 4) diff·검사
# ---------------------------------------------------------------------------

def rule_diff(base_dir: Path, candidate_dir: Path) -> list[dict]:
    """규칙 단위 차이: [{trial_id, rule_id, area, key, before, after}]."""
    diffs = []
    for path in sorted(candidate_dir.glob("*.json")):
        if path.name in (CANDIDATE_FILE, trial_store.FREEZE_FILE):
            continue
        base_path = base_dir / path.name
        if not base_path.exists():
            diffs.append({"trial_id": path.stem, "rule_id": None, "area": "trial", "key": "added", "before": None,
                          "after": True})
            continue
        base, cand = _read(base_path), _read(path)
        b_rules = {r["rule_id"]: r for r in base["parsed"]["rules"]}
        c_rules = {r["rule_id"]: r for r in cand["parsed"]["rules"]}
        for rid in sorted(set(b_rules) | set(c_rules)):
            b, c = b_rules.get(rid), c_rules.get(rid)
            if b is None or c is None:
                diffs.append({"trial_id": path.stem, "rule_id": rid, "area": "rule", "key": "(rule)",
                              "before": b is not None, "after": c is not None})
                continue
            for key in sorted(set(b) | set(c)):
                if b.get(key) != c.get(key):
                    diffs.append({"trial_id": path.stem, "rule_id": rid, "area": "rule", "key": key,
                                  "before": b.get(key), "after": c.get(key)})
            bm, cm = base.get("rule_meta", {}).get(rid, {}), cand.get("rule_meta", {}).get(rid, {})
            for key in sorted(set(bm) | set(cm)):
                if bm.get(key) != cm.get(key):
                    diffs.append({"trial_id": path.stem, "rule_id": rid, "area": "meta", "key": key,
                                  "before": bm.get(key), "after": cm.get(key)})
    return diffs


def check_store(store_dir: Path, trials_dir: Path | None = None) -> list[str]:
    """기준표 오프라인 검사(LLM 없음): 후보 파일 해시(후보 저장소일 때), 로드, 규칙 엔진 필드 형식, 수치 무결성,
    단위만 남은 규칙. 반환: 문제 목록."""
    trials_dir = trials_dir or config.TRIALS_DIR
    problems = verify_candidate(store_dir) if (store_dir / CANDIDATE_FILE).exists() else []
    for path in sorted(store_dir.glob("*.json")):
        if path.name in (CANDIDATE_FILE, trial_store.FREEZE_FILE):
            continue
        trial = _read(trials_dir / f"{path.stem}.json")
        try:
            parsed: ParsedTrial = trial_store.load(trial, store_dir)
        except Exception as e:  # noqa: BLE001 - 어떤 이유로든 로드 실패는 문제
            problems.append(f"{path.stem}: 로드 실패 - {e}")
            continue
        with_fields = {r.rule_id for r in parsed.rules if r.field}  # 이미 비워진 규칙의 과거 기록은 제외
        det = parser_validator.deterministic_check(copy.deepcopy(parsed), trial)
        for rid, msgs in det.items():
            problems += [f"{path.stem} {rid}: {m}" for m in msgs if "LLM 판정으로 전환" in m and rid in with_fields]
        numbers = criteria_parser.numbers_in(f"{trial.get('minimum_age') or ''} {trial.get('maximum_age') or ''}")
        for r in parsed.rules:
            if r.field and not r.structured_evaluable:
                problems.append(f"{path.stem} {r.rule_id}: 규칙 엔진 필드가 있는데 structured_evaluable=false")
            if not r.field and r.unit:
                problems.append(f"{path.stem} {r.rule_id}: 규칙 엔진 필드 없이 단위만 있음 ({r.unit})")
            prob = criteria_parser._integrity_problem(r, numbers)
            if r.field and prob:
                problems.append(f"{path.stem} {r.rule_id}: {prob}")
    return problems
