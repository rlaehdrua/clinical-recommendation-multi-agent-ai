// 연구 담당자 탭 (index.html의 '연구 담당자', /staff로 바로 열림). API: /api/followup/staff/* (docs/dev/followup_api_contract.md)
// 핵심: 연구진이 시험 추천에 쓰는 시간을 줄이는 확인 선택 - 같은 서류로 한 번에 확인하고, 결과가 빨리 나오는 확인부터.
// 화면 원칙 (11-08 동겸): 처음 보는 사람도 따라 할 수 있게 쉬운 말, 지금 할 일 하나를 크게, 버튼 이름은 누르면 일어나는 일.
// 판정 규칙은 서버에만 있음. 담당자는 환자 답변을 대신 입력할 수 없음. 서류 확인 완료·의사 판단(#39)은 서류 확인 담당·의사 권한으로만:
// 발표용 데모 서버(--demo-staff + 합성 검증 모드)에서는 같은 환자 화면 안에서 그 역할로 입력(X-CTREC-Demo-Role, 서버가 루프백만 허용).
// app.js와 같은 페이지에서 돌므로 전역 이름을 만들지 않음 (즉시 실행 함수).
(() => {
"use strict";

const q$ = (s) => document.querySelector(s);
const h = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const KEY = "ctrec-staff-key", SID = "ctrec-staff-id";
const POLL_MS = 10000;
const S = { key: "", sid: "coordinator", demo: false, roles: [], caseId: null, data: null, vdata: null, cdata: null, timer: null, busy: false,
  pick: new Set(), pickInit: false, est: { groups: {}, picked: null }, cards: [], openCards: new Set(), budget: null,
  view: "q", labels: {}, questions: [], cases: [], titles: {} };

// 시험 하나의 결과 (engine.contracts.PairStatus 7가지) - 쉬운 말
const PAIR = {
  CONFIRMED_ELIGIBLE: ["ok", "참여 가능 (확정)"], CONFIRMED_INELIGIBLE: ["bad", "참여 불가 (확정)"], VERIFICATION_PENDING: ["warn", "서류 확인 필요"],
  INFO_MISSING: ["info", "정보가 더 필요"], EXPERT_REVIEW: ["violet", "의사 판단 필요"], BLOCKED_FAILURE: ["muted", "추가 확인 경로 없음 · 보류"],
  NOT_EVALUATED: ["", "판단 보류"],
};
// 확정이 아닌 결과는 '미확정'을 함께 보여 줌 (서류·의사 확인 전에는 확정처럼 보이지 않게)
const UNCONFIRMED = { VERIFICATION_PENDING: "미확정 · 서류 확인 전", EXPERT_REVIEW: "미확정 · 의사 판단 전",
  INFO_MISSING: "미확정 · 정보가 더 필요", BLOCKED_FAILURE: "미확정 · 부적격 아님", NOT_EVALUATED: "미확정 · 판단 근거 부족" };
const GAP = { missing: "정보 없음", stale: "검사가 오래됨", unverified: "서류 확인 필요", conflict: "정보가 서로 다름", physician_review: "의사 판단 필요",
  exclusion_unverified: "안 맞는 이유를 서류로 확인", sources_exhausted: "추가 확인 경로 없음" };
// 환자 화면에 보이는 표시 (app.js STATUS와 같은 뜻)
const MATCH = { CRITERIA_MATCH: ["ok", "조건 맞음"], POTENTIAL_MATCH: ["warn", "서류 확인하면 가능"], NEEDS_REVIEW: ["warn", "의사 확인 필요"],
  INSUFFICIENT_INFORMATION: ["warn", "정보가 더 필요"], CRITERIA_MISMATCH: ["bad", "조건 안 맞음"], NOT_EVALUATED: ["warn", "아직 판단 안 됨"] };
const KIND = { ask_patient: "환자에게 질문", document_review: "서류 확인", clinician_review: "의사 판단", conflict_resolution: "다른 정보 확인" };
const ACTION = { ASK_PATIENT: "환자에게 질문", REQUEST_DOCUMENT: "서류 요청", VERIFY_RECORD: "기록 확인" };  // 의사 판단은 서버가 자동으로 요청을 만듦
const REQ = {
  pending: ["accent", "환자 답 기다림"], answered: ["ok", "환자가 답함"], unknown: ["warn", "환자가 '모름'"], unreachable: ["warn", "연락 안 됨"],
  requires_document: ["warn", "서류 기다림"], requires_clinician: ["violet", "의사 판단 기다림"], document_unavailable: ["muted", "서류 없음"],
  superseded: ["muted", "다른 요청으로 바뀜"], cancelled: ["muted", "취소함"],
  document_verified: ["ok", "서류 확인 끝"], clinician_decided: ["ok", "의사 판단 끝"],
};
// 남은 정보원이 없어 시스템이 만든 임상 검토 (서버 followup.service.NO_PATH_REASONS와 같음): 의사 판단으로 끝낼 수 없고 쌍은 보류
const NO_PATH = new Set(["item_sources_exhausted", "conflict_unresolved"]);
const held = (r) => r.kind === "clinician_review" && r.status === "requires_clinician" && (NO_PATH.has(r.reason_code)
  || !!((S.cdata && (S.cdata.requests || []).find(x => x.request_id === r.request_id && x.judgeable === false))));
const DONE = new Set(["answered", "unknown", "unreachable", "document_unavailable", "superseded", "cancelled", "document_verified", "clinician_decided"]);
// 담당자가 바꿀 수 있는 상태 (followup.models.STAFF_TRANSITIONS와 같음). 완료(답변·서류 확인·판단)는 없음
const MOVES = { ask_patient: { pending: ["unreachable", "cancelled"] }, document_review: { requires_document: ["document_unavailable", "cancelled"] },
  conflict_resolution: { requires_document: ["document_unavailable", "cancelled"] }, clinician_review: { requires_clinician: ["cancelled"] } };
const MOVE_KO = { unreachable: "연락이 안 돼요", document_unavailable: "서류를 구할 수 없어요", cancelled: "요청 취소" };
// 운영 서버(실제 의료기관 인증 없음)에서는 완료 입력이 막혀 있음 -> 요청은 열린 채 남고 결과는 보류
const NOT_SUPPORTED = { requires_document: "서류 확인 끝내기는 '서류 확인 담당' 권한이 있어야 해요. 이 서버에는 그 권한이 없어 결과는 보류로 남아요.",
  requires_clinician: "의사 판단은 의사 권한이 있어야 해요. 이 서버에는 그 권한이 없어 결과는 보류로 남아요." };
// 항목 이름 (info_key -> 쉬운 이름). 모르면 키 그대로
const ITEM = { age: "나이", sex: "성별", ecog: "몸 상태 점수 (ECOG)", "basis:age": "나이 기록", "review:underspecified": "의사가 판단할 조건",
  "biomarker:EGFR": "EGFR 유전자 검사", "biomarker:ALK": "ALK 유전자 검사", "biomarker:PD-L1": "PD-L1 검사", biomarker: "유전자 검사 결과" };
const LAB = { hemoglobin: "혈색소", platelets: "혈소판", "absolute neutrophil count": "호중구 (백혈구의 한 종류)", anc: "호중구 (백혈구의 한 종류)",
  "white blood cell count": "백혈구", wbc: "백혈구", creatinine: "크레아티닌 (콩팥 수치)", "creatinine clearance": "콩팥 기능",
  "total bilirubin": "빌리루빈 (간 수치)", bilirubin: "빌리루빈 (간 수치)", alt: "ALT (간 수치)", ast: "AST (간 수치)", albumin: "알부민",
  inr: "피 굳는 속도 (INR)", tsh: "갑상선 수치 (TSH)", potassium: "칼륨", sodium: "나트륨", glucose: "혈당", hba1c: "당화혈색소" };
const itemName = (k) => { const t = String(k ?? "");
  if (ITEM[t]) return ITEM[t];
  if (t.startsWith("lab:")) return `피 검사 · ${LAB[t.slice(4).toLowerCase()] || t.slice(4)}`;
  if (t.startsWith("biomarker:")) return `유전자 검사 · ${t.slice(10)}`;
  if (/^criterion:/.test(t)) return "의사가 판단할 조건";
  return t; };
// 확인에 쓰는 서류 (같은 서류 = 한 번 받아서 여러 항목을 함께 확인)
const SOURCE = { lab_report: "피 검사 결과지", pathology_report: "조직 검사 보고서", genomic_report: "유전자 검사 보고서", medical_record: "진료 기록",
  imaging_report: "영상 검사 결과", clinician_review: "의사 판단", patient: "환자에게 질문", patient_interview: "환자에게 질문" };
const DOC_KIND = { lab_report: "lab_report", pathology_report: "pathology_report", genomic_report: "genomic_report", imaging_report: "imaging_report" };

const badge = (map, k) => { const [c, t] = map[k] || ["", k]; return `<span class="badge ${c}">${h(t)}</span>`; };
const shortId = (s) => String(s || "").slice(0, 14) + (String(s || "").length > 14 ? "…" : "");
const when = (s) => { const d = new Date(s); return isNaN(d) ? h(s) : h(d.toLocaleString("ko-KR")); };
const who = (cid) => S.labels[cid] || shortId(cid);
const min = (m) => `${Number(m || 0).toLocaleString("ko-KR", { maximumFractionDigits: 1 })}분`;
const trialName = (tid) => S.titles[tid] ? `${tid} · ${S.titles[tid]}` : tid;

function headers(extra) {  // 발표용 데모 서버는 키 없이 받음 (서버가 판단, 화면은 키를 모름)
  return { ...(S.demo ? {} : { "X-CTREC-Staff-Key": S.key }), "X-CTREC-Staff-Id": S.sid, ...(extra || {}) };
}
async function api(path, body) {
  const opt = body === undefined ? { headers: headers() }
    : { method: "POST", headers: headers({ "Content-Type": "application/json", "X-CTREC-Request": "1" }), body: JSON.stringify(body) };
  let r;
  try { r = await fetch("/api/followup/staff" + path, opt); } catch (e) { throw new Error("서버에 연결하지 못했어요."); }
  let d = null; try { d = await r.json(); } catch (e) { d = null; }
  if (!r.ok) {
    const det = d && d.detail; const msg = det && det.message ? det.message : typeof det === "string" ? det : `요청 실패 (${r.status})`;
    const err = new Error(msg); err.status = r.status; err.code = det && det.code; throw err;
  }
  return d;
}
// 서류 확인 담당·의사 역할 호출 (발표용 데모: 키 대신 X-CTREC-Demo-Role, 담당자 헤더는 붙이지 않음 - 서버는 한 요청에 한 역할만 받음)
async function roleApi(role, path, body) {
  const hd = { "X-CTREC-Demo-Role": role };
  const opt = body === undefined ? { headers: hd }
    : { method: "POST", headers: { ...hd, "Content-Type": "application/json", "X-CTREC-Request": "1" }, body: JSON.stringify(body) };
  let r;
  try { r = await fetch("/api/followup" + path, opt); } catch (e) { throw new Error("서버에 연결하지 못했어요."); }
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
  if (!staff && typeof window.fuRefresh === "function") window.fuRefresh(false);  // 환자 화면으로 돌아오면 바로 다시 확인
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
  q$("#st-login-hint").textContent = `${S.sid} 이름으로 연결했어요. 이 탭을 닫으면 키가 지워져요.`;
  clearInterval(S.timer); S.timer = setInterval(poll, POLL_MS);
  return true;
}
// 발표용 데모 서버(--demo-staff)면 키 입력 없이 바로 연결. 실제 서버는 mode "key" -> 키 입력
async function trySession() {
  let d = null;
  try { const r = await fetch("/api/followup/staff/session"); if (r.ok) d = await r.json(); } catch (e) { d = null; }
  if (!d || d.mode !== "demo") return false;
  S.demo = true; S.sid = d.staff_id || "demo-staff"; S.roles = d.demo_roles || [];
  q$("#st-login").hidden = true;  // 데모에서는 연결 칸 대신 '발표용' 표시만 (아래 요약에)
  try { await loadAll(); } catch (e) { q$("#st-login").hidden = false; setConn("연결 실패", "bad"); q$("#st-login-hint").textContent = e.message; return true; }
  q$("#st-work").hidden = false;
  clearInterval(S.timer); S.timer = setInterval(poll, POLL_MS);
  // 발표용 데모: 핵심은 연구진 화면 -> 연구 담당자를 앞에, 환자 화면은 시연용으로 (주소가 / 이고 환자 사례를 진행 중이 아닐 때 연구진 화면부터)
  const nav = q$("#roles"), sb = nav.querySelector('[data-role="staff"]'), pb = nav.querySelector('[data-role="patient"]');
  nav.insertBefore(sb, pb); pb.textContent = "환자 신청 (시연용)";
  let resume = false; try { resume = !!localStorage.getItem("ctrec-followup-case"); } catch (e) { /* 저장소 없음 */ }
  if (location.pathname === "/" && !location.hash && !resume) setRole("staff", true);
  return true;
}
function logout() {
  try { sessionStorage.removeItem(KEY); } catch (e) { /* 저장소 없음 */ }
  S.key = ""; S.caseId = null; S.data = null; clearInterval(S.timer);
  q$("#st-work").hidden = true; q$("#st-logout").hidden = true; q$("#st-key").value = ""; setConn("연결 안 됨", "");
}

// ------------------------------------------------------------ 불러오기
async function loadTitles() {
  if (Object.keys(S.titles).length) return;
  try { const r = await fetch("/api/followup/trials"); if (r.ok) for (const t of (await r.json()).trials) S.titles[t.trial_id] = t.title; } catch (e) { /* 이름 없이 ID만 */ }
}
async function loadAll() {
  const [cases, queue, ins] = await Promise.all([api("/cases"), api("/queue"), api("/insight"), loadTitles()]);
  const list = [...cases.cases].sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)) || String(a.case_id).localeCompare(String(b.case_id)));
  S.labels = Object.fromEntries(list.map((c, i) => [c.case_id, `환자 ${i + 1}`]));  // 들어온 순서 번호 (화면 표시용)
  S.questions = ins.questions.filter(g => g.route !== "clinician_review");  // 의사 판단 요청은 서버가 자동으로 만듦
  S.cases = list;
  S.insight = ins.summary;
  renderSummary(ins.summary, list);
  renderQuestions();
  estimate(true);
  // 읽지 못한 사례(손상된 사례 파일)는 목록에서 빠지므로 조용히 사라지지 않게 개수를 알림 (감사 F-17)
  const unreadable = new Set([cases, queue, ins].flatMap(x => (x && x.unreadable) || []));
  renderCases(list, unreadable.size);
  renderQueue(queue);
}
async function poll() {
  if (!(S.key || S.demo) || S.busy || document.visibilityState !== "visible" || q$("#page-staff").hidden) return;
  try { await loadAll(); if (S.caseId) await refreshCase(); } catch (e) { /* 다음 주기에 다시 */ }
}

