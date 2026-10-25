"""IR·질문 계약 기준별 검수 자료 생성 (P8). 승인 기록을 만들거나 바꾸지 않습니다.

    python scripts/build_review_package.py           # docs/review/ir_review_package.md 작성
    python scripts/build_review_package.py --check   # 자료가 최신인지만 확인

검수자가 기준 하나를 한 곳에서 확인하도록: 원문, 사람이 읽는 IR, 승인 대상 digest, 계약 질문·선택지와 각 선택지가 만드는
명제 사실, 검증 사례, 사전 분류와 위험, 임상 검토 질문·필요한 증거(임상 검토 필요), 지원되지 않는 이유·확장 요건(표현 불가),
검수 체크리스트. 분류·질문은 AI 사전 작성이며 승인이 아닙니다.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from ctrec.condition_ir import walk  # noqa: E402
from ctrec.ir_review import Registry  # noqa: E402
from ctrec.propositions import CONCEPTS, QUALIFIERS, Proposition  # noqa: E402
from ctrec.question_contract import ContractRegistry  # noqa: E402
from report_contract_drafts import CONTRACT_REVIEW, IR_REVIEW, LABEL, PRIORITY  # noqa: E402

OUT = ROOT / "docs" / "review" / "ir_review_package.md"
ANCHOR_KO = {"assessment": "답변 시점", "enrollment": "등록", "screening": "스크리닝", "randomization": "무작위 배정",
             "first_dose": "첫 투여"}

# 임상 전문가 검토 필요 항목: (검토 질문들, 판단에 필요한 증거)
CLINICAL = {
    "NCT06125405:I12": (["진단 목적 ACTH 투여(ACTH 자극 검사 1회)가 'ACTH was used'에 포함되는가?",
                         "3개월을 90일로 환산해도 되는가?", "기준 시점 등록(enrollment)을 답변 시점으로 근사해도 되는가?"],
                        "프로토콜의 ACTH 사용 정의, 연구진 확인"),
    "NCT06468397:E21": (["'Fellow eye:' 표지는 반대쪽 눈 조건의 하위 항목인가? 가족력 조건이 반대쪽 눈과 어떤 관계인가?",
                         "가족의 범위(직계·형제 등)는?"], "프로토콜 원문 전체 맥락(잠긴 기준표 밖 원문)"),
    "NCT06756191:E1": (["암포테리신 B 투여가 폐 털곰팡이증 치료(같은 에피소드)여야 하는가? 아니면 다른 이유의 투여도 포함하는가?",
                        "'5일 초과'는 누적 투여 기간인가, 1회 치료 기간인가?",
                        "위 두 가지가 정해지면 unsupported 노드를 빼고 complete로 바꿀 수 있는가?"],
                       "프로토콜 정의, 연구진 확인 (이 시험의 대상이 현재 PM 에피소드 환자인지)"),
    "NCT06908096:E8": (["'febrile illness'의 체온 기준(예: 38.0°C 이상)은? 환자 자기 보고 '열이 나는 병'으로 충분한가?",
                        "기준 시점 첫 투여(first_dose)를 답변 시점으로 근사해도 되는가?"], "프로토콜의 발열 정의"),
    "NCT06908096:E11": (["유전성·후천성·특발성 = 모든 혈관부종으로 보아도 되는가? 약물(ACE 억제제 등) 유발 혈관부종은?"],
                        "임상 판단"),
    "NCT06908096:E12": (["'within the past year'의 기준 시점(screening) 가정이 맞는가?",
                         "'증상이 마지막으로 있었던 시기'로 묻는 것이 기준(1년 내 특발성 두드러기)과 같은가? 진단 없이 증상만 있던 경우는?"],
                        "프로토콜 정의, 임상 판단"),
    "NCT06908096:E25": (["HIV 진단 이력 = 현재 감염으로 해석해도 되는가?", "자기 보고로 충분한가, 진료 기록(clinical_record)이 필요한가?"],
                        "임상 판단, 근거 수준 정책"),
    "NCT06916494:I3": (["'systemic immunotherapy'는 항암 면역요법만인가(알레르기 면역요법 등 포함 여부)?",
                        "'target therapy'의 범위는?", "범위가 정해지면 complete로 바꿀 수 있는가?"], "프로토콜 정의"),
    "NCT07087314:E15": (["'major surgery' 정의는? 현재 IR은 '수술 없음'만 결정 가능 - 큰 수술 여부를 묻는 계약이 필요한가?",
                         "기준 시점 첫 투여 근사 허용 여부"], "프로토콜 정의 (예: 전신마취·입원 기준)"),
    "NCT07087314:E14": (["환자가 생백신 여부를 스스로 구분할 수 있는가? 예방접종 기록 확인(clinical_record)이 필요한가?",
                         "기준 시점 첫 투여 근사 허용 여부"], "임상 판단, 예방접종 기록 확보 가능성"),
    "NCT07087314:I11": (["원문에 기준 시점이 없음: 등록(enrollment)으로 두어도 되는가?", "2개월을 60일로 환산해도 되는가?"],
                        "프로토콜 정의"),
    "NCT07087314:I12": (["원문에 기준 시점이 없음: 등록(enrollment)으로 두어도 되는가?", "6개월을 180일로 환산해도 되는가?",
                         "상위 개념 계약(QC-ANTICD20-LAST, 항CD20 항체)으로 리툭시맙·오비누투주맙을 함께 물어도 되는가?"],
                        "프로토콜 정의"),
    "NCT07679893:E25": (["'recently smoked'의 기간은? (현재 IR은 현재 흡연 또는 1년 내 흡연, 'recently' 부분은 미지원)",
                         "전자담배·가열담배를 포함하는가?"], "프로토콜 정의"),
}
CONTRACT_CLINICAL = {
    "QC-ACTH-LAST": (["질문에 진단 목적 투여 포함 여부를 밝혀야 하는가?"], "NCT06125405:I12 검토 결과"),
    "QC-ANTICANCER-LAST": (["호르몬 치료·시험약을 '전신 항암 치료'에 포함하는지 선택지에 밝혀야 하는가?"], "NCT06833073:E5 검토 결과"),
    "QC-FEBRILE-ILLNESS-LAST": (["체온 기준을 질문에 넣어야 하는가?"], "NCT06908096:E8 검토 결과"),
    "QC-LIVE-VACCINE-LAST": (["예시 백신(MMR·수두·대상포진 생백신 등)을 질문에 넣어야 하는가? 모름 응답 비율이 높을 것인가?"],
                             "임상 판단"),
    "QC-PREGNANT-NOW": (["자기 보고 '아니요'로 '현재 임신 아님'을 확정해도 되는가, 임신 검사가 필요한가?"],
                        "임상 판단 (8개 기준이 이 계약을 공유)"),
    "QC-SMOKING-LAST": (["전자담배·가열담배를 질문에 포함해야 하는가?"], "NCT07679893:E25 검토 결과"),
    "QC-AMPHOTERICIN-COURSE": (["'5일'을 누적/1회 중 무엇으로 물어야 하는가? 질문 문장에 밝혀야 하는가?"], "NCT06756191:E1 검토 결과"),
    "QC-TRIAL-PARTICIPATION-LAST": (["'참여'의 종료 기준(마지막 시험약 투여·마지막 방문·추적관찰 포함 여부)은?"],
                                    "프로토콜 정의 (NCT06125405:E13)"),
    "QC-CANCER-VACCINE-EVER": (["예방 백신(HPV·B형간염)을 제외하는 것이 기준 의도와 맞는가?"], "프로토콜 정의 (NCT06833073:E6)"),
}
# 현재 IR로 표현 불가: (지원되지 않는 이유, 향후 확장 요건)
INEXPRESS = {
    "NCT00575185:E6": ("'during the study'(연구 기간 중 수유)는 미래 행동이라 현재 사실 명제가 아님",
                       "미래 의향(intent) 양상을 명제에 추가하거나, 동의 절차(연구 중 수유하지 않기로 동의)로 판정에서 분리"),
    "NCT06468397:E5": ("'to be confirmed on treatment day'는 치료일 확인 절차(시점이 미래)",
                       "절차 요건 표기(procedural)를 IR에 추가하고 판정 결과에 '치료일 재확인 필요'로 표시"),
    "NCT06727604:E1": ("방사성 요오드 치료의 '성공' 여부는 검사 결과에 근거한 의료진 판단",
                       "clinical_record 근거와 결과 지표(measurement 명제) 또는 의료진 확인 항목으로 표현"),
    "NCT06833073:E5": ("'including investigational agents'의 범위(시험약을 전신 항암 치료에 포함하는지)가 정해지지 않음",
                       "개념 표에서 investigational_agent와 systemic_anticancer_therapy의 관계 결정 + 계약 선택지에 시험약 명시"),
    "NCT06908096:E1": ("'planning to become pregnant'는 미래 의향", "NCT00575185:E6과 같음 (미래 의향 양상 또는 동의 절차)"),
    "NCT06908096:E3": ("'Immunomodulating therapy'의 정의(약제 범위)가 원문에 없음",
                       "임상 검수자가 면역조절 치료 약제 목록을 정하고 개념 표·계약에 추가한 뒤 IR 작성"),
    "NCT06908096:E5": ("'planning to receive investigational products while on study'는 미래 의향",
                       "NCT00575185:E6과 같음. 과거 30일 시험약 부분은 이미 표현됨(기준 시점 근사 결정 필요)"),
}
CHECKLIST = ["원문 구간(source_span)이 잠긴 기준표 원문과 같고 빠진 조건이 없다",
             "기간·환산(개월->일)과 기준 시점이 원문과 같다 (기준 시점 근사는 anchor_policy로만)",
             "한정어(major, non-diagnostic 등)·예외(excluding ...)가 빠지지 않았다",
             "근거 수준(self_report_ok / clinical_record)이 기준에 맞다",
             "계약 질문이 명제를 정확히 묻고, 각 선택지의 명제 사실이 선택지 문장과 같다",
             "환자(보호자)가 이 질문에 답할 수 있다 (모름은 미확인으로 남음)",
             "검증 사례의 기대값이 원문 의미와 같다"]


def _prop_ko(key: str) -> str:
    p = Proposition.parse(key)
    c = CONCEPTS[p.concept]
    q = "".join(f"[{QUALIFIERS[x]}]" for x in sorted(p.qualifiers))
    s = p.scope
    when = ("현재" if s.kind == "current" else "평생(한 번이라도)" if s.kind == "ever"
            else f"{ANCHOR_KO[s.anchor]} 전 {s.days}일 이내")
    return f"{c.label_ko}{q} · {when}"


def _node_ko(n: dict) -> str:
    op = n["op"]
    if op == "atom":
        return f"⟨{_prop_ko(n['prop'])}⟩"
    if op == "unsupported":
        return f"[미지원: {n['text']} - {n['reason']}]"
    if op == "not":
        return f"아님({_node_ko(n['arg'])})"
    if op == "compare":
        return f"[수치 비교 {n}]"
    joiner = " 그리고 " if op == "and" else " 또는 "
    return "(" + joiner.join(_node_ko(a) for a in n["args"]) + ")"


def _value_ko(v: bool) -> str:
    return "참" if v else "거짓"


def build() -> str:
    reg = Registry()
    cr = ContractRegistry(reg)
    cases = json.loads((ROOT / "contracts" / "ir_cases.json").read_text(encoding="utf-8"))["criteria"]
    recs = {f"{t}:{rid}": rec for t in reg.trial_ids() for rid, rec in reg.trial_records(t).items()}
    missing = sorted((set(k for k, v in IR_REVIEW.items() if v[0] == "clinical") ^ set(CLINICAL))
                     | (set(k for k, v in IR_REVIEW.items() if v[0] == "inexpress") ^ set(INEXPRESS))
                     | (set(k for k, v in CONTRACT_REVIEW.items() if v[0] == "clinical") ^ set(CONTRACT_CLINICAL)))
    if missing:
        raise SystemExit(f"검토 질문·사유가 분류와 맞지 않음: {missing}")
    out = ["# IR·질문 계약 기준별 검수 자료 (자동 생성: scripts/build_review_package.py)", "",
           "**모든 초안은 미승인입니다. 분류·검토 질문은 AI가 만든 사전 자료이며 승인이 아닙니다.** 승인은 임상 검수자가 아래 "
           "digest를 확인하고 검수 기록을 남길 때만 효력이 있고, 초안 내용이 바뀌면 digest가 바뀌어 이전 승인은 무효입니다.", "",
           "검수 기록 (사람만, 자동화 이름·사유 없는 승인은 거부됨):", "",
           "```python", "from ctrec.ir_review import Registry",
           "Registry().record_review(\"ir\", \"<기준>\", \"<IR digest>\", \"approved\" | \"rejected\",",
           "                         reviewer=\"<검수자 이름>\", note=\"<사유>\", clinical_review_confirmed=True)",
           "Registry().record_review(\"contracts\", \"<계약 ID>\", \"<계약 digest>\", ...)  # 계약도 같은 방식", "```", "",
           "기간 기준은 기준 시점 근사(anchor_policy) 정책 결정 후 검수자가 IR에 추가해야 판정에 쓰입니다(추가하면 digest가 바뀜).", ""]
    order = sorted(recs, key=lambda k: (PRIORITY[IR_REVIEW[k][0]], k))
    section = None
    for key in order:
        cls, risk = IR_REVIEW[key]
        if cls != section:
            section = cls
            n = sum(1 for k in order if IR_REVIEW[k][0] == cls)
            out += [f"## {LABEL[cls]} ({n})", ""]
        ir = recs[key].ir
        cc = cases[key]
        anchors = sorted({Proposition.parse(x["prop"]).scope.anchor for x in walk(ir.condition) if x.get("op") == "atom"}
                         - {"assessment"})
        out += [f"### {key} ({'포함' if cc['kind'] == 'inclusion' else '제외'} 기준)", "",
                f"- 원문: \"{ir.criterion_text}\"",
                f"- IR (사람이 읽는 형태): {_node_ko(ir.condition)}",
                f"- 완전성 {ir.completeness} / 근거 수준 {ir.source_requirement} / 기준 시점 근사 필요: "
                f"{', '.join(ANCHOR_KO[a] for a in anchors) or '없음'}",
                f"- 승인 대상: IR v{ir.ir_version} digest `{ir.digest()}`, 잠긴 원문 해시 `{ir.frozen_file_hash[:16]}…`"]
        if ir.unsupported_parts:
            out.append(f"- 표현하지 못한 부분: {'; '.join(ir.unsupported_parts)}")
        if ir.notes:
            out.append(f"- 초안 메모: {ir.notes}")
        if risk:
            out.append(f"- 위험·확인 사항: {risk}")
        if cls == "clinical":
            qs, ev = CLINICAL[key]
            out += ["- 임상 검토 질문:", *[f"  {i}. {q}" for i, q in enumerate(qs, 1)], f"- 판단에 필요한 증거: {ev}"]
        if cls == "inexpress":
            why, ext = INEXPRESS[key]
            out += [f"- 지원되지 않는 이유: {why}", f"- 향후 확장 요건: {ext}",
                    "- 현재 동작: 부분 IR이므로 결정적 판정에 쓰이지 않음 (기존 판정·질문 경로 유지)"]
        out.append("- 계약 후보:" if cc["contract_candidates"] else "- 계약 후보: 없음 (진료 기록 확인 경로)")
        for cid in cc["contract_candidates"]:
            c = cr.raw(cid)
            ccls = CONTRACT_REVIEW.get(cid, ("ready", ""))[0]
            out.append(f"  - `{cid}` v{c.version} ({LABEL[ccls]}) digest `{c.digest()}`: \"{c.question}\"")
            for o in c.options:
                facts = ", ".join(f"⟨{_prop_ko(k)}⟩ = {_value_ko(v)}" for k, v in o["assertions"])
                out.append(f"    - \"{o['label']}\" -> {facts}")
        out.append("- 검증 사례 (사실 -> 기대 판정):")
        for case in cc["cases"]:
            facts = ", ".join(f"⟨{_prop_ko(k)}⟩ = {_value_ko(v)}" for k, v in case["facts"]) or "사실 없음"
            out.append(f"  - {facts} -> {case['expect']}")
        out += ["- 검수 체크리스트:", *[f"  - [ ] {c}" for c in CHECKLIST], ""]
    out += [f"## 임상 전문가 검토가 필요한 질문 계약 ({len(CONTRACT_CLINICAL)})", ""]
    for cid in sorted(CONTRACT_CLINICAL):
        c = cr.raw(cid)
        qs, ev = CONTRACT_CLINICAL[cid]
        out += [f"### {cid} v{c.version}", "", f"- 질문: \"{c.question}\"", f"- digest `{c.digest()}`",
                f"- 위험·확인 사항: {CONTRACT_REVIEW[cid][1]}", "- 검토 질문:",
                *[f"  {i}. {q}" for i, q in enumerate(qs, 1)], f"- 판단에 필요한 증거: {ev}", ""]
    return "\n".join(out).rstrip() + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    text = build()
    if args.check:
        same = OUT.exists() and OUT.read_text(encoding="utf-8") == text
        print("최신" if same else "다름: scripts/build_review_package.py로 다시 작성하세요")
        return 0 if same else 1
    OUT.write_text(text, encoding="utf-8")
    print(f"작성: {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
