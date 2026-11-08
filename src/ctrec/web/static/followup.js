// 연구진 확인 요청 흐름 (환자 화면): 환자 입력 -> 초기 판정 -> 연구 담당자 요청 -> 환자 답변·'모름' -> 재평가 -> 갱신된 추천.
// API: /api/followup/* (docs/dev/followup_api_contract.md). 판정·재평가 규칙은 서버에만 있고, 이 파일은 받은 결과를 그대로 보여 줌.
// 결과 화면은 app.js의 showResult(같은 결과 형식)를 다시 씀. 사례 접근 권한은 서버가 준 HttpOnly 쿠키뿐이며, 여기에는 사례 ID만 저장.
"use strict";

const FU_KEY = "ctrec-followup-case";
const FU_POLL_MS = 20000;
const fu = { caseId: null, revision: 0, trialsLoaded: false, timer: null, sending: false };
// 환자에게 보이는 요청 상태 (내부 코드를 그대로 보이지 않음)
const FU_STATUS = {
  pending: ["답변 기다리는 중", "accent"],
  answered: ["답이 반영됐어요", "ok"],
  unknown: ["'모름'으로 기록 · 연구진이 서류 등 다른 방법으로 확인", "warn"],
  unreachable: ["연락이 닿지 않음으로 기록", "warn"],
  requires_document: ["연구진이 서류로 확인 중", "warn"],
  requires_clinician: ["의료진 판단 대기", "warn"],
  document_unavailable: ["서류 확인 불가", "warn"],
  superseded: ["새 요청으로 바뀜", ""],
  cancelled: ["연구진이 취소함", ""],
};
// 쉬운 질문 (항목을 알 때만): 화면 문구와 입력 칸만 바꾸고, 보내는 답에는 숫자·값을 그대로 담음 (판정 규칙은 서버)
const FU_EASY = {
  age: { q: "나이가 어떻게 되세요? (만 나이)", type: "number", unit: "세", send: (v) => `${v} years old (만 ${v}세)` },
  sex: { q: "성별이 어떻게 되세요?", options: ["남성", "여성"], send: (v) => (v === "남성" ? "male (남성)" : "female (여성)") },
};
// 연구진이 하는 일의 이름 (환자에게 보이는 말)
const FU_ITEM = { ecog: "몸 상태 점수(ECOG)", age: "나이", "basis:age": "나이 기록", "lab:platelets": "혈소판 수치", "lab:hemoglobin": "혈색소 수치",
  "lab:absolute neutrophil count": "백혈구(호중구) 수치", "biomarker:EGFR": "EGFR 유전자 검사" };
const fuItem = (k) => FU_ITEM[k] || (String(k || "").startsWith("lab:") ? "피 검사 수치" : String(k || "").startsWith("biomarker:") ? "유전자 검사"
  : /^(criterion|review):/.test(String(k || "")) ? "의사가 판단할 조건" : "추가 확인");
// 보낸 답 중 화면에 보일 부분 (쉬운 질문은 괄호 안 한국어만)
const fuShownAnswer = (x) => { const a = String((x.response || {}).answer || ""); const m = FU_EASY[x.info_key] && a.match(/\(([^)]+)\)\s*$/); return m ? m[1] : a; };
const FU_WORK = { requires_document: ["연구진이 서류로 확인하고 있어요", "warn"], document_verified: ["서류 확인 끝", "ok"],
  document_unavailable: ["서류를 구하지 못했어요", ""], requires_clinician: ["의사 선생님이 판단하고 있어요", "warn"],
  clinician_decided: ["의사 판단 끝", "ok"] };
const fuGet = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
const fuSet = (k, v) => { try { if (v) localStorage.setItem(k, v); else localStorage.removeItem(k); } catch (e) { /* 저장소 없음 */ } };
const fuUrl = (path) => `/api/followup/cases/${encodeURIComponent(fu.caseId)}${path || ""}`;
async function fuDetail(r) {
  let d = null; try { d = (await r.json()).detail; } catch (e) { /* 본문 없음 */ }
  if (d && typeof d === "object" && d.message) return d.message;
  return typeof d === "string" ? d : `요청이 실패했습니다 (${r.status}).`;
}

window.fuMode = () => (document.querySelector('input[name="flow-mode"]:checked') || {}).value === "followup";

