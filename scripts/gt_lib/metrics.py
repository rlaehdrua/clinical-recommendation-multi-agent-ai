"""설계 D 3절 지표 계산. 모든 비율은 분자·분모를 그대로 남기고 Wilson·정확(Clopper-Pearson) 95% 구간을 붙입니다.
분모가 0이면 '추정 불가'. 분모가 D 2.8의 최소 n보다 작으면 합격 판정을 하지 않습니다 (d_criterion_met=None).

엔진 출력 형식 (gt-engine-output/1, scripts/gt_engine_adapter.py가 G-U에 대해 만듦):
{
  "format": "gt-engine-output/1",
  "gt_set_id": "...", "gt_set_sha256": "<GT 세트 파일 sha256>",
  "level": "U" | "S" | "R",          # 함수 수준 / 대역 Session / 실제 LLM
  "engine": {"commit": "...", "ctrec_file": "...", "reference_date": "..."},
  "generated_at": "...",
  "items": [
    {"item_id": "...", "layer": "G-U", "kind": "conflict", "result": {"directness": "...", "flags": [...]}},
    {"item_id": "...", "layer": "G-U", "kind": "value",    "result": {"accepted": true, "grounding": null}},
    {"item_id": "...", "layer": "G-P", "result": {"pair_status": "CONFIRMED_ELIGIBLE", "hold_reason": "...",
                                                   "deciding_criteria": ["I1"]}},
    {"item_id": "...", "layer": "G-C", "result": {"scope_represented": true}}
  ]
}
"""

from __future__ import annotations

from collections import Counter, defaultdict

from .models import GTSet
from .stats import NOT_ESTIMABLE, mcnemar_exact, rate

ENGINE_FORMAT = "gt-engine-output/1"
OK_DIRECTNESS = frozenset({"direct", "unchecked", "structured"})  # 확정 경로로 넘어감 (evidence_directness.Check.ok)
PAIR_STATUSES = ("CONFIRMED_ELIGIBLE", "CONFIRMED_INELIGIBLE", "VERIFICATION_PENDING", "INFO_MISSING",
                 "EXPERT_REVIEW", "BLOCKED_FAILURE", "NOT_EVALUATED")
CONFIRMED = {"CONFIRMED_ELIGIBLE", "CONFIRMED_INELIGIBLE"}

# D 2.8 최소 n
MIN_N = {"A1": 60, "A2": 60, "A3": 60, "A4": 60, "A5": 60, "B1": 60, "B1_crossing": 30, "B3": 80,
         "C1": 15, "E1": 60, "E2": 60, "E4": None, "E6": None}

NOT_MEASURED = {
    "A6": "대역 Session(S) 측정. 이 도구의 어댑터는 함수 수준(U)만 실행함. 오프라인으로 가능하나 미구현",
    "A7": "실제 Matcher LLM의 인용 선택 (R). 실제 LLM 호출 필요, 승인 전 0건",
    "B5": "회귀 세트 테스트 통과 여부. pytest로 따로 확인 (이 도구 범위 밖)",
    "B6": "프로필러 LLM의 값·문맥 추출 (R). 실제 LLM 호출 필요",
    "C5": "국가 범위가 없는 나이 기준의 판정 변화. 두 커밋의 상태 전이표(EN-7)가 필요",
    "E5": "순환 GT 일관성(rule_gt_disagreements). 독립 GT 지표가 아니므로 계산하지 않음. 정확도로 인용 금지",
}


def _r(metric: str, x: int, n: int, definition: str, criterion: str | None = None, met=None) -> dict:
    out = {"metric": metric, "definition": definition, **rate(x, n)}
    min_n = MIN_N.get(metric)
    out["min_n_D"] = min_n
    out["n_sufficient"] = None if min_n is None else n >= min_n
    out["d_criterion"] = criterion
    if criterion is not None:
        if n == 0:
            out["d_criterion_met"] = None
            out["d_judgement"] = f"{NOT_ESTIMABLE} (분모 0, 합격으로 치지 않음)"
        elif min_n is not None and n < min_n:
            out["d_criterion_met"] = None
            out["d_judgement"] = f"판정 안 함 (n={n} < 최소 {min_n})"
        else:
            out["d_criterion_met"] = met(out) if met else None
            out["d_judgement"] = {True: "충족", False: "미충족", None: "자동 판정 없음"}[out["d_criterion_met"]]
    return out


