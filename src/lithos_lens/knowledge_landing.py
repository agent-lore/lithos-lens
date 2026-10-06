"""The ``/knowledge`` landing's browse view: your notes first, intake second.

Without a query the landing shows two sections (§7.1): **Your notes**, the
newest non-intake notes, then **Recent intake**, the newest intake ones. A note
is intake when it carries an ``ingested-by:*`` tag or its path starts with one
of ``[knowledge].intake_path_prefixes``; a note matching both is intake, once.
Both sections are a partition of the one newest-first ``recent_notes`` walk —
no Lithos call of their own.

The ``?namespace=`` filter is the PATH-derived namespace: ``lithos_list``
cannot filter on frontmatter ``namespace`` (ROADMAP ledger #16), so a namespace
here is a path prefix, matched on whole segments (``influx`` is ``influx/``,
never ``influx-old/``). The filter row offers each note's first path segment.

Split out on the same seam as ``knowledge_search``: one knowledge feature per
Foundation module, keeping each under the architecture line budget.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from lithos_lens.tasks import NoteSummary

# The tag every intake pipeline stamps on what it ingests (``ingested-by:influx``).
INTAKE_TAG_PREFIX = "ingested-by:"

# How many namespaces the filter row offers, most notes first. A row, not a
# picker: past this the operator types ``?namespace=`` or narrows by tag.
NAMESPACE_FACET_LIMIT = 8

# ``?section=`` values: one section on its own, the target of its heading link.
SECTION_NOTES = "notes"
SECTION_INTAKE = "intake"
SECTIONS = (SECTION_NOTES, SECTION_INTAKE)


@dataclass(frozen=True)
class NamespaceFacet:
    """One entry of the namespace filter row: a namespace and its note count."""

    namespace: str
    count: int


@dataclass(frozen=True)
class RecentLanding:
    """The browse landing as rendered: its two sections and the filter row.

    A section is ``None`` when ``?section=`` shows only the other one, and an
    empty tuple when it is shown with nothing in it.
    """

    notes: tuple[NoteSummary, ...] | None = ()
    intake: tuple[NoteSummary, ...] | None = ()
    namespaces: tuple[NamespaceFacet, ...] = ()

    @property
    def rows(self) -> tuple[NoteSummary, ...]:
        """Every rendered row, in page order (your notes, then intake)."""
        return (self.notes or ()) + (self.intake or ())


def is_intake(row: NoteSummary, intake_path_prefixes: Sequence[str]) -> bool:
    """Whether ``row`` is intake: an ``ingested-by:*`` tag, or an intake path."""
    if any(tag.startswith(INTAKE_TAG_PREFIX) for tag in row.tags):
        return True
    return row.path.startswith(tuple(intake_path_prefixes))


def normalize_namespace(raw: str) -> str:
    """The ``?namespace=`` value as matched: trimmed, without edge slashes."""
    return raw.strip().strip("/")


def namespace_path_prefix(namespace: str) -> str:
    """The path prefix a namespace filters on (``""`` for no filter)."""
    return f"{namespace}/" if namespace else ""


def path_namespace(path: str) -> str:
    """A path's namespace for the filter row: its first segment, if it has one."""
    head, sep, _ = path.partition("/")
    return head if sep else ""


def namespace_facets(
    paths: Iterable[str], *, limit: int = NAMESPACE_FACET_LIMIT
) -> tuple[NamespaceFacet, ...]:
    """The namespaces present in ``paths``, most notes first (ties by name).

    Only namespaces that occur are offered, and none is hidden for being
    common: on the live corpus the intake namespace dominates, and it is shown.
    A note at the root has no namespace and is reached through "all".
    """
    counts = Counter(ns for ns in map(path_namespace, paths) if ns)
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return tuple(NamespaceFacet(ns, count) for ns, count in ranked[:limit])


def build_recent_landing(
    rows: Iterable[NoteSummary],
    *,
    intake_path_prefixes: Sequence[str],
    namespace: str,
    section: str,
    limit: int,
) -> RecentLanding:
    """Partition the newest-first ``rows`` into the landing's two sections.

    ``rows`` is the whole (tag-filtered) corpus newest-first, so each section
    is its class's newest ``limit`` notes. The filter row counts EVERY fetched
    row, before both the section and the namespace filter: it is how the
    operator moves between namespaces, and a ``?section=`` view fetched the
    same corpus, so it offers the same row (intake's dominant namespace stays
    on "Your notes"). Only the rendered sections are selected and cut.
    """
    # "" (no namespace) is a prefix of every path.
    prefix = namespace_path_prefix(namespace)
    shown: dict[str, list[NoteSummary]] = {
        key: [] for key in SECTIONS if not section or key == section
    }
    fetched_paths: list[str] = []
    for row in rows:
        fetched_paths.append(row.path)
        kind = SECTION_INTAKE if is_intake(row, intake_path_prefixes) else SECTION_NOTES
        bucket = shown.get(kind)
        if bucket is not None and row.path.startswith(prefix) and len(bucket) < limit:
            bucket.append(row)
    return RecentLanding(
        notes=_section_rows(shown, SECTION_NOTES),
        intake=_section_rows(shown, SECTION_INTAKE),
        namespaces=namespace_facets(fetched_paths),
    )


def _section_rows(
    shown: dict[str, list[NoteSummary]], key: str
) -> tuple[NoteSummary, ...] | None:
    rows = shown.get(key)
    return None if rows is None else tuple(rows)
