"""Browser-side behavior of the gate self-refresh timer (tasks.js).

The Gates section hands the browser a *server-written* instant
(``data-gates-next-ready-at``) and asks it to schedule one refresh at that
moment. Two boundaries of that contract cannot be checked from Python — they
live in ``setTimeout`` semantics — so this module runs the real
``static/tasks.js`` inside Node with a stub DOM and inspects the timers it
arms:

- a delay beyond ``setTimeout``'s signed 32-bit range must be CHAINED, not
  truncated (an oversized delay fires immediately, which would re-arm on every
  render — a refresh loop instead of a one-shot);
- a stamp already in the past on arrival (browser/Lens clock skew) must back
  off to the poll interval instead of the sub-second floor.

T2-A6 adds a second harness for the SIDE PANEL, which lives in the same file
for the same reason: what it pins is browser behaviour with no server half —
which URL a row click fetches, and above all that CLOSING the panel clears only
the selection and leaves the board's filters (``?project=``) exactly as they
were. A Python test can assert what the close LINK says; only this can assert
what ``history.pushState`` is handed.

Node is the same runtime the ``e2e/`` Playwright suite needs; the tests skip
when it is absent rather than failing a pure-Python environment.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

TASKS_JS = Path(__file__).resolve().parents[1] / "src/lithos_lens/static/tasks.js"

# Stub DOM harness: enough of window/document/EventSource for the IIFE to load,
# with every timer and fetch recorded. `fire(index)` runs a scheduled callback
# so a chained re-arm can be observed.
HARNESS = """
const fs = require("fs");
const vm = require("vm");

const [sourcePath, nowRaw, readyAt, pollMs, readyAtAfter] = process.argv.slice(1);
let currentNow = Number(nowRaw);
const timers = [];
const fetches = [];

// The board's stamp is READ each time, not captured: a refresh replaces the
// fragment, so the next schedule sees whatever the fresh board names. When
// `readyAtAfter` is supplied it stands for the next timer gate down the queue.
const currentReadyAt = () =>
  fetches.length && readyAtAfter !== undefined ? readyAtAfter : readyAt;
const board = { dataset: { get gatesNextReadyAt() { return currentReadyAt(); } } };
const document = {
  querySelector(selector) {
    if (selector === "[data-gates-next-ready-at]") {
      return currentReadyAt() ? board : null;
    }
    return null;
  },
  querySelectorAll() { return { length: 0, forEach() {} }; },
  createElement() { return { dataset: {}, style: {}, appendChild() {} }; },
  // The panel (T2-A6) binds its click/keydown handlers at load; this harness
  // never fires one, but the IIFE must be able to install them.
  addEventListener() {},
};

class EventSource {
  addEventListener() {}
  close() {}
}

const sandbox = {
  document,
  EventSource,
  console,
  URL,
  // Controlled clock: the harness asserts on exact delays, and advances time
  // by a timer's own delay when it fires (so a chained sleep converges).
  Date: new Proxy(Date, {
    get: (target, prop) => (prop === "now" ? () => currentNow : target[prop]),
  }),
  DOMParser: class { parseFromString() { return document; } },
  fetch: (...args) => { fetches.push(args); return Promise.resolve({ ok: false }); },
};
sandbox.window = {
  LithosLensTasks: {
    autoRefreshIntervalMs: Number(pollMs),
    eventsUrl: "/tasks/events",
  },
  setTimeout: (fn, delay) => { timers.push({ fn, delay }); return timers.length; },
  clearTimeout() {},
  setInterval: () => 0,
  clearInterval() {},
  addEventListener() {},
  location: { href: "http://lens.test/tasks" },
};
sandbox.window.window = sandbox.window;
Object.assign(sandbox, { setTimeout: sandbox.window.setTimeout });

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(sourcePath, "utf8"), sandbox);

const fireIndex = timers.length - 1;
if (fireIndex >= 0) {
  currentNow += timers[fireIndex].delay;
  timers[fireIndex].fn();
}