// ------------------------------------------------------------ 한눈에 보기: 묶어서 추천 순서로 하면 시간이 얼마나 줄어드는가
// 화면 원칙 (11-08 동겸, 연구진 탭이 핵심): ① 어떤 질문을 할지 ② 그 질문을 고르면 얼마나 줄어드는지를 먼저 보여 줌.
// 모든 분은 엔진 카탈로그의 가정 비용(실측 아님), 시험 수는 답이 모두 온다고 볼 때의 상한. 화면은 숫자를 만들지 않고 서버 값만 씀.
function curveSvg(curve, total, at) {
  const pts = (curve || []).filter(p => p.minutes != null); if (!pts.length || !total) return "";
  const W = 320, H = 96, P = 6, maxM = pts[pts.length - 1].minutes || 1;
  const x = m => P + (W - 2 * P) * Math.min(m, maxM) / maxM, y = n => H - P - (H - 2 * P) * n / total;
  let d = `M${x(0)},${y(0)}`, prev = 0;
  for (const p of pts) { d += ` L${x(p.minutes)},${y(prev)} L${x(p.minutes)},${y(p.decided_pairs)}`; prev = p.decided_pairs; }
  const n = at == null ? null : reachAt(curve, at);
  return `<svg class="curve" viewBox="0 0 ${W} ${H}" role="img" aria-label="추천 순서대로 확인할 때 쓰는 시간(가로)에 따라 결과가 나올 수 있는 시험 수(세로)">
    <line x1="${P}" y1="${H - P}" x2="${W - P}" y2="${H - P}" class="axis"/><path d="${d}" class="line"/>
    ${n == null ? "" : `<line x1="${x(at)}" y1="${P}" x2="${x(at)}" y2="${H - P}" class="mark"/><circle cx="${x(at)}" cy="${y(n)}" r="4" class="dot"/>`}</svg>`;
}
const reachAt = (curve, m) => (curve || []).reduce((n, p) => p.minutes <= m + 1e-9 ? Math.max(n, p.decided_pairs) : n, 0);
function nextStep(list, unsent) {
  const sum = (k) => list.reduce((n, c) => n + ((c.open_by_status || {})[k] || 0), 0);
  const doc = sum("requires_document"), doc2 = sum("requires_clinician"), ans = sum("pending");
  if (!list.length) return { text: "환자 화면에서 환자 정보를 넣고 '연구진 확인 요청하기'를 누르면 여기에 환자가 생겨요.", go: null };
  if (unsent) return { text: `① 질문 고르기: 아래 추천 질문을 고르고 '고른 질문 보내기'를 누르세요.`, go: null };
  if (doc) return { text: `② 서류 확인: 받아야 할 서류가 ${doc}건 있어요. 가상 서류 값이 미리 채워져 있으니 '서류 확인 끝'만 누르세요.`, go: "c", label: "서류 확인하러 가기" };
  if (doc2) return { text: `③ 의사 판단: 의사가 판단할 조건이 ${doc2}건 있어요. 서류 확인을 먼저 끝내면 판단할 수 있어요.`, go: "c", label: "의사 판단하러 가기" };
  if (ans) return { text: `환자가 질문 ${ans}개에 답하기를 기다리고 있어요. 환자 화면에서 답하면 결과가 바로 바뀌어요.`, go: null };
  return { text: "지금 할 일이 없어요. 모든 확인이 끝났거나, 더 확인할 방법이 없는 시험만 남았어요.", go: null };
}
function renderSummary(s, list) {
  const rem = S.questions.flatMap(g => g.patients.filter(p => !p.request_id && p.requestable));
  const step = nextStep(list, rem.length);
  const stepHtml = `<div class="next-step" role="status"><b>지금 할 일</b><p class="m0">${h(step.text)}</p>
    ${step.go ? `<button class="btn sm" type="button" data-go="${h(step.go)}">${h(step.label)}</button>` : ""}</div>`;
  if (!s || !s.cases) { q$("#st-summary").innerHTML = stepHtml; return; }
  const pc = (k) => list.reduce((n, c) => n + ((c.pair_counts || {})[k] || 0), 0);
  const all = list.reduce((n, c) => n + Object.values(c.pair_counts || {}).reduce((a, b) => a + b, 0), 0);
  const remMin = rem.reduce((n, p) => n + (p.minutes || 0), 0), remAlone = rem.reduce((n, p) => n + (p.minutes_alone || 0), 0);
  const saved = Math.max(0, remAlone - remMin), pct = remAlone ? Math.round(100 * saved / remAlone) : 0;
  const open = list.reduce((n, c) => n + (c.open_requests || 0), 0);
  const sentAlone = S.questions.flatMap(g => g.patients.filter(p => p.request_id)).reduce((n, p) => n + (p.minutes_alone || 0), 0);
  const maxM = ((s.c_curve || []).slice(-1)[0] || {}).minutes || 0;
  if (S.budget == null || S.budget > maxM) S.budget = Math.round(maxM / 2);
  q$("#st-summary").innerHTML = `
    <p class="lead m0">TRIALOG는 신청한 환자 전체를 보고 <b>다음에 확인할 질문을 고르고</b>, <b>환자마다 같은 서류로 확인할 것을 묶어</b> 여러 시험의 판정에 필요한 연락을 줄여요.</p>
    ${rem.length ? `<div class="hero-nums" aria-label="남은 확인 ${h(rem.length)}건의 예상 시간">
      <div class="num"><span>하나씩 따로 하면</span><b>${h(min(remAlone))}</b></div>
      <div class="arrow" aria-hidden="true">→</div>
      <div class="num accent"><span>환자마다 같은 서류로 묶으면</span><b>${h(min(remMin))}</b></div>
      <div class="num ok"><span>줄어드는 시간</span><b>${h(min(saved))}</b><em>${h(pct)}% 절약 · 가정된 활동시간 기준</em></div></div>`
      : sentAlone > s.spent_minutes ? `<div class="hero-nums" aria-label="보낸 요청의 예상 시간">
      <div class="num"><span>보낸 요청을 하나씩 했다면</span><b>${h(min(sentAlone))}</b></div>
      <div class="arrow" aria-hidden="true">→</div>
      <div class="num accent"><span>환자마다 같은 서류로 묶어 보낸 시간</span><b>${h(min(s.spent_minutes))}</b></div>
      <div class="num ok"><span>줄어든 시간</span><b>${h(min(sentAlone - s.spent_minutes))}</b><em>${h(Math.round(100 * (sentAlone - s.spent_minutes) / sentAlone))}% 절약 · 가정된 활동시간 기준</em></div></div>
      <p class="m0">보낼 질문을 모두 보냈어요.</p>`
      : `<p class="m0">보낼 질문을 모두 보냈어요. 지금까지 보낸 요청의 예상 시간 ${h(min(s.spent_minutes))}.</p>`}
    <p class="hint m0">환자 ${h(s.cases)}명 · 남은 확인 ${h(rem.length)}건 · 진행 중인 요청 ${h(open)}건 · 결과: 참여 가능(확정) ${h(pc("CONFIRMED_ELIGIBLE"))} · 참여 불가(확정) ${h(pc("CONFIRMED_INELIGIBLE"))} / 전체 ${h(all)}개 시험
      <br>※ '분'은 일의 양을 비교하려고 미리 정한 <b>예상 시간(가정)</b>이에요. 실제로 잰 시간이 아니에요.${S.demo ? " · 발표용 데모 (가상 환자·가상 시험)" : ""}</p>
    ${stepHtml}
    ${maxM > 0 && s.decidable_pairs ? `<details class="budget"${S.budgetOpen ? " open" : ""}><summary><b>이 시간이면?</b> <span class="hint">추천 순서대로 확인할 때 결과가 나올 수 있는 시험 (상한 · 예산 제한은 적용하지 않음)</span></summary>
      <div class="curve-wrap mt8"><div>${curveSvg(s.c_curve, s.decidable_pairs, S.budget)}
        <input type="range" id="st-budget" min="0" max="${h(Math.ceil(maxM))}" step="1" value="${h(S.budget)}" aria-label="쓸 시간 (분)"></div>
        <p class="m0" id="st-budget-out">${budgetText(s, S.budget)}</p></div></details>` : ""}`;
}
function budgetText(s, m) {
  return `<b>${h(min(m))}</b> 쓰면 결과가 나올 수 있는 시험 <b>최대 ${h(reachAt(s.c_curve, m))}개</b> <span class="hint">/ 모두 하면 ${h(s.decidable_pairs)}개</span>`;
}

