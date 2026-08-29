"""One level of the blocker chain, loaded on demand (T1-S8).

The detail page renders level 1 eagerly (``task_detail``); every level below it
is fetched by an HTMX expander on an unfinished blocker line, one level per
interaction. This module owns what a level IS — which lines carry an expander,
where the walk stops, and what a cycle renders instead of recursing — and
nothing else: the reads themselves belong to ``task_links``.

That split is the point rather than tidiness. Three bounds have to hold at
EVERY level, not just the first, and each of them already exists exactly once:

- **Breadth.** A level's blocker set is resolved by
  :func:`~lithos_lens.task_links.load_link_page`, so it gets the same
  ``LINK_PAGE_SIZE`` first page, the same counted tail and the same
  ``link_tail.html`` markup that level 1 gets. Depth alone would not bound this
  surface: a per-level fan-out with no count bound is the identical defect one
  level down, and a second page size here would be a second thing to get wrong.
- **Duration.** Every read on the level — the edge list AND each neighbour
  ``task_get`` — carries :data:`~lithos_lens.task_links.LINK_READ_TIMEOUT_S`.
  Nothing below imposes one (``session.call_tool`` takes no timeout,
  ``SESSION_WAIT_TIMEOUT_S`` covers only session establishment, and uvicorn
  sets no request deadline), so a fragment route that called the client
  directly would inherit no deadline at all. Which is why this module issues no
  ``task_get`` of its own.
- **Depth.** :data:`BLOCKER_MAX_DEPTH` levels, counted by the chain the
  expander URL carries. Lines at the bound render no expander, so the next
  level is never REQUESTED; the loader refuses an over-deep chain before
  reading anything, so a hand-edited URL cannot ask for one either.

The chain itself — root first, ending at the task whose blockers this level
lists — is what makes the walk cycle-safe. ``blocks`` edges are agent-written
and Lithos does not forbid a cycle, so a blocker that is already an ancestor
renders §5.5.2's ``cycle: A -> B -> A`` callout and stops. It carries no
expander, which is what stops the walk rather than merely labelling it.

Carrying the chain in the URL rather than in server state is deliberate: the
fragment is an ordinary stateless GET, so it survives an auto-refresh, a
reload, and a second tab, and no session state has to be evicted.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

from lithos_lens.task_links import (
    BLOCKER_EDGE_TYPES,
    LINK_READ_TIMEOUT_S,
    LinkedTask,
    LinkPage,
    TaskLinkClient,
    incoming_targets,
    load_link_page,
    new_link_limiter,
)
from lithos_lens.tasks import SectionState

# How many levels of blocker lines the chain expands to, level 1 (the eagerly
# rendered one on the detail page) included. §5.5.2's "bounded at depth <= 5".
#
# A depth bound and a per-level page bound answer different questions and
# neither substitutes for the other: this one caps how many FETCHES a walk can
# chain, ``LINK_PAGE_SIZE`` caps how much any one of them resolves. Internal,
# not config — a safety net, not a dial, the same call as the constants in
# ``task_links``.
BLOCKER_MAX_DEPTH = 5


@dataclass(frozen=True)
class BlockerExpansion:
    """What one rendered blocker line offers below itself.

    Exactly one of the two is ever set, and both may be empty (the ordinary
    "this line ends here" case). Computed in Python rather than decided in the
    template so the four-state rule below has one testable home; the partial
    asks and renders.
    """

    expandable: bool = False
    cycle_path: tuple[str, ...] = ()
    # True when the walk between the two named tasks ran through others. They
    # are deliberately NOT named: see :func:`blocker_expansion`.
    cycle_elided: bool = False


@dataclass(frozen=True)
class BlockerLevel:
    """One level of the chain: the task walked to, and its bounded blockers.

    ``chain`` is the ancestor trail, root first, INCLUDING ``task_id`` as its
    last entry — so :attr:`depth` is just its length and the two cannot drift
    apart.

    The two refusal flags mark a level answered from the request alone, before
    any read: ``over_depth`` for a trail past the bound, ``bad_chain`` for one
    that does not describe a walk ending at this task. Both leave ``chain``
    empty — there is no coherent trail to report.
    """

    task_id: str
    chain: tuple[str, ...] = ()
    page: LinkPage = LinkPage()
    state: SectionState = SectionState.OK
    over_depth: bool = False
    bad_chain: bool = False

    @property
    def depth(self) -> int:
        return len(self.chain)

    @property
    def truncated_by_depth(self) -> bool:
        """True when the DEPTH BOUND is what stopped the walk on this level.

        Not simply "this level is the last one". A level at the bound whose
        blockers are all satisfied, unsatisfiable, unresolved or cyclic has
        nothing further to show, and announcing a depth limit over it would
        claim there is more below when the graph says there is not — the same
        over-claim the overflow tail is careful to avoid in the other
        direction. Qualified on a line that WOULD have carried an expander one
        level higher up, so the notice appears exactly when the operator is
        losing something to the bound.
        """
        return self.depth >= BLOCKER_MAX_DEPTH and any(
            _walkable(link, self.chain) for link in self.page.links
        )


def blocker_expansion(link: LinkedTask, chain: Sequence[str] = ()) -> BlockerExpansion:
    """Decide what a blocker line offers: an expander, a cycle callout, or neither.

    ``chain`` is the ancestor trail of the level ``link`` was rendered on (root
    first, ending at the task whose blockers these are). An empty chain means
    the caller is not rendering a chain at all — the provenance lists share the
    same partial — so nothing is offered.

    The order of the tests is load-bearing:

    1. **Non-blocking links get nothing.** One partial renders the blocker
       chain and both provenance directions, so this is what keeps an expander
       off a spawned follow-on.
    2. **A cycle is called out even at the depth bound.** It is information
       about the graph, not a walk, and suppressing it there would replace a
       precise "these two tasks block each other" with a generic "depth limit
       reached".
    3. **The depth bound is checked before expandability.** At the bound no
       line carries an expander, whatever its state — which is what makes the
       next level's fetch never happen rather than merely be refused.
    4. **Expandability comes from the verdicts**
       (:attr:`~lithos_lens.task_links.LinkedTask.expandable`), which states
       what each of the four link states decides and why.

    The callout NAMES only ids this render read from Lithos: ``link.task_id``
    came off the level's own edge list, and ``trail[-1]`` is the task whose
    edges were read. The trail between them arrived in the query string, so
    ``cycle_elided`` replaces it with an ellipsis rather than printing it. That
    matters because ``chain`` is anonymous client input: splicing it into the
    path let a hand-written URL put arbitrary text on the page in Lens's own
    vocabulary, and made the page assert a cycle through tasks it never read —
    a forged answer on the page whose whole job is "why can't this run?". What
    the elision costs is nothing an operator cannot see: the intermediate
    levels are still rendered on screen above this one.
    """
    if not chain or not link.blocking:
        return BlockerExpansion()
    trail = tuple(chain)
    here = trail[-1]
    if link.task_id in trail:
        # Read as "is blocked by": this line blocks the task we are on, and the
        # walk reached that task from this line. Self-blocking collapses to one
        # hop rather than naming the same task three times.
        path = (
            (here, here) if link.task_id == here else (link.task_id, here, link.task_id)
        )
        return BlockerExpansion(
            cycle_path=path,
            cycle_elided=trail.index(link.task_id) < len(trail) - 2,
        )
    if len(trail) >= BLOCKER_MAX_DEPTH or not _walkable(link, trail):
        return BlockerExpansion()
    return BlockerExpansion(expandable=True)


def _walkable(link: LinkedTask, trail: Sequence[str]) -> bool:
    """Whether this line's own blockers could be loaded — the depth bound aside.

    The one definition of "there is something below this line", shared by the
    expander decision above and by
    :attr:`BlockerLevel.truncated_by_depth`, which has to answer the same
    question about a level the bound has already stopped.
    """
    return link.blocking and link.expandable and link.task_id not in tuple(trail)


async def load_blocker_level(
    lithos: TaskLinkClient,
    task_id: str,
    chain: Sequence[str] = (),
) -> BlockerLevel:
    """Read ONE deeper level of the chain: ``task_id``'s own bounded blockers.

    ``chain`` is the ancestor trail the expander URL carried, root first. Depth
    is derived from it rather than sent alongside it, so there is no second
    number a hand-edited URL could disagree with.

    Refuses past :data:`BLOCKER_MAX_DEPTH` BEFORE issuing a read, so an
    over-deep chain costs no round trip. Every read it does issue is deadlined,
    and the neighbour statuses go through
    :func:`~lithos_lens.task_links.load_link_page` — the one bounded fan-out —
    so this level is paginated exactly as level 1 is. A failed edge read
    degrades the level to an error state rather than reading as "nothing is
    blocking this task", the same call the detail page makes.
    """
    if len(chain) > BLOCKER_MAX_DEPTH:
        return BlockerLevel(task_id=task_id, chain=(), over_depth=True)
    trail = _walked_chain(chain, task_id)
    if not trail:
        return BlockerLevel(task_id=task_id, chain=(), bad_chain=True)
    if len(trail) > BLOCKER_MAX_DEPTH:
        return BlockerLevel(task_id=task_id, chain=(), over_depth=True)
    try:
        # Deadlined for the reason the module docstring gives: nothing under
        # this call imposes one, and this level's whole render waits on it.
        # ``types``/``direction`` narrow what the server sends; the selection
        # below is still made on the endpoint ids, because ``direction`` is a
        # server-computed field that normalises to empty when absent.
        edges = await asyncio.wait_for(
            lithos.task_edge_list(
                task_id, direction="incoming", types=list(BLOCKER_EDGE_TYPES)
            ),
            LINK_READ_TIMEOUT_S,
        )
    except Exception:
        return BlockerLevel(task_id=task_id, chain=trail, state=SectionState.ERROR)
    page = await load_link_page(
        lithos,
        incoming_targets(task_id, edges, BLOCKER_EDGE_TYPES),
        # One limiter for this fragment's fan-out, exactly as one detail render
        # shares one across its pages. A fragment IS a whole render here.
        limiter=new_link_limiter(),
    )
    return BlockerLevel(task_id=task_id, chain=trail, page=page)


def _walked_chain(chain: Sequence[str], task_id: str) -> tuple[str, ...]:
    """The ancestor trail as walked, ending at ``task_id`` — or ``()`` if it cannot.

    Everything downstream reads ``chain[-1]`` as "the task this level is
    about": the depth count, the cycle test and the deeper expander URLs all
    proceed from it. So a trail that does not END at ``task_id`` is not a
    walk this level can describe, and returning it unchanged made the page
    speak from a trail whose last node was somewhere else —
    ``?chain=A&chain=B&chain=X`` on B's level reported a cycle running through
    X, which was never walked.

    Two shapes are accepted and one is refused:

    - ``task_id`` already ends the trail — the URL builder's own output, used
      as is;
    - ``task_id`` is absent — appended, so a hand-written URL that named only
      the ancestors still counts this level against the depth bound and still
      sees a blocker pointing back at this very task;
    - ``task_id`` appears EARLIER in the trail — refused (``()``), because
      there is no honest reading of it. Truncating at that point would invent a
      walk the client did not describe, and the caller can say "this expansion
      link is not valid" for the cost of saying nothing at all.

    Empty values are dropped — an id cannot be empty (``normalize_task``
    guarantees it), so ``?chain=`` is noise that would otherwise inflate depth.
    """
    trail = tuple(ancestor for ancestor in chain if ancestor)
    if not trail or trail[-1] != task_id:
        return () if task_id in trail else (*trail, task_id)
    return trail