def _upper_le(t):
    return lambda o: o["numerator"] == 0 or o["exact95"][1] <= t


def _zero(o):
    return o["numerator"] == 0


def _check_result(r, e, what: str):
    """엔진 결과 한 건의 형식. 기본값으로 메우지 않고 틀리면 ValueError (D-2)."""
    iid = r.item_id
    if not isinstance(e, dict):
        raise ValueError(f"{what} {iid}: result가 객체가 아닙니다 ({type(e).__name__})")
    if r.layer == "G-U":
        if r.input.kind == "conflict":
            if not isinstance(e.get("directness"), str) or not e["directness"]:
                raise ValueError(f"{what} {iid}: conflict 항목에 문자열 directness가 없습니다 ({e.get('directness')!r})")
        elif not isinstance(e.get("accepted"), bool):
            raise ValueError(f"{what} {iid}: value 항목의 accepted가 true/false가 아닙니다 ({e.get('accepted', '<없음>')!r})")
    elif r.layer == "G-P":
        if e.get("pair_status") not in PAIR_STATUSES:
            raise ValueError(f"{what} {iid}: 알 수 없는 엔진 pair_status {e.get('pair_status', '<없음>')!r}")
        dc = e.get("deciding_criteria")
        if dc is not None and not (isinstance(dc, list) and all(isinstance(x, str) for x in dc)):
            raise ValueError(f"{what} {iid}: deciding_criteria는 문자열 목록 또는 null이어야 합니다")
    elif "scope_represented" in e and not isinstance(e["scope_represented"], bool):
        raise ValueError(f"{what} {iid}: scope_represented가 true/false가 아닙니다")


def _results(gt: GTSet, out: dict, what: str) -> dict[str, dict]:
    """엔진 출력 items -> {item_id: result}. 구조가 틀린 항목·중복·형식이 틀린 결과는 ValueError."""
    items = out.get("items")
    if not isinstance(items, list):
        raise ValueError(f"{what}: items가 목록이 아닙니다")
    res = {}
    for i, it in enumerate(items):
        if not isinstance(it, dict) or not isinstance(it.get("item_id"), str) or "result" not in it:
            raise ValueError(f"{what}: items[{i}]에 item_id(문자열)와 result가 필요합니다")
        if it["item_id"] in res:
            raise ValueError(f"{what}: item_id {it['item_id']} 중복")
        res[it["item_id"]] = it["result"]
    for r in gt.items:
        if r.item_id in res:
            _check_result(r, res[r.item_id], what)
    return res


def _split(gt: GTSet, res: dict[str, dict]):
    labelled, ambiguous, missing = [], [], []
    for r in gt.items:
        e = res.get(r.item_id)
        if r.item_id not in res:
            missing.append(r.item_id)
        elif r.adjudication.status == "ambiguous":
            ambiguous.append((r, e))
        elif r.final is not None:
            labelled.append((r, e))
    extra = sorted(set(res) - {r.item_id for r in gt.items})
    return labelled, ambiguous, missing, extra


def _coverage_blockers(gt: GTSet, res: dict[str, dict], what: str) -> list[str]:
    """공식 결과를 막는 커버리지 문제. 빠진 항목을 분모에서 조용히 빼지 않도록 공식 결과 자체를 막습니다."""
    ids = {r.item_id for r in gt.items}
    missing = sorted(ids - set(res))
    extra = sorted(set(res) - ids)
    out = []
    if missing:
        out.append(f"{what}이 GT 항목 {len(missing)}/{len(ids)}건을 덮지 않음 (커버리지 100% 아님): {missing[:10]}")
    if extra:
        out.append(f"{what}에 GT에 없는 항목 {len(extra)}건: {extra[:10]}")
    return out


