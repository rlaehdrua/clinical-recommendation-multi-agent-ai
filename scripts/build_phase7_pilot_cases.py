"""Phase 7 파일럿 보강 합성 사례 생성. LLM·네트워크 없이 기존 합성 사례(F-SYN-DEMO-A) 원문을 결정적으로 바꿔 만듭니다.

기존 합성 사례 집합에는 "환자 정보가 서로 충돌하는 사례"와 "단위 환산이 판정을 가르는 사례"가 없어서(Phase 7 조사),
같은 합성 환자에 문장 한 줄만 더하거나 바꾼 대조 사례를 만듭니다. 원본과 한 줄만 다르므로, 결과가 원본(F-SYN-DEMO-A, 정답
ELIGIBLE)과 다르면 그 차이는 그 한 줄 때문입니다.

- P7-SYN-DEMO-A-conflict-ecog : 다른 병원 의뢰서에 ECOG 2 (원문 ECOG 1). 기준 ECOG 0-1 -> 확정할 수 없음 (UNCERTAIN 기대)
- P7-SYN-DEMO-A-conflict-plt  : 같은 날 다른 검사실 혈소판 85 (원문 185). 기준 >=100 -> 확정할 수 없음 (UNCERTAIN 기대)
- P7-SYN-DEMO-A-units-hb      : 혈색소를 g/L로만 표기 (88 g/L = 8.8 g/dL). 기준 >= 9.0 g/dL -> 불충족 (INELIGIBLE 기대)
  규칙 엔진은 g/L와 g/dL를 환산하지 않고 LLM Matcher에 넘기므로(tools/rule_engine._same_scale), 실제 모델의 단위 처리를 봄

기대 판정은 연구진이 정한 평가 기준이며(아래 EXPECT), 의료적 결론이 아닙니다. 실제 환자 정보는 쓰지 않습니다.

    PYTHONPATH=src python scripts/build_phase7_pilot_cases.py          # eval/pilot/에 쓰기
    PYTHONPATH=src python scripts/build_phase7_pilot_cases.py --check  # 파일이 생성 결과와 같은지만 확인
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path

from ctrec import config

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "eval" / "pilot"
CASES = OUT / "phase7_cases.json"
LABELS = OUT / "phase7_labels.csv"
SOURCE_FILE = config.DATA_DIR / "patients" / "masked" / "masked_cases.json"
SOURCE_ID = "F-SYN-DEMO-A"
TRIAL = "DEMO-LUNG-001"

# (사례 ID, 유형, 바꿀 원문 줄 또는 None(추가), 새 줄, 기대 판정, 확인할 규칙, 목적)
VARIANTS = [
    ("P7-SYN-DEMO-A-conflict-ecog", "E", None,
     "- A 2026-09 referral letter from another hospital records ECOG performance status 2.",
     "UNCERTAIN", ["I6"],
     "ECOG 기록 충돌(1 vs 2): 한쪽을 임의로 골라 적합(ELIGIBLE)으로 확정하지 않는지, 충돌을 질문·확인 대상으로 남기는지"),
    ("P7-SYN-DEMO-A-conflict-plt", "E", None,
     "- On 2026-09-10, an outside laboratory reported PLT 85 x10^9/L.",
     "UNCERTAIN", ["I8"],
     "같은 날 혈소판 두 값(185 vs 85): 규칙 엔진은 최신 값을 정할 수 없어 LLM에 넘김. 임의로 한 값을 골라 확정하지 않는지"),
    ("P7-SYN-DEMO-A-units-hb", "G", "- On 2026-09-10, Hb was 11.2 g/dL.",
     "- On 2026-09-10, Hb was 88 g/L.",
     "INELIGIBLE", ["I9"],
     "단위 환산(88 g/L = 8.8 g/dL < 9.0 g/dL): 숫자만 비교해 충족(88 >= 9)으로 보지 않는지"),
]


def build() -> tuple[list[dict], str]:
    data = json.loads(SOURCE_FILE.read_text(encoding="utf-8"))
    src = next(c for c in data if c["patient_id"] == SOURCE_ID)
    if not src.get("synthetic"):
        raise SystemExit("원본이 합성 사례가 아님")
    lines = src["text"].splitlines()
    cases, rows = [], []
    for cid, ctype, old, new, label, rules, purpose in VARIANTS:
        if old is None:
            text = "\n".join(lines + [new])
        else:
            if lines.count(old) != 1:
                raise SystemExit(f"원본에서 바꿀 줄을 찾지 못함: {cid}")
            text = "\n".join(new if ln == old else ln for ln in lines)
        cases.append({"patient_id": cid, "text": text, "hidden_facts": [], "masked_fact_ids": [], "clue_fact_ids": [],
                      "masked_criteria": [], "case_type": "full", "source_patient": src["source_patient"],
                      "candidate_trials": [TRIAL], "synthetic": True,
                      "phase7": {"type": ctype, "derived_from": SOURCE_ID, "changed_line": new,
                                 "expected_eligibility": {TRIAL: label}, "check_rules": [f"{TRIAL}:{r}" for r in rules],
                                 "purpose": purpose}})
        rows.append({"patient_id": cid, "trial_id": TRIAL, "label": label})
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["patient_id", "trial_id", "label"], lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return cases, buf.getvalue()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    cases, labels = build()
    text = json.dumps(cases, ensure_ascii=False, indent=1) + "\n"
    if args.check:
        same = CASES.read_text(encoding="utf-8") == text and LABELS.read_text(encoding="utf-8") == labels
        print(json.dumps({"up_to_date": same}))
        return 0 if same else 1
    OUT.mkdir(parents=True, exist_ok=True)
    CASES.write_text(text, encoding="utf-8", newline="\n")
    LABELS.write_text(labels, encoding="utf-8", newline="\n")
    print(json.dumps({"cases": [c["patient_id"] for c in cases], "written": [str(CASES.relative_to(ROOT)),
                                                                           str(LABELS.relative_to(ROOT))]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
