"""Docs↔config env-var prefix guardrail.

``config.py`` reads a set of environment-variable overrides, and the README
documents them in the "Environment variable overrides" table. Two invariants
must hold, or an operator's `LITHOS_LENS_*` override silently does nothing:

1. Every env var the code reads uses the shared ``LITHOS_LENS_`` prefix.
2. The README table lists exactly the env vars the code actually reads — no
   documented-but-dead entries, no read-but-undocumented ones.

Both are checked by static analysis (AST for the code, table parsing for the
docs) so the test never imports the runtime package or depends on the ambient
environment.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PY = REPO_ROOT / "src" / "lithos_lens" / "config.py"
README = REPO_ROOT / "README.md"

# Shared prefix for every env override (root package "lithos_lens", uppercased).
EXPECTED_PREFIX = "LITHOS_LENS_"

# Rows in the README "Environment variable overrides" table start with the env
# var name in backticks, e.g. ``| `LITHOS_LENS_CONFIG` | ... |``.
_README_ROW = re.compile(r"^\|\s*`([A-Z][A-Z0-9_]*)`\s*\|")


def _config_env_vars() -> set[str]:
    """Env-var names read via ``os.environ.get(...)`` in config.py (by AST)."""
    tree = ast.parse(CONFIG_PY.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match os.environ.get("NAME", ...) — Attribute .get on os.environ.
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "environ"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "os"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names.add(node.args[0].value)
    return names


def _documented_env_vars() -> set[str]:
    """Env vars listed in the README env-var-overrides table."""
    documented: set[str] = set()
    for line in README.read_text(encoding="utf-8").splitlines():
        match = _README_ROW.match(line)
        if match and match.group(1).startswith(EXPECTED_PREFIX):
            documented.add(match.group(1))
    return documented


def test_config_env_vars_use_expected_prefix() -> None:
    env_vars = _config_env_vars()
    # Guard against a silently broken extractor vacuously passing.
    assert env_vars, "no os.environ.get() reads found in config.py"
    offenders = {name for name in env_vars if not name.startswith(EXPECTED_PREFIX)}
    assert not offenders, (
        f"config.py reads env vars without the {EXPECTED_PREFIX!r} prefix: "
        f"{sorted(offenders)}"
    )


def test_readme_documents_exactly_config_env_vars() -> None:
    code = _config_env_vars()
    docs = _documented_env_vars()
    undocumented = code - docs
    dead = docs - code
    assert not undocumented, (
        "config.py reads env vars missing from the README overrides table: "
        f"{sorted(undocumented)}"
    )
    assert not dead, (
        "README documents env vars that config.py never reads: "
        f"{sorted(dead)}"
    )
