// 코호트 현황판 (연구용, 읽기 전용). /api/cohorts의 가장 최근 생성 세트를 보여 줌.
// 쌍 상태는 서버가 엔진 계약 0.2의 7가지 PairStatus로 계산해 board로 보냄 (web/cohort_board.py). 화면은 판정 규칙을 갖지 않음.
// 기본 화면은 정책이 보는 정보(public)만 쓰고, '정답 보기'를 켰을 때만 시뮬레이터 정답(oracle)을 함께 표시.
// 데이터는 모두 textContent로 넣음 (innerHTML 없음).
"use strict";

const $ = (id) => document.getElementById(id);
const SLOT_KO = {
  age: "나이", sex: "성별", ecog: "ECOG 수행 상태",
  "lab.hemoglobin": "혈색소(Hb)", "lab.platelets": "혈소판", "lab.egfr": "eGFR", "lab.white blood cell count": "백혈구(WBC)",
  "lab.absolute neutrophil count": "호중구(ANC)", "lab.creatinine": "크레아티닌", lab_age_days: "마지막 검사(일 전)",
  "dx.ns_etiology": "신증후군 원인", "dx.ns_steroid_response": "스테로이드 반응", "dx.active_hepatitis": "활동성 간염",
  "lab.urine_protein_remission": "소변 단백 관해", "lab.hbv_status": "B형간염 표지자", "lab.hcv_status": "C형간염 표지자",
  "lab.hiv_ab": "HIV 항체", "lab.liver_dysfunction_sustained": "간기능 이상 지속", "lab.inr_within_1_5uln": "INR ≤1.5×ULN",
  "med.anti_cd20_recency": "최근 항-CD20", "med.steroid_sparing_recent": "최근 스테로이드 절약제", "med.acth_3m": "ACTH(3개월)",
  "med.live_vaccine_4w": "생백신(4주)", "med.transfusion_or_gf_7d": "수혈·성장인자(7일)",
  "cm.family_ns_history": "신장질환 가족력", "cm.chronic_active_infection": "만성 활동성 감염", "cm.acute_infection": "급성 감염",
  "cm.immune_onc": "자가면역·면역결핍·종양", "cm.severe_drug_immunosuppression": "약물성 심한 면역저하",
  "cm.heme_endocrine": "혈액·내분비 질환", "cm.serious_organ_disease": "중대한 장기 질환",
  "tx.other_trial_3m": "다른 시험 참여(3개월)", "tx.rtx_first_course_for_ns": "리툭시맙 첫 치료", "tx.prior_anti_cd38": "이전 항-CD38",
  "proc.major_surgery_4w": "주요 수술(4주)", "other.followup_1y": "추적 1년 이상", "other.rec_rtx_admin": "기록: 리툭시맙 투여",
  "other.rec_relapse_pre_1y": "기록: 투여 전 재발", "other.rec_followup_outcomes": "기록: 추적 결과", "other.rec_cd19_followup": "기록: CD19 추적",
  "judge.investigator_unsuitable": "연구자 판단",
};
const EVIDENCE_KO = { record: "진료 기록", self_report: "자기 보고", physician: "의사 판단" };
// 쌍 상태 7가지 (engine.contracts.PairStatus). 순서는 서버의 status_order를 따름
const STATUS_UI = {
  CONFIRMED_ELIGIBLE: { cls: "ok", label: "적격 확정", desc: "필수 기준을 모두 근거로 확인" },
  CONFIRMED_INELIGIBLE: { cls: "bad", label: "부적격 확정", desc: "기록으로 확인된 불충족 기준이 있음" },
  VERIFICATION_PENDING: { cls: "warn", label: "확인 대기", desc: "값은 있으나 기록 확인·상충 해소 필요" },
  INFO_MISSING: { cls: "info", label: "정보 부족", desc: "판정에 필요한 정보가 없거나 검사가 오래됨" },
  EXPERT_REVIEW: { cls: "violet", label: "의료진 검토", desc: "임상의 판단이 필요한 기준이 남음" },
  BLOCKED_FAILURE: { cls: "muted", label: "확보 실패 보류", desc: "확인 수단을 모두 써도 정보를 얻지 못함" },
  NOT_EVALUATED: { cls: "", label: "미평가", desc: "평가하지 못함 (부적격 아님)" },
};
const GAP_KO = { missing: "정보 없음", stale: "검사 오래됨", unverified: "기록 확인 필요", conflict: "정보 상충",
  physician_review: "의료진 판단 필요" };
