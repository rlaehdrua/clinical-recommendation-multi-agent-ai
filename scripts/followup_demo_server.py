"""추가 정보 확인 API 오프라인 데모 서버 (프런트 개발·시연용). **실제 LLM·실제 환자·실제 시험을 쓰지 않습니다.**

- LLM: question_eval.OfflineEnvironment (실제 API 클라이언트·호출 차단, 규칙 기반 대역). 환자 정보 대역은 같은 항목이
  여러 번 나오면 가장 나중 언급을 씀 (확인 답변을 반영하는 실제 추출 흉내).
- 시험: 합성 NSCLC 기준표 3개 (ctrec.sim.demo: T-A, T-B, T-C). 구조화 기준(나이·ECOG·검사 수치)과 LLM 기준(EGFR) 하나.
- 저장: --store (기본 outputs/followup-demo). 서버를 끄고 다시 켜도 사례·요청·응답이 이어집니다.
- 연구 담당자 키: CTREC_STAFF_KEY (없으면 실행할 때마다 새로 만들어 화면에 출력).

실행:
    python scripts/followup_demo_server.py --port 8010
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


def synthetic_catalog() -> TrialCatalog:
    st = demo.trials()
    return TrialCatalog(name="sim-demo-nsclc", trials={t.parsed.trial_id: t.trial for t in st},
                        parsed={t.parsed.trial_id: t.parsed for t in st})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--store", type=Path, default=config.OUTPUT_DIR / "followup-demo")
    args = ap.parse_args()
    if args.host not in ("127.0.0.1", "::1", "localhost"):
        sys.exit("오프라인 데모 서버는 루프백 주소에서만 실행합니다.")
    if not os.getenv("CTREC_STAFF_KEY"):
        os.environ["CTREC_STAFF_KEY"] = secrets.token_urlsafe(16)
        print(f"CTREC_STAFF_KEY (이번 실행용): {os.environ['CTREC_STAFF_KEY']}")
    import uvicorn

    from ctrec.web import app as web
    from ctrec.web import followup_api

    env = qe.OfflineEnvironment(3).__enter__()  # 프로세스가 끝날 때까지 실제 LLM 호출 차단
    pipeline.patient_profiler.profile_patient = latest_mention_profiler
    web.TEST_LLM_BACKEND = "fake"  # 대역만 설치된 프로세스: 실제 호출 허가 게이트 대신 (환경 변수로는 켤 수 없음)
    followup_api.configure(FollowupService(CaseStore(args.store), synthetic_catalog()))
    print(f"followup demo (offline, synthetic): http://{args.host}:{args.port}  store={args.store}", flush=True)
    try:
        uvicorn.run(web.app, host=args.host, port=args.port, log_level="warning")
    finally:
        env.__exit__(None, None, None)


if __name__ == "__main__":
    main()
