"""테스트 전용: 오프라인 데모 서버(scripts/followup_demo_server.py)를 '정해진 난수 ID 흐름'으로 띄움.

사례·별칭·요청·문서 ID(followup.service / followup.store의 secrets.token_hex)를 E2E_TEST_ID_SEED로 시드한 난수로 바꿈.
서로 다른 시드 = 서로 다른 ID 흐름. 판정이 ID 흐름에 따라 달라지면 비결정성 (재현 감사 F-2). 서명 비밀·키(token_urlsafe)는
그대로 실제 난수. 운영 코드에는 이 훅이 없습니다 (테스트가 하위 프로세스 실행 명령을 이 파일로 바꿈).
"""

from __future__ import annotations

import os
import random
import runpy
import secrets as _secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

_rng = random.Random(int(os.environ["E2E_TEST_ID_SEED"]))


class _SeededSecrets:
    @staticmethod
    def token_hex(nbytes: int = 16) -> str:
        return "".join(_rng.choice("0123456789abcdef") for _ in range(2 * nbytes))

    def __getattr__(self, name):
        return getattr(_secrets, name)


import ctrec.followup.service as _service  # noqa: E402
import ctrec.followup.store as _store  # noqa: E402

_service.secrets = _SeededSecrets()
_store.secrets = _SeededSecrets()

script = str(ROOT / "scripts" / "followup_demo_server.py")
sys.argv = [script, *sys.argv[1:]]
runpy.run_path(script, run_name="__main__")
