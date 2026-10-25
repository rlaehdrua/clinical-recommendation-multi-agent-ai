"""Inspector 근거 직접성 진단 표시 (Phase 10-D-4). API 호출 없음: 손으로 만든 기록과 오프라인 대역 실행만 씀.

확인: 기준 상세 표시(evidence_directness·validation_flags·llm_judgment) / 라운드 변화 추적을 판정·근거 상태 수치와 분리 /
이전 형식(필드 없음) 호환 / strict 비노출 / Inspector가 새로 판정·검사하지 않음.
"""

from __future__ import annotations

import io
import json
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import render_inspector as ri  # noqa: E402

from ctrec import inspector  # noqa: E402

SECRET = "PATIENT-RAW-SECRET 혈소판 85 sk-ant-SECRETKEY"


def _a(rid, **k):
    return {"rule_id": rid, "kind": "inclusion", "criterion_text": "x", "method": "llm", **k}


def _done(tid, rnd, elig, items):
    return {"agent": "matcher", "event": "done", "trial_id": tid, "round": rnd, "eligibility": elig, "assessments": items}


def _rec(trace, final):
    return inspector.RunRecord(result={"patient_id": "P1", "qa_log": [],
                                       "matches": {"T1": {"eligibility": "UNCERTAIN", "assessments": final}}}, trace=trace)


HELD = dict(passes=None, holds="unknown", evidence_directness="hedged", validation_flags=["hedged_quote"],
            llm_judgment={"holds": "yes", "applicable": True, "evidence_type": "explicit", "confidence": "high"})


def test_detail_shows_recorded_directness_fields():
    v = inspector.build_view(_rec([], [_a("A", **HELD), _a("B", passes=True, holds="yes", evidence_directness="direct",
                                                          validation_flags=[], llm_judgment=None)]))
    a, b = v["trials"][0]["criteria"]
    assert (a["evidence_directness"], a["validation_flags"], a["llm_judgment"]) == \
        ("hedged", ["hedged_quote"], HELD["llm_judgment"])  # 기록 값 그대로 (다시 검사하지 않음)
    assert (b["evidence_directness"], b["validation_flags"], b["llm_judgment"]) == ("direct", [], None)
    assert v["overview"]["directness_final"] == {"direct": 1, "hedged": 1}
    page = inspector.render_html(v)
    for s in ("근거 직접성", "코드 검증 사유", "코드 검증 전 LLM 판정", "<code>hedged</code>", "추측·불확실 표현",
              "<code>hedged_quote</code>", "evidence_type=<code>explicit</code>", "<code>direct</code> <span class=\"mut\">직접 근거"):
        assert s in page, s
    # 기록된 LLM 원래 판정과 최종 해당 여부가 다르면 표시 (A만)
    assert page.count("최종 해당 여부") == 1