# ---------------------------------------------------------------------------
# G-U
# ---------------------------------------------------------------------------

def _conflict(rows, amb) -> dict:
    pos = [(r, e) for r, e in rows if r.final.conflict]
    neg = [(r, e) for r, e in rows if not r.final.conflict]
    d = lambda e: e.get("directness")
    a1 = sum(d(e) in OK_DIRECTNESS for _, e in pos)
    a2 = sum(d(e) == "conflicting" for _, e in pos)
    a3 = len(pos) - a1 - a2
    a4 = sum(d(e) == "conflicting" for _, e in neg)
    a5 = sum(d(e) not in OK_DIRECTNESS for _, e in neg)
    clusters = defaultdict(lambda: [0, 0])
    for r, e in pos:
        c = clusters[r.final.concept_cluster or "(미지정)"]
        c[1] += 1
        c[0] += d(e) == "conflicting"
    per_cluster = {k: rate(*v) for k, v in sorted(clusters.items())}
    zero_clusters = [k for k, (x, n) in clusters.items() if n >= 5 and x == 0]
    return {
        "n_positive": len(pos), "n_negative": len(neg),
        "A1": _r("A1", a1, len(pos), "상충 양성 중 directness가 확정 경로(direct/unchecked/structured)",
                 "0건 관측 또는 정확 95% 상한 <= 5%", _upper_le(0.05)),
        "A2": _r("A2", a2, len(pos), "상충 양성 중 directness == conflicting",
                 "재현율 >= 0.80, Wilson 하한 >= 0.65, 묶음 하나라도 0/5 이상이면 불합격",
                 lambda o: o["rate"] >= 0.80 and o["wilson95"][0] >= 0.65 and not zero_clusters),
        "A2_per_cluster": per_cluster,
        "A2_zero_recall_clusters_n_ge_5": zero_clusters,
        "A3": _r("A3", a3, len(pos), "상충 양성 중 그 밖의 보류 (예: unlocated). A1 = 1 - A2 - A3"),
        "A4": _r("A4", a4, len(neg), "상충 음성 중 conflicting (거짓 경보)",
                 "정확 95% 상한 <= 5%", _upper_le(0.05)),
        "A5": _r("A5", a5, len(neg), "상충 음성 중 확정 경로가 아닌 결과 (과잉 차단). D 원문 '≠ direct'를 A1과 같은 "
                 "확정 경로 정의(direct/unchecked/structured)로 읽음", "기준선 + 3%p 이하 (기준선 비교 필요)"),
        "errors": {
            "false_accept": sum(r.final.decision == "hold" and d(e) in OK_DIRECTNESS for r, e in rows),
            "over_hold": sum(r.final.decision == "accept" and d(e) not in OK_DIRECTNESS for r, e in rows),
        },
        "ambiguous": {"n": len(amb), "engine_passed_to_confirm": sum(d(e) in OK_DIRECTNESS for _, e in amb)},
    }


def _value(rows, amb) -> dict:
    acc = lambda e: bool(e.get("accepted"))
    pos = [(r, e) for r, e in rows if r.final.decision == "accept"]
    neg = [(r, e) for r, e in rows if r.final.decision == "hold"]
    hearsay_neg = [(r, e) for r, e in neg if r.final.hearsay]
    crossing = [(r, e) for r, e in hearsay_neg if r.final.threshold_crossing]
    one_side = [(r, e) for r, e in rows if r.final.hearsay and r.final.threshold_crossing is False]
    other = defaultdict(lambda: [0, 0])
    for r, e in neg:
        if not r.final.hearsay:
            c = other[r.final.category]
            c[1] += 1
            c[0] += acc(e)
    return {
        "n_positive": len(pos), "n_negative": len(neg),
        "B1": _r("B1", sum(acc(e) for _, e in hearsay_neg), len(hearsay_neg),
                 "전해 들은 값·불확실(hearsay, GT hold) 중 채택", "정확 95% 상한 <= 5%", _upper_le(0.05)),
        "B1_crossing": _r("B1_crossing", sum(acc(e) for _, e in crossing), len(crossing),
                          "그중 범위가 문턱을 가로지르는 하위 집합의 채택", "0건", _zero),
        "B2": _r("B2", sum(acc(e) for _, e in one_side), len(one_side),
                 "범위가 문턱 한쪽에만 있는 전해 들은 값에서 엔진이 채택(판정)한 비율. 정책은 임상 결정(CL-10), 보고만"),
        "B3": _r("B3", sum(not acc(e) for _, e in pos), len(pos), "정상(GT accept) 값 중 보류 (과잉 보류)",
                 "전체 <= 15% (짝지은 비교에서 새 보류 <= 2는 기준선 필요)", lambda o: o["rate"] <= 0.15),
        "B4": {k: rate(*v) for k, v in sorted(other.items())},
        "errors": {
            "false_accept": sum(acc(e) for _, e in neg),
            "over_hold": sum(not acc(e) for _, e in pos),
        },
        "ambiguous": {"n": len(amb), "engine_accepted": sum(acc(e) for _, e in amb)},
    }


