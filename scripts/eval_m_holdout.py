"""held-out 6 평가 (코드 고정 후 1회). 사용: PYTHONPATH=src python scripts/eval_m_holdout.py docs/dev [-v]
- labs: ground/ground_lab 결과 None = 채택. 부정은 보류여야, 정상은 채택이어야.
- conflict: evidence_directness.check(criterion, quote, text, holds="no"). conflicting = 상충 탐지.
  matcher가 확정을 막는 결과(direct/unchecked 밖)도 함께 셈.
- window: rule_engine.evaluate(기간 규칙, 프로필(검사일 = 항목의 lab_date)). 추출기가 날짜를 정확히 옮겼다고 가정한 날짜 계산·
  추정 금지 검사. 날짜 추출 자체는 평가하지 않음.
"""
import collections
import json
import sys
from pathlib import Path

from ctrec import config, evidence_directness as ed, value_grounding as vg
from ctrec.schemas import LabValue, PatientProfile, Rule
from ctrec.tools import rule_engine

config.REFERENCE_DATE = "2026-11-07"
root = Path(sys.argv[1])
verbose = "-v" in sys.argv


def labs():
    d = json.loads((root / "m_holdout_6_labs.json").read_text(encoding="utf-8"))
    for group in ("negative", "positive"):
        wrong, tot, bad = collections.Counter(), collections.Counter(), []
        for it in d[group]:
            f, v, u, t = it["field"], it["value"], it.get("unit"), it["text"]
            r = vg.ground(f, v, t, unit=u) if f in ("age", "sex", "ecog") else vg.ground_lab(f, v, t, t, unit=u)
            accepted = r is None
            w = accepted if group == "negative" else not accepted
            tot[it["category"]] += 1
            if w:
                wrong[it["category"]] += 1
                bad.append((it["category"], r, t))
        label = "leak(accepted)" if group == "negative" else "held"
        print(f"labs {group}: {label} {sum(wrong.values())}/{sum(tot.values())}", {k: f"{wrong[k]}/{tot[k]}" for k in tot})
        if verbose:
            for b in bad:
                print("   ", b[0], b[1], "|", b[2][:170].replace("\n", " / "))


def conflicts():
    d = json.loads((root / "m_holdout_6_cases.json").read_text(encoding="utf-8"))["conflict"]
    res = collections.Counter()
    bad = []
    for it in d:
        c = ed.check(it["criterion"], it["quote"], it["text"], holds="no")
        detected = c.directness == "conflicting"
        blocks = c.directness not in ("direct", "unchecked")
        res[(it["expected"], "conflicting" if detected else ("blocked_other" if blocks else "accepted"))] += 1
        if (it["expected"] == "hold") != detected:
            bad.append((it["expected"], it["category"], c.directness, c.flags, it["text"]))
    hold = [k for k in res if k[0] == "hold"]
    acc = [k for k in res if k[0] == "accept"]
    print("conflict hold(15):", {k[1]: res[k] for k in hold}, "| accept(15):", {k[1]: res[k] for k in acc})
    if verbose:
        for b in bad:
            print("   ", b[:4], "|", b[4][:170])


def windows():
    d = json.loads((root / "m_holdout_6_cases.json").read_text(encoding="utf-8"))["window"]
    res = collections.Counter()
    bad = []
    for it in d:
        rule = Rule(rule_id="I1", kind="inclusion", text=f"within {it['window_days']} days: {it['field']} >= 0",
                    category="lab", field=f"lab:{it['field']}", operator=">=", value="0", unit=it.get("unit"),
                    time_window_days=it["window_days"], structured_evaluable=True, underspecified=False, cohort=None)
        prof = PatientProfile(patient_id="T", age=60, sex="male", primary_diagnosis=None, disease_stage=None, diagnoses=[],
                              biomarkers=[], labs=[LabValue(name=it["field"], value=it["value"], unit=it.get("unit"),
                                                            date=it["lab_date"], source_quote=it["text"][:60])],
                              medications=[], treatment_history=[], ecog=None, other_facts=[], missing_or_ambiguous=[])
        a = rule_engine.evaluate(rule, prof, it["reference_date"]) if "reference_date" in rule_engine.evaluate.__code__.co_varnames \
            else rule_engine.evaluate(rule, prof)
        got = "delegated_to_llm" if a is None else ("inside" if a.passes is not None else
                                                     ("outside" if "lab_window_outside" in (a.validation_flags or []) else "undetermined"))
        res[(it["expected"], got)] += 1
        if got != it["expected"]:
            bad.append((it["expected"], got, it["category"], it["text"]))
    print("window:", dict(sorted(res.items())))
    if verbose:
        for b in bad:
            print("   ", b[:3], "|", b[3][:150])


labs()
conflicts()
windows()
