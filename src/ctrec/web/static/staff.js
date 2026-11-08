// 연구 담당자 탭 (index.html의 '연구 담당자', /staff로 바로 열림). API: /api/followup/staff/* (docs/dev/followup_api_contract.md)
// 핵심: 코디네이터 작업 시간을 줄이는 확인 선택 - 질문별로 적용 환자·판정이 정해질 수 있는 쌍·예상 분을 보여 주고 여러 환자에게 한 번에 요청.
// 판정 규칙은 서버에만 있음. 담당자는 환자 답변을 대신 입력할 수 없음. 서류 검증 완료·의료진 판단(#39)은 검증자·임상의 권한으로만:
// 발표용 데모 서버(--demo-staff + 합성 검증 모드)에서는 같은 사례 화면 안에서 그 역할로 입력(X-CTREC-Demo-Role, 서버가 루프백만 허용).
// app.js와 같은 페이지에서 돌므로 전역 이름을 만들지 않음 (즉시 실행 함수).
(() => {
"use strict";

const q$ = (s) => document.querySelector(s);
const h = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const KEY = "ctrec-staff-key", SID = "ctrec-staff-id";
const POLL_MS = 10000;
const S = { key: "", sid: "coordinator", demo: false, roles: [], caseId: null, data: null, vdata: null, cdata: null, timer: null, busy: false,
  view: "q", labels: {}, questions: [] };

// 쌍 상태 7가지 (engine.contracts.PairStatus) - cohort.js와 같은 표기
const PAIR = {
  CONFIRMED_ELIGIBLE: ["ok", "적격 확정"], CONFIRMED_INELIGIBLE: ["bad", "부적격 확정"], VERIFICATION_PENDING: ["warn", "확인 대기"],
  INFO_MISSING: ["info", "정보 부족"], EXPERT_REVIEW: ["violet", "의료진 검토"], BLOCKED_FAILURE: ["muted", "확보 실패 보류"],
  NOT_EVALUATED: ["", "미평가"],
};
// 확정이 아닌 상태의 뜻 (서류·의료진 확인 전에는 확정으로 보이지 않게)
const UNCONFIRMED = { VERIFICATION_PENDING: "미확정 · 기록·서류 확인 전", EXPERT_REVIEW: "미확정 · 의료진 판단 전",
  INFO_MISSING: "미확정 · 정보 부족", BLOCKED_FAILURE: "미확정 · 확보 수단 소진", NOT_EVALUATED: "미확정 · 평가 근거 부족" };
const GAP = { missing: "정보 없음", stale: "검사 오래됨", unverified: "기록 확인 필요", conflict: "정보 상충", physician_review: "의료진 판단 필요",
  exclusion_unverified: "불충족 근거 미검증", sources_exhausted: "확보 수단 소진" };
// 추천 화면 표시 상태 (app.js STATUS와 같은 표기)
const MATCH = { CRITERIA_MATCH: ["ok", "공개 기준 충족"], POTENTIAL_MATCH: ["warn", "기록 확인 시 충족 가능"], NEEDS_REVIEW: ["warn", "의료진 검토 필요"],
  INSUFFICIENT_INFORMATION: ["warn", "추가 정보 필요"], CRITERIA_MISMATCH: ["bad", "공개 기준 불충족"], NOT_EVALUATED: ["warn", "평가 안 됨"] };
const KIND = { ask_patient: "환자 질문", document_review: "서류 확인", clinician_review: "의료진 판단", conflict_resolution: "상충 확인" };
const ACTION = { ASK_PATIENT: "환자 질문", REQUEST_DOCUMENT: "서류 요청", VERIFY_RECORD: "기록 확인", CLINICIAN_REVIEW: "의료진 판단" };
const REQ = {
  pending: ["accent", "환자 답변 대기"], answered: ["ok", "답변 반영"], unknown: ["warn", "환자 '모름'"], unreachable: ["warn", "연락 안 됨"],
  requires_document: ["warn", "서류 확인 필요"], requires_clinician: ["violet", "의료진 판단 필요"], document_unavailable: ["muted", "서류 확인 불가"],
  superseded: ["muted", "대체됨"], cancelled: ["muted", "취소"],
  // #39 완료 상태: 문서 검증자·임상의 역할만 만들 수 있음 (담당자 화면에는 입력 수단 없음). 판정 인정 여부는 서버 권한이 정함
  document_verified: ["ok", "서류 검증됨 (검증자)"], clinician_decided: ["ok", "의료진 판단됨"],
};
// 담당자가 바꿀 수 있는 상태 (followup.models.STAFF_TRANSITIONS와 같음). 완료(답변·서류 확인·판단)는 없음
const MOVES = { ask_patient: { pending: ["unreachable", "cancelled"] }, document_review: { requires_document: ["document_unavailable", "cancelled"] },
  conflict_resolution: { requires_document: ["document_unavailable", "cancelled"] }, clinician_review: { requires_clinician: ["cancelled"] } };
const MOVE_KO = { unreachable: "연락 안 됨으로 기록", document_unavailable: "서류 확인 불가로 기록", cancelled: "요청 취소" };
// #39 계약: 완료는 문서 검증자(/api/followup/verifier)·임상의(/api/followup/clinician) 역할만, 합성 시험 전용 권한일 때만 가능.
// 운영 모드(실제 의료기관 인증 없음)에서는 그 API가 503 -> 요청은 열린 채 남고 판정은 보류
const NOT_SUPPORTED = { requires_document: "서류 확인 완료는 문서 검증자 권한으로만 입력됩니다 (연구 담당자는 완료로 바꿀 수 없음). 운영 모드에서는 검증 권한이 없어 보류로 남습니다.",
  requires_clinician: "의료진 판단은 임상의 권한으로만 입력됩니다 (연구 담당자는 완료로 바꿀 수 없음). 운영 모드에서는 판단 권한이 없어 보류로 남습니다." };
// 항목 이름 (info_key -> 사람이 읽는 이름). 모르면 키 그대로
const ITEM = { age: "나이", sex: "성별", ecog: "전신 상태 (ECOG)", "basis:age": "나이 확인 기록", "review:underspecified": "의료진 판단 기준",
  "biomarker:EGFR": "EGFR 유전자 변이", "biomarker:ALK": "ALK 유전자 변이", "biomarker:PD-L1": "PD-L1 발현" };
const LAB = { hemoglobin: "혈색소 (헤모글로빈)", platelets: "혈소판 수", "absolute neutrophil count": "절대호중구수 (ANC)", anc: "절대호중구수 (ANC)",
  "white blood cell count": "백혈구 수", wbc: "백혈구 수", creatinine: "크레아티닌", "creatinine clearance": "크레아티닌 청소율",
  "total bilirubin": "총빌리루빈", bilirubin: "빌리루빈", alt: "ALT (간 수치)", ast: "AST (간 수치)", albumin: "알부민", inr: "INR (혈액 응고)",
  tsh: "갑상선자극호르몬 (TSH)", potassium: "칼륨", sodium: "나트륨", glucose: "혈당", hba1c: "당화혈색소" };
const itemName = (k) => { const t = String(k ?? "");
  if (ITEM[t]) return ITEM[t];
  if (t.startsWith("lab:")) return `검사 · ${LAB[t.slice(4).toLowerCase()] || t.slice(4)}`;
  if (t.startsWith("biomarker:")) return `바이오마커 · ${t.slice(10)}`;
  const c = t.match(/^criterion:([^:]+):(.+)$/); if (c) return `기준 ${c[1]} ${c[2]}`;
  return t; };
// 확인에 쓰는 정보원 (같은 정보원 = 한 번의 연락·서류로 함께 확인)
const SOURCE = { lab_report: "검사 결과지", pathology_report: "병리 보고서", genomic_report: "유전자 검사 보고서", medical_record: "진료 기록",
  imaging_report: "영상 판독지", clinician_review: "의료진 판단", patient: "환자 질문", patient_interview: "환자 질문" };
const DOC_KIND = { lab_report: "lab_report", pathology_report: "pathology_report", genomic_report: "genomic_report", imaging_report: "imaging_report" };

const badge = (map, k) => { const [c, t] = map[k] || ["", k]; return `<span class="badge ${c}">${h(t)}</span>`; };
const shortId = (s) => String(s || "").slice(0, 14) + (String(s || "").length > 14 ? "…" : "");
const when = (s) => { const d = new Date(s); return isNaN(d) ? h(s) : h(d.toLocaleString("ko-KR")); };
const who = (cid) => S.labels[cid] || shortId(cid);
const min = (m) => `${Number(m || 0).toLocaleString("ko-KR", { maximumFractionDigits: 1 })}분`;

function headers(extra) {  // 발표용 데모 서버는 키 없이 받음 (서버가 판단, 화면은 키를 모름)
  return { ...(S.demo ? {} : { "X-CTREC-Staff-Key": S.key }), "X-CTREC-Staff-Id": S.sid, ...(extra || {}) };
}
async function api(path, body) {
  const opt = body === undefined ? { headers: headers() }
    : { method: "POST", headers: headers({ "Content-Type": "application/json", "X-CTREC-Request": "1" }), body: JSON.stringify(body) };
  let r;
  try { r = await fetch("/api/followup/staff" + path, opt); } catch (e) { throw new Error("서버에 연결하지 못했습니다."); }
  let d = null; try { d = await r.json(); } catch (e) { d = null; }
  if (!r.ok) {
    const det = d && d.detail; const msg = det && det.message ? det.message : typeof det === "string" ? det : `요청 실패 (${r.status})`;
    const err = new Error(msg); err.status = r.status; err.code = det && det.code; throw err;
  }
  return d;
}
// 검증자·임상의 역할 호출 (발표용 데모: 키 대신 X-CTREC-Demo-Role, 담당자 헤더는 붙이지 않음 - 서버는 한 요청에 한 역할만 받음)
async function roleApi(role, path, body) {
  const hd = { "X-CTREC-Demo-Role": role };
  const opt = body === undefined ? { headers: hd }
    : { method: "POST", headers: { ...hd, "Content-Type": "application/json", "X-CTREC-Request": "1" }, body: JSON.stringify(body) };
  let r;
  try { r = await fetch("/api/followup" + path, opt); } catch (e) { throw new Error("서버에 연결하지 못했습니다."); }
  let d = null; try { d = await r.json(); } catch (e) { d = null; }
  if (!r.ok) { const det = d && d.detail; const err = new Error(det && det.message ? det.message : `요청 실패 (${r.status})`);
    err.status = r.status; err.code = det && det.code; throw err; }
  return d;
}
function setConn(t, c) { const el = q$("#st-conn"); el.textContent = t; el.className = "badge " + (c || ""); }

// ------------------------------------------------------------ 역할 탭 (환자 / 연구 담당자)
const PATIENT_MAINS = ["#page-run", "#page-result", "#page-detail", "#page-summary"];
function setRole(role, push) {
  const staff = role === "staff";
  document.querySelectorAll("#roles button").forEach(b => { const on = b.dataset.role === role; b.classList.toggle("on", on);
    if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current"); });
  q$("#page-staff").hidden = !staff;
  ["#pages", "#run-state", "#elapsed", "#model"].forEach(s => { const el = q$(s); if (el) el.classList.toggle("role-hidden", staff); });
  document.querySelector(".app").classList.toggle("wide", staff);
  if (staff) PATIENT_MAINS.forEach(s => { q$(s).hidden = true; });
  else if (typeof showPage === "function") showPage((typeof state === "object" && state.page) || "run");
  if (push) history.replaceState(null, "", staff ? "/staff" : "/");
  if (staff && !S.key && !S.demo) q$("#st-key").focus();
  if (staff && (S.key || S.demo)) loadAll().catch(() => {});
  window.scrollTo({ top: 0 });
}

// ------------------------------------------------------------ 로그인
async function connect(key, sid) {
  S.key = key; S.sid = sid || "coordinator";
  try { await loadAll(); } catch (e) {
    S.key = ""; setConn(e.status === 503 ? "서버에 담당자 기능 없음" : "연결 실패", "bad");
    q$("#st-login-hint").textContent = e.message; return false;
  }
  try { sessionStorage.setItem(KEY, S.key); sessionStorage.setItem(SID, S.sid); } catch (e) { /* 저장소 없음 */ }
  q$("#st-work").hidden = false; q$("#st-logout").hidden = false; setConn("연결됨", "ok");
  q$("#st-login-hint").textContent = `기록용 이름 ${S.sid}로 연결했습니다. 이 탭을 닫으면 키가 지워집니다.`;
  clearInterval(S.timer); S.timer = setInterval(poll, POLL_MS);
  return true;
}
// 발표용 데모 서버(--demo-staff)면 키 입력 없이 바로 연결. 실제 서버는 mode "key" -> 지금처럼 키 입력
async function trySession() {
  let d = null;
  try { const r = await fetch("/api/followup/staff/session"); if (r.ok) d = await r.json(); } catch (e) { d = null; }
  if (!d || d.mode !== "demo") return false;
  S.demo = true; S.sid = d.staff_id || "demo-staff"; S.roles = d.demo_roles || [];
  q$("#st-login-form").hidden = true;
  q$("#st-login-hint").textContent = "발표용 데모 서버입니다. 이 컴퓨터에서 연 화면만 담당자 키 없이 연결됩니다 (합성 데이터). 실제 서버에서는 담당자 키가 필요합니다.";
  q$("#st-login-h").firstChild.textContent = "담당자 연결 · 발표용 데모 ";
  try { await loadAll(); } catch (e) { setConn("연결 실패", "bad"); q$("#st-login-hint").textContent = e.message; return true; }
  q$("#st-work").hidden = false; setConn("자동 연결됨", "ok");
  clearInterval(S.timer); S.timer = setInterval(poll, POLL_MS);
  return true;
}
function logout() {
  try { sessionStorage.removeItem(KEY); } catch (e) { /* 저장소 없음 */ }
  S.key = ""; S.caseId = null; S.data = null; clearInterval(S.timer);
  q$("#st-work").hidden = true; q$("#st-logout").hidden = true; q$("#st-key").value = ""; setConn("연결 안 됨", "");
}

// ------------------------------------------------------------ 불러오기
async function loadAll() {
  const [cases, queue, ins] = await Promise.all([api("/cases"), api("/queue"), api("/insight")]);
  const list = [...cases.cases].sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)) || String(a.case_id).localeCompare(String(b.case_id)));
  S.labels = Object.fromEntries(list.map((c, i) => [c.case_id, `환자 ${i + 1}`]));  // 들어온 순서 번호 (화면 표시용)
  S.questions = ins.questions;
  renderSummary(ins.summary, list);
  renderQuestions(ins.questions);
  renderCases(list);
  renderQueue(queue);
}
async function poll() {
  if (!(S.key || S.demo) || S.busy || document.visibilityState !== "visible" || q$("#page-staff").hidden) return;
  try { await loadAll(); if (S.caseId) await refreshCase(); } catch (e) { /* 다음 주기에 다시 */ }
}

// ------------------------------------------------------------ 코디네이터 작업 시간 요약
// 핵심 지표: 같은 정보원으로 묶어 요청하면 줄어드는 시간(따로 요청 대비), 추천 순서로 확인할 때 판정이 정해지는 속도
function curveSvg(curve, total) {
  const pts = (curve || []).filter(p => p.minutes != null); if (!pts.length || !total) return "";
  const W = 320, H = 96, P = 6, maxM = pts[pts.length - 1].minutes || 1;
  const x = m => P + (W - 2 * P) * m / maxM, y = n => H - P - (H - 2 * P) * n / total;
  let d = `M${x(0)},${y(0)}`, prev = 0;
  for (const p of pts) { d += ` L${x(p.minutes)},${y(prev)} L${x(p.minutes)},${y(p.decided_pairs)}`; prev = p.decided_pairs; }
  const first = pts.find(p => p.decided_pairs > 0);
  return `<svg class="curve" viewBox="0 0 ${W} ${H}" role="img" aria-label="추천 순서로 확인할 때 코디네이터 분에 따른 판정 가능 쌍 수">
    <line x1="${P}" y1="${H - P}" x2="${W - P}" y2="${H - P}" class="axis"/><path d="${d}" class="line"/>
    ${first ? `<circle cx="${x(first.minutes)}" cy="${y(first.decided_pairs)}" r="3.5" class="dot"/>` : ""}</svg>`;
}
function renderSummary(s, list) {
  if (!s || !s.cases) { q$("#st-summary").innerHTML = '<div class="empty">아직 사례가 없습니다. 환자 탭에서 "연구진 확인 요청"으로 시작하면 여기에 나타납니다.</div>'; return; }
  const open = list.reduce((n, c) => n + (c.open_requests || 0), 0);
  const pc = (k) => list.reduce((n, c) => n + ((c.pair_counts || {})[k] || 0), 0);
  const done = { ce: pc("CONFIRMED_ELIGIBLE"), ci: pc("CONFIRMED_INELIGIBLE"),
    all: list.reduce((n, c) => n + Object.values(c.pair_counts || {}).reduce((a, b) => a + b, 0), 0) };
  const ms = (s.milestones || []).filter(m => m.c_order_minutes != null);
  const last = ms[ms.length - 1];  // 환자 순서와의 비교는 화면에 두지 않음 (11-07 결정: 순서 우열은 WP6 시뮬레이터로 판단)
  const first = (s.c_curve || []).find(p => p.decided_pairs > 0);
  // 아직 보내지 않은 확인만 (보낸 요청은 이미 청구됨 - 남은 계획의 시간·절약에 섞지 않음)
  const rem = S.questions.flatMap(g => g.patients.filter(p => !p.request_id));
  const remMin = rem.reduce((n, p) => n + (p.minutes || 0), 0), remAlone = rem.reduce((n, p) => n + (p.minutes_alone || 0), 0);
  const saved = Math.max(0, remAlone - remMin);
  const pct = remAlone ? Math.round(100 * saved / remAlone) : 0;
  q$("#st-summary").innerHTML = `<div class="tiles">
      <div class="tile ok"><div class="k">판정 확정 (서류·판단 완료)</div><div class="v">적격 ${h(done.ce)}쌍</div>
        <div class="s">부적격 확정 ${h(done.ci)}쌍 · 전체 ${h(done.all)}쌍 중</div></div>
      <div class="tile"><div class="k">열린 사례</div><div class="v">${h(s.cases)}명</div>
        <div class="s">진행 중 요청 ${h(open)}건${s.spent_minutes ? ` · 보낸 요청 ${h(min(s.spent_minutes))}` : ""}</div></div>
      ${rem.length ? `<div class="tile info"><div class="k">아직 보내지 않은 확인 ${h(rem.length)}건, 묶어서 요청하면</div><div class="v">${h(min(remMin))}</div>
        <div class="s">${saved ? `하나씩 따로 요청하면 ${h(min(remAlone))} → <b>${h(min(saved))} (${h(pct)}%) 절약</b>` : "묶을 수 있는 확인 없음"}</div></div>`
      : `<div class="tile info"><div class="k">남은 확인</div><div class="v">0건</div><div class="s">보낸 요청의 서류 검증·판단을 기다리는 중</div></div>`}
      <div class="tile ok"><div class="k">판정이 정해질 수 있는 쌍</div><div class="v">${h(s.decidable_pairs)}개</div>
        <div class="s">${!first ? "지금 계획으로는 없음" : first.minutes > 0 ? `추천 순서로 ${h(min(first.minutes))}에 첫 판정${last ? ` · ${h(min(last.c_order_minutes))}이면 모두` : ""}`
          : "보낸 요청의 결과가 오면 정해질 수 있음"}</div></div></div>
    ${rem.length ? `<div class="curve-wrap">${curveSvg(s.c_curve, s.decidable_pairs)}
      <p class="hint m0">추천 순서(Cohort C)로 확인할 때 코디네이터 분(가로)에 따라 판정이 정해질 수 있는 쌍(세로). 같은 환자·같은 서류는 한 번만 청구하는
      비용 원장의 <b>가정 비용</b>이며 측정한 업무 시간이 아닙니다. '정해질 수 있는'은 답이 실제로 나올 때의 상한입니다.</p></div>`
      : '<p class="hint m0">분은 같은 환자·같은 서류를 한 번만 청구하는 비용 원장의 <b>가정 비용</b>이며 측정한 업무 시간이 아닙니다.</p>'}`;
}

// ------------------------------------------------------------ 질문별 (코디네이터가 물을 것을 고르는 화면)
// 같은 정보원(예: 검사 결과지)끼리 묶어 보여 줌: 한 번의 요청으로 여러 항목을 확인해 연락 비용을 한 번만 씀
function qcard(g, open, single) {
  const todo = g.patients.filter(p => !p.request_id && p.requestable);
  const key = `${g.route}|${g.info_key}`;
  const saved = Math.max(0, (g.minutes_alone || 0) - (g.minutes || 0));
  return `<div class="qcard" data-key="${h(key)}">
    <div class="qhead"><div><p class="qname">${h(itemName(g.info_key))} <span class="badge">${h(KIND[g.route] || g.route)}</span></p>
      ${g.preview ? `<p class="hint m0">환자에게: ${h(g.preview)}</p>` : ""}</div>
      <button class="btn ${single ? "" : "soft "}sm" type="button" data-qbatch="1"${todo.length ? "" : " disabled"}>${todo.length ? `${single ? "" : "이 항목만 "}${h(todo.length)}명에게 요청 보내기` : "보낼 요청 없음"}</button></div>
    <div class="qstats">
      <div><b>${h(g.n_patients)}명</b><span>적용 환자</span></div>
      <div><b>${h(g.n_related_pairs)}쌍</b><span>관련 시험 쌍</span></div>
      <div class="${g.n_newly_decided_in_order ? "good" : ""}"><b>${h(g.n_newly_decided_in_order)}쌍</b><span>추천 순서에서 이 확인으로 판정</span></div>
      <div><b>${h(g.n_next_pairs)}쌍</b><span>하나만 더 확인하면 판정</span></div>
      <div class="${saved ? "good" : ""}"><b>${h(min(g.minutes))}</b><span>${saved ? `예상 시간 · 따로 하면 ${h(min(g.minutes_alone))}` : "예상 코디네이터 시간"}</span></div>
    </div>
    ${g.n_record_after_pairs ? `<p class="hint m0">환자 답변 뒤에도 진료 기록 확인이 필요한 쌍 ${h(g.n_record_after_pairs)}개 (답변만으로는 확정되지 않음)</p>` : ""}
    <details${open.includes(key) ? " open" : ""}><summary class="hint">환자별 보기 (${h(g.patients.length)}명)</summary><table class="list mt8"><thead><tr><th>환자</th><th>추천 순위</th><th>관련 시험</th><th>예상</th><th>상태</th></tr></thead><tbody>
      ${g.patients.map(p => `<tr><td><button class="link" type="button" data-case="${h(p.case_id)}">${h(who(p.case_id))}</button></td><td>${h(p.rank)}</td>
        <td class="mono">${h(p.related_trials.join(", ") || "-")}${p.decided_trials.length ? ` <span class="badge ok">판정 가능 ${h(p.decided_trials.length)}</span>` : ""}</td>
        <td>${h(min(p.minutes))}${p.minutes_alone > p.minutes ? ` <span class="hint">(따로 ${h(min(p.minutes_alone))})</span>` : ""}</td>
        <td>${p.request_status ? badge(REQ, p.request_status) : p.requestable ? '<span class="hint">요청 전</span>' : '<span class="hint">보낼 수 없음</span>'}</td></tr>`).join("")}
    </tbody></table></details></div>`;
}
function renderQuestions(qs) {
  S.questions = qs;
  if (!qs.length) { q$("#st-qlist").innerHTML = '<div class="empty">지금 제안할 확인이 없습니다.</div>'; return; }
  const sort = q$("#st-qsort") ? q$("#st-qsort").value : "rank";
  const open = [...document.querySelectorAll("#st-qlist details[open]")].map(d => d.closest(".qcard").dataset.key);
  const rows = [...qs].sort(sort === "near" ? (a, b) => (b.n_newly_decided_in_order - a.n_newly_decided_in_order) || (b.n_next_pairs - a.n_next_pairs) || (a.minutes - b.minutes) || (a.best_rank - b.best_rank)
    : sort === "reach" ? (a, b) => (b.n_patients - a.n_patients) || (b.n_related_pairs - a.n_related_pairs) || (a.best_rank - b.best_rank)
    : (a, b) => a.best_rank - b.best_rank);
  const sent = rows.filter(g => g.patients.every(p => p.request_id));
  const groups = [];  // 정렬 순서를 지키며 같은 정보원끼리 (모두 보낸 확인은 아래 '보낸 확인'으로)
  for (const g of rows.filter(g => !sent.includes(g))) {
    const src = g.route === "document_review" ? g.source_id || g.info_key : `${g.route}|${g.info_key}`;
    let grp = groups.find(x => x.src === src); if (!grp) groups.push(grp = { src, items: [] }); grp.items.push(g);
  }
  q$("#st-qlist").innerHTML = `<div class="row"><label class="field">정렬
      <select id="st-qsort"><option value="rank"${sort === "rank" ? " selected" : ""}>추천 순서 (Cohort C)</option>
        <option value="near"${sort === "near" ? " selected" : ""}>판정에 가까운 순</option><option value="reach"${sort === "reach" ? " selected" : ""}>적용 환자 많은 순</option></select></label>
      <span class="err" id="st-qerr" role="alert"></span></div>
    ${groups.map(grp => {
      const many = grp.items.length > 1, g0 = grp.items[0];
      const cards = grp.items.map(g => qcard(g, open, !many)).join("");
      if (!many) return `<div class="qgroup single"><p class="qsrc">${h(SOURCE[g0.source_id] || KIND[g0.route] || "")}</p>${cards}</div>`;
      const m = grp.items.reduce((n, g) => n + (g.minutes || 0), 0), alone = grp.items.reduce((n, g) => n + (g.minutes_alone || 0), 0);
      const pats = new Set(grp.items.flatMap(g => g.patients.map(p => p.case_id)));
      const todo = grp.items.reduce((n, g) => n + g.patients.filter(p => !p.request_id && p.requestable).length, 0);
      const decided = grp.items.reduce((n, g) => n + (g.n_newly_decided_in_order || 0), 0);
      return `<div class="qgroup" data-src="${h(grp.src)}"><div class="qghead"><div>
          <p class="qname">${h(SOURCE[grp.src] || grp.src)} 한 장으로 ${h(grp.items.length)}개 항목 확인</p>
          <p class="hint m0">${grp.items.map(g => h(itemName(g.info_key))).join(" · ")} — 환자 ${h(pats.size)}명, 판정 ${h(decided)}쌍</p></div>
          <div class="qsave"><b>${h(min(m))}</b><span>묶어서 요청${alone > m ? ` · 따로 하면 ${h(min(alone))}, <b>${h(min(alone - m))} 절약</b>` : ""}</span></div>
          <button class="btn sm" type="button" data-gbatch="1"${todo ? "" : " disabled"}>${todo ? `묶어서 요청 보내기 (${h(todo)}건)` : "보낼 요청 없음"}</button></div>
        <div class="stack">${cards}</div></div>`;
    }).join("") || '<div class="empty">보낼 확인을 모두 보냈습니다. 환자별 보기에서 서류 검증·의료진 판단을 진행하세요.</div>'}
    ${sent.length ? `<details class="sent"><summary class="hint">요청을 보낸 확인 ${h(sent.length)}개 (결과 대기·완료)</summary><ul class="m0">
      ${sent.map(g => `<li>${h(itemName(g.info_key))} · ${h(g.n_patients)}명 · ${g.patients.map(p => `${h(who(p.case_id))} ${badge(REQ, p.request_status)}`).join(" ")}</li>`).join("")}</ul></details>` : ""}`;
}
async function sendProposals(list, label) {  // list: [{case_id, proposal_id}] - 사례마다 한 번에 보냄 (같은 연락으로 묶임)
  const byCase = {};
  for (const p of list) (byCase[p.case_id] = byCase[p.case_id] || []).push(p.proposal_id);
  S.busy = true; let ok = 0; const fails = [];
  for (const [cid, ids] of Object.entries(byCase)) {
    try { const out = await api(`/cases/${encodeURIComponent(cid)}/requests`, { proposal_ids: ids }); ok += out.created.length; }
    catch (e) { fails.push(`${who(cid)}: ${e.message}`); }
  }
  S.busy = false;
  await loadAll().catch(() => {}); if (S.caseId) await refreshCase();
  q$("#st-qerr").textContent = `${label} 요청 ${ok}건을 보냈습니다.` + (fails.length ? ` 실패 ${fails.length}건 - ${fails.join(" / ")}` : "");
}
function sendBatch(card) {
  const [route, key] = card.dataset.key.split("|");
  const g = S.questions.find(x => x.route === route && x.info_key === key); if (!g) return;
  card.querySelector("[data-qbatch]").disabled = true;
  return sendProposals(g.patients.filter(p => !p.request_id && p.requestable), itemName(key));
}
function sendGroup(box) {
  const src = box.dataset.src;
  const items = S.questions.filter(g => g.route === "document_review" && (g.source_id || g.info_key) === src);
  box.querySelector("[data-gbatch]").disabled = true;
  return sendProposals(items.flatMap(g => g.patients.filter(p => !p.request_id && p.requestable)), SOURCE[src] || src);
}

// ------------------------------------------------------------ 환자별 (사례 목록·상세)
function renderCases(list) {
  q$("#st-cases").innerHTML = [...list].reverse().map(c => `<button class="case-item${c.case_id === S.caseId ? " on" : ""}" type="button" data-case="${h(c.case_id)}">
      <span class="cid">${h(who(c.case_id))}</span> <span class="mono">${h(shortId(c.case_id))}</span> ${c.synthetic ? '<span class="badge demo">가상</span>' : ""}
      ${c.open_requests ? `<span class="badge warn">열린 요청 ${h(c.open_requests)}</span>` : ""}<br>
      <span class="hint">${when(c.created_at)} · 개정 ${h(c.revision)}</span><br>
      ${Object.entries(c.pair_counts || {}).map(([k, n]) => `${badge(PAIR, k)} ${h(n)}`).join(" ")}</button>`).join("")
    || '<div class="empty">아직 사례가 없습니다.</div>';
}
async function openCase(id, focus) {
  S.caseId = id; tab("c");
  document.querySelectorAll(".case-item").forEach(b => b.classList.toggle("on", b.dataset.case === id));
  try { S.data = await api(`/cases/${encodeURIComponent(id)}`); } catch (e) { q$("#st-case").innerHTML = `<div class="note bad">${h(e.message)}</div>`; return; }
  await loadRoles();
  renderCase();
  if (focus) q$("#st-case-h").focus();
}
async function refreshCase() {
  let d; try { d = await api(`/cases/${encodeURIComponent(S.caseId)}`); } catch (e) { return; }
  // 개정 번호가 같아도 서버의 지금 권한 기준 쌍 상태·제안·추천 화면이 바뀔 수 있음 (권한 철회 뒤 재시작): 그것도 비교
  const same = k => JSON.stringify(d[k]) === JSON.stringify(S.data[k]);
  if (!S.data || d.revision !== S.data.revision || !["requests", "pairs", "proposals", "view", "authority_changed"].every(same)) {
    S.data = d; await loadRoles(); renderCase(true); }
}
// 검증자·임상의 보기 (발표용 데모에서 그 역할이 켜져 있을 때만). 담당자 보기와 따로 불러 역할을 섞지 않음
async function loadRoles() {
  S.vdata = S.cdata = null;
  if (!S.caseId || !S.roles.length) return;
  const id = encodeURIComponent(S.caseId);
  const [v, c] = await Promise.all([
    S.roles.includes("verifier") ? roleApi("verifier", `/verifier/cases/${id}`).catch(() => null) : null,
    S.roles.includes("clinician") ? roleApi("clinician", `/clinician/cases/${id}`).catch(() => null) : null]);
  S.vdata = v; S.cdata = c;
}
function pairRows(d) {
  const rec = ((d.view || {}).result || {}).recommendation || {};
  const shown = {};
  for (const r of [...(rec.ranked || []), ...(rec.recommended || [])]) shown[r.trial_id] = r;
  const excluded = new Set((rec.excluded || []).map(x => x.trial_id));
  return Object.entries(d.pairs).map(([tid, p]) => {
    const r = shown[tid], unconfirmed = UNCONFIRMED[p.status];
    const recTxt = r ? `${(rec.recommended || []).some(x => x.trial_id === tid) ? `<span class="badge ok">추천${r.rank ? ` ${h(r.rank)}위` : ""}</span> ` : ""}${badge(MATCH, r.match_status)}`
      : excluded.has(tid) ? '<span class="badge muted">추천 제외</span>' : '<span class="hint">-</span>';
    return `<tr><td class="mono">${h(tid)}</td><td>${badge(PAIR, p.status)}${unconfirmed ? `<br><span class="hint">${h(unconfirmed)}</span>` : ""}</td>
      <td>${(p.pending_kinds || []).map(k => `<span class="badge">${h(GAP[k] || k)}</span>`).join(" ") || '<span class="hint">없음</span>'}</td><td>${recTxt}</td></tr>`;
  }).join("");
}
const shownProposals = (d) => d.proposals.filter(p => ACTION[p.action_type] || p.request_id);  // 요청 경로 없는 내부 행동(RETRIEVE 등)은 숨김
function proposalRows(d) {
  return shownProposals(d).map(p => {
    const can = p.requestable && !p.request_id;
    return `<tr><td>${can ? `<input type="checkbox" name="prop" value="${h(p.proposal_id)}" aria-label="요청 선택 ${h(p.rank)}">` : ""}</td>
      <td>${h(p.rank)}</td><td>${h(ACTION[p.action_type] || p.action_type)}${p.safety_required ? ' <span class="badge bad">안전 필수</span>' : ""}</td>
      <td>${h(itemName(p.info_key || ""))}${p.source_id && SOURCE[p.source_id] ? ` <span class="hint">· ${h(SOURCE[p.source_id])}</span>` : ""}</td>
      <td class="mono">${h((p.target_trials || []).join(", "))}</td>
      <td>${p.patient_question_preview ? `<span class="hint">${h(p.patient_question_preview)}</span>` : ""}</td>
      <td>${p.request_id ? badge(REQ, p.request_status) : p.requestable ? '<span class="hint">요청 전</span>' : '<span class="hint">이 화면에서 보낼 수 없음</span>'}</td></tr>`;
  }).join("");
}
function requestBox(r, responses) {
  const moves = ((MOVES[r.kind] || {})[r.status]) || [];
  const resp = responses.find(x => x.response_id === r.response_id);
  return `<div class="req" data-rid="${h(r.request_id)}"><div class="row">${badge(REQ, r.status)} <b>${h(KIND[r.kind] || r.kind)}</b>
      <span>${h(itemName(r.info_key || ""))}</span> <span class="hint">${h(r.created_by || "")} · ${when(r.created_at)}</span>
      ${r.follows ? `<span class="hint">· 이전 요청 ${h(shortId(r.follows))}의 대체 경로</span>` : ""}</div>
    ${r.question ? `<p class="q">${h(r.question.question)}</p>` : ""}
    ${resp ? `<p class="m0">${resp.outcome === "unknown" ? '<span class="badge warn">환자 "모름"</span>' : `환자 답변: <b>${h(resp.answer)}</b>`}
      ${(resp.conflicts || []).length ? ' <span class="badge bad">기존 사실과 상충 · 보류</span>' : ""} <span class="hint">개정 ${h(resp.revision_after)}</span></p>` : ""}
    ${workArea(r) || (NOT_SUPPORTED[r.status] ? `<p class="hint m0">${h(NOT_SUPPORTED[r.status])}</p>` : "")}
    ${moves.length ? `<div class="row mt8">${moves.map(m => `<button class="btn ghost sm" type="button" data-move="${h(m)}">${h(MOVE_KO[m] || m)}</button>`).join("")}</div>` : ""}
    <details><summary class="hint">이력 ${h((r.history || []).length)}건</summary><ol>${(r.history || []).map(x =>
      `<li>${when(x.at)} · ${h(x.from_status || "생성")} → ${h(x.to_status)} · ${h(x.by)} · <span class="mono">${h(x.reason || "")}</span></li>`).join("")}</ol></details></div>`;
}

// ------------------------------------------------------------ 서류 등록 -> 서류 검증(검증자) -> 의료진 판단(임상의) (#39, 사례 화면 안에서)
// 역할 표시를 분명히: 서류 등록은 담당자, 검증 완료는 검증자, 판단은 임상의. 판정은 서버가 다시 계산(화면은 결과만 보여 줌)
const unitOf = (text) => { const m = String(text || "").match(/[<>=≥≤]+\s*[\d.]+\s*([^\s,;)]+)/); return m && !/^(years?|세)$/i.test(m[1]) ? m[1] : ""; };
const daysAgo = (n) => { const d = new Date(Date.now() - n * 864e5); return d.toISOString().slice(0, 10); };
function docTemplate(vr) {
  const lines = (vr.criteria || []).map(c => `- ${c.criterion} ${c.text}`).join("\n");
  return `[합성 서류 · 발표용] ${SOURCE[vr.source_id] || "진료 기록"}\n확인 항목: ${(vr.allowed_fields || []).map(itemName).join(", ")}\n${lines}\n결과: `;
}
// 기록할 항목: 서버가 받는 사실 항목. 바이오마커는 사실 항목이 'biomarker' 하나(값에 변이 이름)라 'biomarker:EGFR' 같은 정보 키는 뺌
function recordFields(vr) {
  const f = vr.allowed_fields || [];
  return f.includes("biomarker") ? f.filter(x => !x.startsWith("biomarker:")) : f;
}
function workArea(r) {
  if (r.kind === "document_review" && r.status === "requires_document" && S.vdata) {
    const vr = (S.vdata.requests || []).find(x => x.request_id === r.request_id);
    if (!vr || !vr.verifiable) return "";
    const docs = S.vdata.documents || [];
    const facts = (vr.facts || []).filter(f => !f.document_verified);
    return `<div class="work" data-work="doc" data-rid="${h(r.request_id)}">
      <p class="role-tag">① 연구 담당자 · 서류 등록 <span class="hint">(${h(SOURCE[vr.source_id] || "진료 기록")})</span></p>
      ${docs.length ? `<label class="field">등록된 서류 <select data-f="doc">${docs.map((d, i) => `<option value="${h(d.document_id)}"${i === docs.length - 1 ? " selected" : ""}>${h(d.document_id)} · ${h(d.kind)} · ${when(d.registered_at)}</option>`).join("")}</select></label>` : ""}
      <details${docs.length ? "" : " open"}><summary class="hint">${docs.length ? "새 서류 등록" : "서류 내용 (합성)"}</summary>
        <textarea data-f="text" rows="4">${h(docTemplate(vr))}</textarea>
        <button class="btn ghost sm" type="button" data-act="register">서류 등록</button></details>
      <p class="role-tag verifier">② 서류 검증자 역할 <span class="badge demo">발표용 데모 · 합성 권한</span></p>
      <ul class="crit">${(vr.criteria || []).map(c => `<li><span class="mono">${h(c.criterion)}</span> ${h(c.text)}</li>`).join("")}</ul>
      ${facts.map(f => `<label class="inline-check"><input type="checkbox" data-fact="${h(f.fact_id)}" checked> 환자 답변 값 <b>${h(f.value)}${f.unit ? " " + h(f.unit) : ""}</b> (${h(itemName(f.field))})이 서류와 같음</label>`).join("")}
      ${recordFields(vr).filter(fd => !facts.some(f => f.field === fd)).map(fd => `<div class="row vrow" data-field="${h(fd)}">
        <span>${h(itemName(fd.includes(":") ? fd : vr.info_key))}</span><input data-f="value" placeholder="서류의 값" size="10">
        <input data-f="unit" value="${h(unitOf((vr.criteria || [])[0] && vr.criteria[0].text))}" placeholder="단위" size="8">
        <input data-f="date" type="date" value="${h(daysAgo(3))}" aria-label="검사·기록 날짜"></div>`).join("")}
      <div class="row"><button class="btn sm" type="button" data-act="verify"${docs.length ? "" : " disabled"}>검증 완료 · 재평가</button>
        <span class="err" data-f="msg" role="alert"></span></div></div>`;
  }
  if (r.kind === "clinician_review" && r.status === "requires_clinician" && S.cdata) {
    const cr = (S.cdata.requests || []).find(x => x.request_id === r.request_id);
    if (!cr) return "";
    if (!cr.judgeable) return '<p class="hint m0">검사 수치처럼 의료진 판단 기준이 아닌 항목이라 판단으로 닫을 수 없습니다 (취소하거나 열어 둠).</p>';
    const basis = (S.cdata.facts || []).filter(f => f.document_verified);
    return `<div class="work" data-work="judge" data-rid="${h(r.request_id)}">
      <p class="role-tag clinician">임상의 역할 · 의료진 판단 <span class="badge demo">발표용 데모 · 합성 권한</span></p>
      ${(cr.criteria || []).map(c => `<div class="crit-judge" data-crit="${h(c.criterion)}"><p class="m0"><span class="mono">${h(c.criterion)}</span> ${h(c.text)}</p>
        <label class="inline-check"><input type="radio" name="d-${h(r.request_id)}-${h(c.criterion)}" value="MET" checked> 충족</label>
        <label class="inline-check"><input type="radio" name="d-${h(r.request_id)}-${h(c.criterion)}" value="NOT_MET"> 불충족</label></div>`).join("")}
      <p class="hint m0">판단 근거 (서류로 확인된 사실만)</p>
      ${basis.length ? basis.map(f => `<label class="inline-check"><input type="checkbox" data-basis="${h(f.fact_id)}" checked> ${h(itemName(f.field))} ${h(f.value)}${f.unit ? " " + h(f.unit) : ""}</label>`).join("")
        : '<p class="hint m0">아직 서류로 확인된 사실이 없습니다. 진료 기록이 필요한 기준은 먼저 서류 확인(예: 전신 상태 ECOG)을 마쳐야 판단할 수 있습니다.</p>'}
      <div class="row"><button class="btn sm" type="button" data-act="judge">판단 기록 · 재평가</button><span class="err" data-f="msg" role="alert"></span></div></div>`;
  }
  return "";
}
const b64 = (t) => btoa(String.fromCharCode(...new TextEncoder().encode(t)));
const transText = (tr) => (tr || []).map(x => `${x.trial_id}: ${(PAIR[x.before] || ["", x.before])[1]} → ${(PAIR[x.after] || ["", x.after])[1]}`).join(", ");
async function afterWork(box, msg) {
  await openCase(S.caseId); await loadAll().catch(() => {});
  const el = document.querySelector(`#st-case .req[data-rid="${CSS.escape(box.dataset.rid)}"]`) || q$("#st-move-err");
  const note = document.createElement("div"); note.className = "note ok mt8"; note.setAttribute("role", "status"); note.textContent = msg;
  (el.closest(".req") || el).appendChild(note);
}
async function doWork(btn) {
  const box = btn.closest(".work"), msg = box.querySelector('[data-f="msg"]'), act = btn.dataset.act;
  const cid = encodeURIComponent(S.caseId), rid = encodeURIComponent(box.dataset.rid);
  btn.disabled = true; S.busy = true; if (msg) msg.textContent = "";
  try {
    if (act === "register") {
      const vr = (S.vdata.requests || []).find(x => x.request_id === box.dataset.rid) || {};
      await api(`/cases/${cid}/documents`, { kind: DOC_KIND[vr.source_id] || "clinical_note", media_type: "text/plain",
        content_base64: b64(box.querySelector('[data-f="text"]').value), expected_revision: S.data.revision });
      await openCase(S.caseId); return;
    }
    if (act === "verify") {
      const docId = box.querySelector('[data-f="doc"]').value, doc = S.vdata.documents.find(d => d.document_id === docId);
      const items = [...box.querySelectorAll("[data-fact]:checked")].map(i => ({ fact_id: i.dataset.fact }));
      for (const row of box.querySelectorAll(".vrow")) {
        const raw = row.querySelector('[data-f="value"]').value.trim(); if (!raw) continue;
        const num = Number(raw), it = { field: row.dataset.field, value: raw !== "" && !isNaN(num) ? num : raw };
        const unit = row.querySelector('[data-f="unit"]').value.trim(), date = row.querySelector('[data-f="date"]').value;
        if (unit) it.unit = unit; if (date) it.observed_at = date; items.push(it);
      }
      if (!items.length) throw new Error("서류의 값을 입력하거나 환자 답변 값을 확인하세요.");
      const out = await roleApi("verifier", `/verifier/cases/${cid}/requests/${rid}/verification`,
        { document_id: doc.document_id, document_sha256: doc.sha256, items, expected_revision: S.vdata.revision });
      await afterWork(box, `서류 검증 완료. ${transText(out.transitions) || "쌍 상태 변화 없음 (다른 확인이 남아 있음)"}`); return;
    }
    if (act === "judge") {
      const basis = [...box.querySelectorAll("[data-basis]:checked")].map(i => i.dataset.basis);
      const decisions = [...box.querySelectorAll("[data-crit]")].map(c => {
        const d = c.querySelector("input[type=radio]:checked").value;
        return { criterion: c.dataset.crit, decision: d, rationale_code: d === "MET" ? "investigator_judgment_met" : "investigator_judgment_not_met", basis_fact_ids: basis };
      });
      const out = await roleApi("clinician", `/clinician/cases/${cid}/requests/${rid}/judgment`, { decisions, expected_revision: S.cdata.revision });
      await afterWork(box, `의료진 판단 기록. ${transText(out.transitions) || "쌍 상태 변화 없음 (다른 확인이 남아 있음)"}`); return;
    }
  } catch (e) {
    if (msg) msg.textContent = e.code === "stale_revision" ? "그 사이 사례가 바뀌었습니다. 다시 시도하세요." : e.message;
    if (e.code === "stale_revision") await openCase(S.caseId);
  } finally { S.busy = false; btn.disabled = false; }
}

