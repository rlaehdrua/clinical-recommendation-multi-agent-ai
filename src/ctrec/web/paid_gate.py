"""웹 실제 LLM(유료) 호출 허가 (Phase 8). 기본은 꺼짐: API 키가 있어도 웹은 실제 호출을 하지 않습니다.

허가 조건 (모두 충족해야 함, 하나라도 빠지면 분석 시작 거부 503 - 클라이언트 생성·외부 요청 없음):
1. CTREC_WEB_PAID_ENABLED=1 (기본 0)
2. CTREC_WEB_METERING=enforce (record·off로는 실제 호출 불가)
3. enforce 설정 완비 (budget.load_config): 실행별·공용 한도, 가격 확인·가격 모델 = 호출 모델, 대체 모델 끔,
   영속 원장·단일 인스턴스 운영자 확인
4. 원장 정상 (budget.get_ledger: 파일 존재·무결성·다른 프로세스 미사용)
5. 접속 비밀번호 CTREC_PASSWORD 12자 이상, 평문 HTTP 허용(CTREC_ALLOW_INSECURE_HTTP) 꺼짐
6. ANTHROPIC_API_KEY 설정

이 모듈은 운영 환경을 조회하지 않습니다. 영속 디스크·단일 인스턴스는 운영자 확인 표시(환경 변수)로만 압니다.
Phase 5 파일럿 승인 게이트(llm_pilot.authorize, PAID_EXECUTION_ENABLED)와는 별개이며 그것을 바꾸지 않습니다.

반환하는 문제 코드는 설정 이름·상태 코드만이며 값(키·비밀번호)은 넣지 않습니다.
"""

from __future__ import annotations

import os

from .. import web_metering
from . import budget

ENV = "CTREC_WEB_PAID_ENABLED"
MIN_PASSWORD_CHARS = 12


def enabled_flag(env=None) -> bool:
    env = os.environ if env is None else env
    return (env.get(ENV) or "").strip() == "1"


def problems(env=None, default_run_seconds: float = 3600.0, password: str | None = None) -> list[str]:
    """실제 호출을 허가할 수 없는 이유 (빈 목록이면 허가). 스위치가 꺼져 있으면 다른 검사 없이 그 이유 하나만.

    password: 서버가 실제로 쓰는 접속 비밀번호(app.PASSWORD, 시작 시 읽은 값). 없으면 환경 변수."""
    env = os.environ if env is None else env
    if not enabled_flag(env):
        return ["web_paid_disabled"]
    p: list[str] = []
    if web_metering.mode() != "enforce":
        p.append("metering_not_enforce")
    try:
        cfg = budget.load_config(env, default_run_seconds=default_run_seconds)
    except budget.BudgetConfigError as e:
        p += e.problems
        cfg = None
    if cfg is not None and "metering_not_enforce" not in p:
        try:
            budget.get_ledger(cfg)
        except budget.LedgerError as e:
            p.append(e.args[0] if e.args else "ledger_unavailable")
    if len(password if password is not None else (env.get("CTREC_PASSWORD") or "")) < MIN_PASSWORD_CHARS:
        p.append("password_missing_or_short")
    if (env.get("CTREC_ALLOW_INSECURE_HTTP") or "").strip().lower() in ("1", "true", "yes", "on"):
        p.append("insecure_http_allowed")
    if not (env.get("ANTHROPIC_API_KEY") or "").strip():
        p.append("api_key_missing")
    return p
