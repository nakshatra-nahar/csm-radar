"""scripts/charts.py reads the committed evaluation; matplotlib is only needed to draw."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _charts():
    spec = importlib.util.spec_from_file_location("charts", ROOT / "scripts" / "charts.py")
    charts = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(charts)
    return charts


def test_recall_rows_follow_the_policy_order_with_their_intervals():
    ev = json.loads((ROOT / "eval" / "hearthline.json").read_text())
    rows = _charts().recall_rows(ev)
    assert [r[0] for r in rows] == ["random", "renewal_sort", "arr_sort", "rules", "model",
                                    "oracle"]
    name, point, lo, hi = rows[4]
    assert (point, lo, hi) == (ev["ci"]["recall"]["model"]["diff"],
                               ev["ci"]["recall"]["model"]["lo"],
                               ev["ci"]["recall"]["model"]["hi"])
    assert all(lo <= point <= hi for _, point, lo, hi in rows)


def test_recall_rows_refuse_an_eval_without_a_policy():
    ev = json.loads((ROOT / "eval" / "tallyworks.json").read_text())
    del ev["ci"]["recall"]["rules"]
    with pytest.raises(KeyError):
        _charts().recall_rows(ev)
