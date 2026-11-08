"""가상 서류·환자 소개글 만들기와 서류 읽기 (끝까지 자동 실행 scripts/run_e2e_synthetic.py용). 합성 데이터만.

원칙
- 값은 LLM이 정하지 않습니다. 합성 코호트의 정답 사실(oracle truth)에서 오고, LLM은 그 값을 진짜 같은 문장으로 옮기기만 합니다.
- LLM이 쓴 글은 검사기(validate_*)를 통과해야 씁니다. 값·날짜가 다르거나 빠졌거나, 숨겨야 할 값이 들어가면 다시 쓰게 하고,
  두 번 더 실패하면 틀(템플릿) 글로 바꿉니다. 바꾼 횟수는 결과에 남깁니다.
- 모든 서류 첫 줄에 '[가상 서류 · 실제 환자 아님]'을 박습니다.
- 같은 입력(환자·정보원·항목·값·상황·프롬프트 버전·모델)이면 저장해 둔 글을 다시 씁니다 (재현, 비용 한 번).

상황(scenario): normal = 정답 값 그대로 / missing = 서류에 그 항목이 없음 / conflict = 서류 값이 환자가 말한 값과 다름
(conflict는 환자가 말한 값이 있을 때만 씀. 그 경우 서류 값은 정답, 환자가 말한 값이 틀린 쪽).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from pydantic import BaseModel

PROMPT_VERSION = "e2e-docs/1"
WATERMARK = "[가상 서류 · 실제 환자 아님]"

# 항목 -> (서류에 쓰는 이름, 문장에서 찾을 이름들)
FIELDS = {
    "age": ("Age", ["age", "나이", "year-old", "year old", "years old", "세"]),
    "ecog": ("ECOG performance status", ["ecog"]),
    "lab:platelets": ("Platelets (PLT)", ["platelet", "plt", "혈소판"]),
    "lab:absolute neutrophil count": ("Absolute neutrophil count (ANC)", ["anc", "neutrophil", "호중구"]),
    "lab:hemoglobin": ("Hemoglobin (Hb)", ["hemoglobin", "hb", "혈색소", "헤모글로빈"]),
    "biomarker": ("EGFR mutation test", ["egfr"]),
}
SOURCES = {"lab_report": "Laboratory report (검사 결과지)", "pathology_report": "Pathology / molecular report (조직·유전자 검사 보고서)",
           "genomic_report": "Genomic report (유전자 검사 보고서)", "medical_record": "Outpatient clinic note (외래 진료 기록)"}


@dataclass(frozen=True)
class DocItem:
    field: str
    value: object  # 서류에 쓰는 값 (None이면 서류에 없음)
    unit: str | None = None
    observed_at: str | None = None
    scenario: str = "normal"


def fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _num_in(line: str, v) -> bool:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return any(re.search(rf"(?<![\d.]){re.escape(s)}(?![\d])", line) for s in {fmt(v), fmt(float(v)), f"{float(v):.1f}"})
    return all(tok.lower() in line.lower() for tok in re.findall(r"[A-Za-z0-9]+", str(v)) if tok.lower() not in {"egfr"})


def _lines_with(text: str, field: str) -> list[str]:
    """그 항목 이름이 나오는 줄 (영문 이름은 단어 앞 경계: 'age'가 'stage'에 걸리지 않게)."""
    pats = [re.compile(rf"(?<![a-z]){re.escape(n)}" if n.isascii() else re.escape(n)) for n in FIELDS[field][1]]
    return [ln for ln in text.splitlines() if any(p.search(ln.lower()) for p in pats)]


# ---------------------------------------------------------------------------
# 틀(템플릿) - 오프라인 모드, LLM 실패 때 대신
# ---------------------------------------------------------------------------

def template_document(patient: str, source: str, items: list[DocItem], reference_date: str) -> str:
    rows = [f"{FIELDS[i.field][0]}: {fmt(i.value)}{' ' + i.unit if i.unit else ''}" + (f" (검사일 {i.observed_at})" if i.observed_at else "")
            for i in items if i.value is not None]
    return "\n".join([WATERMARK, SOURCES.get(source, source), f"환자: {patient} (합성)", f"작성일: {reference_date}", "", *rows,
                      "", "※ 발표·시험용 가상 서류"])


def template_narrative(public: list[DocItem], hide: set[str]) -> str:
    """오프라인 대역 추출기(qe.stub_profiler)가 읽는 영어 표기. 나중에 말한 값이 이김 (같은 항목 둘이면 자기 보고를 뒤에)."""
    head, lines = ["Synthetic NSCLC patient (not a real person). Stage IV non-small cell lung cancer."], []
    for i in sorted(public, key=lambda x: x.scenario == "self_report"):  # 자기 보고는 맨 뒤 줄 (대역 추출기는 나중 줄이 이김)
        if i.field in hide:
            continue
        own = "Patient reports " if i.scenario == "self_report" else ""
        if i.field == "age":
            head.insert(1, f"{fmt(i.value)}-year-old patient.")
        elif i.field == "ecog" and not own:
            head.append(f"ECOG {fmt(i.value)}.")
        elif i.field in FIELDS:
            name = "ECOG" if i.field == "ecog" else FIELDS[i.field][0].split(" (")[0]
            lines.append(f"{own}{name} {fmt(i.value)}{' ' + i.unit if i.unit else ''}")
    return "\n".join([" ".join(head), *lines])


# ---------------------------------------------------------------------------
# 검사기
# ---------------------------------------------------------------------------

def validate_document(text: str, items: list[DocItem]) -> list[str]:
    """서류가 정해진 값만 담았는지. 문제 목록(비면 통과)."""
    out = []
    if WATERMARK not in text:
        out.append("watermark_missing")
    for i in items:
        lines = _lines_with(text, i.field)
        if i.value is None:
            if lines:
                out.append(f"{i.field}:should_be_absent")
            continue
        if not any(_num_in(ln, i.value) for ln in lines):
            out.append(f"{i.field}:value_missing_or_wrong")
        if i.observed_at and i.observed_at not in text:
            out.append(f"{i.field}:date_missing")
    return out


def validate_narrative(text: str, public: list[DocItem], hide: set[str], hidden_truth: list[DocItem]) -> list[str]:
    """소개글이 공개 값을 담고, 숨길 값·정답(숨긴) 값을 담지 않았는지."""
    out = []
    for i in public:
        lines = _lines_with(text, i.field)
        if i.field in hide:
            if any(_num_in(ln, i.value) for ln in lines):
                out.append(f"{i.field}:hidden_value_leaked")
        elif not any(_num_in(ln, i.value) for ln in lines):
            out.append(f"{i.field}:value_missing_or_wrong")
    shown = {i.field for i in public}
    for i in hidden_truth:
        if i.field not in shown and any(_num_in(ln, i.value) for ln in _lines_with(text, i.field)):
            out.append(f"{i.field}:oracle_value_leaked")
    return out


# ---------------------------------------------------------------------------
# 서류 읽기 (검증자 대역)
# ---------------------------------------------------------------------------

def template_extract(text: str, fields: list[str]) -> dict[str, dict | None]:
    out = {}
    for f in fields:
        hit = None
        for ln in _lines_with(text, f):
            body = ln.split(":", 1)[1] if ":" in ln else ln
            date = re.search(r"\d{4}-\d{2}-\d{2}", body)
            if f == "biomarker":
                val = re.sub(r"\(.*?\)", "", body).strip()
                hit = {"value": val, "unit": None, "observed_at": date.group(0) if date else None} if val else None
            else:
                m = re.search(r"(?<![\d.-])(\d+(?:\.\d+)?)", re.sub(r"\d{4}-\d{2}-\d{2}", "", body))
                if m:
                    u = re.search(r"(x10\^9/L|g/dL|years)", body)
                    hit = {"value": float(m.group(1)) if "." in m.group(1) else int(m.group(1)),
                           "unit": u.group(1) if u else None, "observed_at": date.group(0) if date else None}
            if hit:
                break
        out[f] = hit
    return out


class _ExItem(BaseModel):
    field: str
    found: bool
    value: str
    unit: str
    observed_at: str


class _Extracted(BaseModel):
    items: list[_ExItem]


EXTRACT_SYSTEM = """You read a synthetic medical document and copy values exactly as written. You never guess.
Return one item per requested field, in the same order. The item's "field" must be the exact key before the colon in the
request (for example "lab:platelets" or "biomarker"), not the display name. If the document does not state a value for a field, set found=false and leave value empty.
value: the number only for numeric fields (no unit), or the exact result phrase for test results (e.g. "EGFR exon 19 deletion",
"EGFR wild type"). unit: as written, or "". observed_at: the test/record date as YYYY-MM-DD if written, else ""."""


def llm_extract(text: str, fields: list[str]) -> dict[str, dict | None]:
    from .. import llm
    req = "\n".join(f"- {f}: {FIELDS[f][0]}" for f in fields)
    out = llm.structured(_Extracted, system=EXTRACT_SYSTEM, effort="low", max_tokens=2000,
                         user=f"Requested fields:\n{req}\n\nDocument:\n<<<\n{text}\n>>>")
    got = {i.field: i for i in out.items}
    if not set(fields) & set(got) and len(out.items) == len(fields):  # 키 대신 이름을 돌려준 경우: 요청 순서로 맞춤
        got = dict(zip(fields, out.items))
    res = {}
    for f in fields:
        i = got.get(f)
        if i is None or not i.found or not i.value.strip():
            res[f] = None
            continue
        v: object = i.value.strip()
        if f != "biomarker":
            try:
                v = float(v) if "." in str(v) else int(v)
            except ValueError:
                res[f] = None
                continue
        res[f] = {"value": v, "unit": i.unit.strip() or None, "observed_at": i.observed_at.strip() or None}
    return res


# ---------------------------------------------------------------------------
# LLM 글쓰기 + 저장 재사용
# ---------------------------------------------------------------------------

DOC_SYSTEM = """You write SHORT synthetic medical documents for a software demo. They are fictional; no real patient.
Rules:
1. The first line must be exactly: [가상 서류 · 실제 환자 아님]
2. Include every listed result with EXACTLY the given value, unit and date. Do not round, convert or change any value.
3. Do not add any other test result, diagnosis value or number that is not listed (dates of the document itself are fine).
4. Fields marked ABSENT must not appear at all (do not mention them).
5. Realistic hospital style (Korean headings with English test names is fine), 8-15 lines, plain text, no markdown tables."""

NARRATIVE_SYSTEM = """You write a short self-introduction note that a synthetic lung cancer patient typed into a clinical trial matching site.
It is fictional. Write in English (the offline parser reads English), 2-4 sentences, plain text.
Include exactly the listed facts with the exact numbers using these forms: "<N>-year-old", "ECOG <N>", "<Test name> <value> <unit>".
Put each lab value on its own line at the end. Do not add any other number, test, stage detail or fact. Do not mention age if told to omit it."""


class Cache:
    def __init__(self, root: Path | None):
        self.root = root
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)

    def key(self, **parts) -> str:
        return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:24]

    def get(self, k: str) -> dict | None:
        p = self.root / f"{k}.json" if self.root else None
        return json.loads(p.read_text(encoding="utf-8")) if p and p.exists() else None

    def put(self, k: str, value: dict) -> None:
        if self.root:
            (self.root / f"{k}.json").write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")


@dataclass
class Written:
    text: str
    by: str  # "llm" / "template" / "llm_cached" / "template_fallback"
    attempts: int
    problems: list[list[str]]


def write_document(patient: str, source: str, items: list[DocItem], reference_date: str, *, use_llm: bool,
                   cache: Cache, model: str = "") -> Written:
    if not use_llm:
        return Written(template_document(patient, source, items, reference_date), "template", 0, [])
    k = cache.key(kind="doc", v=PROMPT_VERSION, model=model, patient=patient, source=source, items=[asdict(i) for i in items])
    hit = cache.get(k)
    if hit:
        return Written(hit["text"], "llm_cached", hit["attempts"], hit["problems"])
    from .. import llm
    lines = [f"- {FIELDS[i.field][0]}: " + ("ABSENT (do not mention)" if i.value is None else
             f"{fmt(i.value)}{' ' + i.unit if i.unit else ''}" + (f", date {i.observed_at}" if i.observed_at else "")) for i in items]
    user = (f"Document type: {SOURCES.get(source, source)}\nSynthetic patient id: {patient}\nDocument date: {reference_date}\n"
            f"Results:\n" + "\n".join(lines))
    problems = []
    for attempt in range(1, 4):
        text = llm.plain(system=DOC_SYSTEM, user=user + ("" if attempt == 1 else f"\n\nYour previous draft had problems: {problems[-1]}. Fix them."),
                         effort="low", max_tokens=1500).strip()
        p = validate_document(text, items)
        problems.append(p)
        if not p:
            cache.put(k, {"text": text, "attempts": attempt, "problems": problems})
            return Written(text, "llm", attempt, problems)
    return Written(template_document(patient, source, items, reference_date), "template_fallback", 3, problems)


def write_narrative(patient: str, public: list[DocItem], hide: set[str], hidden_truth: list[DocItem], *, use_llm: bool,
                    cache: Cache, model: str = "") -> Written:
    if not use_llm:
        return Written(template_narrative(public, hide), "template", 0, [])
    k = cache.key(kind="narrative", v=PROMPT_VERSION, model=model, patient=patient, public=[asdict(i) for i in public], hide=sorted(hide))
    hit = cache.get(k)
    if hit:
        return Written(hit["text"], "llm_cached", hit["attempts"], hit["problems"])
    from .. import llm
    facts = [f"- {FIELDS[i.field][0]}: {fmt(i.value)}{' ' + i.unit if i.unit else ''}" + (" (patient's own report)" if i.scenario == "self_report" else "")
             for i in public if i.field not in hide]
    user = ("Facts to include:\n" + "\n".join(facts) + ("\nOmit the age." if "age" in hide else "")
            + "\nIf the same item appears twice, mention the chart value first and the patient's own report last.")
    problems = []
    for attempt in range(1, 4):
        text = llm.plain(system=NARRATIVE_SYSTEM, user=user + ("" if attempt == 1 else f"\n\nYour previous draft had problems: {problems[-1]}. Fix them."),
                         effort="low", max_tokens=800).strip()
        p = validate_narrative(text, public, hide, hidden_truth)
        problems.append(p)
        if not p:
            cache.put(k, {"text": text, "attempts": attempt, "problems": problems})
            return Written(text, "llm", attempt, problems)
    return Written(template_narrative(public, hide), "template_fallback", 3, problems)


__all__ = ["DocItem", "Cache", "Written", "write_document", "write_narrative", "template_document", "template_narrative",
           "template_extract", "llm_extract", "validate_document", "validate_narrative", "FIELDS", "SOURCES", "WATERMARK",
           "PROMPT_VERSION"]
