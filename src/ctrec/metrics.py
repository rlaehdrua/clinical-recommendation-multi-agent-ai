"""평가 지표 계산 (eval/evaluate.py, scripts/budget_sweep.py 공용).

판정 지표
- accuracy: 전체 정확도 (UNCERTAIN·NOT_EVALUATED 예측은 오답)
- coverage: 판정 확정(ELIGIBLE/INELIGIBLE) 비율
- decided_accuracy: 확정 판정 중 정답 비율

안전·상호작용 지표 (정보 부족 ≠ 부적격)
- rescue: 서술만으로는 판정 보류(label_text_only=UNCERTAIN)였고 전체 기록상 적격(label=ELIGIBLE)인 쌍 중,
  ELIGIBLE로 예측한 비율. 숨은 정보를 얻어 실제 후보를 살려냈는가.
- cleanup: 서술만으로는 판정 보류였고 전체 기록상 부적격인 쌍 중, INELIGIBLE로 예측한 비율.
  숨은 정보를 얻어 실제 부적합 시험을 걸러냈는가.
- false_removal: 정답 ELIGIBLE인데 INELIGIBLE로 예측한 비율. 적격 후보를 잘못 제거한 오류.
- premature_match: 정답이 ELIGIBLE이 아닌데 ELIGIBLE로 예측한 비율. 근거 없이 서둘러 적격 판정한 오류.

분모가 0인 지표는 None으로 반환합니다(해당 사례가 평가셋에 없음).
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

CLASSES = ["ELIGIBLE", "INELIGIBLE", "UNCERTAIN", "NOT_EVALUATED"]
UNDECIDED = ("UNCERTAIN", "NOT_EVALUATED")  # NOT_EVALUATED: 파싱·매칭 실패로 판정하지 못함 (부적격 아님)


def load_labels(path: Path, column: str = "label") -> dict[tuple[str, str], str]:
    with open(path, encoding="utf-8") as f:
        return {(r["patient_id"], r["trial_id"]): r[column].strip().upper() for r in csv.DictReader(f)}


def load_predictions(outputs: Path) -> tuple[dict, dict, dict]:
    """outputs/<patient_id>/result.json -> (적격성 예측, 추천 순위, 최종 추천).

    후보 추림을 한 실행에서는 자세히 평가하지 않은 시험도 예측으로 셉니다.
    - 대상이 달라 제외한 시험(excluded) -> INELIGIBLE
    - 해당 가능하지만 상한을 넘어 평가하지 않은 시험(over_limit) -> UNCERTAIN (판정하지 않음)
    """
    elig, ranked, top = {}, {}, {}
    for result in outputs.glob("*/result.json"):
        data = json.loads(result.read_text(encoding="utf-8"))
        pid = data["patient_id"]
        sc = data.get("screening") or {}
        for s in sc.get("excluded", []):
            elig[(pid, s["trial_id"])] = "INELIGIBLE"
        for s in sc.get("over_limit", []):
            elig[(pid, s["trial_id"])] = "UNCERTAIN"
        for tid, m in data["matches"].items():
            elig[(pid, tid)] = m["eligibility"]
        rec = data.get("recommendation") or {}
        ranked[pid] = [r["trial_id"] for r in rec.get("ranked", [])]
        top[pid] = [r["trial_id"] for r in rec.get("recommended", [])] or ranked[pid][:1]
    return elig, ranked, top


def load_usage(outputs: Path) -> dict:
    """환자별 result.json을 합산: LLM 호출·토큰, 질문 수(사용자 부담)."""
    total = Counter()
    n_patients = 0
    for result in outputs.glob("*/result.json"):
        data = json.loads(result.read_text(encoding="utf-8"))
        n_patients += 1
        for k, v in (data.get("usage") or {}).items():
            total[k] += v
        qa = data.get("qa_log") or []
        total["questions"] += len(qa)
        total["eligibility_questions"] += sum(q.get("purpose", "eligibility") == "eligibility" for q in qa)
    out = dict(total)
    out["patients"] = n_patients
    return out


def _rate(num: int, den: int) -> float | None:
    return num / den if den else None


def eligibility_metrics(gold: dict, pred: dict, text_only: dict | None = None) -> dict:
    """gold: 비교할 정답 라벨, text_only: label_text_only(있으면 rescue/cleanup 계산)."""
    pairs = [k for k in gold if k in pred]
    decided = [k for k in pairs if pred[k] not in UNDECIDED]
    gold_elig = [k for k in pairs if gold[k] == "ELIGIBLE"]
    gold_not_elig = [k for k in pairs if gold[k] != "ELIGIBLE"]
    m = {
        "pairs": len(pairs),
        "missing_predictions": sum(k not in pred for k in gold),
        "accuracy": _rate(sum(gold[k] == pred[k] for k in pairs), len(pairs)),
        "coverage": _rate(len(decided), len(pairs)),
        "decided_accuracy": _rate(sum(gold[k] == pred[k] for k in decided), len(decided)),
        "false_removal": _rate(sum(pred[k] == "INELIGIBLE" for k in gold_elig), len(gold_elig)),
        "false_removal_n": f"{sum(pred[k] == 'INELIGIBLE' for k in gold_elig)}/{len(gold_elig)}",
        "premature_match": _rate(sum(pred[k] == "ELIGIBLE" for k in gold_not_elig), len(gold_not_elig)),
        "premature_match_n": f"{sum(pred[k] == 'ELIGIBLE' for k in gold_not_elig)}/{len(gold_not_elig)}",
        "confusion": Counter((gold[k], pred[k]) for k in pairs),
    }
    if text_only is not None:
        hidden = [k for k in pairs if text_only.get(k) == "UNCERTAIN"]
        rescue = [k for k in hidden if gold[k] == "ELIGIBLE"]
        cleanup = [k for k in hidden if gold[k] == "INELIGIBLE"]
        m["rescue"] = _rate(sum(pred[k] == "ELIGIBLE" for k in rescue), len(rescue))
        m["rescue_n"] = f"{sum(pred[k] == 'ELIGIBLE' for k in rescue)}/{len(rescue)}"
        m["cleanup"] = _rate(sum(pred[k] == "INELIGIBLE" for k in cleanup), len(cleanup))
        m["cleanup_n"] = f"{sum(pred[k] == 'INELIGIBLE' for k in cleanup)}/{len(cleanup)}"
    return m


def fmt(v: float | None) -> str:
    return "N/A" if v is None else f"{v:.3f}"


# ---------------------------------------------------------------------------
# 정보 가리기 평가셋: 숨긴 사실을 질문으로 되찾았는가
# ---------------------------------------------------------------------------

def load_label_rows(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_reveals(outputs: Path) -> dict[str, list[dict]]:
    """outputs/<patient_id>/result.json의 revealed_facts (질문별 공개 사실 ID)."""
    out = {}
    for result in outputs.glob("*/result.json"):
        data = json.loads(result.read_text(encoding="utf-8"))
        out[data["patient_id"]] = data.get("revealed_facts") or []
    return out


def fact_acquisition_metrics(rows: list[dict], reveals: dict[str, list[dict]]) -> dict | None:
    """masked_fact_ids 열이 있는 라벨에서 계산. 결과가 있는 '가린 환자'만 대상.

    - masked_fact_recovery: 숨긴 사실 중 질문으로 공개된 비율 (환자 평균)
    - full_recovery: 숨긴 사실을 모두 되찾은 환자 비율
    - useful_question_rate: 적격성 질문 중 숨긴 사실을 하나 이상 공개시킨 질문 비율
    - questions_per_masked_fact: 숨긴 사실 1개를 되찾는 데 쓴 적격성 질문 수
    """
    masked = {r["patient_id"]: set(filter(None, r.get("masked_fact_ids", "").split(";"))) for r in rows}
    masked = {pid: ids for pid, ids in masked.items() if ids and pid in reveals}
    if not masked:
        return None
    recovery, full, useful, n_q, n_found = [], [], 0, 0, 0
    for pid, ids in masked.items():
        log = reveals[pid]
        got = {i for q in log for i in q.get("fact_ids", [])} & ids
        recovery.append(len(got) / len(ids))
        full.append(got == ids)
        n_found += len(got)
        for q in log:
            if q.get("purpose", "eligibility") == "eligibility":
                n_q += 1
                useful += bool(set(q.get("fact_ids", [])) & ids)
    return {
        "masked_patients": len(masked),
        "masked_fact_recovery": sum(recovery) / len(recovery),
        "full_recovery": sum(full) / len(full),
        "useful_question_rate": _rate(useful, n_q),
        "questions_per_masked_fact": _rate(n_q, n_found),
    }


def accuracy_by_group(gold: dict, pred: dict, rows: list[dict], column: str = "case_type") -> dict[str, str]:
    groups = defaultdict(list)
    for r in rows:
        k = (r["patient_id"], r["trial_id"])
        if k in pred and r.get(column):
            groups[r[column]].append(gold[k] == pred[k])
    return {g: f"{sum(v) / len(v):.3f} ({sum(v)}/{len(v)})" for g, v in sorted(groups.items())}


# ---------------------------------------------------------------------------
# 후보 추림: 맞는 시험을 놓치지 않았는가
# ---------------------------------------------------------------------------

def screening_metrics(rows: list[dict], outputs: Path) -> dict | None:
    """result.json의 screening과 라벨(auto 열: 1=다른 분야 자동 라벨)로 계산.

    - eligible_recall: 정답 ELIGIBLE 시험 중 추림에서 선택된 비율 (가장 중요. 놓치면 이후 단계에서 되찾을 수 없음)
    - same_topic_recall: 사람이 정했던 같은 분야 후보 중 선택된 비율
    - off_topic_per_patient: 다른 분야(자동 라벨) 시험을 선택한 수 (환자 평균, 낭비된 자세한 평가)
    - selected_per_patient / pool_size: 자세히 평가한 시험 수 / 전체 풀 크기
    """
    by_pid = {}
    for result in outputs.glob("*/result.json"):
        data = json.loads(result.read_text(encoding="utf-8"))
        if data.get("screening"):
            by_pid[data["patient_id"]] = data["screening"]
    if not by_pid:
        return None
    elig_hit = elig_n = topic_hit = topic_n = off = sel = pool = 0
    missed = []
    for r in rows:
        sc = by_pid.get(r["patient_id"])
        if sc is None or r.get("auto") == "1":
            continue
        chosen = {s["trial_id"] for s in sc["selected"]}
        topic_n += 1
        topic_hit += r["trial_id"] in chosen
        if r["label"].upper() == "ELIGIBLE":
            elig_n += 1
            if r["trial_id"] in chosen:
                elig_hit += 1
            else:
                missed.append(f"{r['patient_id']}/{r['trial_id']}")
    auto = {(r["patient_id"], r["trial_id"]) for r in rows if r.get("auto") == "1"}
    for pid, sc in by_pid.items():
        sel += len(sc["selected"])
        pool += sc["pool_size"]
        off += sum((pid, s["trial_id"]) in auto for s in sc["selected"])
    n = len(by_pid)
    return {
        "patients": n,
        "eligible_recall": _rate(elig_hit, elig_n),
        "eligible_recall_n": f"{elig_hit}/{elig_n}",
        "same_topic_recall": _rate(topic_hit, topic_n),
        "off_topic_per_patient": off / n,
        "selected_per_patient": sel / n,
        "pool_size": pool / n,
        "missed_eligible": missed,
    }
