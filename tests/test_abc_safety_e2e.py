"""A(임상의 판단·환자값 근거) + B(서버 문서 게이트) + C(출력 경계)를 한 Session과 WP6에서 함께 검사. 합성 값, LLM·API 호출 없음.

각 층을 따로 시험하는 파일은 따로 있습니다. 여기서는 같은 실행 안에서 세 경계가 겹칠 때의 결과만 봅니다.
- 실제 Session.run_fixed, save, 웹 result와 같은 호출 경로, OfflineEvaluator가 같은 환자·같은 규칙을 받습니다.
- 값은 "운영 권한"(NoDocumentAuthority)과 "합성 문서 증명"(대조군) 두 조건으로 봅니다.
  conftest의 합성 권한은 대조군 전용이고, 운영 조건은 별도 프로세스에서도 확인합니다 (test_production_process_*).
- 실제 LLM 문장은 없습니다. 판정 LLM 자리는 고정 대역입니다.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType

import pytest

pytest.importorskip("ctrec.sim.evaluator")

sys.path.insert(0, str(Path(__file__).parent))

import test_night2_status_boundary as night  # noqa: E402
from document_support import AUTHORITY  # noqa: E402
from night_consistency_support import AGE, ALIAS, ECOG, cohort_of, lab, parsed, rule, trial  # noqa: E402

from ctrec import cli, conservative_view as cv, document_authority  # noqa: E402
from ctrec.document_authority import NoDocumentAuthority  # noqa: E402
from ctrec.engine.cohort_state import CohortState  # noqa: E402
from ctrec.orchestrator import Orchestrator  # noqa: E402
from ctrec.schemas import LLMAssessment, RuleMeta  # noqa: E402
from ctrec.sim.cohort import SimFact, SimTrial  # noqa: E402
from ctrec.sim.evaluator import OfflineEvaluator, make_fact  # noqa: E402
from ctrec.web import app as web, display  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REF = "2026-10-12"
HB = lab("HB", "hemoglobin", "9")
HB_HIGH = lab("HI", "hemoglobin", "50")
OLDER = AGE.model_copy(update={"rule_id": "AGE99", "text": "Age >= 99 years", "value": "99"})
VAGUE = rule("VG", "Adequate organ function per investigator", underspecified=True)
HISTORY = rule("OT", "Synthetic history item")
VAGUE_YES = {"VG": ("yes", True, "62-year-old")}

# 한 Session에 세 경계와 대조군을 모두 넣음. 시험마다 다른 경계가 걸림
TRIALS = {"SELF": [AGE], "DOC": [HB, AGE], "EXPERT": [VAGUE, AGE], "VALUE": [ECOG, AGE], "OUT": [OLDER, HB], "ASK": [HISTORY, AGE]}
RECORD = "Hb 10 g/dL on 2026-09-01."




@pytest.fixture
def production():
    with document_authority.use(NoDocumentAuthority()):
        yield


@pytest.fixture
def unstated_ecog(monkeypatch):
    real = night.profiler
    monkeypatch.setattr(night, "profiler", lambda pid, raw, reference_date=None: _with_ecog(real, pid, raw, reference_date))


def _with_ecog(real, pid, raw, reference_date):
    p = real(pid, raw, reference_date)
    p.ecog = 1
    return p


def outcome(s, tid="T"):
    v = cv.build(s)
    return s.matches[tid].eligibility, cohort_of(s).pair(ALIAS, tid).status.value, v.engine[tid]["shown"]


def run(tmp_path, trials, raw, **kw):
    kw.setdefault("rounds", 1)
    return night.Run(tmp_path, trials, raw, verdicts=kw.pop("verdicts", VAGUE_YES), **kw)


# ------------------------------------------------------------ 한 Session에서 동시에
def test_three_boundaries_hold_in_one_production_session(tmp_path, production, unstated_ecog):
    with run(tmp_path, TRIALS, RECORD, documented=False) as r:
        got = {t: outcome(r.s, t) for t in TRIALS}
        ranked = {x.trial_id: x.match_status for x in r.s.recommendation.ranked}
    assert got["SELF"] == ("ELIGIBLE", "CONFIRMED_ELIGIBLE", "CRITERIA_MATCH")  # 정상 대조군
    assert got["DOC"] == ("UNCERTAIN", "VERIFICATION_PENDING", "POTENTIAL_MATCH")  # B
    assert got["EXPERT"] == ("UNCERTAIN", "EXPERT_REVIEW", "NEEDS_REVIEW")  # A 임상의
    assert got["VALUE"][0] == "UNCERTAIN" and got["VALUE"][1] != "CONFIRMED_ELIGIBLE"  # A 환자값
    # 명백한 불충족: 판정은 그대로, 운영 권한에서는 근거 문서가 없어 엔진 미확정 -> 확정 부적격으로 보이지 않음 (P1-4)
    assert got["OUT"] == ("INELIGIBLE", "VERIFICATION_PENDING", "INSUFFICIENT_INFORMATION")
    assert got["ASK"][0] == "UNCERTAIN" and got["ASK"][2] == "INSUFFICIENT_INFORMATION"
    assert [t for t, st in ranked.items() if st == "CRITERIA_MATCH"] == ["SELF"]  # 추천의 확정 그룹은 대조군뿐
    assert "OUT" not in ranked


def test_documents_release_only_the_document_boundary(tmp_path, unstated_ecog):
    """같은 Session 구성에 합성 문서 증명만 더함: 문서 기준은 풀리고, 임상의·환자값 경계는 그대로."""
    with run(tmp_path, TRIALS, RECORD) as r:
        got = {t: outcome(r.s, t) for t in TRIALS}
    assert got["DOC"] == ("ELIGIBLE", "CONFIRMED_ELIGIBLE", "CRITERIA_MATCH")
    assert got["OUT"] == ("INELIGIBLE", "CONFIRMED_INELIGIBLE", "CRITERIA_MISMATCH")
    assert got["EXPERT"] == ("UNCERTAIN", "EXPERT_REVIEW", "NEEDS_REVIEW")
    assert got["VALUE"][1] != "CONFIRMED_ELIGIBLE" and got["VALUE"][2] != "CRITERIA_MATCH"
    assert got["SELF"] == ("ELIGIBLE", "CONFIRMED_ELIGIBLE", "CRITERIA_MATCH")


def test_a_test_attestation_means_nothing_under_the_production_authority(tmp_path, production):
    """증명이 있어도 현재 권한이 인정해야 함: 합성 권한이 발급한 증명을 운영 조건에 그대로 넣음."""
    with run(tmp_path, {"T": [HB, AGE]}, RECORD) as r:
        assert outcome(r.s) == ("UNCERTAIN", "VERIFICATION_PENDING", "POTENTIAL_MATCH")


@pytest.mark.parametrize("attested", [False, True], ids=["production", "attested"])
def test_a_value_the_source_does_not_state_is_not_confirmed(tmp_path, monkeypatch, attested):
    """환자값 근거(N2)만 막는 사례: self_report_ok 기준이고 프로필 값이 원문과 다름. 문서 증명으로 풀리지 않음."""
    real = night.profiler

    def wrong_age(pid, raw, reference_date=None):
        p = real(pid, raw, reference_date)
        p.age, p.age_value = 30, 30
        return p
    monkeypatch.setattr(night, "profiler", wrong_age)
    with document_authority.use(AUTHORITY if attested else NoDocumentAuthority()):
        with run(tmp_path, {"T": [AGE]}, "", documented=attested) as r:
            got = outcome(r.s)
    assert got[0] != "ELIGIBLE" and got[1] != "CONFIRMED_ELIGIBLE" and got[2] != "CRITERIA_MATCH"


# ------------------------------------------------------------ 사례별: Session 전 출력 + WP6
# 이름: 시험 규칙, 원문, Run 인자, 운영 권한에서의 (판정, 엔진, 표시), 합성 증명 조건의 (판정, 엔진, 표시)
HELD = ("UNCERTAIN", "VERIFICATION_PENDING", "POTENTIAL_MATCH")
CASES = {
    "self_report_eligible": ([AGE], "", {}, ("ELIGIBLE", "CONFIRMED_ELIGIBLE", "CRITERIA_MATCH"), None),
    "document_required": ([HB, AGE], RECORD, {}, HELD, ("ELIGIBLE", "CONFIRMED_ELIGIBLE", "CRITERIA_MATCH")),
    "clinician_only": ([VAGUE, AGE], "", {}, ("UNCERTAIN", "EXPERT_REVIEW", "NEEDS_REVIEW"), None),
    "clear_ineligible": ([OLDER, AGE], "", {}, ("INELIGIBLE", "CONFIRMED_INELIGIBLE", "CRITERIA_MISMATCH"), None),
    "declared_ineligible": ([HB_HIGH, AGE], RECORD, {}, ("INELIGIBLE", "VERIFICATION_PENDING", "INSUFFICIENT_INFORMATION"),
                            ("INELIGIBLE", "CONFIRMED_INELIGIBLE", "CRITERIA_MISMATCH")),
    "answered_unknown": ([HISTORY, AGE], "", {"answer": "모름", "rounds": 2}, ("UNCERTAIN", "INFO_MISSING", "INSUFFICIENT_INFORMATION"), None),
    "no_answer": ([HISTORY, AGE], "", {"answer": "", "rounds": 2}, ("UNCERTAIN", "INFO_MISSING", "INSUFFICIENT_INFORMATION"), None),
    "conflicting_values": ([HB, AGE], "Hb 8 g/dL on 2026-09-01. Hb 10 g/dL on 2026-09-01.", {},
                           ("UNCERTAIN", "VERIFICATION_PENDING", "INSUFFICIENT_INFORMATION"), ("UNCERTAIN", "VERIFICATION_PENDING", "INSUFFICIENT_INFORMATION")),
    # 문서 게이트가 닿지 않는 기준(self_report_ok): 판정 ELIGIBLE이 남고, 막는 것은 출력 경계뿐
    "conflict_on_self_report_rule": ([HB, AGE], night.RISE, {"gate": False}, ("ELIGIBLE", "VERIFICATION_PENDING", "POTENTIAL_MATCH"),
                                     ("ELIGIBLE", "VERIFICATION_PENDING", "POTENTIAL_MATCH")),
}


def every_output(s, out: Path) -> dict:
    s.save(out)
    saved = json.loads((out / "result.json").read_text(encoding="utf-8"))
    web_view = cv.build(s, display.redact_errors(s.result()), s.report).result
    orch = {t: st for _, t, st in Orchestrator(s)._finalize_recommendation({})["ranked"]}
    ranked = lambda rec: {x["trial_id"]: x["match_status"] for x in rec["ranked"]}  # noqa: E731
    return {"session": {x.trial_id: x.match_status for x in s.recommendation.ranked}, "saved": ranked(saved["recommendation"]),
            "web": ranked(web_view["recommendation"]), "cli": {x.trial_id: x.match_status for x in cli.shown_recommendation(s).ranked},
            "orchestrator": orch}


@pytest.mark.parametrize("name", CASES)
@pytest.mark.parametrize("attested", [False, True], ids=["production", "attested"])
def test_each_case_is_the_same_on_every_output(tmp_path, name, attested):
    rules, raw, kw, plain, documented = CASES[name]
    want = (documented or plain) if attested else plain
    if name == "clear_ineligible" and not attested:
        want = ("INELIGIBLE", "VERIFICATION_PENDING", "INSUFFICIENT_INFORMATION")  # 운영에서는 문서가 없어 확정 부적격에 닿지 않음 (P1-4: 표시도 미확정)
    with document_authority.use(AUTHORITY if attested else NoDocumentAuthority()):
        with run(tmp_path / "w", {"T": rules}, raw, documented=attested, **dict(kw)) as r:
            got = outcome(r.s)
            outputs = every_output(r.s, tmp_path / "out")
    assert got == want
    expected = {} if want[0] == "INELIGIBLE" else {"T": want[2]}
    assert all(v == expected for v in outputs.values()), outputs


@pytest.mark.parametrize("name", [n for n in CASES if not CASES[n][2]])
@pytest.mark.parametrize("attested", [False, True], ids=["declared", "attested"])
def test_session_and_wp6_block_the_same_pairs(tmp_path, name, attested):
    """같은 규칙을 Session과 OfflineEvaluator에 넣음. 판정기는 '임상의 기준도 통과'를 주는 최악 대역."""
    rules, raw, _, _, _ = CASES[name]
    with document_authority.use(AUTHORITY if attested else NoDocumentAuthority()):
        with run(tmp_path, {"T": rules}, raw, documented=attested) as r:
            session_got = outcome(r.s)[:2]
    with document_authority.use(NoDocumentAuthority()):  # 시뮬레이터 권한은 evaluate 안에서만 켜져야 함
        sim_got = wp6(rules, sim_facts(raw), attested)
    assert sim_got == session_got


class PassAll:
    judge_id, include_draft = "pass_all", False

    def digest(self):
        return "pass_all"

    def assess(self, trial_id, rule, facts):
        return LLMAssessment(rule_id=rule.rule_id, applicable=True, holds="yes", evidence_type="explicit", confidence="high",
                             evidence="나이 62세", reasoning="stub", missing_info=None)


def sim_facts(raw):
    facts = [SimFact(field="age", value=62, unit="years"), SimFact(field="sex", value="male")]
    for value, date in night.re.findall(r"Hb (\d+) g/dL on (\S+?)\.", raw):
        facts.append(SimFact(field="lab:hemoglobin", value=float(value), unit="g/dL", observed_at=date))
    return facts


def wp6(rules, facts, attested, tid="T"):
    t = SimTrial(parsed(tid, [x.model_copy() for x in rules]), trial(tid), MappingProxyType({}))
    made = [make_fact(ALIAS, f, seq=i, origin=f"src{i}", source_type="clinical_record", verified=attested,
                      ref_kind="document", ref=None) for i, f in enumerate(facts)]
    state, _ = OfflineEvaluator((t,), REF, PassAll()).evaluate(ALIAS, made)
    return state.trials[tid].eligibility, CohortState.from_decision_states({ALIAS: state}).pair(ALIAS, tid).status.value


def test_wp6_blocks_all_three_inside_one_cohort():
    facts = sim_facts(RECORD)
    trials = {"SELF": [AGE], "DOC": [HB, AGE], "EXPERT": [VAGUE, AGE], "OUT": [OLDER, AGE]}
    got = {t: wp6(r, facts, False, t) for t, r in trials.items()}
    assert got["SELF"] == ("ELIGIBLE", "CONFIRMED_ELIGIBLE")
    assert got["DOC"] == ("UNCERTAIN", "VERIFICATION_PENDING")
    assert got["EXPERT"] == ("UNCERTAIN", "EXPERT_REVIEW")
    assert got["OUT"][0] == "INELIGIBLE"
    released = {t: wp6(r, facts, True, t) for t, r in trials.items()}
    assert released["DOC"] == ("ELIGIBLE", "CONFIRMED_ELIGIBLE")  # 합성 문서는 문서 경계만 풂
    assert released["EXPERT"] == ("UNCERTAIN", "EXPERT_REVIEW")


# ------------------------------------------------------------ C는 판정·근거를 덮어쓰지 않음
def test_lowered_output_keeps_verdict_evidence_and_the_original_status(tmp_path, production):
    raw = f"{night.DEL} {night.WT}"
    with run(tmp_path / "w", {"T": [night.EG, AGE]}, raw, documented=False, report=True, verdicts=night.EG_YES) as r:
        s = r.s
        matches = copy.deepcopy(s.matches)
        facts = [f.model_dump() for f in s.fact_store.facts(s.case.patient_id)]
        original = s.result()
        s.save(tmp_path / "out")
        assert s.matches == matches and [f.model_dump() for f in s.fact_store.facts(s.case.patient_id)] == facts
    saved = json.loads((tmp_path / "out" / "result.json").read_text(encoding="utf-8"))
    side = json.loads((tmp_path / "out" / "conservative_view.json").read_text(encoding="utf-8"))
    kept, was = saved["matches"]["T"], original["matches"]["T"]
    assert kept["match_status"] == "NEEDS_REVIEW" and was["match_status"] == "POTENTIAL_MATCH"
    assert kept["eligibility"] == was["eligibility"] and kept["assessments"] == was["assessments"]
    assert saved["engine_boundary"]["T"]["external"] == "POTENTIAL_MATCH" and saved["engine_boundary"]["T"]["shown"] == "NEEDS_REVIEW"
    assert side["holds"][0]["side"] == "potential" and side["holds"][0]["kinds"] == ["differing_biomarker"]
    assert (tmp_path / "out" / "report.raw.md").read_text(encoding="utf-8") == s.report  # LLM 원문은 따로


def test_conflict_reason_survives_the_document_gate(tmp_path, production):
    """게이트가 판정을 보류해도 상충 사유가 사라지지 않음. 통합 전에는 사유 없이 '기록 확인 시 충족 가능'으로 나왔음."""
    with run(tmp_path, {"T": [night.EG, AGE]}, f"{night.DEL} {night.WT}", documented=False, verdicts=night.EG_YES) as r:
        v = cv.build(r.s)
        item = v.result["recommendation"]["ranked"][0]
    assert r.s.matches["T"].eligibility == "UNCERTAIN" and v.holds["T"].kinds == ("differing_biomarker",)
    assert item["match_status"] == "NEEDS_REVIEW" and any("바이오마커" in n for n in item["verification_needed"])


def test_unconfirmed_control_is_not_rewritten(tmp_path, production):
    with run(tmp_path, {"T": [HB, AGE]}, RECORD, documented=False) as r:
        v = cv.build(r.s)
    assert v.holds == {} and v.engine["T"]["shown"] == v.engine["T"]["external"] == "POTENTIAL_MATCH"


# ------------------------------------------------------------ 테스트용 권한이 운영에 없음
PROBE = """
import json, os, sys, tempfile, pathlib
sys.path.insert(0, sys.argv[1])
from ctrec import document_authority as da, pipeline, cli
from ctrec.web import app
assert "ctrec.sim" not in sys.modules and "ctrec.sim.evaluator" not in sys.modules, "sim loaded by a production entry"
assert "document_support" not in sys.modules, "test authority loaded"
from night_consistency_support import AGE, Env, lab, session, run_once
assert "document_support" not in sys.modules, "test authority loaded"
HB = lab("HB", "hemoglobin", "9")
raw = "Synthetic 62-year-old male. Hb 10 g/dL on 2026-09-01."
with Env(pathlib.Path(tempfile.mkdtemp())) as e:
    def prof(pid, text, reference_date=None):
        from ctrec.schemas import LabValue
        from ctrec import question_eval as qe
        p = qe.stub_profiler(pid, text, reference_date)
        p.labs = [LabValue(name="hemoglobin", value=10.0, unit="g/dL", date="2026-09-01", source_quote="Hb 10 g/dL on 2026-09-01")]
        return p
    e.oe._set(pipeline.patient_profiler, "profile_patient", prof)
    s = session({"T": [HB, AGE]}, raw=raw)
    s.profile_patient()
    for f in s.fact_store.facts(s.case.patient_id):
        s.fact_store.set_verification(f.fact_id, "document_verified", reason="r", by="b", document_reference="DOC")
    s.match()
