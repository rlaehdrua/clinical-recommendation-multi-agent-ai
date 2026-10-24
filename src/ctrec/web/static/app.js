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
  CRITERIA_MATCH: ["공개 기준 충족", "ok", "공개 기준 모두 충족 · 참여 확정 아님"],
  POTENTIAL_MATCH: ["기록 확인 시 충족 가능", "warn", "답변상 충족 · 진료 기록 확인 필요"],
  NEEDS_REVIEW: ["의료진 검토 필요", "warn", "의료진이 프로토콜과 대조할 조건 있음"],
  INSUFFICIENT_INFORMATION: ["추가 정보 필요", "warn", "확인 못 한 정보 있음 · 불충족 아님"],
  CRITERIA_MISMATCH: ["공개 기준 불충족", "bad", "맞지 않는 조건 있음"],
  NOT_EVALUATED: ["평가 실패", "warn", "평가 오류 · 부적격 아님"],
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
    try { state.ko = await (await fetch("/api/ko")).json(); } catch (e) { state.ko = {}; }  // 번역이 없어도 원문으로 동작
  } catch (e) { $("#start-hint").textContent = "서버에 연결하지 못했습니다. 잠시 후 새로고침하세요."; return; }
  const groups = {};
  for (const p of state.patients) (groups[p.group] ||= []).push(p);
  $("#patient").innerHTML = Object.entries(groups).map(([g, ps]) =>
    `<optgroup label="${esc(g)} (가상 데이터)">${ps.map(p => `<option value="${esc(p.patient_id)}">${esc(p.patient_id)} · ${esc(caseLabel(p))}</option>`).join("")}</optgroup>`).join("");
  $("#trials").innerHTML = state.trials.map(t => `<label class="trial-opt"><input type="checkbox" value="${esc(t.trial_id)}">
    <span><span class="tid">${esc(t.trial_id)}</span> ${statusBadge(t.overall_status)}${t.synthetic ? ' <span class="badge">가상 시험</span>' : ""}
    <span class="ttl"${koTitle(t.trial_id, t.title) !== t.title ? ` title="${esc(t.title)}"` : ""}>${esc(koTitle(t.trial_id, t.title))}</span></span></label>`).join("");
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
  ["#final", "#detail"].forEach(s => $(s).addEventListener("click", onResultClick));
  $("#detail-back").addEventListener("click", () => showPage("result")); $("#summary-back").addEventListener("click", () => showPage("result"));
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
  $("#custom").value = shownText(p); onTextInput(); $("#custom").focus();  // 직접 입력에 넣으면 보이는 한국어 그대로 분석
}
// 예시 사례: 핵심 요약(항목: 내용)을 먼저 보여 주고, 전체 번역·원문은 접어 둠 (표시 전용, data/i18n/patients_ko.json).
// 사례를 골라 분석하면 서버는 원문을 씀
const shownText = (p) => p.text_ko || p.text;
function caseLabel(p) {
  const row = (k) => ((p.summary_ko || []).find(([l]) => l === k) || [])[1];
  const who = row("나이·성별"), what = row("진단") || row("내원 사유");
  return who ? [who, what].filter(Boolean).join(" · ").slice(0, 46) : shownText(p).slice(0, 40) + "…";
}
function caseHtml(p) {
  if (!p || !p.text) return "";
  const rows = p.summary_ko || [];
  const fold = (title, text, en) => `<details class="raw"><summary>${esc(title)}</summary><div class="rawtext"${en ? ' lang="en"' : ""}>${esc(text)}</div></details>`;
  if (!rows.length) return `<div class="preview">${esc(shownText(p))}</div>` + (p.text_ko ? fold("영어 원문 보기 · 분석은 원문으로 해요", p.text, true) : "");
  return `<div class="kv case-kv">${rows.map(([k, v]) => `<div><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("")}</div>`
    + (p.text_ko ? fold("전체 내용 보기 (한국어 번역)", p.text_ko) + fold("영어 원문 보기 · 분석은 원문으로 해요", p.text, true)
      : fold("원문 보기 · 분석은 원문으로 해요", p.text));
}
function onPatient() {
  const p = state.patients.find(x => x.patient_id === $("#patient").value); if (!p) return;
  $("#preview").innerHTML = caseHtml(p);
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
  const pick = state.patients.find(p => p.patient_id === body.patient_id) || {};
  const text = body.custom_text ?? shownText(pick) ?? "";
  $("#review-meta").textContent = `${state.custom ? "직접 입력" : "예시 사례 " + body.patient_id} · ${text.length.toLocaleString()}자 · `
    + (body.screen ? "전체 시험에서 자동 추림" : `직접 선택한 시험 ${body.trial_ids.length}개`) + (body.allow_search ? " · 추가 검색 허용" : "");
  if (body.custom_text == null) { $("#review-text").className = "case"; $("#review-text").innerHTML = caseHtml(pick); }
  else { $("#review-text").className = "preview"; $("#review-text").textContent = text.length > 600 ? text.slice(0, 600) + " …" : text; }
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
  showInput(false);  // 분석 중에는 진행·질문 화면만 (시안: 한 화면에 한 가지 일)
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
function showInput(on) { $("#intro").hidden = !on; $("#input-panel").hidden = !on; }
function newRun() {
  if (state.es) { state.es.close(); state.es = null; }
  state.ended = true; state.running = false; remember(null); clearInterval(state.timer);
  resetView(); lockInput(false); showInput(true); $("#progress-panel").hidden = true; $("#elapsed").hidden = true; setRunState("대기 중", "");
  showPage("run"); $("#input-panel").scrollIntoView({ block: "start" }); (state.custom ? $("#custom") : $("#patient")).focus();
}
// 화면: 입력·진행(run) → 결과 목록(result) → 상세(detail) / 의사용 요약(summary). 상세·요약은 결과 탭에 속함
const PAGES = ["run", "result", "detail", "summary"];
function showPage(name) {
  for (const p of PAGES) $("#page-" + p).hidden = p !== name;
  const tab = name === "run" ? "run" : "result";
  document.querySelectorAll("#pages button").forEach(b => { b.classList.toggle("on", b.dataset.page === tab);
    if (b.dataset.page === tab) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current"); });
  window.scrollTo({ top: 0 });
}
function onResultClick(e) {
  const t = e.target.closest("[data-detail], [data-summary]"); if (!t) return;
  if (t.dataset.detail) renderDetail(t.dataset.detail); else renderSummary();
}
function setRunState(t, c) { const el = $("#run-state"); el.textContent = t; el.className = "badge " + (c || ""); }
function resetView() {
  drawGraph(); drawSteps(); drawStages();
  Object.assign(state, { cards: {}, feedN: 0, lastId: -1, waiting: false, answered: false, questionsSeen: false, reconnects: 0, ended: false, submitting: false,
    sent: null, screen: null });
  $("#decision-body").innerHTML = ""; $("#decision-panel").hidden = !DIAGNOSTIC;
  showPage("run"); $('#pages [data-page="result"]').disabled = true;
  $("#feed").innerHTML = '<div class="empty">진행 기록이 여기에 표시됩니다.</div>'; $("#feed-count").textContent = "";
  $("#cards").innerHTML = '<div class="empty">아직 검토를 시작한 임상시험이 없습니다.</div>';
  $("#final").innerHTML = '<div class="empty">분석이 끝나면 결과가 표시됩니다.</div>'; $("#qslot").innerHTML = ""; $("#usage").textContent = "";
  $("#detail").innerHTML = ""; $("#summary").innerHTML = ""; state.res = null; $("#progress-panel").classList.remove("asking");
  ["#conn-note", "#end-note", "#wait-note"].forEach(s => { $(s).hidden = true; });
  $("#end-note").textContent = ""; $("#conn-note").textContent = "";
  stageNote("분석 준비 중");
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
    if (ev.cancelling) { setRunState("취소 중", "warn"); $("#qslot").innerHTML = ""; $("#progress-panel").classList.remove("asking"); state.waiting = false; stageNote("취소 요청을 받았습니다. 진행 중인 단계가 끝나면 멈춥니다."); }
    if (ev.patient_id) { activate("prep", "분석 준비 중"); feed(0, "pipeline", `분석 대상 · 시험 ${esc((ev.trial_ids || []).length)}개`); }
    else if (ev.message) { feed(null, "pipeline", esc(ev.message)); if (!ev.cancelling) stageNote(ev.message); }
    return;
  }
  if (ev.type === "trace") { onTrace(ev.record || {}); return; }
  if (ev.type === "questions") { showQuestions(ev.questions || []); return; }
  if (ev.type === "answers") {
    $("#qslot").innerHTML = sentSummary(); $("#progress-panel").classList.remove("asking"); state.waiting = false; state.answered = true; state.submitting = false;
    setStage("questions", "done"); activate("reanalysis", "답변 반영 중 · 환자 정보 다시 정리"); setRunState("분석 중", "accent");
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
  if (state.waiting || !state.answered) $("#qslot").innerHTML = "";  // 답하지 못한 질문은 닫고, 보낸 답변 요약은 남김
  $("#progress-panel").classList.remove("asking");
  const sent = $("#sent-state"); if (sent && state.sent) sent.textContent = `보낸 답변 ${state.sent.qs.length}개` + (s === "completed" ? " · 반영 완료" : "");
  const note = $("#end-note"); note.hidden = false; note.className = "note " + (c === "ok" ? "ok" : c === "warn" ? "warn" : "bad");
  if (s !== "error" || !note.textContent) note.textContent = msg;
  if (["budget_exceeded", "usage_unknown", "budget_error", "analysis_disabled"].includes(s)) $('#pages [data-page="result"]').disabled = true;  // 중단된 분석은 결과 화면 없음
  stageNote(msg);
  $("#conn-note").hidden = true;
  endLocal(t, c); remember(s === "completed" ? state.runId : null);
  if (s !== "completed") showInput(true);  // 끝나지 못한 분석은 입력을 고쳐 다시 시작할 수 있게
  if (s !== "completed" && !$("#page-run").hidden) note.focus();  // 키보드·화면 낭독 사용자가 종료 사유를 바로 들음
}
function onTrace(r) {
  const a = r.agent, e = r.event;
  if (a === "orchestrator") {
    nodeState("orchestrator", "active"); nodeState("input", "done");
    if (e === "plan") { feed(r.t, a, esc(r.message)); return; }
    if (e === "tool_call") {
      if (r.tool === "ask_clarifying_questions") activate("questions", "질문 준비 중");
      feed(r.t, a, `도구 호출 <span class="chip">${esc(TOOL_KO[r.tool] || r.tool)}</span>`); return;
    }
    if (e === "tool_error") { feed(r.t, a, `도구 오류: ${esc(r.message)}`); return; }
    if (e === "search" || e === "fallback_finalize") feed(r.t, a, esc(r.message));
    return;
  }
  if (a === "pipeline") { if (e === "start") { setStage("prep", "done"); nodeState("input", "done"); step(0); } if (e === "error") feed(r.t, a, esc(r.message)); return; }
  if (a === "patient_profiler") {
    if (e === "start") { if (state.answered) activate("reanalysis", "답변 반영 중 · 환자 정보 다시 정리"); else activate("profile", "환자 정보 정리 중 · 진단·검사·치료 이력"); }
    else if (!state.answered) setStage("profile", "done");
    step(1, e === "done" ? "done" : undefined); nodeState(a, e === "start" ? "active" : "done");
    feed(r.t, a, e === "start" ? "환자 정보 정리 중" : "환자 정보 정리 완료");
    return;
  }
  if (a === "screener" || a === "criteria_parser" || a === "matcher") {
    if (e === "start") { if (state.answered) activate("reanalysis", "답변 반영 중 · 기준 다시 비교"); else activate("criteria", "선정·제외 기준 비교 중"); }
    if (a === "screener") {
      if (e === "start") { state.screen = { pool: r.pool }; stageNote(`후보 고르는 중 · 전체 ${r.pool}개`); }
      else { state.screen = { ...state.screen, selected: (r.selected || []).length }; stageNote(`후보 ${state.screen.selected}개 선택 · 전체 ${state.screen.pool ?? "-"}개 중`); }
      feed(r.t, a, e === "start" ? `전체 ${esc(r.pool)}개 시험에서 후보 추림 중` : `후보 ${esc((r.selected || []).length)}개 선택`); return;
    }
    if (a === "criteria_parser") { upsertCard(r.trial_id, { parsing: e === "start" }); feed(r.t, a, `${esc(r.trial_id)} 기준 정리 ${e === "start" ? "중" : "완료"}`); return; }
    nodeState(a, e === "start" ? "active" : "done"); step(2, e === "start" ? undefined : "done");
    if (e === "start") { upsertCard(r.trial_id, { matching: true }); feed(r.t, a, `${esc(r.trial_id)} 조건 비교 중`); }
    else {
      // 판정 배지와 조건 수만 (요약 문구에는 내부 표기가 섞여 있어 그대로 보이지 않음)
      const snap = roundSnap(r);
      upsertCard(r.trial_id, { matching: false, snap });
      feed(r.t, a, `${esc(r.trial_id)} ${matchBadge(snap.status)}`);
    }
    return;
  }
  if (a === "question_generator") {
    const qs = r.questions || [];
    if (!qs.length) { setStage("questions", "skipped"); stageNote("추가 질문 없음 · 결과 정리 중"); }
    else activate("questions", "질문 준비 완료");
    step(3, "done"); step(4); nodeState(a, "done");
    feed(r.t, a, `라운드 ${esc(r.round)} · 질문 ${qs.length}개`);
    return;
  }
  if (a === "answerer") { nodeState("answerer", "done"); feed(r.t, a, `답변 ${(r.qa || []).length}개 반영`); return; }
  if (a === "recommender") {
    if (e === "start") {
      if (!state.questionsSeen && state.stages.questions !== "skipped") setStage("questions", "skipped");
      if (!state.answered) setStage("reanalysis", "skipped");
      activate("recommend", "추천 결과 정리 중");
      const sent = $("#sent-state"); if (sent && state.sent) sent.textContent = `보낸 답변 ${state.sent.qs.length}개 · 반영 완료`;
    }
    step(5, e === "done" ? "done" : undefined); nodeState(a, e === "start" ? "active" : "done");
    feed(r.t, a, e === "start" ? `시험 ${esc(r.n_trials)}개 종합 중` : "추천 정리 완료");
    return;
  }
  if (a === "explainer") { nodeState(a, e === "start" ? "active" : "done"); feed(r.t, a, e === "start" ? "설명 보고서 작성 중" : "설명 보고서 작성 완료"); }
}
// 가상 데모 시험은 실제 시험과 섞여 보이지 않게 모든 카드에 표시 (/api/trials의 synthetic)
function demoBadge(tid) { return trialMeta(tid).synthetic ? '<span class="badge demo">가상 시험 · 실제 모집 아님</span>' : ""; }
// 매칭 완료 기록의 조건 수: 서버 요약 문구에서 "정보 부족"과 "진료 기록 확인 필요"를 나눠 읽고, 못 읽으면 숫자 필드만 씀
function roundSnap(r) {
  const m = /통과 (\d+) \/ 불충족 (\d+) \/ 판정불가 (\d+)(?: \(\+[^)]*?(\d+)\))?/.exec(String(r.message || ""));
  const [pass, fail, missing, verify] = m ? [+m[1], +m[2], +m[3], +(m[4] || 0)] : [r.n_pass, r.n_fail, r.n_unknown, null];
  return { round: Number(r.round) || 0, status: r.match_status || String(r.message || "").split(":")[0], pass, fail, missing, verify };
}
function snapText(s) {
  return `충족 ${s.pass ?? "-"} · 불충족 ${s.fail ?? "-"} · 정보 부족 ${s.missing ?? "-"}` + (s.verify != null ? ` · 추가 확인 ${s.verify}` : "");
}
// 답변 반영 전후 비교: 줄글 대신 "항목  전 → 후" 숫자 줄 (같은 라운드를 다시 평가하면 마지막 값으로 덮음)
// better: 늘면 좋은 항목("up", 충족)·줄면 좋은 항목("down", 정보 부족·불충족)·좋고 나쁨이 없는 항목("none")
function kvChange(label, from, to, better) {
  if (from == null || to == null) return "";
  const improved = better === "up" ? to > from : better === "down" ? to < from : null;
  const cls = from === to || improved === null ? "" : improved ? "good" : "worse";
  return `<div><span>${esc(label)}</span><b class="${cls}">${from === to ? `${to}개` : `<span class="from">${from}개</span><span class="arr">→</span>${to}개`}</b></div>`;
}
function compareBlock(c) {
  const rs = Object.keys(c.rounds || {}).map(Number).sort((a, b) => a - b).map(k => c.rounds[k]);
  if (rs.length < 2) return "";
  const [a, b] = [rs[0], rs[rs.length - 1]];
  return `<div class="cmp" role="group" aria-label="답변 반영 전후 비교"><div class="cap">답변 반영 결과</div><div class="kv">
    <div><span>판정</span><b class="stat">${a.status === b.status ? matchBadge(b.status) : `${matchBadge(a.status)}<span class="arr">→</span>${matchBadge(b.status)}`}</b></div>
    ${kvChange("충족", a.pass, b.pass, "up")}${kvChange("정보 부족", a.missing, b.missing, "down")}
    ${kvChange("서류·기록 확인", a.verify, b.verify, "none")}${kvChange("불충족", a.fail, b.fail, "down")}</div></div>`;
}
function upsertCard(tid, patch) {
  if (!tid) return;
  const c = (state.cards[tid] ||= { rounds: {} });
  if (patch.snap) { c.rounds[patch.snap.round] = patch.snap; patch = { ...patch, status: patch.snap.status }; delete patch.snap; }
  Object.assign(c, patch);
  const t = trialMeta(tid), last = Object.values(c.rounds).sort((a, b) => b.round - a.round)[0];
  const status = c.parsing ? '<span class="badge accent">기준 정리 중</span>' : c.matching ? '<span class="badge accent">조건 비교 중</span>'
    : c.status ? matchBadge(c.status) : '<span class="badge">대기</span>';
  const ttl = koTitle(tid, t.title || "");
  const html = `<div class="head"><span class="tid">${esc(tid)}</span>${statusBadge(t.overall_status)}${demoBadge(tid)}${status}</div>
    <div class="ttl">${esc(ttl)}</div>${c.counts ? `<div class="sum">${esc(c.counts)}</div>` : last ? `<div class="sum">${esc(snapText(last))}</div>` : ""}
    ${compareBlock(c)}`;
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
// 화면: 한 번에 질문 하나 (patient-screens 시안). 모든 질문은 DOM에 두고 지금 질문만 보이게 함 -> 보내는 값·검증 규칙은 이전과 같음
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
// 객관식 질문 (팀원 4ea12d6): 서버(web/app.answer_problem)와 같은 규칙. 저장값 = 선택지 문구, 여러 개는 ", ", 기타는 "기타: 내용"
const ANSWER_INPUTS = "input[data-q], input[data-yn], input[data-ch], input[data-mc], input[data-etc]";
const isEtc = (o) => String(o).trim().startsWith("기타");
function choiceOptions(q) {
  if (q.answer_type !== "single_choice" && q.answer_type !== "multi_choice") return null;
  const opts = (q.options || []).map(o => String(o).trim()).filter(Boolean);
  return opts.length >= 2 ? opts : null;  // 선택지가 모자라면 자유 입력으로 받음 (서버도 형식 검사를 하지 않음)
}
function choiceAnswer(q, box) {
  const opts = choiceOptions(q), etc = box.querySelector("input[data-etc]");
  const picked = [...box.querySelectorAll("input[data-ch]:checked, input[data-mc]:checked")].map(i => opts[+i.value]).filter(o => o != null);
  const text = (etc && etc.value || "").trim();
  return picked.map(o => isEtc(o) && text ? `기타: ${text}` : o).join(", ");
}
function syncEtc(box) {  // '기타'를 골랐을 때만 내용 입력란을 켬
  const etc = box.querySelector("input[data-etc]"); if (!etc) return;
  const unknown = box.querySelector("input[data-unknown]").checked;
  const qid = etc.dataset.etc, q = (state.questions || []).find(x => x.question_id === qid);
  const on = !unknown && q && [...box.querySelectorAll("input[data-ch]:checked, input[data-mc]:checked")].some(i => isEtc(choiceOptions(q)[+i.value]));
  etc.disabled = !on;
}
const qBox = (q) => document.querySelector(`#qslot .q[data-qid="${CSS.escape(q.question_id)}"]`);
function isAnswered(box) {
  return box.querySelector("input[data-unknown]").checked
    || [...box.querySelectorAll("input[data-yn], input[data-ch], input[data-mc]")].some(i => i.checked)
    || [...box.querySelectorAll("input[data-q]")].some(i => i.value.trim());
}
function showQuestions(qs) {
  state.questions = qs; state.qi = 0;
  state.waiting = true; state.questionsSeen = true; state.submitting = false;
  activate("questions", qs.length ? `추가 질문 ${qs.length}개 · 답하면 다시 분석해요` : "질문이 없습니다.");
  setRunState("답변 대기", "warn");
  const field = (q, n) => {
    const id = esc(q.question_id);
    const desc = `qwhy-${id} qerr-${id}` + (FORMAT[q.answer_type] && FORMAT[q.answer_type][0] ? ` qfmt-${id}` : "");
    const unk = `<label class="unk"><input type="checkbox" data-unknown="${id}"> 모름 · 확인 불가</label>`;
    if (q.answer_type === "yes_no") return `<fieldset class="opts" aria-describedby="${desc}"><legend class="sr-only">질문 ${n} 답변</legend>
        <label><input type="radio" name="yn-${id}" value="예" data-yn="${id}"> 예</label><label><input type="radio" name="yn-${id}" value="아니요" data-yn="${id}"> 아니요</label>${unk}</fieldset>`;
    const opts = choiceOptions(q);
    if (opts) {  // 객관식 (팀원 4ea12d6 질문 형식): 선택지 문구가 곧 보낼 값. 입력 값에는 번호만 두고 문구는 글자로만 표시
      const multi = q.answer_type === "multi_choice";
      return `<fieldset class="opts" aria-describedby="${desc}"><legend class="sr-only">질문 ${n} 답변${multi ? " (여러 개 선택 가능)" : ""}</legend>
        ${multi ? '<div class="fmt">해당하는 것을 모두 고르세요.</div>' : ""}
        ${opts.map((o, k) => `<label><input type="${multi ? "checkbox" : "radio"}" name="ch-${id}" value="${k}" ${multi ? "data-mc" : "data-ch"}="${id}"> ${esc(o)}</label>`).join("")}
        ${opts.some(isEtc) ? `<input type="text" data-etc="${id}" maxlength="${MAX_ANSWER}" aria-label="질문 ${n}의 기타 내용" placeholder="기타를 골랐다면 내용을 적어 주세요" autocomplete="off" disabled>` : ""}
        ${unk}</fieldset>`;
    }
    const [fmt, mode] = FORMAT[q.answer_type] || FORMAT.free_text;
    const ph = q.answer_type === "free_text" ? "알고 있는 만큼 적어 주세요. 일부만 알아도 괜찮아요." : q.answer_type === "number" && q.unit ? `단위: ${q.unit}` : "";
    return `<div class="stack"><label class="field" for="qin-${id}">${q.answer_type === "free_text" ? "들은 그대로 적어도 돼요" : "답변"}</label>
      <input type="text" id="qin-${id}" data-q="${id}" data-type="${esc(q.answer_type)}" maxlength="${MAX_ANSWER}" inputmode="${mode}"${ph ? ` placeholder="${esc(ph)}"` : ""}
      aria-describedby="qt-${id} ${desc}" autocomplete="off">${fmt ? `<div class="fmt" id="qfmt-${id}">${esc(fmt)}${q.answer_type === "number" && q.unit ? ` 단위: ${esc(q.unit)}` : ""}</div>` : ""}
      <fieldset class="opts"><legend class="sr-only">질문 ${n} 모름</legend>${unk}</fieldset></div>`;
  };
  const kindBadge = (q) => q.purpose === "physician_reference" ? '<span class="badge warn qbadge">의료진 참고용 · 판정에 쓰지 않음</span>' : "";
  $("#qslot").innerHTML = `<div class="qbox" role="group" aria-labelledby="q-h">
    <div class="qtop"><h3 id="q-h" class="sr-only" tabindex="-1">추가 질문 ${qs.length}개</h3>
      <div class="line"><span id="qpos" aria-live="polite"></span><span id="qprog"></span></div>
      <div class="seg" id="qseg" aria-hidden="true">${qs.map(() => "<span></span>").join("")}</div></div>
    ${qs.map((q, i) => `<div class="q" data-qid="${esc(q.question_id)}" hidden>
      <div class="qhead">${kindBadge(q)}
        <p class="qt" id="qt-${esc(q.question_id)}"><span class="sr-only">질문 ${i + 1}. </span>${esc(q.question)}</p>
        <p class="why" id="qwhy-${esc(q.question_id)}">${q.why ? esc(q.why) : ""}</p></div>
      ${field(q, i + 1)}<div class="qerr" id="qerr-${esc(q.question_id)}" role="alert"></div></div>`).join("")}
    <div id="qreview" hidden></div>
    <div class="qfoot" id="qfoot">
      <p class="lock"><span class="ico lock-ico" aria-hidden="true"></span><span>이름·연락처 없이 진행돼요</span></p>
      <div class="qnav"><button class="btn ghost" id="qprev" type="button">이전</button>
        <button class="btn" id="qnext" type="button">다음</button><button class="btn" id="send" type="button" hidden>답변 확인</button></div>
      <button class="link" id="skip" type="button">모두 모름으로 보내기</button>
    </div>
    <div class="err" id="qerror" role="alert"></div></div>`;
  $("#progress-panel").classList.add("asking");
  $("#qslot").querySelectorAll("input[data-unknown]").forEach(cb => cb.addEventListener("change", () => {
    const box = cb.closest(".q"); box.querySelectorAll(ANSWER_INPUTS).forEach(i => { i.disabled = cb.checked; i.removeAttribute("aria-invalid"); });
    if (!cb.checked) syncEtc(box);
    box.querySelector(".qerr").textContent = ""; qProgress(qs);
  }));
  $("#qslot").querySelectorAll(ANSWER_INPUTS).forEach(i => i.addEventListener(i.type === "text" ? "input" : "change", () => {
    i.removeAttribute("aria-invalid");
    const box = i.closest(".q"); box.querySelector(".qerr").textContent = ""; syncEtc(box); qProgress(qs);
  }));
  $("#qslot").querySelectorAll("input[data-q], input[data-etc]").forEach(i => i.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); (state.qi < qs.length - 1 ? $("#qnext") : $("#send")).click(); }  // Enter로 바로 보내지 않고 다음 질문·확인 단계로
  }));
  $("#qprev").addEventListener("click", () => gotoQ(qs, state.qi - 1));
  $("#qnext").addEventListener("click", () => { if (checkQ(qs[state.qi])) gotoQ(qs, state.qi + 1); });
  $("#send").addEventListener("click", () => reviewAnswers(qs, false));
  $("#skip").addEventListener("click", () => reviewAnswers(qs, true));
  gotoQ(qs, 0);
}
// 지금 질문 하나만 보이게. 마지막 질문에서는 "다음" 대신 "답변 확인"
function gotoQ(qs, i) {
  if (i < 0 || i >= qs.length) return;
  state.qi = i; $("#qreview").hidden = true; $("#qfoot").hidden = false; $("#qerror").textContent = "";
  qs.forEach((q, k) => { qBox(q).hidden = k !== i; });
  $("#qprev").disabled = i === 0;
  $("#qnext").hidden = i === qs.length - 1; $("#send").hidden = i !== qs.length - 1;
  qProgress(qs);
  // 질문마다 같은 위치에서 시작 (스크롤이 남아 있으면 다음 질문의 버튼 자리가 달라져 잘못 누르기 쉬움)
  $("#qslot").scrollIntoView({ block: "start" });
  const box = qBox(qs[i]); const t = box.querySelector(".qt"); t.tabIndex = -1; t.focus({ preventScroll: true });
}
// 이 질문의 형식만 확인 (보낼 때 전체를 한 번 더 확인)
function checkQ(q) {
  const box = qBox(q), t = box.querySelector("input[data-q]");
  if (!t || box.querySelector("input[data-unknown]").checked) return true;
  const err = formatError(q.answer_type, t.value.trim());
  box.querySelector(".qerr").textContent = err;
  if (err) { t.setAttribute("aria-invalid", "true"); t.focus(); return false; }
  t.removeAttribute("aria-invalid"); return true;
}
// 진행 표시: 몇 번째 질문인지, 몇 개 답했는지 (실제 질문 수로만 계산)
function qProgress(qs) {
  let n = 0; const segs = [...document.querySelectorAll("#qseg > span")];
  qs.forEach((q, k) => {
    const box = qBox(q); if (!box) return;
    const done = isAnswered(box); if (done) n++;
    if (segs[k]) segs[k].className = done ? "on" : k === state.qi ? "now" : "";
  });
  const pos = $("#qpos"); if (pos) pos.textContent = `질문 ${(state.qi ?? 0) + 1} / ${qs.length}`;
  const el = $("#qprog"); if (el) el.textContent = `${n}개 답함`;
}
// 보낸 답변은 서버가 다시 보내 주지 않으므로(SSE에 답변 내용 없음) 이 화면 메모리에만 두고 요약해 보여 줌
function answerRows(s) {
  return s.qs.map((q, i) => `<tr><th scope="row">${i + 1}. ${esc(q.question)}</th><td>${s.answers[q.question_id] === UNKNOWN_ANSWER
    ? '<span class="badge">모름 · 확인 불가</span>' : esc(s.answers[q.question_id])}</td></tr>`).join("");
}
function sentSummary() {
  const s = state.sent; if (!s) return "";
  return `<details class="sent"><summary><span id="sent-state">보낸 답변 ${s.qs.length}개 · 반영 중</span></summary>
    <table class="review"><caption class="sr-only">보낸 답변</caption>${answerRows(s)}</table></details>`;
}
function collectAnswers(qs, allUnknown) {
  const out = {}, errors = [];
  for (const q of qs) {
    const box = qBox(q);
    const unknown = allUnknown || box.querySelector("input[data-unknown]").checked;
    let v = "";
    if (!unknown) {
      const t = box.querySelector("input[data-q]"); const yn = box.querySelector("input[data-yn]:checked");
      v = choiceOptions(q) ? choiceAnswer(q, box) : (t ? t.value : yn ? yn.value : "").trim();
      if (t && q.answer_type === "number" && q.unit && v && !DECLINED.has(v) && !v.includes(q.unit)) v = `${v} ${q.unit}`;
      const err = t ? formatError(q.answer_type, v) : "";
      box.querySelector(".qerr").textContent = err;
      if (t) { if (err) t.setAttribute("aria-invalid", "true"); else t.removeAttribute("aria-invalid"); }
      if (err) errors.push(q);
    }
    out[q.question_id] = unknown || DECLINED.has(v) ? UNKNOWN_ANSWER : v.slice(0, MAX_ANSWER);
  }
  return [out, errors];
}
function reviewAnswers(qs, allUnknown) {
  if (state.submitting) return;
  const [answers, errors] = collectAnswers(qs, allUnknown);
  $("#qerror").textContent = "";
  if (errors.length) {
    gotoQ(qs, qs.indexOf(errors[0])); const t = qBox(errors[0]).querySelector("input[data-q]"); if (t) t.focus();
    $("#qerror").textContent = "형식이 맞지 않는 답변이 있습니다. 표시된 질문을 확인하세요."; return;
  }
  qs.forEach(q => { qBox(q).hidden = true; }); $("#qfoot").hidden = true;
  const box = $("#qreview"); box.hidden = false;
  box.innerHTML = `<div class="stack" role="region" aria-labelledby="qreview-h"><div><h3 class="h3" id="qreview-h" tabindex="-1">이 답변을 보낼까요?</h3>
    <p class="hint m0">보낸 뒤에는 바꿀 수 없고, 답변을 반영해 다시 분석해요.</p></div>
    <table class="review"><caption class="sr-only">보낼 답변</caption>${answerRows({ qs, answers })}</table>
    <button class="btn block" id="confirm-send" type="button">이대로 보내기</button>
    <button class="btn ghost block" id="edit-answers" type="button">답변 수정</button></div>`;
  $("#qslot").scrollIntoView({ block: "start" });
  $("#confirm-send").addEventListener("click", () => submitAnswers(answers));
  $("#edit-answers").addEventListener("click", () => { const k = qs.findIndex(q => !isAnswered(qBox(q))); gotoQ(qs, k >= 0 ? k : qs.length - 1); });
  $("#confirm-send").focus({ preventScroll: true });
}
async function submitAnswers(answers) {
  if (state.submitting) return;  // 중복 제출 방지
  state.submitting = true;
  const btns = ["#confirm-send", "#edit-answers", "#send", "#skip"].map(s => $(s)).filter(Boolean);
  btns.forEach(b => { b.disabled = true; }); $("#confirm-send").textContent = "보내는 중…"; $("#qerror").textContent = "";
  state.sent = { qs: state.questions || [], answers };  // answers 이벤트가 응답보다 먼저 올 수 있어 보내기 전에 기억
  let r = null; try { r = await post(`/api/runs/${encodeURIComponent(state.runId)}/answers`, { answers }); } catch (e) { r = null; }
  if (r && r.ok) { stageNote("답변 전송 완료 · 다시 분석 중"); return; }  // 화면 전환은 서버의 answers 이벤트로
  state.submitting = false; if (!state.answered) state.sent = null;
  if (r && r.status === 409) { $("#qerror").textContent = "지금은 답변을 받을 수 없는 상태입니다(이미 보냈거나 분석이 끝남). 진행 상황을 확인하세요."; return; }
  btns.forEach(b => { b.disabled = false; }); $("#confirm-send").textContent = "다시 보내기";
  $("#qerror").textContent = "답변을 보내지 못했습니다. 입력한 답변은 그대로 있으니 다시 보내 주세요." + (r ? ` (${await detailOf(r)})` : "");
  $("#confirm-send").focus();
}

