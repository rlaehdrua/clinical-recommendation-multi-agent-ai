// 임상시험 탐색 도우미 화면 (CSP script-src 'self': 인라인 스크립트·이벤트 속성을 쓰지 않음)
// 화면 상태는 메모리에만 둠. 실행 ID만 탭 단위(sessionStorage)로 기억 (환자 정보는 어디에도 저장하지 않음)
"use strict";
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const MAX_TEXT = 20000;  // 서버 MAX_TEXT_CHARS와 같음 (넘으면 서버가 거절)
const MAX_ANSWER = 2000;  // 서버 MAX_ANSWER_CHARS와 같음
const LONG_WAIT_MS = 45000;
const UNKNOWN_ANSWER = "모름";  // 서버의 표준 미응답 값 (decision_state.DECLINED_ANSWERS)
const DECLINED = new Set(["", "모름", "모르겠음", "모르겠어요", "잘 모름", "unknown", "답변 거부", "거부"]);
const state = { patients: [], trials: [], custom: true, runId: null, es: null, t0: 0, timer: null, cards: {}, feedN: 0,
  lastId: -1, lastEventAt: 0, running: false, waiting: false, answered: false, questionsSeen: false, stages: {},
  reconnects: 0, ended: false, submitting: false };

// 서버 공식 외부 상태 - 사용자에게 보이는 이름과 설명 (참여 자격 확정처럼 보이지 않게)
const STATUS = {
  CRITERIA_MATCH: ["공개 기준 충족", "ok", "입력된 정보로 보면 공개된 선정·제외 기준을 모두 충족합니다. 참여 확정이 아니며, 연구기관 스크리닝이 필요합니다."],
  POTENTIAL_MATCH: ["기록 확인 시 충족 가능", "warn", "남은 조건이 답변상으로는 충족하지만, 진료 기록으로 확인해야 합니다."],
  NEEDS_REVIEW: ["의료진 검토 필요", "warn", "원문에 구체적인 기준이 없어 의료진이 전체 프로토콜과 대조해야 하는 조건이 남아 있습니다."],
  INSUFFICIENT_INFORMATION: ["추가 정보 필요", "warn", "아직 확인하지 못한 정보가 있습니다. 불충족이라는 뜻이 아닙니다."],
  CRITERIA_MISMATCH: ["공개 기준 불충족", "bad", "입력된 정보로 보면 공개된 기준 중 맞지 않는 조건이 있습니다."],
  NOT_EVALUATED: ["평가 실패", "warn", "평가 중 오류로 판정하지 못했습니다. 부적격이라는 뜻이 아닙니다."],
};
// 진행 중 카드의 내부 판정 (최종 결과 전 중간 값)
const INTERIM = { ELIGIBLE: ["중간 판정: 기준 충족", "ok"], INELIGIBLE: ["중간 판정: 기준 불충족", "bad"], UNCERTAIN: ["중간 판정: 확인 필요", "warn"],
  NOT_EVALUATED: ["평가 실패", "warn"] };
const RECRUIT = { RECRUITING: ["모집 중","ok"], NOT_YET_RECRUITING: ["모집 예정","ok"], COMPLETED: ["모집 완료","bad"], TERMINATED: ["조기 종료","bad"],
  WITHDRAWN: ["철회","bad"], ACTIVE_NOT_RECRUITING: ["모집 종료","bad"], SUSPENDED: ["중단","bad"], ENROLLING_BY_INVITATION: ["초청 모집","warn"],
  UNKNOWN: ["모집 상태 미확인","warn"] };
// 서버 문구에 이런 내부 표기가 있으면 사용자 설명으로 그대로 보이지 않게 접어 둠 (글자를 바꾸지는 않음)
const CODE_RE = /\b(ELIGIBLE|INELIGIBLE|UNCERTAIN|NOT_EVALUATED|UNVERIFIED|CRITERIA_MATCH|POTENTIAL_MATCH|CRITERIA_MISMATCH|INSUFFICIENT_INFORMATION|NEEDS_REVIEW|ACTIVE_NOT_RECRUITING|NOT_YET_RECRUITING|RECRUITING|COMPLETED|TERMINATED|WITHDRAWN|SUSPENDED)\b/;
function statusBadge(s) {
  const [t, c] = RECRUIT[s] || ["모집 상태 미확인", "warn"]; return `<span class="badge ${c}">${esc(t)}</span>`;
}
function recruitText(s) { return (RECRUIT[s] || ["모집 상태 미확인"])[0]; }
function matchBadge(s) { const [t, c] = STATUS[s] || INTERIM[s] || ["검토 중", "accent"]; return `<span class="badge ${c}">${esc(t)}</span>`; }
// 외부 링크는 ClinicalTrials.gov의 https 주소만 링크로 만듦 (그 밖의 주소는 링크로 만들지 않음)
function safeTrialUrl(u) {
  try { const x = new URL(u); return x.protocol === "https:" && /(^|\.)clinicaltrials\.gov$/.test(x.hostname) ? x.href : null; } catch (e) { return null; }
}
function trialMeta(tid) { return state.trials.find(t => t.trial_id === tid) || {}; }

// ------------------------------------------------------------ 진행 단계 (서버 이벤트로만 바뀜, 진행률 숫자 없음)
const STAGES = [
  ["prep", "분석 준비"], ["profile", "환자 정보 분석"], ["criteria", "임상시험 조건 검토"], ["questions", "추가 질문"],
  ["reanalysis", "답변 반영 및 재분석"], ["recommend", "추천 결과 정리"], ["done", "완료"],
];
const STAGE_TEXT = { pending: "대기", active: "진행 중", done: "완료", skipped: "해당 없음", failed: "중단됨" };
function drawStages() {
  $("#stages").innerHTML = STAGES.map(([k, t], i) => `<li id="stage-${k}" data-state="pending"><span class="dot" aria-hidden="true">${i + 1}</span>
    <span>${esc(t)}</span><span class="st">${STAGE_TEXT.pending}</span></li>`).join("");
  state.stages = {};
}
function setStage(k, s) {
  const el = document.getElementById("stage-" + k); if (!el) return;
  if (state.stages[k] === "done" && s === "active" && k !== "questions" && k !== "reanalysis") return;  // 끝난 단계는 되돌리지 않음
  state.stages[k] = s; el.className = s === "pending" ? "" : s; el.dataset.state = s;
  el.querySelector(".st").textContent = STAGE_TEXT[s];
  if (s === "active") el.setAttribute("aria-current", "step"); else el.removeAttribute("aria-current");
}
function finishActive(except) { for (const [k] of STAGES) if (state.stages[k] === "active" && k !== except) setStage(k, "done"); }
function activate(k, note) { finishActive(k); setStage(k, "active"); if (note) stageNote(note); }
function stageNote(t) { $("#stage-note").textContent = t; }

// ------------------------------------------------------------ 개발자용 구성도·기록 (접힘)
const NODES = {
  input: { x: 70, y: 40, w: 110, label: "입력", sub: "환자 · 임상시험" }, orchestrator: { x: 360, y: 40, w: 200, label: "오케스트레이터", sub: "계획 수립 · 도구 호출" },
  patient_profiler: { x: 180, y: 150, w: 140, label: "환자 정보 이해", sub: "프로파일 구조화" }, criteria_parser: { x: 360, y: 150, w: 140, label: "기준 파싱", sub: "기준 → 원자 규칙" },
  matcher: { x: 540, y: 150, w: 140, label: "추론·매칭", sub: "규칙별 판정 + 근거" }, rule_engine: { x: 640, y: 230, w: 130, label: "규칙 엔진", sub: "나이·수치 결정론 판정" },
  answerer: { x: 60, y: 295, w: 100, label: "환자", sub: "답변" }, question_generator: { x: 210, y: 295, w: 140, label: "질문 생성", sub: "부족 정보 확인" },
  recommender: { x: 400, y: 295, w: 140, label: "추천", sub: "우선순위 결정" }, explainer: { x: 580, y: 295, w: 130, label: "결과 설명", sub: "근거 보고서" },
};
const EDGES = [["input","orchestrator"], ["orchestrator","patient_profiler"], ["orchestrator","criteria_parser"], ["orchestrator","matcher"],
  ["criteria_parser","matcher"], ["patient_profiler","matcher"], ["rule_engine","matcher"], ["orchestrator","question_generator"],
  ["question_generator","answerer"], ["answerer","patient_profiler"], ["orchestrator","recommender"], ["recommender","explainer"]];