print(json.dumps({"authority": type(da.current()).__name__, "eligibility": s.matches["T"].eligibility}))
"""


@pytest.mark.parametrize("env", [{}, {"CTREC_DOCUMENT_AUTHORITY": "synthetic", "CTREC_TEST_AUTHORITY": "1", "PYTEST_CURRENT_TEST": "x"}],
                         ids=["plain", "env_flags"])
def test_production_process_has_no_document_authority(env):
    """conftest 없는 새 프로세스: 운영 진입점을 읽어도 기본 권한이고, 문자열로 표시한 문서 검증은 확정되지 않음. 환경 변수로 켜지지 않음."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC", "PYTEST"))}
    clean.update(env, PYTHONPATH=str(ROOT / "src"), PYTHONIOENCODING="utf-8", ANTHROPIC_BASE_URL="http://127.0.0.1:9")
    out = subprocess.run([sys.executable, "-c", PROBE, str(ROOT / "tests")], capture_output=True, text=True, encoding="utf-8",
                         env=clean, cwd=ROOT, timeout=120)
    assert out.returncode == 0, out.stderr[-800:]
    assert json.loads(out.stdout.strip().splitlines()[-1]) == {"authority": "NoDocumentAuthority", "eligibility": "UNCERTAIN"}


def test_no_shipped_file_reaches_a_synthetic_authority():
    """패키지·배포 파일이 테스트·시뮬레이터 권한을 부르지 않음. 시뮬레이터 권한은 sim 패키지 안에서만 쓰임."""
    src = ROOT / "src" / "ctrec"
    shipped = [p for p in src.rglob("*.py")]
    assert not [p for p in shipped if "document_support" in p.read_text(encoding="utf-8")]
    sim_users = {p.relative_to(src).parts[0] for p in shipped if "SIM_AUTHORITY" in p.read_text(encoding="utf-8")}
    assert sim_users == {"sim"}
    assert not [p for p in shipped if p.relative_to(src).parts[0] != "sim" and "from .sim" in p.read_text(encoding="utf-8")]
    deploy = [ROOT / "render.yaml", *(ROOT / "deploy").iterdir()]
    assert not [p for p in deploy if "AUTHORITY" in p.read_text(encoding="utf-8").upper()]


