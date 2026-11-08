"""누출 검사 (D 2.2, 2.6).

거부 사유:
1. text_overlap: GT 문장이 기존 held-out·실패 세트(세트 1-6, 회귀 세트) 또는 테스트 고정 문장과
   정규화 후 같거나, 한쪽이 다른 쪽에 포함되거나, 토큰 겹침이 높음.
2. engine_field: 항목에 엔진 출력 필드(directness, match_status, CONFIRMED_* 상태 등)가 있음.
3. no_blinding: 세트에 블라인딩 증명이 없음.
4. fixture: FIXTURE_ONLY 표시가 있는 세트·문장 (allow_fixture일 때만 통과. 도구 테스트 전용).

입력은 원시 JSON(dict)입니다. 스키마 검증과 독립으로 동작해, 스키마가 느슨해져도 이 검사는 남습니다.
"""

from __future__ import annotations

import ast
import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import REPO_ROOT
from .models import FIXTURE_MARKER

# 기존 실패·held-out·회귀 세트. 독립 평가 자료로 재사용 금지.
DEFAULT_CORPUS_GLOBS = [
    "docs/dev/*holdout*.json",
    "docs/dev/m_holdout_6_*.json",
    "docs/dev/*.json",
    "tests/data/**/*.json",
    "eval/**/*.json",
]
# 테스트 코드 안의 문자열 상수 (고정 문장). GT 도구 자신의 테스트·가짜 데이터는 제외 (FIXTURE 표시로 따로 거부).
DEFAULT_TEST_GLOBS = ["tests/**/*.py"]
EXCLUDE_PARTS = ("gt_fixture",)
EXCLUDE_NAME_PREFIX = ("test_gt_",)

# 엔진 출력 필드. 키 이름 (소문자 비교).
ENGINE_KEYS = {
    "directness", "validation_flags", "match_status", "matcher_status", "engine_status", "engine_output",
    "engine_result", "engine_verdict", "engine_label", "pair_status_engine", "confirmed_status", "verdict",
    "decision_record", "result_hash", "inspector", "inspector_view", "grounding", "grounding_reason",
    "rule_engine", "passes", "assessment", "assessments", "deciding_criteria", "hold_reason",
}
# 블라인딩 증명의 필드 (엔진 출력이 아님)
ATTESTATION_KEYS = {"engine_fields_absent", "labelers_saw_engine_output", "adjudicator_saw_engine_output"}
# 엔진 쌍 상태 값. GT는 ELIGIBLE/INELIGIBLE/NOT_DETERMINABLE/NEEDS_CLINICIAN만 씀.
ENGINE_VALUES = {
    "CONFIRMED_ELIGIBLE", "CONFIRMED_INELIGIBLE", "VERIFICATION_PENDING", "INFO_MISSING", "EXPERT_REVIEW",
    "BLOCKED_FAILURE", "NOT_EVALUATED",
}
# 자유 텍스트(메모·사유·출처 설명 등) 안의 엔진 상태 문자열 (D-3). 값 전체가 같을 때만 보던 검사를 보강합니다.
# GT 어휘(ELIGIBLE, INELIGIBLE 단독)와 띄어 쓴 일반 영어("expert review", "not evaluated")는 잡지 않습니다.
ENGINE_TEXT_PATTERNS = [
    re.compile(r"confirmed[_\s-]?(?:in)?eligible", re.I),                    # CONFIRMED_ELIGIBLE, confirmed eligible
    re.compile(r"confirmed[_-]unverified", re.I),
    re.compile(r"(?:verification[_-]?pending|info[_-]?missing|expert[_-]?review|blocked[_-]?failure|"
               r"not[_-]?evaluated)", re.I),                               # VERIFICATION_PENDING, ExpertReview
    re.compile(r"likely[_-](?:in)?eligible|\bneeds_review\b", re.I),        # Session match_status 값
    re.compile(r"(?:engine|match|matcher)[_-]?(?:status|verdict|output|result)", re.I),  # EngineStatus, match_status
    re.compile(r"\bPairStatus\b"),                                         # 엔진 enum 클래스 이름
    re.compile(r"\b[A-Z][A-Za-z]*(?:Status|Verdict)\.[A-Z][A-Z_]+"),       # MatchStatus.ELIGIBLE 같은 enum 표기
]

