"""어댑터는 라벨을 받지 않습니다. 엔진 함수에 넘어가는 인자에 라벨·판정·출처 정보가 없는지 확인합니다."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gt_engine_adapter as ad  # noqa: E402
import gt_eval  # noqa: E402
from gt_lib.models import UtteranceInput  # noqa: E402

FX = ROOT / "tests" / "data" / "gt_fixture" / "FIXTURE_ONLY_g_u.json"


def test_ctrec_is_worktree_copy():
    import ctrec
    assert Path(ctrec.__file__).resolve().is_relative_to(ROOT / "src")


def _label_strings(raw: dict) -> set[str]:
    """라벨 쪽 문자열 전부 (annotations, final, adjudication, needs_expert, source)."""
    out = set()

    def walk(o):
        if isinstance(o, str):
            out.add(o)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    for it in raw["items"]:
        for k in ("annotations", "final", "adjudication", "needs_expert", "source"):
            walk(it.get(k))
    inputs = set()
    for it in raw["items"]:
        inputs |= {v for v in it["input"].values() if isinstance(v, str)}
    # 짧은 열거값(hold, accept 등)은 키 이름(holds, accepted)과 겹치므로 빼고, 구별되는 문자열만 봅니다
    return {s for s in out if s not in inputs and len(s) >= 8}


def test_engine_functions_never_receive_labels(monkeypatch):
    from ctrec import evidence_directness as ed, value_grounding as vg

    raw = json.loads(FX.read_text(encoding="utf-8"))
    secrets = _label_strings(raw)
    assert any("SENTINEL" in s for s in secrets)
    seen = []
    real_check, real_ground, real_lab = ed.check, vg.ground, vg.ground_lab

    def spy(real):
        def f(*a, **k):
            seen.append(repr(a) + repr(k))
            return real(*a, **k)
        return f
    monkeypatch.setattr(ed, "check", spy(real_check))
    monkeypatch.setattr(vg, "ground", spy(real_ground))
    monkeypatch.setattr(vg, "ground_lab", spy(real_lab))
    out = ad.build_output(FX, "2026-11-07", engine_commit="test")
    assert len(seen) == len(raw["items"])
    blob = "\n".join(seen) + json.dumps(out, ensure_ascii=False)
    leaked = [s for s in secrets if s in blob]
    assert leaked == [], f"라벨 문자열이 엔진 쪽에 닿음: {leaked}"
    assert "SENTINEL" not in blob


def test_blind_inputs_drops_everything_but_input():
    raw = json.loads(FX.read_text(encoding="utf-8"))
    pairs = ad.blind_inputs(raw)
    assert [i for i, _ in pairs] == [it["item_id"] for it in raw["items"]]
    for _, inp in pairs:
        assert isinstance(inp, UtteranceInput)
        assert set(inp.model_dump()) == set(UtteranceInput.model_fields)
    assert not {"decision", "conflict", "hearsay", "category", "final", "annotations"} & set(UtteranceInput.model_fields)


def test_run_units_rejects_records_and_dicts():
    raw = json.loads(FX.read_text(encoding="utf-8"))
    with pytest.raises(TypeError):
        ad.run_units([(raw["items"][0]["item_id"], raw["items"][0])], "2026-11-07")
    with pytest.raises(TypeError):
        ad.run_units([("x", raw["items"][0]["input"])], "2026-11-07")


def test_label_in_input_is_rejected():
    raw = json.loads(FX.read_text(encoding="utf-8"))
    raw["items"][0]["input"]["expected"] = "hold"  # 세트 6 방식처럼 입력 옆에 정답을 두면 거부
    with pytest.raises(Exception):
        ad.blind_inputs(raw)


def test_non_unit_layers_refused():
    raw = json.loads((FX.parent / "FIXTURE_ONLY_g_p.json").read_text(encoding="utf-8"))
    with pytest.raises(ValueError):
        ad.blind_inputs(raw)


def test_adapter_output_round_trips_into_eval(tmp_path):
    out = tmp_path / "engine.json"
    assert ad.main([str(FX), "--out", str(out), "--engine-commit", "test"]) == 0
    o = json.loads(out.read_text(encoding="utf-8"))
    assert o["format"] == "gt-engine-output/1" and o["level"] == "U"
    assert Path(o["engine"]["ctrec_file"]).resolve().is_relative_to(ROOT / "src")
    rep = gt_eval.run(FX, out)
    assert rep["coverage"]["missing_engine_output"] == [] and rep["fixture_only"] is True
    assert rep["warning"].startswith("FIXTURE_ONLY")


def test_reference_date_restored():
    from ctrec import config
    before = config.REFERENCE_DATE
    ad.build_output(FX, "2030-01-01", engine_commit="test")
    assert config.REFERENCE_DATE == before