// ------------------------------------------------------------ ① 질문 고르기 (질문 = 연락 한 번: 서류 한 장 또는 환자에게 묻는 항목 하나)
// 같은 서류(예: 피 검사 결과지)로 확인하는 항목은 한 질문으로 묶음: 한 번 받아서 여러 항목을 함께 확인하니 연락·서류 비용을 한 번만 씀
const cardKey = (g) => g.route === "document_review" ? `doc|${g.source_id || g.info_key}` : `${g.route}|${g.info_key}`;
function buildCards() {
  const cards = [];
  for (const g of [...S.questions].sort((a, b) => a.best_rank - b.best_rank)) {
    const k = cardKey(g);
    let c = cards.find(x => x.key === k);
    if (!c) cards.push(c = { key: k, route: g.route, source_id: g.source_id, rank: g.best_rank, items: [] });
    c.items.push(g);
  }
  for (const c of cards) {
    const ps = c.items.flatMap(g => g.patients.map(p => ({ ...p, info_key: g.info_key })));
    c.picks = ps.filter(p => !p.request_id && p.requestable).map(p => ({ case_id: p.case_id, proposal_id: p.proposal_id }));
    c.sent = ps.filter(p => p.request_id);
    const todo = ps.filter(p => !p.request_id && p.requestable), scope = todo.length ? todo : ps;  // 보낼 것이 있으면 아직 안 보낸 환자만 셈
    c.patients = [...new Set(scope.map(p => p.case_id))];
    const pairs = new Set(scope.flatMap(p => p.related_trials.map(t => `${p.case_id}|${t}`)));
    c.related = pairs.size;
    c.recordAfter = c.items.reduce((n, g) => n + (g.n_record_after_pairs || 0), 0);
    c.rows = ps;
  }
  return cards;
}
function cardTitle(c) {
  if (c.route === "document_review") return `${SOURCE[c.source_id] || "서류"} 요청`;
  if (c.route === "ask_patient") return `환자에게 질문 · ${itemName(c.items[0].info_key)}`;
  return `${KIND[c.route] || c.route} · ${itemName(c.items[0].info_key)}`;
}
function qcard(c, i) {
  const e = (S.est.groups || {})[c.key];
  const on = S.pick.has(c.key), open = !c.picks.length;
  const saved = e ? Math.max(0, e.minutes_alone - e.minutes) : 0;
  const gain = e ? Math.max(0, e.decidable_pairs - e.decidable_pairs_before) : null;
  return `<div class="qc${on ? " on" : ""}${open ? " sent" : ""}" data-key="${h(c.key)}">
    <label class="qc-pick"><input type="checkbox" data-pick="${h(c.key)}"${on ? " checked" : ""}${open ? " disabled" : ""} aria-label="${h(cardTitle(c))} 고르기">
      <span class="qc-rank" title="추천 순서">${h(i + 1)}</span></label>
    <div class="qc-body">
      <p class="qname">${h(cardTitle(c))} ${open ? '<span class="badge ok">보냄</span>' : ""}</p>
      ${c.route === "document_review" ? `<p class="m0 qc-items">${c.items.map(g => h(itemName(g.info_key))).join(" · ")}</p>` : ""}
      <dl class="qc-facts">
        <div><dt>누구에게</dt><dd>환자 ${h(c.patients.length)}명${c.route === "document_review" ? ' <span class="hint">· 환자마다 서류 한 장</span>' : ""}${!open && c.sent.length ? ` <span class="hint">(이미 보낸 ${h(c.sent.length)}건 제외)</span>` : ""}</dd></div>
        <div><dt>왜</dt><dd>${gain == null ? "계산 중…" : gain ? `<b class="good">결과가 나올 수 있는 시험 +${h(gain)}개</b> <span class="hint">(최대)</span>` : `관련 시험 ${h(c.related)}개 <span class="hint">(다른 확인과 함께 하면 결과가 나와요)</span>`}</dd></div>
        <div><dt>시간</dt><dd>${!e ? "계산 중…" : open ? `보낸 요청 ${h(c.sent.length)}건` : saved ? `<b>${h(min(e.minutes))}</b> <span class="hint">하나씩 하면 ${h(min(e.minutes_alone))}</span> <b class="good">${h(min(saved))} 절약</b>` : `<b>${h(min(e.minutes))}</b>`}</dd></div>
      </dl>
      ${c.recordAfter ? `<p class="hint m0">환자가 답해도 서류 확인이 한 번 더 필요한 시험 ${h(c.recordAfter)}개</p>` : ""}
      <details${S.openCards.has(c.key) ? " open" : ""} data-k="${h(c.key)}"><summary class="hint">환자별로 보기</summary><table class="list mt8"><thead><tr><th>환자</th><th>항목</th><th>관련 시험</th><th>상태</th></tr></thead><tbody>
        ${c.rows.map(p => `<tr><td><button class="link" type="button" data-case="${h(p.case_id)}">${h(who(p.case_id))}</button></td><td>${h(itemName(p.info_key))}</td>
          <td class="mono">${h(p.related_trials.join(", ") || "-")}</td>
          <td>${p.request_status ? badge(REQ, p.request_status) : p.requestable ? '<span class="hint">아직 안 보냄</span>' : '<span class="hint">보낼 수 없음</span>'}</td></tr>`).join("")}
      </tbody></table></details></div></div>`;
}
function renderQuestions() {
  S.openCards = new Set([...document.querySelectorAll("#st-qlist details[data-k][open]")].map(d => d.dataset.k));
  const cards = S.cards = buildCards();
  const todo = cards.filter(c => c.picks.length), sent = cards.filter(c => !c.picks.length);
  for (const k of [...S.pick]) if (!todo.some(c => c.key === k)) S.pick.delete(k);
  if (!S.pickInit && todo.length) { todo.slice(0, 3).forEach(c => S.pick.add(c.key)); S.pickInit = true; }  // 처음엔 추천 3개를 골라 둠
  if (!cards.length) { q$("#st-qlist").innerHTML = '<div class="empty">지금 보낼 질문이 없어요.</div>'; renderPickBar(); return; }
  const top = todo.slice(0, 3), rest = todo.slice(3);
  q$("#st-qlist").innerHTML = `
    <div class="row qc-head"><p class="m0"><b>추천 질문</b> <span class="hint">추천 순서(Cohort C) · 예산 제한은 적용하지 않음</span></p>
      <span class="row"><button class="btn ghost sm" type="button" data-pick-top="1">추천 3개 고르기</button><button class="btn ghost sm" type="button" data-pick-none="1">모두 해제</button></span></div>
    ${top.map((c, i) => qcard(c, i)).join("") || '<div class="empty">보낼 질문을 모두 보냈어요. 이제 ② 받은 서류·판단 처리에서 서류를 확인하세요.</div>'}
    ${rest.length ? `<details class="qc-more"${S.moreOpen ? " open" : ""}><summary>나머지 질문 ${h(rest.length)}개 보기</summary><div class="stack mt8">${rest.map((c, i) => qcard(c, i + 3)).join("")}</div></details>` : ""}
    ${sent.length ? `<details class="qc-more"><summary class="hint">이미 보낸 질문 ${h(sent.length)}개</summary><div class="stack mt8">${sent.map((c, i) => qcard(c, todo.length + i)).join("")}</div></details>` : ""}
    <p class="hint m0">의사가 판단할 조건은 필요할 때 자동으로 요청이 만들어져요 (② 받은 서류·판단 처리에서 처리).</p>`;
  renderPickBar();
}
function pickedList() { return S.cards.filter(c => S.pick.has(c.key)).flatMap(c => c.picks); }
function renderPickBar() {
  const bar = q$("#st-pickbar"); if (!bar) return;
  const n = S.cards ? S.cards.filter(c => S.pick.has(c.key)).length : 0, e = S.est.picked;
  if (!n) { bar.innerHTML = `<p class="m0 hint">질문을 고르면 함께 보낼 때의 예상 시간이 여기에 나와요.</p>${S.sendMsg ? `<span class="err ok-msg" role="status">${h(S.sendMsg)}</span>` : ""}`; return; }
  const saved = e ? Math.max(0, e.minutes_alone - e.minutes) : 0, gain = e ? Math.max(0, e.decidable_pairs - e.decidable_pairs_before) : 0;
  bar.innerHTML = `<div class="pick-sum"><b>고른 질문 ${h(n)}개</b>
      ${e ? `<span>함께 보내면 <b>${h(min(e.minutes))}</b></span>${saved ? `<span class="hint">하나씩 하면 ${h(min(e.minutes_alone))}</span><span class="good"><b>${h(min(saved))} 절약</b> <span class="hint">(가정된 활동시간 기준)</span></span>` : ""}
      <span>결과가 나올 수 있는 시험 <b>+${h(gain)}개</b> <span class="hint">(최대)</span></span>` : '<span class="hint">계산 중…</span>'}</div>
    <button class="btn" type="button" data-send-picked="1">고른 질문 보내기 (${h(pickedList().length)}건)</button>
    <span class="err" id="st-qerr" role="alert">${h(S.sendMsg || "")}</span>`;
}
let estSeq = 0;
async function estimate(withGroups) {
  const seq = ++estSeq, cards = S.cards || [];
  const body = { picks: pickedList(), groups: withGroups ? cards.map(c => c.picks.length ? c.picks : []) : [] };
  let out; try { out = await api("/insight/estimate", body); } catch (e) { return; }
  if (seq !== estSeq && !withGroups) return;  // 고르기를 빨리 바꾸면 마지막 결과만
  S.est.picked = out;
  if (withGroups) { S.est.groups = {}; cards.forEach((c, i) => { if (c.picks.length) S.est.groups[c.key] = out.groups[i]; }); renderQuestions(); }
  else renderPickBar();
}
async function sendProposals(list, label) {  // list: [{case_id, proposal_id}] - 환자마다 한 번에 보냄 (같은 연락으로 묶임)
  const byCase = {};
  for (const p of list) (byCase[p.case_id] = byCase[p.case_id] || []).push(p.proposal_id);
  S.busy = true; let ok = 0; const fails = [];
  for (const [cid, ids] of Object.entries(byCase)) {
    try { const out = await api(`/cases/${encodeURIComponent(cid)}/requests`, { proposal_ids: ids }); ok += out.created.length; }
    catch (e) { fails.push(`${who(cid)}: ${e.message}`); }
  }
  S.busy = false; S.pickInit = true;
  S.sendMsg = `${label} 요청 ${ok}건을 보냈어요.` + (fails.length ? ` 못 보낸 것 ${fails.length}건 - ${fails.join(" / ")}` : "");
  await loadAll().catch(() => {}); if (S.caseId) await refreshCase();
}
function sendPicked(btn) {
  const list = pickedList(); if (!list.length) return;
  btn.disabled = true; S.pick.clear();
  return sendProposals(list, "고른 질문");
}