function drawGraph() {
  const H = 34; let html = "";
  for (const [a, b] of EDGES) {
    const A = NODES[a], B = NODES[b];
    const d = Math.abs(A.y - B.y) < 5 && Math.abs(A.x - B.x) > 220
      ? `M${A.x},${A.y+22} C${A.x},${A.y+75} ${B.x},${B.y+75} ${B.x},${B.y+22}` : `M${A.x},${A.y} C${A.x},${(A.y+B.y)/2} ${B.x},${(A.y+B.y)/2} ${B.x},${B.y}`;
    html += `<path class="edge" id="e-${a}-${b}" d="${d}"/>`;
  }
  for (const [k, n] of Object.entries(NODES)) html += `<g class="node" id="n-${k}"><rect x="${n.x-n.w/2}" y="${n.y-H/2-4}" width="${n.w}" height="${H+10}" rx="10"/>
    <text x="${n.x}" y="${n.y+1}">${esc(n.label)}</text><text class="sub" x="${n.x}" y="${n.y+15}">${esc(n.sub)}</text></g>`;
  $("#graph").innerHTML = html;
}
function nodeState(k, s) { const el = document.getElementById("n-" + k); if (!el) return; el.classList.remove("active", "done"); if (s) el.classList.add(s); }
function clearEdges() { document.querySelectorAll(".edge.on").forEach(e => e.classList.remove("on")); }
const DEV_STEPS = ["① 기준 입력","② 환자 정보","③ 추출·매칭","④ 부족 정보 탐지","⑤ 질문·재평가","⑥ 추천"];
function drawSteps() { $("#steps").innerHTML = DEV_STEPS.map((s, i) => `<div class="step" id="s${i}"><b>${s.slice(0,1)}</b> ${esc(s.slice(2))}</div>`).join(""); }
function step(i, s) { const el = $("#s" + i); if (!el) return; if (s === "done") { el.classList.remove("active"); el.classList.add("done"); } else if (!el.classList.contains("done")) el.classList.add("active"); }
const WHO = { pipeline: "파이프라인", orchestrator: "오케스트레이터", screener: "후보 추림", patient_profiler: "환자 정보 이해", criteria_parser: "기준 파싱",
  matcher: "추론·매칭", question_generator: "질문 생성", answerer: "환자 답변", recommender: "추천", explainer: "결과 설명" };
const TOOL_KO = { screen_trials: "후보 시험 추림", profile_patient: "환자 프로파일 구조화", list_candidate_trials: "후보 시험 확인", search_trials: "CT.gov 검색",
  evaluate_trials: "시험 적격성 평가", ask_clarifying_questions: "확인 질문 라운드", finalize_recommendation: "최종 추천 생성" };
function feed(t, who, html) {
  const f = $("#feed"); if (state.feedN === 0) f.innerHTML = "";
  state.feedN++; $("#feed-count").textContent = `${state.feedN}개 기록`;
  const d = document.createElement("div"); d.className = "ev";
  d.innerHTML = `<div class="t">${t == null ? "" : esc(Number(t).toFixed(1)) + "s"}</div><div><span class="who">${esc(WHO[who] || who)}</span>${html}</div>`;
  f.appendChild(d); f.scrollTop = f.scrollHeight;
}

// ------------------------------------------------------------ 시작·입력
async function init() {
  drawGraph(); drawSteps(); drawStages();
  try {
    const cfg = await (await fetch("/api/config")).json();
    $("#model").textContent = "모델 " + cfg.model; $("#disclaimer").textContent = cfg.disclaimer;
    const an = cfg.analysis || {};
    if (an.backend === "test_double") $("#model").textContent = "테스트 대역 (실제 AI 분석 아님)";  // 테스트 하네스에서만
    $("#analysis-off").hidden = an.enabled !== false;
    state.patients = await (await fetch("/api/patients")).json();
    state.trials = await (await fetch("/api/trials")).json();
  } catch (e) { $("#start-hint").textContent = "서버에 연결하지 못했습니다. 잠시 후 새로고침하세요."; return; }
  const groups = {};
  for (const p of state.patients) (groups[p.group] ||= []).push(p);
  $("#patient").innerHTML = Object.entries(groups).map(([g, ps]) =>
    `<optgroup label="${esc(g)} (가상 데이터)">${ps.map(p => `<option value="${esc(p.patient_id)}">${esc(p.patient_id)} · ${esc(p.text.slice(0, 48))}…</option>`).join("")}</optgroup>`).join("");
  $("#trials").innerHTML = state.trials.map(t => `<label class="trial-opt"><input type="checkbox" value="${esc(t.trial_id)}">
    <span><span class="tid">${esc(t.trial_id)}</span> ${statusBadge(t.overall_status)}${t.synthetic ? ' <span class="badge">가상 시험</span>' : ""}
    <span class="ttl">${esc(t.title)}</span></span></label>`).join("");
  $("#trials").addEventListener("change", countTrials);
  $("#patient").addEventListener("change", onPatient);
  document.querySelectorAll('input[name="cand-mode"]').forEach(r => r.addEventListener("change", countTrials));
  $("#to-custom").addEventListener("click", () => setCustom(true)); $("#to-list").addEventListener("click", () => setCustom(false));
  $(".tabs").addEventListener("keydown", tabKeys);
  $("#custom").addEventListener("input", onTextInput);
  $("#fill-example").addEventListener("click", fillExample);
  $("#clear-input").addEventListener("click", () => { $("#custom").value = ""; onTextInput(); $("#custom").focus(); });
  $("#review").addEventListener("click", review);
  $("#edit").addEventListener("click", () => { $("#review-panel").hidden = true; (state.custom ? $("#custom") : $("#patient")).focus(); });
  $("#start").addEventListener("click", start); $("#cancel").addEventListener("click", cancelRun); $("#new-run").addEventListener("click", newRun);
  document.querySelectorAll("#pages button").forEach(b => b.addEventListener("click", () => showPage(b.dataset.page)));
  const first = state.patients.find(p => p.group === "사업단 제공") || state.patients[0];
  if (first) { $("#patient").value = first.patient_id; onPatient(); }
  setCustom(true); countTrials(); onTextInput();
  $("#resume-active").addEventListener("click", () => { $("#active-note").hidden = true; resume(state.activeId); });
  if (remembered()) resume(); else checkActive();
}
function tabKeys(e) {  // 탭 목록: 왼쪽·오른쪽 화살표로 이동 (WAI-ARIA 탭 패턴)
  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
  const other = e.target.id === "to-custom" ? $("#to-list") : $("#to-custom");
  if (other.disabled) return;
  other.focus(); other.click(); e.preventDefault();
}
function onTextInput() {
  const n = $("#custom").value.length, el = $("#char-count");
  el.textContent = `${n.toLocaleString()} / ${MAX_TEXT.toLocaleString()}자`; el.classList.toggle("over", n > MAX_TEXT);
  $("#review-panel").hidden = true; $("#input-error").textContent = "";
  $("#custom").removeAttribute("aria-invalid");
}
function fillExample() {
  // 합성 예시 (실제 환자 아님): 예시 사례 중 하나의 원문을 그대로 넣음
  const p = state.patients.find(x => x.patient_id === $("#patient").value) || state.patients[0];
  if (!p) return;
  $("#custom").value = p.text; onTextInput(); $("#custom").focus();
}
function onPatient() {
  const p = state.patients.find(x => x.patient_id === $("#patient").value); if (!p) return;
  $("#preview").textContent = p.text;
  document.querySelectorAll("#trials input").forEach(i => { i.checked = p.candidate_trials.includes(i.value); });
  const list = $("#trials"); [...list.children].sort((x, y) => y.querySelector("input").checked - x.querySelector("input").checked).forEach(el => list.appendChild(el));
  list.scrollTop = 0; countTrials(); $("#review-panel").hidden = true;
}
function setCustom(on) {
  state.custom = on; $("#custom-wrap").hidden = !on; $("#preview-wrap").hidden = on;
  $("#to-custom").setAttribute("aria-selected", String(on)); $("#to-list").setAttribute("aria-selected", String(!on));
  $("#to-custom").tabIndex = on ? 0 : -1; $("#to-list").tabIndex = on ? -1 : 0;
  $("#review-panel").hidden = true; $("#input-error").textContent = "";
  if (!on) onPatient();
}
function selectedTrials() { return [...document.querySelectorAll("#trials input:checked")].map(i => i.value); }
function screenMode() { return document.querySelector('input[name="cand-mode"]:checked').value === "screen"; }
function countTrials() {
  const on = screenMode();
  $("#trials").classList.toggle("off", on);
  document.querySelectorAll("#trials input").forEach(i => { i.disabled = on || state.running; });
  $("#trial-count").textContent = on ? `(전체 ${state.trials.length}개에서 자동 추림)` : `(${selectedTrials().length}개 선택)`;
  $("#cand-hint").textContent = on ? "전체 임상시험 중 이 환자에게 해당될 만한 시험(최대 5개)을 먼저 추린 뒤 자세히 비교합니다."
    : "비교할 시험을 직접 고릅니다. 예시 사례를 고르면 미리 정해 둔 후보가 선택됩니다.";
  $("#review-panel").hidden = true;
}
function collectRequest() {
  const body = { trial_ids: screenMode() ? [] : selectedTrials(), screen: screenMode(), allow_search: $("#search").checked };
  if (DIAGNOSTIC) body.decision_diagnostic = true;
  if (state.custom) {
    const text = $("#custom").value.trim();
    if (!text) return [null, "환자 정보를 입력하세요."];
    if (text.length > MAX_TEXT) return [null, `환자 정보는 ${MAX_TEXT.toLocaleString()}자 이하로 입력하세요.`];
    body.custom_text = text;
  } else body.patient_id = $("#patient").value;
  if (!body.screen && !body.trial_ids.length && !body.allow_search) return [null, "비교할 임상시험을 하나 이상 선택하세요."];
  return [body, null];
}
function review() {
  const [body, err] = collectRequest();
  $("#input-error").textContent = err || ""; $("#start-hint").textContent = "";
  if (!body) {
    if (state.custom) { $("#custom").setAttribute("aria-invalid", "true"); $("#custom").focus(); } else $("#patient").focus();
    return;
  }
  const text = body.custom_text ?? (state.patients.find(p => p.patient_id === body.patient_id) || {}).text ?? "";
  $("#review-meta").textContent = `${state.custom ? "직접 입력" : "예시 사례 " + body.patient_id} · ${text.length.toLocaleString()}자 · `
    + (body.screen ? "전체 시험에서 자동 추림" : `직접 선택한 시험 ${body.trial_ids.length}개`) + (body.allow_search ? " · 추가 검색 허용" : "");
  $("#review-text").textContent = text.length > 600 ? text.slice(0, 600) + " …" : text;
  $("#review-panel").hidden = false; $("#start").focus();
}
function lockInput(on) {
  ["#custom", "#patient", "#to-custom", "#to-list", "#review", "#fill-example", "#clear-input", "#search", "#start", "#edit"].forEach(s => { $(s).disabled = on; });
  document.querySelectorAll('input[name="cand-mode"]').forEach(i => { i.disabled = on; });
  document.querySelectorAll("#trials input").forEach(i => { i.disabled = on || screenMode(); });
}

