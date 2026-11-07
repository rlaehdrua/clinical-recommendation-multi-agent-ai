// 연구 담당자 화면: 사례 목록·확인 대기열 -> 사례 판정(쌍 상태)·제안 행동(DCV 후보, C 순서) -> 요청 보내기 -> 요청 이력·재평가·추천·Inspector.
// API: /api/followup/staff/* (docs/dev/followup_api_contract.md). 판정 규칙은 서버에만 있음. 담당자는 환자 답변을 대신 입력할 수 없고,
// 서류 확인 완료·의료진 판단 완료 입력 계약이 없으므로 그 상태는 '완료'로 바꾸는 버튼을 두지 않음.
"use strict";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const KEY = "ctrec-staff-key", SID = "ctrec-staff-id";
const POLL_MS = 10000;
const S = { key: "", sid: "coordinator", caseId: null, data: null, timer: null, busy: false };

// 쌍 상태 7가지 (engine.contracts.PairStatus) - cohort.js와 같은 표기
const PAIR = {
  CONFIRMED_ELIGIBLE: ["ok", "적격 확정"], CONFIRMED_INELIGIBLE: ["bad", "부적격 확정"], VERIFICATION_PENDING: ["warn", "확인 대기"],
  INFO_MISSING: ["info", "정보 부족"], EXPERT_REVIEW: ["violet", "의료진 검토"], BLOCKED_FAILURE: ["muted", "확보 실패 보류"],
  NOT_EVALUATED: ["", "미평가"],
};
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
};
// 담당자가 바꿀 수 있는 상태 (followup.models.STAFF_TRANSITIONS와 같음). 완료(답변·서류 확인·판단)는 없음
const MOVES = { ask_patient: { pending: ["unreachable", "cancelled"] }, document_review: { requires_document: ["document_unavailable", "cancelled"] },
  conflict_resolution: { requires_document: ["document_unavailable", "cancelled"] }, clinician_review: { requires_clinician: ["cancelled"] } };
const MOVE_KO = { unreachable: "연락 안 됨으로 기록", document_unavailable: "서류 확인 불가로 기록", cancelled: "요청 취소" };
const NOT_SUPPORTED = { requires_document: "서류 확인 완료를 입력하는 기능이 아직 없습니다 (백엔드 계약 없음). 이 요청은 완료로 표시할 수 없습니다.",
  requires_clinician: "의료진 판단 결과를 입력하는 기능이 아직 없습니다 (백엔드 계약 없음). 이 요청은 완료로 표시할 수 없습니다." };

const badge = (map, k) => { const [c, t] = map[k] || ["", k]; return `<span class="badge ${c}">${esc(t)}</span>`; };
const shortId = (s) => String(s || "").slice(0, 14) + (String(s || "").length > 14 ? "…" : "");
const when = (s) => { const d = new Date(s); return isNaN(d) ? esc(s) : esc(d.toLocaleString("ko-KR")); };