// The re-arm after a refresh is a promise continuation, so report only once
// the microtask queue has drained — otherwise the chained schedule is invisible.
setImmediate(() => {
  console.log(JSON.stringify({
    delays: timers.map((timer) => timer.delay),
    fetches: fetches.length,
  }));
});
"""

MAX_TIMER_DELAY_MS = 2147483647
GRACE_MS = 500
POLL_MS = 30000
NOW_MS = 1_800_000_000_000


def _run(ready_at: str, ready_at_after: str | None = None) -> dict:
    """Load tasks.js against a board carrying ``ready_at``; fire the last timer.

    ``ready_at_after`` is what the board names once a refresh has happened —
    the next timer gate in the queue, which the refreshed fragment carries.
    """
    assert NODE is not None
    argv = [str(TASKS_JS), str(NOW_MS), ready_at, str(POLL_MS)]
    if ready_at_after is not None:
        argv.append(ready_at_after)
    result = subprocess.run(
        [NODE, "-e", HARNESS, "--", *argv],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def _iso(offset_ms: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp((NOW_MS + offset_ms) / 1000, UTC).isoformat()


def test_near_timer_gate_arms_one_refresh_at_the_instant() -> None:
    two_hours = 2 * 60 * 60 * 1000
    result = _run(_iso(two_hours))

    # Two entries, not one: firing the first refreshes the board (one fetch)
    # and then re-arms. The board here still names the same stamp, now in the
    # past, so the re-arm takes the poll-interval backoff. That second entry
    # was always there — the harness used to report before the promise
    # continuation ran, so it could not see it.
    assert result["delays"] == [two_hours + GRACE_MS, POLL_MS]
    assert result["fetches"] == 1


def test_far_timer_gate_chains_instead_of_overflowing_the_timer() -> None:
    """Regression (round-2 correctness f-001): ``setTimeout`` stores its delay in
    a signed 32-bit int, so a gate ~30 days out used to fire immediately and
    re-arm on every render — a refresh loop against the same future stamp."""
    thirty_days = 30 * 24 * 60 * 60 * 1000
    assert thirty_days > MAX_TIMER_DELAY_MS
    result = _run(_iso(thirty_days))

    # First sleep is clamped to the max supported delay…
    assert result["delays"][0] == MAX_TIMER_DELAY_MS
    # …and firing it re-arms for the remainder instead of refreshing.
    assert result["fetches"] == 0
    assert result["delays"][1] == thirty_days - MAX_TIMER_DELAY_MS + GRACE_MS


def test_stamp_already_past_on_arrival_backs_off_to_the_poll_interval() -> None:
    """Regression (round-2 security f-003): ``next_gate_ready_at`` filters
    against the LENS clock while the browser compares against its own. When the
    browser runs ahead, the stamp is future server-side and past client-side —
    the old floor made every tab re-request /tasks twice a second for the whole
    skew window."""
    result = _run(_iso(-60_000))

    # And the re-arm after the refresh backs off the same way, for the same
    # reason — a stamp that is still past is not something to be on time for.
    assert result["delays"] == [POLL_MS, POLL_MS]


def test_no_timer_gate_arms_no_refresh() -> None:
    result = _run("")

    assert result["delays"] == []
    assert result["fetches"] == 0


def test_a_second_gate_due_soon_is_not_delayed_to_the_poll_interval() -> None:
    """The floor bounds the refresh RATE; it must not swallow the next deadline.

    Gates fall due one after another, and each refresh re-reads the board for
    the next one. `lastGateRefreshAt` is stamped at the START of every refresh,
    so the re-arm that follows sees `sinceLast` ~= 0 and lifts ANY delay to a
    whole poll interval — including a fresh, authoritative, still-future stamp
    the server just published. A gate due 3.5s after the one that triggered the
    refresh then ticks down to "ready now" and sits there for the rest of the
    interval, which is exactly the staleness the one-shot refresh exists to
    prevent.

    Rate-limiting is still right — a server value must not be able to make a
    tab hammer /tasks — so the floor stays; it just has to be a bound on the
    rate rather than the polling cadence borrowed for the purpose.
    """
    first = 2 * 60 * 60 * 1000
    result = _run(_iso(first), _iso(first + 3_500))

    assert result["delays"][0] == first + GRACE_MS
    assert result["fetches"] == 1
    # The re-arm is for the SECOND gate, not a poll interval. The harness clock
    # advances by the delay it fires, so GRACE_MS of the 3.5s gap is already
    # spent by the time the re-arm computes its own — it lands on the same
    # absolute instant (the stamp plus one grace).
    assert result["delays"][1] == 3_500, (
        f"second gate was scheduled {result['delays'][1]}ms out, not 3500ms"
    )


# ── the side panel (T2-A6): row click, close, Escape, back/forward ──────────

# A second stub DOM, and a deliberately CONTROLLED one: the panel's defects
# live in the gaps between a request leaving and its response landing, so every
# fetch here is deferred and settled by name. That is what lets a test stage an
# out-of-order pair (click A, click B, answer A last) — the interleaving a
# harness that drains each request before the next action can never produce.
#
# It models what the panel code actually touches: two rows with server-built
# panel URLs, a host whose `innerHTML` setter maintains the panel node that
# `replaceFragment` swaps, a history STACK with back/forward, an EventSource
# that delivers events to the real handler, and a timer list the test fires to
# run the reconcile. `DOMParser` parses the fake document as JSON keyed by
# refresh-fragment name — the fragments are the contract, not the markup.
PANEL_HARNESS = """
const fs = require("fs");
const vm = require("vm");