// 정보원 이름: 생성기 ID(S_*)와 엔진 계약 I-5 ID를 모두 표시 (Q-G1 통일 전후 공통)
const SOURCE_KO = {
  S_lab: "혈액검사 결과지", S_path: "병리·유전자 결과지", S_med: "처방 목록", S_summary: "진료·퇴원 요약",
  S_chart: "전체 진료기록", S_phone: "환자 전화", S_md: "담당의 문의",
  patient_interview: "환자 문진", lab_report: "검사 결과지", pathology_report: "병리·유전자 결과지",
  medication_list: "처방 목록", medical_record: "진료기록", clinician_review: "의료진 검토",
  system_check: "시스템 확인", system_reparse: "재추출", trial_registry: "시험 등록 정보",
};
const sourceName = (id) => {
  const [base, slot] = String(id).split(":");
  return (SOURCE_KO[base] || base) + (slot ? ` · ${slotName(slot)}` : "");
};
const HOLDS_KO = { yes: "성립", no: "불성립", unknown: "미확인" };

let list = [];
let ko = {};
let current = null;
let selected = null;

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else e.setAttribute(k, v);
  }
  for (const c of kids) if (c != null) e.append(c);
  return e;
}
const slotName = (s) => SLOT_KO[s] || s;
const trialTitle = (t) => (ko[t] && ko[t].title) || t;
const ruleText = (t, rid) => {
  const r = ko[t] && ko[t].rules && ko[t].rules[rid];
  return r ? (r.ko || r.en || "") : "";
};
const passes = (kind, h) => (h === "unknown" ? null : kind === "inclusion" ? h === "yes" : h === "no");
const showOracle = () => $("oracle").checked;

async function getJSON(url) {
  const r = await fetch(url, { headers: { Accept: "application/json" } });
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  return r.json();
}

// 서버가 계산한 쌍 상태 (정책이 지금 아는 정보 = public만 사용). 미확정이면 남은 기준 수를 붙임
function pairStatus(c, pid, t) {
  const b = c.board.pairs[`${pid}|${t}`];
  const ui = STATUS_UI[b.status] || { cls: "", label: b.status };
  const open = b.rules.filter((r) => r.gap !== "none").length;
  const confirmed = b.status.startsWith("CONFIRMED_");
  return { ...ui, status: b.status, open, label: open && !confirmed ? `${ui.label} · ${open}개` : ui.label };
}
const ruleGap = (c, pid, t, rid) => {
  const r = c.board.pairs[`${pid}|${t}`].rules.find((x) => x.rule_id === rid);
  return r ? r.gap : "none";
};

function fillSelectors() {
  const scenarios = [...new Set(list.map((c) => c.cohort_id.replace(/-r\d+$/, "")))];
  $("scenario").replaceChildren(...scenarios.map((s) => el("option", { value: s, text: s })));
  fillReplicates();
}

function fillReplicates() {
  const s = $("scenario").value;
  const reps = list.filter((c) => c.cohort_id.replace(/-r\d+$/, "") === s);
  $("replicate").replaceChildren(...reps.map((c) =>
    el("option", { value: c.cohort_id, text: `${c.cohort_id.slice(-3)} · ${c.split === "dev" ? "개발용" : "평가용"}${c.null ? " · 영 시나리오" : ""}` })));
}

async function load(id) {
  current = await getJSON(`/api/cohorts/${encodeURIComponent(id)}`);
  selected = null;
  render();
}

function render() {
  const c = current;
  const k = c.knobs;
  $("knobs").replaceChildren(...[
    el("span", { class: "badge", text: `환자당 후보 시험 ${k.trials_per_patient}개` }),
    el("span", { class: "badge", text: k.cost_heterogeneity === 0 ? "비용 균일" : `비용 이질성 ${k.cost_heterogeneity}` }),
    el("span", { class: "badge", text: k.grouping === "source" ? "소스 묶음 있음" : "소스 묶음 없음 (정보별 확인)" }),
    el("span", { class: "badge", text: k.enrollment_remaining == null ? "정원 제한 없음" : `시험별 남은 정원 ${k.enrollment_remaining}명` }),
    c.null ? el("span", { class: "badge accent wrap", text: "영 시나리오: 시험 겹침·비용 차이·소스 묶음을 없앤 대조 조건" }) : null,
  ].filter(Boolean));  // replaceChildren(null)은 "null" 글자를 넣으므로 빼고 넘김
  renderTiles();
  renderUnbound();
  renderMatrix();
  renderSources();
  $("detail-panel").hidden = true;
}

