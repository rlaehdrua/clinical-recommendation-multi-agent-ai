"""추가 정보 확인 흐름을 실제 HTTP로 재현 (오프라인 데모 서버를 띄우고, 중간에 서버 프로세스를 끄고 다시 띄움).

환자 입력 -> 담당자 검토·요청 -> [서버 재시작] -> 환자 확인·답변 -> 중복 재전송 -> '모름' -> 대체 경로 ->
충돌 답변 -> 보류 -> [서버 재시작] -> 상태 유지 확인 -> 갱신된 추천·Inspector·Cohort C 대기열.
합성 데이터만 씁니다. 요청·응답 기록을 --out(JSON)으로 저장합니다 (프런트 계약 문서의 예시 출처).

    python scripts/followup_demo_flow.py --out outputs/followup_demo_transcript.json
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
TEXT = "Synthetic NSCLC patient, male, stage IV non-small cell lung cancer. ECOG 1."


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, store: Path, key: str):
        self.store, self.key, self.port, self.proc = store, key, _port(), None

    def start(self):
        env = {**os.environ, "CTREC_STAFF_KEY": self.key, "PYTHONIOENCODING": "utf-8",
               "PYTHONPATH": str(ROOT / "src")}
        for k in [k for k in env if k.upper().startswith(("ANTHROPIC_", "OPENAI_"))]:
            env.pop(k)
        self.proc = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "followup_demo_server.py"),
                                      "--port", str(self.port), "--store", str(self.store)], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(200):
            try:
                if httpx.get(f"http://127.0.0.1:{self.port}/healthz", timeout=0.5).status_code == 200:
                    return self
            except httpx.HTTPError:
                time.sleep(0.1)
        raise RuntimeError("demo server did not start")

    def stop(self):
        self.proc.kill()  # 정상 종료 절차 없이 끝냄 (저장은 요청마다 원자적으로 끝나 있어야 함)
        self.proc.wait(10)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    store = Path(tempfile.mkdtemp(prefix="followup-demo-"))
    key = secrets.token_urlsafe(16)
    srv = Server(store, key).start()
    base = f"http://127.0.0.1:{srv.port}"
    patient = httpx.Client(base_url=base, headers={"X-CTREC-Request": "1"}, timeout=60)
    staff = httpx.Client(base_url=base, headers={"X-CTREC-Request": "1", "X-CTREC-Staff-Key": key,
                                                 "X-CTREC-Staff-Id": "coord-demo"}, timeout=60)
    log: list[dict] = []

    def call(who, method, path, body=None, expect=None):
        c = patient if who == "patient" else staff
        r = c.request(method, path, json=body)
        entry = {"who": who, "method": method, "path": path, "body": body, "status": r.status_code, "response": r.json()}
        log.append(entry)
        if expect is not None and r.status_code != expect:
            raise SystemExit(f"{method} {path}: {r.status_code} {r.text[:400]}")
        return r.json()

    case = call("patient", "POST", "/api/followup/cases", {"patient_text": TEXT, "trial_ids": ["T-A", "T-B", "T-C"]}, 201)
    cid = case["case_id"]
    review = call("staff", "GET", f"/api/followup/staff/cases/{cid}", expect=200)
    pick = {p["info_key"]: p["proposal_id"] for p in review["proposals"] if p["action_type"] == "ASK_PATIENT"}
    made = call("staff", "POST", f"/api/followup/staff/cases/{cid}/requests",
                {"proposal_ids": [pick["age"], pick["lab:platelets"], pick["lab:hemoglobin"], pick["biomarker:EGFR"]]}, 200)
    call("staff", "POST", f"/api/followup/staff/cases/{cid}/requests", {"proposal_ids": [pick["lab:platelets"]]}, 200)

    srv.stop()
    srv.start()  # 서버 재시작 1 (같은 포트, 같은 저장 위치, 같은 브라우저 쿠키)
    reqs = call("patient", "GET", f"/api/followup/cases/{cid}/requests", expect=200)
    assert reqs["pending"] == 4, reqs
    by_key = {r["info_key"]: r["request_id"] for r in made["case"]["requests"]}
    call("patient", "POST", f"/api/followup/cases/{cid}/requests/{by_key['age']}/response",
         {"answer": "I am 80 years old"}, 200)
    call("patient", "POST", f"/api/followup/cases/{cid}/requests/{by_key['lab:platelets']}/response",
         {"answer": "Platelets 180 x10^9/L", "client_response_id": "ui-1"}, 200)
    dup = call("patient", "POST", f"/api/followup/cases/{cid}/requests/{by_key['lab:platelets']}/response",
               {"answer": "Platelets 180 x10^9/L", "client_response_id": "ui-1"}, 200)
    assert dup["duplicate"] is True
    call("patient", "POST", f"/api/followup/cases/{cid}/requests/{by_key['lab:platelets']}/response",
         {"answer": "Platelets 95 x10^9/L"}, 409)
    call("patient", "POST", f"/api/followup/cases/{cid}/requests/{by_key['lab:hemoglobin']}/response", {"unknown": True}, 200)
    call("patient", "POST", f"/api/followup/cases/{cid}/requests/{by_key['biomarker:EGFR']}/response",
         {"answer": "EGFR exon 19 deletion.\nMy ECOG performance status is 3 now."}, 200)
    call("patient", "POST", f"/api/followup/cases/{cid}/requests/FR-0000000000000000/response", {"answer": "x"}, 404)
    call("staff", "POST", f"/api/followup/cases/{cid}/requests/{by_key['biomarker:EGFR']}/response", {"answer": "x"}, 403)  # 담당자 대리 기입 거절
    before = call("staff", "GET", f"/api/followup/staff/cases/{cid}", expect=200)

    srv.stop()
    srv.start()  # 서버 재시작 2
    after = call("staff", "GET", f"/api/followup/staff/cases/{cid}", expect=200)
    for k in ("revision", "requests", "responses", "evaluations", "pairs", "proposals"):
        assert after[k] == before[k], k
    call("patient", "GET", f"/api/followup/cases/{cid}", expect=200)
    call("patient", "GET", f"/api/followup/cases/{cid}/requests", expect=200)
    call("staff", "GET", "/api/followup/staff/queue", expect=200)
    insp = call("staff", "GET", f"/api/followup/staff/cases/{cid}/inspector", expect=200)
    srv.stop()

    summary = {
        "case_id": cid, "revision": after["revision"],
        "pairs_initial": {t: p["status"] for t, p in review["pairs"].items()},
        "pairs_final": {t: p["status"] for t, p in after["pairs"].items()},
        "requests": [(r["kind"], r["status"], r["info_key"], r["created_by"]) for r in after["requests"]],
        "equivalence": [e["equivalence"]["status"] for e in after["evaluations"]],
        "transitions": [t for e in after["evaluations"] for t in e["transitions"]],
        "recommended_initial": [x["trial_id"] for x in case["view"]["result"]["recommendation"]["ranked"]],
        "recommended_final": [x["trial_id"] for x in after["view"]["result"]["recommendation"]["ranked"]],
        "inspector_sections": sorted(insp),
        "http_calls": len(log), "server_restarts": 2,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({"summary": summary, "calls": log}, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