const [sourcePath, initialHref, actionsRaw, selectionParam] = process.argv.slice(1);
const actions = JSON.parse(actionsRaw);

const entries = [initialHref];
let cursor = 0;
const pushed = [];
const fetches = [];      // { url, settle } — settled by an explicit action
const prevented = [];
const listeners = {};    // document/window listeners, by type
const sse = {};          // EventSource listeners, by type
const timers = new Map();
let timerId = 0;
let board = "board:initial";

function href() { return entries[cursor]; }
function absolute(url) { return new URL(url, "http://lens.test").href; }

// The host, with the `innerHTML` setter the panel code writes through. The
// node it keeps is what `document.querySelector('[data-refresh-fragment=
// "panel"]')` finds, so a reconcile can replace exactly what an open panel put
// there — the live-status path this exists to exercise.
const host = {
  dataset: {},
  panel: null,
  _html: "",
  get innerHTML() { return this._html; },
  set innerHTML(value) {
    this._html = value;
    this.panel = value
      ? { html: value, replaceWith(next) { host.innerHTML = next.html; } }
      : null;
  },
};

// What `dashboard.html` renders for a request that arrived with `?selected=`:
// the panel already in the host, and the host carrying the URL the SERVER
// built for that selection — the only source for a task with no row.
const initialSelection =
  new URL(initialHref).searchParams.get(selectionParam) || "";
if (initialSelection) {
  host.dataset.panelSelected = initialSelection;
  host.dataset.panelUrl =
    "/tasks/" + initialSelection + "?project=influx&fragment=panel";
  host.innerHTML = "panel:" + initialSelection;
}

const rows = {
  alpha: {
    dataset: {
      taskId: "alpha",
      panelUrl: "/tasks/alpha?project=influx&fragment=panel",
    },
  },
  beta: {
    dataset: {
      taskId: "beta",
      panelUrl: "/tasks/beta?project=influx&fragment=panel",
    },
  },
  // A task really can be called `graph`, and the server addresses it through
  // the query alias because `/tasks/graph` is the graph PAGE.
  graph: {
    dataset: {
      taskId: "graph",
      panelUrl: "/tasks/id?task_id=graph&project=influx&fragment=panel",
    },
  },
};

const boardNode = { replaceWith(next) { board = next.html; } };
const titleLink = { classList: { contains: (name) => name === "task-title" } };
const tagLink = { classList: { contains: () => false } };

function clickEvent(map, label) {
  return {
    button: 0,
    defaultPrevented: false,
    target: { closest: (selector) => map[selector] || null },
    preventDefault() { prevented.push(label); },
  };
}

const document = {
  querySelector(selector) {
    if (selector === "[data-panel-host]") return host;
    if (selector === '[data-refresh-fragment="panel"]') return host.panel;
    if (selector === '[data-refresh-fragment="dashboard-data"]') return boardNode;
    const row = /\\[data-task-row\\]\\[data-task-id="([^"]+)"\\]/.exec(selector);
    if (row) return rows[row[1]] || null;
    return null;
  },
  querySelectorAll() { return { length: 0, forEach() {} }; },
  createElement() { return { dataset: {}, style: {}, appendChild() {} }; },
  addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
};

class EventSource {
  addEventListener(type, fn) { (sse[type] = sse[type] || []).push(fn); }
  close() {}
}

// What the SERVER would answer for a URL. A panel fetch returns that task's
// partial; the reconcile fetches the live board URL, so its document carries
// the board plus the panel for whatever `selected=` that URL names — which is
// exactly how a reconcile's panel can be one selection behind.
function bodyFor(url) {
  if (url.indexOf("fragment=panel") !== -1) {
    const path = /\\/tasks\\/([^?]+)\\?/.exec(url);
    const alias = /task_id=([^&]+)/.exec(url);
    const id = decodeURIComponent(alias ? alias[1] : path ? path[1] : "");
    return "panel:" + id;
  }
  const selected = new URL(url, "http://lens.test").searchParams.get(selectionParam);
  const doc = { "dashboard-data": "board:fresh" };
  if (selected) doc.panel = "panel:" + selected + ":fresh";
  return JSON.stringify(doc);
}