function evalRows(d) {
  const TRIG = { intake: "환자 입력", response: "환자 응답", staff_status: "요청 상태 변경", document_verification: "서류 검증",
    clinician_judgment: "의료진 판단" };
  return [...d.evaluations].reverse().map(e => {
    const t = e.trigger || {};
    const tr = (e.transitions || []).map(x => `<span class="mono">${h(x.trial_id || "")}</span> ${badge(PAIR, x.before)} → ${badge(PAIR, x.after)}`).join("<br>");
    return `<tr><td>${h(e.revision)}</td><td>${when(e.at)}</td><td>${h(TRIG[t.type] || t.type || "")}${t.outcome ? ` · ${h(t.outcome)}` : ""}</td>
      <td>${tr || '<span class="hint">쌍 상태 변화 없음</span>'}${(e.conflicts || []).length ? `<br><span class="badge bad">상충 ${h(e.conflicts.length)}건</span>` : ""}</td>
      <td class="hint">재평가 ${h((e.reevaluated_trials || []).length)} · 재사용 ${h((e.reused_trials || []).length)}${e.equivalence && e.equivalence.status ? ` · 동등성 ${h(e.equivalence.status)}` : ""}</td></tr>`;
  }).join("");
}
function renderCase(keepOpen) {
  const d = S.data; if (!d) return;
  const open = keepOpen ? [...document.querySelectorAll("#st-case details[open]")].map(x => x.dataset.k) : [];
  q$("#st-case-h").textContent = `${who(d.case_id)} · 사례 ${shortId(d.case_id)}`;
  const reqs = [...d.requests].reverse();
  const requestable = d.proposals.filter(p => p.requestable && !p.request_id).length;
  const mine = S.questions.flatMap(g => g.patients.filter(p => p.case_id === d.case_id && !p.request_id));
  const mm = mine.reduce((n, p) => n + (p.minutes || 0), 0), ma = mine.reduce((n, p) => n + (p.minutes_alone || 0), 0);
  const md = mine.reduce((n, p) => n + (p.newly_decided_in_order || 0), 0);
  q$("#st-case").innerHTML = `<div class="row">${d.synthetic ? '<span class="badge demo">가상 데이터</span>' : ""}<span class="hint">개정 ${h(d.revision)} · 생성 ${when(d.created_at)}</span>
      ${Object.entries(d.pair_counts || {}).map(([k, n]) => `${badge(PAIR, k)} ${h(n)}`).join(" ")}</div>
    ${mine.length ? `<div class="note info m0">이 환자에게 남은 확인 ${h(mine.length)}건 · 추천 계획대로 묶어서 하면 <b>${h(min(mm))}</b>${ma > mm ? ` (하나씩 따로 하면 ${h(min(ma))})` : ""}
      · 판정이 정해질 수 있는 쌍 ${h(md)}개</div>` : ""}
    <div class="staff-sec"><h3>시험별 판정과 추천</h3><table class="list"><thead><tr><th>시험</th><th>쌍 상태</th><th>남은 확인</th><th>추천 화면</th></tr></thead>
      <tbody>${pairRows(d)}</tbody></table></div>
    <div class="staff-sec"><h3>확인 제안 <span class="hint">(정책 엔진 후보 · 위에서부터 먼저)</span></h3>
      ${shownProposals(d).length ? `<table class="list"><thead><tr><th></th><th>#</th><th>확인</th><th>항목</th><th>대상 시험</th><th>환자 질문 미리보기</th><th>상태</th></tr></thead>
        <tbody>${proposalRows(d)}</tbody></table>
        <div class="row mt8"><button class="btn sm" id="st-send" type="button"${requestable ? "" : " disabled"}>선택한 확인 요청 보내기</button>
          <span class="err" id="st-send-err" role="alert"></span></div>` : '<div class="empty">지금 제안할 확인이 없습니다.</div>'}
      ${(d.held || []).filter(x => x.action_type === "DEFER").length ? `<p class="hint">보류된 항목 ${h(d.held.filter(x => x.action_type === "DEFER").length)}건 (확보 수단 소진 등, 같은 질문을 다시 하지 않음)</p>` : ""}</div>
    <div class="staff-sec"><h3>요청 이력 ${h(reqs.length)}건</h3><span class="err" id="st-move-err" role="alert"></span>
      ${reqs.length ? reqs.map(r => requestBox(r, d.responses)).join("") : '<div class="empty">아직 보낸 요청이 없습니다.</div>'}</div>
    <div class="staff-sec"><h3>재평가 기록</h3><table class="list"><thead><tr><th>개정</th><th>시각</th><th>계기</th><th>변화</th><th>범위</th></tr></thead><tbody>${evalRows(d)}</tbody></table></div>
    <div class="staff-sec"><h3>Inspector</h3><button class="btn ghost sm" id="st-insp" type="button">판정 기록 열기</button><div id="st-insp-body"></div></div>`;
  document.querySelectorAll("#st-case details").forEach((x, i) => { x.dataset.k = String(i); if (open.includes(String(i))) x.open = true; });
}
async function sendRequests() {
  const ids = [...document.querySelectorAll('#st-case input[name="prop"]:checked')].map(i => i.value);
  if (!ids.length) { q$("#st-send-err").textContent = "보낼 확인을 하나 이상 고르세요."; return; }
  S.busy = true; q$("#st-send").disabled = true; q$("#st-send-err").textContent = "";
  try {
    const out = await api(`/cases/${encodeURIComponent(S.caseId)}/requests`, { proposal_ids: ids, expected_revision: S.data.revision });
    S.data = out.case; renderCase(true); await loadAll().catch(() => {});
    const n = out.created.length, dup = out.existing.length;
    q$("#st-send-err").textContent = `요청 ${n}건을 보냈습니다${dup ? ` (이미 보낸 요청 ${dup}건은 다시 보내지 않음)` : ""}.`;
  } catch (e) {
    q$("#st-send-err").textContent = e.code === "stale_revision" ? "그 사이 사례가 바뀌었습니다. 최신 내용으로 다시 고르세요." : e.message;
    if (e.status === 409) await openCase(S.caseId);
  } finally { S.busy = false; }
}
async function move(rid, status) {
  S.busy = true;
  try {
    const out = await api(`/cases/${encodeURIComponent(S.caseId)}/requests/${encodeURIComponent(rid)}/status`, { status });
    S.data = out.case; renderCase(true); await loadAll().catch(() => {});
    if (out.routed && out.routed.length) q$("#st-move-err").textContent = `대체 경로 요청 ${out.routed.length}건이 만들어졌습니다.`;
  } catch (e) { q$("#st-move-err").textContent = e.message; } finally { S.busy = false; }
}
async function inspector() {
  const box = q$("#st-insp-body"); box.innerHTML = '<p class="hint">불러오는 중…</p>';
  let v; try { v = await api(`/cases/${encodeURIComponent(S.caseId)}/inspector`); } catch (e) { box.innerHTML = `<div class="note bad">${h(e.message)}</div>`; return; }
  const o = v.overview || {}, scope = v.record_scope || { current: [], history: [] };
  const part = keys => Object.fromEntries(Object.entries(v).filter(([k]) => keys.includes(k)));
  const rest = Object.fromEntries(Object.entries(v).filter(([k]) => !scope.history.includes(k)));
  // 과거 평가 기록: 저장된 라운드별 기록을 고쳐 쓰지 않고 그대로 보이되, 현재 판정과 나누고 안내를 붙임
  const past = (v.steps || []).filter(s => s.trial_id && s.eligibility);
  box.innerHTML = `<section class="insp-current" data-scope="current"><h4>현재 판정 <span class="hint">(지금 권한·근거 기준)</span></h4>
    ${v.authority_changed ? '<div class="note warn">권한이 바뀌어 저장된 확정을 지금 권한 기준으로 다시 보류했습니다.</div>' : ""}
    <table class="list mt8"><tbody>
      <tr><th>라운드</th><td>${h(o.n_rounds)}</td></tr><tr><th>질문</th><td>${h(o.n_questions)}</td></tr>
      <tr><th>판정 변화</th><td>${h(JSON.stringify(o.n_changes || {}))}</td></tr>
      <tr><th>최종 판정</th><td>${(o.final || []).map(f => `<span class="mono">${h(f.trial_id)}</span> ${badge(MATCH, f.match_status)}`).join("<br>")}</td></tr></tbody></table>
    <details><summary class="hint">현재 판정 원본 JSON</summary><pre class="raw">${h(JSON.stringify(rest, null, 1).slice(0, 200000))}</pre></details></section>
    <section class="insp-history mt8" data-scope="history"><h4>과거 평가 기록</h4>
    <div class="note" role="note">${h(scope.history_notice || "평가 당시 기록 — 현재 판정과 다를 수 있음")}</div>
    ${past.length ? `<table class="list mt8"><thead><tr><th>라운드</th><th>단계</th><th>시험</th><th>평가 당시 판정</th></tr></thead><tbody>
      ${past.map(s => `<tr><td>${h(s.round ?? "-")}</td><td>${h(s.agent || "")} ${h(s.event || "")}</td><td class="mono">${h(s.trial_id)}</td><td>${h(s.eligibility)}</td></tr>`).join("")}</tbody></table>`
      : '<p class="hint">시험별 평가 기록 없음</p>'}
    <details><summary class="hint">과거 평가 기록 원본 JSON</summary><pre class="raw">${h(JSON.stringify(part(scope.history), null, 1).slice(0, 200000))}</pre></details></section>`;
}

