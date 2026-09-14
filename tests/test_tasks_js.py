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

// Registered before anything runs, which also overrides Node's default of
// crashing the process: an unhandled rejection is a RESULT this harness
// reports, because "the panel never leaves a rejection dangling" is part of
// what the failure paths promise.
const unhandled = [];
process.on("unhandledRejection", (reason) => { unhandled.push(String(reason)); });

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
  // A row from the Gates section. It renders gate chrome instead of the
  // claim/blocker chrome of an ordinary row, so it carries `data-gate-row`
  // and NOT `data-task-row` — and the SAME panel contract, which is the whole
  // point: one click handler for every row on the board (§5.5).
  gate: {
    gateRow: true,
    dataset: {
      taskId: "gate",
      panelUrl: "/tasks/gate?project=influx&fragment=panel",
    },
  },
};

// The one selector tasks.js closest()s a click up to. Spelled once here for
// the same reason it is spelled once there: a row that does not match it is a
// row the panel cannot be opened from.
const PANEL_ROW = "[data-panel-url][data-task-id]";

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
    // The PANEL contract — carried by every row on the board, gates included.
    const selected =
      /\\[data-panel-url\\]\\[data-task-id="([^"]+)"\\]/.exec(selector);
    if (selected) return rows[selected[1]] || null;
    // The SSE handlers' hook, which a gate row does not carry: its chrome is
    // not the claim/status chrome those handlers rewrite.
    const row = /\\[data-task-row\\]\\[data-task-id="([^"]+)"\\]/.exec(selector);
    if (row) {
      const found = rows[row[1]];
      return found && !found.gateRow ? found : null;
    }
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
  //
  // Each request can also FAIL in each of the three ways a real one can: a
  // rejected fetch (no connection), a non-OK answer, and a body that never
  // finishes reading. They are separate outcomes in the browser and separate
  // actions here.
  fetch: (url) => new Promise((resolveResponse, rejectResponse) => {
    let resolveBody = () => {};
    let rejectBody = () => {};
    const body = new Promise((resolve, reject) => {
      resolveBody = resolve;
      rejectBody = reject;
    });
    // The HARNESS's own handler, so a body rejected for a request the code
    // under test abandoned is not reported as ITS unhandled rejection. The
    // awaiting code still sees the rejection; this only keeps the books
    // honest about whose it is.
    body.catch(() => {});
    fetches.push({
      url,
      headers: () => resolveResponse({ ok: true, text: () => body }),
      body: () => resolveBody(bodyFor(url)),
      settle() { this.headers(); this.body(); },
      fail: () => resolveResponse({ ok: false, text: () => body }),
      reject: () => rejectResponse(new Error("network is down")),
      rejectBody: () => rejectBody(new Error("connection reset mid-body")),
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
    { [PANEL_ROW]: rows.alpha, "a[href]": tagLink }, "tag",
  )),
  // The <summary> of a gate row's waiter list: the browser's own control for
  // the <details> it opens, inside a row the panel handler claims.
  waiters: () => fire("click", clickEvent(
    { [PANEL_ROW]: rows.gate, summary: {} }, "waiters",
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
        { [PANEL_ROW]: rows[argument], "a[href]": titleLink }, "row:" + argument,
      ));
    } else if (name === "click-body") {
      // The row itself, away from any link — "clicking a ROW opens the panel"
      // (§5.5), not only clicking its title.
      fire("click", clickEvent(
        { [PANEL_ROW]: rows[argument] }, "row-body:" + argument,
      ));
    } else if (name === "settle") {
      fetches[Number(argument)].settle();
    } else if (name === "headers") {
      fetches[Number(argument)].headers();
    } else if (name === "body") {
      fetches[Number(argument)].body();
    } else if (name === "fail") {
      fetches[Number(argument)].fail();
    } else if (name === "reject") {
      fetches[Number(argument)].reject();
    } else if (name === "reject-body") {
      fetches[Number(argument)].rejectBody();
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
  // One more turn, so a rejection left dangling by the last action is reported
  // before the results are: Node raises `unhandledRejection` a tick after the
  // rejection itself.
  await new Promise((resolve) => setImmediate(resolve));
  console.log(JSON.stringify({
    pushed,
    fetches: fetches.map((entry) => entry.url),
    prevented,
    href: href(),
    panel: host.innerHTML,
    board,
    unhandled,
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


def test_clicking_a_gate_row_opens_its_panel_like_any_other_row() -> None:
    """A gate is a row on the board, so §5.5's "clicking a row opens a panel"
    covers it. Gate rows carry gate chrome instead of claim chrome and so do
    NOT carry `data-task-row`; keying the handler off that attribute left the
    whole Gates section navigating away on a title click and inert everywhere
    else. The panel contract is `data-panel-url` + `data-task-id`, which every
    rendered row carries."""
    result = _panel_run(["click:gate", "settle:0"])

    assert result["fetches"] == ["/tasks/gate?project=influx&fragment=panel"]
    assert result["pushed"] == ["/tasks?project=influx&selected=gate"]
    assert result["panel"] == "panel:gate"
    assert result["prevented"] == ["row:gate"]


def test_a_gate_rows_waiter_list_still_opens_natively() -> None:
    """The waiter count is a <details> so it expands with no JS at all. Its
    <summary> is the browser's own control, and a row handler that swallowed
    the click would trade the disclosure for a panel open — the list could
    then never be expanded again."""
    result = _panel_run(["waiters"])

    assert result["fetches"] == []
    assert result["pushed"] == []
    assert result["prevented"] == []


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


ANCHORED_HREF = "http://lens.test/tasks?project=influx#task-group-blocked"


def test_the_boards_section_anchor_survives_both_panel_transitions() -> None:
    """A summary card links to a SECTION of the board — `task_card_url` appends
    `#task-group-blocked` — so the fragment is generated dashboard state that
    says where the operator is, exactly like the filters say what they are
    looking at. Rebuilding the URL from `pathname + search` alone dropped it on
    the first open and never gave it back, which breaks "closing clears the
    selection and nothing else" one click earlier than the close."""
    result = _panel_run(["click:alpha", "settle:0", "close"], ANCHORED_HREF)

    assert result["pushed"] == [
        "/tasks?project=influx&selected=alpha#task-group-blocked",
        "/tasks?project=influx#task-group-blocked",
    ]
    assert result["href"] == ANCHORED_HREF


# --- Reopening the selected task is not a navigation -----------------------


def test_reopening_the_selected_task_does_not_stack_a_second_entry() -> None:
    """Clicking the row that is already selected re-fetches its panel — the
    open is real, and the response may well be newer — but it does NOT move
    the address bar, so it must not write a history entry.

    An identical entry stacked here is invisible until the operator leaves:
    Back lands on the twin, `handlePanelPopstate` sees the selection it already
    intends and returns, and the board stays exactly as it was. The panel then
    takes two Backs to leave and the first one looks broken."""
    result = _panel_run(["click:alpha", "settle:0", "click:alpha", "settle:1", "back"])

    # The second click is a real open: it fetches.
    assert result["fetches"] == [
        "/tasks/alpha?project=influx&fragment=panel",
        "/tasks/alpha?project=influx&fragment=panel",
    ]
    # …and exactly one entry was ever written for it.
    assert result["pushed"] == ["/tasks?project=influx&selected=alpha"]
    # So ONE Back leaves the task, rather than landing on its duplicate.
    assert result["href"] == BOARD_HREF
    assert result["panel"] == ""


def test_retrying_a_task_whose_back_navigation_failed_pushes_nothing() -> None:
    """The same shape by the other route. A failed fetch on Back clears the
    panel and the selection but leaves the URL the browser already moved — so
    the retry that finally answers is an open under a URL that ALREADY names
    the task, and pushing there would bury the entry Back is meant to reach."""
    result = _panel_run(
        [
            "click:alpha",
            "settle:0",
            "close",
            "back",
            "fail:1",
            "click:alpha",
            "settle:2",
        ]
    )

    assert result["pushed"] == [
        "/tasks?project=influx&selected=alpha",
        "/tasks?project=influx",
    ]
    assert result["href"] == "http://lens.test/tasks?project=influx&selected=alpha"
    assert result["panel"] == "panel:alpha"


# --- A panel that never arrives: three failures, two navigation modes -------

# Back and Forward move the URL BEFORE the panel code runs, so a failed fetch
# there is not the same event as a failed click: the address bar already names
# the new task while the previous one is still on screen. Each failure class is
# exercised on that path, because they enter the code at three different points.
OPEN_A_THEN_B = ["click:alpha", "settle:0", "click:beta", "settle:1"]


def test_a_failed_response_on_back_never_leaves_the_previous_task_on_screen() -> None:
    """The reviewer's schedule: A, then B, then Back to A — and A's fragment
    answers non-200. The URL says A. Leaving B's panel under it is the one
    state the panel must never be in, so it is cleared: a missing answer beats
    a wrong one, and the URL stays intact for a reload to retry."""
    result = _panel_run(OPEN_A_THEN_B + ["back", "fail:2"])

    assert result["href"] == "http://lens.test/tasks?project=influx&selected=alpha"
    assert result["panel"] == ""
    assert result["unhandled"] == []


def test_a_rejected_fetch_on_back_is_handled_the_same_way() -> None:
    """A transport failure never reaches the `response.ok` test at all — it
    rejects the fetch. Same outcome required, and nothing left dangling: with
    no caller awaiting `openPanel`, an uncaught rejection is an unhandled one."""
    result = _panel_run(OPEN_A_THEN_B + ["back", "reject:2"])

    assert result["href"] == "http://lens.test/tasks?project=influx&selected=alpha"
    assert result["panel"] == ""
    assert result["unhandled"] == []


def test_a_body_that_fails_to_read_on_back_is_handled_the_same_way() -> None:
    """The third class, and the one that gets furthest in: the response
    arrives, `ok` is true, and the connection dies while the body is read."""
    result = _panel_run(OPEN_A_THEN_B + ["back", "headers:2", "reject-body:2"])

    assert result["href"] == "http://lens.test/tasks?project=influx&selected=alpha"
    assert result["panel"] == ""
    assert result["unhandled"] == []


def test_a_failed_click_leaves_the_panel_and_the_url_exactly_as_they_were() -> None:
    """The other navigation mode. A click pushes only on success, so nothing
    has moved: the panel on screen still describes the URL, and both stay."""
    result = _panel_run(["click:alpha", "settle:0", "click:beta", "fail:1"])

    assert result["panel"] == "panel:alpha"
    assert result["href"] == "http://lens.test/tasks?project=influx&selected=alpha"
    assert result["pushed"] == ["/tasks?project=influx&selected=alpha"]
    assert result["unhandled"] == []


def test_a_failed_click_does_not_leave_its_task_claimed_as_the_intent() -> None:
    """What the failed click must NOT leave behind. The intent is compared
    against on every history move, so a click that failed while claiming B
    makes the next Forward ONTO B match it, return early, and leave A's panel
    sitting under B's URL. The intent is walked back to what is on screen, so
    that Forward re-fetches instead."""
    result = _panel_run(
        OPEN_A_THEN_B
        + [
            "back",  # -> ?selected=alpha
            "settle:2",  # …shown; the forward entry (beta) is still in history
            "click:beta",  # a click that fails, pushing nothing
            "fail:3",
            "forward",  # -> ?selected=beta, which the stale intent would match
            "settle:4",
        ]
    )

    assert result["href"] == "http://lens.test/tasks?project=influx&selected=beta"
    assert result["panel"] == "panel:beta"
    assert result["unhandled"] == []


# ── the graph canvas (T2-A4): overlays, isolated, the pill, node clicks ─────

# A third harness, and the reason it is not the panel one: what A4 adds lives
# between the payload the server already embedded and Cytoscape — which URL an
# overlay toggle pushes, which elements are drawn afterwards, that NOTHING is
# fetched to do it, and that a task event never moves the layout. None of that
# is visible to Python (the server renders the same HTML either way) and none of
# it is visible to the e2e suite either, which photographs pixels.
#
# It loads BOTH files, in the page's own order, because "one panel
# implementation for rows and nodes" (D9) is exactly the promise that a canvas
# with its own private copy of the panel would stop keeping: a node tap here has
# to travel through `tasks.js`.
#
# Cytoscape itself is stubbed — a real one needs a canvas — but the stub
# resolves STYLE the way the library does (last matching rule wins), so
# "every drawn edge carries an arrowhead" is a question this harness can
# actually answer rather than assume.
GRAPH_HARNESS = """
const fs = require("fs");
const vm = require("vm");

const [tasksPath, graphPath, initialHref, actionsRaw, payloadRaw] =
  process.argv.slice(1);
const actions = JSON.parse(actionsRaw);

const entries = [initialHref];
let cursor = 0;
const pushed = [];
const fetches = [];
const listeners = {};
const sse = {};
let layouts = 0;

function href() { return entries[cursor]; }

function element(extra) {
  return Object.assign({
    dataset: {},
    attributes: {},
    hidden: false,
    textContent: "",
    setAttribute(name, value) { this.attributes[name] = value; },
    getAttribute(name) { return this.attributes[name]; },
  }, extra || {});
}

const surface = element();
const container = element();
const host = element({
  panel: null,
  _html: "",
  get innerHTML() { return this._html; },
  set innerHTML(value) { this._html = value; },
});
const payloadScript = element({ textContent: payloadRaw });
const pill = element({ hidden: true });
const textToggle = element({ hidden: true });
const disclosure = element({ open: false });
const layersSection = element();
const hierarchySection = element();
const isolatedToggle = element({ dataset: {} });
const overlayToggles = {
  hierarchy: element({ dataset: { toggleOverlay: "hierarchy" } }),
  provenance: element({ dataset: { toggleOverlay: "provenance" } }),
};

const SINGLE = {
  "[data-graph-canvas-layout]": surface,
  "[data-graph-canvas]": container,
  "[data-graph-payload]": payloadScript,
  "[data-panel-host]": host,
  "[data-graph-refresh-pill]": pill,
  "[data-toggle-text]": textToggle,
  "[data-isolated-disclosure]": disclosure,
};
const MANY = {
  "[data-toggle-overlay]": [overlayToggles.hierarchy, overlayToggles.provenance],
  "[data-toggle-isolated]": [isolatedToggle],
  "[data-graph-text]": [layersSection, disclosure, hierarchySection],
};

const document = {
  querySelector(selector) { return SINGLE[selector] || null; },
  querySelectorAll(selector) { return MANY[selector] || []; },
  createElement() { return { dataset: {}, style: {}, appendChild() {} }; },
  addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
};

class EventSource {
  addEventListener(type, fn) { (sse[type] = sse[type] || []).push(fn); }
  close() {}
}

// ── the Cytoscape stub ────────────────────────────────────────────────────
//
// Style resolution is real enough to be worth asking: a rule matches when its
// `node`/`edge` head matches the element kind and every `.class` token is on
// it, and the LAST matching rule wins — which is how the library itself
// resolves a stylesheet.
function selectorMatches(ele, selector) {
  const parts = selector.trim().split(".");
  const head = parts.shift();
  if (head === "node" && ele.kind !== "node") return false;
  if (head === "edge" && ele.kind !== "edge") return false;
  if (head && head !== "node" && head !== "edge") return false;
  return parts.every(function (name) { return ele.classes.has(name); });
}

function cytoscape(options) {
  const style = options.style || [];
  const nodes = [];
  const edges = [];
  const handlers = [];
  function build(spec) {
    const kind = spec.data.source ? "edge" : "node";
    const ele = {
      kind,
      classes: new Set((spec.classes || "").split(" ").filter(Boolean)),
      inline: {},
      id() { return spec.data.id; },
      data(key) { return key === undefined ? spec.data : spec.data[key]; },
      hasClass(name) { return ele.classes.has(name); },
      addClass(name) { ele.classes.add(name); return ele; },
      removeClass(name) { ele.classes.delete(name); return ele; },
      at: { x: 0, y: 0 },
      position(next) {
        if (next !== undefined) { ele.at = next; return ele; }
        return ele.at;
      },
      style(name, value) {
        if (value !== undefined) { ele.inline[name] = value; return ele; }
        if (name in ele.inline) return ele.inline[name];
        let resolved;
        style.forEach(function (rule) {
          if (selectorMatches(ele, rule.selector) && rule.style[name] !== undefined) {
            resolved = rule.style[name];
          }
        });
        return resolved;
      },
    };
    (kind === "edge" ? edges : nodes).push(ele);
  }
  (options.elements || []).forEach(build);
  const collection = function (all, selector) {
    if (!selector) return all;
    return all.filter(function (ele) { return selectorMatches(ele, selector); });
  };
  return {
    nodes(selector) { return collection(nodes, selector); },
    edges(selector) { return collection(edges, selector); },
    // The overlay edges, which join after the layout so that hierarchy cannot
    // decide the dependency graph's shape.
    add(specs) { specs.forEach(build); },
    on(event, selector, fn) { handlers.push({ event, selector, fn }); },
    // Counted, because "exactly one, ever" is the claim: D8 forbids a second.
    layout() { layouts += 1; return { run() {} }; },
    // A pan and a zoom over the positions that one layout produced — not a
    // layout, which is why it is a separate method here as it is there.
    fit() {},
    _fire(event, id) {
      const target = nodes.concat(edges).filter(function (ele) {
        return ele.id() === id;
      })[0];
      handlers.forEach(function (entry) {
        if (entry.event === event) entry.fn({ target });
      });
    },
  };
}

const sandbox = {
  document,
  EventSource,
  console,
  URL,
  URLSearchParams,
  DOMParser: class {
    parseFromString() { return { querySelector() { return null; } }; }
  },
  // Answered immediately and recorded: the overlay tests assert this list
  // stays EMPTY, and the node-click test needs the panel to actually land.
  fetch: (url) => {
    fetches.push(url);
    return Promise.resolve({ ok: true, text: () => Promise.resolve("panel:" + url) });
  },
};
sandbox.window = {
  LithosLensTasks: {
    selectionParam: "focus",
    panelAliasPath: "/tasks/id",
    panelAliasKey: "task_id",
    liveRefresh: false,
    eventsUrl: "/tasks/events",
  },
  cytoscape,
  setTimeout: (fn) => { return 0; },
  clearTimeout() {},
  setInterval: () => 0,
  clearInterval() {},
  addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
  location: {
    get href() { return href(); },
    // A real navigation, which is what a double-click on a node is.
    set href(value) { entries.push(value); cursor = entries.length - 1; },
  },
  history: {
    pushState(state, title, url) {
      pushed.push(url);
      entries.splice(cursor + 1);
      entries.push(new URL(url, "http://lens.test").href);
      cursor = entries.length - 1;
    },
  },
};
sandbox.window.window = sandbox.window;
Object.assign(sandbox, { setTimeout: sandbox.window.setTimeout });

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(tasksPath, "utf8"), sandbox);
vm.runInContext(fs.readFileSync(graphPath, "utf8"), sandbox);

function fire(type, event) {
  (listeners[type] || []).forEach((listener) => listener(event));
}

function clickOn(map) {
  return {
    button: 0,
    defaultPrevented: false,
    target: { closest: (selector) => map[selector] || null },
    preventDefault() { this.defaultPrevented = true; },
  };
}

let eventSeq = 0;

function snapshot() {
  const drawn = sandbox.window.LithosLensGraph.shown();
  return {
    href: href(),
    nodes: drawn.nodes,
    edges: drawn.edges.map((edge) => edge.type),
    arrowless: drawn.edges
      .filter((edge) => !edge.arrow || edge.arrow === "none")
      .map((edge) => edge.id),
    pillHidden: pill.hidden,
    textHidden: layersSection.hidden,
    panel: host.innerHTML,
    focused: sandbox.window.LithosLensGraph.cy
      .nodes()
      .filter((node) => node.hasClass("focused"))
      .map((node) => node.id()),
    disclosureOpen: disclosure.open,
  };
}

(async () => {
  const states = [];
  for (const action of actions) {
    const [name, argument] = action.split(":");
    if (name === "overlay") {
      fire("click", clickOn({ "[data-toggle-overlay]": overlayToggles[argument] }));
    } else if (name === "isolated") {
      fire("click", clickOn({ "[data-toggle-isolated]": isolatedToggle }));
    } else if (name === "text") {
      fire("click", clickOn({ "[data-toggle-text]": textToggle }));
    } else if (name === "back") {
      if (cursor > 0) cursor -= 1;
      fire("popstate", {});
    } else if (name === "forward") {
      if (cursor < entries.length - 1) cursor += 1;
      fire("popstate", {});
    } else if (name === "tap" || name === "dbltap") {
      sandbox.window.LithosLensGraph.cy._fire(name, argument);
    } else if (name === "event") {
      eventSeq += 1;
      (sse["task.updated"] || []).forEach((listener) => listener({
        lastEventId: "event-" + eventSeq,
        data: JSON.stringify({
          type: "task.updated", task_id: argument, requires_refresh: true,
        }),
      }));
    } else if (name === "finding") {
      eventSeq += 1;
      (sse["finding.posted"] || []).forEach((listener) => listener({
        lastEventId: "event-" + eventSeq,
        data: JSON.stringify({ type: "finding.posted", task_id: argument }),
      }));
    }
    await new Promise((resolve) => setImmediate(resolve));
    states.push(snapshot());
  }
  await new Promise((resolve) => setImmediate(resolve));
  console.log(JSON.stringify({
    states,
    final: snapshot(),
    pushed,
    fetches,
    layouts,
    positions: sandbox.window.LithosLensGraph.positions(),
  }));
})();
"""

GRAPH_JS = Path(__file__).resolve().parents[1] / "src/lithos_lens/static/graph.js"

# One scope's payload in the shape `graph_view.payload_json` emits, carrying
# every branch the canvas has a rule for: a drawable cycle, a dependency ghost,
# a CONTEXT ghost reachable only through its provenance edge, an isolated node
# whose only edge is hierarchy, an inactive edge and an unknown one.
GRAPH_PAYLOAD: dict = {
    "scope": {
        "kind": "project",
        "key": "loom",
        "include_resolved": False,
        "focus": "",
        "overlays": [],
        "isolated": False,
    },
    "nodes": [
        {
            "id": "epic",
            "label": "Epic",
            "status": "open",
            "type": "epic",
            "layer": 0,
            "ghost": False,
            "ghost_kind": "",
            "projects": ["loom"],
            "completeness": "ok",
            "claims": [],
            "detail_url": "/tasks/epic",
            "cycle": "",
            "flagged": False,
            "cycle_unknown": False,
            "blocked_via_cycle": False,
            "isolated": True,
        },
        {
            "id": "schema",
            "label": "Schema",
            "status": "open",
            "type": "task",
            "layer": 0,
            "ghost": False,
            "ghost_kind": "",
            "projects": ["loom"],
            "completeness": "ok",
            "claims": ["agent-zero"],
            "detail_url": "/tasks/schema",
            "cycle": "",
            "flagged": False,
            "cycle_unknown": False,
            "blocked_via_cycle": False,
            "isolated": False,
        },
        {
            "id": "ship",
            "label": "Ship",
            "status": "open",
            "type": "task",
            "layer": 1,
            "ghost": False,
            "ghost_kind": "",
            "projects": ["loom"],
            "completeness": "ok",
            "claims": [],
            "detail_url": "/tasks/ship",
            "cycle": "",
            "flagged": False,
            "cycle_unknown": False,
            "blocked_via_cycle": False,
            "isolated": False,
        },
        {
            "id": "cycle-a",
            "label": "A",
            "status": "open",
            "type": "task",
            "layer": 0,
            "ghost": False,
            "ghost_kind": "",
            "projects": ["loom"],
            "completeness": "ok",
            "claims": [],
            "detail_url": "/tasks/cycle-a",
            "cycle": "cycle-a",
            "flagged": True,
            "cycle_unknown": False,
            "blocked_via_cycle": False,
            "isolated": False,
        },
        {
            "id": "cycle-b",
            "label": "B",
            "status": "open",
            "type": "task",
            "layer": 0,
            "ghost": False,
            "ghost_kind": "",
            "projects": ["loom"],
            "completeness": "ok",
            "claims": [],
            "detail_url": "/tasks/cycle-b",
            "cycle": "cycle-a",
            "flagged": True,
            "cycle_unknown": False,
            "blocked_via_cycle": False,
            "isolated": False,
        },
        {
            "id": "far",
            "label": "Far",
            "status": "open",
            "type": "task",
            "layer": 2,
            "ghost": True,
            "ghost_kind": "dependency",
            "projects": ["lens"],
            "completeness": "ok",
            "claims": [],
            "detail_url": "/tasks/far",
            "cycle": "",
            "flagged": False,
            "cycle_unknown": False,
            "blocked_via_cycle": False,
            "isolated": False,
        },
        {
            "id": "source",
            "label": "Source",
            "status": "completed",
            "type": "task",
            "layer": 0,
            "ghost": True,
            "ghost_kind": "context",
            "projects": ["loom"],
            "completeness": "ok",
            "claims": [],
            "detail_url": "/tasks/source",
            "cycle": "",
            "flagged": False,
            "cycle_unknown": False,
            "blocked_via_cycle": False,
            "isolated": False,
        },
        {
            "id": "note",
            "label": "Note",
            "status": "open",
            "type": "task",
            "layer": 0,
            "ghost": False,
            "ghost_kind": "",
            "projects": ["loom"],
            "completeness": "ok",
            "claims": [],
            "detail_url": "/tasks/note",
            "cycle": "",
            "flagged": False,
            "cycle_unknown": False,
            "blocked_via_cycle": False,
            "isolated": True,
        },
    ],
    "edges": [
        {
            "from": "schema",
            "to": "ship",
            "type": "blocks",
            "state": "active",
            "reason": "",
        },
        {
            "from": "ship",
            "to": "far",
            "type": "blocks",
            "state": "active",
            "reason": "",
        },
        {
            "from": "cycle-a",
            "to": "cycle-b",
            "type": "blocks",
            "state": "active",
            "reason": "",
        },
        {
            "from": "cycle-b",
            "to": "cycle-a",
            "type": "blocks",
            "state": "active",
            "reason": "",
        },
        {
            "from": "epic",
            "to": "schema",
            "type": "parent_child",
            "state": "",
            "reason": "",
        },
        {
            "from": "epic",
            "to": "note",
            "type": "parent_child",
            "state": "",
            "reason": "",
        },
        {
            "from": "source",
            "to": "note",
            "type": "discovered_from",
            "state": "",
            "reason": "",
        },
    ],
    "layers": [["schema", "cycle-a", "cycle-b"], ["ship"], ["far"]],
    "cycles": [
        {
            "id": "cycle-a",
            "members": ["cycle-a", "cycle-b"],
            "path": ["cycle-a", "cycle-b", "cycle-a"],
            "scc": True,
            "flagged": True,
            "message": "Dependency cycle.",
        },
    ],
    "ghosts": ["far", "source"],
    "longest_chain": {
        "nodes": ["schema", "ship", "far"],
        "length": 3,
        "bound": "exact",
    },
    "roots": ["schema", "cycle-a", "epic", "source"],
    "isolated": ["epic", "note"],
    "incomplete": {},
    "as_of": "2026-09-14T10:00:00+00:00",
}

#: This harness's own address. Deliberately not the panel harness's
#: ``GRAPH_HREF`` above: that one carries overlays and an isolated toggle
#: already applied, and the canvas tests below start from the page's defaults.
GRAPH_CANVAS_HREF = "http://lens.test/tasks/graph?project=loom"


def _graph_run(
    actions: list[str], href: str = GRAPH_CANVAS_HREF, payload: dict | None = None
) -> dict:
    """Load tasks.js then graph.js against one embedded payload, run ``actions``.

    The order matters and is the page's own: ``graph.js`` opens the side panel
    through the API ``tasks.js`` publishes, so a harness that loaded only the
    second would be testing a graph page that cannot open a panel at all.
    """
    assert NODE is not None
    result = subprocess.run(
        [
            NODE,
            "-e",
            GRAPH_HARNESS,
            "--",
            str(TASKS_JS),
            str(GRAPH_JS),
            href,
            json.dumps(actions),
            json.dumps(payload or GRAPH_PAYLOAD),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def test_the_default_canvas_draws_dependency_edges_with_an_arrowhead_on_each() -> None:
    """D8's default view: `blocks` and `waits_on_gate`, arrowheads throughout.

    The overlays are in the payload from the first render (D6 resolves them so
    a toggle needs no fetch) and must nevertheless be OFF, along with the
    context ghost that only a provenance edge anchors — otherwise the default
    picture is the hairball the overlay decision exists to prevent.
    """
    result = _graph_run([])
    final = result["final"]

    assert sorted(final["edges"]) == ["blocks"] * 4
    assert final["arrowless"] == [], "an edge whose direction cannot be read"
    assert "source" not in final["nodes"], "context ghost drawn with its overlay off"
    # The dependency ghost is not a context one: it is a live cross-project
    # blocker and belongs in the default picture (D5).
    assert "far" in final["nodes"]
    # Isolated tasks are folded away on a project scope …
    assert "epic" not in final["nodes"]
    assert "note" not in final["nodes"]
    # … and nothing was fetched to decide any of it.
    assert result["fetches"] == []


def test_toggling_hierarchy_adds_the_parent_child_edges_and_the_url_remembers() -> None:
    """The acceptance criterion, both halves: the edges appear and
    `overlays=hierarchy` lands on the URL — from the payload the page already
    had, with no request."""
    result = _graph_run(["overlay:hierarchy"])
    final = result["final"]

    assert "parent_child" in final["edges"]
    assert final["edges"].count("parent_child") == 2
    assert result["pushed"] == ["/tasks/graph?project=loom&overlays=hierarchy"]
    assert result["fetches"] == []
    # The overlay pulls its own endpoints in: an epic's whole hierarchy hangs
    # off a node with no dependency edge of its own, and folding that away
    # would hide the very edges the toggle just asked for.
    assert "epic" in final["nodes"]
    assert final["arrowless"] == []


def test_toggling_provenance_shows_the_discovered_from_edge_and_its_context_ghost() -> (
    None
):
    """D6's context ghost, and the point of resolving it on every request: the
    source is a completed task outside the open-only scope, and the toggle has
    to reveal it without going back to Lithos."""
    result = _graph_run(["overlay:provenance"])
    final = result["final"]

    assert "discovered_from" in final["edges"]
    assert "source" in final["nodes"], "the context ghost the edge points from"
    assert "note" in final["nodes"], "the follow-on the edge points to"
    assert result["pushed"] == ["/tasks/graph?project=loom&overlays=provenance"]
    assert result["fetches"] == []


def test_back_after_a_toggle_hides_the_overlay_again_without_a_reload() -> None:
    """`popstate` re-applies the URL's overlays from the static payload (D8) —
    the other half of "remembered in the URL", and the half a page that only
    ever pushed would get wrong."""
    result = _graph_run(["overlay:hierarchy", "overlay:provenance", "back", "back"])
    after_both, after_one, back_to_none = (
        result["states"][1],
        result["states"][2],
        result["states"][3],
    )

    assert "parent_child" in after_both["edges"]
    assert "discovered_from" in after_both["edges"]
    # One step back drops provenance and keeps hierarchy …
    assert "parent_child" in after_one["edges"]
    assert "discovered_from" not in after_one["edges"]
    # … and the second lands on the URL the page loaded with.
    assert "parent_child" not in back_to_none["edges"]
    assert "source" not in back_to_none["nodes"]
    assert result["fetches"] == []


def test_the_isolated_toggle_moves_the_url_and_the_text_disclosure_together() -> None:
    """`isolated=1|0` (D8). The canvas and the disclosure answer the same
    question, so they are never allowed to disagree about it."""
    result = _graph_run(["isolated"])
    final = result["final"]

    assert result["pushed"] == ["/tasks/graph?project=loom&isolated=1"]
    assert "epic" in final["nodes"]
    assert "note" in final["nodes"]
    assert final["disclosureOpen"] is True
    assert result["fetches"] == []


def test_clicking_a_node_opens_that_task_s_panel_and_pushes_focus() -> None:
    """D9's promise, through D8's parameter: the node has no DOM row, so the
    tap travels through the API `tasks.js` publishes — one panel
    implementation, and `focus` is the graph page's only selection key."""
    result = _graph_run(["tap:ship"])

    assert result["fetches"] == ["/tasks/id?task_id=ship&fragment=panel"]
    assert result["final"]["panel"] == "panel:/tasks/id?task_id=ship&fragment=panel"
    assert result["pushed"] == ["/tasks/graph?project=loom&focus=ship"]
    assert result["final"]["focused"] == ["ship"]


def test_a_task_event_for_a_node_raises_the_pill_and_never_re_layouts() -> None:
    """D8: the graph stays still while it is read. The pill is the whole
    response to an event — a re-layout under the operator's cursor is the
    behaviour this replaces."""
    result = _graph_run(["event:ship"])

    assert result["final"]["pillHidden"] is False
    assert result["layouts"] == 1, "the page laid itself out again"
    # And no reconcile: the graph page tells tasks.js not to re-render, or one
    # event would cost a whole graph assembly.
    assert result["fetches"] == []


def test_an_event_for_a_task_that_is_not_on_this_page_raises_nothing() -> None:
    """The pill says "this picture is out of date", so it may only fire when
    the event touches a node actually drawn from this payload."""
    result = _graph_run(["event:somewhere-else"])

    assert result["final"]["pillHidden"] is True


def test_show_as_text_collapses_the_baseline_and_leaves_it_in_the_dom() -> None:
    """D3: the canvas hides the text layers behind a toggle and never removes
    them — the text is the page for a screen reader and a PR screenshot."""
    result = _graph_run(["text", "text"])

    assert result["states"][0]["textHidden"] is False, "the toggle did not reveal it"
    assert result["states"][1]["textHidden"] is True, "the toggle does not close again"
    # Hidden, never detached: the section object is still the one the page
    # rendered, which is what "stays in the DOM" means.
    assert result["final"]["textHidden"] is True


def test_a_focus_in_the_url_opens_its_panel_without_pushing_a_second_entry() -> None:
    """Load with `focus=A` (D8). The address bar already names the selection,
    so re-pushing it would leave a twin entry the first Back appears to
    ignore."""
    result = _graph_run(
        [], href="http://lens.test/tasks/graph?project=loom&focus=cycle-a"
    )

    assert result["fetches"] == ["/tasks/id?task_id=cycle-a&fragment=panel"]
    assert result["pushed"] == []
    assert result["final"]["focused"] == ["cycle-a"]


def test_a_double_click_leaves_for_the_task_s_own_page() -> None:
    """The server built that URL (`tasks.task_detail_path`), because an id that
    collides with a page under `/tasks/` has to be addressed through the query
    alias — a rule the browser never restates."""
    result = _graph_run(["dbltap:ship"])

    assert result["final"]["href"] == "/tasks/ship"