def test_directness_changes_are_tracked_separately_from_verdict_and_evidence():
    r0 = _a("A", passes=None, holds="unknown", gap="ask", evidence_directness="none", validation_flags=[],
            llm_judgment={"holds": "unknown"})
    r1 = _a("A", passes=None, holds="unknown", gap="ask", **{k: HELD[k] for k in inspector.DIRECTNESS_KEYS})
    r2 = _a("A", passes=True, holds="yes", gap=None, evidence_directness="direct", validation_flags=[],
            llm_judgment={"holds": "yes"})
    v = inspector.build_view(_rec([_done("T1", 0, "UNCERTAIN", [r0]), _done("T1", 1, "UNCERTAIN", [r1]),
                                   _done("T1", 2, "ELIGIBLE", [r2])], [r2]))
    ch = v["changes"]
    d = [(c["field"], c["from_round"], c["to_round"]) for c in ch["directness"]]
    assert sorted(d) == sorted([("evidence_directness", 0, 1), ("validation_flags", 0, 1), ("llm_judgment", 0, 1),
                                ("evidence_directness", 1, 2), ("validation_flags", 1, 2), ("llm_judgment", 1, 2)])
    assert {c["field"] for c in ch["verdict"]} == {"passes", "holds"} and {c["field"] for c in ch["evidence"]} == {"gap"}
    assert all(c["from_round"] == 1 for c in ch["verdict"] + ch["evidence"])  # 라운드 0->1은 직접성만 바뀜
    # 기존 수치(n_changes)는 그대로 4개 키, 직접성은 따로
    assert v["overview"]["n_changes"] == {"trial": 1, "verdict": 2, "evidence": 1, "incomparable": 0}
    assert v["overview"]["n_directness_changes"] == 6 and ch["n_criteria"]["directness"] == 1
    assert [x["directness_changes"] for x in v["timeline"]] == [[], ["T1:A"], ["T1:A"]]
    assert [x["verdict_changes"] for x in v["timeline"]] == [[], [], ["T1:A"]]
    c1 = next(c for c in ch["directness"] if c["field"] == "validation_flags" and c["to_round"] == 1)
    assert (c1["before"], c1["after"]) == ([], ["hedged_quote"])
    page = inspector.render_html(v)
    assert "⑤ 근거 직접성 진단 변화" in page and "바뀐 기준 1개, 변경 항목 6건" in page
    assert "근거 직접성 진단이 변경된 기준 1개" in page and "근거 직접성 바뀐 기준 1개" in page
    assert "라운드 사이 변경 6건" in page


def test_unchanged_directness_is_same_not_missing():
    r = _a("A", **HELD)
    v = inspector.build_view(_rec([_done("T1", 0, "UNCERTAIN", [r]), _done("T1", 1, "UNCERTAIN", [dict(r)])], [r]))
    assert v["changes"]["directness"] == [] and v["overview"]["n_directness_changes"] == 0
    assert "비교한 기준의 근거 직접성 진단 변화 없음" in inspector.render_html(v)


def test_legacy_records_without_fields_render_as_not_recorded():
    """필드가 없는 이전 형식: 'none'·0건으로 꾸미지 않고 '기록 없음'"""
    old = _a("A", passes=None, holds="unknown")
    v = inspector.build_view(_rec([_done("T1", 0, "UNCERTAIN", [old]), _done("T1", 1, "UNCERTAIN", [old])], [old]))
    c = v["trials"][0]["criteria"][0]
    assert not any(k in c for k in inspector.DIRECTNESS_KEYS)
    assert v["overview"]["n_directness_changes"] is None and v["overview"]["directness_final"] is None
    assert v["changes"]["incomparable"] == [] and v["anomalies"] == []  # 양쪽 다 없으면 비교 불가로 세지 않음
    assert any("trace 판정 기록에 근거 직접성 필드" in g for g in v["gaps"])
    assert any("이전 형식 결과" in g for g in v["gaps"])
    page = inspector.render_html(v)
    assert page.count('<span class="na">기록 없음</span>') >= 3 and "비교할 기록 없음" in page
    assert "0건이라는 뜻 아님" in page


def test_current_pipeline_shape_final_only():
    """현재 pipeline.trace_match는 직접성 필드를 trace에 싣지 않음: 최종 값만 보이고 라운드 비교는 '기록 없음'"""
    t = _a("A", passes=None, holds="unknown")
    v = inspector.build_view(_rec([_done("T1", 0, "UNCERTAIN", [t]), _done("T1", 1, "UNCERTAIN", [t])],
                                  [_a("A", **HELD)]))
    assert v["overview"]["directness_final"] == {"hedged": 1} and v["overview"]["n_directness_changes"] is None
    assert any("라운드 사이 직접성 변화는 비교할 수 없음" in g for g in v["gaps"])
    assert not any("이전 형식 결과" in g for g in v["gaps"])
    assert "trace 판정 기록에 직접성 필드가 없어" in inspector.render_html(v)