function tile(label, value, sub, cls = "") {
  return el("div", { class: `tile ${cls}` }, el("div", { class: "k", text: label }), el("div", { class: "v", text: String(value) }),
    sub ? el("div", { class: "s", text: sub }) : null);
}

function renderTiles() {
  const c = current;
  let pairs = 0, trueEligible = 0;
  for (const p of c.public.patients) for (const t of p.candidate_trials) {
    pairs++;
    if (c.oracle.pair_label[`${p.patient_id}|${t}`] === "ELIGIBLE") trueEligible++;
  }
  const counts = c.board.status_counts;
  const kids = [
    tile("환자", c.public.patients.length, `시험 ${c.public.trials.length}개`),
    tile("환자–시험 쌍", pairs, "상태 기준: 지금 정보"),
    ...c.board.status_order.map((s) => tile(STATUS_UI[s].label, counts[s] || 0, STATUS_UI[s].desc, `st ${STATUS_UI[s].cls}`)),
    tile("확인 수단", Object.keys(c.public.sources).length, "소스 수"),
  ];
  if (showOracle()) kids.push(tile("실제 적격", trueEligible, `정답 기준 · ${pairs ? Math.round((trueEligible / pairs) * 100) : 0}%`));
  $("tiles").replaceChildren(...kids);
}

function renderUnbound() {
  const ub = current.public.unbound_rules;
  const total = Object.values(ub).reduce((a, r) => a + r.length, 0);
  const bound = Object.values(current.public.rules).reduce((a, r) => a + r.length, 0);
  $("unbound-note").hidden = !total;
  if (!total) return;
  $("unbound-note").replaceChildren(el("b", { text: `검수 전 기준 ${total}개는 정답에서 빠져 있습니다. ` }),
    `정답 계산에 쓰인 기준은 ${bound}개(자동 연결 + 사람 검수 완료)이며, 나머지는 '충족'으로 간주했습니다. ` +
    "슬롯 사전 검수가 끝나면 적격률이 낮아지고 확인 수단도 다양해집니다.");
}

function renderMatrix() {
  const c = current;
  const head = el("tr", {}, el("th", { class: "pt", scope: "col", text: "환자 · 미확인 정보" }),
    ...c.public.trials.map((t) => {
      const cap = c.public.enrollment_remaining[t];
      return el("th", { scope: "col", title: trialTitle(t) }, t,
        el("div", { class: "faint", text: cap == null ? trialTitle(t).slice(0, 22) : `남은 정원 ${cap}명` }));
    }));
  const rows = c.public.patients.map((p) => {
    const unk = p.unknown_slots.length ? p.unknown_slots.map(slotName).join(", ") : "없음";
    const cells = c.public.trials.map((t) => {
      if (!p.candidate_trials.includes(t)) return el("td", { class: "na", text: "—" });
      const s = pairStatus(c, p.patient_id, t);
      const btn = el("button", { class: "cell", type: "button", "aria-label": `${p.patient_id} ${t}: ${s.label}` },
        el("span", { class: `badge ${s.cls}`, text: s.label }));
      if (showOracle()) {
        const truth = c.oracle.pair_label[`${p.patient_id}|${t}`];
        btn.append(el("span", { class: "truth", text: `정답: ${truth === "ELIGIBLE" ? "적격" : "부적격"}` }));
      }
      if (selected && selected.pid === p.patient_id && selected.t === t) btn.classList.add("sel");
      btn.addEventListener("click", () => { selected = { pid: p.patient_id, t }; renderMatrix(); renderDetail(); });
      return el("td", {}, btn);
    });
    return el("tr", {}, el("th", { class: "pt", scope: "row" }, el("span", { class: "id", text: p.patient_id }),
      el("span", { class: "unk", text: `미확인: ${unk}` })), ...cells);
  });
  $("matrix").replaceChildren(el("thead", {}, head), el("tbody", {}, ...rows));
}

const SEX_KO = { male: "남성", female: "여성" };
const fmt = (v, slot) => (slot === "sex" ? SEX_KO[v] || v : String(v));