def _unit_correct(r, e) -> bool:
    if r.input.kind == "conflict":
        ok = e.get("directness") in OK_DIRECTNESS
    else:
        ok = bool(e.get("accepted"))
    return ok == (r.final.decision == "accept")


def _unit_paired(rows, base_res) -> dict:
    out = {}
    for kind in ("conflict", "value"):
        fixed = broken = 0
        newly_held, newly_accepted = [], []
        base_cat = Counter()
        cur_cat = Counter()
        n = 0
        for r, e in rows:
            if r.input.kind != kind or r.item_id not in base_res:
                continue
            n += 1
            b = base_res[r.item_id]
            cb, ca = _unit_correct(r, b), _unit_correct(r, e)
            fixed += (not cb) and ca
            broken += cb and (not ca)
            if r.final.decision == "accept" and cb and not ca:
                newly_held.append(r.item_id)
            if r.final.decision == "hold" and cb and not ca:
                newly_accepted.append(r.item_id)
                cur_cat[r.final.category] += 1
            if r.final.decision == "hold" and not cb and ca:
                base_cat[r.final.category] += 1
        out[kind] = {"n_paired": n, "mcnemar": mcnemar_exact(fixed, broken),
                     "newly_held_correct_accepts": newly_held, "newly_accepted_holds": newly_accepted,
                     "newly_accepted_by_category": dict(cur_cat)}
    return out


# ---------------------------------------------------------------------------
# G-P
# ---------------------------------------------------------------------------

