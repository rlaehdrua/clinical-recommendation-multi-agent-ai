"""문서 검증·임상의 판단 완료 경로를 실제 HTTP로 재현 (오프라인 데모 서버 프로세스를 띄우고, 중간에 강제 종료 후 다시 띄움).

환자 입력(T-A, T-D) -> 담당자 질문 요청 -> 환자 답변 -> [재시작] -> 문서 검토 요청 -> 문서 등록 -> 권한 없는 검증 거부 ->
검증자 문서 확인 3건 -> T-A 확정 적격 -> 임상의 판단(근거 = 문서 확인 사실) -> T-D 확정 적격 -> [재시작] -> 상태·감사 기록 유지 ->
[서명 비밀이 다른 서버] -> 확정이 보이지 않음 -> [검증 모드 끔] -> 검증자 API 503.
합성 데이터·합성 시험 전용 권한만 씁니다 (운영 검증 권한 아님). 요청·응답 기록을 --out(JSON)으로 저장합니다.

    python scripts/followup_verification_flow.py --out outputs/followup_verification_transcript.json
"""

from __future__ import annotations

import argparse
import base64
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
# 환자는 처음 입력에서 혈소판 값을 말함(자기 보고). P 근거 경로 뒤 검사·ECOG(진료 기록 필요)는 환자 질문이 아니라 문서 확인
TEXT = "Synthetic NSCLC patient. 64-year-old male with stage IV non-small cell lung cancer. ECOG 1.\nPlatelets 180 x10^9/L"
REPORT = b"SYNTHETIC LAB REPORT (not a real patient)\nPlatelets 180 x10^9/L\nANC 2.1 x10^9/L\nECOG 1\n"
CE = "CONFIRMED_ELIGIBLE"


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, store: Path, env: dict):
        self.store, self.env, self.port, self.proc = store, env, _port(), None

    def start(self):
        env = {**os.environ, **self.env, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(ROOT / "src")}
        for k in [k for k in env if k.upper().startswith(("ANTHROPIC_", "OPENAI_"))]:
            env.pop(k)
        for k in [k for k, v in self.env.items() if v is None]:
            env.pop(k, None)
        args = [sys.executable, str(ROOT / "scripts" / "followup_demo_server.py"), "--port", str(self.port),
                "--store", str(self.store)]
        if self.env.get("CTREC_VERIFICATION_MODE"):
            args.append("--synthetic-verification")
        self.proc = subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(300):
            try:
                if httpx.get(f"http://127.0.0.1:{self.port}/healthz", timeout=0.5).status_code == 200:
                    return self
            except httpx.HTTPError:
                time.sleep(0.1)
        raise RuntimeError("demo server did not start")

    def stop(self):
        self.proc.kill()  # 정상 종료 없이 (저장은 요청마다 원자적으로 끝나 있어야 함)
        self.proc.wait(10)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    store = Path(tempfile.mkdtemp(prefix="followup-verify-"))
    keys = {"CTREC_STAFF_KEY": secrets.token_urlsafe(18), "CTREC_VERIFICATION_MODE": "synthetic",
            "CTREC_SYNTHETIC_DOC_VERIFIER_KEY": secrets.token_urlsafe(18),
            "CTREC_SYNTHETIC_CLINICIAN_KEY": secrets.token_urlsafe(18),
            "CTREC_SYNTHETIC_SIGNING_SECRET": secrets.token_urlsafe(24)}
    log: list[dict] = []
    checks: list[str] = []
    state = {"srv": Server(store, keys).start()}

    def clients():
        base = f"http://127.0.0.1:{state['srv'].port}"
        common = {"X-CTREC-Request": "1"}
        return {"patient": httpx.Client(base_url=base, headers=common, timeout=120),
                "staff": httpx.Client(base_url=base, timeout=120, headers={
                    **common, "X-CTREC-Staff-Key": keys["CTREC_STAFF_KEY"], "X-CTREC-Staff-Id": "coord-demo"}),
                "verifier": httpx.Client(base_url=base, timeout=120, headers={
                    **common, "X-CTREC-Verifier-Key": keys["CTREC_SYNTHETIC_DOC_VERIFIER_KEY"],
                    "X-CTREC-Verifier-Id": "verifier-demo"}),
                "clinician": httpx.Client(base_url=base, timeout=120, headers={
                    **common, "X-CTREC-Clinician-Key": keys["CTREC_SYNTHETIC_CLINICIAN_KEY"],
                    "X-CTREC-Clinician-Id": "dr-demo"}),
                "anonymous": httpx.Client(base_url=base, headers=common, timeout=120)}

    c = clients()
    cookies = {}

    def call(who, method, path, body=None, expect=None, raw=False):
        cl = c[who]
        if who == "patient":
            cl.cookies.update(cookies)
        r = cl.request(method, path, json=body)
        if who == "patient":
            cookies.update(dict(r.cookies))
        out = r.content if raw else r.json()
        shown = {k: v for k, v in (body or {}).items() if k != "content_base64"} if body else body
        log.append({"who": who, "method": method, "path": path, "body": shown, "status": r.status_code,
                    "response": None if raw else out})
        if expect is not None and r.status_code != expect:
            raise SystemExit(f"{who} {method} {path}: {r.status_code} {r.text[:500]}")
        return out

    def check(cond, msg):
        if not cond:
            raise SystemExit(f"FAILED: {msg}")
        checks.append(msg)

    def restart(env_update=None):
        state["srv"].stop()
        if env_update:
            keys.update(env_update)
        state["srv"] = Server(store, keys).start()
        c.update(clients())

    # 1. 환자 입력 -> 질문 요청 -> 답변
    case = call("patient", "POST", "/api/followup/cases", {"patient_text": TEXT, "trial_ids": ["T-A", "T-D"]}, 201)
    cid = case["case_id"]
    s = call("staff", "GET", f"/api/followup/staff/cases/{cid}", expect=200)
    check(s["pairs"]["T-D"]["status"] == "EXPERT_REVIEW", "T-D starts in EXPERT_REVIEW")
    clin_req = next(r for r in s["requests"] if r["kind"] == "clinician_review")
    check(not [p for p in s["proposals"] if p["action_type"] == "ASK_PATIENT" and p["info_key"].startswith("lab:")],
          "record-only lab items are not patient questions (P evidence routing)")
    check(s["pairs"]["T-A"]["status"] == "INFO_MISSING", "self-reported platelets and missing ANC leave T-A INFO_MISSING")

    restart()  # 2. 서버 강제 종료 후 재시작
    docs = [p["proposal_id"] for p in call("staff", "GET", f"/api/followup/staff/cases/{cid}", expect=200)["proposals"]
            if p["action_type"] == "REQUEST_DOCUMENT" and p["target_trials"] == ["T-A"]]
    made = call("staff", "POST", f"/api/followup/staff/cases/{cid}/requests", {"proposal_ids": docs}, 200)
    doc_reqs = {r["info_key"]: r["request_id"] for r in made["case"]["requests"] if r["kind"] == "document_review"}
    check(set(doc_reqs) == {"lab:platelets", "lab:absolute neutrophil count", "ecog"}, "three document reviews requested")
    doc = call("staff", "POST", f"/api/followup/staff/cases/{cid}/documents",
               {"kind": "lab_report", "media_type": "text/plain",
                "content_base64": base64.b64encode(REPORT).decode()}, 201)["document"]
    content = call("verifier", "GET", f"/api/followup/verifier/cases/{cid}/documents/{doc['document_id']}/content",
                   expect=200, raw=True)
    check(content == REPORT, "verifier downloads the registered document")

    # 3. 권한 없는 완료 시도
    vview = call("verifier", "GET", f"/api/followup/verifier/cases/{cid}", expect=200)
    plt_req = next(r for r in vview["requests"] if r["request_id"] == doc_reqs["lab:platelets"])
    plt_fact = next(f for f in plt_req["facts"] if f["field"] == "lab:platelets")
    body = {"document_id": doc["document_id"], "document_sha256": doc["sha256"], "items": [{"fact_id": plt_fact["fact_id"]}]}
    path = f"/api/followup/verifier/cases/{cid}/requests/{doc_reqs['lab:platelets']}/verification"
    for who in ("anonymous", "staff", "clinician"):
        r = call(who, "POST", path, body)
        check(r["detail"]["code"] == "verifier_forbidden", f"{who} cannot complete document verification")
    r = call("verifier", "POST", f"/api/followup/clinician/cases/{cid}/requests/{clin_req['request_id']}/judgment",
             {"decisions": [{"criterion": "T-D:I2", "decision": "MET", "rationale_code": "x"}]})
    check(r["detail"]["code"] == "clinician_forbidden", "verifier cannot record a clinician judgment")
    r = call("verifier", "POST", f"/api/followup/cases/{cid}/requests/{doc_reqs['lab:platelets']}/response", {"answer": "x"})
    check(r["detail"]["code"] == "staff_cannot_answer", "verifier cannot answer for the patient")

    # 4. 문서 확인 3건 -> 재평가
    out = call("verifier", "POST", path, body, 200)
    check(out["pairs"]["T-A"]["status"] != CE, "one verified lab is not enough")
    # ANC: 환자가 말한 적 없는 값 -> 문서에서 기록 (record). ECOG: 처음 입력의 값을 문서로 확인 (confirm)
    out = call("verifier", "POST", f"/api/followup/verifier/cases/{cid}/requests/{doc_reqs['lab:absolute neutrophil count']}"
               "/verification", {"document_id": doc["document_id"], "document_sha256": doc["sha256"],
                                 "items": [{"field": "lab:absolute neutrophil count", "value": 2.1, "unit": "x10^9/L",
                                            "observed_at": "2026-10-01"}]}, 200)
    check(out["pairs"]["T-A"]["status"] != CE, "two verified labs are not enough (ECOG unverified)")
    for key in ("ecog",):
        req = next(r for r in call("verifier", "GET", f"/api/followup/verifier/cases/{cid}", expect=200)["requests"]
                   if r["request_id"] == doc_reqs[key])
        fact = next(f for f in req["facts"] if f["field"] == key and not f["document_verified"])
        out = call("verifier", "POST", f"/api/followup/verifier/cases/{cid}/requests/{doc_reqs[key]}/verification",
                   {"document_id": doc["document_id"], "document_sha256": doc["sha256"],
                    "items": [{"fact_id": fact["fact_id"]}]}, 200)
    check(out["pairs"]["T-A"]["status"] == CE, "T-A CONFIRMED_ELIGIBLE after three document verifications")
    ecog_fact = fact["fact_id"]

    # 5. 임상의 판단 (근거 = 문서로 확인된 ECOG 사실)
    cview = call("clinician", "GET", f"/api/followup/clinician/cases/{cid}", expect=200)
    check(next(r for r in cview["requests"] if r["request_id"] == clin_req["request_id"])["judgeable"],
          "T-D:I2 review is judgeable")
    out = call("clinician", "POST", f"/api/followup/clinician/cases/{cid}/requests/{clin_req['request_id']}/judgment",
               {"decisions": [{"criterion": "T-D:I2", "decision": "MET", "rationale_code": "life_expectancy_adequate",
                               "basis_fact_ids": [ecog_fact]}], "expected_revision": cview["revision"]}, 200)
    check(out["pairs"]["T-D"]["status"] == CE, "T-D CONFIRMED_ELIGIBLE after clinician judgment")
    before = call("staff", "GET", f"/api/followup/staff/cases/{cid}", expect=200)
    insp = call("staff", "GET", f"/api/followup/staff/cases/{cid}/inspector", expect=200)
    check(len(insp["verification"]["verifications"]) == 3 and len(insp["verification"]["judgments"]) == 1,
          "Inspector shows verifications and judgment")
    queue = call("staff", "GET", "/api/followup/staff/queue", expect=200)
    check(not [i for i in queue["items"] if i["action_type"] == "REQUEST_DOCUMENT"
               and i["target_criteria"][0].startswith("T-A:")], "Cohort C queue drops resolved T-A document work")

    restart()  # 6. 재시작 뒤 유지
    after = call("staff", "GET", f"/api/followup/staff/cases/{cid}", expect=200)
    for k in ("revision", "pairs", "requests", "verifications", "judgments", "evaluations"):
        check(after[k] == before[k], f"{k} preserved across restart")
    audit = call("staff", "GET", f"/api/followup/staff/cases/{cid}/audit", expect=200)
    kinds = [e["event"]["kind"] for e in audit["entries"]]
    check(audit["intact"] and kinds.count("document_verified") == 3 and "clinician_judged" in kinds
          and kinds.count("denied") >= 1, "audit chain intact with verifications, judgment and denials")
    pv = call("patient", "GET", f"/api/followup/cases/{cid}", expect=200)
    check(pv["pairs"]["T-A"]["status"] == CE and "verifications" not in pv, "patient sees results, not verifier data")

    restart({"CTREC_SYNTHETIC_SIGNING_SECRET": secrets.token_urlsafe(24)})  # 7. 서명 비밀이 다른 서버
    changed = call("staff", "GET", f"/api/followup/staff/cases/{cid}", expect=200)
    check(changed["authority_changed"] and changed["pairs"]["T-A"]["status"] != CE
          and changed["pairs"]["T-D"]["status"] != CE, "different signing secret: stored confirmations not shown")
    restart({"CTREC_VERIFICATION_MODE": None})  # 8. 검증 모드 끔 (운영 기본값)
    r = call("verifier", "GET", f"/api/followup/verifier/cases/{cid}")
    check(r["detail"]["code"] == "verification_not_configured", "no verification mode: verifier API 503")
    r = call("staff", "GET", f"/api/followup/staff/cases/{cid}", expect=200)
    check(r["pairs"]["T-A"]["status"] != CE, "no verification mode: no confirmation from documents")
    state["srv"].stop()

    print(f"OK {len(checks)} checks, {len(log)} HTTP calls, case {cid}")
    for m in checks:
        print(" -", m)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({"checks": checks, "calls": log}, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