const sandbox = {
  document,
  EventSource,
  console,
  URL,
  URLSearchParams,
  DOMParser: class {
    parseFromString(text) {
      let parsed = {};
      try { parsed = JSON.parse(text); } catch (error) { parsed = {}; }
      return {
        querySelector(selector) {
          const match = /data-refresh-fragment="([^"]+)"/.exec(selector);
          if (!match || parsed[match[1]] === undefined) return null;
          return { html: parsed[match[1]] };
        },
      };
    }
  },
  // Headers and BODY are separate suspension points, and the panel code checks
  // itself at both: `await fetch(...)` resolves when the response arrives,
  // `await response.text()` when it has been read. A harness that answered them
  // together could only ever exercise the first check.
  fetch: (url) => new Promise((resolveResponse) => {
    let resolveBody = () => {};
    const body = new Promise((resolve) => { resolveBody = resolve; });
    fetches.push({
      url,
      headers: () => resolveResponse({ ok: true, text: () => body }),
      body: () => resolveBody(bodyFor(url)),
      settle() { this.headers(); this.body(); },
    });
  }),
};
sandbox.window = {
  LithosLensTasks: {
    eventsUrl: "/tasks/events",
    // The HOST page's selection parameter — `selected` on the dashboard,
    // `focus` on the graph page. Driven from the test, because "one panel
    // implementation for rows and nodes" is exactly the promise a hard-coded
    // key here would stop protecting.
    selectionParam,
    panelAliasPath: "/tasks/id",
    panelAliasKey: "task_id",
  },
  setTimeout: (fn, delay) => { timerId += 1; timers.set(timerId, fn); return timerId; },
  clearTimeout: (id) => { timers.delete(id); },
  setInterval: () => 0,
  clearInterval() {},
  addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
  location: { get href() { return href(); } },
  history: {
    pushState(state, title, url) {
      pushed.push(url);
      entries.splice(cursor + 1);
      entries.push(absolute(url));
      cursor = entries.length - 1;
    },
  },
};
sandbox.window.window = sandbox.window;
Object.assign(sandbox, { setTimeout: sandbox.window.setTimeout });

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(sourcePath, "utf8"), sandbox);

function fire(type, event) {
  (listeners[type] || []).forEach((listener) => listener(event));
}

let eventSeq = 0;

const ACTIONS = {
  escape: () => fire("keydown", { key: "Escape" }),
  close: () => fire("click", clickEvent({ "[data-panel-close]": {} }, "close")),
  expand: () => fire("click", clickEvent(
    { "[data-task-panel]": {}, "a[href]": tagLink }, "expand",
  )),
  tag: () => fire("click", clickEvent(
    { "[data-task-row]": rows.alpha, "a[href]": tagLink }, "tag",
  )),
  back: () => { if (cursor > 0) cursor -= 1; fire("popstate", {}); },
  forward: () => {
    if (cursor < entries.length - 1) cursor += 1;
    fire("popstate", {});
  },
  // One task event: the real handler debounces a reconcile behind a timer.
  event: () => {
    eventSeq += 1;
    (sse["task.updated"] || []).forEach((listener) => listener({
      lastEventId: "event-" + eventSeq,
      data: JSON.stringify({
        type: "task.updated", task_id: "alpha", requires_refresh: true,
      }),
    }));
  },
  // The board was replaced by a reconcile and this row is no longer on it —
  // the one way a task in this tab's history can have no server-built URL left.
  drop: () => {},
  timers: () => {
    const pending = Array.from(timers.entries());
    timers.clear();
    pending.forEach(([, fn]) => fn());
  },
};