MIN_SEG_TOKENS = 5        # 유사도(겹침) 비교 대상 조각의 최소 토큰 수
MIN_EXACT_SEG_TOKENS = 4  # 정규화 후 완전 일치를 보는 조각의 최소 토큰 수 (짧은 상투 문장 오탐 방지)
MIN_EXACT_SEG_CHARS = 12
MIN_SUBSTR_CHARS = 25
JACCARD_T = 0.8
CONTAIN_T = 0.9
CONTAIN_MIN_TOKENS = 8
MAX_DF_FOR_INDEX = 400

_TOKEN = re.compile(r"\w+", re.UNICODE)
_SPLIT = re.compile(r"(?<=[.!?。])\s+|\n+|\s/\s|;\s")


def normalise(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def tokens(s: str) -> list[str]:
    return _TOKEN.findall(normalise(s))


def segments(text: str) -> list[str]:
    """전체 문장 + 문장 단위 조각 (정규화된 문자열)."""
    out = [normalise(text)]
    for part in _SPLIT.split(text):
        n = normalise(part)
        if n and n != out[0] and (len(n.split()) >= MIN_EXACT_SEG_TOKENS and len(n) >= MIN_EXACT_SEG_CHARS):
            out.append(n)
    return [o for o in out if o]


@dataclass
class CorpusEntry:
    source: str
    text: str


@dataclass
class Corpus:
    entries: list[CorpusEntry] = field(default_factory=list)
    _segs: list[tuple[int, str, frozenset]] = field(default_factory=list)
    _exact: dict = field(default_factory=dict)
    _index: dict = field(default_factory=lambda: defaultdict(list))

    def add(self, source: str, text: str):
        if not isinstance(text, str) or len(text.strip()) < 15:
            return
        idx = len(self.entries)
        self.entries.append(CorpusEntry(source, text))
        for seg in segments(text):
            toks = frozenset(seg.split())
            if len(toks) < 3:
                continue
            sid = len(self._segs)
            self._segs.append((idx, seg, toks))
            self._exact.setdefault(seg, sid)
            for t in toks:
                self._index[t].append(sid)

    def candidates(self, toks: frozenset) -> list[int]:
        c = Counter()
        for t in toks:
            post = self._index.get(t)
            if post and len(post) <= MAX_DF_FOR_INDEX:
                c.update(post)
        need = max(2, len(toks) // 3)
        return [sid for sid, k in c.items() if k >= need]

    def match(self, text: str) -> list[dict]:
        hits = {}
        for seg in segments(text):
            toks = frozenset(seg.split())
            if seg in self._exact:
                sid = self._exact[seg]
                hits.setdefault(self._segs[sid][0], ("exact_after_normalisation", 1.0, seg))
                continue
            if len(toks) < MIN_SEG_TOKENS:
                continue
            for sid in self.candidates(toks):
                eidx, cseg, ctoks = self._segs[sid]
                if eidx in hits:
                    continue
                reason = None
                score = len(toks & ctoks) / len(toks | ctoks)
                small = min(len(toks), len(ctoks))
                contain = len(toks & ctoks) / small if small else 0.0
                if (len(cseg) >= MIN_SUBSTR_CHARS and cseg in seg) or (len(seg) >= MIN_SUBSTR_CHARS and seg in cseg):
                    reason, score = "substring_after_normalisation", 1.0
                elif score >= JACCARD_T and len(ctoks) >= MIN_SEG_TOKENS:
                    reason = "token_jaccard"
                elif small >= CONTAIN_MIN_TOKENS and contain >= CONTAIN_T:
                    reason, score = "token_containment", contain
                if reason:
                    hits[eidx] = (reason, round(score, 3), seg)
        return [{"corpus_source": self.entries[i].source, "corpus_text": self.entries[i].text[:200],
                 "reason": r, "score": s, "gt_segment": g[:200]} for i, (r, s, g) in hits.items()]


def _json_strings(obj, out: list[str]):
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _json_strings(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _json_strings(v, out)


def _excluded(p: Path) -> bool:
    return any(x in p.parts or x in p.name for x in EXCLUDE_PARTS) or p.name.startswith(EXCLUDE_NAME_PREFIX)


def build_corpus(root: Path | None = None, extra: list[Path] | None = None) -> Corpus:
    root = root or REPO_ROOT
    corpus = Corpus()
    seen = set()
    for g in DEFAULT_CORPUS_GLOBS:
        for p in sorted(root.glob(g)):
            if p in seen or _excluded(p):
                continue
            seen.add(p)
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            strs: list[str] = []
            _json_strings(data, strs)
            rel = p.relative_to(root).as_posix()
            for s in strs:
                corpus.add(rel, s)
    for g in DEFAULT_TEST_GLOBS:
        for p in sorted(root.glob(g)):
            if p in seen or _excluded(p):
                continue
            seen.add(p)
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            rel = p.relative_to(root).as_posix()
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) >= 25:
                    corpus.add(rel, node.value)
    for p in extra or []:
        p = Path(p)
        files = sorted(p.rglob("*")) if p.is_dir() else [p]
        for f in files:
            if f.suffix == ".json":
                strs = []
                _json_strings(json.loads(f.read_text(encoding="utf-8")), strs)
            elif f.is_file():
                strs = f.read_text(encoding="utf-8").splitlines()
            else:
                continue
            for s in strs:
                corpus.add(str(f), s)
    return corpus


def _engine_fields(obj, path="") -> list[str]:
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = str(k).lower()
            if kl not in ATTESTATION_KEYS and (kl in ENGINE_KEYS or kl.startswith("engine_")):
                found.append(f"{path}.{k}")
            found += _engine_fields(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found += _engine_fields(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        if obj.strip() in ENGINE_VALUES:
            found.append(f"{path}={obj.strip()}")
        else:
            for pat in ENGINE_TEXT_PATTERNS:
                m = pat.search(obj)
                if m:
                    found.append(f"{path}~{m.group(0)}")
                    break
    return found


def _item_texts(item: dict) -> list[str]:
    # 기준 문장(G-U의 criterion)은 시험 사이에 원래 반복되므로 검사하지 않습니다. 사례 원문과 G-C 기준 원문만 봅니다.
    inp = item.get("input") or {}
    keys = ("text", "criterion_text")
    return [inp[k] for k in keys if isinstance(inp.get(k), str)]


def check_set(raw: dict, corpus: Corpus, *, allow_fixture: bool = False) -> list[dict]:
    """반환: 거부 목록. 비어 있으면 통과."""
    refusals = []
    set_id = raw.get("set_id", "?")
    fixture = bool(raw.get("fixture_only")) or FIXTURE_MARKER in str(set_id)
    if fixture and not allow_fixture:
        refusals.append({"item_id": None, "kind": "fixture", "detail": f"{set_id}: FIXTURE_ONLY 세트는 GT가 아닙니다"})
    if not raw.get("blinding"):
        refusals.append({"item_id": None, "kind": "no_blinding", "detail": f"{set_id}: 블라인딩 증명이 없습니다"})
    top = {k: v for k, v in raw.items() if k != "items"}
    for f in _engine_fields(top):
        refusals.append({"item_id": None, "kind": "engine_field", "detail": f})
    for item in raw.get("items", []):
        iid = item.get("item_id")
        for f in _engine_fields(item):
            refusals.append({"item_id": iid, "kind": "engine_field", "detail": f})
        for t in _item_texts(item):
            if FIXTURE_MARKER in t and not allow_fixture:
                refusals.append({"item_id": iid, "kind": "fixture", "detail": "FIXTURE_ONLY 문장"})
            for h in corpus.match(t):
                refusals.append({"item_id": iid, "kind": "text_overlap", "detail": h})
    return refusals
