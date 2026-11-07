"""추가 정보 확인 흐름의 화면 연결 (환자 화면 followup.js, 연구 담당자 화면 /staff): 화면이 부르는 API가 실제로 있는지,
담당자 화면이 서버의 상태 전이 규칙과 같은지, 담당자가 환자 답변을 보내는 경로가 없는지. 실제 브라우저 흐름은
scripts/followup_demo_server.py + 브라우저로 확인 (README §10). 합성 데이터만."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import test_followup_flow as F  # noqa: E402
import web_harness as h  # noqa: E402
from ctrec.followup.models import STAFF_TRANSITIONS  # noqa: E402
from ctrec.web import followup_api  # noqa: E402

env = F.env  # 같은 오프라인 서비스·합성 시험 카탈로그
STATIC = Path(__file__).resolve().parents[1] / "src" / "ctrec" / "web" / "static"


def _js(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_catalog_trials_lists_the_flow_catalog(env):
    r = h.browser().get("/api/followup/trials")
    assert r.status_code == 200
    d = r.json()
    assert d["synthetic"] is True and [t["trial_id"] for t in d["trials"]] == F.TRIALS
    assert all(set(t) == {"trial_id", "title", "overall_status", "synthetic"} for t in d["trials"])


def test_staff_page_and_scripts_are_served_under_csp():
    c = h.browser()
    r = c.get("/staff")  # 같은 페이지 (index.html), staff.js가 경로를 보고 연구 담당자 탭을 엶
    assert r.status_code == 200 and "default-src 'none'" in r.headers["content-security-policy"]
    assert r.text == c.get("/").text
    assert '/static/staff.js' in r.text and "<script>" not in r.text and 'id="page-staff"' in r.text
    assert 'data-role="patient"' in r.text and 'data-role="staff"' in r.text
    for name in ("staff.js", "staff.css", "followup.js"):
        assert c.get(f"/static/{name}").status_code == 200, name
    index = c.get("/").text
    assert "/static/followup.js" in index and 'name="flow-mode"' in index and 'id="fu-panel"' in index


def _routes() -> list[re.Pattern]:
    out = []
    for route in followup_api.router.routes:
        pat = re.sub(r"\{[^}]+\}", "[^/]+", route.path)
        out.append((route.methods, re.compile("^" + pat + "$")))
    return out


def _called_paths(src: str, prefix: str) -> set[str]:
    """화면 코드의 API 경로 (템플릿 변수는 값 하나로 바꿔 실제 경로 모양으로)."""
    paths = set()
    for m in re.finditer(r"[`\"](/api/followup[^`\"]*)[`\"]", src):
        if m.group(1) != "/api/followup/staff":  # api()가 앞에 붙이는 접두사 자체
            paths.add(re.sub(r"\$\{[^}]+\}", "X", m.group(1)))
    for m in re.finditer(r"(?:api|fuUrl)\(\s*[`\"]([^`\"]*)[`\"]", src):
        paths.add(prefix + re.sub(r"\$\{[^}]+\}", "X", m.group(1)))
    return paths


def test_every_path_the_screens_call_exists():
    routes = _routes()
    called = _called_paths(_js("followup.js"), "/api/followup/cases/X") | _called_paths(_js("staff.js"), "/api/followup/staff")
    assert called
    missing = [p for p in called if not any(rx.match(p) for _, rx in routes)]
    assert not missing, missing


def test_staff_screen_cannot_answer_for_the_patient():
    src = _js("staff.js")
    assert "/response" not in src  # 응답 API는 환자 쿠키만 받음 (담당자 헤더가 붙으면 403)


def test_staff_screen_moves_match_server_transitions():
    m = re.search(r"const MOVES = (\{.*?\});", _js("staff.js"), re.S)
    js = re.sub(r"([{,]\s*)([a-z_]+):", r'\1"\2":', m.group(1))  # 키에 따옴표
    moves = json.loads(js)
    server = {k: {s: sorted(v) for s, v in d.items()} for k, d in STAFF_TRANSITIONS.items()}
    assert {k: {s: sorted(v) for s, v in d.items()} for k, d in moves.items()} == server
    done = {"answered", "document_verified", "clinician_decided"}
    assert not any(t in done for d in moves.values() for v in d.values() for t in v)  # 완료 입력 버튼 없음


def test_demo_default_path_is_followup():
    """최종 데모 경로 통일 (docs/dev/web_demo_flow.md): 환자 화면 기본 = 연구진 확인 요청(/api/followup), 바로 분석(/api/runs)은 선택."""
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    checked = re.search(r'name="flow-mode" value="(\w+)" checked', index)
    assert checked and checked.group(1) == "followup"
    assert 'id="fu-setup" hidden' not in index


def test_staff_queue_can_send_and_marks_unconfirmed_states():
    src = _js("staff.js")
    assert "data-qsend" in src and "queueSend" in src
    for status in ("VERIFICATION_PENDING", "EXPERT_REVIEW"):
        assert re.search(status + r': "미확정', src), status


def test_staff_script_makes_no_globals():
    """app.js와 같은 페이지에서 돌므로 전역 이름 충돌이 없게 즉시 실행 함수로 감쌈."""
    src = _js("staff.js")
    code = "\n".join(line for line in src.splitlines() if not line.startswith("//"))
    assert code.lstrip().startswith("(() => {") and code.rstrip().endswith("})();")


def _two_cases():
    patient = h.browser()
    F._create(patient)
    other = h.browser(client=("198.51.100.31", 50002))
    F._create(other, text="Synthetic NSCLC patient, female, 52 years old. ECOG 0.")
    return patient


def test_insight_counts_patients_pairs_and_minutes(env):
    _two_cases()
    staff = F.staff_client()
    assert staff.get("/api/followup/staff/insight").status_code in (403, 503)  # 담당자 키 없으면 거절
    d = staff.get("/api/followup/staff/insight", headers=F.STAFF).json()
    s = d["summary"]
    assert s["cases"] == 2 and s["items"] >= len(d["questions"]) > 0
    queue = staff.get("/api/followup/staff/queue", headers=F.STAFF).json()["items"]
    for g in d["questions"]:
        ids = {p["case_id"] for p in g["patients"]}
        assert g["n_patients"] == len(g["patients"]) == len(ids)
        assert g["n_decided_pairs"] <= g["n_related_pairs"] and g["n_next_pairs"] <= g["n_related_pairs"]
        assert g["minutes"] == round(sum(p["minutes"] for p in g["patients"]), 1) >= 0
        for p in g["patients"]:  # 대기열(Cohort C)과 같은 순위·같은 행동
            assert any(q["proposal_id"] == p["proposal_id"] and q["rank"] == p["rank"] for q in queue)
    curve = s["c_curve"]
    assert [p["minutes"] for p in curve] == sorted(p["minutes"] for p in curve)  # 누적 분은 줄지 않음
    assert curve[-1]["minutes"] == s["minutes_all"]
    assert sorted(x["decided_pairs"] for x in s["patient_order_curve"])[-1] == curve[-1]["decided_pairs"]  # 같은 확인, 다른 순서


def test_insight_does_not_change_cases(env):
    _two_cases()
    staff = F.staff_client()
    before = staff.get("/api/followup/staff/cases", headers=F.STAFF).json()
    staff.get("/api/followup/staff/insight", headers=F.STAFF)
    assert staff.get("/api/followup/staff/cases", headers=F.STAFF).json() == before


# ------------------------------------------------------------ 발표용 데모: 담당자 키 없이 (followup_api.DEMO_STAFF)
def _loopback():
    return h.browser(base_url="http://127.0.0.1", client=("127.0.0.1", 50100))


def test_staff_key_is_required_unless_demo_server(env, monkeypatch):
    F._create(h.browser())
    assert followup_api.DEMO_STAFF is False  # 기본은 꺼짐
    c = _loopback()
    assert c.get("/api/followup/staff/session").json() == {"mode": "key", "configured": True}
    assert c.get("/api/followup/staff/cases").status_code == 403  # 루프백이어도 키 필요
    monkeypatch.setattr(followup_api, "DEMO_STAFF", True)
    assert c.get("/api/followup/staff/session").json()["mode"] == "demo"
    assert c.get("/api/followup/staff/cases").status_code == 200
    assert c.get("/api/followup/staff/insight").status_code == 200
    outside = F.staff_client()  # 루프백이 아닌 접속은 데모 서버여도 키 필요
    assert outside.get("/api/followup/staff/session").json()["mode"] == "key"
    assert outside.get("/api/followup/staff/cases").status_code == 403
    assert outside.get("/api/followup/staff/cases", headers=F.STAFF).status_code == 200


def test_demo_staff_still_cannot_answer_for_the_patient(env, monkeypatch):
    patient = h.browser()
    # 나이(자기 보고 허용)를 빼서 환자 질문이 있게 함: P 근거 경로 뒤 검사 항목은 환자 질문이 아니라 문서 확인
    case = F._create(patient, text=F.TEXT_NO_AGE)
    monkeypatch.setattr(followup_api, "DEMO_STAFF", True)
    c = _loopback()
    pid = next(p["proposal_id"] for p in c.get(f"/api/followup/staff/cases/{case['case_id']}").json()["proposals"]
               if p["requestable"] and p["route"] == "ask_patient")
    rid = c.post(f"/api/followup/staff/cases/{case['case_id']}/requests", json={"proposal_ids": [pid]}).json()["created"][0]
    url = f"/api/followup/cases/{case['case_id']}/requests/{rid}/response"
    assert c.post(url, json={"answer": "x"}, headers={"X-CTREC-Staff-Id": "demo-staff"}).status_code in (403, 404)
    assert c.post(url, json={"answer": "x"}).status_code == 404  # 환자 쿠키 없음


def test_only_the_demo_server_turns_demo_staff_on():
    """DEMO_STAFF를 켜는 곳은 오프라인 데모 서버 스크립트뿐 (환경 변수·설정으로 켤 수 없음)."""
    root = Path(__file__).resolve().parents[1]
    hits = [p.relative_to(root).as_posix() for d in ("src", "scripts") for p in (root / d).rglob("*.py")
            if re.search(r"DEMO_STAFF\s*=\s*True", p.read_text(encoding="utf-8"))]
    assert hits == ["scripts/followup_demo_server.py"]
    assert "DEMO_STAFF" not in (root / "src/ctrec/config.py").read_text(encoding="utf-8")
    assert not re.search(r"getenv\([^)]*DEMO", (root / "src/ctrec/web/followup_api.py").read_text(encoding="utf-8"))
