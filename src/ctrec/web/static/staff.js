// 연구 담당자 탭 (index.html의 '연구 담당자', /staff로 바로 열림). API: /api/followup/staff/* (docs/dev/followup_api_contract.md)
// 핵심: 코디네이터 작업 시간을 줄이는 확인 선택 - 질문별로 적용 환자·판정이 정해질 수 있는 쌍·예상 분을 보여 주고 여러 환자에게 한 번에 요청.
// 판정 규칙은 서버에만 있음. 담당자는 환자 답변을 대신 입력할 수 없고, 서류 확인 완료·의료진 판단 완료 입력 계약이 없으므로 그 버튼은 두지 않음.
// app.js와 같은 페이지에서 돌므로 전역 이름을 만들지 않음 (즉시 실행 함수).
(() => {
"use strict";

const q$ = (s) => document.querySelector(s);
const h = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const KEY = "ctrec-staff-key", SID = "ctrec-staff-id";
const POLL_MS = 10000;
const S = { key: "", sid: "coordinator", demo: false, caseId: null, data: null, timer: null, busy: false, view: "q", labels: {}, questions: [] };

// 쌍 상태 7가지 (engine.contracts.PairStatus) - cohort.js와 같은 표기
const PAIR = {
  CONFIRMED_ELIGIBLE: ["ok", "적격 확정"], CONFIRMED_INELIGIBLE: ["bad", "부적격 확정"], VERIFICATION_PENDING: ["warn", "확인 대기"],
  INFO_MISSING: ["info", "정보 부족"], EXPERT_REVIEW: ["violet", "의료진 검토"], BLOCKED_FAILURE: ["muted", "확보 실패 보류"],
  NOT_EVALUATED: ["", "미평가"],
};
// 확정이 아닌 상태의 뜻 (서류·의료진 확인 전에는 확정으로 보이지 않게)
const UNCONFIRMED = { VERIFICATION_PENDING: "미확정 · 기록·서류 확인 전", EXPERT_REVIEW: "미확정 · 의료진 판단 전 (입력 기능 없음)",
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
const ITEM = { age: "나이", sex: "성별", ecog: "전신 상태 (ECOG)", "basis:age": "나이 근거 기록" };
const itemName = (k) => ITEM[k] || (String(k).startsWith("lab:") ? `검사 수치 · ${String(k).slice(4)}`
  : String(k).startsWith("biomarker:") ? `바이오마커 · ${String(k).slice(10)}` : String(k));

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
  S.demo = true; S.sid = d.staff_id || "demo-staff";
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
function renderSummary(s, list) {
  if (!s || !s.cases) { q$("#st-summary").innerHTML = '<div class="empty">아직 사례가 없습니다. 환자 탭에서 "연구진 확인 요청"으로 시작하면 여기에 나타납니다.</div>'; return; }
  const open = list.reduce((n, c) => n + (c.open_requests || 0), 0);
  const ms = (s.milestones || []).filter(m => m.c_order_minutes != null);
  const last = ms[ms.length - 1];  // 환자 순서와의 비교는 화면에 두지 않음 (11-07 결정: 순서 우열은 WP6 시뮬레이터로 판단)
  q$("#st-summary").innerHTML = `<div class="tiles">
      <div class="tile"><div class="k">열린 사례</div><div class="v">${h(s.cases)}명</div><div class="s">진행 중 요청 ${h(open)}건</div></div>
      <div class="tile info"><div class="k">남은 확인 (추천 계획)</div><div class="v">${h(s.items)}건</div><div class="s">모두 하면 약 ${h(min(s.minutes_all))}</div></div>
      <div class="tile ok"><div class="k">판정이 정해질 수 있는 쌍</div><div class="v">${h(s.decidable_pairs)}개</div>
        <div class="s">${last ? `추천 순서로 ${h(min(last.c_order_minutes))}이면 도달` : "지금 계획으로는 없음"}</div></div></div>
    <p class="hint m0">같은 환자·같은 연락은 한 번만 청구하는 비용 원장 그대로 계산한 예상 분입니다(가정 비용). '판정이 정해질 수 있는'은 답이 실제로 나온다고 볼 때의 상한이며,
      모름·연락 불가·상충이면 정해지지 않습니다. 아래 '질문별'에서 적은 시간에 많은 판정에 닿는 확인을 고르세요.</p>`;
}

// ------------------------------------------------------------ 질문별 (코디네이터가 물을 것을 고르는 화면)
function renderQuestions(qs) {
  S.questions = qs;
  if (!qs.length) { q$("#st-qlist").innerHTML = '<div class="empty">지금 제안할 확인이 없습니다.</div>'; return; }
  const sort = q$("#st-qsort") ? q$("#st-qsort").value : "rank";
  const open = [...document.querySelectorAll("#st-qlist details[open]")].map(d => d.closest(".qcard").dataset.key);
  const rows = [...qs].sort(sort === "near" ? (a, b) => (b.n_decided_pairs - a.n_decided_pairs) || (b.n_next_pairs - a.n_next_pairs) || (a.minutes - b.minutes) || (a.best_rank - b.best_rank)
    : sort === "reach" ? (a, b) => (b.n_patients - a.n_patients) || (b.n_related_pairs - a.n_related_pairs) || (a.best_rank - b.best_rank)
    : (a, b) => a.best_rank - b.best_rank);
  q$("#st-qlist").innerHTML = `<div class="row"><label class="field">정렬
      <select id="st-qsort"><option value="rank"${sort === "rank" ? " selected" : ""}>추천 순서 (Cohort C)</option>
        <option value="near"${sort === "near" ? " selected" : ""}>판정에 가까운 순</option><option value="reach"${sort === "reach" ? " selected" : ""}>적용 환자 많은 순</option></select></label>
      <span class="err" id="st-qerr" role="alert"></span></div>
    ${rows.map(g => {
      const todo = g.patients.filter(p => !p.request_id && p.requestable);
      const key = `${g.route}|${g.info_key}`;
      return `<div class="qcard" data-key="${h(key)}">
        <div class="qhead"><div><p class="qname">${h(itemName(g.info_key))} <span class="badge">${h(KIND[g.route] || g.route)}</span></p>
          ${g.preview ? `<p class="hint m0">${h(g.preview)}</p>` : ""}</div>
          <button class="btn sm" type="button" data-qbatch="1"${todo.length ? "" : " disabled"}>${todo.length ? `${h(todo.length)}명에게 요청 보내기` : "보낼 요청 없음"}</button></div>
        <div class="qstats">
          <div><b>${h(g.n_patients)}명</b><span>적용 환자</span></div>
          <div><b>${h(g.n_related_pairs)}쌍</b><span>관련 시험 쌍</span></div>
          <div class="${g.n_decided_pairs ? "good" : ""}"><b>${h(g.n_decided_pairs)}쌍 · ${h(g.n_decided_patients)}명</b><span>확인하면 판정 가능</span></div>
          <div><b>${h(g.n_next_pairs)}쌍</b><span>하나만 더 확인하면 판정</span></div>
          <div><b>${h(min(g.minutes))}</b><span>예상 코디네이터 시간</span></div>
        </div>
        ${g.n_record_after_pairs ? `<p class="hint m0">환자 답변 뒤에도 진료 기록 확인이 필요한 쌍 ${h(g.n_record_after_pairs)}개 (답변만으로는 확정되지 않음)</p>` : ""}
        <details${open.includes(key) ? " open" : ""}><summary class="hint">환자별 보기 (${h(g.patients.length)}명)</summary><table class="list mt8"><thead><tr><th>환자</th><th>추천 순위</th><th>관련 시험</th><th>예상</th><th>상태</th></tr></thead><tbody>
          ${g.patients.map(p => `<tr><td><button class="link" type="button" data-case="${h(p.case_id)}">${h(who(p.case_id))}</button></td><td>${h(p.rank)}</td>
            <td class="mono">${h(p.related_trials.join(", ") || "-")}${p.decided_trials.length ? ` <span class="badge ok">판정 가능 ${h(p.decided_trials.length)}</span>` : ""}</td>
            <td>${h(min(p.minutes))}</td><td>${p.request_status ? badge(REQ, p.request_status) : p.requestable ? '<span class="hint">요청 전</span>' : '<span class="hint">보낼 수 없음</span>'}</td></tr>`).join("")}
        </tbody></table></details></div>`;
    }).join("")}`;
}
async function sendBatch(card) {
  const [route, key] = card.dataset.key.split("|");
  const g = S.questions.find(x => x.route === route && x.info_key === key); if (!g) return;
  const todo = g.patients.filter(p => !p.request_id && p.requestable);
  card.querySelector("[data-qbatch]").disabled = true; S.busy = true;
  let ok = 0; const fails = [];
  for (const p of todo) {
    try { const out = await api(`/cases/${encodeURIComponent(p.case_id)}/requests`, { proposal_ids: [p.proposal_id] }); ok += out.created.length; }
    catch (e) { fails.push(`${who(p.case_id)}: ${e.message}`); }
  }
  S.busy = false;
  await loadAll().catch(() => {});
  q$("#st-qerr").textContent = `${itemName(key)} 요청 ${ok}건을 보냈습니다.` + (fails.length ? ` 실패 ${fails.length}건 - ${fails.join(" / ")}` : "");
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
  renderCase();
  if (focus) q$("#st-case-h").focus();
}
async function refreshCase() {
  let d; try { d = await api(`/cases/${encodeURIComponent(S.caseId)}`); } catch (e) { return; }
  if (!S.data || d.revision !== S.data.revision || JSON.stringify(d.requests) !== JSON.stringify(S.data.requests)) { S.data = d; renderCase(true); }
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
function proposalRows(d) {
  return d.proposals.map(p => {
    const can = p.requestable && !p.request_id;
    return `<tr><td>${can ? `<input type="checkbox" name="prop" value="${h(p.proposal_id)}" aria-label="요청 선택 ${h(p.rank)}">` : ""}</td>
      <td>${h(p.rank)}</td><td>${h(ACTION[p.action_type] || p.action_type)}${p.safety_required ? ' <span class="badge bad">안전 필수</span>' : ""}</td>
      <td>${h(itemName(p.info_key || ""))}</td><td class="mono">${h((p.target_trials || []).join(", "))}</td>
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
    ${NOT_SUPPORTED[r.status] ? `<p class="hint m0">${h(NOT_SUPPORTED[r.status])}</p>` : ""}
    ${moves.length ? `<div class="row mt8">${moves.map(m => `<button class="btn ghost sm" type="button" data-move="${h(m)}">${h(MOVE_KO[m] || m)}</button>`).join("")}</div>` : ""}
    <details><summary class="hint">이력 ${h((r.history || []).length)}건</summary><ol>${(r.history || []).map(x =>
      `<li>${when(x.at)} · ${h(x.from_status || "생성")} → ${h(x.to_status)} · ${h(x.by)} · <span class="mono">${h(x.reason || "")}</span></li>`).join("")}</ol></details></div>`;
}
function evalRows(d) {
  const TRIG = { intake: "환자 입력", response: "환자 응답", status: "요청 상태 변경" };
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
  q$("#st-case").innerHTML = `<div class="row">${d.synthetic ? '<span class="badge demo">가상 데이터</span>' : ""}<span class="hint">개정 ${h(d.revision)} · 생성 ${when(d.created_at)}</span>
      ${Object.entries(d.pair_counts || {}).map(([k, n]) => `${badge(PAIR, k)} ${h(n)}`).join(" ")}</div>
    <div class="staff-sec"><h3>시험별 판정과 추천</h3><table class="list"><thead><tr><th>시험</th><th>쌍 상태</th><th>남은 확인</th><th>추천 화면</th></tr></thead>
      <tbody>${pairRows(d)}</tbody></table></div>
    <div class="staff-sec"><h3>확인 제안 <span class="hint">(정책 엔진 후보 · 위에서부터 먼저)</span></h3>
      ${d.proposals.length ? `<table class="list"><thead><tr><th></th><th>#</th><th>확인</th><th>항목</th><th>대상 시험</th><th>환자 질문 미리보기</th><th>상태</th></tr></thead>
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
  const o = v.overview || {};
  box.innerHTML = `<table class="list mt8"><tbody>
      <tr><th>라운드</th><td>${h(o.n_rounds)}</td></tr><tr><th>질문</th><td>${h(o.n_questions)}</td></tr>
      <tr><th>판정 변화</th><td>${h(JSON.stringify(o.n_changes || {}))}</td></tr>
      <tr><th>최종 판정</th><td>${(o.final || []).map(f => `<span class="mono">${h(f.trial_id)}</span> ${badge(MATCH, f.match_status)}`).join("<br>")}</td></tr></tbody></table>
    <details><summary class="hint">원본 JSON</summary><pre class="raw">${h(JSON.stringify(v, null, 1).slice(0, 200000))}</pre></details>`;
}

// ------------------------------------------------------------ 확인 대기열 (Cohort C)
function renderQueue(queue) {
  const items = queue.items || [];
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
    const c = e.target.closest("[data-case]"); if (c) { openCase(c.dataset.case, true); return; }
    if (e.target.closest("#st-send")) { sendRequests(); return; }
    if (e.target.closest("#st-insp")) { inspector(); return; }
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