def test_field_present_in_only_one_record_is_incomparable():
    a = _a("A", passes=None, holds="unknown")
    b = _a("A", passes=None, holds="unknown", evidence_directness="hedged")
    v = inspector.build_view(_rec([_done("T1", 0, "UNCERTAIN", [a]), _done("T1", 1, "UNCERTAIN", [b])], [b]))
    assert [(c["field"], c["reason"]) for c in v["changes"]["incomparable"]] == \
        [("evidence_directness", "필드가 한쪽 기록에만 있음")]
    assert v["changes"]["directness"] == []


@pytest.mark.parametrize("mode", ["strict", "synthetic"])
def test_free_text_in_directness_fields_never_rendered(mode, tmp_path):
    bad = _a("A", passes=None, holds="unknown", evidence=SECRET, reasoning=SECRET, missing_info=SECRET,
             evidence_directness=SECRET, validation_flags=["hedged_quote", SECRET, 7],
             llm_judgment={"holds": SECRET, "evidence_type": "explicit", "reasoning": SECRET, "evidence": SECRET})
    malformed = _a("B", passes=None, holds="unknown", validation_flags=SECRET, llm_judgment=[SECRET])
    trace = [_done("T1", 0, "UNCERTAIN", [_a("A", evidence_directness="none", validation_flags=[], llm_judgment={}),
                                          _a("B", validation_flags=[], llm_judgment=None)]),
             _done("T1", 1, "UNCERTAIN", [bad, malformed])]
    rec = _rec(trace, [bad, malformed])
    if mode == "synthetic":
        rec.meta = {"synthetic": True, "llm_backend": "offline_stub"}
    v = inspector.build_view(rec, synthetic_details=mode == "synthetic")
    dumped, page = json.dumps(v, ensure_ascii=False, default=str), inspector.render_html(v)
    for s in ("PATIENT-RAW-SECRET", "혈소판 85", "sk-ant-SECRETKEY"):
        assert s not in dumped and s not in page, s
    a, b = v["trials"][0]["criteria"]
    assert a["evidence_directness"] == "<text>" and a["validation_flags"] == ["hedged_quote", "<text>", "<text>"]
    assert a["llm_judgment"] == {"holds": "<text>", "evidence_type": "explicit"}  # 허용 키만, 그 밖의 키는 읽지 않음
    assert b["validation_flags"] == "<text>" and b["llm_judgment"] == "<text>"
    assert "(서술 값, 표시 안 함)" in page


@pytest.fixture(scope="module")
def demo_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("insp_dir") / "run"
    with redirect_stdout(io.StringIO()):
        ri.run_offline_demo(ri.DEFAULT_CASE, d)
    return d


def test_real_offline_run_matches_result_json_and_hides_text(demo_dir):
    rec = inspector.load_run(demo_dir)
    v = inspector.build_view(rec)
    recorded = {(tid, a["rule_id"]): a for tid, m in rec.result["matches"].items() for a in m["assessments"]}
    for t in v["trials"]:
        for c in t["criteria"]:
            a = recorded[(t["trial_id"], c["rule_id"])]
            assert c["evidence_directness"] == a["evidence_directness"]  # 기록 그대로 (Inspector는 판정하지 않음)
            assert c["validation_flags"] == a["validation_flags"]
    counts: dict = {}
    for a in recorded.values():
        counts[a["evidence_directness"]] = counts.get(a["evidence_directness"], 0) + 1
    assert v["overview"]["directness_final"] == dict(sorted(counts.items()))
    assert v["overview"]["n_directness_changes"] is None  # 현재 trace에는 직접성 필드가 없음
    page = inspector.render_html(v)
    for a in recorded.values():  # 근거 문장은 strict 화면에 없음
        ev = a.get("evidence") or ""
        if len(ev) >= 12 and ev != "근거 없음":
            assert ev not in page


def test_inspector_does_not_import_judgment_modules():
    src = Path(inspector.__file__).read_text(encoding="utf-8")
    imports = " ".join(re.findall(r"^(?:from|import) .+$", src, flags=re.M))
    for mod in ("evidence_directness", "matcher", "qa_guard", "evaluator"):
        assert mod not in imports, mod
