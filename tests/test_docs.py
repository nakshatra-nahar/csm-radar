"""The hiring-team documents cite files, tests and functions; these checks keep the citations
true as the code moves. No database needed."""

import ast
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ["README.md", "docs/decisions.md", "docs/research/sources.md"]
LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")  # markdown links and images
TEST_REF = re.compile(r"(tests/test_\w+\.py)::(\w+)")
FUNC_REF = re.compile(r"`(\w+)` \((radar/[\w/]+\.py):(\d+)\)")


def _text(doc: str) -> str:
    return (ROOT / doc).read_text()


def _params_table() -> str:
    spec = importlib.util.spec_from_file_location("params_table",
                                                  ROOT / "scripts" / "params_table.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.table()


@pytest.mark.parametrize("doc", DOCS)
def test_relative_links_resolve(doc):
    base = (ROOT / doc).parent
    missing = [t for t in LINK.findall(_text(doc))
               if "://" not in t and not t.startswith(("mailto:", "<"))
               and not (base / t).exists()]
    assert missing == []


@pytest.mark.parametrize("doc", DOCS)
def test_cited_tests_exist(doc):
    missing = []
    for path, name in TEST_REF.findall(_text(doc)):
        tree = ast.parse((ROOT / path).read_text())
        names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        if name not in names:
            missing.append(f"{path}::{name}")
    assert missing == []


@pytest.mark.parametrize("doc", DOCS)
def test_cited_functions_sit_on_their_lines(doc):
    wrong = []
    for name, path, line in FUNC_REF.findall(_text(doc)):
        lines = (ROOT / path).read_text().splitlines()
        n = int(line)
        if n > len(lines) or not re.match(rf"\s*(async )?(def|class) {name}\b", lines[n - 1]):
            wrong.append(f"{name} {path}:{line}")
    assert wrong == []


def test_readme_generator_table_is_printed_from_params():
    assert _params_table() in _text("README.md")