(async () => {
  for (const action of actions) {
    const [name, argument] = action.split(":");
    if (name === "click") {
      fire("click", clickEvent(
        { "[data-task-row]": rows[argument], "a[href]": titleLink }, "row:" + argument,
      ));
    } else if (name === "click-body") {
      // The row itself, away from any link — "clicking a ROW opens the panel"
      // (§5.5), not only clicking its title.
      fire("click", clickEvent(
        { "[data-task-row]": rows[argument] }, "row-body:" + argument,
      ));
    } else if (name === "settle") {
      fetches[Number(argument)].settle();
    } else if (name === "headers") {
      fetches[Number(argument)].headers();
    } else if (name === "body") {
      fetches[Number(argument)].body();
    } else if (name === "drop") {
      delete rows[argument];
    } else {
      ACTIONS[name]();
    }
    // Between actions only: each one is a discrete operator/browser event, and
    // whatever it set in motion gets to make whatever progress it can before
    // the next. Nothing here waits for a fetch — only an explicit `settle`
    // answers one.
    await new Promise((resolve) => setImmediate(resolve));
  }
  console.log(JSON.stringify({
    pushed,
    fetches: fetches.map((entry) => entry.url),
    prevented,
    href: href(),
    panel: host.innerHTML,
    board,
  }));
})();
"""

BOARD_HREF = "http://lens.test/tasks?project=influx"


def _panel_run(
    actions: list[str],
    href: str = BOARD_HREF,
    selection_param: str = "selected",
) -> dict:
    """Load tasks.js against a two-row board, then run ``actions`` in order.

    Every fetch stays unanswered until an explicit action: ``settle:<n>``
    answers one whole, ``headers:<n>`` and ``body:<n>`` answer its two halves
    separately. So a test says exactly which response lands when, and where in
    a response's own lifetime the next thing happens.

    ``selection_param`` is the HOST page's one selection parameter — the
    dashboard's ``selected``, the graph page's ``focus`` — because the panel is
    one implementation for both and nothing in it may assume either spelling.
    """
    assert NODE is not None
    result = subprocess.run(
        [
            NODE,
            "-e",
            PANEL_HARNESS,
            "--",
            str(TASKS_JS),
            href,
            json.dumps(actions),
            selection_param,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_clicking_a_row_fetches_its_panel_fragment_and_pushes_the_selection() -> None:
    """§5.5: a row click opens the panel instead of navigating. It fetches the
    URL the SERVER put on the row — so the id encoding and the board's filters
    are the server's decision — and only then pushes `selected` onto the URL, so
    a failed fetch can never leave the address bar claiming an open panel."""
    result = _panel_run(["click:alpha", "settle:0"])

    assert result["fetches"] == ["/tasks/alpha?project=influx&fragment=panel"]
    assert result["pushed"] == ["/tasks?project=influx&selected=alpha"]
    assert result["panel"] == "panel:alpha"
    assert result["prevented"] == ["row:alpha"]


def test_nothing_is_pushed_until_the_panel_has_actually_arrived() -> None:
    """The push follows the swap. While the fetch is in flight the URL still
    describes what is on screen."""
    result = _panel_run(["click:alpha"])

    assert result["pushed"] == []
    assert result["panel"] == ""


def test_closing_the_panel_keeps_the_boards_project_filter() -> None:
    """THE acceptance criterion for the close path: closing clears the
    selection and preserves list state. Rebuilt from the live URL rather than
    from a remembered query string, so every filter — `project` here, but tags,
    the epic scope and the since window alike — survives."""
    result = _panel_run(["click:alpha", "settle:0", "close"])

    assert result["pushed"][-1] == "/tasks?project=influx"
    assert result["href"] == "http://lens.test/tasks?project=influx"
    assert result["panel"] == ""


def test_escape_closes_the_panel_the_same_way() -> None:
    """Escape is the keyboard half of the close button, not a second path with
    its own URL handling."""
    result = _panel_run(["click:alpha", "settle:0", "escape"])

    assert result["pushed"][-1] == "/tasks?project=influx"
    assert result["panel"] == ""


def test_escape_with_no_panel_open_pushes_nothing() -> None:
    """An Escape on a board with no selection must not write a history entry —
    it would make Back a no-op the operator has to press twice."""
    result = _panel_run(["escape"])

    assert result["pushed"] == []


def test_a_tag_chip_inside_a_row_keeps_its_own_navigation() -> None:
    """A row click is the TITLE link and the row itself. The tag chips inside
    the row are filter links and must still filter, or the panel would swallow
    the only way to scope the board from a row."""
    result = _panel_run(["tag"])

    assert result["fetches"] == []
    assert result["pushed"] == []
    assert result["prevented"] == []


def test_links_inside_the_panel_navigate_normally() -> None:
    """Expand is the whole point of the panel's link set: it LEAVES for the
    full page. A handler that swallowed clicks inside the panel would strand
    the operator in a summary."""
    result = _panel_run(["expand"])

    assert result["fetches"] == []
    assert result["pushed"] == []
    assert result["prevented"] == []


# --- Ordering: the LATEST intent owns the panel, whatever answers last ------


def test_a_late_response_never_overwrites_a_newer_selection() -> None:
    """Two clicks in flight at once, answered in the wrong order. Panels are
    fetched, so this is ordinary — and without a guard the panel becomes
    whichever request happened to answer LAST: A's stale response would repaint
    A and push `selected=A` over B, so the first click would win."""
    result = _panel_run(["click:alpha", "click:beta", "settle:1", "settle:0"])

    assert result["panel"] == "panel:beta"
    assert result["pushed"] == ["/tasks?project=influx&selected=beta"]
    assert result["href"] == "http://lens.test/tasks?project=influx&selected=beta"


def test_a_response_that_lands_after_a_close_does_not_reopen_the_panel() -> None:
    """The same defect with a stronger contradiction: the panel is closed, the
    URL says nothing is selected, and a request issued before the close answers
    afterwards. Reopening then leaves a panel the URL does not describe."""
    result = _panel_run(["click:alpha", "close", "settle:0"])

    assert result["panel"] == ""
    assert result["pushed"] == ["/tasks?project=influx"]


def test_back_past_an_in_flight_selection_leaves_the_url_it_landed_on() -> None:
    """The reviewer's interleaving, end to end: from B, Back to A starts A's
    fetch, and a second Back to the unselected board arrives before it lands.
    The late A response must not reopen A under a URL that has moved on."""
    result = _panel_run(
        [
            "click:alpha",
            "settle:0",
            "click:beta",
            "settle:1",
            "back",  # -> ?selected=alpha, starts a fetch
            "back",  # -> the unselected board, closes the panel
            "settle:2",  # …and only now does alpha answer
        ]
    )

    assert result["panel"] == ""
    assert result["href"] == "http://lens.test/tasks?project=influx"
    # Neither Back pushed: back/forward walk history, they do not extend it.
    assert result["pushed"] == [
        "/tasks?project=influx&selected=alpha",
        "/tasks?project=influx&selected=beta",
    ]


def test_forward_onto_the_visible_selection_still_supersedes_an_open() -> None:
    """Back to A and straight Forward to B, before A has answered. B is still
    what is on SCREEN, so a handler comparing against the screen has nothing to
    do and returns — leaving A's open running under B's URL, and A's response
    free to paint itself there. The comparison is against the INTENT for
    exactly this schedule."""
    result = _panel_run(
        [
            "click:alpha",
            "settle:0",
            "click:beta",
            "settle:1",
            "back",  # -> ?selected=alpha, starts alpha's fetch
            "forward",  # -> ?selected=beta again, before it answers
            "settle:2",  # …and only now does alpha answer
        ]
    )

    assert result["href"] == "http://lens.test/tasks?project=influx&selected=beta"
    assert result["panel"] == "panel:beta"


def test_a_reconcile_started_before_a_click_pushed_does_not_paint_it() -> None:
    """The reconcile fetches the LIVE URL, and a click already in flight has
    not pushed its own yet — so a reconcile started in that window carries the
    previous selection's panel while the generation it captured is the new
    click's. Generation alone therefore cannot spot it; the URL it fetched can."""
    result = _panel_run(
        [
            "click:beta",
            "settle:0",
            "click:alpha",  # in flight, so the URL still says beta
            "event",
            "timers",  # the reconcile leaves, fetching ?selected=beta
            "settle:1",  # alpha lands and pushes ?selected=alpha
            "settle:2",  # …then the reconcile answers, with beta's panel
        ]
    )

    assert result["href"] == "http://lens.test/tasks?project=influx&selected=alpha"
    assert result["panel"] == "panel:alpha"
    # The board fragment is applied either way: it does not depend on the
    # selection, and dropping it would leave the board stale for no reason.
    assert result["board"] == "board:fresh"