// ------------------------------------------------------------ ② 서류·판단 처리 (환자별)
function renderCases(list, unreadable) {
  const notice = unreadable ? `<div class="note warn" role="status">읽지 못한 사례 ${h(unreadable)}건: 사례 기록이 손상되어 목록에서 빠졌어요. 관리자에게 알려 주세요.</div>` : "";
  q$("#st-cases").innerHTML = notice + ([...list].map(c => {
    const o = c.open_by_status || {}, todo = (o.requires_document || 0) + (o.requires_clinician || 0);
    return `<button class="case-item${c.case_id === S.caseId ? " on" : ""}" type="button" data-case="${h(c.case_id)}">
      <span class="cid">${h(who(c.case_id))}</span> ${c.synthetic ? '<span class="badge demo">가상</span>' : ""}
      ${todo ? `<span class="badge warn">할 일 ${h(todo)}</span>` : o.pending ? `<span class="badge accent">환자 답 기다림</span>` : ""}<br>
      ${Object.entries(c.pair_counts || {}).map(([k, n]) => `${badge(PAIR, k)} ${h(n)}`).join(" ")}</button>`;
  }).join("") || (unreadable ? "" : '<div class="empty">아직 환자가 없어요.</div>'));
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
// 서류 확인 담당·의사 보기 (발표용 데모에서 그 역할이 켜져 있을 때만). 담당자 보기와 따로 불러 역할을 섞지 않음
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
  return Object.entries(d.pairs).map(([tid, p]) => {
    const r = shown[tid];
    const rest = (p.pending_kinds || []).map(k => GAP[k] || k);
    return `<tr><td><b>${h(trialName(tid))}</b></td><td>${badge(PAIR, p.status)}${UNCONFIRMED[p.status] ? `<br><span class="hint">${h(UNCONFIRMED[p.status])}</span>` : ""}</td>
      <td>${rest.length ? h([...new Set(rest)].join(", ")) : '<span class="hint">없음</span>'}</td>
      <td>${r ? badge(MATCH, r.match_status) : '<span class="hint">추천 목록에 없음</span>'}</td></tr>`;
  }).join("");
}
const shownProposals = (d) => d.proposals.filter(p => ACTION[p.action_type]);  // 의사 판단(자동 요청)·내부 행동(RETRIEVE 등)은 뺌
function proposalRows(d) {
  return shownProposals(d).map(p => {
    const can = p.requestable && !p.request_id;
    return `<tr><td>${can ? `<input type="checkbox" name="prop" value="${h(p.proposal_id)}" checked aria-label="${h(itemName(p.info_key || ""))} 요청 고르기">` : ""}</td>
      <td>${h(itemName(p.info_key || ""))}${p.safety_required ? ' <span class="badge bad">안전 때문에 꼭 필요</span>' : ""}</td>
      <td>${h(ACTION[p.action_type] || p.action_type)}${p.source_id && SOURCE[p.source_id] && p.action_type !== "ASK_PATIENT" ? ` · ${h(SOURCE[p.source_id])}` : ""}</td>
      <td class="mono">${h((p.target_trials || []).join(", "))}</td>
      <td>${p.request_id ? badge(REQ, p.request_status) : p.requestable ? '<span class="hint">아직 안 보냄</span>' : '<span class="hint">여기서 보낼 수 없음</span>'}</td></tr>`;
  }).join("");
}
function requestBox(r, responses) {
  const moves = ((MOVES[r.kind] || {})[r.status]) || [];
  const resp = responses.find(x => x.response_id === r.response_id);
  const work = workArea(r);
  if (held(r)) return `<div class="req held" data-rid="${h(r.request_id)}"><div class="row"><span class="badge muted">추가 확인 경로 없음 · 보류</span> <b>${h(itemName(r.info_key || ""))}</b>
      <span class="hint">자동으로 만들어짐</span></div>
    <p class="hint m0">${r.reason_code === "conflict_unresolved" ? "서로 다른 정보를 확인할 기록이 없어요." : "이 항목을 확인할 서류를 구할 수 없었어요."}
      의사가 판단하는 조건(예: 기대 여명)이 아니라 값이 있어야 하는 조건이라, 이 시험은 <b>추가 확인 경로 없음 · 보류</b>으로 남아요 (참여 불가라는 뜻이 아니에요). 잘못 확정하지 않으려는 안전한 처리예요.
      새 서류가 생기면 다시 요청하면 돼요.</p></div>`;
  return `<div class="req${DONE.has(r.status) ? " done" : ""}" data-rid="${h(r.request_id)}"><div class="row">${badge(REQ, r.status)} <b>${h(itemName(r.info_key || ""))}</b>
      <span class="hint">${h(KIND[r.kind] || r.kind)}${r.created_by === "system" ? " · 자동으로 만들어짐" : ""}</span></div>
    ${r.question && r.kind === "ask_patient" ? `<p class="q">환자에게 간 질문: ${h(r.question.question)}</p>` : ""}
    ${resp ? `<p class="m0">${resp.outcome === "unknown" ? '<span class="badge warn">환자가 "모르겠어요"를 눌렀어요</span>' : `환자의 답: <b>${h(resp.answer)}</b>`}
      ${(resp.conflicts || []).length ? ' <span class="badge bad">전에 알려 준 내용과 달라서 보류</span>' : ""}</p>` : ""}
    ${work || (NOT_SUPPORTED[r.status] ? `<p class="hint m0">${h(NOT_SUPPORTED[r.status])}</p>` : "")}
    ${docLink(r)}
    ${moves.length ? `<div class="row mt8">${moves.map(m => `<button class="btn ghost sm" type="button" data-move="${h(m)}">${h(MOVE_KO[m] || m)}</button>`).join("")}</div>` : ""}</div>`;
}