async function fuLoadTrials() {
  if (fu.trialsLoaded) return;
  let d = null;
  try { const r = await fetch("/api/followup/trials"); if (r.ok) d = await r.json(); } catch (e) { d = null; }
  if (!d) { $("#fu-trials-hint").textContent = "확인할 수 있는 임상시험 목록을 불러오지 못했습니다."; return; }
  fu.trialsLoaded = true;
  const few = d.trials.length <= 5;  // 목록이 짧으면(데모) 모두 미리 선택
  $("#fu-trials").innerHTML = d.trials.map(t => `<label class="trial-opt"><input type="checkbox" value="${esc(t.trial_id)}"${few ? " checked" : ""}>
    <span><span class="tid">${esc(t.trial_id)}</span> ${statusBadge(t.overall_status)}${t.synthetic ? ' <span class="badge demo">가상 시험 · 실제 모집 아님</span>' : ""}
    <span class="ttl">${esc(koTitle(t.trial_id, t.title))}</span></span></label>`).join("") || '<div class="empty">확인할 수 있는 임상시험이 없습니다.</div>';
  $("#fu-trials-hint").textContent = d.synthetic ? "이 서버는 가상 시험만 씁니다 (실제 모집 아님). 연구진이 확인할 시험을 고르세요."
    : "연구진이 확인할 임상시험을 고르세요. 시험마다 AI 판정이 실행됩니다.";
}
function fuOnMode() {
  const on = window.fuMode();
  $("#fu-setup").hidden = !on; $("#advanced").hidden = on;
  $("#start").textContent = on ? "연구진 확인 요청하기" : "분석 시작";
  if (on) fuLoadTrials();
}

function fuPatientText() {
  if (state.custom) return $("#custom").value.trim();
  const p = state.patients.find(x => x.patient_id === $("#patient").value);
  return p ? (p.text || "").trim() : "";
}
window.fuStart = async function () {
  const text = fuPatientText(), ids = [...document.querySelectorAll("#fu-trials input:checked")].map(i => i.value);
  const err = !text ? "환자 정보를 입력하세요." : text.length > MAX_TEXT ? `환자 정보는 ${MAX_TEXT.toLocaleString()}자 이하로 입력하세요.`
    : !ids.length ? "연구진이 확인할 임상시험을 하나 이상 선택하세요." : "";
  $("#input-error").textContent = err; $("#start-hint").textContent = "";
  if (err) { (text ? $("#fu-trials") : $("#custom")).focus(); return; }
  state.running = true; lockInput(true); $("#start").textContent = "초기 판정 중…"; setRunState("분석 중", "accent");
  let r = null;
  try { r = await post("/api/followup/cases", { patient_text: text, trial_ids: ids, input_source: "self_report" }); } catch (e) { r = null; }
  state.running = false;
  if (!r || r.status !== 201) {
    lockInput(false); fuOnMode(); setRunState("대기 중", "");
    $("#start-hint").textContent = r ? await fuDetail(r) : "서버에 연결하지 못했습니다. 잠시 후 다시 시도하세요.";
    $("#start").focus(); return;
  }
  const data = await r.json();
  fu.caseId = data.case_id; fuSet(FU_KEY, fu.caseId);
  await fuShow(data, true);
};