def test_a_body_that_arrives_after_a_newer_panel_is_rendered_is_dropped() -> None:
    """The second suspension point, on its own. A's response ARRIVES, then B is
    clicked and fully rendered, and only then is A's body read. Between those
    two awaits the panel and the URL have both moved on, so the check after
    `response.text()` is what stops A's markup landing on top of B."""
    result = _panel_run(
        ["click:alpha", "headers:0", "click:beta", "settle:1", "body:0"]
    )

    assert result["panel"] == "panel:beta"
    assert result["pushed"] == ["/tasks?project=influx&selected=beta"]
    assert result["href"] == "http://lens.test/tasks?project=influx&selected=beta"


def test_back_to_an_earlier_selection_shows_that_task_without_a_push() -> None:
    """Back and forward walk the exploration: a previous NON-EMPTY selection is
    re-fetched and re-shown, and no history entry is written for the move."""
    opened = ["click:alpha", "settle:0", "click:beta", "settle:1"]
    result = _panel_run(opened + ["back", "settle:2"])

    assert result["panel"] == "panel:alpha"
    assert result["href"] == "http://lens.test/tasks?project=influx&selected=alpha"
    assert result["pushed"] == [
        "/tasks?project=influx&selected=alpha",
        "/tasks?project=influx&selected=beta",
    ]