function headers(extra) {
  return { "X-CTREC-Staff-Key": S.key, "X-CTREC-Staff-Id": S.sid, ...(extra || {}) };
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
function setConn(t, c) { const el = $("#conn"); el.textContent = t; el.className = "badge " + (c || ""); }

// ------------------------------------------------------------ 로그인
async function connect(key, sid) {
  S.key = key; S.sid = sid || "coordinator";
  try { await loadLists(); } catch (e) {
    S.key = ""; setConn(e.status === 503 ? "서버에 담당자 기능 없음" : "연결 실패", "bad");
    $("#login-hint").textContent = e.message; return false;
  }
  try { sessionStorage.setItem(KEY, S.key); sessionStorage.setItem(SID, S.sid); } catch (e) { /* 저장소 없음 */ }
  $("#workspace").hidden = false; $("#logout").hidden = false; setConn("연결됨", "ok");
  $("#login-hint").textContent = `기록용 이름 ${S.sid}로 연결했습니다. 이 탭을 닫으면 키가 지워집니다.`;
  return true;
}
function logout() {
  try { sessionStorage.removeItem(KEY); } catch (e) { /* 저장소 없음 */ }
  S.key = ""; S.caseId = null; S.data = null; clearInterval(S.timer);
  $("#workspace").hidden = true; $("#logout").hidden = true; $("#staff-key").value = ""; setConn("연결 안 됨", "");
}

// ------------------------------------------------------------ 사례 목록·대기열
async function loadLists() {
  const [cases, queue] = await Promise.all([api("/cases"), api("/queue")]);
  const list = [...cases.cases].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
  $("#cases").innerHTML = list.length ? list.map(c => `<button class="case-item${c.case_id === S.caseId ? " on" : ""}" type="button" data-case="${esc(c.case_id)}">
      <span class="cid">${esc(shortId(c.case_id))}</span> ${c.synthetic ? '<span class="badge demo">가상</span>' : ""}
      ${c.open_requests ? `<span class="badge warn">열린 요청 ${esc(c.open_requests)}</span>` : ""}<br>
      <span class="hint">${when(c.created_at)} · 개정 ${esc(c.revision)}</span><br>
      ${Object.entries(c.pair_counts || {}).map(([k, n]) => `${badge(PAIR, k)} ${esc(n)}`).join(" ")}</button>`).join("")
    : '<div class="empty">아직 사례가 없습니다. 환자 화면에서 "연구진 확인 요청"으로 시작하면 여기에 나타납니다.</div>';
  const items = queue.items || [];
  $("#queue").innerHTML = items.length ? `<p class="hint m0">여러 사례에 걸친 확인 순서 (C 순서, 안전 필수 먼저).</p><table class="list"><thead><tr><th>#</th><th>사례</th><th>확인</th><th>상태</th></tr></thead><tbody>
    ${items.map(x => `<tr><td>${esc(x.rank)}</td><td><button class="link" type="button" data-case="${esc(x.case_id)}">${esc(shortId(x.case_id))}</button></td>
      <td>${esc(ACTION[x.action_type] || x.action_type)} · <span class="mono">${esc(x.info_key || "")}</span>${x.safety_required ? ' <span class="badge bad">안전 필수</span>' : ""}</td>
      <td>${x.request_status ? badge(REQ, x.request_status) : '<span class="hint">요청 전</span>'}</td></tr>`).join("")}</tbody></table>`
    : '<div class="empty">대기열이 비어 있습니다.</div>';
}

// ------------------------------------------------------------ 사례 상세
async function openCase(id, focus) {
  S.caseId = id;
  document.querySelectorAll(".case-item").forEach(b => b.classList.toggle("on", b.dataset.case === id));
  try { S.data = await api(`/cases/${encodeURIComponent(id)}`); } catch (e) { $("#case").innerHTML = `<div class="note bad">${esc(e.message)}</div>`; return; }
  renderCase();
  clearInterval(S.timer);
  S.timer = setInterval(pollCase, POLL_MS);
  if (focus) $("#case-h").focus();
}
async function pollCase() {
  if (!S.caseId || S.busy || document.visibilityState !== "visible") return;
  let d; try { d = await api(`/cases/${encodeURIComponent(S.caseId)}`); } catch (e) { return; }
  if (!S.data || d.revision !== S.data.revision || JSON.stringify(d.requests) !== JSON.stringify(S.data.requests)) {
    S.data = d; renderCase(true); loadLists().catch(() => {});
  }
}
function pairRows(d) {
  const rec = ((d.view || {}).result || {}).recommendation || {};
  const shown = {};
  for (const r of [...(rec.ranked || []), ...(rec.recommended || [])]) shown[r.trial_id] = r;
  const excluded = new Set((rec.excluded || []).map(x => x.trial_id));
  return Object.entries(d.pairs).map(([tid, p]) => {
    const r = shown[tid];
    const recTxt = r ? `${(rec.recommended || []).some(x => x.trial_id === tid) ? `<span class="badge ok">추천${r.rank ? ` ${esc(r.rank)}위` : ""}</span> ` : ""}${badge(MATCH, r.match_status)}`
      : excluded.has(tid) ? '<span class="badge muted">추천 제외</span>' : '<span class="hint">-</span>';
    return `<tr><td class="mono">${esc(tid)}</td><td>${badge(PAIR, p.status)}</td>
      <td>${(p.pending_kinds || []).map(k => `<span class="badge">${esc(GAP[k] || k)}</span>`).join(" ") || '<span class="hint">없음</span>'}</td><td>${recTxt}</td></tr>`;
  }).join("");
}
function proposalRows(d) {
  return d.proposals.map(p => {
    const can = p.requestable && !p.request_id;
    return `<tr><td>${can ? `<input type="checkbox" name="prop" value="${esc(p.proposal_id)}" aria-label="요청 선택 ${esc(p.rank)}">` : ""}</td>
      <td>${esc(p.rank)}</td><td>${esc(ACTION[p.action_type] || p.action_type)}${p.safety_required ? ' <span class="badge bad">안전 필수</span>' : ""}</td>
      <td class="mono">${esc(p.info_key || "")}</td><td class="mono">${esc((p.target_trials || []).join(", "))}</td>
      <td>${p.patient_question_preview ? `<span class="hint">${esc(p.patient_question_preview)}</span>` : ""}</td>
      <td>${p.request_id ? badge(REQ, p.request_status) : p.requestable ? '<span class="hint">요청 전</span>' : '<span class="hint">이 화면에서 보낼 수 없음</span>'}</td></tr>`;
  }).join("");
}
function requestBox(r, responses) {
  const moves = ((MOVES[r.kind] || {})[r.status]) || [];
  const resp = responses.find(x => x.response_id === r.response_id);
  return `<div class="req" data-rid="${esc(r.request_id)}"><div class="row">${badge(REQ, r.status)} <b>${esc(KIND[r.kind] || r.kind)}</b>
      <span class="mono">${esc(r.info_key || "")}</span> <span class="hint">${esc(r.created_by || "")} · ${when(r.created_at)}</span>
      ${r.follows ? `<span class="hint">· 이전 요청 ${esc(shortId(r.follows))}의 대체 경로</span>` : ""}</div>
    ${r.question ? `<p class="q">${esc(r.question.question)}</p>` : ""}
    ${resp ? `<p class="m0">${resp.outcome === "unknown" ? '<span class="badge warn">환자 "모름"</span>' : `환자 답변: <b>${esc(resp.answer)}</b>`}
      ${(resp.conflicts || []).length ? ' <span class="badge bad">기존 사실과 상충 · 보류</span>' : ""} <span class="hint">개정 ${esc(resp.revision_after)}</span></p>` : ""}
    ${NOT_SUPPORTED[r.status] ? `<p class="hint m0">${esc(NOT_SUPPORTED[r.status])}</p>` : ""}
    ${moves.length ? `<div class="row mt8">${moves.map(m => `<button class="btn ghost sm" type="button" data-move="${esc(m)}">${esc(MOVE_KO[m] || m)}</button>`).join("")}</div>` : ""}
    <details><summary class="hint">이력 ${esc((r.history || []).length)}건</summary><ol>${(r.history || []).map(h =>
      `<li>${when(h.at)} · ${esc(h.from_status || "생성")} → ${esc(h.to_status)} · ${esc(h.by)} · <span class="mono">${esc(h.reason || "")}</span></li>`).join("")}</ol></details></div>`;
}
function evalRows(d) {
  const TRIG = { intake: "환자 입력", response: "환자 응답", status: "요청 상태 변경" };
  return [...d.evaluations].reverse().map(e => {
    const t = e.trigger || {};
    const tr = (e.transitions || []).map(x => `<span class="mono">${esc(x.trial_id || x.pair || "")}</span> ${badge(PAIR, x.before)} → ${badge(PAIR, x.after)}`).join("<br>");
    return `<tr><td>${esc(e.revision)}</td><td>${when(e.at)}</td><td>${esc(TRIG[t.type] || t.type || "")}${t.outcome ? ` · ${esc(t.outcome)}` : ""}</td>
      <td>${tr || '<span class="hint">쌍 상태 변화 없음</span>'}${(e.conflicts || []).length ? `<br><span class="badge bad">상충 ${esc(e.conflicts.length)}건</span>` : ""}</td>
      <td class="hint">재평가 ${esc((e.reevaluated_trials || []).length)} · 재사용 ${esc((e.reused_trials || []).length)}${e.equivalence && e.equivalence.status ? ` · 동등성 ${esc(e.equivalence.status)}` : ""}</td></tr>`;
  }).join("");
}
function renderCase(keepOpen) {
  const d = S.data; if (!d) return;
  const open = keepOpen ? [...document.querySelectorAll("#case details[open]")].map(x => x.dataset.k) : [];
  $("#case-h").textContent = `사례 ${shortId(d.case_id)}`;
  const reqs = [...d.requests].reverse();
  const requestable = d.proposals.filter(p => p.requestable && !p.request_id).length;
  let html = `<div class="row">${d.synthetic ? '<span class="badge demo">가상 데이터</span>' : ""}<span class="hint">개정 ${esc(d.revision)} · 생성 ${when(d.created_at)}</span>
      ${Object.entries(d.pair_counts || {}).map(([k, n]) => `${badge(PAIR, k)} ${esc(n)}`).join(" ")}</div>
    <div class="staff-sec"><h3>시험별 판정과 추천</h3><table class="list"><thead><tr><th>시험</th><th>쌍 상태</th><th>남은 확인</th><th>추천 화면</th></tr></thead>
      <tbody>${pairRows(d)}</tbody></table></div>
    <div class="staff-sec"><h3>확인 제안 <span class="hint">(정책 엔진 후보 · 위에서부터 먼저)</span></h3>
      ${d.proposals.length ? `<table class="list"><thead><tr><th></th><th>#</th><th>확인</th><th>항목</th><th>대상 시험</th><th>환자 질문 미리보기</th><th>상태</th></tr></thead>
        <tbody>${proposalRows(d)}</tbody></table>
        <div class="row mt8"><button class="btn sm" id="send" type="button"${requestable ? "" : " disabled"}>선택한 확인 요청 보내기</button>
          <span class="err" id="send-err" role="alert"></span></div>` : '<div class="empty">지금 제안할 확인이 없습니다.</div>'}
      ${(d.held || []).filter(h => h.action_type === "DEFER").length ? `<p class="hint">보류된 항목 ${esc(d.held.filter(h => h.action_type === "DEFER").length)}건 (확보 수단 소진 등, 같은 질문을 다시 하지 않음)</p>` : ""}</div>
    <div class="staff-sec"><h3>요청 이력 ${esc(reqs.length)}건</h3><span class="err" id="move-err" role="alert"></span>
      ${reqs.length ? reqs.map(r => requestBox(r, d.responses)).join("") : '<div class="empty">아직 보낸 요청이 없습니다.</div>'}</div>
    <div class="staff-sec"><h3>재평가 기록</h3><table class="list"><thead><tr><th>개정</th><th>시각</th><th>계기</th><th>변화</th><th>범위</th></tr></thead><tbody>${evalRows(d)}</tbody></table></div>
    <div class="staff-sec"><h3>Inspector</h3><button class="btn ghost sm" id="insp" type="button">판정 기록 열기</button><div id="insp-body"></div></div>`;
  $("#case").innerHTML = html;
  document.querySelectorAll("#case details").forEach((x, i) => { x.dataset.k = String(i); if (open.includes(String(i))) x.open = true; });
}
async function sendRequests() {
  const ids = [...document.querySelectorAll('#case input[name="prop"]:checked')].map(i => i.value);
  if (!ids.length) { $("#send-err").textContent = "보낼 확인을 하나 이상 고르세요."; return; }
  S.busy = true; $("#send").disabled = true; $("#send-err").textContent = "";
  try {
    const out = await api(`/cases/${encodeURIComponent(S.caseId)}/requests`, { proposal_ids: ids, expected_revision: S.data.revision });
    S.data = out.case; renderCase(true); loadLists().catch(() => {});
    const n = out.created.length, dup = out.existing.length;
    $("#send-err").textContent = `요청 ${n}건을 보냈습니다${dup ? ` (이미 보낸 요청 ${dup}건은 다시 보내지 않음)` : ""}.`;
  } catch (e) {
    $("#send-err").textContent = e.code === "stale_revision" ? "그 사이 사례가 바뀌었습니다. 최신 내용으로 다시 고르세요." : e.message;
    if (e.status === 409) await openCase(S.caseId);
  } finally { S.busy = false; }
}
async function move(rid, status) {
  S.busy = true;
  try {
    const out = await api(`/cases/${encodeURIComponent(S.caseId)}/requests/${encodeURIComponent(rid)}/status`, { status });
    S.data = out.case; renderCase(true); loadLists().catch(() => {});
    if (out.routed && out.routed.length) $("#move-err").textContent = `대체 경로 요청 ${out.routed.length}건이 만들어졌습니다.`;
  } catch (e) { $("#move-err").textContent = e.message; } finally { S.busy = false; }
}
async function inspector() {
  const box = $("#insp-body"); box.innerHTML = '<p class="hint">불러오는 중…</p>';
  let v; try { v = await api(`/cases/${encodeURIComponent(S.caseId)}/inspector`); } catch (e) { box.innerHTML = `<div class="note bad">${esc(e.message)}</div>`; return; }
  const o = v.overview || {};
  box.innerHTML = `<table class="list mt8"><tbody>
      <tr><th>라운드</th><td>${esc(o.n_rounds)}</td></tr><tr><th>질문</th><td>${esc(o.n_questions)}</td></tr>
      <tr><th>판정 변화</th><td>${esc(JSON.stringify(o.n_changes || {}))}</td></tr>
      <tr><th>최종 판정</th><td>${(o.final || []).map(f => `<span class="mono">${esc(f.trial_id)}</span> ${badge(MATCH, f.match_status)}`).join("<br>")}</td></tr></tbody></table>
    <details><summary class="hint">원본 JSON</summary><pre class="raw">${esc(JSON.stringify(v, null, 1).slice(0, 200000))}</pre></details>`;
}

// ------------------------------------------------------------ 시작
function tab(which) {
  const q = which === "queue";
  $("#tab-cases").setAttribute("aria-selected", String(!q)); $("#tab-queue").setAttribute("aria-selected", String(q));
  $("#cases").hidden = q; $("#queue").hidden = !q;
}
function init() {
  $("#login-form").addEventListener("submit", (e) => { e.preventDefault(); connect($("#staff-key").value, $("#staff-id").value.trim()); });
  $("#logout").addEventListener("click", logout);
  $("#reload").addEventListener("click", async () => { try { await loadLists(); if (S.caseId) await openCase(S.caseId); } catch (e) { setConn(e.message, "bad"); } });
  $("#tab-cases").addEventListener("click", () => tab("cases")); $("#tab-queue").addEventListener("click", () => tab("queue"));
  document.querySelector(".staff-grid").addEventListener("click", (e) => {
    const c = e.target.closest("[data-case]"); if (c) { openCase(c.dataset.case, true); return; }
    if (e.target.closest("#send")) { sendRequests(); return; }
    if (e.target.closest("#insp")) { inspector(); return; }
    const m = e.target.closest("[data-move]");
    if (m) move(m.closest(".req").dataset.rid, m.dataset.move);
  });
  let key = "", sid = "";
  try { key = sessionStorage.getItem(KEY) || ""; sid = sessionStorage.getItem(SID) || ""; } catch (e) { /* 저장소 없음 */ }
  if (sid) $("#staff-id").value = sid;
  if (key) connect(key, sid);
}
document.addEventListener("DOMContentLoaded", init);