def _pairs(rows, amb, base_res) -> dict:
    st = lambda e: e.get("pair_status")
    gt = lambda r: r.final.pair_status
    bad = [st(e) for _, e in rows + amb if st(e) not in PAIR_STATUSES]
    if bad:
        raise ValueError(f"알 수 없는 엔진 pair_status: {sorted(set(map(str, bad)))[:5]}")
    ce = [(r, e) for r, e in rows if st(e) == "CONFIRMED_ELIGIBLE"]
    ci = [(r, e) for r, e in rows if st(e) == "CONFIRMED_INELIGIBLE"]
    g_el = [(r, e) for r, e in rows if gt(r) == "ELIGIBLE"]
    g_in = [(r, e) for r, e in rows if gt(r) == "INELIGIBLE"]
    g_nc = [(r, e) for r, e in rows if gt(r) == "NEEDS_CLINICIAN"]
    fce = sum(gt(r) != "ELIGIBLE" for r, _ in ce)
    fci = sum(gt(r) in ("ELIGIBLE", "NOT_DETERMINABLE") for r, _ in ci)
    hold_cost = Counter()
    for r, e in g_el:
        if st(e) != "CONFIRMED_ELIGIBLE":
            hold_cost[f"{st(e)}|{e.get('hold_reason') or '-'}"] += 1
    unresolved = [(r, e) for r, e in rows if st(e) not in CONFIRMED and gt(r) == "INELIGIBLE"]
    over_hold = [(r, e) for r, e in g_el if st(e) not in CONFIRMED]
    out = {
        "n_pairs": len(rows),
        "gt_distribution": dict(Counter(gt(r) for r, _ in rows)),
        "engine_distribution": dict(Counter(st(e) for _, e in rows)),
        "E1": _r("E1", fce, len(ce), "엔진 CONFIRMED_ELIGIBLE 중 GT가 ELIGIBLE이 아님 (거짓 확정 적격, 정밀도형)",
                 "0건. 분모 < 60이면 추정 불가", _zero),
        "E1_vs_gt_ineligible": rate(sum(gt(r) == "INELIGIBLE" for r, _ in ce), len(g_in)),
        "E2": _r("E2", fci, len(ci), "엔진 CONFIRMED_INELIGIBLE 중 GT가 ELIGIBLE 또는 NOT_DETERMINABLE",
                 "0건 목표, 정확 95% 상한 <= 5%", _upper_le(0.05)),
        "E2_ci_on_needs_clinician": sum(gt(r) == "NEEDS_CLINICIAN" for r, _ in ci),
        "E4": _r("E4", sum(st(e) != "CONFIRMED_ELIGIBLE" for _, e in g_el), len(g_el), "GT ELIGIBLE 중 엔진이 CONFIRMED_ELIGIBLE이 아님 (보류 비용)"),
        "E4_breakdown": dict(hold_cost),
        "E6": _r("E6", sum(st(e) == "CONFIRMED_ELIGIBLE" for _, e in g_nc), len(g_nc),
                 "GT NEEDS_CLINICIAN 중 엔진 CONFIRMED_ELIGIBLE", "0건", _zero),
        "errors": {
            "false_eligible": fce,
            "false_ineligible": fci,
            "false_ineligible_on_needs_clinician": sum(gt(r) == "NEEDS_CLINICIAN" for r, _ in ci),
            "unresolved_gt_ineligible": len(unresolved),
            "over_hold_gt_eligible": len(over_hold),
            "engine_not_evaluated_or_blocked": sum(st(e) in ("NOT_EVALUATED", "BLOCKED_FAILURE") for _, e in rows),
        },
        "ambiguous": {"n": len(amb), "engine_confirmed": sum(st(e) in CONFIRMED for _, e in amb)},
    }
    # 국가 격자 (C2-C4)
    grid = [(r, e) for r, e in rows + amb if r.input.grid is not None]
    if grid:
        cell = lambda c: [(r, e) for r, e in grid if r.input.grid.country_cell == c]
        out_s, in_s, unk = cell("out_of_scope"), cell("in_scope"), cell("unknown")

        def attributed(r, e):
            dc = e.get("deciding_criteria")
            return dc is None or r.input.grid.criterion_id in dc

        out["C2"] = _r("C2", sum(st(e) == "CONFIRMED_INELIGIBLE" and attributed(r, e) and
                                 (r.final is None or gt(r) != "INELIGIBLE") for r, e in out_s), len(out_s),
                       "범위 밖 셀에서 그 기준 때문에 CONFIRMED_INELIGIBLE (GT는 INELIGIBLE 아님)", "0건", _zero)
        out["C3"] = _r("C3", sum(st(e) == "CONFIRMED_ELIGIBLE" and (r.final is None or gt(r) != "ELIGIBLE")
                                 for r, e in in_s), len(in_s),
                       "범위 안 셀에서 CONFIRMED_ELIGIBLE인데 GT는 ELIGIBLE 아님", "0건", _zero)
        out["C4"] = _r("C4", sum(st(e) in CONFIRMED for _, e in unk), len(unk),
                       "국가 모름 셀에서 확정(CE·CI)", "0건", _zero)
    if base_res is not None:
        table = Counter()
        for r, e in rows + amb:
            b = base_res.get(r.item_id)
            if b is not None:
                table[(b.get("pair_status"), st(e))] += 1
        out["E3"] = {
            "transitions": {f"{a} -> {b}": n for (a, b), n in sorted(table.items(), key=str)},
            "entered_confirmed": sum(n for (a, b), n in table.items() if b in CONFIRMED and a != b),
            "left_confirmed": sum(n for (a, b), n in table.items() if a in CONFIRMED and a != b),
        }
    return out


