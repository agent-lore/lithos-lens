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


def _class_names_in(css: str) -> set[str]:
    """Every class name mentioned in CODE, given the text of a stylesheet.

    Takes the text rather than reading the file so the tests below exercise
    THIS function — a regression test that re-implements the stripping proves
    only that the copy in the test works.

    Comments and quoted strings are the two places a class name can appear
    without defining anything, so both go. ORDER IS LOAD-BEARING and not
    interchangeable: comments in this stylesheet contain apostrophes ("a gate's
    own chrome"), and running the string strip first makes one of those open a
    quote that swallows real selectors until the next apostrophe — 148 names
    collapse to 83. Comments first, then strings.

    Deliberately not a selector parser. A name used only in a descendant or
    compound selector (`.gate-group-title span`) IS defined for this purpose,
    and a prelude parser would additionally have to model the one `@media`
    block or lose every rule nested inside it.
    """
    css = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)
    css = re.sub(r'"[^"]*"|\'[^\']*\'', " ", css)
    return set(re.findall(r"\.([A-Za-z][A-Za-z0-9_-]*)", css))


def _defined_classes() -> set[str]:
    """Every class name the served stylesheet defines."""
    return _class_names_in(STYLESHEET.read_text())


def _value_tokens(value: str) -> set[str]:
    """The literal class names in one class attribute, expressions blanked.

    Splits on the marker rather than discarding any token that contains it,
    because a literal can ABUT an expression and still be a complete class:
    `class="epic-chip{{ " epic-chip-selected" if ... }}"` carries `epic-chip`,
    and `class="produced-by-chip{% if %} ...{% endif %}"` carries
    `produced-by-chip`. Treating the whole token as dynamic dropped both, so
    two real classes were never checked.

    A token that touches an expression cannot be read whole, and there is one
    documented shape for that: this codebase builds a dynamic name as
    `prefix-{{ ... }}` — `badge-`, `attention-chip-`, `blocker-chip-`,
    `note-status-`. So a touching token ending in `-` is a prefix and is
    skipped; anything else is taken as complete. If that ever guesses wrong the
    guard fails loudly and the fix is to write the dynamic name in the `-`
    form, which is the right way round for a check whose job is exhaustiveness.
    """
    tokens: set[str] = set()
    runs = value.split(_DYNAMIC_MARK)
    for index, run in enumerate(runs):
        parts = run.split()
        if not parts:
            continue
        glued_left = index > 0 and not run[:1].isspace()
        glued_right = index < len(runs) - 1 and not run[-1:].isspace()
        for position, token in enumerate(parts):
            touches = (glued_left and position == 0) or (
                glued_right and position == len(parts) - 1
            )
            if touches and token.endswith("-"):
                continue  # a dynamic name's prefix, not a class
            tokens.add(token)
    return tokens


def _emitted_classes() -> dict[str, set[str]]:
    """Literal class tokens per template, keyed by repo-relative path."""
    emitted: dict[str, set[str]] = {}
    for template in sorted(TEMPLATES.rglob("*.html")):
        tokens: set[str] = set()
        static = _JINJA.sub(_DYNAMIC_MARK, template.read_text())
        for value in re.findall(r'class="([^"]*)"', static):
            tokens.update(_value_tokens(value))
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


def test_a_comment_only_mention_is_not_a_definition() -> None:
    """The stylesheet's own comments name the classes they relate to, so a scan
    of raw text let prose stand in for a rule — deleting `.chip` outright left
    the guard still calling it defined, because a comment two hundred lines
    away mentioned it.

    Driven through :func:`_class_names_in`, the function the production path
    uses. An earlier version of this test re-implemented the comment strip on
    its own text, so reverting the real reader would not have failed it.
    """
    css = STYLESHEET.read_text()
    assert "/*" in css, "no comments left to scan — this test would be vacuous"

    invented = "definitely-not-a-real-class"
    commented = css.replace("/*", f"/* .{invented} ", 1)

    assert f".{invented}" in commented  # it really is in the text...
    assert invented not in _class_names_in(commented)  # ...and still not defined


def test_stripping_comments_before_strings_keeps_real_selectors() -> None:
    """The two strips inside :func:`_class_names_in` are order-coupled, which
    is invisible from the outside and worth pinning.

    Comments here contain apostrophes ("a gate's own chrome"). Strip strings
    first and one of those opens a quote that runs on until the next
    apostrophe, blanking every selector in between — the count drops by
    roughly half, and the guard then reports dozens of defined classes as
    missing. This asserts the real reader keeps them.
    """
    css = STYLESHEET.read_text()
    assert re.search(r"/\*[^*]*'", css), "no apostrophe-bearing comment left to trip on"

    names = _class_names_in(css)
    wrong_order = set(
        re.findall(
            r"\.([A-Za-z][A-Za-z0-9_-]*)",
            re.sub(
                r"/\*.*?\*/", " ", re.sub(r'"[^"]*"|\'[^\']*\'', " ", css), flags=re.S
            ),
        )
    )

    assert len(names) > len(wrong_order)
    # And the real reader keeps the classes this PR is about.
    assert {"chip", "related-panel", "note-status"} <= names


def test_a_literal_class_abutting_an_expression_is_still_read() -> None:
    """Two live examples the first version of this scan silently dropped, and
    the prefix form it must keep dropping.

    `epic-chip` (tasks/dashboard.html) and `produced-by-chip` (note.html) are
    complete class names written flush against a Jinja expression that supplies
    an optional second class. Blanking the expression to a marker and rejecting
    any token containing it removed the literal along with the expression.
    """
    emitted = set().union(*_emitted_classes().values())

    assert "epic-chip" in emitted
    assert "produced-by-chip" in emitted
    assert "produced-by-chip-record" in emitted
    # ...while a dynamic name's prefix is still not mistaken for a class.
    assert not {token for token in emitted if token.endswith("-")}


def test_the_prefix_rule_is_applied_only_where_a_name_is_actually_cut() -> None:
    """`badge` and `badge-` come out of the same attribute; only the one the
    expression cuts is a prefix."""
    tokens = _value_tokens(f"badge badge-{_DYNAMIC_MARK}")

    assert tokens == {"badge"}
    # A hyphen elsewhere in the value is untouched — nothing is cutting it.
    assert _value_tokens("badge badge-open") == {"badge", "badge-open"}
