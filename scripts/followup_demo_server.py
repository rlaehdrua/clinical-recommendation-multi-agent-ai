"""추가 정보 확인 API 오프라인 데모 서버 (프런트 개발·시연용). **실제 LLM·실제 환자·실제 시험을 쓰지 않습니다.**

- LLM: question_eval.OfflineEnvironment (실제 API 클라이언트·호출 차단, 규칙 기반 대역). 환자 정보 대역은 같은 항목이
  여러 번 나오면 가장 나중 언급을 씀 (확인 답변을 반영하는 실제 추출 흉내).
- 시험: 합성 NSCLC 기준표 3개 (ctrec.sim.demo: T-A, T-B, T-C). 구조화 기준(나이·ECOG·검사 수치)과 LLM 기준(EGFR) 하나.
- 저장: --store (기본 outputs/followup-demo). 서버를 끄고 다시 켜도 사례·요청·응답이 이어집니다.
- 연구 담당자 키: CTREC_STAFF_KEY (없으면 실행할 때마다 새로 만들어 화면에 출력).
- --synthetic-verification: 합성 시험 전용 문서 검증자·임상의 권한 (CTREC_VERIFICATION_MODE=synthetic).
  서명 비밀(CTREC_SYNTHETIC_SIGNING_SECRET)을 고정하지 않으면 재시작할 때마다 바뀌어 이전 증명·판단이 인정되지 않음.
  계약: docs/dev/document_clinician_api_contract.md
  --demo-staff: 발표용. 이 서버(루프백 전용)에서는 담당자 탭이 키 없이 바로 열림 (followup_api.DEMO_STAFF).
- --seed-demo: 발표용. 저장소가 비어 있으면 합성 NSCLC 환자 6명(ctrec.sim.demo SYN-01~06, 공개 사실만)을 신청된 상태로 넣음.
  연구진 화면이 처음부터 여러 환자를 함께 보도록 (한 명씩 신청하지 않음). 같은 순서·같은 글이라 녹화마다 숫자가 같음.
  미리 넣은 환자는 환자 화면 쿠키가 없어 환자로서 답할 수 없으므로, 환자 질문 장면은 환자 탭에서 한 명을 직접 신청해서 보여 줌.

실행:
    python scripts/followup_demo_server.py --port 8010 [--demo-staff] [--seed-demo]
    # 환자 화면: POST http://127.0.0.1:8010/api/followup/cases  (X-CTREC-Request: 1)
    # 담당자:    GET  http://127.0.0.1:8010/api/followup/staff/cases  (X-CTREC-Staff-Key: <키>)
계약: docs/dev/followup_api_contract.md
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ctrec import config, pipeline  # noqa: E402
from ctrec import question_eval as qe  # noqa: E402
from ctrec.followup.service import FollowupService, TrialCatalog  # noqa: E402
from ctrec.followup.store import CaseStore  # noqa: E402
from ctrec.sim import demo  # noqa: E402


def latest_mention_profiler(patient_id, raw_text, reference_date=None):
    return qe.stub_profiler(patient_id, "\n".join(reversed(raw_text.splitlines())), reference_date)


# 발표 화면용 이름 (합성 시험임을 이름에도 남김). 판정에 쓰는 기준·ID는 sim.demo 그대로
TITLES = {"T-A": "가상 시험 A · 혈액 수치·전신 상태 기준", "T-B": "가상 시험 B · EGFR 변이 대상",
          "T-C": "가상 시험 C · 75세 이하·혈색소 기준", "T-D": "가상 시험 D · 의료진 판단 기준 포함"}
# 발표용 예시 사례 (합성 NSCLC 환자, 실제 환자 아님). 분석은 영문 원문, 화면은 한국어 요약. 오프라인 대역이 읽는 표기만 씀
DEMO_PATIENTS = [
    {"patient_id": "DEMO-NSCLC-1", "text": "Synthetic NSCLC patient. 64-year-old male with stage IV non-small cell lung cancer, "
     "adenocarcinoma. ECOG 1.\nPlatelets 180 x10^9/L",
     "summary_ko": [["나이·성별", "64세 남성"], ["진단", "비소세포폐암 4기 (선암)"], ["전신 상태", "ECOG 1"], ["검사", "혈소판 180"]]},
    {"patient_id": "DEMO-NSCLC-2", "text": "Synthetic NSCLC patient. 58-year-old female with stage IV non-small cell lung cancer. ECOG 1.",
     "summary_ko": [["나이·성별", "58세 여성"], ["진단", "비소세포폐암 4기"], ["전신 상태", "ECOG 1"]]},
    {"patient_id": "DEMO-NSCLC-3", "text": "Synthetic NSCLC patient. 71-year-old male with stage IIIB non-small cell lung cancer. ECOG 0.",
     "summary_ko": [["나이·성별", "71세 남성"], ["진단", "비소세포폐암 3B기"], ["전신 상태", "ECOG 0"]]},
    # 나이를 적지 않은 환자: 연구진이 환자에게 직접 묻는 장면 (나이는 환자 답으로 확인할 수 있는 기준)
    {"patient_id": "DEMO-NSCLC-4", "text": "Synthetic NSCLC patient. Female with stage IV non-small cell lung cancer. ECOG 1.",
     "summary_ko": [["나이·성별", "여성 (나이 미기재)"], ["진단", "비소세포폐암 4기"], ["전신 상태", "ECOG 1"]]},
]


_ORIGINAL_PATIENTS = list  # main에서 원래 /api/patients 함수로 바꿈


def demo_patients() -> list[dict]:
    """발표용 합성 NSCLC 환자를 맨 앞에, 그 뒤에 원래 예시 사례 전체 (원래 목록을 지우지 않음)."""
    out = []
    for p in DEMO_PATIENTS:
        ko = " · ".join(v for _, v in p["summary_ko"]) + " (합성 환자)"
        out.append({"patient_id": p["patient_id"], "group": "demo", "text": p["text"], "text_ko": ko, "summary_ko": p["summary_ko"],
                    "candidate_trials": [], "has_hidden": False})
    return out + _ORIGINAL_PATIENTS()


def synthetic_catalog() -> TrialCatalog:
    st = [*demo.trials(), demo.expert_review_trial()]  # T-D: 임상의 판단 기준 (문서·임상의 완료 경로 시연)
    for t in st:  # 화면 이름만 바꿈 (기준·ID는 그대로)
        t.trial["title"] = TITLES.get(t.parsed.trial_id, t.trial.get("title"))
    st = [demo.SimTrial(t.parsed.model_copy(update={"title": t.trial["title"]}), t.trial, t.rule_meta) for t in st]
    return TrialCatalog(name="sim-demo-nsclc", trials={t.parsed.trial_id: t.trial for t in st},
                        parsed={t.parsed.trial_id: t.parsed for t in st})


SEED_TRIALS = ["T-A", "T-B", "T-C", "T-D"]


def seed_texts() -> list[tuple[str, str]]:
    """sim.demo 코호트(SYN-01~06)의 공개 사실만으로 쓴 소개글 (숨긴 값은 넣지 않음, 오프라인 대역이 읽는 영어 표기).

    미리 넣은 환자는 환자로서 답할 수 없으므로, 환자가 알 수 있는 ECOG가 공개 사실에 없으면 정답 값을 자기 보고로 적음
    (환자 질문 대신 서류 확인 장면이 되도록). 검사 수치·유전자 등 서류가 필요한 값은 넣지 않음."""
    from ctrec.sim import documents as D
    co = demo.cohort()
    out = []
    for p in co.patients:
        items = [D.DocItem(f.field, f.value, f.unit, f.observed_at, "self_report" if f.source_type == "self_report" else "normal")
                 for f in p.public]
        if not any(i.field == "ecog" for i in items):
            ecog = next((f for f in co.oracle.truth[p.key] if f.field == "ecog"), None)
            if ecog is not None:
                items.append(D.DocItem("ecog", ecog.value, None, None, "self_report"))
        out.append((p.key, D.template_narrative(items, set())))
    return out


def seed_demo(svc: FollowupService, gap: float = 1.05) -> int:
    """저장소가 비어 있을 때만 합성 환자를 순서대로 신청 상태로 넣음 (다시 켜도 중복되지 않음). 넣은 수."""
    import time

    from ctrec.web import access
    if svc.store.case_ids():
        return 0
    texts = seed_texts()
    for i, (_, text) in enumerate(texts):
        if i:
            time.sleep(gap)  # 신청 시각(초 단위)이 달라야 연구진 화면의 환자 번호·순서가 매번 같음
        svc.create_case(patient_text=text, trial_ids=SEED_TRIALS, input_source="self_report",
                        patient_token_digest=access.digest(access.new_token()), synthetic=True)
    return len(texts)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--store", type=Path, default=config.OUTPUT_DIR / "followup-demo")
    ap.add_argument("--synthetic-verification", action="store_true",
                    help="합성 시험 전용 문서 검증자·임상의 권한을 켬 (키가 환경 변수에 없으면 새로 만듦)")
    ap.add_argument("--demo-staff", action="store_true", help="발표용: 담당자 키 없이 담당자 탭을 엶 (루프백 접속만)")
    ap.add_argument("--seed-demo", action="store_true", help="발표용: 저장소가 비어 있으면 합성 NSCLC 환자 6명을 미리 넣음")
    args = ap.parse_args()
    if args.host not in ("127.0.0.1", "::1", "localhost"):
        sys.exit("오프라인 데모 서버는 루프백 주소에서만 실행합니다.")
    if not os.getenv("CTREC_STAFF_KEY"):
        os.environ["CTREC_STAFF_KEY"] = secrets.token_urlsafe(16)
        print(f"CTREC_STAFF_KEY (이번 실행용): {os.environ['CTREC_STAFF_KEY']}")
    if args.synthetic_verification:  # 합성 시험 전용 검증자·임상의 권한 (실제 운영 권한이 아님)
        os.environ["CTREC_VERIFICATION_MODE"] = "synthetic"
        for name in ("CTREC_SYNTHETIC_DOC_VERIFIER_KEY", "CTREC_SYNTHETIC_CLINICIAN_KEY", "CTREC_SYNTHETIC_SIGNING_SECRET"):
            if not os.getenv(name):
                os.environ[name] = secrets.token_urlsafe(24)
                if name != "CTREC_SYNTHETIC_SIGNING_SECRET":
                    print(f"{name} (이번 실행용): {os.environ[name]}")
    import uvicorn

    from ctrec.web import app as web
    from ctrec.web import followup_api

    env = qe.OfflineEnvironment(3).__enter__()  # 프로세스가 끝날 때까지 실제 LLM 호출 차단
    pipeline.patient_profiler.profile_patient = latest_mention_profiler
    web.TEST_LLM_BACKEND = "fake"  # 대역만 설치된 프로세스: 실제 호출 허가 게이트 대신 (환경 변수로는 켤 수 없음)
    svc = FollowupService(CaseStore(args.store), synthetic_catalog())
    followup_api.configure(svc)
    if args.seed_demo:
        n = seed_demo(svc)
        print(f"발표용 합성 환자 {n}명을 미리 넣었습니다." if n else "저장소에 사례가 있어 합성 환자를 다시 넣지 않았습니다.", flush=True)
    # 예시 사례 목록 맨 앞에 발표용 합성 NSCLC 환자를 더함 (이 프로세스에서만, 원래 예시 사례는 그 뒤에 그대로)
    global _ORIGINAL_PATIENTS
    _ORIGINAL_PATIENTS = web.list_patients
    web.app.router.routes = [r for r in web.app.router.routes if getattr(r, "path", None) != "/api/patients"]
    web.app.get("/api/patients")(demo_patients)
    if args.demo_staff:
        followup_api.DEMO_STAFF = True
        print("발표용 데모: 담당자 탭이 키 없이 열립니다 (이 컴퓨터에서만, 합성 데이터)", flush=True)
    print(f"followup demo (offline, synthetic): http://{args.host}:{args.port}  store={args.store}", flush=True)
    try:
        uvicorn.run(web.app, host=args.host, port=args.port, log_level="warning")
    finally:
        env.__exit__(None, None, None)


if __name__ == "__main__":
    main()
