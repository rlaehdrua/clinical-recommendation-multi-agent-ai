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
    r = c.get("/staff")
    assert r.status_code == 200 and "default-src 'none'" in r.headers["content-security-policy"]
    assert '/static/staff.js' in r.text and "<script>" not in r.text and "style=" not in r.text
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
