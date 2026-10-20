"""웹 실행별 접근 권한 (실행을 만든 브라우저만 이벤트 조회·답변·취소).

설계
- 실행을 만들 때 실행마다 새 소유권 토큰(256비트 난수)을 발급해 쿠키로만 전달합니다.
  쿠키 속성: HttpOnly(스크립트가 읽지 못함), SameSite=Strict(다른 사이트에서 보낸 요청에는 붙지 않음),
  Path=/api/runs/<run_id>(그 실행의 API에만 전송 -> 여러 탭·여러 실행의 토큰이 섞이지 않음),
  Secure(서버가 실제로 HTTPS로 받은 요청일 때. CTREC_COOKIE_SECURE=auto(기본)/1/0), Max-Age(실행 보관 기간).
- HTTPS·프록시 신뢰 경계: 이 모듈은 X-Forwarded-Proto 같은 프록시 헤더를 직접 읽지 않습니다. 요청의 scheme과
  클라이언트 주소는 uvicorn이 신뢰하는 프록시(--forwarded-allow-ips, 기본 127.0.0.1)에서 온 헤더만 반영한 값입니다.
  루프백(127.0.0.1, ::1)이 아닌 곳에서 온 평문 HTTP 요청은 거절합니다(transport_allowed). 운영에서 프록시 설정이
  빠져 HTTPS가 인식되지 않으면 조용히 평문으로 동작하지 않고 실패합니다. 사내망 시연 등에서 평문 HTTP를 일부러
  허용하려면 CTREC_ALLOW_INSECURE_HTTP=1 (이때 쿠키는 Secure가 아니며 도청 위험이 있음).
  운영 배포는 CTREC_REQUIRE_HTTPS=1(HTTPS만, 예외 없음)과 CTREC_COOKIE_SECURE=1(쿠키 항상 Secure)을 함께 씁니다.
- 서버는 토큰의 SHA-256만 실행 정보에 보관하고 평문은 저장하지 않습니다. 실행 기록이 지워지면 토큰도 무효가 됩니다.
- 토큰은 URL·SSE 이벤트·응답 본문·오류 메시지·로그에 넣지 않습니다(EventSource는 같은 출처 쿠키를 자동으로 보냄).
- 검증 실패와 없는 실행은 같은 404 응답이라 다른 실행의 존재 여부가 드러나지 않습니다.
- 상태를 바꾸는 POST에는 사용자 정의 헤더(X-CTREC-Request)를 요구하고, Origin 헤더가 있으면 서버 주소와 같아야 합니다
  (다른 사이트의 폼·스크립트가 쿠키를 이용해 요청하는 CSRF 차단. 사용자 정의 헤더는 CORS 사전 요청 없이 보낼 수 없음).
- 브라우저 식별 쿠키(ctrec_client, 난수)로 같은 브라우저의 동시·시간당 실행 수를 제한합니다(쿠키를 지우면 우회 가능 ->
  전체 동시 실행 상한이 함께 적용됨).
이 방식은 '이 실행을 만든 브라우저'만 확인하며 사용자 신원 확인(계정 인증)을 대신하지 않습니다.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import secrets
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response

RUN_COOKIE = "ctrec_run"
CLIENT_COOKIE = "ctrec_client"
CSRF_HEADER = "x-ctrec-request"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def matches(token: str | None, expected_digest: str | None) -> bool:
    if not token or not expected_digest:
        return False
    return hmac.compare_digest(digest(token), expected_digest)


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def is_https(request: Request) -> bool:
    """서버가 실제로 HTTPS로 받았는가. 프록시 헤더는 uvicorn이 신뢰하는 프록시에서 온 것만 scheme에 반영됨."""
    return request.url.scheme == "https"


def is_loopback(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def transport_allowed(request: Request) -> bool:
    """HTTPS, 같은 컴퓨터(루프백)의 개발용 HTTP, 또는 명시적으로 허용한 평문 HTTP만.

    운영(CTREC_REQUIRE_HTTPS=1)에서는 HTTPS만 허용: 모든 프록시를 신뢰하는 배포(--forwarded-allow-ips '*')에서는
    X-Forwarded-For로 루프백 주소를 꾸밀 수 있으므로 루프백 예외와 평문 허용 설정을 쓰지 않음.
    """
    if is_https(request):
        return True
    if _flag("CTREC_REQUIRE_HTTPS"):
        return False
    return is_loopback(request) or _flag("CTREC_ALLOW_INSECURE_HTTP")


def _secure(request: Request) -> bool:
    mode = os.getenv("CTREC_COOKIE_SECURE", "auto").strip().lower()
    if mode in ("1", "true", "yes", "on"):
        return True
    if mode in ("0", "false", "no", "off"):
        return False
    # auto: 서버가 HTTPS로 받은 요청일 때만 Secure. 클라이언트가 보낸 X-Forwarded-Proto는 직접 보지 않음
    return is_https(request)


def set_cookie(response: Response, request: Request, name: str, value: str, path: str, max_age: int) -> None:
    response.set_cookie(name, value, max_age=max_age, path=path, httponly=True, samesite="strict",
                        secure=_secure(request))


def check_state_changing(request: Request) -> None:
    """상태를 바꾸는 POST의 CSRF 방어: 사용자 정의 헤더 + Origin 확인."""
    if request.headers.get(CSRF_HEADER) != "1":
        raise HTTPException(403, "요청을 확인할 수 없습니다. 화면을 새로고침한 뒤 다시 시도하세요.")
    origin = request.headers.get("origin")
    if origin and origin != "null":
        if urlsplit(origin).netloc.lower() != request.headers.get("host", "").strip().lower():
            raise HTTPException(403, "다른 사이트에서 보낸 요청은 처리하지 않습니다.")
    elif origin == "null":
        raise HTTPException(403, "다른 사이트에서 보낸 요청은 처리하지 않습니다.")
