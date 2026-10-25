"""재설계(P1~P6) 테스트 공용 도구. 모든 저장소는 tmp_path 안의 합성 기준표·사이드카이며,
실제 Frozen Store(data/parsed)와 저장소의 contracts/는 쓰지 않습니다. 테스트 안의 승인은 tmp 사이드카에만 기록됩니다.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import inspect

from ctrec.agents import matcher as _matcher
from ctrec.condition_ir import CriterionIR
from ctrec.ir_review import Registry
from ctrec.question_contract import ContractRegistry
from ctrec.schemas import QAPair, Rule

REF = "2026-10-24"
# 보호 파일 패치 03(matcher에 qa_guard 연결)이 적용되고 그 경계가 켜진 환경인가. 켜지면 '경계 전 기존 Matcher 결과'도 이미
# 경계를 거침 -> 대조 테스트는 이 값에 따라 기존 경로의 기대값을 고름 (경계 후 기대값은 환경과 무관하게 같음).
# 패치 03 v2(P8)는 정책 선택(qa_guard.policy, CTREC_QA_POLICY)일 때만 경계를 적용, v1은 항상 적용
_MATCH_SRC = inspect.getsource(_matcher.match_trial)
MATCHER_GUARDED = "guard_assessments" in _MATCH_SRC and (
    "qa_guard.policy()" not in _MATCH_SRC or __import__("ctrec.qa_guard", fromlist=["policy"]).policy() == "contract")
SID = "S-contract-test"
REVIEWER = "임상검수자A"

W = "w28d-assessment"
QC_SURGERY = {
    "contract_id": "QC-SURGERY-LAST", "version": 1, "answer_type": "single_choice",
    "question": "가장 최근에 수술(내시경·조직검사 같은 시술 제외)을 받은 시기는 언제인가요?",
    "options": [
        {"label": "최근 4주(28일) 이내", "assertions": [[f"surgery/event/{W}", True]]},
        {"label": "4주(28일)보다 전, 3개월(90일) 이내",
         "assertions": [[f"surgery/event/{W}", False], ["surgery/event/w90d-assessment", True]]},
        {"label": "3개월(90일)보다 전", "assertions": [["surgery/event/w90d-assessment", False],
                                                  ["surgery/event/ever", True]]},
        {"label": "수술을 받은 적 없음", "assertions": [["surgery/event/ever", False]]},
    ]}
QC_CHEMO = {
    "contract_id": "QC-CHEMO-LAST", "version": 1, "answer_type": "single_choice",
    "question": "가장 최근에 항암 화학요법(주사·먹는 항암제)을 받은 시기는 언제인가요?",
    "options": [
        {"label": "최근 4주(28일) 이내", "assertions": [[f"chemotherapy/event/{W}", True]]},
        {"label": "4주(28일)보다 전", "assertions": [[f"chemotherapy/event/{W}", False],
                                                 ["chemotherapy/event/ever", True]]},
        {"label": "받은 적 없음", "assertions": [["chemotherapy/event/ever", False]]},
    ]}
QC_PREG = {"contract_id": "QC-PREGNANT-NOW", "version": 1, "answer_type": "yes_no",
           "question": "현재 임신 중인가요?",
           "options": [{"label": "예", "assertions": [["pregnancy/condition/current", True]]},
                       {"label": "아니요", "assertions": [["pregnancy/condition/current", False]]}]}
QC_LACT = {"contract_id": "QC-LACTATING-NOW", "version": 1, "answer_type": "yes_no",
           "question": "현재 모유 수유 중인가요?",
           "options": [{"label": "예", "assertions": [["lactation/condition/current", True]]},
                       {"label": "아니요", "assertions": [["lactation/condition/current", False]]}]}
QC_PREG_EVER = {"contract_id": "QC-PREGNANT-EVER", "version": 1, "answer_type": "yes_no",
                "question": "지금까지 임신한 적이 있나요?",
                "options": [{"label": "예", "assertions": [["pregnancy/condition/ever", True]]},
                            {"label": "아니요", "assertions": [["pregnancy/condition/ever", False]]}]}
QC_RITUX = {"contract_id": "QC-RITUXIMAB-6M", "version": 1, "answer_type": "yes_no",
            "question": "최근 6개월(180일) 안에 리툭시맙(rituximab) 주사를 맞은 적이 있나요?",
            "options": [{"label": "예", "assertions": [["rituximab/event/w180d-assessment", True]]},
                        {"label": "아니요", "assertions": [["rituximab/event/w180d-assessment", False]]}]}
QC_ANTICD20 = {"contract_id": "QC-ANTICD20-6M", "version": 1, "answer_type": "yes_no",
               "question": "최근 6개월(180일) 안에 항CD20 항체 주사(리툭시맙·오비누투주맙 등)를 맞은 적이 있나요?",
               "options": [{"label": "예", "assertions": [["anti_cd20_antibody/event/w180d-assessment", True]]},
                           {"label": "아니요", "assertions": [["anti_cd20_antibody/event/w180d-assessment", False]]}]}
ALL_CONTRACTS = [QC_SURGERY, QC_CHEMO, QC_PREG, QC_LACT, QC_PREG_EVER, QC_RITUX, QC_ANTICD20]


def rule(rule_id: str, text: str, kind: str = "exclusion", category: str = "procedure", **kw) -> Rule:
    base = dict(rule_id=rule_id, kind=kind, text=text, category=category, field=None, operator=None, value=None,
                unit=None, time_window_days=None, structured_evaluable=False, underspecified=False, cohort=None)
    base.update(kw)
    return Rule(**base)


RULES = {
    "SURG": rule("E1", "Major surgery within 4 weeks before first dose", time_window_days=28),
    "CHEMO": rule("E2", "Chemotherapy within 4 weeks prior to first dose", category="treatment_history",
                  time_window_days=28),
    "PREG": rule("E3", "Pregnant or breastfeeding", category="reproductive"),
    "RITUX": rule("I1", "No rituximab was used within 6 months prior to the enrollment", kind="inclusion",
                  category="medication", time_window_days=180),
}


def frozen_dir(tmp: Path, rules: list[Rule], trial_id: str = "T1") -> Path:
    """합성 잠긴 기준표 (tmp). 반환: parsed_dir."""
    d = tmp / "parsed"
    d.mkdir(parents=True, exist_ok=True)
    body = {"trial_id": trial_id, "parsed": {"trial_id": trial_id, "title": "t", "target_population": "",
                                              "intervention_summary": "", "key_conditions": [], "cohorts": [],
                                              "rules": [r.model_dump() for r in rules], "parsing_notes": ""},
            "rule_meta": {}}
    p = d / f"{trial_id}.json"
    p.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    manifest = json.loads((d / "FROZEN.json").read_text(encoding="utf-8")) if (d / "FROZEN.json").exists() \
        else {"files": {}}
    manifest["files"][trial_id] = hashlib.sha256(p.read_bytes()).hexdigest()
    (d / "FROZEN.json").write_text(json.dumps(manifest), encoding="utf-8")
    return d


def frozen_hash(parsed_dir: Path, trial_id: str = "T1") -> str:
    return json.loads((parsed_dir / "FROZEN.json").read_text(encoding="utf-8"))["files"][trial_id]


def ir_dict(r: Rule, parsed_dir: Path, condition: dict, *, trial_id="T1", completeness="complete",
            source_requirement="self_report_ok", anchor_policy=None, applicability=None, span=None, **kw) -> dict:
    d = {"trial_id": trial_id, "rule_id": r.rule_id, "ir_version": 1, "criterion_text": r.text,
         "source_span": span or r.text, "frozen_file_hash": frozen_hash(parsed_dir, trial_id), "condition": condition,
         "completeness": completeness, "source_requirement": source_requirement, "applicability": applicability,
         "anchor_policy": anchor_policy or {}, "unsupported_parts": kw.pop("unsupported_parts", []),
         "notes": "test", "author": "test", "review_status": "draft"}
    d.update(kw)
    return d


def atom(key: str) -> dict:
    return {"op": "atom", "prop": key}


SURG_IR = atom("surgery(major)/event/w28d-first_dose")
CHEMO_IR = atom("chemotherapy/event/w28d-first_dose")
PREG_IR = {"op": "or", "args": [atom("pregnancy/condition/current"), atom("lactation/condition/current")]}
RITUX_IR = atom("rituximab/event/w180d-enrollment")
PROXY = {"first_dose": "assessment_proxy", "enrollment": "assessment_proxy"}


def sidecar(tmp: Path, parsed_dir: Path, irs: list[dict], contracts=ALL_CONTRACTS, *, approve_ir=True,
            approve_contracts=True, trial_id="T1") -> Registry:
    root = tmp / "contracts"
    (root / "ir").mkdir(parents=True, exist_ok=True)
    (root / "ir" / f"{trial_id}.json").write_text(json.dumps({"format_version": 1, "trial_id": trial_id,
                                                              "criteria": irs}, ensure_ascii=False), encoding="utf-8")
    (root / "question_contracts.json").write_text(json.dumps({"contracts": contracts}, ensure_ascii=False),
                                                  encoding="utf-8")
    reg = Registry(root, parsed_dir)
    if approve_ir:
        for d in irs:
            reg.record_review("ir", f"{trial_id}:{d['rule_id']}", CriterionIR.from_dict(d).digest(), "approved",
                              reviewer=REVIEWER, note="테스트 검수", clinical_review_confirmed=True)
    if approve_contracts:
        cr = ContractRegistry(reg)
        for c in contracts:
            reg.record_review("contracts", c["contract_id"], cr.raw(c["contract_id"]).digest(), "approved",
                              reviewer=REVIEWER, note="테스트 검수", clinical_review_confirmed=True)
    return reg


def session_text(initial: str, rounds, session_id: str | None = SID, purpose: str = "eligibility"):
    """rounds: [[(질문, 답, 대상 키), ...], ...] -> (Session.clarify 형식 원문, qa_log)."""
    text, log = initial, []
    for r, pairs in enumerate(rounds, 1):
        text += f"\n\n[추가 확인 정보 - 라운드 {r}]\n" + "\n".join(f"Q: {q}\nA: {a}" for q, a, _ in pairs)
        log += [QAPair(round=r, question_id=f"Q{i}", question=q, answer=a, target=[t], session_id=session_id,
                       purpose=purpose) for i, (q, a, t) in enumerate(pairs, 1)]
    return text, log
