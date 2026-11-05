"""긴 시험의 선정·제외 기준 분할 파싱 (출력 잘림 방지). LLM은 대역만 씀."""

from ctrec import config
from ctrec.agents import criteria_parser as cp
from ctrec.schemas import ParsedTrial, Rule


def _rule(rid, kind, text, cohort=None):
    return Rule(rule_id=rid, kind=kind, text=text, category="other", field=None, operator=None, value=None, unit=None,
                time_window_days=None, structured_evaluable=False, underspecified=False, cohort=cohort)


def _out(rules, cohorts=(), notes=""):
    return ParsedTrial(trial_id="X", title="t", target_population="p", intervention_summary="i", key_conditions=[],
                       cohorts=list(cohorts), rules=rules, parsing_notes=notes)


def _trial(n_chars):
    return {"trial_id": "NCT00000001", "title": "t", "eligibility_criteria": "x" * n_chars}


def _fake(monkeypatch, outputs):
    calls = []

    def structured(model_cls, *, system, user, **kw):
        calls.append(user)
        return outputs[len(calls) - 1].model_copy(deep=True)

    monkeypatch.setattr(cp.llm, "structured", structured)
    return calls


def test_short_trial_keeps_single_call(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    calls = _fake(monkeypatch, [_out([_rule("I1", "inclusion", "a"), _rule("E1", "exclusion", "b")])])
    p = cp.parse_trial(_trial(cp.SPLIT_PARSE_CHARS), use_cache=False)
    assert len(calls) == 1 and calls[0].endswith("위 임상시험의 기준을 원자 규칙으로 구조화하세요.")
    assert [r.rule_id for r in p.rules] == ["I1", "E1"] and "[선정 기준 호출]" not in p.parsing_notes


def test_long_trial_splits_and_merges(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    inc = _out([_rule("I1", "inclusion", "adult"), _rule("I2", "inclusion", "NSCLC", cohort="A"),
                _rule("E1", "exclusion", "stray exclusion")], cohorts=["A"], notes="inc note")
    exc = _out([_rule("I1", "inclusion", "stray inclusion"), _rule("E1", "exclusion", "pregnant"),
                _rule("E2", "exclusion", "HIV", cohort="B")], cohorts=["A", "B"], notes="exc note")
    calls = _fake(monkeypatch, [inc, exc])
    p = cp.parse_trial(_trial(cp.SPLIT_PARSE_CHARS + 1), use_cache=False)

    assert len(calls) == 2 and "선정(Inclusion) 기준만" in calls[0] and "제외(Exclusion) 기준만" in calls[1]
    assert [(r.rule_id, r.text) for r in p.rules] == [("I1", "adult"), ("I2", "NSCLC"), ("E1", "pregnant"),
                                                       ("E2", "HIV")]
    assert p.cohorts == ["A", "B"] and p.trial_id == "NCT00000001"
    assert "[선정 기준 호출] inc note" in p.parsing_notes and "[제외 기준 호출] exc note" in p.parsing_notes
    assert "요청하지 않은 종류의 규칙 2개를 뺌" in p.parsing_notes


def test_split_result_uses_its_own_cache_entry(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    _fake(monkeypatch, [_out([_rule("I1", "inclusion", "a")]), _out([_rule("E1", "exclusion", "b")])])
    cp.parse_trial(_trial(cp.SPLIT_PARSE_CHARS + 1), use_cache=False)
    calls = _fake(monkeypatch, [])
    p = cp.parse_trial(_trial(cp.SPLIT_PARSE_CHARS + 1))  # 캐시 적중: LLM 호출 없음
    assert calls == [] and [r.rule_id for r in p.rules] == ["I1", "E1"]
    assert len(list((tmp_path / "parsed").glob("NCT00000001_*.json"))) == 1


# ---- PR #8 리뷰: 동결 시험 제외, parse_mode 메타, 뺀 규칙을 integrity_issues에도
def test_frozen_trial_is_never_split(monkeypatch, tmp_path):
    """동결 시험 NCT06908096(6,801자)은 다시 컴파일해도 한 번 호출 (동결본과 같은 방식·캐시 키)."""
    import json

    real = json.loads((config.DATA_DIR / "trials" / "NCT06908096.json").read_text(encoding="utf-8"))
    assert len(real["eligibility_criteria"]) > cp.SPLIT_PARSE_CHARS
    assert cp.split_parse(real) is False and cp.parse_mode(real) == "single"

    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    calls = _fake(monkeypatch, [_out([_rule("I1", "inclusion", "a"), _rule("E1", "exclusion", "b")])])
    long_frozen = dict(_trial(cp.SPLIT_PARSE_CHARS + 1), trial_id="NCT06908096")
    cp.parse_trial(long_frozen, use_cache=False)
    assert len(calls) == 1 and "기준만" not in calls[0]


def test_parse_mode_is_recorded_in_store_meta(tmp_path):
    from ctrec import trial_store
    from ctrec.agents.parser_validator import ValidationReport

    for n, mode in ((cp.SPLIT_PARSE_CHARS, "single"), (cp.SPLIT_PARSE_CHARS + 1, "split")):
        t = _trial(n)
        trial_store.save(t, _out([_rule("I1", "inclusion", "a")]), {}, ValidationReport(), tmp_path)
        rec = trial_store.read_record(t["trial_id"], tmp_path)
        assert rec["parse_mode"] == mode and rec["parser_prompt_hash"] == trial_store.parser_prompt_hash()


def test_dropped_split_rules_are_integrity_issues_too(monkeypatch, tmp_path):
    from ctrec.agents import parser_validator

    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    inc = _out([_rule("I1", "inclusion", "adult"), _rule("E9", "exclusion", "stray exclusion")])
    exc = _out([_rule("I7", "inclusion", "stray inclusion"), _rule("E1", "exclusion", "pregnant")])
    _fake(monkeypatch, [inc, exc])
    trial = _trial(cp.SPLIT_PARSE_CHARS + 1)
    p = cp.parse_trial(trial, use_cache=False)

    split = [i for i in p.integrity_issues if i.startswith("split_")]
    assert len(split) == 2
    assert split[0].startswith("split_inclusion_1:") and "rule_id=E9" in split[0] and "stray exclusion" in split[0]
    assert split[1].startswith("split_exclusion_1:") and "rule_id=I7" in split[1]
    assert p.parsing_notes.count(cp.SPLIT_DROP_TAG) == 2  # 메모에도 그대로 (중복 기록 없음)

    # 캐시에서 다시 불러와도, compile_trials처럼 check_integrity를 다시 돌려도 남음
    _fake(monkeypatch, [])
    cached = cp.parse_trial(trial)
    assert [i for i in cached.integrity_issues if i.startswith("split_")] == split
    assert [i for i in cp.check_integrity(cached, trial) if i.startswith("split_")] == split
    # 저장소 검증(deterministic)에도 들어감 -> trial_store.load가 integrity_issues로 되살림
    det = parser_validator.deterministic_check(cached, trial)
    assert {"split_inclusion_1", "split_exclusion_1"} <= set(det)
