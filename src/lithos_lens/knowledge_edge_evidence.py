"""Why a typed edge exists: its parsed evidence and its provenance.

The related panel's typed-edge rows (§5.7) carry a "why?" disclosure built from
three of the twelve keys every ``lithos_related`` edge row has. ``evidence`` is
a JSON string or null; every LLM-inferred edge stores it as
``{"rationale": ..., "model": ..., "confidence": ...}`` (lithos
``src/lithos/lcma/edge_inference.py`` at 0.6.0 @ d2c49bb). ``provenance_type``
and ``provenance_actor`` say how the edge came to be — the only explanation a
reinforced or frontmatter edge has, since neither stores a rationale.

Pure parsing, kept out of ``knowledge`` (which holds the panel's view models)
so that module stays under the god-module ceiling.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Plain-language provenance for the values the live corpus carries (K2 PRD D7).
# ``inferred`` names its actor; the other two are the same act whoever did it.
# Any other value is shown as stored, with its actor.
_PROVENANCE_PHRASES = {
    "consolidation": "reinforced by citation",
    "frontmatter": "declared in frontmatter",
}


@dataclass(frozen=True)
class EdgeEvidence:
    """An edge's parsed ``evidence``: the three known keys, or the raw text.

    ``raw`` is set only when the string could not be read as a JSON object
    (or one of its values could not be converted); it is shown as escaped text
    so the reader still sees what was stored.
    """

    rationale: str = ""
    model: str = ""
    confidence: float | None = None
    raw: str = ""

    @property
    def is_empty(self) -> bool:
        """A decoded object carrying none of the three keys: nothing to show."""
        return self == EdgeEvidence()


@dataclass(frozen=True)
class EdgeWhy:
    """What the "why?" disclosure on a typed-edge row shows."""

    provenance: str = ""
    evidence: EdgeEvidence | None = None


def parse_edge_evidence(value: Any) -> EdgeEvidence | None:
    """Parse an edge row's ``evidence``; ``None`` when it is null (or blank).

    ``json.loads`` on the string: a dict yields whichever of ``rationale`` /
    ``model`` / ``confidence`` it carries (missing or mistyped ones omitted, so
    ``{}`` yields an empty projection). Any failure — invalid or too deeply
    nested JSON, a non-object, a ``confidence`` number no float holds
    (``1e400``, ``NaN``) — falls back to the raw string.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        # RecursionError: nesting deeper than the decoder's limit is a decode
        # failure too, and must not fail the panel the row sits in.
        return EdgeEvidence(raw=value)
    if not isinstance(parsed, dict):
        return EdgeEvidence(raw=value)
    confidence = parsed.get("confidence")
    if _is_number(confidence) and number_or_none(confidence) is None:
        return EdgeEvidence(raw=value)
    return EdgeEvidence(
        rationale=_text(parsed.get("rationale")),
        model=_text(parsed.get("model")),
        confidence=number_or_none(confidence),
    )


def provenance_label(provenance_type: Any, provenance_actor: Any) -> str:
    """The plain-language provenance line, e.g. "inferred by lithos-enrich".

    ``""`` when the row names neither a type nor an actor.
    """
    kind = _text(provenance_type)
    actor = _text(provenance_actor)
    if kind in _PROVENANCE_PHRASES:
        return _PROVENANCE_PHRASES[kind]
    if kind and actor:
        return f"{kind} by {actor}"
    if actor:
        return f"recorded by {actor}"
    return kind


def edge_why(row: Mapping[str, Any]) -> EdgeWhy | None:
    """The disclosure for one raw edge row; ``None`` when it would be empty."""
    why = EdgeWhy(
        provenance=provenance_label(
            row.get("provenance_type"), row.get("provenance_actor")
        ),
        evidence=parse_edge_evidence(row.get("evidence")),
    )
    if why.evidence is not None and why.evidence.is_empty:
        why = EdgeWhy(provenance=why.provenance)
    return why if why.provenance or why.evidence else None


def number_or_none(value: Any) -> float | None:
    """A finite JSON number (not a bool) as a float; anything else is ``None``.

    Shared with ``knowledge``'s edge-row ``weight``, the same kind of column.
    An integer too large for a float (``float`` raises) is ``None`` too, so one
    odd row never fails the panel it sits in.
    """
    if not _is_number(value):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
