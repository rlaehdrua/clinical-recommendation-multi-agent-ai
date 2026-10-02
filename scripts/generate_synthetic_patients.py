"""가상(synthetic) 환자 데이터 생성 - 과제 안내의 '모델 개발 시 가상 데이터 생성 권장'에 대응.

지정한 시험의 기준을 바탕으로 목표 라벨(ELIGIBLE / INELIGIBLE / UNCERTAIN)을 갖는 가상 환자를 만들고,
정답 라벨 CSV에 행을 추가합니다. 실존 인물 정보는 사용하지 않습니다.

    python scripts/generate_synthetic_patients.py --trial data/trials/NCT01234567.json --n 3

- text: 파이프라인에 입력되는 공개 정보 (UNCERTAIN 목표일 때는 일부 핵심 정보를 의도적으로 누락)
- hidden_details: 누락된 정보를 포함한 전체 기록 (SimulatedPatientAnswerer만 사용)
- 목표 라벨은 '생성 의도'일 뿐이므로, 평가 전 연구진이 검토·수정해야 합니다.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from ctrec import config, llm
from ctrec.tools import ctgov


class SyntheticPatient(BaseModel):
    text: str
    hidden_details: str
    design_note: str


SYSTEM = """당신은 임상시험 매칭 시스템 개발을 위한 가상 환자 기록을 만드는 의료 데이터 설계자입니다.
- 실존 인물·병원·식별 정보를 절대 사용하지 않습니다. 이름 대신 ID만 씁니다.
- 기록은 한국 병원 경과기록 스타일(한국어+영문 의학 용어 혼용)로 인구학 정보, 진단/병기, 바이오마커, 치료 이력, 최근 검사 결과(날짜 포함), 투약, 동반 질환, 수행 상태를 포함합니다.
- 목표 라벨에 맞게 설계합니다.
  * ELIGIBLE: 모든 선정 기준 충족, 제외 기준 비해당이 기록에서 확인 가능
  * INELIGIBLE: 하나 이상의 기준을 명확히 위반(자연스럽게, 너무 노골적이지 않게)
  * UNCERTAIN: 전체 기록은 적격이지만 text에서는 1-3개 핵심 정보를 누락. 누락 정보는 hidden_details에만 둠
- hidden_details에는 text의 모든 내용 + 누락된 세부 정보를 담습니다.
- design_note에는 어떤 기준을 어떻게 설계했는지 적습니다(평가 검토용)."""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trial", required=True, help="시험 JSON 경로")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--out-dir", default=str(config.DATA_DIR / "patients" / "synthetic"))
    ap.add_argument("--labels", default=str(config.DATA_DIR / "labels" / "synthetic_labels.csv"))
    args = ap.parse_args()

    trial = ctgov.load_local(Path(args.trial))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    labels_path = Path(args.labels)
    labels_path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not labels_path.exists()

    targets: list[Literal["ELIGIBLE", "INELIGIBLE", "UNCERTAIN"]] = ["ELIGIBLE", "INELIGIBLE", "UNCERTAIN"]
    existing = len(list(out_dir.glob("SYN-*.json")))
    with labels_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(["patient_id", "trial_id", "label"])
        for i in range(args.n):
            label = targets[i % 3]
            pid = f"SYN-{existing + i + 1:03d}"
            p = llm.structured(
                SyntheticPatient,
                system=SYSTEM,
                user=f"""<trial>
{trial['trial_id']}: {trial['title']}
{trial['eligibility_criteria']}
</trial>
목표 라벨: {label}
환자 ID: {pid}""",
                effort="medium",
            )
            record = {
                "patient_id": pid,
                "synthetic": True,
                "text": p.text,
                "hidden_details": p.hidden_details,
                "labels": {"target_trial": trial["trial_id"], "intended_label": label, "design_note": p.design_note},
            }
            (out_dir / f"{pid}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            # UNCERTAIN은 hidden_details까지 반영하면 적격이므로, 대화 후 정답은 ELIGIBLE
            writer.writerow([pid, trial["trial_id"], "ELIGIBLE" if label == "UNCERTAIN" else label])
            print(f"{pid}: intended={label}")


if __name__ == "__main__":
    main()
