"""IR·질문 계약 초안의 검수 준비 상태표 생성 (P7). 승인 기록을 만들거나 바꾸지 않습니다.

    python scripts/report_contract_drafts.py           # docs/review/contract_draft_review.md 작성
    python scripts/report_contract_drafts.py --check   # 표가 최신인지만 확인

분류 (사람 검수 전 사전 분류이며 승인이 아님)
- ready      : 검수 준비 완료 (원문 대조·형식·정적 검사에서 발견한 문제 없음. 사람 검수 필요)
- fix        : 수정 필요 (현재 IR 체계로 표현 가능하나 초안이 원문을 덜 표현함)
- clinical   : 임상 전문가 검토 필요 (원문 해석·의학적 범위·환자 답변 신뢰도 판단이 필요)
- inexpress  : 현재 IR로 표현 불가 (미래 의향·절차·정의 없는 범위 등, unsupported로 남김)
정적 검사(코드로 계산): 형식 오류, 잠긴 원문 일치, 기준 시점 결정 필요, 근거 수준, 계약 후보의 결정력, 계약 공유 범위, 미사용 계약.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ctrec.condition_ir import CriterionIR, walk  # noqa: E402
from ctrec.ir_review import Registry  # noqa: E402
from ctrec.propositions import Proposition, TimeScope, ancestors, entails  # noqa: E402
from ctrec.question_contract import ContractRegistry  # noqa: E402

OUT = ROOT / "docs" / "review" / "contract_draft_review.md"
LABEL = {"ready": "검수 준비 완료", "fix": "수정 필요", "clinical": "임상 전문가 검토 필요", "inexpress": "현재 IR로 표현 불가"}
PRIORITY = {"clinical": 1, "fix": 2, "ready": 3, "inexpress": 4}

# 의미 판단 (사람이 다시 확인할 사전 분류). key -> (분류, 위험·확인 사항)
IR_REVIEW = {
    "DEMO-LUNG-001:E2": ("ready", "환자가 'ADC' 용어를 모를 수 있음 (모름 -> 미확인으로 남음)"),
    "DEMO-LUNG-001:E3": ("ready", "'스테로이드가 필요했던 ILD'를 한 개념으로 둠"),
    "DEMO-LUNG-001:E4": ("ready", "현재 임신·수유 자기 보고의 신뢰도는 QC-PREGNANT-NOW 검토 사항"),
    "DEMO-LUNG-001:E5": ("ready", "clinical_record로 상향: 자기 보고만으로는 UNVERIFIED (계약 없음)"),
    "NCT07087314:E15": ("clinical", "'major' 정의 없음, 기준 시점 first_dose. '수술 없음'만 결정 가능"),
    "NCT07087314:E13": ("ready", "환자가 'CD38' 치료명을 모를 수 있음"),
    "NCT07087314:E14": ("clinical", "환자가 생백신 여부를 구분할 수 있는지 (답 신뢰도), 기준 시점 first_dose"),
    "NCT07087314:I11": ("clinical", "원문에 기준 시점이 없어 enrollment로 가정, 2개월=60일 환산"),
    "NCT07087314:I12": ("clinical", "기준 시점 가정(enrollment), 6개월=180일 환산"),
    "NCT07087314:E11": ("ready", ""),
    "NCT07087314:E1": ("ready", "가족 범위(직계 등)가 원문에 없음 - 질문은 '가족 중'"),
    "NCT06391359:E5": ("ready", ""),
    "NCT06727604:E1": ("inexpress", "'successfully treated'는 의료진 판단 (부분 IR)"),
    "NCT06294847:E1": ("ready", ""),
    "NCT06294847:E5": ("ready", ""),
    "NCT06080789:E4": ("ready", "P7에서 수정: necrosectomy를 췌장 괴사 제거술로 한정 (다른 부위 제거술로 잘못 제외하던 결함)"),
    "NCT06833073:E4": ("ready", "기준 시점 randomization 근사는 팀 결정"),
    "NCT06833073:E5": ("inexpress", "'including investigational agents' 범위 (부분 IR)"),
    "NCT06833073:E6": ("ready", ""),
    "NCT06833073:E10": ("ready", "clinical_record (활동성 결핵)"),
    "NCT06833073:E11": ("ready", ""),
    "NCT06833073:E12": ("ready", ""),
    "NCT06916494:I3": ("clinical", "systemic immunotherapy·target therapy의 범위 (부분 IR)"),
    "NCT04063540:E9": ("ready", ""),
    "NCT04761523:E6": ("ready", ""),
    "NCT06756191:E13": ("ready", ""),
    "NCT07369063:E9": ("ready", ""),
    "NCT00575185:E6": ("inexpress", "'during the study' 미래 수유 (부분 IR)"),
    "NCT06468397:E5": ("inexpress", "'to be confirmed on treatment day' 절차 (부분 IR)"),
    "NCT06468397:E21": ("clinical", "'Fellow eye:' 표지와 가족력의 관계 불분명"),
    "NCT06756191:E1": ("clinical", "P8 수정: '5일 초과'를 한정어(over_5d)로 표현. 남은 쟁점: 투여가 폐 털곰팡이증 치료(같은 "
                                   "에피소드)여야 하는지 (부분 IR: 결정적 판정에 쓰이지 않음), 5일이 누적인지 1회 투여 기간인지"),
    "NCT06756191:E10": ("ready", ""),
    "NCT06756191:E11": ("ready", ""),
    "NCT06756191:E12": ("ready", "현재 ECMO 중인 환자는 직접 답하기 어려움 (보호자 답)"),
    "NCT06908096:E1": ("inexpress", "임신 계획(미래 의향)"),
    "NCT06908096:E3": ("inexpress", "면역조절 치료 범위가 원문에 없음"),
    "NCT06908096:E4": ("ready", "기준 시점 first_dose 근사는 팀 결정"),
    "NCT06908096:E5": ("inexpress", "연구 중 시험약 계획(미래 의향)"),
    "NCT06908096:E7": ("ready", ""),
    "NCT06908096:E8": ("clinical", "'febrile illness' 체온 기준 정의, 기준 시점 first_dose"),
    "NCT06908096:E11": ("clinical", "'유전성·후천성·특발성 = 모든 혈관부종' 해석 확인"),
    "NCT06908096:E12": ("clinical", "'within the past year' 기준 시점을 screening으로 가정"),
    "NCT06908096:E14": ("ready", "임신성 당뇨 제외는 개념 구분으로 표현"),
    "NCT06908096:E22": ("ready", ""),
    "NCT06908096:E23": ("ready", ""),
    "NCT06908096:E25": ("clinical", "'HIV 진단 이력 = 감염' 해석 확인"),
    "NCT07516951:E1": ("ready", "clinical_record ('documented')"),
    "NCT07516951:E2": ("ready", "clinical_record ('Documented'), 계약 없음"),
    "NCT06125405:I7": ("ready", "6개월=180일 환산 확인"),
    "NCT06125405:I8": ("ready", "3개월=90일 환산 확인"),
    "NCT06125405:I9": ("ready", "3개월=90일 환산 확인"),
    "NCT06125405:I10": ("ready", "3개월=90일 환산 확인"),
    "NCT06125405:I11": ("ready", "3개월=90일 환산 확인"),
    "NCT06125405:I12": ("clinical", "진단 목적 ACTH(자극 검사) 투여가 'used'에 포함되는지"),
    "NCT06125405:E1": ("ready", ""),
    "NCT06125405:E13": ("ready", "'참여' 정의(동의 시점·투약 시점) 확인"),
    "NCT07679893:E25": ("clinical", "'recently smoked' 기간 정의, 전자담배 포함 여부 (부분 IR)"),
}

CONTRACT_REVIEW = {
    "QC-PREGNANT-NOW": ("clinical", "초기 임신은 본인이 모를 수 있음. 자기 보고 '아니요'로 '임신 아님'을 확정해도 되는지 (검사 요구 여부)"),
    "QC-LIVE-VACCINE-LAST": ("clinical", "환자가 생백신 여부를 구분하기 어려움"),
    "QC-ANTICANCER-LAST": ("clinical", "호르몬 치료 포함 여부가 선택지에 드러나지 않음"),
    "QC-SMOKING-LAST": ("clinical", "전자담배·가열담배 포함 여부"),
    "QC-ACTH-LAST": ("clinical", "진단 목적 ACTH 투여 포함 여부"),
    "QC-TRIAL-PARTICIPATION-LAST": ("clinical", "P8 수정: '참여한 시기'가 시작 시점으로 읽혀 현재 참여 중인 환자가 '3개월보다 "
                                                "전'을 고를 수 있었음(잘못된 통과) -> 마지막 참여 시점·현재 참여 안내. "
                                                "'참여' 종료 기준(마지막 투약·방문·추적관찰 포함 여부)은 임상 결정"),
    "QC-CANCER-VACCINE-EVER": ("clinical", "P8 수정: '암 백신'이 예방 백신(HPV 등)으로 읽힐 수 있었음(잘못된 제외) -> 치료용 "
                                           "명시. 예방 백신 제외가 기준 의도와 맞는지 임상 확인"),
    "QC-AMPHOTERICIN-COURSE": ("clinical", "P8 신규(QC-AMPHOTERICIN-EVER 대체): '5일'이 누적 기간인지 1회 투여 기간인지 "
                                           "환자가 다르게 해석할 수 있음"),
    "QC-FEBRILE-ILLNESS-LAST": ("clinical", "'열이 나는 병'의 체온 기준"),
    "QC-SURGERY-LAST": ("ready", "'시술 제외' 안내가 환자마다 다르게 해석될 수 있음 (복강경 수술은 수술)"),
    "QC-URTICARIA-LAST": ("ready", "P7에서 수정: '진단 시기' -> '증상이 마지막으로 있었던 시기' (기준은 1년 내 증상)"),
    "QC-PANCREATIC-NECROSECTOMY-EVER": ("ready", "P7에서 수정: 췌장 괴사 제거술로 한정"),
}


def _decides(contract, atoms) -> bool:
    return any(entails(Proposition.parse(k), v, a) is not None
               for o in contract.options for k, v in o["assertions"] for a in atoms)


def build() -> str:
    reg = Registry()
    cr = ContractRegistry(reg)
    cases = json.loads((ROOT / "contracts" / "ir_cases.json").read_text(encoding="utf-8"))["criteria"]
    irs = {}
    for t in reg.trial_ids():
        for rid, rec in reg.trial_records(t).items():
            irs[f"{t}:{rid}"] = rec
    missing = sorted(set(irs) ^ set(IR_REVIEW))
    if missing:
        raise SystemExit(f"분류가 없거나 남는 IR: {missing}")
    used = defaultdict(list)
    rows = []
    for key, rec in sorted(irs.items(), key=lambda kv: (PRIORITY[IR_REVIEW[kv[0]][0]], kv[0])):
        ir: CriterionIR = rec.ir
        atoms = []
        for n in walk(ir.condition):
            if n.get("op") == "atom":
                p = Proposition.parse(n["prop"])
                atoms.append(p.with_scope(TimeScope("window", p.scope.days)) if p.scope.kind == "window" else p)
        anchors = sorted(ir.anchors() - {"assessment"})
        hier = sorted({a.concept for a in atoms if len(ancestors(a.concept)) > 1})
        cands = cases[key]["contract_candidates"]
        weak = [c for c in cands if not _decides(cr.raw(c), atoms)]
        for c in cands:
            used[c].append(key)
        cls, note = IR_REVIEW[key]
        static = []
        if rec.status != "unreviewed":
            static.append(f"상태 {rec.status} {rec.reasons}")
        if anchors:
            static.append("기준 시점 결정 필요: " + ", ".join(anchors))
        if weak:
            static.append("결정 못 하는 계약 후보: " + ", ".join(weak))
        if not cands:
            static.append("계약 후보 없음 (기록 확인 경로)")
        rows.append(f"| {key} | {LABEL[cls]} | {ir.completeness} | {ir.source_requirement} | "
                    f"{', '.join(sorted({a.key for a in atoms})) or '-'} | {', '.join(hier) or '-'} | "
                    f"{', '.join(cands) or '-'} | {'; '.join(static) or '-'} | {note or '-'} |")
    contracts = json.loads((ROOT / "contracts" / "question_contracts.json").read_text(encoding="utf-8"))["contracts"]
    crow = []
    for c in sorted(contracts, key=lambda c: (PRIORITY[CONTRACT_REVIEW.get(c["contract_id"], ("ready", ""))[0]],
                                              c["contract_id"])):
        cid = c["contract_id"]
        st, errs = cr.status(cid)
        cls, note = CONTRACT_REVIEW.get(cid, ("ready", ""))
        keys = sorted({k for o in c["options"] for k, _ in o["assertions"]})
        static = [] if st == "unreviewed" else [f"상태 {st} {errs}"]
        if not used[cid]:
            static.append("어느 IR의 후보도 아님 (미사용)")
        crow.append(f"| {cid} | {LABEL[cls]} | {c['answer_type']} | {len(c['options'])} | {', '.join(keys)} | "
                    f"{len(used[cid])} ({', '.join(used[cid]) or '-'}) | {'; '.join(static) or '-'} | {note or '-'} |")
    count = defaultdict(int)
    for k in IR_REVIEW.values():
        count[k[0]] += 1
    ccount = defaultdict(int)
    for c in contracts:
        ccount[CONTRACT_REVIEW.get(c["contract_id"], ("ready", ""))[0]] += 1
    head = ("# IR·질문 계약 초안 검수표 (자동 생성: scripts/report_contract_drafts.py)\n\n"
            "**모든 초안은 미승인(unreviewed)입니다. '검수 준비 완료'는 승인이 아니며, 사람(임상 검수자)의 검수 기록 없이는 "
            "결정적 판정에 쓰이지 않습니다.** 의미 분류는 AI가 만든 사전 분류이므로 검수자가 다시 판단해야 합니다.\n\n"
            "검수 우선순위: 임상 전문가 검토 필요 -> 수정 필요 -> 검수 준비 완료 -> 현재 IR로 표현 불가(부분 IR, 확정에 쓰이지 않음).\n"
            "기간 기준의 '기준 시점 결정 필요'는 팀 정책(P0 K-2) 결정 후 검수자가 anchor_policy를 추가해야 판정에 쓰입니다.\n\n"
            f"## IR {len(irs)}개\n\n"
            + " / ".join(f"{LABEL[k]} {count[k]}" for k in PRIORITY) + "\n\n"
            "| 기준 | 사전 분류 | 완전성 | 근거 수준 | 명제 | 상위 개념 사용 | 계약 후보 | 정적 검사 | 위험·확인 사항 |\n"
            "|---|---|---|---|---|---|---|---|---|\n")
    chead = (f"\n## 질문 계약 {len(contracts)}개\n\n"
             + " / ".join(f"{LABEL[k]} {ccount[k]}" for k in PRIORITY if ccount[k]) + "\n\n"
             "공유 범위: 같은 계약을 여러 IR이 쓰는 것은 명제 키가 같거나 코드로 승인된 함의(넓은 기간 '없음' -> 좁은 기간 "
             "'없음', 하위 개념 '있음' -> 상위 개념 '있음')로만 이어질 때입니다. 아래 '사용 IR'이 그 목록입니다.\n\n"
             "| 계약 | 사전 분류 | 형식 | 선택지 수 | 단언 명제 | 사용 IR | 정적 검사 | 위험·확인 사항 |\n"
             "|---|---|---|---|---|---|---|---|\n")
    return head + "\n".join(rows) + "\n" + chead + "\n".join(crow) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    text = build()
    if args.check:
        ok = OUT.exists() and OUT.read_text(encoding="utf-8") == text
        print("최신" if ok else "다름")
        return 0 if ok else 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"작성: {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