def _criteria(rows) -> dict:
    scoped = [(r, e) for r, e in rows if r.final.country_scope.scoped]
    have = [(r, e) for r, e in scoped if "scope_represented" in e]
    if not scoped:
        return {"C1": {"status": NOT_ESTIMABLE, "reason": "국가 범위 기준이 GT에 없음"}}
    if len(have) < len(scoped):
        return {"C1": {"status": "미측정", "reason": "엔진 출력에 scope_represented가 없음 (파싱본 후처리 측정 필요)"}}
    return {"C1": _r("C1", sum(bool(e["scope_represented"]) for _, e in have), len(have),
                     "국가 범위 기준 중 파싱본이 범위를 표현하거나 판단 보류로 보낸 비율", "100%",
                     lambda o: o["numerator"] == o["denominator"])}


# ---------------------------------------------------------------------------

def evaluate(gt: GTSet, engine: dict, baseline: dict | None = None) -> dict:
    if engine.get("format") != ENGINE_FORMAT:
        raise ValueError(f"엔진 출력 형식이 {ENGINE_FORMAT}이 아닙니다")
    if engine.get("gt_set_id") != gt.set_id:
        raise ValueError(f"엔진 출력의 gt_set_id({engine.get('gt_set_id')})가 GT({gt.set_id})와 다릅니다")
    res = _results(gt, engine, "엔진 출력")
    labelled, amb, missing, extra = _split(gt, res)
    blockers = _coverage_blockers(gt, res, "엔진 출력")
    base_res = None
    if baseline is not None:
        if baseline.get("format") != ENGINE_FORMAT:
            raise ValueError(f"기준선 출력 형식이 {ENGINE_FORMAT}이 아닙니다")
        if baseline.get("gt_set_id") != gt.set_id:
            raise ValueError("기준선 출력의 gt_set_id가 GT와 다릅니다")
        base_res = _results(gt, baseline, "기준선 출력")
        blockers += _coverage_blockers(gt, base_res, "기준선 출력")
    report = {
        "gt_set_id": gt.set_id, "gt_version": gt.gt_version, "layer": gt.layer, "fixture_only": gt.fixture_only,
        "level": engine.get("level"), "engine": engine.get("engine"),
        "coverage": {"gt_items": len(gt.items), "labelled": len(labelled), "ambiguous": len(amb),
                     "pending_or_unlabelled": len(gt.items) - len(labelled) - len(amb) - len(missing),
                     "missing_engine_output": missing, "extra_engine_items": extra,
                     "complete": not missing and not extra},
        # 비어 있지 않으면 공식 결과가 될 수 없음 (gt_eval.run이 동결 조건과 합쳐 official을 정함)
        "official_blockers": blockers,
        "not_measured": dict(NOT_MEASURED),
    }
    if gt.fixture_only:
        report["warning"] = "FIXTURE_ONLY: 도구 시험용 가짜 데이터. 이 수치는 엔진 정확도와 무관합니다"
    if gt.layer == "G-U":
        conf = [(r, e) for r, e in labelled if r.input.kind == "conflict"]
        val = [(r, e) for r, e in labelled if r.input.kind == "value"]
        report["A"] = _conflict(conf, [(r, e) for r, e in amb if r.input.kind == "conflict"])
        report["B"] = _value(val, [(r, e) for r, e in amb if r.input.kind == "value"])
        if base_res is not None:
            report["paired"] = _unit_paired(labelled, base_res)
    elif gt.layer == "G-P":
        report["E"] = _pairs(labelled, amb, base_res)
        if "E3" not in report["E"]:
            report["not_measured"]["E3"] = "기준선(--baseline) 엔진 출력이 없음"
        if "C2" not in report["E"]:
            report["not_measured"]["C2-C4"] = "국가 격자(input.grid) 쌍이 없음"
    else:
        report["C"] = _criteria(labelled)
    return report