def test_forward_returns_to_the_later_selection() -> None:
    """…and the same in the other direction, which is what makes the URL — not
    a click — the state."""
    result = _panel_run(
        [
            "click:alpha",
            "settle:0",
            "click:beta",
            "settle:1",
            "back",
            "settle:2",
            "forward",
            "settle:3",
        ]
    )

    assert result["panel"] == "panel:beta"
    assert result["href"] == "http://lens.test/tasks?project=influx&selected=beta"
    assert len(result["pushed"]) == 2


# --- Live reconciliation: an open panel's statuses stay live ----------------


def test_a_task_event_refreshes_the_open_panel_alongside_the_board() -> None:
    """The panel states BLOCKER and DEPENDENT statuses, so a stale one is a
    wrong answer, not merely an old one. A task event reconciles the board, and
    the panel is swapped from the same response — while the selection and the
    URL stay exactly where they were."""
    result = _panel_run(["click:alpha", "settle:0", "event", "timers", "settle:1"])

    # The reconcile fetched the live URL, which names the open selection.
    assert (
        result["fetches"][1] == "http://lens.test/tasks?project=influx&selected=alpha"
    )
    assert result["panel"] == "panel:alpha:fresh"
    assert result["board"] == "board:fresh"
    assert result["href"] == "http://lens.test/tasks?project=influx&selected=alpha"
    # No history entry: a refresh is not a navigation.
    assert result["pushed"] == ["/tasks?project=influx&selected=alpha"]


def test_a_reconcile_fetched_for_one_selection_never_paints_another() -> None:
    """The reconcile carries a panel for whatever was selected when it LEFT. If
    the operator has opened another task since, that panel is one selection
    behind and must not be applied — the board fragment still is, because it
    does not depend on the selection."""
    result = _panel_run(
        [
            "click:alpha",
            "settle:0",
            "event",
            "timers",  # the reconcile leaves, carrying alpha's panel
            "click:beta",
            "settle:2",  # beta's panel arrives first
            "settle:1",  # …then the reconcile answers, with alpha
        ]
    )

    assert result["panel"] == "panel:beta"
    assert result["board"] == "board:fresh"
    assert result["href"] == "http://lens.test/tasks?project=influx&selected=beta"


# --- The fallback URL: every id addressable, page words included ------------


def test_a_selection_with_no_row_reopens_through_the_hosts_own_url() -> None:
    """A deep-linked task need not have a row — a filter can exclude it, or it
    resolved outside the window — so the host carries the URL the server built
    for the selection it rendered. Closing and going Back reopens it through
    that, with the board's filters intact."""
    result = _panel_run(
        ["close", "back", "settle:0"],
        href="http://lens.test/tasks?project=influx&selected=ghost",
    )

    assert result["fetches"] == ["/tasks/ghost?project=influx&fragment=panel"]
    assert result["panel"] == "panel:ghost"


def test_a_page_word_id_is_never_refetched_through_a_browser_built_path() -> None:
    """The last resort, reached when the row a selection came from has left the
    board on a reconcile. A task id is an arbitrary string and `graph` is a
    PAGE under /tasks/, so a path assembled in the browser would fetch the
    graph page and swap it into the panel host. The alias route addresses every
    id, and its spelling comes from the server rather than from here."""
    result = _panel_run(
        ["click:graph", "settle:0", "close", "drop:graph", "back", "settle:1"]
    )

    # First through the row's server-built URL, then — with the row gone —
    # through the alias, never `/tasks/graph`.
    assert result["fetches"] == [
        "/tasks/id?task_id=graph&project=influx&fragment=panel",
        "/tasks/id?task_id=graph&fragment=panel",
    ]
    assert result["panel"] == "panel:graph"
    assert not any(url.startswith("/tasks/graph") for url in result["fetches"])