// 사례 화면: 결과(showResult) + 연구진 요청 목록
async function fuShow(view, focus) {
  fu.revision = view.revision;
  const v = view.view || {};
  state.sent = null;
  showResult({ result: v.result, display: v.display, trials: v.trials, match_note: v.match_note, report: v.report });
  lockInput(true); fuLock(true); $("#start").textContent = "연구진 확인 진행 중"; $("#progress-panel").hidden = true;
  setRunState(view.pending_requests ? `연구진 요청 ${view.pending_requests}건` : "연구진 확인 대기", view.pending_requests ? "warn" : "");
  $("#fu-panel").hidden = false;
  await fuLoadRequests();
  fuPoll();
  if (focus) $("#fu-h").focus();
}
function fuAnswerInput(rid, q, key) {
  const name = `fu-a-${rid}`, easy = FU_EASY[key];
  if (easy && easy.options) return easy.options.map(o => `<label class="inline-check big"><input type="radio" name="${name}" value="${esc(o)}"> ${esc(o)}</label>`).join(" ");
  if (easy) return `<input class="fu-input" type="${easy.type}" id="${name}" aria-label="답변" min="0" max="120"> <span class="hint">${esc(easy.unit)}</span>`;
  const opts = (q.options || []).filter(o => String(o).trim());
  if (q.answer_type === "yes_no") return ["예", "아니요"].map(o => `<label class="inline-check"><input type="radio" name="${name}" value="${esc(o)}"> ${esc(o)}</label>`).join(" ");
  if ((q.answer_type === "single_choice" || q.answer_type === "multi_choice") && opts.length >= 2) {
    const t = q.answer_type === "single_choice" ? "radio" : "checkbox";
    return opts.map(o => `<label class="inline-check"><input type="${t}" name="${name}" value="${esc(o)}"> ${esc(o)}</label>`).join("<br>");
  }
  const type = q.answer_type === "number" ? "number" : q.answer_type === "date" ? "date" : null;
  if (type) return `<input class="fu-input" type="${type}" id="${name}" aria-label="답변"${q.unit ? ` placeholder="${esc(q.unit)}"` : ""}>${q.unit ? ` <span class="hint">${esc(q.unit)}</span>` : ""}`;
  return `<textarea class="fu-input" id="${name}" rows="3" maxlength="2000" aria-label="답변"></textarea>`;
}
function fuReadAnswer(box, rid, q, key) {
  const name = `fu-a-${rid}`, easy = FU_EASY[key];
  if (easy) {
    const el = easy.options ? box.querySelector(`input[name="${CSS.escape(name)}"]:checked`) : box.querySelector(`#${CSS.escape(name)}`);
    const v = el ? String(el.value).trim() : ""; return v ? easy.send(v) : "";
  }
  if (["yes_no", "single_choice", "multi_choice"].includes(q.answer_type) && box.querySelector(`input[name="${CSS.escape(name)}"]`))
    return [...box.querySelectorAll(`input[name="${CSS.escape(name)}"]:checked`)].map(i => i.value).join(", ");
  const el = box.querySelector(`#${CSS.escape(name)}`);
  if (!el) return "";
  return q.unit && el.value.trim() && q.answer_type === "number" ? `${el.value.trim()} ${q.unit}` : el.value.trim();
}
async function fuLoadRequests() {
  let d = null, r = null;
  try { r = await fetch(fuUrl("/requests")); if (r.ok) d = await r.json(); } catch (e) { d = null; }
  if (!d) { $("#fu-body").innerHTML = `<div class="note bad">연구진 요청을 불러오지 못했습니다${r ? ` (${esc(r.status)})` : ""}.</div>`; return; }
  const open = d.requests.filter(x => x.answerable), done = d.requests.filter(x => !x.answerable);
  const work = d.in_progress || [];
  let html = open.length ? `<p class="m0"><b>연구진의 질문 ${open.length}개</b>가 있어요. 아는 만큼 답해 주세요. 모르면 '모르겠어요'를 누르면 돼요.</p>`
    : `<div class="note info">지금 답할 질문이 없어요.${work.some(w => FU_WORK[w.status] && FU_WORK[w.status][1] === "warn") ? " 연구진이 서류를 확인하고 있으니 기다려 주세요." : ""} 질문이 오면 여기에 나와요.</div>`;
  html += open.map(x => `<div class="fu-req" data-rid="${esc(x.request_id)}" data-key="${esc(x.info_key || "")}">
      <p class="fu-q">${esc((FU_EASY[x.info_key] || {}).q || x.question.question)}</p>
      <div class="fu-a">${fuAnswerInput(x.request_id, x.question, x.info_key)}</div>
      <div class="row mt8"><button class="btn sm" type="button" data-fu-send="1">답변 보내기</button>
        <button class="btn ghost sm" type="button" data-fu-unknown="1">모르겠어요</button></div>
      <div class="err" role="alert"></div></div>`).join("");
  if (work.length) html += `<div class="fu-work"><p class="m0"><b>연구진이 하고 있는 일</b> <span class="hint">(환자분이 하실 일은 없어요)</span></p><ul>
    ${work.map(w => { const [t, c] = FU_WORK[w.status] || [w.status, ""]; return `<li>${esc(fuItem(w.info_key))} <span class="badge ${c}">${esc(t)}</span></li>`; }).join("")}</ul></div>`;
  if (done.length) html += `<details class="more"${open.length ? "" : " open"}><summary>지난 요청 ${done.length}건</summary><div class="stack mt8">${done.map(x => {
    const [t, c] = FU_STATUS[x.status] || [x.status, ""];
    return `<div class="fu-req done"><p class="fu-q">${esc((FU_EASY[x.info_key] || {}).q || x.question.question)}</p><p class="m0"><span class="badge ${c} wrap">${esc(t)}</span>
      ${x.response && x.response.outcome === "answered" ? ` <span class="hint">내 답: ${esc(fuShownAnswer(x))}</span>` : ""}</p></div>`;
  }).join("")}</div></details>`;
  $("#fu-body").innerHTML = html;
}
async function fuRespond(box, unknown) {
  if (fu.sending) return;
  const rid = box.dataset.rid, errEl = box.querySelector(".err");
  const req = await (async () => { try { const r = await fetch(fuUrl("/requests")); return r.ok ? (await r.json()).requests.find(x => x.request_id === rid) : null; } catch (e) { return null; } })();
  if (!req || !req.answerable) { await fuRefresh(true); return; }
  const answer = unknown ? null : fuReadAnswer(box, rid, req.question, req.info_key);
  if (!unknown && !answer) { errEl.textContent = "답을 입력하거나 '모르겠어요'를 누르세요."; return; }
  fu.sending = true; box.querySelectorAll("button, input, textarea").forEach(e => { e.disabled = true; });
  errEl.textContent = ""; setRunState("답변 반영 중", "accent");
  const body = unknown ? { unknown: true } : { answer, client_response_id: "ui-" + Math.random().toString(36).slice(2, 12) };
  let r = null;
  try { r = await post(fuUrl(`/requests/${encodeURIComponent(rid)}/response`), body); } catch (e) { r = null; }
  fu.sending = false;
  if (!r || !r.ok) {
    box.querySelectorAll("button, input, textarea").forEach(e => { e.disabled = false; });
    errEl.textContent = r ? await fuDetail(r) : "서버에 연결하지 못했습니다. 다시 시도하세요.";
    if (r && r.status === 409) await fuRefresh(true);
    return;
  }
  const out = await r.json();
  await fuShow(out.case, false);
  const msg = out.conflict ? "이전에 알려 주신 내용과 달라서, 연구진이 확인할 때까지 해당 조건은 보류돼요."
    : out.outcome === "unknown" ? "'모름'으로 기록했어요. 같은 질문은 다시 하지 않고, 연구진이 서류 등 다른 방법으로 확인해요."
    : "답변을 반영해 다시 평가했어요. 아래 결과가 최신 판정이에요.";
  $("#fu-body").insertAdjacentHTML("afterbegin", `<div class="note ${out.conflict ? "warn" : "ok"}" role="status">${esc(msg)}</div>`);
  $("#fu-h").focus();
}
async function fuRefresh(force) {
  if (!fu.caseId) return;
  let r = null;
  try { r = await fetch(fuUrl()); } catch (e) { r = null; }
  if (r && r.status === 404) { window.fuForget(); $("#start-hint").textContent = "이 브라우저의 연구진 확인 요청 사례를 찾을 수 없습니다."; return; }
  if (!r || !r.ok) return;
  const data = await r.json();
  if (force || data.revision !== fu.revision || data.pending_requests) await fuShow(data, false);
}
function fuPoll() {
  clearInterval(fu.timer);
  fu.timer = setInterval(() => { if (document.visibilityState === "visible" && !fu.sending) fuRefresh(false); }, FU_POLL_MS);
}
function fuLock(on) { document.querySelectorAll('input[name="flow-mode"], #fu-trials input').forEach(i => { i.disabled = on; }); }
window.fuForget = function () {
  fuLock(false); clearInterval(fu.timer); fu.timer = null; fu.caseId = null; fu.revision = 0; fuSet(FU_KEY, null);
  $("#fu-panel").hidden = true; $("#fu-body").innerHTML = "";
  setTimeout(fuOnMode, 0);  // newRun이 입력을 다시 연 뒤 버튼 문구를 맞춤
};
window.fuInit = async function () {
  document.querySelectorAll('input[name="flow-mode"]').forEach(r => r.addEventListener("change", fuOnMode));
  fuOnMode();  // 데모 기본 경로 = 연구진 확인 요청 (/api/followup). 바로 분석(/api/runs)은 선택 사항
  $("#fu-refresh").addEventListener("click", () => fuRefresh(true));
  $("#fu-body").addEventListener("click", (e) => {
    const box = e.target.closest(".fu-req"); if (!box) return;
    if (e.target.closest("[data-fu-send]")) fuRespond(box, false);
    if (e.target.closest("[data-fu-unknown]")) fuRespond(box, true);
  });
  const saved = fuGet(FU_KEY);
  if (!saved) return false;
  fu.caseId = saved;
  let r = null;
  try { r = await fetch(fuUrl()); } catch (e) { r = null; }
  if (!r || !r.ok) { fu.caseId = null; fuSet(FU_KEY, null); return false; }
  document.querySelector('input[name="flow-mode"][value="followup"]').checked = true; fuOnMode();
  await fuShow(await r.json(), false);
  return true;
};