def test_the_web_worker_thread_runs_under_the_default_authority():
    """conftest 권한은 테스트 컨텍스트 값이라 웹 작업 스레드로 넘어가지 않음: 웹 테스트의 결과도 운영 기본 권한 기준."""
    import threading
    seen = []
    t = threading.Thread(target=lambda: seen.append(type(document_authority.current()).__name__))
    t.start()
    t.join()
    assert seen == ["NoDocumentAuthority"] and web.RUNS is not None


# ------------------------------------------------------------ WP6 가정 표시
def test_wp6_output_states_that_documents_are_a_synthetic_assumption(capsys):
    from ctrec.engine.contracts import EffortBudget
    from ctrec.sim import __main__ as sim_main, demo, runner
    from ctrec.sim.evaluator import DOCUMENT_ASSUMPTION

    co = demo.cohort()
    b = EffortBudget(coordinator_minutes=45, clinician_minutes=60, horizon_steps=3)
    manifest = runner.run(runner.build_spec("x", "no_action", co, b, 1, {}), co).manifest
    assert manifest["document_authority"] == DOCUMENT_ASSUMPTION
    assert DOCUMENT_ASSUMPTION["production_authority"] == "NoDocumentAuthority"
    assert DOCUMENT_ASSUMPTION["kind"] == "synthetic" and DOCUMENT_ASSUMPTION["operational_performance"] is False
    assert sim_main.main(["--policies", "no_action", "--budgets", "45", "--horizon", "3"]) == 0
    text = capsys.readouterr().out
    assert sim_main.DOCUMENT_NOTE in text and "운영" in sim_main.DOCUMENT_NOTE