// ------------------------------------------------------------ 추천 결과 (서버 결과·표시 정보 필드만 사용, 판정은 바꾸지 않음)
const SOURCE = { record: "환자 기록", self_report: "본인·보호자 답변", unknown: "출처 미확인", none: "근거 없음" };
// 서버 판정 함수로 분류한 기준 묶음 (web/display.py). 화면 이름은 시안(patient-screens)의 쉬운 말
const GROUPS = ["met", "record_check", "missing", "physician_review", "procedural", "not_met", "unclassified"];
const GROUP_UI = {
  met: ["확인됨", "✓"], record_check: ["서류 확인 필요", "!"], missing: ["정보 부족", "?"],
  physician_review: ["연구진 판단", "?"], procedural: ["병원에서 확인", "+"], not_met: ["맞지 않음", "×"],
  unclassified: ["확인 필요", "?"],
};
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
// AI 문장 속 내부 기준 번호 "(I3)", "(I7-I9)", "(E2)"는 사용자에게 뜻이 없어 표시에서만 뺌
const RULE_TAIL = /\s*\((?:[IE]\d+)(?:\s*[-~,·]\s*[IE]?\d+)*\)/g;
function aiList(label, items, cls) {
  const list = (items || []).filter(Boolean); if (!list.length) return "";
  // 내부 표기가 섞인 항목만 접고 나머지는 그대로 보여 줌 (원문은 지우지 않음)
  const clean = list.filter(x => !CODE_RE.test(x)), coded = list.filter(x => CODE_RE.test(x));
  if (!clean.length) return aiText(label, list.join("\n"));
  return `<div class="ai${cls ? " " + cls : ""}"><span class="lbl">${esc(label)} · 검증된 사실 아님</span><ul>${clean.map(x => `<li>${esc(String(x).replace(RULE_TAIL, ""))}</li>`).join("")}</ul>
    ${coded.length ? raw(coded.join("\n"), `내부 표기가 섞인 항목 ${coded.length}개 보기`) : ""}</div>`;
}
function groupOf(d, a) {
  const g = d && d.groups ? d.groups[a.rule_id] : null;
  if (g && GROUPS.includes(g)) return g;
  if (a.passes === true) return "met";
  if (a.passes === false) return "not_met";
  return "unclassified";  // 표시 정보가 없으면 판정 보류 항목은 중립 분류
}
function grouped(m, d) {
  const out = Object.fromEntries(GROUPS.map(g => [g, []]));
  for (const a of (m && m.assessments) || []) out[groupOf(d, a)].push(a);
  return out;
}
// 조건 확인 현황: 조건 하나당 한 칸 (실제 조건 수로만 그림, 임의 진행률 없음)
function condStat(m, d) {
  if (!m || m.eligibility === "NOT_EVALUATED") return "";
  const g = grouped(m, d), total = (m.assessments || []).length; if (!total) return "";
  const order = GROUPS.filter(k => g[k].length);
  return `<div class="cstat"><div class="line"><b>조건 확인</b><span><em>${g.met.length}</em> / ${total}개</span></div>
    <div class="seg" aria-hidden="true">${order.map(k => g[k].map(() => `<span class="${k}"></span>`).join("")).join("")}</div>
    <div class="legend2" aria-label="조건 수">${order.map(k => `<span><i class="${k}" aria-hidden="true"></i>${esc(GROUP_UI[k][0])}<b>${g[k].length}</b></span>`).join("")}</div></div>`;
}
// 다음에 할 일: AI 문장이 아니라 서버 분류 결과(조건 묶음 수)로만 만듦. [할 일, 개수, 색]
function todoLines(m, d) {
  if (!m) return [];
  if (m.eligibility === "NOT_EVALUATED") return [["평가 오류 · 다시 분석 필요 (부적격 아님)", "", "warn"]];
  const g = grouped(m, d), out = [];
  if (g.not_met.length) out.push(["맞지 않는 조건 확인", g.not_met.length, "bad"]);
  if (g.record_check.length) out.push(["검사 결과지·진료 기록 준비", g.record_check.length, "warn"]);
  if (g.missing.length) out.push(["부족한 정보 채우기", g.missing.length, "warn"]);
  if (g.physician_review.length) out.push(["담당 의사와 프로토콜 대조", g.physician_review.length, "warn"]);
  if (g.unclassified.length) out.push(["의료진 확인", g.unclassified.length, "warn"]);
  if (g.procedural.length) out.push(["참여 단계에서 연구기관이 확인", g.procedural.length, ""]);
  if (!out.length) out.push(["연구기관 스크리닝 받기", "", "ok"]);
  return out;
}
function todoBox(m, d, all) {
  const lines = todoLines(m, d); if (!lines.length) return "";
  return `<div class="todo"><b>다음에 할 일</b><ul class="tl">${(all ? lines : lines.slice(0, 3)).map(([t, n, c]) =>
    `<li class="${c}"><span>${esc(t)}</span><b>${n === "" ? "" : esc(n) + "개"}</b></li>`).join("")}</ul></div>`;
}
function badges(tid, status, info) {
  return `<div class="badges">${status ? matchBadge(status) : ""}${statusBadge(info.overall_status)}${demoBadge(tid)}</div>`;
}
function trialLink(tid, info) {
  const url = safeTrialUrl(info.url);
  return url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">ClinicalTrials.gov 원문 보기 (전문 용어)<span class="sr-only"> (${esc(tid)}, 새 창)</span></a>` : "";
}
const EXCLUDE_TEXT = {
  not_evaluated: () => "제외 사유: 평가 오류 (부적격 아님)",
  closed: (d) => `제외 사유: 모집 종료 (${recruitText(d.recruitment_status)})`,
  demo: () => "제외 사유: 가상 데모 시험",
  mismatch: () => "제외 사유: 맞지 않는 조건 있음",
  unknown: () => "제외 사유: 기타 (조건 확인 참고)",
};
function noRecommendation(rec, display) {
  const kinds = (display && display.no_recommendation) || null;
  let html = `<div class="callout">현재 추천할 수 있는 임상시험이 없어요.`;
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
function reportHtml(report) {
  if (!report) return "";
  // AI 보고서 마크다운: 안에 섞인 HTML은 해석하지 않고 글자로 (1차), 그 결과도 DOMPurify로 정화 (2차, style 제거)
  return window.marked && window.DOMPurify
    ? DOMPurify.sanitize(marked.parse(report, { renderer: REPORT_RENDERER }), { FORBID_TAGS: ["style"], FORBID_ATTR: ["style"] })
    : `<pre class="rawtext">${esc(report)}</pre>`;
}
// ------------------------------------------------------------ 한국어 표시 (번역 파일 data/i18n/trials_ko.json, 팀원 4ea12d6)
// 판정·상태 코드는 바꾸지 않고 글자만: 번역 당시 원문과 지금 원문이 같을 때만 번역을 쓰고, 원문은 함께 보여 줌
function koTitle(tid, title) {
  const k = (state.ko || {})[tid];
  return k && k.title && title && k.title_en === title ? k.title : title;
}
function koRule(tid, rid, en) {
  const r = (((state.ko || {})[tid] || {}).rules || {})[rid];
  return r && r.ko && r.en === en ? r.ko : null;
}
const critLabel = (a) => a.criterion_ko || a.criterion_text || "";
function origLine(shown, original) {
  return original && shown !== original ? `<div class="hint orig" lang="en">원문: ${esc(original)}</div>` : "";
}
function annotateKo(result) {
  for (const [tid, m] of Object.entries((result && result.matches) || {}))
    for (const a of m.assessments || []) { const ko = koRule(tid, a.rule_id, a.criterion_text); if (ko) a.criterion_ko = ko; }
}

// ------------------------------------------------------------ 결과 목록 (시안 results.html)
function resultCard(tid, status, extra) {
  const R = state.res, info = R.trials[tid] || {}, m = R.matches[tid], d = R.dOf(tid), card = state.cards[tid];
  const ttl = koTitle(tid, info.title || "");
  return `<article class="rec${extra && extra.top ? " top" : ""}">${badges(tid, status, info)}
    <div><h3 class="rtitle">${extra && extra.rank ? `<span class="rank" aria-label="표시 순서 ${esc(extra.rank)}">${esc(extra.rank)}.</span> ` : ""}${esc(ttl)}</h3>${origLine(ttl, info.title)}
      <p class="rmeta mt4">${esc(tid)} · ${esc((STATUS[status] || [])[2] || "")}</p></div>
    ${extra && extra.note ? `<p class="hint m0">${esc(extra.note)}</p>` : ""}
    ${condStat(m, d)}${card ? compareBlock(card) : ""}${todoBox(m, d, false)}
    <div class="actions"><button class="btn ghost" type="button" data-detail="${esc(tid)}">자세히 보기</button></div></article>`;
}
function showResult(ev) {
  annotateKo(ev.result);
  const result = ev.result || {}; const rec = result.recommendation || { ranked: [], excluded: [] };
  const matches = result.matches || {}; const trials = ev.trials || {};
  const display = ev.display || null; const dOf = (tid) => (display && display.trials && display.trials[tid]) || null;
  state.res = { result, rec, matches, trials, display, dOf, report: ev.report || "", note: ev.match_note || "" };
  for (const [tid, m] of Object.entries(matches)) {
    const g = grouped(m, dOf(tid));
    const c = m.eligibility === "NOT_EVALUATED" ? "" : `충족 ${g.met.length} · 불충족 ${g.not_met.length} · 정보 부족 ${g.missing.length} · 추가 확인 ${g.record_check.length + g.physician_review.length + g.procedural.length + g.unclassified.length}`;
    upsertCard(tid, { status: m.match_status || m.eligibility, parsing: false, matching: false, counts: c });
  }
  const u = result.usage || {};
  $("#usage").textContent = u.calls ? `AI 호출 ${u.calls}회` : "";
  const top = rec.recommended || [], flow = [], nEval = Object.keys(matches).length;
  flow.push(state.screen && state.screen.pool ? `${state.screen.pool}개 중 ${state.screen.selected ?? nEval}개 비교` : `${nEval}개 비교`);
  if (state.sent) flow.push(`질문 ${state.sent.qs.length}개 반영`);
  let html = `<div class="rhead"><p class="eyebrow">분석 완료</p>
    <h3 class="big">${top.length ? `검토해 볼 임상시험<br><em>${top.length}건</em>을 찾았어요` : "지금 추천할 임상시험이 없어요"}</h3>
    <div class="chips">${flow.map(f => `<span class="badge">${esc(f)}</span>`).join("")}</div></div>
    <div class="infobox"><span class="i" aria-hidden="true">i</span><p class="m0" title="${esc(state.res.note)}"><b>참여 확정 아님</b> · 최종 판단은 연구진이 해요</p></div>`;
  if (!display) html += `<div class="note warn">결과 분류 정보를 받지 못해 일부 조건을 "확인 필요"로 표시합니다. 판정 상태는 서버 결과 그대로입니다.</div>`;
  if (rec.no_recommendation_reason) html += noRecommendation(rec, display);
  html += top.map(r => resultCard(r.trial_id, r.match_status || r.eligibility, { top: true, rank: top.length > 1 ? r.rank : null })).join("");
  const others = (rec.ranked || []).filter(r => !top.some(t => t.trial_id === r.trial_id));
  if (others.length) html += `<details class="more"><summary>추가 확인이 필요한 다른 후보 ${others.length}건 보기</summary><div class="stack">${others.map(r => resultCard(r.trial_id, r.match_status || r.eligibility)).join("")}</div></details>`;
  const shown = new Set([...top, ...others].map(r => r.trial_id));
  const excluded = rec.excluded || [];
  if (excluded.length) html += `<details class="more"><summary>지금 조건과 맞지 않는 연구 ${excluded.length}건 보기</summary><div class="stack">${excluded.map(x => {
    shown.add(x.trial_id); const d = dOf(x.trial_id), kind = d && d.exclusion && EXCLUDE_TEXT[d.exclusion] ? d.exclusion : "unknown";
    const m = matches[x.trial_id];
    const bad = kind === "mismatch" && m ? (m.assessments || []).filter(a => groupOf(d, a) === "not_met").slice(0, 3).map(critLabel) : [];
    return resultCard(x.trial_id, m ? m.match_status : null, { note: EXCLUDE_TEXT[kind](d || {}) + (bad.length ? ` · ${bad.join(" / ")}` : "") })
      + (kind !== "not_evaluated" ? raw(x.reason, "시스템 원문 사유 보기") : "");  // 평가 오류 원문은 보이지 않음 (요청 내용이 섞일 수 있음)
  }).join("")}</div></details>`;
  const rest = Object.entries(matches).filter(([tid]) => !shown.has(tid));
  if (rest.length) html += `<details class="more"><summary>그 밖에 평가한 시험 ${rest.length}건</summary><div class="stack">${rest.map(([tid, m]) => resultCard(tid, m.match_status)).join("")}</div></details>`;
  if (!top.length && !others.length && !rec.no_recommendation_reason) html += '<div class="empty">추천할 임상시험이 없습니다.</div>';
  html += `<details class="more"><summary>상태 설명 보기</summary><p class="hint mt4">${esc(state.res.note)}</p>${top.length && rec.selection_reason ? (CODE_RE.test(rec.selection_reason) ? raw(rec.selection_reason, "표시 순서 기준") : `<p class="hint">표시 순서 · ${esc(rec.selection_reason)}</p>`) : ""}<div class="legend mt8">${Object.entries(STATUS).map(([, [t, c, d]]) =>
    `<span class="badge ${c}">${esc(t)}</span><span>${esc(d)}</span>`).join("")}</div></details>`;
  if (rec.overall_comment && rec.overall_comment !== rec.no_recommendation_reason) html += `<details class="more"><summary>AI 종합 설명 보기</summary>${aiText("AI 종합 설명", rec.overall_comment)}</details>`;
  if (ev.report) html += `<details class="more report"><summary>AI 설명 보고서 (검증된 사실 아님 · 내부 표기가 포함될 수 있음)</summary><div class="md">${reportHtml(ev.report)}</div></details>`;
  html += `<button class="btn block" type="button" data-summary="1">담당 의사에게 보여줄 요약 만들기</button>`;
  $("#final").innerHTML = html;
  finishActive(); setStage("recommend", "done");
  $('#pages [data-page="result"]').disabled = false; showPage("result");
  $("#result-h").focus();
}

// ------------------------------------------------------------ 상세: 시험 설명과 내 조건 (시안 detail.html)
function condRow(tid, a, g, said) {
  const name = critLabel(a), [state0, mark] = GROUP_UI[g];
  // 오른쪽 상태 이름은 짧게, 설명은 한 줄 (줄글 대신)
  let stateText = state0, desc = "";
  if (g === "met") { stateText = "확인됨"; desc = a.method === "rule_engine" ? "규칙으로 계산" : "AI 판정 · 검증된 사실 아님"; }
  if (g === "not_met") stateText = a.kind === "exclusion" ? "제외 기준 해당" : "맞지 않음";
  if (g === "record_check") { stateText = "서류 확인 필요"; desc = (said === "yes" ? "답변과 일치 · " : "") + "검사 결과지·진료 기록으로 확정"; }
  if (g === "missing") { stateText = "정보 부족"; desc = a.missing_info ? `필요: ${a.missing_info}` : "불충족 아님"; }
  if (g === "physician_review") { stateText = "연구진 판단"; desc = "기준에 구체적인 값 없음 · 의사가 프로토콜과 대조"; }
  if (g === "procedural") { stateText = "병원에서 확인"; desc = "참여 단계에서 확인 · 지금 준비할 것 없음"; }
  if (g === "unclassified") { stateText = "확인 필요"; desc = "분류 불가 · 의료진 확인"; }
  const ev = a.evidence && !/^근거 없음/.test(a.evidence) ? `<p class="cev">${g === "met" || g === "not_met" ? "근거" : "입력 내용"} <q>${esc(a.evidence)}</q>
    <span class="hint"> · ${esc(SOURCE[a.evidence_source] || "근거 없음")}</span></p>` : "";
  return `<div class="cond"><span class="dot ${g}" aria-hidden="true">${mark}</span><div class="ctext">
    <div class="ctop"><p class="cname">${esc(name)}</p><p class="cstate ${g}">${esc(stateText)}</p></div>
    <p class="cdesc">${a.kind === "exclusion" ? "제외 기준" : "선정 기준"}${a.cohort ? ` · 코호트 ${esc(a.cohort)}` : ""}${desc ? ` · ${esc(desc)}` : ""}</p>
    ${ev}${origLine(name, a.criterion_text)}</div></div>`;
}
function renderDetail(tid) {
  const R = state.res; if (!R) return;
  const info = R.trials[tid] || {}, m = R.matches[tid], d = R.dOf(tid), card = state.cards[tid];
  const r = [...(R.rec.ranked || []), ...(R.rec.recommended || [])].find(x => x.trial_id === tid) || {};
  const status = r.match_status || (m && m.match_status) || null, ttl = koTitle(tid, info.title || "");
  const said = Object.fromEntries(((d && d.record_checks) || []).map(x => [x.rule_id, x.said]));
  let html = `<div class="dhead"><div class="dmeta">${badges(tid, status, info)}</div><h2 id="detail-h" tabindex="-1">${esc(ttl)}</h2>${origLine(ttl, info.title)}
    <p class="hint m0">${esc(tid)} · ${esc((STATUS[status] || [])[2] || "")}</p>${trialLink(tid, info)}</div>`;
  if (card && compareBlock(card)) html += `<section class="sec">${compareBlock(card)}</section>`;
  if (r.key_matches || r.key_concerns || r.next_steps) html += `<section class="sec"><h3>AI가 정리한 내용</h3>
    <div class="two">${aiList("맞는 점", r.key_matches, "good")}${aiList("확인할 점", r.key_concerns, "check")}</div>${aiList("다음 단계 제안", r.next_steps)}</section>`;
  html += todoBox(m, d, true);
  if (m && m.eligibility === "NOT_EVALUATED") html += `<div class="note warn">평가 중 오류로 판정하지 못했습니다${d && d.error_type ? ` (오류 종류: ${esc(d.error_type)})` : ""}. 부적격이라는 뜻이 아니며, 오류 상세는 서버 기록에만 남깁니다.</div>`;
  else if (m) {
    const g = grouped(m, d), total = (m.assessments || []).length;
    html += `<section class="stack"><div class="sech"><h3>내 조건 확인</h3><span>${g.met.length} / ${total}개 확인</span></div>`;
    if (g.met.length) {
      const first = g.met.slice(0, 2), more = g.met.slice(2);
      html += `<div class="conds">${first.map(a => condRow(tid, a, "met")).join("")}
        ${more.length ? `<details><summary>확인된 조건 ${more.length}개 더 보기</summary>${more.map(a => condRow(tid, a, "met")).join("")}</details>` : ""}</div>`;
    }
    for (const k of GROUPS.filter(k => k !== "met" && g[k].length))
      html += `<div class="cbox ${k}"><div class="gcap"><span>${esc(GROUP_UI[k][0])}</span><span>${g[k].length}개</span></div>${g[k].map(a => condRow(tid, a, k, said[a.rule_id])).join("")}</div>`;
    html += `</section>`;
  }
  const notes = [...(r.verification_needed || []), ...(r.physician_review || [])];
  if (notes.length) html += raw(notes.join("\n"), "시스템 메모 원문 보기");
  html += `<button class="btn ghost block" type="button" data-summary="1">담당 의사와 공유하기</button>`;
  $("#detail").innerHTML = html;
  showPage("detail"); $("#detail-h").focus();
}

// ------------------------------------------------------------ 담당 의사에게 보여줄 요약 (인쇄·PDF 저장용, 서버 결과 그대로)
function renderSummary() {
  const R = state.res; if (!R) return;
  const list = [...(R.rec.recommended || []), ...(R.rec.ranked || [])].filter((r, i, a) => a.findIndex(x => x.trial_id === r.trial_id) === i);
  const sec = (r) => {
    const tid = r.trial_id, info = R.trials[tid] || {}, m = R.matches[tid], d = R.dOf(tid), g = grouped(m, d);
    const items = (k) => g[k].length ? `<tr><th scope="row">${esc(GROUP_UI[k][0])} (${g[k].length})</th><td><ul class="checks">${g[k].map(a => `<li>${esc(critLabel(a))}</li>`).join("")}</ul></td></tr>` : "";
    const url = safeTrialUrl(info.url);
    return `<section class="sec"><h3>${esc(koTitle(tid, info.title || ""))}</h3><p class="hint m0">${esc(tid)} · ${esc((STATUS[r.match_status] || [])[0] || "")} · ${esc(recruitText(info.overall_status))}${trialMeta(tid).synthetic ? " · 가상 시험(실제 모집 아님)" : ""}${url ? ` · ${esc(url)}` : ""}</p>
      <table class="sumtable"><caption class="sr-only">조건 확인 요약</caption><tr><th scope="row">확인됨</th><td>${g.met.length}개</td></tr>
        ${["record_check", "missing", "physician_review", "not_met", "unclassified", "procedural"].map(items).join("")}</table></section>`;
  };
  let html = `<div class="dhead"><h2 id="summary-h" tabindex="-1">담당 의사에게 보여줄 요약</h2>
    <p class="hint m0">진료 때 이 화면을 보여 주거나 인쇄·PDF로 저장하세요. AI가 공개 기준과 비교한 참고 자료이며 진단이나 참여 확정이 아니에요.</p></div>`;
  html += list.length ? list.map(sec).join("") : `<div class="note">추천 목록에 오른 시험이 없어요.</div>`;
  if (state.sent) html += `<section class="sec"><h3>추가 질문과 답변</h3><table class="review"><caption class="sr-only">추가 질문과 답변</caption>${answerRows(state.sent)}</table></section>`;
  if (R.report) html += `<details class="more report"><summary>AI 설명 보고서 (검증된 사실 아님)</summary><div class="md">${reportHtml(R.report)}</div></details>`;
  html += `<div class="note warn">${esc($("#disclaimer").textContent)}</div><button class="btn block noprint" id="print" type="button">인쇄 또는 PDF로 저장</button>`;
  $("#summary").innerHTML = html;
  $("#print").addEventListener("click", () => window.print());
  showPage("summary"); $("#summary-h").focus();
}

init();
