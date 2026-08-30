"""Every class a template emits has a rule in the one stylesheet we serve.

The two sides are otherwise unconnected: a template can emit any class and the
stylesheet can drop any rule, and every other test in the suite passes either
way. These are markup-and-CSS defects that only rendered output shows, and the
e2e capture is reviewed by eye — so they accumulate silently. When this guard
was written the backlog was 29 classes, and three of them had arrived in the
previous day's three merged slices.

WHAT THIS PROVES, stated as narrowly as it deserves: that the rule is PRESENT,
not that the page looks right. It would pass on a stylesheet whose values are
wrong — `.healthy-stripe`'s missing inset (task dafa6221) is exactly that shape
and this test does not see it. The distinction it does catch is between a class
with no rule at all and a class with a bad one, and only the first is a silent
no-op.

NO ALLOWLIST, deliberately. A name in `class=` is a styling hook by definition;
a hook that exists only for tests or scripts belongs in `data-*`, which this
codebase already uses everywhere for exactly that (`data-task-row`,
`data-blocker-expand`, `data-approximate-count`). Making the rule "if it is in
`class=`, it has a rule" keeps that separation honest and leaves nothing to rot
— an allowlist would become the place undefined classes go to be forgotten.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "src/lithos_lens/templates"
STYLESHEET = REPO_ROOT / "src/lithos_lens/static/lens.css"

# A class token built by Jinja (`badge-{{ status }}`, `{{ tag_chip_class(t) }}`)
# is not a literal this scan can check, so it is skipped rather than guessed at.
# Expressions are blanked to a sentinel BEFORE the attributes are found, for
# two reasons. They contain spaces, so splitting first turns `badge-{{ x.y }}`
# into `badge-{{`, `x.y`, `}}` and reports `x.y` as an undefined class. And they
# contain QUOTES — `class="epic-chip{{ " epic-chip-selected" if ... }}"` — so an
# attribute regex run first stops at the inner quote and captures a fragment.
_JINJA = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.S)
_DYNAMIC_MARK = "\x00"


def _defined_classes() -> set[str]:
    """Every class name the stylesheet mentions, wherever it appears.

    Deliberately not a selector parser: a name used only in a descendant or
    compound selector (`.gate-group-title span`) is still defined for this
    purpose, and matching on the bare token keeps the check from claiming a
    rule is missing when it is merely nested.
    """
    return set(re.findall(r"\.([A-Za-z][A-Za-z0-9_-]*)", STYLESHEET.read_text()))


def _emitted_classes() -> dict[str, set[str]]:
    """Literal class tokens per template, keyed by repo-relative path."""
    emitted: dict[str, set[str]] = {}
    for template in sorted(TEMPLATES.rglob("*.html")):
        tokens: set[str] = set()
        static = _JINJA.sub(_DYNAMIC_MARK, template.read_text())
        for value in re.findall(r'class="([^"]*)"', static):
            tokens.update(
                token for token in value.split() if token and _DYNAMIC_MARK not in token
            )
        if tokens:
            emitted[str(template.relative_to(TEMPLATES))] = tokens
    return emitted


def test_every_emitted_class_has_a_rule_in_the_served_stylesheet() -> None:
    defined = _defined_classes()
    undefined = {
        template: sorted(tokens - defined)
        for template, tokens in _emitted_classes().items()
        if tokens - defined
    }

    assert not undefined, (
        "Templates emit class names the served stylesheet never defines, so "
        "they render unstyled:\n"
        + "\n".join(
            f"  {template}: {', '.join(names)}" for template, names in undefined.items()
        )
        + "\n\nAdd a rule to src/lithos_lens/static/lens.css, or — if the name "
        "exists only as a test or script hook — move it to a `data-` attribute, "
        "which is what this codebase uses for hooks that carry no styling."
    )


def test_the_scan_actually_reads_both_sides() -> None:
    """Non-vacuity guard: an empty scan on either side would make the check
    above pass by finding nothing to compare, which is the way a test like this
    rots into decoration."""
    emitted = _emitted_classes()
    assert len(emitted) >= 5, "found almost no templates — the glob is wrong"
    assert sum(len(tokens) for tokens in emitted.values()) >= 50
    assert len(_defined_classes()) >= 50, "found almost no rules — the path is wrong"
