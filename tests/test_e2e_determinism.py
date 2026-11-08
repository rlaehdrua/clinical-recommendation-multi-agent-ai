"""오프라인 합성 E2E의 결정성 (재현 감사 F-2).

원인: Follow-up 사례 별칭이 전부 난수(secrets.token_hex)였고, 정책은 우선순위가 같은 행동을 별칭 순서로 고름
(LegacyAdapterPolicy.order_key, 환자별 최소 배정). 그래서 같은 입력·같은 설정에서도 실행마다 담당자 대기열 순서가 바뀌고,
그 순서에 따라 SYN-02 T-A·T-C가 CONFIRMED_INELIGIBLE / BLOCKED_FAILURE로 갈렸음.
수정: 별칭 앞자리를 접수 순번으로 (service.intake_alias). 난수 ID 자체는 그대로 난수 - 여기서는 '판정이 ID 흐름에 따라
달라지지 않는다'를 봅니다. 오프라인 대역(LLM 호출 0), 합성 데이터만.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from ctrec import pipeline  # noqa: E402
from ctrec import question_eval as qe  # noqa: E402
from ctrec.engine.contracts import ALIAS_PATTERN  # noqa: E402
from ctrec.followup import service as fsvc  # noqa: E402
from ctrec.followup import store as fstore  # noqa: E402
from ctrec.followup.service import FollowupService, intake_alias  # noqa: E402
from ctrec.followup.store import CaseStore  # noqa: E402
from ctrec.sim import documents as D  # noqa: E402


class _Stream:
    """정해진 token_hex 흐름: up=True면 점점 커지고, False면 점점 작아짐 (별칭 난수 부분의 순서를 뒤집는 시험)."""

    def __init__(self, up: bool):
        self.n, self.up = 0, up

    def token_hex(self, nbytes: int = 16) -> str:
        self.n += 1
        v = self.n if self.up else 16 ** (2 * nbytes) - 1 - self.n
        return f"{v:0{2 * nbytes}x}"

    def __getattr__(self, name):
        import secrets
        return getattr(secrets, name)


def test_intake_alias_keeps_contract_and_sorts_by_intake_order():
    import secrets
    aliases = [intake_alias(i) for i in range(300)]
    assert all(re.match(ALIAS_PATTERN, a) for a in aliases)
    assert len(set(aliases)) == len(aliases)
    assert sorted(aliases) == aliases  # 난수 부분과 무관하게 접수 순서 = 별칭 순서
    assert intake_alias(0) != intake_alias(0)  # 같은 순번이어도 난수 부분이 남음 (동시 접수)
    assert re.match(ALIAS_PATTERN, intake_alias(10 ** 9)) and re.match(ALIAS_PATTERN, intake_alias(-1))
    assert secrets.token_hex  # 실제 난수 함수를 숨기지 않음


def _syn_texts() -> list[str]:
    import run_e2e_synthetic as E
    co = E.cohort()
    out = []
    for p in co.patients:
        hide = {"age"} if p.key in E.HIDE_AGE else set()
        out.append(D.write_narrative(p.key, E.items_of(p.public), hide, [], use_llm=False, cache=D.Cache(None)).text)
    return out


def _queue_under_stream(monkeypatch, root: Path, up: bool) -> tuple[list[str], list[tuple]]:
    import run_e2e_synthetic as E
    from followup_demo_server import latest_mention_profiler, synthetic_catalog  # E2E 서버와 같은 카탈로그·대역
    stream = _Stream(up)
    monkeypatch.setattr(fsvc, "secrets", stream)
    monkeypatch.setattr(fstore, "secrets", stream)
    texts = _syn_texts()
    with qe.OfflineEnvironment(3):
        pipeline.patient_profiler.profile_patient = latest_mention_profiler  # 환경이 끝날 때 원래 값으로 되돌림
        svc = FollowupService(CaseStore(root), synthetic_catalog())
        order = []
        for i, t in enumerate(texts):
            c = svc.create_case(patient_text=t, trial_ids=E.TRIALS, input_source="self_report",
                                patient_token_digest=f"{i:064x}", synthetic=True)
            order.append(c["case_id"])
        q = svc.staff_queue()["items"]
        aliases = [svc.store.load(cid)["alias"] for cid in order]
    intake = {cid: i for i, cid in enumerate(order)}
    return aliases, [(intake[x["case_id"]], x["action_type"], x["source_id"], x["info_key"], x["route"]) for x in q]


def test_staff_queue_order_does_not_depend_on_random_ids(monkeypatch, tmp_path):
    """같은 6명을 같은 순서로 접수 -> 별칭 난수 부분이 오름차순이든 내림차순이든 대기열(접수 순번 기준)이 같아야 함."""
    up_alias, up_q = _queue_under_stream(monkeypatch, tmp_path / "up", up=True)
    down_alias, down_q = _queue_under_stream(monkeypatch, tmp_path / "down", up=False)
    assert sorted(up_alias) == up_alias and sorted(down_alias) == down_alias  # 별칭 순서 = 접수 순서
    assert up_q and up_q == down_q


@pytest.mark.parametrize("seeds", [(1, 3)])  # 수정 전: 시드 1 = SYN-02 T-A/T-C BLOCKED_FAILURE, 시드 3 = CONFIRMED_INELIGIBLE
def test_offline_e2e_verdicts_do_not_depend_on_server_id_stream(monkeypatch, tmp_path, seeds):
    """끝까지 실행(서버는 하위 프로세스)을 서로 다른 ID 흐름으로 두 번 -> 모든 (환자, 시험) 판정·점수·요청 수가 같아야 함."""
    import run_e2e_synthetic as E
    real_popen = E.subprocess.Popen
    helper = str(ROOT / "tests" / "e2e_id_stream_server.py")

    def run_with(seed: int) -> dict:
        def popen(cmd, env=None, **kw):
            env = dict(env or {})
            env["E2E_TEST_ID_SEED"] = str(seed)
            return real_popen([cmd[0], helper, *cmd[2:]], env=env, **kw)

        monkeypatch.setattr(E.subprocess, "Popen", popen)
        args = argparse.Namespace(mode="offline", env_file=None, patients=None, missing=0.15, clinician="MET", seed=1,
                                  budget=600, per_round=6, max_rounds=12, url=None, keep_server=False, cache=None,
                                  out=str(tmp_path / f"ids{seed}"))
        return E.run(args)

    a, b = (run_with(s) for s in seeds)
    pairs = lambda r: {(p["patient"], p["trial"]): p["status"] for p in r["pairs"]}  # noqa: E731
    assert pairs(a) == pairs(b)
    assert a["score"] == b["score"] and a["stats"].get("requests_sent") == b["stats"].get("requests_sent")
    assert a["safety_violations"] == 0 and b["safety_violations"] == 0
