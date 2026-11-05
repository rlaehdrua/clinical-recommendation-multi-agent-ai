"""역전 지표 출력의 식별자: 파일과 기본 dict에는 원시 run_id·환자 별칭·시험 ID가 남지 않음 (합성 값)."""

from __future__ import annotations

import json

from ctrec.engine import reversal_metrics as rm
from ctrec.engine.contracts import PairStatus

RUN, ALIAS, TRIAL = "RUN-SYNSECRET1", "PA-SYNSECRET2", "NCT-SYNSECRET3"


def summary() -> rm.ReversalSummary:
    e = rm.ReversalEvent(run_id=RUN, patient_alias=ALIAS, trial_id=TRIAL, before=PairStatus.CONFIRMED_ELIGIBLE,
                         after=PairStatus.CONFIRMED_INELIGIBLE, anchor_step=1, step=3, via=("VERIFICATION_PENDING",),
                         new_evidence=True, verified=True)
    return rm.ReversalSummary(runs=1, pairs_observed=1, confirmed_eligible_to_ineligible=1, events=[e])


def test_default_dict_has_no_raw_identifiers():
    d = rm.ReversalSummary.to_dict(summary())
    text = json.dumps(d, ensure_ascii=False, default=str)
    assert "SYNSECRET" not in text
    event = d["events"][0]
    assert not {"run_id", "patient_alias", "trial_id"} & set(event)
    assert event["direction"] == "eligible_to_ineligible" and event["step"] == 3 and event["path_kind"] == "verification_only"


def test_same_pair_keeps_the_same_reference():
    a, b = summary().to_dict()["events"][0], summary().to_dict()["events"][0]
    assert a["pair_ref"] == b["pair_ref"] and len(a["pair_ref"]) == 12


def test_identifiers_only_on_explicit_request():
    event = summary().to_dict(identifiers=True)["events"][0]
    assert (event["run_id"], event["patient_alias"], event["trial_id"]) == (RUN, ALIAS, TRIAL)


def test_metrics_file_has_no_raw_identifiers_or_folder_names(tmp_path):
    out = tmp_path / "metrics.json"
    rm.write_metrics(summary(), out, [f"{RUN}:no_v2_records", f"{RUN}:records_unreadable", "other:no_v2_records"])
    text = out.read_text(encoding="utf-8")
    assert "SYNSECRET" not in text
    assert json.loads(text)["load_problems"] == ["run-1:no_v2_records", "run-1:records_unreadable", "run-2:no_v2_records"]