// ------------------------------------------------------------ 확인 대기열 (Cohort C)
function renderQueue(queue) {
  const all = queue.items || [], items = all.filter(x => x.route || x.request_status);  // 요청 경로 없는 내부 행동은 숨김
  q$("#st-queue").innerHTML = items.length ? `<p class="hint m0">열린 사례 ${h(queue.cases)}명을 한 코호트로 본 확인 순서 (Cohort C, 안전 필수 먼저). 같은 예산이면 위에서부터 확인합니다.</p>
    <span class="err" id="st-queue-err" role="alert"></span><table class="list"><thead><tr><th>#</th><th>환자</th><th>확인</th><th>상태</th></tr></thead><tbody>
    ${items.map(x => `<tr><td>${h(x.rank)}</td><td><button class="link" type="button" data-case="${h(x.case_id)}">${h(who(x.case_id))}</button></td>
      <td>${h(ACTION[x.action_type] || x.action_type)} · ${h(itemName(x.info_key || ""))}${x.safety_required ? ' <span class="badge bad">안전 필수</span>' : ""}</td>
      <td>${x.request_status ? badge(REQ, x.request_status) : x.route ? `<button class="btn soft sm" type="button" data-qsend="${h(x.proposal_id)}" data-qcase="${h(x.case_id)}">요청 보내기</button>`
        : '<span class="hint">요청 경로 없음</span>'}</td></tr>`).join("")}</tbody></table>`
    : '<div class="empty">대기열이 비어 있습니다.</div>';
}
async function queueSend(btn) {
  btn.disabled = true; S.busy = true;
  try {
    const out = await api(`/cases/${encodeURIComponent(btn.dataset.qcase)}/requests`, { proposal_ids: [btn.dataset.qsend] });
    await loadAll(); if (S.caseId === btn.dataset.qcase) await refreshCase();
    q$("#st-queue-err").textContent = out.created.length ? "요청을 보냈습니다." : "이미 보낸 요청입니다.";  // 목록을 다시 그린 뒤 (안내 칸도 새로 만들어짐)
  } catch (e) { const el = q$("#st-queue-err"); if (el) el.textContent = e.message; btn.disabled = false; } finally { S.busy = false; }
}