// ------------------------------------------------------------ 실행·연결
const DIAGNOSTIC = new URLSearchParams(location.search).get("diagnostic") === "1";
const RUN_KEY = "ctrec-run-id";
function remember(id) { try { if (id) sessionStorage.setItem(RUN_KEY, id); else sessionStorage.removeItem(RUN_KEY); } catch (e) { /* 저장소 없음 */ } }
function remembered() { try { return sessionStorage.getItem(RUN_KEY); } catch (e) { return null; } }
function post(url, body) {
  return fetch(url, { method: "POST", headers: { "Content-Type": "application/json", "X-CTREC-Request": "1" }, body: JSON.stringify(body || {}) });
}
async function detailOf(r) { let d = null; try { d = (await r.json()).detail; } catch (e) { /* 본문 없음 */ } return typeof d === "string" ? d : `요청이 실패했습니다 (${r.status}).`; }
async function start() {
  if (state.running) return;  // 중복 제출 방지
  const [body, err] = collectRequest();
  if (!body) { $("#start-hint").textContent = err; return; }
  if (state.es) { state.es.close(); state.es = null; }
  resetView();
  state.running = true; lockInput(true); $("#start-hint").textContent = ""; $("#start").textContent = "분석 요청 중…";
  let r;
  try { r = await post("/api/runs", body); } catch (e) { r = null; }
  $("#start").textContent = "이 내용으로 분석 시작";
  if (!r || !r.ok) {
    state.running = false; lockInput(false);
    $("#start-hint").textContent = r ? await detailOf(r) : "서버에 연결하지 못했습니다. 잠시 후 다시 시도하세요.";
    $("#start").focus();
    if (r && r.status === 429) checkActive();  // 다른 탭에서 시작한 분석이 남아 있으면 이어 보기 안내
    return;
  }
  $("#review-panel").hidden = true;
  connect((await r.json()).run_id, true);
}
function connect(runId, fresh) {
  if (state.es) state.es.close();
  state.runId = runId; remember(runId); state.running = true; state.ended = false; lockInput(true);
  $("#active-note").hidden = true;
  $("#progress-panel").hidden = false; $("#cancel").hidden = false; $("#cancel").disabled = false;
  setRunState("분석 중", "accent");
  if (fresh) state.t0 = Date.now();
  state.lastEventAt = Date.now();
  clearInterval(state.timer);
  state.timer = setInterval(tick, 1000);
  $("#elapsed").hidden = false;
  openStream(runId);
  if (fresh) { $("#progress-panel").scrollIntoView({ block: "start" }); $("#progress-h").focus(); }
}
function openStream(runId) {
  // 이어 받기: 마지막으로 받은 이벤트 다음부터 (중복은 id로 한 번 더 걸러냄)
  const url = `/api/runs/${encodeURIComponent(runId)}/events` + (state.lastId >= 0 ? `?after=${state.lastId}` : "");
  const es = new EventSource(url); state.es = es;
  es.onmessage = (m) => { if (state.es !== es) return; let ev; try { ev = JSON.parse(m.data); } catch (e) { return; } onEvent(ev); };
  es.onopen = () => { if (state.reconnects) $("#conn-note").hidden = true; };
  es.onerror = () => {
    if (state.es !== es || state.ended) return;
    if (es.readyState === EventSource.CLOSED) reconnect();  // 브라우저가 스스로 다시 붙지 않는 끊김 -> 직접 복구
    else { $("#conn-note").hidden = false; $("#conn-note").textContent = "서버와의 연결이 잠시 끊겼습니다. 다시 연결하는 중입니다…"; }
  };
}
async function reconnect() {
  if (state.ended) return;
  state.reconnects++;
  $("#conn-note").hidden = false; $("#conn-note").textContent = "서버와의 연결이 끊겼습니다. 실행 상태를 확인하고 다시 연결하는 중입니다…";
  if (state.reconnects > 8) { $("#conn-note").textContent = "서버에 다시 연결하지 못했습니다. 잠시 후 새로고침하면 진행 상황을 이어서 볼 수 있습니다."; endLocal("연결 끊김", "bad"); return; }
  await new Promise(res => setTimeout(res, Math.min(1000 * state.reconnects, 5000)));
  let r = null; try { r = await fetch(`/api/runs/${encodeURIComponent(state.runId)}`); } catch (e) { r = null; }
  if (r && r.status === 404) {
    remember(null); $("#conn-note").textContent = "실행 기록을 찾을 수 없습니다(보관 기간이 지났거나 접근 권한이 없음).";
    endLocal("기록 없음", "bad"); return;
  }
  if (!r || !r.ok) { reconnect(); return; }
  openStream(state.runId);
}
function tick() {
  $("#elapsed").textContent = Math.round((Date.now() - state.t0) / 1000) + "초";
  const quiet = state.running && !state.waiting && !state.ended && Date.now() - state.lastEventAt > LONG_WAIT_MS;
  $("#wait-note").hidden = !quiet;
}
async function cancelRun() {
  if (!state.runId || state.ended) return;
  $("#cancel").disabled = true;
  let r = null; try { r = await post(`/api/runs/${encodeURIComponent(state.runId)}/cancel`); } catch (e) { r = null; }
  if (r && r.ok) { setRunState("취소 중", "warn"); stageNote("취소 요청을 보냈습니다. 진행 중인 단계가 끝나면 멈춥니다."); }
  else { $("#cancel").disabled = false; stageNote("취소 요청이 실패했습니다: " + (r ? await detailOf(r) : "서버에 연결하지 못했습니다.")); }
}
async function checkActive() {
  // 탭을 닫으면 실행 ID(sessionStorage)가 사라짐 -> 같은 브라우저의 진행 중 실행만 서버에 물어 이어 보기 버튼을 보여 줌
  let d = null; try { const r = await fetch("/api/runs/active"); if (r.ok) d = await r.json(); } catch (e) { d = null; }
  state.activeId = d && typeof d.run_id === "string" ? d.run_id : null;
  if (state.activeId && !state.running) $("#active-note").hidden = false;
}
async function resume(given) {
  const id = given || remembered(); if (!id) return;
  let r = null; try { r = await fetch(`/api/runs/${encodeURIComponent(id)}`); } catch (e) { r = null; }
  if (!r || !r.ok) { remember(null); $("#start-hint").textContent = "이전 분석에 접근할 수 없습니다(보관 기간이 지났거나 권한이 없음)."; return; }
  resetView();
  $("#input-error").textContent = "새로고침한 화면입니다. 보안상 입력 내용은 브라우저에 저장하지 않아 다시 표시하지 않습니다.";
  state.t0 = Date.now();
  connect(id, false);
}
function newRun() {
  if (state.es) { state.es.close(); state.es = null; }
  state.ended = true; state.running = false; remember(null); clearInterval(state.timer);
  resetView(); lockInput(false); $("#progress-panel").hidden = true; $("#elapsed").hidden = true; setRunState("대기 중", "");
  showPage("run"); $("#input-panel").scrollIntoView({ block: "start" }); (state.custom ? $("#custom") : $("#patient")).focus();
}
function showPage(name) {
  $("#page-run").hidden = name !== "run"; $("#page-result").hidden = name !== "result";
  document.querySelectorAll("#pages button").forEach(b => { b.classList.toggle("on", b.dataset.page === name);
    if (b.dataset.page === name) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current"); });
  window.scrollTo({ top: 0 });
}
function setRunState(t, c) { const el = $("#run-state"); el.textContent = t; el.className = "badge " + (c || ""); }
function resetView() {
  drawGraph(); drawSteps(); drawStages();
  Object.assign(state, { cards: {}, feedN: 0, lastId: -1, waiting: false, answered: false, questionsSeen: false, reconnects: 0, ended: false, submitting: false });
  $("#decision-body").innerHTML = ""; $("#decision-panel").hidden = !DIAGNOSTIC;
  showPage("run"); $('#pages [data-page="result"]').disabled = true;
  $("#feed").innerHTML = '<div class="empty">진행 기록이 여기에 표시됩니다.</div>'; $("#feed-count").textContent = "";
  $("#cards").innerHTML = '<div class="empty">아직 검토를 시작한 임상시험이 없습니다.</div>';
  $("#final").innerHTML = '<div class="empty">분석이 끝나면 결과가 표시됩니다.</div>'; $("#qslot").innerHTML = ""; $("#usage").textContent = "";
  ["#conn-note", "#end-note", "#wait-note"].forEach(s => { $(s).hidden = true; });
  $("#end-note").textContent = ""; $("#conn-note").textContent = "";
  stageNote("분석을 준비하고 있습니다.");
}
function endLocal(label, cls) {
  state.ended = true; state.running = false; state.waiting = false;
  if (state.es) state.es.close(); clearInterval(state.timer); $("#cancel").hidden = true; $("#wait-note").hidden = true;
  setRunState(label, cls); lockInput(false); clearEdges();
  document.querySelectorAll(".node.active").forEach(n => n.classList.remove("active"));
}

// ------------------------------------------------------------ 이벤트 처리 (서버 이벤트 순서대로, id로 중복 제거)
function onEvent(ev) {
  if (typeof ev.id === "number") { if (ev.id <= state.lastId) return; state.lastId = ev.id; }
  state.lastEventAt = Date.now(); $("#wait-note").hidden = true;
  if (state.reconnects) $("#conn-note").hidden = true;
  handle(ev);
}
function handle(ev) {
  if (ev.type === "status") {
    if (ev.cancelling) { setRunState("취소 중", "warn"); $("#qslot").innerHTML = ""; state.waiting = false; stageNote("취소 요청을 받았습니다. 진행 중인 단계가 끝나면 멈춥니다."); }
    if (ev.patient_id) { activate("prep", "분석을 준비하고 있습니다."); feed(0, "pipeline", `분석 대상 · 시험 ${esc((ev.trial_ids || []).length)}개`); }
    else if (ev.message) { feed(null, "pipeline", esc(ev.message)); if (!ev.cancelling) stageNote(ev.message); }
    return;
  }
  if (ev.type === "trace") { onTrace(ev.record || {}); return; }
  if (ev.type === "questions") { showQuestions(ev.questions || []); return; }
  if (ev.type === "answers") {
    $("#qslot").innerHTML = ""; state.waiting = false; state.answered = true; state.submitting = false;
    setStage("questions", "done"); activate("reanalysis", "답변을 반영해 환자 정보를 다시 분석하고 있습니다."); setRunState("분석 중", "accent");
    $("#progress-h").focus();
    return;
  }
  if (ev.type === "result") { showResult(ev); return; }
  if (ev.type === "decision_diagnostic") { showDecision(ev.diagnostic || {}); return; }
  if (ev.type === "error") { $("#end-note").hidden = false; $("#end-note").className = "note bad"; $("#end-note").textContent = ev.message || "오류가 발생했습니다."; return; }
  if (ev.type === "end") onEnd(ev.state);
}
function onEnd(s) {
  const END = { completed: ["완료", "ok", "분석이 끝났습니다. 추천 결과 화면에서 확인하세요."],
    cancelled: ["취소됨", "warn", "분석이 취소되었습니다. 결과는 만들어지지 않았습니다."],
    timed_out: ["시간 초과", "bad", "실행 시간 제한을 넘어 분석을 멈췄습니다. 결과는 만들어지지 않았습니다."],
    error: ["오류", "bad", "분석 중 오류가 발생해 결과를 만들지 못했습니다."],
    // 비용 통제(enforce)로 멈춘 경우: 중간 판정은 남기되 추천 결과로 보이지 않게
    budget_exceeded: ["비용 한도 도달", "warn", "비용 한도에 도달해 새 AI 호출을 멈췄습니다. 분석이 끝나지 않았으므로 추천 결과가 아닙니다. 아래 '검토 중인 임상시험'의 중간 판정은 참고용입니다."],
    usage_unknown: ["사용량 확인 불가", "warn", "AI 호출의 사용량을 확인할 수 없어 안전을 위해 분석을 멈췄습니다. 추천 결과가 아니며, 중간 판정은 참고용입니다."],
    budget_error: ["비용 관리 오류", "bad", "서버의 비용 관리 기능에 문제가 있어 분석을 멈췄습니다. 관리자에게 문의하세요."],
    analysis_disabled: ["실제 분석 꺼짐", "bad", "이 서버에서는 실제 AI 분석이 꺼져 있어 분석하지 않았습니다. 입력한 환자 정보는 외부로 보내지 않았습니다."] };
  const [t, c, msg] = END[s] || END.error;
  if (s === "completed") { finishActive(); setStage("done", "done"); }
  else for (const [k] of STAGES) if (state.stages[k] === "active") setStage(k, "failed");
  $("#qslot").innerHTML = "";
  const note = $("#end-note"); note.hidden = false; note.className = "note " + (c === "ok" ? "ok" : c === "warn" ? "warn" : "bad");
  if (s !== "error" || !note.textContent) note.textContent = msg;
  if (["budget_exceeded", "usage_unknown", "budget_error", "analysis_disabled"].includes(s)) $('#pages [data-page="result"]').disabled = true;  // 중단된 분석은 결과 화면 없음
  stageNote(msg);
  $("#conn-note").hidden = true;
  endLocal(t, c); remember(s === "completed" ? state.runId : null);
  if (s !== "completed" && !$("#page-run").hidden) note.focus();  // 키보드·화면 낭독 사용자가 종료 사유를 바로 들음
}
function onTrace(r) {
  const a = r.agent, e = r.event;
  if (a === "orchestrator") {
    nodeState("orchestrator", "active"); nodeState("input", "done");
    if (e === "plan") { feed(r.t, a, esc(r.message)); return; }
    if (e === "tool_call") {
      if (r.tool === "ask_clarifying_questions") activate("questions", "부족한 정보를 확인할 질문을 준비하고 있습니다.");
      feed(r.t, a, `도구 호출 <span class="chip">${esc(TOOL_KO[r.tool] || r.tool)}</span>`); return;
    }
    if (e === "tool_error") { feed(r.t, a, `도구 오류: ${esc(r.message)}`); return; }
    if (e === "search" || e === "fallback_finalize") feed(r.t, a, esc(r.message));
    return;
  }
  if (a === "pipeline") { if (e === "start") { setStage("prep", "done"); nodeState("input", "done"); step(0); } if (e === "error") feed(r.t, a, esc(r.message)); return; }
  if (a === "patient_profiler") {
    if (e === "start") { if (state.answered) activate("reanalysis", "답변을 반영해 환자 정보를 다시 분석하고 있습니다."); else activate("profile", "입력한 환자 정보에서 진단, 검사, 치료 이력 등을 정리하고 있습니다."); }
    else if (!state.answered) setStage("profile", "done");
    step(1, e === "done" ? "done" : undefined); nodeState(a, e === "start" ? "active" : "done");
    feed(r.t, a, e === "start" ? "환자 정보 정리 중" : "환자 정보 정리 완료");
    return;
  }
  if (a === "screener" || a === "criteria_parser" || a === "matcher") {
    if (e === "start") { if (state.answered) activate("reanalysis", "답변을 반영해 임상시험 조건을 다시 비교하고 있습니다."); else activate("criteria", "임상시험의 선정·제외 기준과 환자 정보를 비교하고 있습니다."); }
    if (a === "screener") { feed(r.t, a, e === "start" ? `전체 ${esc(r.pool)}개 시험에서 후보 추림 중` : `후보 ${esc((r.selected || []).length)}개 선택`); return; }
    if (a === "criteria_parser") { upsertCard(r.trial_id, { parsing: e === "start" }); feed(r.t, a, `${esc(r.trial_id)} 기준 정리 ${e === "start" ? "중" : "완료"}`); return; }
    nodeState(a, e === "start" ? "active" : "done"); step(2, e === "start" ? undefined : "done");
    if (e === "start") { upsertCard(r.trial_id, { matching: true }); feed(r.t, a, `${esc(r.trial_id)} 조건 비교 중`); }
    else {
      // 중간 판정 배지만 (요약 문구에는 내부 표기가 섞여 있어 보이지 않음)
      const elig = String(r.message || "").split(":")[0];
      upsertCard(r.trial_id, { matching: false, eligibility: elig });
      feed(r.t, a, `${esc(r.trial_id)} ${matchBadge(elig)}`);
    }
    return;
  }
  if (a === "question_generator") {
    const qs = r.questions || [];
    if (!qs.length) { setStage("questions", "skipped"); stageNote("추가로 확인할 질문이 없습니다. 결과를 정리합니다."); }
    else activate("questions", "질문을 준비했습니다.");
    step(3, "done"); step(4); nodeState(a, "done");
    feed(r.t, a, `라운드 ${esc(r.round)} · 질문 ${qs.length}개`);
    return;
  }
  if (a === "answerer") { nodeState("answerer", "done"); feed(r.t, a, `답변 ${(r.qa || []).length}개 반영`); return; }
  if (a === "recommender") {
    if (e === "start") {
      if (!state.questionsSeen && state.stages.questions !== "skipped") setStage("questions", "skipped");
      if (!state.answered) setStage("reanalysis", "skipped");
      activate("recommend", "비교 결과를 바탕으로 추천 결과를 정리하고 있습니다.");
    }
    step(5, e === "done" ? "done" : undefined); nodeState(a, e === "start" ? "active" : "done");
    feed(r.t, a, e === "start" ? `시험 ${esc(r.n_trials)}개 종합 중` : "추천 정리 완료");
    return;
  }
  if (a === "explainer") { nodeState(a, e === "start" ? "active" : "done"); feed(r.t, a, e === "start" ? "설명 보고서 작성 중" : "설명 보고서 작성 완료"); }
}
function upsertCard(tid, patch) {
  if (!tid) return;
  const c = (state.cards[tid] ||= { history: [] });
  if (patch.eligibility && c.eligibility !== patch.eligibility) c.history.push(patch.eligibility);
  Object.assign(c, patch);
  const t = trialMeta(tid);
  const status = c.parsing ? '<span class="badge accent">기준 정리 중</span>' : c.matching ? '<span class="badge accent">조건 비교 중</span>'
    : c.eligibility ? matchBadge(c.eligibility) : '<span class="badge">대기</span>';
  const html = `<div class="head"><span class="tid">${esc(tid)}</span>${statusBadge(t.overall_status)}${status}</div>
    <div class="ttl">${esc(t.title || "")}</div>${c.counts ? `<div class="sum">${esc(c.counts)}</div>` : ""}
    ${c.history.length > 1 ? `<div class="hist">판정 변화: ${c.history.map(h => matchBadge(h)).join(" → ")}</div>` : ""}`;
  let el = document.getElementById("card-" + tid);
  if (!el) { if ($("#cards .empty")) $("#cards").innerHTML = "";
    el = document.createElement("div"); el.className = "card"; el.id = "card-" + tid; $("#cards").appendChild(el); }
  el.innerHTML = html;
}
function showDecision(d) {
  $("#decision-panel").hidden = false;
  const acts = (d.actions || []).map(a => `<div class="mt4">• <span class="chip">${esc(a.action_type)}</span> ${esc(a.reason_code)}
    <span class="hint">${esc(a.human_readable_reason)} · ${esc(a.status)}</span></div>`).join("");
  const div = document.createElement("div"); div.className = "card mt6";
  div.innerHTML = `<b>라운드 ${esc(d.round)}</b> · ${esc(d.status)}${d.stop_reason ? ` · 종료 사유 ${esc(d.stop_reason)}` : ""}${d.error_type ? ` · 오류 ${esc(d.error_type)}` : ""}${acts}`;
  $("#decision-body").appendChild(div);
}

// ------------------------------------------------------------ 추가 질문 (서버 스키마: 답변은 문자열, 형식은 answer_type)
const FORMAT = {
  number: ["숫자로 입력하세요. 단위가 있으면 함께 적어도 됩니다 (예: 1.2 mg/dL, <5, 120~130).", "decimal"],
  date: ["날짜를 YYYY-MM-DD 형식으로 입력하세요. 연·월만 알면 YYYY-MM, 연도만 알면 YYYY.", "numeric"],
  free_text: ["", "text"],
};
// 형식만 확인 (의료적 범위·기준은 질문 스키마에 없으므로 만들지 않음)
const NUM_RE = /^\s*(?:[<>≤≥]=?\s*|약\s*)?[-+]?\d+(?:[.,]\d+)?(?:\s*[~-]\s*[-+]?\d+(?:[.,]\d+)?)?(?:\s*[^\d\s].*)?$/;
function validDate(v) {
  const m = /^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$/.exec(v.trim()); if (!m) return false;
  const [y, mo, d] = [+m[1], m[2] ? +m[2] : 1, m[3] ? +m[3] : 1];
  if (mo < 1 || mo > 12) return false;
  const dt = new Date(Date.UTC(y, mo - 1, d));
  return dt.getUTCFullYear() === y && dt.getUTCMonth() === mo - 1 && dt.getUTCDate() === d;
}
function formatError(type, v) {
  if (!v || DECLINED.has(v)) return "";
  if (type === "number" && !NUM_RE.test(v)) return "숫자로 입력하거나 \"모름 · 확인 불가\"를 선택하세요.";
  if (type === "date" && !validDate(v)) return "YYYY-MM-DD(또는 YYYY-MM, YYYY) 형식의 실제 날짜로 입력하거나 \"모름 · 확인 불가\"를 선택하세요.";
  return "";
}
function showQuestions(qs) {
  state.waiting = true; state.questionsSeen = true; state.submitting = false;
  activate("questions", qs.length ? `추가 질문 ${qs.length}개가 있습니다. 답해 주시면 다시 분석합니다.` : "질문이 없습니다.");
  setRunState("답변 대기", "warn");
  const field = (q, n) => {
    const id = esc(q.question_id);
    const desc = `qwhy-${id} qerr-${id}` + (FORMAT[q.answer_type] && FORMAT[q.answer_type][0] ? ` qfmt-${id}` : "");
    if (q.answer_type === "yes_no") return `<fieldset class="opts" aria-describedby="${desc}"><legend class="sr-only">질문 ${n} 답변</legend>
        <label><input type="radio" name="yn-${id}" value="예" data-yn="${id}"> 예</label><label><input type="radio" name="yn-${id}" value="아니요" data-yn="${id}"> 아니요</label></fieldset>`;
    const [fmt, mode] = FORMAT[q.answer_type] || FORMAT.free_text;
    return `<input type="text" data-q="${id}" data-type="${esc(q.answer_type)}" maxlength="${MAX_ANSWER}" inputmode="${mode}"
      aria-labelledby="qt-${id}" aria-describedby="${desc}" autocomplete="off">${fmt ? `<div class="fmt" id="qfmt-${id}">${esc(fmt)}</div>` : ""}`;
  };
  $("#qslot").innerHTML = `<div class="qbox" role="group" aria-labelledby="q-h"><h3 id="q-h" tabindex="-1">추가 질문 (${qs.length}개)</h3>
    <div class="hint mb10">판단에 부족한 정보입니다. 모르거나 확인할 수 없으면 "모름 · 확인 불가"를 고르세요(빈칸도 "모름"으로 보냅니다). 개인 식별 정보는 입력하지 마세요.</div>
    ${qs.map((q, i) => `<div class="q" data-qid="${esc(q.question_id)}"><p class="qt" id="qt-${esc(q.question_id)}">${i + 1}. ${esc(q.question)}
      <span class="badge ${q.purpose === "physician_reference" ? "warn" : "accent"}">${q.purpose === "physician_reference" ? "의료진 참고용" : "판단용"}</span></p>
      <div class="why" id="qwhy-${esc(q.question_id)}">${q.why ? "왜 묻나요? " + esc(q.why) : ""}</div>${field(q, i + 1)}
      <label class="unk"><input type="checkbox" data-unknown="${esc(q.question_id)}"> 모름 · 확인 불가</label>
      <div class="qerr" id="qerr-${esc(q.question_id)}" role="alert"></div></div>`).join("")}
    <div class="row mt12"><button class="btn" id="send" type="button">답변 확인</button>
      <button class="btn ghost" id="skip" type="button">모두 모름으로 보내기</button></div>
    <div id="qreview" hidden></div><div class="err mt8" id="qerror" role="alert"></div></div>`;
  $("#qslot").querySelectorAll("input[data-unknown]").forEach(cb => cb.addEventListener("change", () => {
    const box = cb.closest(".q"); box.querySelectorAll("input[data-q], input[data-yn]").forEach(i => { i.disabled = cb.checked; i.removeAttribute("aria-invalid"); });
    box.querySelector(".qerr").textContent = ""; $("#qreview").hidden = true;
  }));
  $("#qslot").querySelectorAll("input[data-q], input[data-yn]").forEach(i => i.addEventListener("input", () => {
    $("#qreview").hidden = true; i.removeAttribute("aria-invalid");
    const box = i.closest(".q"); box.querySelector(".qerr").textContent = "";
  }));
  $("#send").addEventListener("click", () => reviewAnswers(qs, false));
  $("#skip").addEventListener("click", () => reviewAnswers(qs, true));
  $("#qslot").scrollIntoView({ block: "start" });
  $("#q-h").focus();
}
function collectAnswers(qs, allUnknown) {
  const out = {}, errors = [];
  for (const q of qs) {
    const box = document.querySelector(`#qslot .q[data-qid="${CSS.escape(q.question_id)}"]`);
    const unknown = allUnknown || box.querySelector("input[data-unknown]").checked;
    let v = "";
    if (!unknown) {
      const t = box.querySelector("input[data-q]"); const yn = box.querySelector("input[data-yn]:checked");
      v = (t ? t.value : yn ? yn.value : "").trim();
      const err = t ? formatError(q.answer_type, v) : "";
      box.querySelector(".qerr").textContent = err;
      if (t) { if (err) t.setAttribute("aria-invalid", "true"); else t.removeAttribute("aria-invalid"); }
      if (err) errors.push(t);
    }
    out[q.question_id] = unknown || DECLINED.has(v) ? UNKNOWN_ANSWER : v.slice(0, MAX_ANSWER);
  }
  return [out, errors];
}
function reviewAnswers(qs, allUnknown) {
  if (state.submitting) return;
  const [answers, errors] = collectAnswers(qs, allUnknown);
  $("#qerror").textContent = "";
  if (errors.length) { $("#qreview").hidden = true; errors[0].focus(); $("#qerror").textContent = "형식이 맞지 않는 답변이 있습니다. 표시된 질문을 확인하세요."; return; }
  const rows = qs.map((q, i) => `<tr><th scope="row">${i + 1}. ${esc(q.question)}</th><td>${answers[q.question_id] === UNKNOWN_ANSWER ? '<span class="badge">모름 · 확인 불가</span>' : esc(answers[q.question_id])}</td></tr>`).join("");
  const box = $("#qreview"); box.hidden = false;
  box.innerHTML = `<div class="note info mt12" role="region" aria-labelledby="qreview-h"><b id="qreview-h">이 답변을 보낼까요?</b> 보낸 뒤에는 바꿀 수 없고, 답변을 반영해 다시 분석합니다.
    <table class="review"><caption class="sr-only">보낼 답변</caption>${rows}</table>
    <div class="row mt10"><button class="btn" id="confirm-send" type="button">이대로 보내기</button>
    <button class="btn ghost" id="edit-answers" type="button">답변 수정</button></div></div>`;
  $("#confirm-send").addEventListener("click", () => submitAnswers(answers));
  $("#edit-answers").addEventListener("click", () => { box.hidden = true; const f = $("#qslot input[data-q]:not(:disabled), #qslot input[data-yn]:not(:disabled)"); if (f) f.focus(); });
  $("#confirm-send").focus();
}
async function submitAnswers(answers) {
  if (state.submitting) return;  // 중복 제출 방지
  state.submitting = true;
  const btns = ["#confirm-send", "#edit-answers", "#send", "#skip"].map(s => $(s)).filter(Boolean);
  btns.forEach(b => { b.disabled = true; }); $("#confirm-send").textContent = "보내는 중…"; $("#qerror").textContent = "";
  let r = null; try { r = await post(`/api/runs/${encodeURIComponent(state.runId)}/answers`, { answers }); } catch (e) { r = null; }
  if (r && r.ok) { stageNote("답변을 보냈습니다. 답변을 반영해 다시 분석합니다."); return; }  // 화면 전환은 서버의 answers 이벤트로
  state.submitting = false;
  if (r && r.status === 409) { $("#qerror").textContent = "지금은 답변을 받을 수 없는 상태입니다(이미 보냈거나 분석이 끝남). 진행 상황을 확인하세요."; return; }
  btns.forEach(b => { b.disabled = false; }); $("#confirm-send").textContent = "다시 보내기";
  $("#qerror").textContent = "답변을 보내지 못했습니다. 입력한 답변은 그대로 있으니 다시 보내 주세요." + (r ? ` (${await detailOf(r)})` : "");
  $("#confirm-send").focus();
}

// ------------------------------------------------------------ 추천 결과 (서버 결과·표시 정보 필드만 사용, 판정은 바꾸지 않음)
const SOURCE = { record: "환자 기록", self_report: "답변(본인·보호자 보고)", unknown: "출처 확인 불가 · 검증된 기록 아님", none: "근거 없음" };
const GROUPS = [  // 서버 판정 함수로 분류한 기준 묶음 (web/display.py)
  ["met", "충족한 조건"], ["not_met", "충족하지 않은 조건"], ["missing", "정보가 부족한 조건"],
  ["record_check", "추가 확인 필요 · 진료 기록으로 확인"], ["physician_review", "추가 확인 필요 · 의료진 확인"],
  ["procedural", "연구기관이 확인하는 절차 조건 (판정에 영향 없음)"], ["unclassified", "추가 확인 필요 · 분류할 수 없는 조건"],
];
const EXTRA = ["record_check", "physician_review", "procedural", "unclassified"];
const SAID = { yes: "해당한다고 답함", no: "해당하지 않는다고 답함", not_applicable: "대상이 아니라고 답함", unclear: "답변이 분명하지 않음" };
// 원문 보존: 서버 문구는 지우거나 고치지 않고 접어서 보여 줌
function raw(text, title) {
  if (!text) return "";
  return `<details class="raw"><summary>${esc(title || "시스템 원문 보기")} <span class="hint">(내부 표기 포함)</span></summary><div class="rawtext">${esc(text)}</div></details>`;
}
// AI가 쓴 글: 검증된 사실이 아님을 표시하고, 내부 표기가 섞였으면 중립 안내 뒤 원문을 접어 둠
function aiText(label, text) {
  if (!text) return "";
  if (CODE_RE.test(text)) return `<div class="ai"><span class="lbl">${esc(label)} · 검증된 사실 아님</span>AI 설명에 시스템 내부 표기가 섞여 있어 접어 두었습니다. 위의 상태와 조건별 판정을 기준으로 확인하세요.${raw(text, "AI 원문 보기")}</div>`;
  return `<div class="ai"><span class="lbl">${esc(label)} · 검증된 사실 아님</span>${esc(text)}</div>`;
}
function aiList(label, items) {
  const list = (items || []).filter(Boolean); if (!list.length) return "";
  if (list.some(x => CODE_RE.test(x))) return aiText(label, list.join("\n"));
  return `<div class="ai"><span class="lbl">${esc(label)} · 검증된 사실 아님</span><ul>${list.map(x => `<li>${esc(x)}</li>`).join("")}</ul></div>`;
}
function critOutcome(a) {
  if (a.applicable === false) return "대상 아님(통과)";
  if (a.passes === true) return a.kind === "exclusion" ? "제외 기준에 해당하지 않음" : "충족";
  if (a.passes === false) return a.kind === "exclusion" ? "제외 기준에 해당함" : "충족하지 않음";
  return "판정 보류";
}
function critItem(a, g) {
  const ev = a.evidence && !/^근거 없음/.test(a.evidence) ? `<div class="ev">근거: <q>${esc(a.evidence)}</q></div>` : "";
  const extra = [];
  if (g === "record_check") extra.push("답변만으로 판정하지 않았습니다. 검사 결과지·진료 기록으로 확인이 필요합니다.");
  if (g === "physician_review") extra.push("원문에 구체적인 기준이 없어 의료진이 전체 프로토콜과 대조해 확인해야 합니다.");
  if (g === "procedural") extra.push("동의서 등 연구기관에서 확인하는 절차 조건으로, 이 결과의 판정에는 영향을 주지 않습니다.");
  if (g === "unclassified") extra.push("판정이 보류되었으나 자세한 분류를 할 수 없습니다. 의료진 확인이 필요합니다.");
  if (g === "missing" && a.missing_info) extra.push("필요한 정보: " + a.missing_info);
  return `<div class="crit ${g}"><div class="ct">${esc(a.criterion_text)}</div>
    <div class="meta"><span class="badge">${a.kind === "exclusion" ? "제외 기준" : "선정 기준"}</span><span class="badge">${esc(critOutcome(a))}</span>
      <span class="badge wrap">${a.method === "rule_engine" ? "규칙 엔진 판정" : "AI 판정 · 검증된 사실 아님"}</span>
      <span class="badge wrap">근거 출처: ${esc(SOURCE[a.evidence_source] || "근거 없음")}</span>${a.cohort ? `<span class="badge">코호트 ${esc(a.cohort)}</span>` : ""}</div>
    ${ev}${extra.map(x => `<div class="ev">${esc(x)}</div>`).join("")}</div>`;
}
function groupOf(d, a) {
  const g = d && d.groups ? d.groups[a.rule_id] : null;
  if (g && GROUPS.some(([k]) => k === g)) return g;
  if (a.passes === true) return "met";
  if (a.passes === false) return "not_met";
  return "unclassified";  // 표시 정보가 없으면 판정 보류 항목은 중립 분류
}
function counts(m, d) {
  if (!m || m.eligibility === "NOT_EVALUATED") return "";
  const c = { met: 0, not_met: 0, missing: 0, extra: 0 };
  for (const a of m.assessments || []) { const g = groupOf(d, a); if (EXTRA.includes(g)) c.extra++; else c[g]++; }
  return `<div class="counts" aria-label="조건 수"><span class="badge ok">충족 ${c.met}</span><span class="badge bad">불충족 ${c.not_met}</span>
    <span class="badge">정보 부족 ${c.missing}</span><span class="badge warn">추가 확인 ${c.extra}</span></div>`;
}
function criteriaDetails(m, d) {
  if (!m) return "";
  if (m.eligibility === "NOT_EVALUATED") {
    const kind = d && d.error_type ? d.error_type : "";
    return `<div class="note warn mt8">평가 중 오류로 판정하지 못했습니다${kind ? ` (오류 종류: ${esc(kind)})` : ""}. 부적격이라는 뜻이 아니며, 오류 상세는 서버 기록에만 남깁니다.</div>`;
  }
  const as = m.assessments || [];
  const body = GROUPS.map(([g, t]) => [g, t, as.filter(a => groupOf(d, a) === g)]).filter(x => x[2].length).map(([g, t, list]) =>
    `<div class="crit-group"><h4>${esc(t)} (${list.length})</h4>${list.map(a => critItem(a, g)).join("")}</div>`).join("");
  return `<details class="more mt8"><summary>조건별 판정과 근거 보기 (${as.length}개 조건${m.matched_cohort ? ` · 코호트 ${esc(m.matched_cohort)}` : ""})</summary>${body || '<div class="empty">표시할 조건이 없습니다.</div>'}</details>`;
}
function checksBlock(r, d) {
  let html = "";
  const rc = (d && d.record_checks) || [];
  if (rc.length) html += `<div class="phys"><b>진료 기록 확인 필요 (${rc.length})</b>${rc.map(x => `<div class="item">• ${esc(x.criterion_text)}
      <div class="hint">${esc(SAID[x.said] || SAID.unclear)}. 이 조건은 진료 기록이 필요해 답변만으로 판정하지 않았습니다.</div>
      ${x.evidence && !/^근거 없음/.test(x.evidence) ? `<div class="hint">근거: <q>${esc(x.evidence)}</q></div>` : ""}</div>`).join("")}</div>`;
  const ph = (d && d.physician_items) || [];
  if (ph.length) html += `<div class="phys"><b>의사 확인 필요 (${ph.length})</b>${ph.map(x => `<div class="item">• ${esc(x.criterion_text)}
      <div class="hint">공개된 프로토콜 원문에 구체 기준이 없어 시스템이 판정하지 않았습니다. 참여 결정 전 담당 의사가 전체 프로토콜과 대조해 확인하세요.</div>
      ${(x.related_qa || []).map(q => `<div class="hint">관련 문답: ${esc(q.question)} → ${esc(q.answer)}</div>`).join("")}</div>`).join("")}</div>`;
  const notes = [...(r.verification_needed || []), ...(r.physician_review || [])];
  if (notes.length && (rc.length || ph.length)) html += raw(notes.join("\n"), "시스템 메모 원문 보기");
  else if (notes.length) html += `<div class="phys"><b>확인이 필요한 항목이 있습니다 (${notes.length})</b> 자세한 분류를 받지 못해 원문을 접어 두었습니다.${raw(notes.join("\n"), "시스템 메모 원문 보기")}</div>`;
  return html;
}
function trialHead(tid, status, info, rank) {
  const url = safeTrialUrl(info.url);
  return `<div class="head">${rank ? `<span class="rank" aria-label="표시 순서 ${esc(rank)}">${esc(rank)}</span>` : ""}<span class="tid">${esc(tid)}</span>${matchBadge(status)}${statusBadge(info.overall_status)}
    ${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">ClinicalTrials.gov에서 원문 보기<span class="sr-only"> (${esc(tid)}, 새 창)</span><span class="hint" aria-hidden="true"> (새 창)</span></a>` : ""}</div>
    <div class="ttl">${esc(info.title || "")}</div><div class="hint mt4">${esc((STATUS[status] || [])[2] || "")}</div>`;
}
const EXCLUDE_TEXT = {
  not_evaluated: () => "평가 중 오류로 판정하지 못했습니다(부적격 아님). 다시 분석하거나 담당자 확인이 필요합니다.",
  closed: (d) => `모집이 끝난 시험이라 현재 참여할 수 없습니다 (모집 상태: ${recruitText(d.recruitment_status)}).`,
  demo: () => "실제 임상시험이 아닌 가상 데모 시험이라 추천에서 제외했습니다.",
  mismatch: () => "입력된 정보로 보면 맞지 않는 조건이 있어 추천에서 제외했습니다.",
  unknown: () => "추천에서 제외되었습니다. 자세한 내용은 조건별 판정을 확인하세요.",
};
function excludedCard(x, m, d, info) {
  const kind = d && d.exclusion && EXCLUDE_TEXT[d.exclusion] ? d.exclusion : "unknown";
  let html = `<div class="card mt8">${trialHead(x.trial_id, m ? m.match_status : null, info)}<div class="sum">${esc(EXCLUDE_TEXT[kind](d || {}))}</div>`;
  if (kind === "mismatch" && m) {
    const bad = (m.assessments || []).filter(a => groupOf(d, a) === "not_met");
    if (bad.length) html += `<div class="sum">맞지 않는 조건:</div><ul>${bad.slice(0, 3).map(a => `<li>${esc(a.criterion_text)}</li>`).join("")}</ul>${bad.length > 3 ? `<div class="hint">외 ${bad.length - 3}개는 조건별 판정에서 확인하세요.</div>` : ""}`;
  }
  html += counts(m, d) + criteriaDetails(m, d);
  if (kind !== "not_evaluated") html += raw(x.reason, "시스템 원문 사유 보기");  // 평가 오류 원문은 보이지 않음 (요청 내용이 섞일 수 있음)
  return html + "</div>";
}
function noRecommendation(rec, display) {
  const kinds = (display && display.no_recommendation) || null;
  let html = `<div class="callout">현재 추천할 수 있는 임상시험이 없습니다.`;
  if (kinds) {
    const label = { mismatch: "모집 중이지만 맞지 않는 조건이 있는 시험", closed: "모집이 끝난 시험", demo: "가상 데모 시험(실제 시험 아님)",
      not_evaluated: "평가에 실패한 시험(부적격 아님)", unknown: "그 밖의 이유로 제외된 시험" };
    const items = Object.entries(kinds).filter(([k, v]) => k !== "null" && v.length).map(([k, v]) => `<li>${esc(label[k] || label.unknown)} ${v.length}개</li>`);
    if (items.length) html += `<ul>${items.join("")}</ul>`;
  }
  html += `</div>` + raw(rec.no_recommendation_reason, "시스템 원문 사유 보기");
  return html;
}
// marked 12 렌더러: 원시 HTML 블록·인라인 HTML은 이스케이프한 글자로 출력
const REPORT_RENDERER = window.marked ? Object.assign(new marked.Renderer(), { html: (html) => esc(html) }) : null;
function showResult(ev) {
  const result = ev.result || {}; const rec = result.recommendation || { ranked: [], excluded: [] };
  const matches = result.matches || {}; const trials = ev.trials || {};
  const display = ev.display || null; const dOf = (tid) => (display && display.trials && display.trials[tid]) || null;
  for (const [tid, m] of Object.entries(matches)) {
    const d = dOf(tid); let c = "";
    if (m.eligibility !== "NOT_EVALUATED") {
      const n = { met: 0, not_met: 0, missing: 0, extra: 0 };
      for (const a of m.assessments || []) { const g = groupOf(d, a); if (EXTRA.includes(g)) n.extra++; else n[g]++; }
      c = `충족 ${n.met} · 불충족 ${n.not_met} · 정보 부족 ${n.missing} · 추가 확인 ${n.extra}`;
    }
    upsertCard(tid, { eligibility: m.match_status || m.eligibility, parsing: false, matching: false, counts: c });
  }
  const u = result.usage || {};
  $("#usage").textContent = u.calls ? `AI 호출 ${u.calls}회` : "";
  let html = `<div class="note info">${esc(ev.match_note || "")} <b>이 결과는 임상시험 참여 자격을 확정하지 않습니다.</b></div>`;
  html += `<details class="more"><summary>상태 설명 보기</summary><div class="legend">${Object.entries(STATUS).map(([, [t, c, d]]) =>
    `<span class="badge ${c}">${esc(t)}</span><span>${esc(d)}</span>`).join("")}</div></details>`;
  if (!display) html += `<div class="note warn">결과 분류 정보를 받지 못해 일부 조건을 "분류할 수 없는 조건"으로 표시합니다. 판정 상태는 서버 결과 그대로입니다.</div>`;
  if (rec.no_recommendation_reason) html += noRecommendation(rec, display);
  const recCard = (r, top) => {
    const info = trials[r.trial_id] || {}; const d = dOf(r.trial_id);
    return `<div class="rec${top ? " top" : ""}">${trialHead(r.trial_id, r.match_status || r.eligibility, info, r.rank)}${counts(matches[r.trial_id], d)}
      ${aiText("AI 요약", r.rationale)}${aiList("AI가 정리한 확인할 점", r.key_concerns)}${aiList("AI가 제안한 다음 단계", r.next_steps)}
      ${checksBlock(r, d)}${criteriaDetails(matches[r.trial_id], d)}</div>`;
  };
  const top = rec.recommended || [];
  if (top.length) {
    html += `<h3 class="h3 big">검토해 볼 임상시험 <span class="hint">${matchBadge(top[0].match_status)} ${top.length}개</span></h3>`;
    html += top.map(r => recCard(r, true)).join("");
    if (rec.selection_reason) html += `<div class="hint">${CODE_RE.test(rec.selection_reason) ? "표시 순서 기준은 아래 원문을 확인하세요." + raw(rec.selection_reason) : esc(rec.selection_reason)}</div>`;
  }
  const others = (rec.ranked || []).filter(r => !top.some(t => t.trial_id === r.trial_id));
  if (others.length) html += `<details class="more"><summary>다른 후보 ${others.length}개 (추가 확인이 필요한 시험)</summary><div class="mt8">${others.map(r => recCard(r, false)).join("")}</div></details>`;
  const shown = new Set([...top, ...others].map(r => r.trial_id));
  const excluded = rec.excluded || [];
  if (excluded.length) html += `<details class="more"><summary>추천에서 제외된 시험 ${excluded.length}개</summary>${excluded.map(x => {
    shown.add(x.trial_id); return excludedCard(x, matches[x.trial_id], dOf(x.trial_id), trials[x.trial_id] || {}); }).join("")}</details>`;
  const rest = Object.entries(matches).filter(([tid]) => !shown.has(tid));
  if (rest.length) html += `<details class="more"><summary>그 밖에 평가한 시험 ${rest.length}개</summary>${rest.map(([tid, m]) =>
    `<div class="card mt8">${trialHead(tid, m.match_status, trials[tid] || {})}${counts(m, dOf(tid))}${criteriaDetails(m, dOf(tid))}</div>`).join("")}</details>`;
  if (!top.length && !others.length && !rec.no_recommendation_reason) html += '<div class="empty">추천할 임상시험이 없습니다.</div>';
  if (rec.overall_comment && rec.overall_comment !== rec.no_recommendation_reason) html += aiText("AI 종합 설명", rec.overall_comment);
  if (ev.report) {
    // AI 보고서 마크다운: 안에 섞인 HTML은 해석하지 않고 글자로 (1차), 그 결과도 DOMPurify로 정화 (2차, style 제거)
    const md = window.marked && window.DOMPurify
      ? DOMPurify.sanitize(marked.parse(ev.report, { renderer: REPORT_RENDERER }), { FORBID_TAGS: ["style"], FORBID_ATTR: ["style"] })
      : `<pre class="rawtext">${esc(ev.report)}</pre>`;
    html += `<details class="report"><summary>AI 설명 보고서 (검증된 사실 아님 · 내부 표기가 포함될 수 있음)</summary><div class="md">${md}</div></details>`;
  }
  $("#final").innerHTML = html;
  finishActive(); setStage("recommend", "done");
  $('#pages [data-page="result"]').disabled = false; showPage("result");
  $("#result-h").focus();
}

init();
