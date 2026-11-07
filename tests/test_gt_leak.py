"""누출 검사: 세트 6 문장 복사, 엔진 출력 필드, 블라인딩 증명 없음, FIXTURE 표시."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gt_leak_check  # noqa: E402
from gt_lib.leak import build_corpus, check_set, normalise  # noqa: E402

FX = ROOT / "tests" / "data" / "gt_fixture"
H6 = ROOT / "docs" / "dev" / "m_holdout_6_cases.json"


@pytest.fixture(scope="module")
def corpus():
    return build_corpus()


def _fixture():
    return json.loads((FX / "FIXTURE_ONLY_g_u.json").read_text(encoding="utf-8"))


def _h6_conflict(i):
    return json.loads(H6.read_text(encoding="utf-8"))["conflict"][i]


def _with_text(text, quote):
    d = _fixture()
    d["items"][1]["input"].update(text=text, quote=quote)
    return d


def _overlaps(refusals):
    return [r for r in refusals if r["kind"] == "text_overlap"]


def test_corpus_includes_holdout_6_and_regression_sets(corpus):
    sources = {e.source for e in corpus.entries}
    assert "docs/dev/m_holdout_6_cases.json" in sources
    assert "docs/dev/m_holdout_6_labs.json" in sources
    assert "docs/dev/specimen_holdout_5.json" in sources
    assert "docs/dev/value_context_holdout_4.json" in sources
    assert "tests/data/value_context_regression.json" in sources
    assert not any("gt_fixture" in s or "test_gt_" in s for s in sources)


def test_clean_fixture_passes_only_with_allow_fixture(corpus):
    assert check_set(_fixture(), corpus, allow_fixture=True) == []
    kinds = {r["kind"] for r in check_set(_fixture(), corpus)}
    assert kinds == {"fixture"}


def test_exact_copy_from_holdout_6_refused(corpus):
    c = _h6_conflict(2)
    hits = _overlaps(check_set(_with_text(c["text"], c["quote"]), corpus, allow_fixture=True))
    assert hits and hits[0]["detail"]["corpus_source"] == "docs/dev/m_holdout_6_cases.json"
    assert hits[0]["detail"]["reason"] == "exact_after_normalisation"


def test_copy_after_case_whitespace_punctuation_change_refused(corpus):
    c = _h6_conflict(6)
    text = "  " + c["text"].upper().replace(" ", "   ").replace(",", " ;") + " "
    assert text != c["text"] and normalise(text) == normalise(c["text"])
    hits = _overlaps(check_set(_with_text(text, text.strip()[:10]), corpus, allow_fixture=True))
    assert any(h["detail"]["corpus_source"] == "docs/dev/m_holdout_6_cases.json" for h in hits)


def test_copied_sentence_embedded_in_new_text_refused(corpus):
    c = _h6_conflict(4)
    text = "Seen in clinic today with her son, who translated. " + c["text"] + " Plan reviewed with the team."
    hits = _overlaps(check_set(_with_text(text, "Seen in clinic today"), corpus, allow_fixture=True))
    assert any(h["detail"]["corpus_source"] == "docs/dev/m_holdout_6_cases.json" for h in hits)


def test_lightly_edited_copy_refused_by_token_overlap(corpus):
    c = _h6_conflict(0)  # 긴 영어 문장: 한 단어만 바꿈
    text = c["text"].replace("former smoker", "ex smoker")
    hits = _overlaps(check_set(_with_text(text, text[:12]), corpus, allow_fixture=True))
    assert any(h["detail"]["corpus_source"] == "docs/dev/m_holdout_6_cases.json" for h in hits)


def test_unrelated_new_sentence_not_refused(corpus):
    text = "Grandmother's quilt pattern was discussed at length; the patient prefers blue and yellow squares."
    assert _overlaps(check_set(_with_text(text, "Grandmother"), corpus, allow_fixture=True)) == []


def test_engine_output_fields_refused(corpus):
    d = _fixture()
    d["items"][0]["input"]["directness"] = "conflicting"
    d["items"][1]["engine_result"] = {"x": 1}
    d["items"][2]["final"]["note"] = "CONFIRMED_ELIGIBLE"
    r = [x for x in check_set(d, corpus, allow_fixture=True) if x["kind"] == "engine_field"]
    details = " ".join(x["detail"] for x in r)
    assert "directness" in details and "engine_result" in details and "CONFIRMED_ELIGIBLE" in details


def test_set_without_blinding_refused(corpus):
    d = _fixture()
    d.pop("blinding")
    assert any(r["kind"] == "no_blinding" for r in check_set(d, corpus, allow_fixture=True))
    d["blinding"] = None
    assert any(r["kind"] == "no_blinding" for r in check_set(d, corpus, allow_fixture=True))


def test_cli_exit_codes(tmp_path):
    ok = tmp_path / "ok.json"
    ok.write_text(json.dumps(_fixture(), ensure_ascii=False), encoding="utf-8")
    assert gt_leak_check.main([str(ok), "--allow-fixture"]) == 0
    assert gt_leak_check.main([str(ok)]) == 1
    c = _h6_conflict(1)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(_with_text(c["text"], c["quote"]), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "r.json"
    assert gt_leak_check.main([str(bad), "--allow-fixture", "--json", str(out)]) == 1
    assert json.loads(out.read_text(encoding="utf-8"))[str(bad)]
