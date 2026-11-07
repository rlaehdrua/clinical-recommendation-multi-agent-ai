"""고정 시험 최악 조건: LLM 자리가 모든 기준을 '기록에 그렇게 적혀 있다'로 통과시키는 합성 Session.

발생 빈도가 아니라 상한 측정입니다. 규칙 엔진은 끄고 전부 LLM 대역으로 보냅니다. API 호출 없음.
게이트 전 트리와 비교하려면 WT로 그 worktree를 지정합니다. OUT: 결과 JSON 경로.
"""
import json
import os
import sys
from collections import Counter
from contextlib import nullcontext
from pathlib import Path

WT = Path(os.environ.get("WT") or Path(__file__).resolve().parent.parent).resolve()
sys.path[:0] = [str(WT / "src"), str(WT / "scripts"), str(WT / "tests")]
os.chdir(WT)
import ctrec  # noqa: E402

assert str(WT).replace("\\", "/").lower() in ctrec.__file__.replace("\\", "/").lower(), ctrec.__file__
from bench_incremental import load_frozen  # noqa: E402
from decision_scenarios import load_trials  # noqa: E402

from ctrec import config, pipeline  # noqa: E402
from ctrec.agents import matcher  # noqa: E402
from ctrec.contract_session import ContractSession  # noqa: E402
from ctrec.decision_state import SessionView, build_state  # noqa: E402
from ctrec.engine.cohort_state import CohortState  # noqa: E402
from ctrec.pipeline import PatientCase, Session  # noqa: E402
from ctrec.schemas import Fact, LLMAssessment, MatcherOutput, PatientProfile  # noqa: E402
from ctrec.tools import rule_engine  # noqa: E402

try:
    from ctrec import document_authority
    from document_support import AUTHORITY, attest_declared
    GATE = True
except ImportError:
    GATE = False

PID = "PA-0123456789abcdef"
QUOTE = "진료 기록상 해당 기준을 충족한 것으로 확인되었습니다"
TEXT = f"62세 여성. {QUOTE}."
parsed = load_frozen()
trials = load_trials()
config.WRITE_REPORT = False
rule_engine.evaluate = lambda rule, profile: None


class Tracer:
    def log(self, *a, **k):
        pass


def llm(model, *, system=None, user=None, **k):
    rs = json.loads(user.split("<rules_to_assess>")[1].split("</rules_to_assess>")[0])
    return MatcherOutput(assessments=[LLMAssessment(
        rule_id=r["rule_id"], applicable=True, holds="yes" if r.get("kind") == "inclusion" else "no", evidence_type="explicit",
        confidence="high", evidence=QUOTE, reasoning="stub", missing_info="") for r in rs])


matcher.llm.structured = llm


def profile(pid, raw, reference_date=None):
    return PatientProfile(patient_id=PID, age=62, age_value=62, age_unit="years", sex="female", primary_diagnosis=None, disease_stage=None,
                          diagnoses=[], biomarkers=[], labs=[], medications=[], treatment_history=[], ecog=None,
                          other_facts=[Fact(description=QUOTE, date=None, source_quote=QUOTE)], missing_or_ambiguous=[])


pipeline.patient_profiler.profile_patient = profile
current = {}
pipeline.trial_store.get = lambda t: current["p"]


def forged(s):
    for f in s.fact_store.facts(PID):
        f.verification_status = "document_verified"


def free_text(s):
    for f in s.fact_store.facts(PID):
        s.fact_store.set_verification(f.fact_id, "document_verified", reason="r", by="b", document_reference="아무 글자")


CASES = {"unknown": ("unknown", None), "self_report": ("self_report", None), "declared": ("clinical_record", None),
         "status_string": ("clinical_record", forged), "free_text_reference": ("clinical_record", free_text)}
if GATE:
    CASES["synthetic_attested"] = ("clinical_record", lambda s: attest_declared(s.fact_store, PID))

out = {}
for cls in (Session, ContractSession):
    for name, (source, prep) in CASES.items():
        c, per = Counter(), {}
        for tid, p in sorted(parsed.items()):
            current["p"] = p
            with (document_authority.use(AUTHORITY) if name == "synthetic_attested" else nullcontext()):  # 대조군만 합성 권한
                s = cls(case=PatientCase(PID, TEXT, input_source=source), trials={tid: trials[tid]}, answerer=None, tracer=Tracer())
                s.profile_patient()
                if prep:
                    prep(s)
                s.match()
                v = SessionView.from_session(s)
                v.matched_versions = {t: v.snapshot.version for t in s.matches}
                pair = CohortState.from_decision_states({PID: build_state(v)}).pair(PID, tid)
            m = s.matches[tid]
            per[tid] = (m.eligibility, m.match_status, pair.status.value)
            c[f"elig:{m.eligibility}"] += 1
            c[f"ext:{m.match_status}"] += 1
            c[f"pair:{pair.status.value}"] += 1
        out[f"{cls.__name__}/{name}"] = {"counts": dict(sorted(c.items())), "trials": per}
Path(os.environ.get("OUT", "declared_record_measure.json")).write_text(json.dumps(out, ensure_ascii=False, indent=0), encoding="utf-8")
print({k: {x: y for x, y in v["counts"].items() if x in ("pair:CONFIRMED_ELIGIBLE", "ext:CRITERIA_MATCH")} for k, v in out.items()})