// 확인에 쓴 서류 보기 (서류 확인 담당 권한으로 원문을 불러옴 - 발표용 데모에서만)
function docLink(r) {
  const v = r.status === "document_verified" && S.roles.includes("verifier") && ((S.data || {}).verifications || []).find(x => x.request_id === r.request_id);
  return v ? `<details class="doc-view" data-doc="${h(v.document_id)}"><summary class="hint">확인에 쓴 서류 보기</summary><pre class="raw doc-text">불러오는 중…</pre></details>` : "";
}
async function showDoc(box) {
  const pre = box.querySelector(".doc-text"); if (box.dataset.loaded) return;
  try {
    const r = await fetch(`/api/followup/verifier/cases/${encodeURIComponent(S.caseId)}/documents/${encodeURIComponent(box.dataset.doc)}/content`,
      { headers: { "X-CTREC-Demo-Role": "verifier" } });
    pre.textContent = r.ok ? await r.text() : `서류를 불러오지 못했어요 (${r.status})`; box.dataset.loaded = "1";
  } catch (e) { pre.textContent = "서류를 불러오지 못했어요."; }
}
// 서류 확인 (담당자가 서류를 올리고, 서류 확인 담당이 값을 확인) -> 의사 판단. 판정은 서버가 다시 계산(화면은 결과만 보여 줌)
const unitOf = (text) => { const m = String(text || "").match(/[<>=≥≤]+\s*[\d.]+\s*([^\s,;)]+)/); return m && !/^(years?|세)$/i.test(m[1]) ? m[1] : ""; };
const daysAgo = (n) => { const d = new Date(Date.now() - n * 864e5); return d.toISOString().slice(0, 10); };
// 발표용 가상 서류 값: 요청한 조건 문장에서 기준값을 읽어, 조건에 맞는(또는 안 맞는) 값을 미리 채움.
// 화면은 값만 채우고 판정은 서버가 서류 확인 뒤 다시 계산함. 가상 사례(합성 검증 모드)에서만 이 화면이 열림.
const GENE = /EGFR|ALK|ROS1|KRAS|BRAF|MET|RET|HER2|PD-L1/i;
const LAB_RE = { "absolute neutrophil count": /\bANC\b|neutrophil/i, "white blood cell count": /\bWBC\b|white blood/i, platelets: /platelet/i };
const escRe = (t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
function critFor(vr, fd) {
  const cs = vr.criteria || [];
  const re = fd.startsWith("lab:") ? (LAB_RE[fd.slice(4).toLowerCase()] || new RegExp(escRe(fd.slice(4)), "i"))
    : fd.startsWith("biomarker") ? (fd.includes(":") ? new RegExp(escRe(fd.slice(10)), "i") : new RegExp(GENE.source + "|mutation|biomarker", "i"))
    : { age: /\bage\b|years? (old|of age)/i, ecog: /ecog|performance/i, sex: /\b(fe)?male\b|\bsex\b|wom[ae]n|\bmen\b/i }[fd];
  return (re && cs.find(c => re.test(c.text || ""))) || (cs.length === 1 ? cs[0] : null);
}
// 한 요청이 여러 시험의 기준을 함께 확인할 때(예: 혈소판 >= 75 / 100 / 150) 그 항목의 기준 전부
function critsFor(vr, fd) {
  const one = critFor(vr, fd); if (!one) return [];
  const re = fd.startsWith("lab:") ? (LAB_RE[fd.slice(4).toLowerCase()] || new RegExp(escRe(fd.slice(4)), "i"))
    : { age: /\bage\b|years? (old|of age)/i, ecog: /ecog|performance/i }[fd];
  const all = re ? (vr.criteria || []).filter(c => re.test(c.text || "")) : [];
  return all.length ? all : [one];
}
// 기준 문장 하나가 값 v로 충족되는가 (숫자 비교만, 모르면 null)
function meets(c, v) {
  const t = String((c && c.text) || ""), x = Number(v); if (isNaN(x)) return null;
  const m = t.match(/(>=|<=|≥|≤|>|<)\s*(\d+(?:\.\d+)?)/), r = t.match(/(\d+(?:\.\d+)?)\s*[-–~]\s*(\d+(?:\.\d+)?)/);
  let holds = null;
  if (m) { const y = Number(m[2]); holds = { ">=": x >= y, "≥": x >= y, "<=": x <= y, "≤": x <= y, ">": x > y, "<": x < y }[m[1]]; }
  else if (r) holds = x >= Number(r[1]) && x <= Number(r[2]);
  return holds == null ? null : (c.kind === "exclusion" ? !holds : holds);
}
// '조건에 맞게'면 그 항목의 기준 전부를 충족하는 값, '안 맞게'면 전부 어기는 값 (없으면 가장 많이 맞추는/어기는 값)
function demoValueAll(fd, vr, meet) {
  const cs = critsFor(vr, fd); if (!cs.length) return "";
  const cand = [...new Set(cs.flatMap(c => [demoValue(fd, c, true), demoValue(fd, c, false)]).filter(v => v !== ""))];
  if (cand.length < 2 || cs.length < 2) return demoValue(fd, cs[0], meet);
  const score = (v) => cs.reduce((n, c) => n + (meets(c, v) === meet ? 1 : 0), 0);
  return cand.reduce((best, v) => score(v) > score(best) ? v : best, demoValue(fd, cs[0], meet));
}
const round = (x) => x >= 10 ? Math.round(x) : Math.round(x * 10) / 10;
function demoValue(fd, c, meet) {
  const t = String((c && c.text) || ""), ok = c && c.kind === "exclusion" ? !meet : meet;
  if (fd.startsWith("biomarker")) {
    const g = ((t.match(GENE) || [fd.slice(10) || "EGFR"])[0]).toUpperCase();
    return ok ? (g === "EGFR" ? "EGFR exon 19 deletion" : `${g} positive`) : `${g} negative`;
  }
  if (fd === "sex") { const f = /female|wom[ae]n/i.test(t); return ok === f ? "female" : "male"; }
  const m = t.match(/(>=|<=|≥|≤|>|<)\s*(\d+(?:\.\d+)?)/), r = t.match(/(\d+(?:\.\d+)?)\s*[-–~]\s*(\d+(?:\.\d+)?)/);
  const whole = fd === "age" || fd === "ecog";
  if (m) {
    const v = Number(m[2]), up = /[>≥]/.test(m[1]);
    if (fd === "ecog") return up ? (ok ? v : v - 1) : (ok ? v : v + 1);
    if (fd === "age") return up ? (ok ? Math.max(v + 5, 60) : Math.max(v - 3, 0)) : (ok ? Math.min(v - 10, 60) : v + 5);
    const x = up === ok ? v * 1.2 : v * 0.8;
    return whole ? Math.round(x) : round(x);
  }
  if (r) return ok ? Number(r[2]) : Number(r[2]) + 1;  // 예: ECOG 0-1
  return "";
}
function docRows(box) {
  const said = [...box.querySelectorAll("[data-fact]:checked")].map(i => `- ${i.dataset.label}: ${i.dataset.value}`);
  return said.concat([...box.querySelectorAll(".vrow")].map(row => {
    const v = row.querySelector('[data-f="value"]').value.trim(), u = row.querySelector('[data-f="unit"]').value.trim();
    const d = row.querySelector('[data-f="date"]').value;
    return v ? `- ${row.dataset.label}: ${v}${u ? " " + u : ""}${d ? ` (검사일 ${d})` : ""}` : "";
  })).filter(Boolean);
}
function docTemplate(vr, rows) {
  return `[가상 서류 · 발표용] ${SOURCE[vr.source_id] || "진료 기록"}\n확인할 것: ${(vr.allowed_fields || []).map(itemName).join(", ")}\n결과:${rows && rows.length ? "\n" + rows.join("\n") : " "}`;
}
// '조건에 맞게/안 맞게'를 바꾸면 값과 서류 내용을 다시 채움 (서류 내용을 손으로 고쳤으면 그대로 둠)
function refillDoc(box, meet) {
  const vr = (S.vdata.requests || []).find(x => x.request_id === box.dataset.rid); if (!vr) return;
  if (meet !== null) for (const row of box.querySelectorAll(".vrow")) row.querySelector('[data-f="value"]').value = demoValueAll(row.dataset.field, vr, meet);
  const ta = box.querySelector('[data-f="text"]'); if (ta && !ta.dataset.edited) ta.value = docTemplate(vr, docRows(box));
}
// 기록할 항목: 서버가 받는 사실 항목. 유전자 검사는 사실 항목이 'biomarker' 하나(값에 변이 이름)라 'biomarker:EGFR' 같은 키는 뺌
function recordFields(vr) {
  const f = vr.allowed_fields || [];
  const g = f.filter(x => !x.startsWith("basis:"));  // basis:* 는 근거 확인 요청 키 (사실 항목은 같이 오는 age 등)
  return g.includes("biomarker") ? g.filter(x => !x.startsWith("biomarker:")) : g;
}
function workArea(r) {
  if (r.kind === "document_review" && r.status === "requires_document" && S.vdata) {
    const vr = (S.vdata.requests || []).find(x => x.request_id === r.request_id);
    if (!vr || !vr.verifiable) return "";
    const docs = S.vdata.documents || [];
    const facts = (vr.facts || []).filter(f => !f.document_verified);
    const fields = recordFields(vr).filter(fd => !facts.some(f => f.field === fd));
    const label = (fd) => itemName(fd.includes(":") ? fd : (fd === "biomarker" || !vr.info_key ? fd : vr.info_key));
    const unitFor = (fd) => { const c = critFor(vr, fd); return c ? unitOf(c.text) : ""; };
    const factText = (f) => `${f.value}${f.unit ? " " + f.unit : ""}`;
    const rows = facts.map(f => `- ${itemName(f.field)}: ${factText(f)}`).concat(fields.map(fd => { const v = demoValueAll(fd, vr, true), u = unitFor(fd);
      return v === "" ? "" : `- ${label(fd)}: ${v}${u ? " " + u : ""} (검사일 ${daysAgo(3)})`; })).filter(Boolean);
    return `<div class="work" data-work="doc" data-rid="${h(r.request_id)}">
      <p class="work-title">서류 확인하기 <span class="badge demo">발표용 · 가상 권한</span></p>
      <ol class="steps-ko">
        <li><b>받은 서류</b> <span class="hint">(${h(SOURCE[vr.source_id] || "진료 기록")})</span>
          ${docs.length ? `<select data-f="doc"><option value="">새 서류 (아래 내용)</option>${docs.map((d, i) => `<option value="${h(d.document_id)}"${i === docs.length - 1 ? " selected" : ""}>이미 올린 서류 ${i + 1} · ${when(d.registered_at)}</option>`).join("")}</select>` : ""}
          <details><summary class="hint">서류 내용 보기·고치기 (가상 서류가 미리 채워져 있어요)</summary><textarea data-f="text" rows="${3 + rows.length}">${h(docTemplate(vr, rows))}</textarea></details></li>
        <li><b>서류에 적힌 값</b> <span class="hint">이 조건을 확인해요: ${(vr.criteria || []).map(c => h(c.text)).join(" / ")}</span>
          ${facts.map(f => `<label class="inline-check"><input type="checkbox" data-fact="${h(f.fact_id)}" data-label="${h(itemName(f.field))}" data-value="${h(factText(f))}" checked> 환자가 말한 <b>${h(f.value)}${f.unit ? " " + h(f.unit) : ""}</b>이(가) 서류와 같아요</label>`).join("")}
          ${fields.length ? `<label class="hint inline-check">가상 서류 값 <select data-f="meet" aria-label="가상 서류 값" style="width:auto;display:inline-block"><option value="1">조건에 맞게</option><option value="0">조건에 안 맞게</option></select> (발표용으로 미리 채운 값, 고칠 수 있어요)</label>` : ""}
          ${fields.map(fd => `<div class="row vrow" data-field="${h(fd)}" data-label="${h(label(fd))}"><span>${h(label(fd))}</span>
            <input data-f="value" value="${h(demoValueAll(fd, vr, true))}" placeholder="값" size="10" aria-label="${h(label(fd))} 값">
            <input data-f="unit" value="${h(unitFor(fd))}" placeholder="단위" size="8" aria-label="단위">
            <input data-f="date" type="date" value="${h(daysAgo(3))}" aria-label="검사한 날"></div>`).join("")}</li>
        <li><button class="btn sm" type="button" data-act="verify">서류 확인 끝</button> <span class="hint">누르면 결과가 다시 계산돼요</span>
          <span class="err" data-f="msg" role="alert"></span></li></ol></div>`;
  }
  if (r.kind === "clinician_review" && r.status === "requires_clinician" && S.cdata) {
    const cr = (S.cdata.requests || []).find(x => x.request_id === r.request_id);
    if (!cr) return "";
    if (!cr.judgeable) return "";  // requestBox가 '추가 확인 경로 없음 · 보류'로 보여 줌
    const basis = (S.cdata.facts || []).filter(f => f.document_verified);
    return `<div class="work" data-work="judge" data-rid="${h(r.request_id)}">
      <p class="work-title clinician">의사 판단하기 <span class="badge demo">발표용 · 가상 권한</span></p>
      ${(cr.criteria || []).map(c => `<div class="crit-judge" data-crit="${h(c.criterion)}"><p class="m0">조건: <b>${h(c.text)}</b></p>
        <label class="inline-check big"><input type="radio" name="d-${h(r.request_id)}-${h(c.criterion)}" value="MET" checked> 맞아요</label>
        <label class="inline-check big"><input type="radio" name="d-${h(r.request_id)}-${h(c.criterion)}" value="NOT_MET"> 아니에요</label></div>`).join("")}
      ${basis.length ? `<p class="hint m0">판단 근거 (서류로 확인된 것): ${basis.map(f => `${h(itemName(f.field))} ${h(f.value)}${f.unit ? " " + h(f.unit) : ""}`).join(", ")}</p>
        ${basis.map(f => `<input type="hidden" data-basis="${h(f.fact_id)}">`).join("")}
        <div class="row"><button class="btn sm" type="button" data-act="judge">의사 판단 끝</button><span class="err" data-f="msg" role="alert"></span></div>`
        : '<p class="note warn m0">먼저 이 환자의 서류 확인을 하나 이상 끝내야 의사가 판단할 수 있어요 (판단에는 서류로 확인된 근거가 필요해요).</p>'}</div>`;
  }
  return "";
}
const b64 = (t) => btoa(String.fromCharCode(...new TextEncoder().encode(t)));
const transText = (tr) => (tr || []).map(x => `${trialName(x.trial_id)}: ${(PAIR[x.before] || ["", x.before])[1]} → ${(PAIR[x.after] || ["", x.after])[1]}`).join(", ");
async function afterWork(rid, msg) {
  await openCase(S.caseId); await loadAll().catch(() => {});
  const note = document.createElement("div"); note.className = "note ok"; note.setAttribute("role", "status"); note.textContent = msg;
  q$("#st-case").prepend(note);
}
async function doWork(btn) {
  const box = btn.closest(".work"), msg = box.querySelector('[data-f="msg"]'), act = btn.dataset.act;
  const cid = encodeURIComponent(S.caseId), rid = encodeURIComponent(box.dataset.rid);
  btn.disabled = true; S.busy = true; if (msg) msg.textContent = "";
  try {
    if (act === "verify") {
      const items = [...box.querySelectorAll("[data-fact]:checked")].map(i => ({ fact_id: i.dataset.fact }));
      for (const row of box.querySelectorAll(".vrow")) {
        const raw = row.querySelector('[data-f="value"]').value.trim(); if (!raw) continue;
        const num = Number(raw), it = { field: row.dataset.field, value: !isNaN(num) ? num : raw };
        const unit = row.querySelector('[data-f="unit"]').value.trim(), date = row.querySelector('[data-f="date"]').value;
        if (unit) it.unit = unit; if (date) it.observed_at = date; items.push(it);
      }
      if (!items.length) throw new Error("서류에 적힌 값이 비어 있어요. 값을 하나 이상 채워 주세요.");
      // 서류: 고른 것이 없으면 이 칸의 내용으로 새로 올림 (담당자 권한), 그다음 서류 확인 담당 권한으로 확인
      const sel = box.querySelector('[data-f="doc"]');
      let doc = sel && sel.value ? S.vdata.documents.find(d => d.document_id === sel.value) : null;
      if (!doc) {
        const vr = (S.vdata.requests || []).find(x => x.request_id === box.dataset.rid) || {}, ta = box.querySelector('[data-f="text"]');
        const text = ta.dataset.edited ? ta.value : docTemplate(vr, docRows(box));  // 손으로 안 고쳤으면 지금 칸의 값으로 서류를 씀
        doc = (await api(`/cases/${cid}/documents`, { kind: DOC_KIND[vr.source_id] || "clinical_note", media_type: "text/plain",
          content_base64: b64(text) })).document;
        S.vdata = await roleApi("verifier", `/verifier/cases/${cid}`);
      }
      const out = await roleApi("verifier", `/verifier/cases/${cid}/requests/${rid}/verification`,
        { document_id: doc.document_id, document_sha256: doc.sha256, items, expected_revision: S.vdata.revision });
      await afterWork(box.dataset.rid, `서류 확인 끝. ${transText(out.transitions) || "아직 다른 확인이 남아 있어서 결과는 그대로예요."}`); return;
    }
    if (act === "judge") {
      const basis = [...box.querySelectorAll("[data-basis]")].map(i => i.dataset.basis);
      const decisions = [...box.querySelectorAll("[data-crit]")].map(c => {
        const d = c.querySelector("input[type=radio]:checked").value;
        return { criterion: c.dataset.crit, decision: d, rationale_code: d === "MET" ? "investigator_judgment_met" : "investigator_judgment_not_met", basis_fact_ids: basis };
      });
      const out = await roleApi("clinician", `/clinician/cases/${cid}/requests/${rid}/judgment`, { decisions, expected_revision: S.cdata.revision });
      await afterWork(box.dataset.rid, `의사 판단 끝. ${transText(out.transitions) || "아직 다른 확인이 남아 있어서 결과는 그대로예요."}`); return;
    }
  } catch (e) {
    if (msg) msg.textContent = e.code === "stale_revision" ? "그 사이 내용이 바뀌었어요. 다시 눌러 주세요." : e.message;
    if (e.code === "stale_revision") await openCase(S.caseId);
  } finally { S.busy = false; btn.disabled = false; }
}

function evalRows(d) {
  const TRIG = { intake: "환자 입력", response: "환자 답", staff_status: "요청 상태 바꿈", document_verification: "서류 확인", clinician_judgment: "의사 판단" };
  return [...d.evaluations].reverse().map(e => {
    const t = e.trigger || {};
    const tr = (e.transitions || []).map(x => `<span class="mono">${h(x.trial_id || "")}</span> ${badge(PAIR, x.before)} → ${badge(PAIR, x.after)}`).join("<br>");
    return `<tr><td>${when(e.at)}</td><td>${h(TRIG[t.type] || t.type || "")}</td>
      <td>${tr || '<span class="hint">결과 변화 없음</span>'}${(e.conflicts || []).length ? `<br><span class="badge bad">정보가 서로 다름 ${h(e.conflicts.length)}건</span>` : ""}</td></tr>`;
  }).join("");
}
function renderCase(keepOpen) {
  const d = S.data; if (!d) return;
  const open = keepOpen ? [...document.querySelectorAll("#st-case details[open]")].map(x => x.dataset.k) : [];
  q$("#st-case-h").textContent = `${who(d.case_id)}${d.synthetic ? " (가상 환자)" : ""}`;
  const reqs = [...d.requests].reverse(), todo = reqs.filter(r => !DONE.has(r.status) && !held(r)), stuck = reqs.filter(held),
    done = reqs.filter(r => DONE.has(r.status));
  const props = shownProposals(d), requestable = props.filter(p => p.requestable && !p.request_id).length;
  const mine = S.questions.flatMap(g => g.patients.filter(p => p.case_id === d.case_id && !p.request_id && p.requestable));
  const mm = mine.reduce((n, p) => n + (p.minutes || 0), 0), ma = mine.reduce((n, p) => n + (p.minutes_alone || 0), 0);
  q$("#st-case").innerHTML = `
    <div class="staff-sec"><h3>시험별 결과</h3><table class="list"><thead><tr><th>시험</th><th>지금 결과</th><th>남은 확인</th><th>환자 화면</th></tr></thead>
      <tbody>${pairRows(d)}</tbody></table></div>
    <div class="staff-sec"><h3>해야 할 일 ${h(todo.length)}개</h3>
      ${todo.length ? todo.map(r => requestBox(r, d.responses)).join("") : '<div class="empty">지금 이 환자에게 할 일이 없어요.</div>'}
      <span class="err" id="st-move-err" role="alert"></span></div>
    ${stuck.length ? `<div class="staff-sec"><h3>추가 확인 경로가 없어 보류된 것 ${h(stuck.length)}개</h3>${stuck.map(r => requestBox(r, d.responses)).join("")}</div>` : ""}
    ${requestable ? `<div class="staff-sec"><h3>아직 안 보낸 요청 ${h(requestable)}개</h3>
      ${mine.length ? `<p class="hint m0">고른 요청을 한 번에 보내면 약 ${h(min(mm))}${ma > mm ? ` (하나씩 보내면 ${h(min(ma))})` : ""} 걸려요.</p>` : ""}
      <table class="list"><thead><tr><th></th><th>무엇을</th><th>어떻게</th><th>관련 시험</th><th>상태</th></tr></thead><tbody>${proposalRows(d)}</tbody></table>
      <div class="row mt8"><button class="btn sm" id="st-send" type="button"${requestable ? "" : " disabled"}>고른 요청 보내기</button>
        <span class="err" id="st-send-err" role="alert"></span></div></div>` : ""}
    ${done.length ? `<details class="staff-sec"><summary><b>끝난 일 ${h(done.length)}개</b></summary>${done.map(r => requestBox(r, d.responses)).join("")}</details>` : ""}
    <details class="staff-sec"><summary class="hint">자세한 기록 (결과가 바뀐 이력 · 판정 기록)</summary>
      <table class="list"><thead><tr><th>시각</th><th>무슨 일</th><th>결과 변화</th></tr></thead><tbody>${evalRows(d)}</tbody></table>
      <div class="mt8"><button class="btn ghost sm" id="st-insp" type="button">판정 기록 열기 (Inspector)</button><div id="st-insp-body"></div></div></details>`;
  document.querySelectorAll("#st-case details").forEach((x, i) => { x.dataset.k = String(i); if (open.includes(String(i))) x.open = true; });
}
async function sendRequests() {
  const ids = [...document.querySelectorAll('#st-case input[name="prop"]:checked')].map(i => i.value);
  if (!ids.length) { q$("#st-send-err").textContent = "보낼 요청을 하나 이상 골라 주세요."; return; }
  S.busy = true; q$("#st-send").disabled = true; q$("#st-send-err").textContent = "";
  try {
    const out = await api(`/cases/${encodeURIComponent(S.caseId)}/requests`, { proposal_ids: ids, expected_revision: S.data.revision });
    S.data = out.case; await loadRoles(); renderCase(true); await loadAll().catch(() => {});
    const n = out.created.length, dup = out.existing.length;
    q$("#st-move-err").textContent = `요청 ${n}건을 보냈어요${dup ? ` (이미 보낸 ${dup}건은 다시 보내지 않았어요)` : ""}. 위 '해야 할 일'에서 이어서 처리하세요.`;
  } catch (e) {
    q$("#st-send-err").textContent = e.code === "stale_revision" ? "그 사이 내용이 바뀌었어요. 다시 골라 주세요." : e.message;
    if (e.status === 409) await openCase(S.caseId);
  } finally { S.busy = false; }
}
async function move(rid, status) {
  S.busy = true;
  try {
    const out = await api(`/cases/${encodeURIComponent(S.caseId)}/requests/${encodeURIComponent(rid)}/status`, { status });
    S.data = out.case; await loadRoles(); renderCase(true); await loadAll().catch(() => {});
    if (out.routed && out.routed.length) q$("#st-move-err").textContent = `다른 방법으로 확인하는 요청 ${out.routed.length}건이 새로 만들어졌어요.`;
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

// ------------------------------------------------------------ 확인 대기열 = 화면의 '전체 순서' (모든 환자의 확인을 추천 순서대로, 예산 제약 미적용)
function renderQueue(queue) {
  const all = queue.items || [], items = all.filter(x => x.route && x.route !== "clinician_review" || x.request_status);
  q$("#st-queue").innerHTML = items.length ? `<p class="hint m0">환자 ${h(queue.cases)}명의 확인을 한 줄로 세운 추천 순서예요 (예산 제약 미적용). 안전 때문에 꼭 필요한 확인이 맨 먼저이고, 그다음은 남은 조건 수와 예상 시간(가정 비용)으로 정한 순서예요. 시간이 가장 적게 든다는 보장은 아니에요.</p>
    <span class="err" id="st-queue-err" role="alert"></span><table class="list"><thead><tr><th>순서</th><th>환자</th><th>확인할 것</th><th>상태</th></tr></thead><tbody>
    ${items.map((x, i) => `<tr><td>${h(i + 1)}</td><td><button class="link" type="button" data-case="${h(x.case_id)}">${h(who(x.case_id))}</button></td>
      <td>${h(itemName(x.info_key || ""))} <span class="hint">· ${h(ACTION[x.action_type] || KIND[x.route] || x.action_type)}</span>${x.safety_required ? ' <span class="badge bad">안전 때문에 꼭 필요</span>' : ""}</td>
      <td>${x.request_status ? badge(REQ, x.request_status) : x.route ? `<button class="btn soft sm" type="button" data-qsend="${h(x.proposal_id)}" data-qcase="${h(x.case_id)}">보내기</button>`
        : '<span class="hint">보낼 수 없음</span>'}</td></tr>`).join("")}</tbody></table>`
    : '<div class="empty">아직 확인할 것이 없어요.</div>';
}
async function queueSend(btn) {
  btn.disabled = true; S.busy = true;
  try {
    const out = await api(`/cases/${encodeURIComponent(btn.dataset.qcase)}/requests`, { proposal_ids: [btn.dataset.qsend] });
    await loadAll(); if (S.caseId === btn.dataset.qcase) await refreshCase();
    q$("#st-queue-err").textContent = out.created.length ? "요청을 보냈어요." : "이미 보낸 요청이에요.";  // 목록을 다시 그린 뒤 (안내 칸도 새로 만들어짐)
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
  q$("#page-staff").addEventListener("input", (e) => {
    if (e.target.id === "st-budget") { S.budget = Number(e.target.value); q$("#st-budget-out").innerHTML = budgetText(S.insight, S.budget);
      e.target.previousElementSibling.outerHTML = curveSvg(S.insight.c_curve, S.insight.decidable_pairs, S.budget); } });
  q$("#page-staff").addEventListener("toggle", (e) => { if (e.target.matches && e.target.matches("details.budget")) S.budgetOpen = e.target.open;
    if (e.target.matches && e.target.matches("details.qc-more") && !e.target.querySelector(".sent")) S.moreOpen = e.target.open; }, true);
  q$("#page-staff").addEventListener("change", (e) => {
    if (e.target.matches("[data-pick]")) { const k = e.target.dataset.pick; if (e.target.checked) S.pick.add(k); else S.pick.delete(k);
      S.pickInit = true; S.sendMsg = ""; e.target.closest(".qc").classList.toggle("on", e.target.checked); renderPickBar(); estimate(false); }
    if (e.target.matches('.work [data-f="meet"]')) refillDoc(e.target.closest(".work"), e.target.value === "1");
    if (e.target.matches('.work .vrow input, .work [data-fact]')) refillDoc(e.target.closest(".work"), null); });
  q$("#page-staff").addEventListener("input", (e) => { if (e.target.matches('.work textarea[data-f="text"]')) e.target.dataset.edited = "1"; });
  q$("#page-staff").addEventListener("toggle", (e) => { if (e.target.matches && e.target.matches(".doc-view") && e.target.open) showDoc(e.target); }, true);
  q$("#page-staff").addEventListener("click", (e) => {
    const go = e.target.closest("[data-go]");
    if (go) { const v = go.dataset.go; tab(v);
      if (v === "c" && !S.caseId) { const c = S.cases.find(x => { const o = x.open_by_status || {}; return o.requires_document || o.requires_clinician; }); if (c) openCase(c.case_id, true); }
      return; }
    const qs = e.target.closest("[data-qsend]"); if (qs) { queueSend(qs); return; }
    if (e.target.closest("[data-send-picked]")) { sendPicked(e.target.closest("[data-send-picked]")); return; }
    if (e.target.closest("[data-pick-top]")) { S.pick = new Set(S.cards.filter(c => c.picks.length).slice(0, 3).map(c => c.key)); S.pickInit = true; renderQuestions(); estimate(false); return; }
    if (e.target.closest("[data-pick-none]")) { S.pick.clear(); S.pickInit = true; renderQuestions(); estimate(false); return; }
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