# ------------------------------------------------------------ 웹 API 사용자 흐름 (실제 FastAPI·오케스트레이터·Session, LLM만 대역)
def test_web_api_flow_never_claims_what_the_engine_did_not_confirm(monkeypatch, tmp_path):
    pytest.importorskip("httpx")
    import web_harness as h

    h.install(monkeypatch, tmp_path)
    monkeypatch.setenv("CTREC_WEB_SAVE_OUTPUTS", "1")
    client = h.browser()
    rid = client.post("/api/runs", json={"custom_text": h.SYN_TEXT, "trial_ids": h.TRIAL_IDS, "screen": False}).json()["run_id"]
    run = web.RUNS[rid]
    q = h.wait_for(run, "questions")
    client.post(f"/api/runs/{rid}/answers", json={"answers": {x["question_id"]: h.SYN_ANSWER for x in q["questions"]}})
    h.wait_for(run, "end", 30)
    replay = h.sse_events(client.get(f"/api/runs/{rid}/events").text)
    result = next(e for e in replay if e["type"] == "result")["result"]
    assert result["engine_boundary"], "no boundary in the web result"
    for tid, e in result["engine_boundary"].items():
        assert result["matches"][tid]["match_status"] == e["shown"]
        if e["shown"] == "CRITERIA_MATCH":
            assert e["engine_status"] == "CONFIRMED_ELIGIBLE"
    saved = json.loads((tmp_path / "outputs" / "web" / rid / "result.json").read_text(encoding="utf-8"))
    assert {t: m["match_status"] for t, m in saved["matches"].items()} == {t: m["match_status"] for t, m in result["matches"].items()}
    assert {t: m["eligibility"] for t, m in saved["matches"].items()} == {t: m["eligibility"] for t, m in result["matches"].items()}
    rec = result["recommendation"]
    assert not [x for x in rec["ranked"] if x["match_status"] == "CRITERIA_MATCH" and result["engine_boundary"][x["trial_id"]]["engine_status"] != "CONFIRMED_ELIGIBLE"]
