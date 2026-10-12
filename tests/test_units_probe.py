import pytest

from ctrec.tools.rule_engine import _norm_unit


@pytest.mark.parametrize("unit", ["/microL", "cells/microL", "cells/microliter"])
def test_microliter_spellings_match_per_ul(unit):
    assert _norm_unit(unit) == _norm_unit("/ul")
