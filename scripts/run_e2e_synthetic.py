"""끝까지 자동 실행: 합성 환자 입력 -> 연구진 요청 -> 환자 답 -> 가상 서류 -> 서류 확인 -> 의사 판단 -> 결과 확정 -> 채점.

    python scripts/run_e2e_synthetic.py --mode offline            # 틀(템플릿) 글, LLM 호출 0, 결정적
    python scripts/run_e2e_synthetic.py --mode llm --env-file .env # 환자 소개글·서류 작성·서류 읽기를 LLM으로
    [--patients SYN-01,SYN-03] [--missing 0.15] [--seed 1] [--budget 600] [--url http://127.0.0.1:8011] [--out DIR]

- 데이터: sim.demo 합성 NSCLC 코호트 6명(SYN-01~06, 공개 사실·정답 사실) x 가상 시험 T-A~D. 실제 환자·실제 시험 없음.
- 서버: 오프라인 데모 서버(scripts/followup_demo_server.py --demo-staff --synthetic-verification)를 새 저장소로 띄움
  (--url이면 이미 떠 있는 서버를 씀: 발표 화면에서 결과를 그대로 볼 수 있음). 서버 쪽 판정(Matcher)은 오프라인 대역.
  LLM은 이 스크립트에서만 쓰고(소개글·서류·서류 읽기), 정답 값은 LLM이 아니라 코호트 정답 사실에서 옴 (sim.documents).
- 역할은 서버 API 그대로: 담당자(데모 예외), 서류 확인 담당·의사(X-CTREC-Demo-Role, 루프백·합성 권한만), 환자(사례 쿠키).
  자동 대역: 담당자 = 추천 순서(Cohort C 대기열) 위에서부터, 같은 환자·같은 서류는 한 번에 / 환자 = 정답 값으로 답 /
  서류 = 정답 값으로 작성 (--missing 비율로 항목을 뺌) / 서류 확인 담당 = 서류를 읽어 값 확인·기록 (없으면 '서류 없음') /
  의사 = 판단 기준을 '맞음'으로 (판단 기준은 정답이 없어 따로 셈).
- 채점: 같은 평가기로 정답 사실 전체를 넣어 만든 규칙 GT(runner.rule_labels, 순환 GT). 확정이 GT와 반대면 안전 위반.
  분은 엔진 카탈로그의 가정 비용(측정 시간 아님). 결과는 합성 조건의 동작 확인이며 임상 정확도가 아닙니다.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from ctrec.engine.bundles import SourceCatalog  # noqa: E402
from ctrec.sim import demo, runner  # noqa: E402
from ctrec.sim import documents as D  # noqa: E402
from ctrec.sim.cohort import make_cohort  # noqa: E402

TRIALS = ["T-A", "T-B", "T-C", "T-D"]
HIDE_AGE = {"SYN-05"}  # 나이를 말하지 않은 환자 -> 연구진이 환자에게 직접 묻는 장면
WILD = "EGFR wild type (no sensitizing mutation)"


# ---------------------------------------------------------------------------
# 데이터
# ---------------------------------------------------------------------------

def cohort():
    """sim.demo 코호트 + 판단 기준 시험 T-D (서버의 데모 카탈로그와 같은 시험)."""
    base = demo.cohort()
    trials = [*base.trials, demo.expert_review_trial()]
    pats = [(p.key, list(p.public), list(base.oracle.truth[p.key]), None) for p in base.patients]
    return make_cohort("e2e-demo", base.reference_date, trials, pats, base.salt)


def items_of(facts, scenario=None) -> list[D.DocItem]:
    return [D.DocItem(f.field, f.value, f.unit, f.observed_at, scenario or ("self_report" if f.source_type == "self_report" else "normal"))
            for f in facts]


def truth_value(truth: dict, field: str):
    if field == "biomarker":
        return truth.get("biomarker") or D.DocItem("biomarker", WILD)
    return truth.get(field)


def same_value(read, truth) -> bool:
    """읽은 값이 정답과 같은가 (숫자는 값, 글은 읽은 낱말이 모두 정답 문구에 있음: 'EGFR wild type' ~ 'EGFR wild type (no ...)')."""
    if isinstance(truth, (int, float)) and not isinstance(truth, bool):
        try:
            return abs(float(read) - float(truth)) < 1e-9
        except (TypeError, ValueError):
            return False
    words = lambda x: set(re.findall(r"[a-z0-9]+", str(x).lower()))  # noqa: E731
    return bool(words(read)) and words(read) <= words(truth)


def chance(seed: int, *parts) -> float:
    h = hashlib.sha256(json.dumps([seed, *parts]).encode()).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


# ---------------------------------------------------------------------------
# 서버
# ---------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(store: Path, log: Path) -> tuple[subprocess.Popen, str]:
    port = free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CTREC_", "ANTHROPIC_"))}  # 서버는 오프라인: 키를 넘기지 않음
    env["CTREC_SYNTHETIC_SIGNING_SECRET"] = secrets.token_urlsafe(24)
    proc = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "followup_demo_server.py"), "--port", str(port), "--store", str(store),
                             "--demo-staff", "--synthetic-verification"], env=env, stdout=log.open("w"), stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            if httpx.get(url + "/healthz", timeout=1).status_code == 200:
                return proc, url
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    proc.kill()
    raise SystemExit(f"데모 서버가 뜨지 않음 (로그: {log})")


class Api:
    def __init__(self, url: str):
        self.url = url
        self.h = {"Origin": url, "X-CTREC-Request": "1"}
        self.staff = httpx.Client(base_url=url, timeout=120)

    def call(self, c: httpx.Client, method: str, path: str, body=None, role: str | None = None, ok=(200, 201)):
        hd = dict(self.h)
        if role:
            hd["X-CTREC-Demo-Role"] = role
        r = c.request(method, "/api/followup" + path, json=body, headers=hd)
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {path} -> {r.status_code} {r.text[:300]}")
        return r.json()

    def s(self, method, path, body=None):
        return self.call(self.staff, method, "/staff" + path, body)

    def role(self, role, method, path, body=None):
        return self.call(self.staff, method, path, body, role=role)


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------

def run(args) -> dict:
    out = Path(args.out)
    (out / "docs").mkdir(parents=True, exist_ok=True)
    use_llm = args.mode == "llm"
    if use_llm:
        from dotenv import load_dotenv
        if args.env_file:
            load_dotenv(args.env_file, override=False)  # 키 값은 읽기만 하고 출력·저장하지 않음
        from ctrec import config, llm
        config.MODEL = os.getenv("CTREC_MODEL", config.MODEL)
        llm.reset_usage()
        model = config.MODEL
    else:
        model = ""
    cache = D.Cache(Path(args.cache) if args.cache else out / "cache")
    co = cohort()
    keys = [k for k in (args.patients.split(",") if args.patients else [p.key for p in co.patients])]
    truth = {p.key: {f.field: D.DocItem(f.field, f.value, f.unit, f.observed_at) for f in co.oracle.truth[p.key]} for p in co.patients}
    public = {p.key: items_of(p.public) for p in co.patients}
    labels = runner.rule_labels(co, SourceCatalog())
    alias = co.aliases()

    proc = None
    if args.url:
        url = args.url
    else:
        proc, url = start_server(out / "store", out / "server.log")
    api = Api(url)
    log, stats = [], Counter()
    t0 = time.time()
    try:
        # 1. 환자 입력 (소개글은 공개 사실만, 검사기 통과)
        cases, patients, written = {}, {}, {}
        for k in keys:
            hide = {"age"} if k in HIDE_AGE else set()
            hidden = [i for f, i in truth[k].items() if f not in {x.field for x in public[k]}]
            w = D.write_narrative(k, public[k], hide, hidden, use_llm=use_llm, cache=cache, model=model)
            stats[f"narrative_{w.by}"] += 1
            written[k] = w.text
            (out / "docs" / f"{k}_intro.txt").write_text(w.text, encoding="utf-8")
            pc = httpx.Client(base_url=url, timeout=120)
            case = api.call(pc, "POST", "/cases", {"patient_text": w.text, "trial_ids": TRIALS, "input_source": "self_report"})
            cases[k], patients[k] = case["case_id"], pc
            log.append({"step": "intake", "patient": k, "by": w.by, "attempts": w.attempts, "problems": w.problems})
        who = {v: k for k, v in cases.items()}

        spent = 0.0
        for rnd in range(1, args.max_rounds + 1):
            acted = 0
            # 2. 담당자: 추천 순서 위에서부터, 같은 환자·같은 서류는 한 번에 (예산 안에서)
            ins = api.s("GET", "/insight")
            spent = ins["summary"].get("spent_minutes", spent) if ins["summary"].get("cases") else spent
            if spent < args.budget:
                q = api.s("GET", "/queue")["items"]
                todo = [x for x in q if x["case_id"] in who and x.get("route") and x["route"] != "clinician_review" and not x.get("request_status")]
                groups = []
                for x in todo:
                    g = (x["case_id"], x["source_id"])
                    if g not in groups:
                        groups.append(g)
                for cid, src in groups[:args.per_round]:
                    ids = [x["proposal_id"] for x in todo if (x["case_id"], x["source_id"]) == (cid, src)]
                    r = api.s("POST", f"/cases/{cid}/requests", {"proposal_ids": ids})
                    acted += len(r["created"])
                    stats["requests_sent"] += len(r["created"])
                    log.append({"step": "request", "round": rnd, "patient": who[cid], "source": src, "items": len(ids)})
            # 3. 환자: 받은 질문에 정답 값으로 답 (나이는 화면과 같은 형식)
            for k, cid in cases.items():
                for rq in api.call(patients[k], "GET", f"/cases/{cid}/requests")["requests"]:
                    if not rq["answerable"]:
                        continue
                    t = truth[k].get(rq.get("info_key") or "")
                    body = {"answer": f"{D.fmt(t.value)} years old (만 {D.fmt(t.value)}세)"} if rq.get("info_key") == "age" and t else {"unknown": True}
                    api.call(patients[k], "POST", f"/cases/{cid}/requests/{rq['request_id']}/response", body)
                    acted += 1
                    stats["patient_answers" if "answer" in body else "patient_unknown"] += 1
                    log.append({"step": "patient_answer", "round": rnd, "patient": k, "item": rq.get("info_key"), "unknown": "unknown" in body})
            # 4. 서류: 환자·서류 종류마다 한 장 (열린 서류 요청의 항목을 모두 담음) -> 등록 -> 서류 확인 담당이 읽고 확인
            for k, cid in cases.items():
                v = api.role("verifier", "GET", f"/verifier/cases/{cid}")
                open_reqs = [r for r in v["requests"] if r["status"] == "requires_document" and r["verifiable"]]
                by_src: dict[str, list] = {}
                for r in open_reqs:
                    by_src.setdefault(r["source_id"], []).append(r)
                for src, reqs in by_src.items():
                    fields = []
                    for r in reqs:
                        for f in r["allowed_fields"]:
                            if f not in D.FIELDS:  # 사실 항목이 아닌 요청 키(basis:age, biomarker:EGFR 등)는 서류에 쓰지 않음
                                continue
                            if f not in fields:
                                fields.append(f)
                    items = []
                    for f in fields:
                        t = truth_value(truth[k], f)
                        if t is None:
                            items.append(D.DocItem(f, None, scenario="missing"))
                            continue
                        missing = chance(args.seed, k, f) < args.missing
                        items.append(D.DocItem(f, None if missing else t.value, t.unit, t.observed_at, "missing" if missing else "normal"))
                    w = D.write_document(k, src, items, co.reference_date, use_llm=use_llm, cache=cache, model=model)
                    stats[f"document_{w.by}"] += 1
                    name = f"{k}_{src}_r{rnd}.txt"
                    (out / "docs" / name).write_text(w.text, encoding="utf-8")
                    doc = api.s("POST", f"/cases/{cid}/documents", {"kind": src if src in D.SOURCES and src != "medical_record" else "clinical_note",
                                                                     "media_type": "text/plain",
                                                                     "content_base64": base64.b64encode(w.text.encode()).decode()})["document"]
                    read = D.llm_extract(w.text, fields) if use_llm else D.template_extract(w.text, fields)
                    for f in fields:  # 읽은 값과 정답 비교 (기록만, 판정에 쓰지 않음)
                        it = next(i for i in items if i.field == f)
                        got = read.get(f)
                        ok = (got is None) == (it.value is None) and (got is None or same_value(got["value"], it.value))
                        stats["read_ok" if ok else "read_wrong"] += 1
                    log.append({"step": "document", "round": rnd, "patient": k, "source": src, "file": name, "by": w.by,
                                "attempts": w.attempts, "problems": w.problems, "items": [(i.field, i.scenario) for i in items],
                                "read": {f: (g or {}).get("value") for f, g in read.items()}})
                    for r in reqs:
                        v = api.role("verifier", "GET", f"/verifier/cases/{cid}")
                        r = next((x for x in v["requests"] if x["request_id"] == r["request_id"]), None)
                        if r is None or r["status"] != "requires_document":  # 앞의 확인으로 이미 닫힘·대체됨
                            continue
                        sub = []
                        for f in [x for x in r["allowed_fields"] if x in fields]:
                            got = read.get(f)
                            if got is None:
                                continue
                            fact = next((x for x in r["facts"] if x["field"] == f and not x["document_verified"]
                                         and D._num_in(str(x["value"]), got["value"])), None)
                            if fact:
                                sub.append({"fact_id": fact["fact_id"]})
                            else:
                                it = {"field": f, "value": got["value"]}
                                if got.get("unit"):
                                    it["unit"] = got["unit"]
                                if got.get("observed_at"):
                                    it["observed_at"] = got["observed_at"]
                                sub.append(it)
                        if not sub:  # 서류에 그 항목이 없음 -> 담당자가 '서류 없음'으로 기록 (서버가 다른 경로를 찾음)
                            api.s("POST", f"/cases/{cid}/requests/{r['request_id']}/status", {"status": "document_unavailable"})
                            stats["document_unavailable"] += 1
                            log.append({"step": "document_unavailable", "round": rnd, "patient": k, "item": r["info_key"]})
                        else:
                            res = api.role("verifier", "POST", f"/verifier/cases/{cid}/requests/{r['request_id']}/verification",
                                           {"document_id": doc["document_id"], "document_sha256": doc["sha256"], "items": sub,
                                            "expected_revision": v["revision"]})
                            stats["verified_requests"] += 1
                            log.append({"step": "verified", "round": rnd, "patient": k, "item": r["info_key"],
                                        "modes": [("confirm" if "fact_id" in x else "record") for x in sub],
                                        "transitions": res["transitions"]})
                        acted += 1
            # 5. 의사: 판단 기준 요청을 서류로 확인된 근거로 판단 (근거가 아직 없으면 다음 회차)
            for k, cid in cases.items():
                c = api.role("clinician", "GET", f"/clinician/cases/{cid}")
                basis = [f["fact_id"] for f in c["facts"] if f["document_verified"]]
                for r in c["requests"]:
                    if r["status"] != "requires_clinician" or not r["judgeable"] or not basis:
                        continue
                    res = api.role("clinician", "POST", f"/clinician/cases/{cid}/requests/{r['request_id']}/judgment",
                                   {"decisions": [{"criterion": x["criterion"], "decision": args.clinician, "rationale_code": "synthetic_e2e_judgment",
                                                   "basis_fact_ids": basis} for x in r["criteria"]], "expected_revision": c["revision"]})
                    c = api.role("clinician", "GET", f"/clinician/cases/{cid}")
                    acted += 1
                    stats["clinician_judgments"] += 1
                    log.append({"step": "judged", "round": rnd, "patient": k, "transitions": res["transitions"]})
            log.append({"step": "round_end", "round": rnd, "acted": acted})
            if not acted:
                break

        # 6. 채점
        ins = api.s("GET", "/insight")
        rows, score = [], Counter()
        for k, cid in cases.items():
            d = api.s("GET", f"/cases/{cid}")
            for t in TRIALS:
                st = d["pairs"][t]["status"]
                gt = labels.get(f"{alias[k]}|{t}", "unlabeled")
                verdict = ("correct_confirmed" if st == "CONFIRMED_ELIGIBLE" and gt == "eligible"
                           else "confirmed_after_clinician" if st == "CONFIRMED_ELIGIBLE" and gt == "eligible_pending_expert"
                           else "violation_false_eligible" if st == "CONFIRMED_ELIGIBLE"
                           else "correct_ineligible" if st == "CONFIRMED_INELIGIBLE" and gt == "ineligible"
                           else "violation_false_ineligible" if st == "CONFIRMED_INELIGIBLE"
                           else "unconfirmed")
                score[verdict] += 1
                rows.append({"patient": k, "trial": t, "status": st, "gt": gt, "verdict": verdict})
        usage = {}
        if use_llm:
            from ctrec import llm
            usage = llm.usage_snapshot()
        report = {"mode": args.mode, "model": model or None, "patients": keys, "trials": TRIALS, "seed": args.seed, "missing_rate": args.missing,
                  "clinician_decision": args.clinician, "budget_minutes": args.budget, "spent_minutes": ins["summary"].get("spent_minutes"),
                  "rounds": rnd, "seconds": round(time.time() - t0, 1), "score": dict(score), "stats": dict(stats), "llm_usage": usage,
                  "safety_violations": score["violation_false_eligible"] + score["violation_false_ineligible"],
                  "gt": "rule_gt_circular (같은 평가기에 정답 사실 전체, 판단 기준은 eligible_pending_expert)",
                  "claims": "합성 환자·가상 시험의 동작 확인. 임상 정확도 아님. 분은 가정 비용", "pairs": rows, "server": url}
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        (out / "log.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in log) + "\n", encoding="utf-8")
        return report
    finally:
        if proc is not None and not args.keep_server:
            proc.terminate()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--mode", choices=["offline", "llm"], default="offline")
    ap.add_argument("--env-file", default=None, help="LLM 키가 든 .env 경로 (값은 출력하지 않음)")
    ap.add_argument("--patients", default=None, help="쉼표로 (기본: SYN-01~06 전부)")
    ap.add_argument("--missing", type=float, default=0.15, help="서류에서 항목을 빼는 비율 (안전 경로 시험)")
    ap.add_argument("--clinician", choices=["MET", "NOT_MET"], default="MET")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--budget", type=float, default=600, help="담당자 요청 예산 (가정 분)")
    ap.add_argument("--per-round", type=int, default=6, help="회차마다 보내는 (환자, 서류) 묶음 수")
    ap.add_argument("--max-rounds", type=int, default=12)
    ap.add_argument("--url", default=None, help="이미 떠 있는 데모 서버 (없으면 새로 띄움)")
    ap.add_argument("--keep-server", action="store_true")
    ap.add_argument("--cache", default=None, help="LLM 글 저장 폴더 (기본: <out>/cache)")
    ap.add_argument("--out", default=str(ROOT / "outputs" / "e2e" / time.strftime("%Y%m%d-%H%M%S")))
    args = ap.parse_args()
    r = run(args)
    s = r["score"]
    print(f"[{r['mode']}] 환자 {len(r['patients'])}명 x 시험 {len(r['trials'])}개, {r['rounds']}회차, {r['seconds']}초, 보낸 요청 {r['stats'].get('requests_sent', 0)}건"
          f" (가정 {r['spent_minutes']}분)")
    print(f"  참여 가능 확정: 정답과 일치 {s.get('correct_confirmed', 0)} + 의사 판단 뒤 {s.get('confirmed_after_clinician', 0)}"
          f" / 참여 불가 확정 {s.get('correct_ineligible', 0)} / 미확정 {s.get('unconfirmed', 0)} / 안전 위반 {r['safety_violations']}")
    print(f"  글: {dict((k, v) for k, v in r['stats'].items() if k.startswith(('narrative_', 'document_')))}"
          f" · 서류 읽기 정확 {r['stats'].get('read_ok', 0)} / 틀림 {r['stats'].get('read_wrong', 0)}")
    if r["llm_usage"]:
        u = r["llm_usage"]
        print(f"  LLM: 호출 {u.get('calls', 0)}회, 입력 {u.get('input_tokens', 0)} / 출력 {u.get('output_tokens', 0)} 토큰")
    print(f"  결과: {args.out}/report.json")
    return 1 if r["safety_violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