# --- One panel, two hosts: the selection parameter is the host's ------------

# What the graph page (T2-A4) will hand this code: its own selection parameter
# and its own URL state around it. The panel is ONE implementation for rows and
# nodes, so every transition below runs against `focus` here and `selected`
# above, from the same source.
GRAPH_HREF = (
    "http://lens.test/tasks/graph"
    "?project=lithos-loom&overlays=hierarchy&isolated=1&focus=alpha"
)


def test_a_node_click_pushes_the_hosts_own_selection_parameter() -> None:
    """On the graph page the parameter is `focus`, and nothing may write
    `selected` there: §5.5 gives each host exactly one selection parameter, and
    two on one URL is two selections."""
    result = _panel_run(["click:beta", "settle:0"], GRAPH_HREF, selection_param="focus")

    assert result["pushed"] == [
        "/tasks/graph?project=lithos-loom&overlays=hierarchy&isolated=1&focus=beta"
    ]
    assert "selected=" not in result["href"]


def test_closing_on_the_graph_page_clears_focus_and_keeps_the_graph_state() -> None:
    """Close clears the host's selection and nothing else — here that means the
    scope, the overlays and the isolated toggle all survive, exactly as the
    dashboard's filters do."""
    result = _panel_run(
        ["click:beta", "settle:0", "close"], GRAPH_HREF, selection_param="focus"
    )

    assert result["pushed"][-1] == (
        "/tasks/graph?project=lithos-loom&overlays=hierarchy&isolated=1"
    )
    assert result["panel"] == ""


def test_back_on_the_graph_page_restores_the_focus_in_the_url() -> None:
    """…and back/forward read the same parameter they wrote."""
    result = _panel_run(
        ["close", "back", "settle:0"], GRAPH_HREF, selection_param="focus"
    )

    assert result["fetches"] == ["/tasks/alpha?project=influx&fragment=panel"]
    assert result["panel"] == "panel:alpha"
    assert result["href"] == GRAPH_HREF


# --- The interaction contract: a ROW opens the panel, not just its title ----


def test_clicking_the_row_away_from_any_link_opens_the_panel() -> None:
    """§5.5 says "clicking a row opens a panel". The title link is the obvious
    target, but it is not the contract: a click on the row's own body — its
    meta line, its whitespace — opens the same panel, and a handler that only
    caught the title would leave most of the row inert."""
    result = _panel_run(["click-body:alpha", "settle:0"])

    assert result["fetches"] == ["/tasks/alpha?project=influx&fragment=panel"]
    assert result["pushed"] == ["/tasks?project=influx&selected=alpha"]
    assert result["panel"] == "panel:alpha"
    assert result["prevented"] == ["row-body:alpha"]


def test_escape_closes_a_server_rendered_panel_with_no_click_before_it() -> None:
    """A deep link renders the panel server-side, so the browser never opened
    it — the selection comes off the URL at load. Escape has to close THAT
    panel too, which is what the load-time seed is for: without it the first
    Escape on a shared link does nothing."""
    result = _panel_run(
        ["escape"], "http://lens.test/tasks?project=influx&selected=ghost"
    )

    assert result["panel"] == ""
    assert result["pushed"] == ["/tasks?project=influx"]


# --- Close preserves the WHOLE list state, not just one filter -------------

COMPOSITE_HREF = (
    "http://lens.test/tasks"
    "?status=open&project=influx&project=loom&tag=area%3Adata&tag=ops"
    "&agent=worker-a&epic=epic-1&since=2026-08-01&selected=alpha"
)


def test_closing_removes_only_the_selection_from_a_full_board_url() -> None:
    """ "Close preserves list state" is the whole query, not the one filter the
    headline example happens to use: the epic scope the build criterion names,
    a repeated multi-select, a tag whose value carries a colon, and the
    resolved-since window. Asserted as a multimap, so a rebuild that dropped a
    duplicate or reordered the pairs fails even if it kept every key."""
    from urllib.parse import parse_qsl, urlsplit

    result = _panel_run(["close"], COMPOSITE_HREF)

    expected = [
        (key, value)
        for key, value in parse_qsl(urlsplit(COMPOSITE_HREF).query)
        if key != "selected"
    ]
    closed = parse_qsl(urlsplit(result["pushed"][-1]).query)
    assert closed == expected
    assert "selected" not in dict(closed)