function renderDetail() {
  const c = current;
  const { pid, t } = selected;
  const p = c.public.patients.find((x) => x.patient_id === pid);
  const hidden = c.oracle.hidden[pid] || {};
  const items = c.public.rules[t].map((r) => {
    const key = `${pid}|${t}|${r.rule_id}`;
    const h = c.public.initial_holds[key];
    const slots = [r.slot, ...(r.extra_slots || [])];
    const has = (s) => Object.prototype.hasOwnProperty.call(p.known, s);
    const ps = passes(r.kind, h);
    const gap = ruleGap(c, pid, t, r.rule_id);
    const cls = ps === true ? "ok" : ps === false ? "bad" : gap === "physician_review" ? "violet" : "info";
    const li = el("li", {},
      el("span", { class: "rid", text: r.rule_id }),
      el("span", { class: "badge", text: r.kind === "inclusion" ? "선정" : "제외" }),
      el("span", { class: `badge ${cls}`, text: h === "unknown" ? `미확인 · ${GAP_KO[gap] || gap}` : ps ? "통과" : "불통과" }),
      el("span", { class: "slot", text: slots.map(slotName).join(" + ") }),
      el("span", { class: "val", text: slots.map((s) => (has(s) ? `${slotName(s)} ${fmt(p.known[s], s)}` : `${slotName(s)} 모름`)).join(" · ") }));
    if (r.time_window_days != null) {
      const age = p.known.lab_age_days;
      const stale = age != null && age > r.time_window_days;
      li.append(el("span", { class: `badge ${stale ? "warn" : ""}`, text: `검사 ${r.time_window_days}일 이내 필요 · 마지막 검사 ${age}일 전${stale ? " (새 결과지 필요)" : ""}` }));
    }
    if (showOracle() && slots.some((s) => !has(s))) {
      const th = c.oracle.truth[key];
      const vals = slots.filter((s) => !has(s)).map((s) => `${slotName(s)} ${fmt(hidden[s], s)}`).join(" · ");
      li.append(el("span", { class: "val", text: `→ 실제 값 ${vals} · ${HOLDS_KO[th]}` }));
    }
    const txt = ruleText(t, r.rule_id);
    if (txt) li.append(el("span", { class: "val", text: txt }));
    return li;
  });
  const ub = c.public.unbound_rules[t] || [];
  const st = pairStatus(c, pid, t);
  const kinds = c.board.pairs[`${pid}|${t}`].pending_kinds;
  $("detail").replaceChildren(...[
    el("p", { class: "m0", text: `${pid} · ${t} ${trialTitle(t) !== t ? "· " + trialTitle(t) : ""}` }),
    el("p", { class: "m0 mt6" }, el("span", { class: `badge ${st.cls}`, text: STATUS_UI[st.status] ? STATUS_UI[st.status].label : st.status }),
      " ", el("span", { class: "faint", text: (STATUS_UI[st.status] || {}).desc || "" }),
      kinds.length ? el("span", { class: "faint", text: ` · 남은 작업: ${kinds.map((k) => GAP_KO[k] || k).join(", ")}` }) : null),
    el("ul", { class: "rules mt10" }, ...items),
    ub.length ? el("p", { class: "faint mt10", text: `검수 전이라 정답에서 빠진 기준 ${ub.length}개: ${ub.join(", ")}` }) : null,
  ].filter(Boolean));
  $("detail-panel").hidden = false;
  $("detail-h").focus();
}

function renderSources() {
  const rows = Object.values(current.public.sources).map((s) =>
    el("tr", {}, el("td", { text: sourceName(s.source_id), title: s.source_id }), el("td", { text: `${s.cost_min}분` }),
      el("td", { text: EVIDENCE_KO[s.evidence] || s.evidence }), el("td", { text: s.reveals.map(slotName).join(", ") })));
  $("sources").replaceChildren(
    el("thead", {}, el("tr", {}, ...["소스", "비용", "근거 수준", "드러나는 정보"].map((h) => el("th", { scope: "col", text: h })))),
    el("tbody", {}, ...rows));
}

async function init() {
  try {
    const [res, k] = await Promise.all([getJSON("/api/cohorts"), getJSON("/api/ko").catch(() => ({}))]);
    list = res.cohorts || [];
    ko = k || {};
    if (!list.length) { $("empty").hidden = false; return; }
    $("set-meta").textContent = `생성 세트 ${res.config_hash} · ${res.generator} · 코호트 ${list.length}개`;
    for (const id of ["pick-panel", "tiles", "matrix-panel", "sources-panel"]) $(id).hidden = false;
    fillSelectors();
    $("scenario").addEventListener("change", () => { fillReplicates(); load($("replicate").value); });
    $("replicate").addEventListener("change", () => load($("replicate").value));
    $("oracle").addEventListener("change", () => { renderTiles(); renderMatrix(); if (selected) renderDetail(); });
    await load($("replicate").value);
  } catch (e) {
    $("empty").hidden = false;
    $("empty").textContent = "코호트를 불러오지 못했습니다. 서버 로그를 확인하세요.";
  }
}

init();