// ------------------------------------------------------------ 시작
function tab(which) {
  S.view = which;
  [["q", "#st-tab-q", "#st-questions"], ["c", "#st-tab-c", "#st-cases-view"], ["u", "#st-tab-u", "#st-queue-view"]].forEach(([k, t, p]) => {
    q$(t).setAttribute("aria-selected", String(k === which)); q$(p).hidden = k !== which; });
}
async function init() {
  document.querySelectorAll("#roles button").forEach(b => b.addEventListener("click", () => setRole(b.dataset.role, true)));
  q$("#st-login-form").addEventListener("submit", (e) => { e.preventDefault(); connect(q$("#st-key").value, q$("#st-id").value.trim()); });
  q$("#st-logout").addEventListener("click", logout);
  q$("#st-reload").addEventListener("click", async () => { try { await loadAll(); if (S.caseId) await refreshCase(); } catch (e) { setConn(e.message, "bad"); } });
  q$("#st-tab-q").addEventListener("click", () => tab("q")); q$("#st-tab-c").addEventListener("click", () => tab("c")); q$("#st-tab-u").addEventListener("click", () => tab("u"));
  q$("#page-staff").addEventListener("change", (e) => { if (e.target.id === "st-qsort") renderQuestions(S.questions); });
  q$("#page-staff").addEventListener("click", (e) => {
    const qs = e.target.closest("[data-qsend]"); if (qs) { queueSend(qs); return; }
    const qb = e.target.closest("[data-qbatch]"); if (qb) { sendBatch(qb.closest(".qcard")); return; }
    const gb = e.target.closest("[data-gbatch]"); if (gb) { sendGroup(gb.closest(".qgroup")); return; }
    const c = e.target.closest("[data-case]"); if (c) { openCase(c.dataset.case, true); return; }
    if (e.target.closest("#st-send")) { sendRequests(); return; }
    if (e.target.closest("#st-insp")) { inspector(); return; }
    const w = e.target.closest("[data-act]"); if (w) { doWork(w); return; }
    const m = e.target.closest("[data-move]");
    if (m) move(m.closest(".req").dataset.rid, m.dataset.move);
  });
  let key = "", sid = "";
  try { key = sessionStorage.getItem(KEY) || ""; sid = sessionStorage.getItem(SID) || ""; } catch (e) { /* 저장소 없음 */ }
  if (sid) q$("#st-id").value = sid;
  if (location.pathname === "/staff" || location.hash === "#staff") setRole("staff", false);
  if (await trySession()) return;
  if (key) connect(key, sid);
}
document.addEventListener("DOMContentLoaded", init);
})();
