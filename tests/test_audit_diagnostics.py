"""Inspector v2 진단 연결 (engine.audit_diagnostics): 정상 기록 + decision_record_issues.jsonl 함께 조회. API 호출 없음."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import test_record_producer as trp  # noqa: E402  (실제 생산기 경로로 정상·차단 단계를 만드는 고정 입력)

from ctrec.engine import audit  # noqa: E402
from ctrec.engine import audit_diagnostics as ad  # noqa: E402
from ctrec.engine import record_producer as rp  # noqa: E402
from ctrec.eval_hygiene import LeakScanner  # noqa: E402

PS = trp.PS
A = trp.A
NEW_KEYS = {ad.VIEW_KEY, "audit_status"}
SCAN = LeakScanner()
# 합성 표지값. 노출 검사는 실패해도 값이 출력되지 않게 bool로 비교함 (_leaked)
ID_MARK = "ZZSYN-ORIG-7731"
TEXT_MARK = "합성 원문 문장 표지"


def _ok_step(step=1, warn=False):
    f = trp.Facts()
    if warn:
        f.add_answer("smoker", True, "smoker", ref="raw_text:answers")  # 귀속 불가 사실 -> 경고
    return rp.build_step(trp.obs(step=step, facts=f))


def _blocked_step(step=2):
    before = trp.cohort(trp.pair("T1", PS.CONFIRMED_ELIGIBLE, [trp.crit("T1", "I1", "MET")], "ELIGIBLE"))
    after = trp.cohort(trp.pair("T1", PS.CONFIRMED_INELIGIBLE, [trp.crit("T1", "I1", "NOT_MET")], "INELIGIBLE"))
    return rp.build_step(trp.obs(step=step, before=before, after=after))


def _write(run_dir: Path, *results):
    records = audit.RecordWriter(run_dir / audit.RECORDS_FILE, "RUN-1")
    issues = rp.IssueWriter(run_dir / rp.ISSUES_FILE, "RUN-1")
    for r in results:
        rp.write_step(r, records, issues)
    return run_dir


def _issue_line(**kw) -> str:
    base = dict(issue_id="RUN-1:1:unattributed_fact:x", run_id="RUN-1", step=1, action_id=f"{A}:fp1",
                code="unattributed_fact", severity="warning", patient_alias=A)
    base.update(kw)
    return rp.RecordIssue(**base).model_dump_json()


def _append(run_dir: Path, *lines: str):
    with (run_dir / rp.ISSUES_FILE).open("a", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")


def _base_unchanged(view, run_dir):
    legacy = audit.build_v2_view(audit.load_run_v2(run_dir))
    assert {k: v for k, v in view.items() if k not in NEW_KEYS} == legacy
    assert set(view) == set(legacy) | NEW_KEYS


# ------------------------------------------------------------ 1. 정상 기록만 (진단 파일 없음)
def test_records_only_run_without_issue_file(tmp_path):
    _write(tmp_path, _ok_step())
    assert not (tmp_path / rp.ISSUES_FILE).exists()  # IssueWriter는 진단이 있을 때만 파일을 만듦
    view = ad.inspect_run(tmp_path, scanner=SCAN)
    _base_unchanged(view, tmp_path)
    d = view[ad.VIEW_KEY]
    assert d["file_status"] == "absent" and d["problems"] == [] and d["items"] == []
    assert d["steps"] == [{"step": 1, "status": "recorded", "has_record": True, "action_ids": [f"{A}:fp1"],
                           "blocking": 0, "warnings": 0, "codes": []}]
    assert view["audit_status"] == "no_blocking_diagnostics"  # '정상 완료'로 단정하지 않음
    assert "complete" not in view["audit_status"] and "complete" not in json.dumps(view[ad.VIEW_KEY])


# ------------------------------------------------------------ 2. 정상 기록 + 경고 진단
def test_records_with_warning_diagnostics(tmp_path):
    _write(tmp_path, _ok_step(warn=True))
    assert audit.load_run_v2(tmp_path).issues == []  # 기존 로더 의미 그대로 (진단을 섞지 않음)
    view = ad.inspect_run(tmp_path, scanner=SCAN)
    _base_unchanged(view, tmp_path)
    d = view[ad.VIEW_KEY]
    assert d["file_status"] == "ok" and d["warnings"] == 1 and d["blocking"] == 0
    assert d["steps"][0]["status"] == "recorded_with_warnings" and d["steps"][0]["codes"] == ["unattributed_fact"]
    assert d["items"][0]["has_record"] is True and d["blocked_transitions"] == []
    assert view["audit_status"] == "no_blocking_diagnostics"


# ------------------------------------------------------------ 3. 계약 위반으로 차단된 단계
def test_blocked_step_is_shown_without_restoring_a_record(tmp_path):
    _write(tmp_path, _ok_step(1), _blocked_step(2))
    view = ad.inspect_run(tmp_path, scanner=SCAN)
    _base_unchanged(view, tmp_path)
    d = view[ad.VIEW_KEY]
    assert [s["step"] for s in view["steps"]] == [1]  # 정상 기록은 복원하지 않음
    st = {s["step"]: s for s in d["steps"]}
    assert st[2]["status"] == "contract_violation" and st[2]["has_record"] is False
    assert "direct_confirmed_reversal" in st[2]["codes"] and d["violation_steps"] == [2]
    assert view["audit_status"] == "contract_violation" and d["problems"] == []
    # 차단된 전이는 별도 목록에만 있고 정상 쌍 이력에는 없음
    assert d["blocked_transitions"] == [{"step": 2, "action_id": f"{A}:fp1", "pair": f"{A}|T1",
                                         "before": "CONFIRMED_ELIGIBLE", "after": "CONFIRMED_INELIGIBLE",
                                         "code": "direct_confirmed_reversal", "detail": []}]
    assert all(h["step"] != 2 for hist in view["pair_history"].values() for h in hist)
    # 기준 단위 차단 진단(withheld_criterion_change)은 쌍 전이 목록에 넣지 않음
    assert any(i["code"] == "withheld_criterion_change" for i in d["items"])
    assert all(b["code"] != "withheld_criterion_change" for b in d["blocked_transitions"])
    html = ad.render_diagnostics_html(view)
    assert "계약 위반 전이 (" in html and "contract_violation" in html
    assert "차단된 전이" not in html  # 뜻이 다른 두 종류를 한 제목으로 묶지 않음


def test_withheld_pair_transition_stays_out_of_pair_history(tmp_path):
    _write(tmp_path, _ok_step(1))
    _append(tmp_path, _issue_line(issue_id="RUN-1:2:withheld_pair_transition:T2", step=2, severity="blocking",
                                  code="withheld_pair_transition", trial_id="T2", before="INFO_MISSING",
                                  after="BLOCKED_FAILURE", detail=("authority:policy",)))
    view = ad.inspect_run(tmp_path, scanner=SCAN)
    d = view[ad.VIEW_KEY]
    assert [b["code"] for b in d["blocked_transitions"]] == ["withheld_pair_transition"]
    assert d["violation_transitions"] == [] and [b["code"] for b in d["withheld_transitions"]] == [ad.WITHHELD_PAIR_CODE]
    assert d["withheld_transitions"][0]["withheld_by"] == []  # 원인 진단이 없으면 추정하지 않음
    assert "확인되지 않음" in ad.render_diagnostics_html(view)
    assert f"{A}|T2" not in view["pair_history"]


def _section(html: str, title: str) -> str:
    """제목 title로 시작하는 h3 절 하나의 HTML."""
    return html.split(f"<h3>{title}", 1)[1].split("<h3>", 1)[0]


def _mixed_step(step=2):
    """같은 단계에 직접 역전(T1) + 정상 정책 전이(T2 -> BLOCKED_FAILURE) + 기준 변화."""
    C = trp.C
    before = trp.cohort(trp.pair("T1", PS.CONFIRMED_ELIGIBLE, [trp.crit("T1", "I1", "MET")], "ELIGIBLE"),
                        trp.pair("T2", PS.INFO_MISSING, [trp.crit("T2", "I1", "UNKNOWN")]))
    after = trp.cohort(trp.pair("T1", PS.CONFIRMED_INELIGIBLE, [trp.crit("T1", "I1", "NOT_MET")], "INELIGIBLE"),
                       trp.pair("T2", PS.BLOCKED_FAILURE, [trp.crit("T2", "I1", "UNKNOWN")],
                                pending=("sources_exhausted",)))
    pt = C.PairTransition(pair=C.PairKey(patient_alias=A, trial_id="T2"), before=PS.INFO_MISSING,
                          after=PS.BLOCKED_FAILURE, authority="policy", reason_code="sources_exhausted",
                          pending_kinds=("sources_exhausted",))
    return rp.build_step(trp.obs(trp.action(trials=("T1", "T2"), targets=("T1:I1", "T2:I1")), step=step, before=before,
                                 after=after, policy_transitions=(pt,), reevaluated_trials=frozenset({"T1"})))


def test_violation_and_withheld_in_same_step_are_separated(tmp_path):
    _write(tmp_path, _ok_step(1), _mixed_step(2))
    view = ad.inspect_run(tmp_path, scanner=SCAN)
    _base_unchanged(view, tmp_path)  # 정상 이력(pair_history·steps[].pairs) 포함 기존 키·값 그대로
    d = view[ad.VIEW_KEY]
    assert d["violation_transitions"] == [{"step": 2, "action_id": d["violation_transitions"][0]["action_id"],
                                           "pair": f"{A}|T1", "before": "CONFIRMED_ELIGIBLE",
                                           "after": "CONFIRMED_INELIGIBLE", "code": "direct_confirmed_reversal",
                                           "detail": []}]
    w = d["withheld_transitions"]
    assert [(b["pair"], b["before"], b["after"], b["code"]) for b in w] == [
        (f"{A}|T2", "INFO_MISSING", "BLOCKED_FAILURE", "withheld_pair_transition")]
    assert w[0]["detail"] == ["authority:policy", "reason:sources_exhausted"]
    assert w[0]["withheld_by"] == ["direct_confirmed_reversal"]  # withheld_* 자신은 원인이 아님
    assert not any(h["step"] == 2 for hist in view["pair_history"].values() for h in hist)
    assert not any(p for s in view["steps"] if s["step"] == 2 for p in s["pairs"])

    html = ad.render_diagnostics_html(view)
    vio, held = _section(html, "계약 위반 전이"), _section(html, "보류된 전이")
    assert "direct_confirmed_reversal" in vio and "T1" in vio and "T2" not in vio
    assert "BLOCKED_FAILURE" in held and "T2" in held and "CONFIRMED_INELIGIBLE" not in held
    assert "direct_confirmed_reversal" in held  # 보류 원인 칸


def test_withheld_criterion_change_stays_out_of_all_pair_transition_lists(tmp_path):
    _write(tmp_path, _mixed_step(2))
    d = ad.inspect_run(tmp_path, scanner=SCAN)[ad.VIEW_KEY]
    assert any(i["code"] == "withheld_criterion_change" for i in d["items"])
    for key in ("violation_transitions", "withheld_transitions", "blocked_transitions"):
        assert all(b["code"] != "withheld_criterion_change" for b in d[key])
    assert all(c != "withheld_criterion_change" for b in d["withheld_transitions"] for c in b["withheld_by"])


def test_withheld_cause_may_be_a_step_level_violation(tmp_path):
    """위반이 쌍 전이가 아니어도(사실 소실 등) 보류 원인이 보이고, 위반 전이 목록은 비어 있음."""
    _write(tmp_path, _ok_step(1))
    _append(tmp_path,
            _issue_line(issue_id="RUN-1:2:fact_missing_after", step=2, severity="blocking", code="fact_missing_after"),
            _issue_line(issue_id="RUN-1:2:withheld_pair_transition:T2", step=2, severity="blocking",
                        code="withheld_pair_transition", trial_id="T2", before="INFO_MISSING", after="BLOCKED_FAILURE"))
    d = ad.inspect_run(tmp_path, scanner=SCAN)[ad.VIEW_KEY]
    assert d["violation_transitions"] == []
    assert d["withheld_transitions"][0]["withheld_by"] == ["fact_missing_after"]


@pytest.mark.parametrize("code", ["direct_confirmed_reversal", "transition_rejected", "state_change_without_execution",
                                  "policy_transition_disagrees_with_state", "future_unknown_blocking_code"])
def test_every_non_withheld_pair_code_is_a_violation(tmp_path, code):
    _write(tmp_path, _ok_step(1))
    _append(tmp_path, _issue_line(issue_id=f"RUN-1:2:{code}:T9", step=2, severity="blocking", code=code, trial_id="T9",
                                  before="INFO_MISSING", after="CONFIRMED_ELIGIBLE"))
    view = ad.inspect_run(tmp_path, scanner=SCAN)
    d = view[ad.VIEW_KEY]
    assert [b["code"] for b in d["violation_transitions"]] == [code] and d["withheld_transitions"] == []
    assert f"{A}|T9" not in view["pair_history"]


def test_blocked_transitions_stays_the_legacy_union(tmp_path):
    """호환: 기존 키는 두 목록의 합본이고 행 모양·순서가 그대로 (withheld_by 없음)."""
    _write(tmp_path, _ok_step(1), _mixed_step(2))
    d = ad.inspect_run(tmp_path, scanner=SCAN)[ad.VIEW_KEY]
    legacy_keys = {"step", "action_id", "pair", "before", "after", "code", "detail"}
    assert all(set(b) == legacy_keys for b in d["blocked_transitions"])
    assert [b["code"] for b in d["blocked_transitions"]] == ["direct_confirmed_reversal", "withheld_pair_transition"]
    stripped = [{k: v for k, v in b.items() if k != "withheld_by"} for b in d["withheld_transitions"]]
    assert sorted(d["blocked_transitions"], key=str) == sorted(d["violation_transitions"] + stripped, key=str)


def test_html_splits_a_legacy_view_without_new_keys(tmp_path):
    _write(tmp_path, _ok_step(1), _mixed_step(2))
    view = ad.inspect_run(tmp_path, scanner=SCAN)
    del view[ad.VIEW_KEY]["violation_transitions"], view[ad.VIEW_KEY]["withheld_transitions"]
    html = ad.render_diagnostics_html(view)
    vio, held = _section(html, "계약 위반 전이"), _section(html, "보류된 전이")
    assert "T1" in vio and "T2" not in vio and "T2" in held and "T1" not in held


def test_normal_blocked_failure_policy_transition_is_distinct_from_contract_violation(tmp_path):
    """쌍 상태 BLOCKED_FAILURE(확보 수단 소진, 정상 정책 전이)와 단계 상태 contract_violation(차단 진단)은 다른 개념."""
    C = trp.C
    # 단계 1: 무응답 -> 정책 권한 INFO_MISSING -> BLOCKED_FAILURE (계약상 정상 전이, 정상 기록)
    a1 = trp.action("fp3", targets=("T3:I1",), trials=("T3",))
    pt = C.PairTransition(pair=C.PairKey(patient_alias=A, trial_id="T3"), before=PS.INFO_MISSING,
                          after=PS.BLOCKED_FAILURE, authority="policy", reason_code="sources_exhausted",
                          pending_kinds=("sources_exhausted",))
    s1 = rp.build_step(trp.obs(a1, execution="declined", policy_transitions=(pt,), reevaluated_trials=frozenset(),
                               before=trp.cohort(trp.pair("T3", PS.INFO_MISSING, [trp.crit("T3", "I1", "UNKNOWN")])),
                               after=trp.cohort(trp.pair("T3", PS.BLOCKED_FAILURE, [trp.crit("T3", "I1", "UNKNOWN")],
                                                         pending=("sources_exhausted",)))))
    assert s1.status == "recorded"
    # 단계 2: T1 직접 역전(차단) + 같은 단계의 정상 정책 전이 T2 -> BLOCKED_FAILURE는 보류(withheld) 진단으로만 남음
    s2 = _mixed_step(2)
    assert s2.status == "contract_violation"
    _write(tmp_path, s1, s2)

    view = ad.inspect_run(tmp_path, scanner=SCAN)
    d = view[ad.VIEW_KEY]
    st = {s["step"]: s for s in d["steps"]}
    # 정상 BLOCKED_FAILURE 정책 전이: 기록된 단계, 정상 쌍 이력에 정책 권한으로 있음, 차단 목록에 없음
    assert st[1]["status"] == "recorded" and st[1]["has_record"] and st[1]["blocking"] == 0
    assert view["pair_history"][f"{A}|T3"] == [{"step": 1, "action_id": a1.action_id, "before": "INFO_MISSING",
                                                 "after": "BLOCKED_FAILURE", "authority": "policy"}]
    assert all(b["pair"] != f"{A}|T3" for b in d["blocked_transitions"])
    assert all(b["pair"] != f"{A}|T3" for b in d["violation_transitions"] + d["withheld_transitions"])
    # contract_violation 단계: 기록 없음, 역전은 위반 목록, 정상 BLOCKED_FAILURE 전이는 보류 목록에만 있음
    assert st[2]["status"] == "contract_violation" and not st[2]["has_record"]
    assert [(b["pair"], b["after"]) for b in d["violation_transitions"]] == [(f"{A}|T1", "CONFIRMED_INELIGIBLE")]
    assert [(b["pair"], b["after"]) for b in d["withheld_transitions"]] == [(f"{A}|T2", "BLOCKED_FAILURE")]
    assert f"{A}|T1" not in view["pair_history"] and f"{A}|T2" not in view["pair_history"]
    assert [p["after"] for s in view["steps"] for p in s["pairs"]] == ["BLOCKED_FAILURE"]  # 단계 1의 정상 전이만
    assert view["audit_status"] == "contract_violation" and d["violation_steps"] == [2]


# ------------------------------------------------------------ 4. 진단 파일 없는 기존 실행 (회귀)
def test_legacy_run_loads_exactly_as_before(tmp_path):
    rec = trp.rp.build_step(trp.obs()).record
    audit.RecordWriter(tmp_path / audit.RECORDS_FILE, "RUN-1").write(rec)
    loaded, diag = ad.load_run_with_diagnostics(tmp_path)
    assert loaded.records == audit.load_run_v2(tmp_path).records and loaded.issues == []
    assert diag.file_status == "absent" and diag.issues == [] and diag.problems == []


def test_trace_only_run_and_missing_run_dir(tmp_path):
    (tmp_path / "trace.jsonl").write_text("", encoding="utf-8")
    view = ad.inspect_run(tmp_path, scanner=SCAN)
    assert view["issues"] == ["no_v2_records"] and view[ad.VIEW_KEY]["file_status"] == "absent"
    with pytest.raises(audit.AuditError):
        ad.inspect_run(tmp_path / "missing")


# ------------------------------------------------------------ 5. 손상된 진단 항목
def test_damaged_lines_are_reported_without_values_and_good_lines_kept(tmp_path):
    _write(tmp_path, _ok_step(1, warn=True))
    secret = "환자 원문 문장 김철수 010-1234-5678"
    bad_extra = json.loads(_issue_line(issue_id="RUN-1:1:x:extra"))
    bad_extra["note"] = secret  # 모델에 없는 자유 서술 필드
    bad_value = json.loads(_issue_line(issue_id="RUN-1:1:x:bad"))
    bad_value["detail"] = [secret]  # 형식 위반 값
    _append(tmp_path, "{not json", "[1, 2]", json.dumps(bad_extra, ensure_ascii=False),
            json.dumps(bad_value, ensure_ascii=False), "")
    view = ad.inspect_run(tmp_path, scanner=SCAN)
    d = view[ad.VIEW_KEY]
    assert d["file_status"] == "partial" and view["audit_status"] == "diagnostics_damaged"
    assert d["problems"] == ["line 2: corrupt_json", "line 3: not_object", "line 4: invalid_issue:<extra>",
                             "line 5: invalid_issue:<root>"]  # detail은 모델 검사기에서 거부 (값은 남기지 않음)
    assert d["warnings"] == 1  # 정상 줄은 유지
    out = json.dumps(view, ensure_ascii=False) + ad.render_diagnostics_html(view)
    assert secret not in out and "김철수" not in out


def test_non_utf8_issue_file_is_unreadable(tmp_path):
    _write(tmp_path, _ok_step(1))
    (tmp_path / rp.ISSUES_FILE).write_bytes("모름".encode("cp949"))
    view = ad.inspect_run(tmp_path, scanner=SCAN)
    assert view[ad.VIEW_KEY]["file_status"] == "unreadable"
    assert view["audit_status"] == "diagnostics_unreadable"
    assert view[ad.VIEW_KEY]["problems"] == ["issues_file_not_utf8"]


def test_issue_file_link_is_not_followed(tmp_path):
    run = tmp_path / "run"
    _write(run, _ok_step(1))
    outside = tmp_path / "outside.jsonl"
    outside.write_text(_issue_line() + "\n", encoding="utf-8")
    try:
        (run / rp.ISSUES_FILE).symlink_to(outside)
    except OSError:
        pytest.skip("이 환경에서 심볼릭 링크를 만들 수 없음")
    d = ad.inspect_run(run)[ad.VIEW_KEY]
    assert d["file_status"] == "unreadable" and d["problems"] == ["issues_file_link"] and d["items"] == []


def test_other_run_lines_are_excluded(tmp_path):
    _write(tmp_path, _ok_step(1))
    _append(tmp_path, _issue_line(run_id="RUN-2", issue_id="RUN-2:1:unattributed_fact:x"))
    d = ad.inspect_run(tmp_path, scanner=SCAN)[ad.VIEW_KEY]
    assert d["items"] == [] and d["problems"] == ["line 1: other_run"]


def test_injected_scanner_excludes_flagged_lines(tmp_path):
    class Scanner:
        def scan(self, text):
            return ["original_id"] if "SECRET-ID" in text else []

    _write(tmp_path, _ok_step(1))
    _append(tmp_path, _issue_line(trial_id="SECRET-ID"), _issue_line(issue_id="RUN-1:1:ok"))
    view = ad.inspect_run(tmp_path, scanner=Scanner())
    d = view[ad.VIEW_KEY]
    assert d["problems"] == ["line 1: sensitive_text:original_id"] and len(d["items"]) == 1
    assert "SECRET-ID" not in json.dumps(view) + ad.render_diagnostics_html(view)


def test_real_leak_scanner_blocks_sensitive_lines_through_every_entry_point(tmp_path):
    results = (_ok_step(1), _mixed_step(2))
    _write(tmp_path, *results)  # 실제 진단(상태 토큰 포함)은 오탐 없이 통과해야 함
    clean = sum(len(r.issues) for r in results)
    assert len(ad.inspect_run(tmp_path, scanner=SCAN)[ad.VIEW_KEY]["items"]) == clean
    _append(tmp_path,
            _issue_line(issue_id="RUN-1:2:withheld_pair_transition:ORIG-777", step=2, severity="blocking",
                        code="withheld_pair_transition", trial_id="ORIG-777", before="INFO_MISSING",
                        after="BLOCKED_FAILURE"),
            _issue_line(issue_id="RUN-1:2:direct_confirmed_reversal:M-SYN-A", step=2, severity="blocking",
                        code="direct_confirmed_reversal", trial_id="M-SYN-A", before="CONFIRMED_ELIGIBLE",
                        after="CONFIRMED_INELIGIBLE"))
    scanner = LeakScanner(literals={"ORIG-777"})
    view = ad.inspect_run(tmp_path, scanner=scanner)
    d = view[ad.VIEW_KEY]
    assert len(d["items"]) == clean and view["audit_status"] == "contract_violation"
    assert [p.split(": ")[1] for p in d["problems"]] == ["sensitive_text:original_id", "sensitive_text:case_id_shape"]
    out = json.dumps(view, ensure_ascii=False) + ad.render_diagnostics_html(view)
    assert "ORIG-777" not in out and "M-SYN-A" not in out
    # 하위 진입점도 같은 scanner를 전달함
    _, diag = ad.load_run_with_diagnostics(tmp_path, scanner=scanner)
    assert len(diag.issues) == clean and len(diag.problems) == 2


def _leaked(view: dict, mark: str) -> bool:
    return mark in json.dumps(view, ensure_ascii=False) or mark in ad.render_diagnostics_html(view)


# 형식 검사를 통과하는 필드들. 값은 합성 표지이고 매개변수 이름에는 필드 이름만 씀
_LOW_MARK = ID_MARK.lower().replace("-", "_")
_ID_SHAPED = {"trial_id": ({"trial_id": ID_MARK}, ID_MARK), "issue_id": ({"issue_id": f"RUN-1:1:{ID_MARK}"}, ID_MARK),
              "action_id": ({"action_id": ID_MARK}, ID_MARK), "criterion_key": ({"criterion_key": f"{ID_MARK}:I1"}, ID_MARK),
              "code": ({"code": _LOW_MARK}, _LOW_MARK), "before": ({"before": "ZZSYN_NAME"}, "ZZSYN_NAME"),
              "detail": ({"detail": (_LOW_MARK,)}, _LOW_MARK)}


class _AllowAll:
    def scan(self, text):
        return []


@pytest.mark.parametrize("name", sorted(_ID_SHAPED))
def test_without_scanner_no_issue_value_reaches_view_or_html(tmp_path, name):
    """ID 모양 값은 형식 검사를 통과함 -> scanner가 없으면 진단 값을 아예 내보내지 않음. 정상 기록 조회는 그대로."""
    _write(tmp_path, _ok_step(1))
    kw, mark = _ID_SHAPED[name]
    _append(tmp_path, _issue_line(**kw))
    reachable = _leaked(ad.inspect_run(tmp_path, scanner=_AllowAll()), mark)
    assert reachable is True  # 이 값은 형식 검사를 통과함: 아래 차단은 scanner 게이트 때문임
    view = ad.inspect_run(tmp_path)
    leaked = _leaked(view, mark)
    assert leaked is False
    _base_unchanged(view, tmp_path)
    assert [s["step"] for s in view["steps"]] == [1]
    d = view[ad.VIEW_KEY]
    assert d["file_status"] == "not_scanned" and d["problems"] == ["scanner_required"] and d["items"] == []
    assert view["audit_status"] == "diagnostics_not_scanned"  # '차단 없음'으로 보고하지 않음
    assert "scanner_required" in ad.render_diagnostics_html(view)


def test_without_scanner_blocking_step_is_not_reported_as_clean(tmp_path):
    _write(tmp_path, _ok_step(1), _mixed_step(2))
    view = ad.inspect_run(tmp_path)
    assert view["audit_status"] == "diagnostics_not_scanned"
    _, diag = ad.load_run_with_diagnostics(tmp_path)
    assert diag.file_status == "not_scanned" and diag.issues == []
    assert ad.load_diagnostics(tmp_path).file_status == "not_scanned"


def test_without_scanner_absent_file_and_legacy_view_still_work(tmp_path):
    _write(tmp_path, _ok_step(1))  # 진단 파일 없음: 내보낼 값이 없으므로 기존과 같음
    view = ad.inspect_run(tmp_path)
    _base_unchanged(view, tmp_path)
    assert view[ad.VIEW_KEY]["file_status"] == "absent" and view["audit_status"] == "no_blocking_diagnostics"


@pytest.mark.parametrize("scanner", [None, SCAN], ids=["no_scanner", "scanner"])
def test_unknown_field_name_from_file_is_not_echoed(tmp_path, scanner):
    """추가 필드의 키 이름도 파일 내용임. scanner는 검증 실패 줄을 보지 못하므로 문제 목록에 그대로 쓰면 안 됨."""
    _write(tmp_path, _ok_step(1))
    bad = json.loads(_issue_line())
    bad[TEXT_MARK] = 1
    (tmp_path / rp.ISSUES_FILE).write_text(json.dumps(bad, ensure_ascii=False) + "\n", encoding="utf-8")
    view = ad.inspect_run(tmp_path, scanner=scanner)
    leaked = _leaked(view, TEXT_MARK)
    assert leaked is False
    if scanner is not None:
        assert view[ad.VIEW_KEY]["problems"] == ["line 1: invalid_issue:<extra>"]


@pytest.mark.parametrize("field,value", [("trial_id", TEXT_MARK), ("detail", (TEXT_MARK,)), ("fact_ids", (TEXT_MARK,)),
                                         ("before", TEXT_MARK)], ids=["trial_id", "detail", "fact_ids", "before"])
def test_free_text_is_rejected_by_field_formats_even_with_a_permissive_scanner(tmp_path, field, value):
    _write(tmp_path, _ok_step(1))
    line = json.loads(_issue_line())
    line[field] = list(value) if isinstance(value, tuple) else value
    _append(tmp_path, json.dumps(line, ensure_ascii=False))
    view = ad.inspect_run(tmp_path, scanner=_AllowAll())
    leaked = _leaked(view, TEXT_MARK)
    assert leaked is False and view[ad.VIEW_KEY]["items"] == []


def test_scanner_sees_issue_lines_only_and_record_loading_is_unchanged(tmp_path):
    class Spy:
        def __init__(self):
            self.seen = []

        def scan(self, text):
            self.seen.append(text)
            return []

    _write(tmp_path, _ok_step(1, warn=True))
    spy = Spy()
    view = ad.inspect_run(tmp_path, scanner=spy)
    assert len(spy.seen) == 1 and '"issue_id"' in spy.seen[0]  # 진단 줄만. 정상 기록은 조회 시 검사하지 않음
    _base_unchanged(view, tmp_path)


# ------------------------------------------------------------ 6. 동일 step의 중복 진단
def test_duplicate_issue_id_is_dropped_but_distinct_codes_on_same_step_are_kept(tmp_path):
    _write(tmp_path, _ok_step(1))
    a = _issue_line(issue_id="RUN-1:1:unattributed_fact:F-1")
    b = _issue_line(issue_id="RUN-1:1:decline_marker_fact:F-2", code="decline_marker_fact")
    _append(tmp_path, a, a, b)
    d = ad.inspect_run(tmp_path, scanner=SCAN)[ad.VIEW_KEY]
    assert d["problems"] == ["line 2: duplicate_issue"]
    assert [i["code"] for i in d["items"]] == ["decline_marker_fact", "unattributed_fact"]
    assert d["steps"][0]["warnings"] == 2 and d["by_code"] == {"warning:decline_marker_fact": 1,
                                                               "warning:unattributed_fact": 1}


# ------------------------------------------------------------ 기록-진단 불일치 (복원·추정하지 않고 보고)
def test_blocking_issue_on_recorded_step_is_inconsistent(tmp_path):
    _write(tmp_path, _ok_step(1))
    _append(tmp_path, _issue_line(issue_id="RUN-1:1:direct_confirmed_reversal:T1", severity="blocking",
                                  code="direct_confirmed_reversal", trial_id="T1", before="CONFIRMED_ELIGIBLE",
                                  after="CONFIRMED_INELIGIBLE"))
    view = ad.inspect_run(tmp_path, scanner=SCAN)
    d = view[ad.VIEW_KEY]
    assert d["steps"][0]["status"] == "inconsistent" and "blocking_step_has_record:1" in d["problems"]
    assert view["audit_status"] == "contract_violation"


def test_warning_without_record_and_action_mismatch_are_reported(tmp_path):
    _write(tmp_path, _ok_step(1))
    _append(tmp_path, _issue_line(issue_id="RUN-1:3:w", step=3),
            _issue_line(issue_id="RUN-1:1:w", action_id=f"{A}:other"))
    d = ad.inspect_run(tmp_path, scanner=SCAN)[ad.VIEW_KEY]
    st = {s["step"]: s for s in d["steps"]}
    assert st[3]["status"] == "diagnostics_without_record" and st[3]["has_record"] is False
    assert set(d["problems"]) == {"warning_step_without_record:3", "issue_action_mismatch:1"}


# ------------------------------------------------------------ 7. 기존 Inspector 회귀 호환
def test_existing_api_and_html_are_unchanged(tmp_path):
    _write(tmp_path, _ok_step(1, warn=True), _blocked_step(2))
    loaded = audit.load_run_v2(tmp_path)
    assert loaded.issues == [] and [r.step for r in loaded.records] == [1]  # 기존 결과 그대로
    view = ad.inspect_run(tmp_path, scanner=SCAN)
    base_html = audit.render_v2_html(audit.build_v2_view(loaded))
    html = ad.render_diagnostics_html(view)
    assert html.startswith(base_html[: -len("</body></html>")]) and html.endswith("</body></html>")
    assert ad.render_diagnostics_html(audit.build_v2_view(loaded)) == base_html  # 진단 키 없으면 그대로
    assert "<script" not in html


def test_html_escapes_diagnostic_values(tmp_path):
    view = audit.build_v2_view(audit.LoadResult())
    view[ad.VIEW_KEY] = {"file": rp.ISSUES_FILE, "file_status": "partial", "problems": ["<script>x</script>"],
                         "blocking": 0, "warnings": 0, "by_code": {}, "violation_steps": [], "steps": [],
                         "blocked_transitions": [], "items": []}
    view["audit_status"] = "<b>"
    html = ad.render_diagnostics_html(view)
    assert "<script>x" not in html and "&lt;script&gt;" in html and "<b>&lt;b&gt;</b>" in html


def test_html_escapes_both_transition_tables():
    bad = "<img src=x onerror=y>"
    row = {"step": bad, "pair": f"{A}|{bad}", "before": bad, "after": bad, "code": bad, "detail": [bad]}
    view = audit.build_v2_view(audit.LoadResult())
    view[ad.VIEW_KEY] = {"file": rp.ISSUES_FILE, "file_status": "ok", "problems": [], "blocking": 0, "warnings": 0,
                         "by_code": {}, "violation_steps": [], "steps": [], "items": [],
                         "violation_transitions": [row], "withheld_transitions": [{**row, "withheld_by": [bad]}],
                         "blocked_transitions": [row, row]}
    view["audit_status"] = "x"
    html = ad.render_diagnostics_html(view)
    assert "<img" not in html and html.count("&lt;img") >= 8


def test_module_respects_engine_isolation():
    import ast
    tree = ast.parse(Path(ad.__file__).read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add(node.module or "")
    banned = {"question_eval", "eval_hygiene", "metrics", "llm", "pipeline", "web", "answerers", "sim"}
    assert not {p for m in mods for p in m.split(".")} & banned
