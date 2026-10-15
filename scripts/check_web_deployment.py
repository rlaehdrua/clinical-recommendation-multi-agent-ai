"""웹 배포 사전 점검 (Phase 6-4). 설정 파일·환경 변수·원장 파일만 읽습니다. LLM API·Render API를 부르지 않습니다.

    PYTHONPATH=src python scripts/check_web_deployment.py                       # render.yaml 기준, 목표는 render.yaml의 모드
    PYTHONPATH=src python scripts/check_web_deployment.py --target enforce --env-file deploy/web-enforce.env.example
    PYTHONPATH=src python scripts/check_web_deployment.py --process-env --probe  # 서버가 도는 환경 안에서 (예: Render Shell)

결과: PASS(확인됨) / WARNING(배포는 되지만 위험) / BLOCKED(이대로 배포하면 안 됨) / UNVERIFIED(이 도구로 확인할 수 없음)
종료 코드: BLOCKED가 하나라도 있으면 2, 아니면 0.

이 도구는 실제 Render 인프라(디스크 마운트·인스턴스 수·대시보드 비밀값)를 조회하지 않습니다. 확인할 수 없는 항목은 UNVERIFIED로
남기며, 영속 디스크가 있다고 추정하지 않습니다. 비밀값은 출력하지 않고 "설정됨/없음"만 표시합니다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 공식 가격표(https://platform.claude.com/docs/en/about-claude/pricing)를 2026-10-15에 확인한 값 (USD / 100만 토큰).
# 가격표가 바뀌면 이 표와 CTREC_WEB_PRICE_*를 함께 갱신해야 함. 이 표는 enforce 설정값이 공식값보다 낮은지 보는 데만 씀.
OFFICIAL_PRICES = {
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.2,
                        "checked": "2026-10-15", "note": "5분 캐시 쓰기 기준 (1시간 캐시 쓰기 $4는 웹에서 쓰지 않음)"},
}
PRICE_KEYS = {"input": "CTREC_WEB_PRICE_INPUT", "output": "CTREC_WEB_PRICE_OUTPUT",
              "cache_write": "CTREC_WEB_PRICE_CACHE_WRITE", "cache_read": "CTREC_WEB_PRICE_CACHE_READ"}
SECRET_KEYS = ("ANTHROPIC_API_KEY", "CTREC_PASSWORD")
TESTED_PYTHON = (3, 10)  # 이 저장소의 테스트를 실제로 돌린 버전 (배포 버전과 다르면 경고)

PASS, WARNING, BLOCKED, UNVERIFIED = "PASS", "WARNING", "BLOCKED", "UNVERIFIED"


# ---------------------------------------------------------------- 입력 읽기

def parse_render_yaml(text: str) -> dict:
    """render.yaml의 첫 web 서비스에서 필요한 항목만 읽음 (PyYAML 의존 없이, 이 저장소 형식 기준).
    envVars는 {key: value} 또는 sync:false면 {key: None}. disk는 mountPath만."""
    svc: dict = {"env": {}, "disk": None}
    cur_key, in_env, in_disk = None, False, False
    for raw in text.splitlines():
        line = raw.split(" #")[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line.strip():
            continue
        s = line.strip()
        indent = len(line) - len(line.lstrip())
        if indent <= 4 and not s.startswith("- key:") and in_env and not s.startswith(("value:", "sync:")):
            in_env = False
        if indent <= 4 and in_disk and not s.startswith(("name:", "mountPath:", "sizeGB:")):
            in_disk = False
        if s == "envVars:":
            in_env = True
            continue
        if s == "disk:":
            in_disk, svc["disk"] = True, {}
            continue
        if in_env:
            if s.startswith("- key:"):
                cur_key = s.split(":", 1)[1].strip()
                svc["env"][cur_key] = None
            elif s.startswith("value:") and cur_key:
                svc["env"][cur_key] = s.split(":", 1)[1].strip().strip("'\"")
            continue
        if in_disk:
            k, _, v = s.partition(":")
            svc["disk"][k.strip()] = v.strip().strip("'\"")
            continue
        for k in ("plan", "numInstances", "startCommand", "healthCheckPath", "buildCommand", "runtime"):
            if s.startswith(f"{k}:") or s.startswith(f"- {k}:"):
                svc[k] = s.split(":", 1)[1].strip()
    return svc


def read_env_file(path: str) -> dict:
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.split(" #")[0].strip().strip("'\"")
    return out


def _flag(v) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- 점검

class Report:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, area: str, status: str, message: str) -> None:
        self.items.append({"area": area, "status": status, "message": message})

    def count(self) -> dict:
        c = {s: 0 for s in (PASS, WARNING, BLOCKED, UNVERIFIED)}
        for it in self.items:
            c[it["status"]] += 1
        return c


def _workers(cmd: str) -> int | None:
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return None
    for i, p in enumerate(parts):
        if p == "--workers" and i + 1 < len(parts):
            return int(parts[i + 1]) if parts[i + 1].isdigit() else -1
        if p.startswith("--workers="):
            v = p.split("=", 1)[1]
            return int(v) if v.isdigit() else -1
    return None


def check_render(r: Report, svc: dict, target: str, ledger: str) -> None:
    n = svc.get("numInstances")
    if n == "1":
        r.add("instances", PASS, "numInstances: 1")
    elif n is None:
        r.add("instances", WARNING, "numInstances가 없음 (Render 기본값 확인 필요, 실행 상태가 메모리에 있어 1개여야 함)")
    else:
        r.add("instances", BLOCKED, f"numInstances: {n} (실행 상태·소유권·SSE가 프로세스 메모리에 있어 1개여야 함)")
    cmd = svc.get("startCommand") or ""
    w = _workers(cmd)
    if w == 1:
        r.add("workers", PASS, "uvicorn --workers 1")
    elif w is None:
        r.add("workers", BLOCKED, "--workers 없음: uvicorn은 WEB_CONCURRENCY(Render가 설정할 수 있음)만큼 워커를 띄움 -> --workers 1 필요")
    else:
        r.add("workers", BLOCKED, f"--workers {w}: 워커가 여러 개면 다른 워커로 간 요청이 실행을 찾지 못함")
    if "--host 0.0.0.0" in cmd and "--port $PORT" in cmd:
        r.add("port", PASS, "0.0.0.0:$PORT 바인딩")
    else:
        r.add("port", BLOCKED, "시작 명령에 --host 0.0.0.0 --port $PORT가 필요 (Render가 PORT로 연결)")
    if "--proxy-headers" in cmd and "--forwarded-allow-ips" in cmd:
        r.add("proxy", WARNING if "'*'" in cmd or '"*"' in cmd else PASS,
              "프록시 헤더 신뢰 ('*'는 서비스가 Render 프록시 뒤에서만 접근 가능하다는 전제에서만 안전)")
    else:
        r.add("proxy", BLOCKED, "--proxy-headers/--forwarded-allow-ips 없음: HTTPS가 인식되지 않아 CTREC_REQUIRE_HTTPS=1에서 403")
    m = re.search(r"--log-level\s+(\S+)", cmd)
    lvl = m.group(1) if m else "info"
    r.add("log_level", PASS if lvl in ("warning", "error") else WARNING,
          f"uvicorn 로그 수준 {lvl} (앱 로그는 오류 종류만 남김; info 이하면 접근 로그에 실행 ID 경로가 남음)")
    hp = svc.get("healthCheckPath")
    if hp == "/healthz":
        r.add("health_check", PASS, "healthCheckPath: /healthz (앱 라우트와 일치, 생존 확인만 함)")
    else:
        r.add("health_check", WARNING, f"healthCheckPath: {hp} (앱의 상태 확인 경로는 /healthz)")
    plan, disk = svc.get("plan"), svc.get("disk")
    if target == "enforce":
        if plan in (None, "free"):
            r.add("plan", BLOCKED, f"plan: {plan or '없음'} -> 무료 플랜은 영속 디스크를 붙일 수 없음 (enforce 원장 보존 불가)")
        else:
            r.add("plan", PASS, f"plan: {plan}")
        if not disk or not disk.get("mountPath"):
            r.add("disk", BLOCKED, "render.yaml에 disk가 없음: 원장이 재배포 때 사라짐")
        else:
            mp = disk["mountPath"].rstrip("/") + "/"
            inside = ledger.replace("\\", "/").startswith(mp)
            r.add("disk", PASS if inside else BLOCKED,
                  f"disk mountPath {disk['mountPath']} / 원장 경로 {'안' if inside else '밖'}" +
                  ("" if inside else " (마운트 경로 밖의 파일은 재시작 때 사라짐)"))
        r.add("disk_persistence", UNVERIFIED, "실제 디스크가 마운트되어 있고 재배포 뒤에도 남는지는 이 도구로 확인할 수 없음 "
              "(배포 뒤 Render Shell에서 --process-env로 원장 확인 + 재배포 전후 summary 비교)")
    else:
        r.add("plan", PASS, f"plan: {plan or '없음'} (record 구성: 영속 저장소 불필요)")


def check_env(r: Report, env: dict, target: str, provided: set) -> None:
    mode = (env.get("CTREC_WEB_METERING") or "record").strip().lower()
    if mode not in ("off", "record", "enforce"):
        r.add("budget_mode", BLOCKED, f"CTREC_WEB_METERING={mode}: 알 수 없는 값 (서버는 enforce로 취급해 시작을 거부)")
    elif mode != target:
        r.add("budget_mode", BLOCKED, f"CTREC_WEB_METERING={mode}인데 점검 목표는 {target}")
    elif mode == "record":
        r.add("budget_mode", PASS, "record: 사용량 기록만, 비용 강제 차단 없음 (유료 호출 허용을 뜻하지 않음)")
    elif mode == "off":
        r.add("budget_mode", WARNING, "off: 사용량을 기록하지 않음")
    else:
        r.add("budget_mode", PASS, "enforce: 호출 전 예약·한도 검사")

    model = env.get("CTREC_MODEL") or "claude-sonnet-5"
    if model in OFFICIAL_PRICES:
        r.add("model", PASS, f"{model}: 공식 가격표에 있음 ({OFFICIAL_PRICES[model]['checked']} 확인)")
    else:
        r.add("model", BLOCKED if target == "enforce" else WARNING, f"{model}: 확인된 가격표에 없는 모델")
    r.add("model_access", UNVERIFIED, "이 API 키로 모델을 실제 호출할 수 있는지는 유료 호출 없이 확인할 수 없음")

    fb = env.get("CTREC_FALLBACKS", "1")
    if fb == "0":
        r.add("fallbacks", PASS, "CTREC_FALLBACKS=0 (대체 모델 없음)")
    elif target == "enforce":
        r.add("fallbacks", BLOCKED, f"CTREC_FALLBACKS={fb}: 대체 모델 단가를 알 수 없어 enforce가 시작을 거부")
    else:
        r.add("fallbacks", WARNING, f"CTREC_FALLBACKS={fb}: 거절 시 검증되지 않은 대체 모델이 판정·비용 미확인")

    key = "ANTHROPIC_API_KEY"
    if key not in provided:
        r.add("api_key", UNVERIFIED, "ANTHROPIC_API_KEY: 대시보드 값(sync: false)이라 확인 불가")
    elif env.get(key):
        r.add("api_key", PASS, "ANTHROPIC_API_KEY: 설정됨 (값은 표시하지 않음)")
    else:
        r.add("api_key", WARNING, "ANTHROPIC_API_KEY: 없음 -> 분석 시작 시 오류 (유료 호출은 일어나지 않음)")

    pw_known = "CTREC_PASSWORD" in provided
    pw = env.get("CTREC_PASSWORD") or ""
    if not pw_known:
        r.add("auth", UNVERIFIED, "CTREC_PASSWORD: 대시보드 값이라 확인 불가 (비우면 누구나 접속)")
    elif pw and len(pw) >= 12:
        r.add("auth", PASS, "CTREC_PASSWORD: 설정됨 (12자 이상)")
    elif pw:
        r.add("auth", WARNING, "CTREC_PASSWORD: 12자 미만")
    elif target == "record" and env.get(key):
        r.add("auth", BLOCKED, "CTREC_PASSWORD 없음 + API 키 있음 + 비용 강제 차단 없음: 누구나 유료 호출을 일으킬 수 있음")
    else:
        r.add("auth", WARNING, "CTREC_PASSWORD 없음: 누구나 접속 가능")

    for k in ("CTREC_REQUIRE_HTTPS", "CTREC_COOKIE_SECURE"):
        r.add(k.lower(), PASS if env.get(k) == "1" else BLOCKED, f"{k}={env.get(k) or '없음'} (공개 배포는 1)")

    if _flag(env.get("CTREC_WEB_SAVE_OUTPUTS")):
        r.add("outputs", WARNING, "CTREC_WEB_SAVE_OUTPUTS=1: 환자 입력이 담긴 결과 파일을 서버 디스크에 저장 (기본 0 권장)")
    else:
        r.add("outputs", PASS, "결과 파일 저장 꺼짐 (기본값)")

    py = env.get("PYTHON_VERSION")
    if py and tuple(int(x) for x in py.split(".")[:2] if x.isdigit()) != TESTED_PYTHON:
        r.add("python_version", WARNING, f"배포 Python {py} / 테스트한 Python {'.'.join(map(str, TESTED_PYTHON))}: 배포 버전에서 테스트 필요")
    elif py:
        r.add("python_version", PASS, f"Python {py}")


def check_enforce(r: Report, env: dict, on_server: bool) -> None:
    from ctrec import config
    from ctrec.web import budget
    model = env.get("CTREC_MODEL") or "claude-sonnet-5"
    with config.overrides(MODEL=model, USE_FALLBACKS=env.get("CTREC_FALLBACKS", "1") == "1"):
        try:
            cfg = budget.load_config(env)
            r.add("enforce_config", PASS, "enforce 필수 설정 완비")
        except budget.BudgetConfigError as e:
            cfg = None
            for p in e.problems:
                r.add("enforce_config", BLOCKED, p)
    official = OFFICIAL_PRICES.get(model)
    if official:
        for k, name in PRICE_KEYS.items():
            try:
                v = float(env.get(name) or "nan")
            except ValueError:
                continue
            if v != v:
                continue
            if v < official[k]:
                r.add("price", BLOCKED, f"{name}={v} < 공식 {official[k]} ({official['checked']} 확인): 비용을 낮게 계산함")
            elif v > official[k]:
                r.add("price", WARNING, f"{name}={v} > 공식 {official[k]}: 보수적이지만 가격표 재확인 필요")
            else:
                r.add("price", PASS, f"{name}={v} (공식값과 같음)")
    r.add("price_current", UNVERIFIED, "가격표는 바뀔 수 있음: 배포 직전에 공식 가격표를 다시 확인하고 CTREC_WEB_PRICE_SOURCE에 날짜 기록")
    ledger = (env.get("CTREC_WEB_BUDGET_LEDGER") or "").strip()
    if not ledger:
        return
    d = os.path.dirname(os.path.abspath(ledger))
    if not os.path.isdir(d):
        if on_server:
            r.add("ledger_dir", BLOCKED, "원장 디렉터리가 없음: 디스크가 마운트되지 않았거나 경로가 틀림")
        else:
            r.add("ledger_dir", UNVERIFIED, "원장 디렉터리가 이 컴퓨터에 없음 (배포 환경의 경로일 수 있음) "
                  "-> 서버 환경 안에서 --process-env로 다시 점검")
        return
    try:
        with tempfile.NamedTemporaryFile(dir=d, prefix=".ctrec-write-test-"):
            pass
        r.add("ledger_dir", PASS, "원장 디렉터리 쓰기 가능 (SQLite 저널 파일 생성에 필요)")
    except OSError:
        r.add("ledger_dir", BLOCKED, "원장 디렉터리에 쓸 수 없음")
    try:
        info = budget.inspect_ledger(ledger)
        r.add("ledger_file", PASS, f"원장 무결성 확인 (행 {info['rows']}, 상태별 {info['rows_by_status']})")
        if info["rows_by_status"].get("unknown"):
            r.add("ledger_unknown", WARNING, f"UNKNOWN {info['rows_by_status']['unknown']}건: 운영자 정산 필요 "
                  "(scripts/web_budget_ledger.py unknown)")
    except budget.LedgerError as e:
        code = e.args[0] if e.args else "ledger_unavailable"
        r.add("ledger_file", BLOCKED, f"{code}" + (": scripts/web_budget_ledger.py init으로 만드세요" if code == "ledger_missing"
                                                  else ": 백업에서 복구 필요 (docs/web_budget_operations.md)"))


def check_repo(r: Report, svc: dict) -> None:
    for k in SECRET_KEYS:
        if svc["env"].get(k):
            r.add("secrets_in_blueprint", BLOCKED, f"render.yaml에 {k} 값이 들어 있음 (sync: false로 대시보드에서 입력)")
    try:
        files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, timeout=30).stdout.split()
    except (OSError, subprocess.SubprocessError):
        r.add("secrets_in_repo", UNVERIFIED, "git ls-files 실행 불가")
        return
    if any(Path(f).name == ".env" for f in files):
        r.add("secrets_in_repo", BLOCKED, ".env 파일이 저장소에 포함됨")
    pat = re.compile(rb"sk-ant-(?!test-)[A-Za-z0-9_-]{20,}")  # 테스트용 가짜 키(sk-ant-test-...)는 제외
    hits = []
    for f in files:
        p = ROOT / f
        try:
            if p.stat().st_size < 5_000_000 and pat.search(p.read_bytes()):
                hits.append(f)
        except OSError:
            continue
    r.add("secrets_in_repo", BLOCKED if hits else PASS,
          f"API 키 형태 문자열이 있는 추적 파일: {hits}" if hits else "추적 파일에 API 키 형태 문자열 없음")
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    m = re.search(r"^anthropic(\S*)", req, re.M)
    r.add("sdk_pin", PASS if m and "<" in m.group(1) else WARNING,
          f"requirements anthropic{m.group(1) if m else ''} (주 버전 고정 권장: 사용량 필드·재시도 동작에 의존)")


def probe_health(r: Report) -> None:
    try:
        from fastapi.testclient import TestClient

        from ctrec.web import app as web
        resp = TestClient(web.app, base_url="https://localhost").get("/healthz")
        r.add("health_probe", PASS if resp.status_code == 200 and resp.json() == {"ok": True} else BLOCKED,
              f"/healthz -> {resp.status_code} (앱 내부 호출, 실제 배포 주소는 확인하지 않음)")
    except Exception as e:  # noqa: BLE001
        r.add("health_probe", BLOCKED, f"앱을 불러오지 못함: {type(e).__name__}")


def run(argv=None, environ=None) -> tuple[Report, dict]:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--render-yaml", default=str(ROOT / "render.yaml"))
    ap.add_argument("--env-file", help="대시보드에 넣을 값을 적은 파일 (KEY=VALUE). 비밀값을 넣었다면 점검 뒤 지우세요.")
    ap.add_argument("--process-env", action="store_true", help="현재 프로세스 환경 변수도 사용 (서버 환경 안에서 실행할 때)")
    ap.add_argument("--target", choices=("record", "enforce"))
    ap.add_argument("--probe", action="store_true", help="앱을 불러와 /healthz를 내부 호출")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    svc = parse_render_yaml(Path(a.render_yaml).read_text(encoding="utf-8"))
    env = {k: v for k, v in svc["env"].items() if v is not None}
    provided = set(env)
    if a.process_env:
        src = os.environ if environ is None else environ
        env.update(src)
        provided |= set(src)
    if a.env_file:
        extra = read_env_file(a.env_file)
        env.update(extra)
        provided |= set(extra)
    target = a.target or (env.get("CTREC_WEB_METERING") or "record").strip().lower()
    target = target if target in ("record", "enforce") else "enforce"
    r = Report()
    check_render(r, svc, target, env.get("CTREC_WEB_BUDGET_LEDGER", ""))
    check_env(r, env, target, provided)
    if target == "enforce":
        check_enforce(r, env, a.process_env)
    check_repo(r, svc)
    if a.probe:
        probe_health(r)
    return r, {"target": target, "json": a.json}


def main(argv=None) -> int:
    r, meta = run(argv)
    c = r.count()
    if meta["json"]:
        print(json.dumps({"target": meta["target"], "counts": c, "items": r.items}, ensure_ascii=False, indent=1))
    else:
        print(f"웹 배포 사전 점검 (목표 구성: {meta['target']})")
        for it in r.items:
            print(f"  [{it['status']:<10}] {it['area']}: {it['message']}")
        print(f"합계: PASS {c[PASS]} / WARNING {c[WARNING]} / BLOCKED {c[BLOCKED]} / UNVERIFIED {c[UNVERIFIED]}")
        if c[UNVERIFIED]:
            print("UNVERIFIED 항목은 실제 인프라·대시보드에서 직접 확인해야 합니다 (docs/web_deployment.md).")
    return 2 if c[BLOCKED] else 0


if __name__ == "__main__":
    sys.exit(main())
