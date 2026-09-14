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
  readyState: "complete",
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
  // What a DEFERRED script sees (the parser sets `"interactive"` before working
  // through the deferred list); `DOMContentLoaded` is fired below, once the
  // file has run, exactly as the browser does it.
  readyState: "interactive",
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

// Every deferred script has run; the event stream opens here, not earlier.
fire("DOMContentLoaded", {});

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
# overlay toggle pushes, what is drawn afterwards and in what style, that
# NOTHING is fetched to do it, and that a task event never moves the layout.
# None of that is visible to Python (the server renders the same HTML either
# way) and none of it is visible to the e2e suite either, which photographs
# pixels.
#
# It loads BOTH files, in the page's own order, because "one panel
# implementation for rows and nodes" (D9) is exactly the promise that a canvas
# with its own private copy of the panel would stop keeping: a node tap here has
# to travel through `tasks.js`.
#
# And it runs the REAL vendored Cytoscape, headless, inside the same context —
# not a stub. A stub can only answer the questions its author thought to
# reimplement, and every claim this slice makes about the picture (the rank a
# node lands in, the shape its type gives it, which box a cycle member is
# actually inside, whether an arrowhead resolves) is a question for the library
# that will draw it in production. Style resolution, compound parents, layout
# and event delegation are therefore the shipped 3.30.3's own.
GRAPH_HARNESS = """
const fs = require("fs");
const vm = require("vm");

const [
  tasksPath, graphPath, cytoscapePath, initialHref, actionsRaw, payloadRaw,
  reducedMotionRaw, servedPanelRaw, panelFetchRaw, readyStateRaw, lifecycleRaw,
] = process.argv.slice(1);
// Which document lifecycle events the browser fires once both files have run.
// The page's own is `DOMContentLoaded` then `load`; a script that arrived after
// the first of those sees only the second.
const lifecycle = (lifecycleRaw === undefined ? "DOMContentLoaded" : lifecycleRaw)
  .split(",")
  .filter(Boolean);
const actions = JSON.parse(actionsRaw);
const reducedMotion = reducedMotionRaw === "1";
// How the SERVER answers a panel request. A node open that never lands is the
// case the canvas has to walk its optimistic focus ring back from; `hold`
// leaves the response IN FLIGHT until a `release` action lands it, which is
// the only way to put another gesture between a request and its answer.
const panelFetch = panelFetchRaw || "ok";
const heldResponses = [];

const entries = [initialHref];
let cursor = 0;
const pushed = [];
const fetches = [];
const listeners = {};
const sse = {};
const layoutCalls = [];

function href() { return entries[cursor]; }

function element(extra) {
  return Object.assign({
    dataset: {},
    attributes: {},
    hidden: false,
    textContent: "",
    setAttribute(name, value) { this.attributes[name] = value; },
    getAttribute(name) { return this.attributes[name] || ""; },
  }, extra || {});
}

const container = element({ hidden: true });
const panHint = element({ hidden: true });
const host = element({
  _html: "",
  get innerHTML() { return this._html; },
  set innerHTML(value) { this._html = value; },
});
// What the SERVER rendered into the host for a request carrying `focus=`
// (D9's no-JS baseline). When it is present the client must NOT fetch the
// panel again; when it is absent the client is the only thing that can open it.
if (servedPanelRaw) {
  host.dataset.panelSelected = servedPanelRaw;
  host.dataset.panelUrl = "/tasks/" + servedPanelRaw + "?fragment=panel";
  host.innerHTML = "panel:server:" + servedPanelRaw;
}
const payloadScript = element({ textContent: payloadRaw });
const pill = element({ hidden: true });
const textToggle = element({ hidden: true });
const disclosure = element({ open: false });
const layersSection = element();
const hierarchySection = element();
const isolatedToggle = element();
const resolvedToggle = element();
const overlayToggles = {
  hierarchy: element({ dataset: { toggleOverlay: "hierarchy" } }),
  provenance: element({ dataset: { toggleOverlay: "provenance" } }),
};

const SINGLE = {
  "[data-graph-canvas]": container,
  "[data-graph-pan-hint]": panHint,
  "[data-graph-payload]": payloadScript,
  "[data-panel-host]": host,
  "[data-graph-refresh-pill]": pill,
  "[data-toggle-text]": textToggle,
  "[data-isolated-disclosure]": disclosure,
};
const MANY = {
  "[data-toggle-overlay]": [overlayToggles.hierarchy, overlayToggles.provenance],
  "[data-toggle-isolated]": [isolatedToggle],
  "[data-toggle-resolved]": [resolvedToggle],
  "[data-graph-refresh-pill]": [pill],
  "[data-graph-text]": [layersSection, disclosure, hierarchySection],
};

const document = {
  // The page's real entry state. A DEFERRED script runs at `"interactive"` —
  // the parser sets that before working through the deferred list — and
  // `DOMContentLoaded` is what says they are all done. This harness reproduces
  // that sequence below rather than pretending the document was complete.
  readyState: readyStateRaw || "interactive",
  querySelector(selector) { return SINGLE[selector] || null; },
  querySelectorAll(selector) { return MANY[selector] || []; },
  createElement() {
    // Cytoscape's headless renderer touches no DOM; `tasks.js` builds a span
    // to escape HTML with, which is the only element anything here creates.
    return { dataset: {}, style: {}, appendChild() {}, getContext() { return {}; } };
  },
  addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
};

// COUNTED, not merely recorded: `connect()` closes any open stream and opens a
// new one, so "a connection exists" cannot tell one call from two — and two is
// what a page that fires both lifecycle events gets without the idempotence
// guard.
let eventSources = 0;
class EventSource {
  constructor() { eventSources += 1; }
  addEventListener(type, fn) { (sse[type] = sse[type] || []).push(fn); }
  close() {}
}

const sandbox = {
  document,
  EventSource,
  console,
  URL,
  URLSearchParams,
  Math,
  Date,
  JSON,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  DOMParser: class {
    parseFromString() { return { querySelector() { return null; } }; }
  },
  // Answered immediately and recorded: the overlay tests assert this list
  // stays EMPTY, and the node-click test needs the panel to actually land.
  fetch: (url) => {
    fetches.push(url);
    if (panelFetch === "reject") return Promise.reject(new Error("network is down"));
    if (panelFetch === "fail") {
      return Promise.resolve({ ok: false, text: () => Promise.resolve("") });
    }
    if (panelFetch === "body") {
      return Promise.resolve({
        ok: true,
        text: () => Promise.reject(new Error("connection reset mid-body")),
      });
    }
    if (panelFetch === "hold") {
      return new Promise((resolve) => {
        heldResponses.push(() => resolve({
          ok: true, text: () => Promise.resolve("panel:" + url),
        }));
      });
    }
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
  matchMedia: (query) => ({
    matches: reducedMotion && query.indexOf("reduced-motion") !== -1,
  }),
  setTimeout: (fn) => 0,
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

// The shipped bundle, in this context. Its UMD wrapper has no `module` here, so
// it publishes onto the contextified global; `graph.js` reads `window`, and the
// wrapper below is what bridges the two — and what records the layout options,
// which are otherwise invisible once the library has consumed them.
vm.runInContext(fs.readFileSync(cytoscapePath, "utf8"), sandbox);
// The wrapper is built INSIDE the context, not here. Cytoscape's entry point
// asks whether its argument is a plain object, and an object minted in another
// realm is not one — called from this side it silently returns `undefined`.
// The recorder is the only thing that crosses, because a function may.
sandbox.__recordLayout = (info) => { layoutCalls.push(info); };
vm.runInContext(`
  window.cytoscape = function (options) {
    var cy = cytoscape(Object.assign({}, options, {
      // No container and no renderer: positions, styles, compound parents and
      // event delegation are all computed without one.
      container: null,
      headless: true,
      styleEnabled: true
    }));
    var layout = cy.layout.bind(cy);
    cy.layout = function (opts) {
      __recordLayout({
        name: opts.name,
        directed: opts.directed === true,
        animate: opts.animate === true,
        roots: (opts.roots || []).map(function (node) { return node.id(); }).sort(),
        // What the layout could SEE. The overlays are added afterwards on
        // purpose, so hierarchy cannot decide the dependency graph's shape.
        edgeTypes: cy.edges().map(function (edge) { return edge.data("type"); }).sort()
      });
      return layout(opts);
    };
    return cy;
  };
`, sandbox);

vm.runInContext(fs.readFileSync(tasksPath, "utf8"), sandbox);

// THE WINDOW THE FINDING IS ABOUT. On the real page a ~400KB Cytoscape bundle
// is fetched and parsed between these two files, and `graph.js` is what
// subscribes for the pill. Anything the stream consumed here would be
// deduplicated away with no subscriber to hear it — so the stream must not be
// open yet.
const streamBeforeGraph = Object.keys(sse).length > 0;

vm.runInContext(fs.readFileSync(graphPath, "utf8"), sandbox);

// …and then the browser announces where in the lifecycle it has got to.
lifecycle.forEach((event) => {
  (listeners[event] || []).forEach((listener) => listener({}));
});

const graph = sandbox.window.LithosLensGraph;

// Cytoscape's element ids are opaque (graph.js explains why: the shipped
// bundle throws on an element called `__proto__`), so this harness names
// elements the way the PAYLOAD does — the only vocabulary a test should need.
const payload = JSON.parse(payloadRaw);
const nameOf = Object.create(null);
(payload.nodes || []).forEach((node) => {
  const element = graph.node(node.id);
  if (element.length) nameOf[element.id()] = node.id;
});
(payload.cycles || []).forEach((cycle) => {
  if (!cycle.scc) return;
  const element = graph.cycle(cycle.id);
  if (element.length) nameOf[element.id()] = "cycle::" + cycle.id;
});
const named = (element) => nameOf[element.id()] || element.id();

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

// Every toolbar control is a REAL LINK (it has to be: the no-JS page is the
// baseline), so a click the page does not intercept is a main-document
// navigation — the browser follows the `href` and replaces the page, payload,
// canvas and all. `fetch` cannot see that, which is why "the overlays cost no
// request" is not provable from the fetch log alone: a handler that forgot
// `preventDefault()` would reload `/tasks/graph` on every toggle and leave
// that log empty. So the browser's own default action is modelled here, and
// the tests assert this list stays empty.
const navigations = [];

function clickLink(selector, target) {
  const event = clickOn({ [selector]: target });
  fire("click", event);
  if (event.defaultPrevented) return;
  const href = target.getAttribute ? target.getAttribute("href") : "";
  if (href) navigations.push(href);
}

let eventSeq = 0;

function snapshot() {
  const drawn = graph.shown();
  return {
    href: href(),
    nodes: drawn.nodes,
    edges: drawn.edges.map((edge) => edge.type),
    arrowless: drawn.edges
      .filter((edge) => !edge.arrow || edge.arrow === "none")
      .map((edge) => edge.id),
    pillHidden: pill.hidden,
    canvasHidden: container.hidden,
    // The partial-view state, which is a CLAIM about what is on screen and so
    // has to follow every transition that changes what is drawn.
    clipped: container.dataset.canvasClipped || "",
    panHintHidden: panHint.hidden,
    textHidden: layersSection.hidden,
    panel: host.innerHTML,
    focused: graph.cy
      .nodes()
      .filter((node) => node.hasClass("focused"))
      .map(named),
    disclosureOpen: disclosure.open,
    hrefs: {
      resolved: resolvedToggle.getAttribute("href"),
      pill: pill.getAttribute("href"),
      hierarchy: overlayToggles.hierarchy.getAttribute("href"),
      isolated: isolatedToggle.getAttribute("href"),
    },
  };
}

// Everything the LIBRARY resolved, which is the point of running the real one:
// a class this file never asserts on is a class the page can lose silently.
function styles() {
  // Null-prototype, because a task may legitimately be called `__proto__` and
  // `out["__proto__"] = …` on a plain object stores nothing at all.
  const out = Object.create(null);
  graph.cy.elements().forEach((ele) => {
    const node = ele.isNode();
    out[named(ele)] = {
      kind: node ? "node" : "edge",
      classes: ele.classes().slice().sort(),
      parent: node && ele.parent().length ? named(ele.parent()) : "",
      source: node ? "" : named(ele.source()),
      target: node ? "" : named(ele.target()),
      type: ele.data("type") || "",
      display: ele.style("display"),
      opacity: Number(ele.style("opacity")),
      shape: node ? ele.style("shape") : "",
      background: node ? ele.style("background-color") : "",
      backgroundOpacity: node ? parseFloat(ele.style("background-opacity")) : 0,
      blacken: node ? Number(ele.style("background-blacken")) : 0,
      borderStyle: ele.style("border-style"),
      borderColor: ele.style("border-color"),
      borderWidth: parseFloat(ele.style("border-width")),
      overlayOpacity: node ? Number(ele.style("overlay-opacity")) : 0,
      lineStyle: node ? "" : ele.style("line-style"),
      lineColor: node ? "" : ele.style("line-color"),
      // "px"-suffixed, so parsed rather than cast: `Number("1.6px")` is NaN,
      // and a NaN serialises to null and compares equal to nothing.
      width: parseFloat(ele.style("width")),
      arrow: node ? "" : ele.style("target-arrow-shape"),
      // Its own field, because the SHAPE and the COLOUR are separate rules and
      // a trace whose arrowhead kept the default grey would still be "an edge
      // with an arrowhead" to a test that only read the shape.
      arrowColor: node ? "" : ele.style("target-arrow-color"),
    };
  });
  return out;
}

function ranks() {
  const out = Object.create(null);
  graph.cy.nodes().forEach((node) => {
    out[named(node)] = Math.round(node.position().y * 100) / 100;
  });
  return out;
}

(async () => {
  const states = [];
  for (const action of actions) {
    const [name, argument] = action.split(":");
    if (name === "overlay") {
      clickLink("[data-toggle-overlay]", overlayToggles[argument]);
    } else if (name === "isolated") {
      clickLink("[data-toggle-isolated]", isolatedToggle);
    } else if (name === "text") {
      clickLink("[data-toggle-text]", textToggle);
    } else if (name === "back") {
      if (cursor > 0) cursor -= 1;
      fire("popstate", {});
    } else if (name === "forward") {
      if (cursor < entries.length - 1) cursor += 1;
      fire("popstate", {});
    } else if (name === "close") {
      fire("click", clickOn({ "[data-panel-close]": {} }));
    } else if (name === "escape") {
      fire("keydown", { key: "Escape" });
    } else if (name === "tap" || name === "firsttap" || name === "dbltap") {
      // The SEQUENCE the library emits, not one event out of it. Cytoscape
      // emits a `tap` per click and then decides by TIME alone: a second tap
      // inside `multiClickDebounceTime` makes the pair a `dbltap` and the held
      // `onetap` is dropped; nothing else makes it a `onetap`. A harness that
      // emitted only `tap` would call every gesture the same thing, which is
      // precisely the confusion the page has to keep apart.
      //
      // `dbltap:a~b` is the same window closing across two DIFFERENT targets —
      // the library compares only the time since the previous tap, never what
      // it was on, so two quick clicks on unrelated nodes are a `dbltap` on
      // the second. `dbltap:~b` is the background then a node. Both are real
      // gestures an operator makes, and neither is that node's double-click.
      const ids = (argument || "").split("~");
      const target = graph.node(ids[ids.length - 1]);
      if (name === "dbltap") {
        const opening = ids.length > 1 ? ids[0] : ids[ids.length - 1];
        if (opening) graph.node(opening).emit("tap");
        else graph.cy.emit("tap"); // the empty background taps as the core
      }
      target.emit("tap");
      if (name === "tap") target.emit("onetap");
      else if (name === "dbltap") target.emit("dbltap");
      // `firsttap` stops at the one tap: the first half of a double-click,
      // with the window still open and nothing settled.
    } else if (name === "secondtap") {
      // The CLOSING half of a double-click whose first half was `firsttap`:
      // the tap the library emits for the second click, then the `dbltap` the
      // pair makes. `firsttap:x` followed by `secondtap:x` emits exactly what
      // `dbltap:x` does, with somewhere to put an action in between.
      const target = graph.node(argument);
      target.emit("tap");
      target.emit("dbltap");
    } else if (name === "release") {
      // Every panel response held so far, answered now — an older request
      // landing in the middle of whatever gesture the actions have reached.
      heldResponses.splice(0).forEach((land) => land());
    } else if (name === "event") {
      eventSeq += 1;
      (sse["task.updated"] || []).forEach((listener) => listener({
        lastEventId: "event-" + eventSeq,
        data: JSON.stringify({
          type: "task.updated", task_id: argument, requires_refresh: true,
        }),
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
    navigations,
    // The roots the library was handed, named the way the payload names them.
    streamBeforeGraph,
    streamOpen: Object.keys(sse).length > 0,
    eventSources,
    layouts: layoutCalls.map((call) => Object.assign({}, call, {
      roots: call.roots.map((id) => nameOf[id] || id).sort(),
    })),
    styles: styles(),
    ranks: ranks(),
    positions: graph.positions(),
    // A claimed node BREATHES unless the operator asked for stillness; the
    // library is the only thing that can say whether an animation is running.
    animating: graph.cy
      .nodes()
      .filter((node) => node.animated())
      .map(named)
      .sort(),
  }));
  // The pulse re-arms itself forever, so this process will not end on its own.
  process.exit(0);
})();
"""

GRAPH_JS = Path(__file__).resolve().parents[1] / "src/lithos_lens/static/graph.js"
#: The SHIPPED bundle, not a copy: a harness that stubbed it could only answer
#: the questions its author remembered to reimplement.
CYTOSCAPE_JS = (
    Path(__file__).resolve().parents[1]
    / "src/lithos_lens/static/vendor/cytoscape.min.js"
)


def _node(
    task_id: str,
    *,
    layer: int = 0,
    status: str = "open",
    task_type: str = "task",
    ghost: str = "",
    projects: tuple[str, ...] = ("loom",),
    completeness: str = "ok",
    claims: tuple[str, ...] = (),
    cycle: str = "",
    flagged: bool = False,
    isolated: bool = False,
    detail_url: str = "",
) -> dict:
    """One payload node in the shape `graph_view.payload_json` emits."""
    return {
        "id": task_id,
        "label": task_id.title(),
        "status": status,
        "type": task_type,
        "layer": layer,
        "ghost": bool(ghost),
        "ghost_kind": ghost,
        "projects": list(projects),
        "completeness": completeness,
        "claims": list(claims),
        "detail_url": detail_url or f"/tasks/{task_id}",
        "cycle": cycle,
        "flagged": flagged,
        "cycle_unknown": False,
        "blocked_via_cycle": False,
        "isolated": isolated,
    }


def _edge(
    from_id: str,
    to_id: str,
    edge_type: str = "blocks",
    state: str = "active",
    reason: str = "",
) -> dict:
    return {
        "from": from_id,
        "to": to_id,
        "type": edge_type,
        "state": state,
        "reason": reason,
    }


def _payload(nodes: list[dict], edges: list[dict], **scope: object) -> dict:
    """A whole payload around one node/edge set, with the rest defaulted.

    Everything the server computes and the canvas only reads — layers, the
    chain, roots, the isolated fold — is passed per fixture, because those ARE
    the claims under test: the picture has to agree with them, not re-derive
    them.
    """
    layers: dict[int, list[str]] = {}
    folded = [node["id"] for node in nodes if node["isolated"]]
    for node in nodes:
        if node["id"] not in folded:
            layers.setdefault(int(node["layer"]), []).append(node["id"])
    body = {
        "scope": {
            "kind": "project",
            "key": "loom",
            "include_resolved": False,
            "focus": "",
            "overlays": [],
            "isolated": False,
        },
        "nodes": nodes,
        "edges": edges,
        "layers": [
            layers.get(index, []) for index in range(max(layers, default=0) + 1)
        ],
        "cycles": [],
        "ghosts": [node["id"] for node in nodes if node["ghost"]],
        "longest_chain": {"nodes": [], "length": 0, "bound": "exact"},
        "roots": [],
        "isolated": folded,
        "incomplete": {},
        "as_of": "2026-09-14T10:00:00+00:00",
    }
    scope_overrides = {
        key: scope.pop(key) for key in list(scope) if key in body["scope"]
    }
    body["scope"].update(scope_overrides)  # type: ignore[union-attr]
    body.update(scope)
    # The chain's own condensation membership, which the server always states
    # (`graph_layout.BlockingChain.members`) because it is the ACTIVE
    # projection's partition and nothing else in the payload carries it. A
    # fixture that names no members is one where every chain node stands alone,
    # which is the ordinary case — the two that are not spell it out.
    chain = body["longest_chain"]
    assert isinstance(chain, dict)
    chain.setdefault("members", [[node] for node in chain["nodes"]])
    return body


# The main fixture: one scope carrying every branch the canvas has a rule for —
# a drawable cycle, a dependency ghost, a CONTEXT ghost reachable only through
# its provenance edge, an isolated node whose only edge is hierarchy, a gate and
# its `waits_on_gate` edge, a completed predecessor on an inactive edge, a
# cancelled task, a ghost whose status could not be read and the `unknown` edge
# that follows from it, and a claimed task. Every clause of D8's styling has
# something here to be wrong about.
GRAPH_PAYLOAD: dict = _payload(
    [
        _node("epic", task_type="epic", isolated=True),
        _node("schema", claims=("agent-zero",)),
        _node("ship", layer=1),
        _node("announce", layer=2),
        _node("gate", task_type="gate", layer=1),
        _node("done", status="completed"),
        _node("stopped", status="cancelled"),
        _node("stranded", layer=1),
        _node("cycle-a", cycle="cycle-a", flagged=True),
        _node("cycle-b", cycle="cycle-a", flagged=True),
        _node("far", layer=2, ghost="dependency", projects=("lens",)),
        _node(
            "unread",
            ghost="dependency",
            status="unknown",
            completeness="status_unknown",
            projects=("lens",),
        ),
        _node("source", status="completed", ghost="context"),
        _node("note", isolated=True),
        # The id that collides with a page under `/tasks/`: it can only be
        # addressed through the query alias, and the server said so.
        _node("graph", layer=2, detail_url="/tasks/id?task_id=graph"),
    ],
    [
        _edge("schema", "ship"),
        _edge("ship", "far"),
        _edge("ship", "graph"),
        _edge("gate", "announce", "waits_on_gate"),
        _edge("done", "ship", state="inactive", reason="satisfied"),
        _edge("stopped", "stranded"),
        _edge("unread", "announce", state="unknown"),
        _edge("cycle-a", "cycle-b"),
        _edge("cycle-b", "cycle-a"),
        _edge("epic", "schema", "parent_child", state=""),
        _edge("epic", "note", "parent_child", state=""),
        _edge("source", "note", "discovered_from", state=""),
    ],
    cycles=[
        {
            "id": "cycle-a",
            "members": ["cycle-a", "cycle-b"],
            "path": ["cycle-a", "cycle-b", "cycle-a"],
            "scc": True,
            "flagged": True,
            "message": "Dependency cycle.",
        }
    ],
    longest_chain={"nodes": ["schema", "ship", "far"], "length": 3, "bound": "exact"},
    roots=["schema", "gate", "done", "stopped", "cycle-a", "epic", "source", "unread"],
)

# The reviewer's own DAG (correctness f-001): `A → B → C → D` plus `A → D`. The
# server layers it by LONGEST path, so D is layer 3; `breadthfirst` ranks by
# shortest path and would draw D level with B.
DIAMOND_PAYLOAD: dict = _payload(
    [_node("a"), _node("b", layer=1), _node("c", layer=2), _node("d", layer=3)],
    [_edge("a", "b"), _edge("b", "c"), _edge("c", "d"), _edge("a", "d")],
    roots=["a"],
)

# A chain that crosses a cycle (correctness f-003). The server condenses the
# cycle to its representative `cyc-a`, so the chain reads `p → cyc-a → d` while
# the edge that actually enters it is `p → cyc-b`.
CYCLE_CHAIN_PAYLOAD: dict = _payload(
    [
        _node("p"),
        _node("cyc-a", layer=1, cycle="cyc-a", flagged=True),
        _node("cyc-b", layer=1, cycle="cyc-a", flagged=True),
        _node("d", layer=2),
    ],
    [
        _edge("p", "cyc-b"),
        _edge("cyc-a", "cyc-b"),
        _edge("cyc-b", "cyc-a"),
        _edge("cyc-a", "d"),
    ],
    cycles=[
        {
            "id": "cyc-a",
            "members": ["cyc-a", "cyc-b"],
            "path": ["cyc-a", "cyc-b", "cyc-a"],
            "scc": True,
            "flagged": True,
            "message": "Dependency cycle.",
        }
    ],
    longest_chain={
        "nodes": ["p", "cyc-a", "d"],
        "length": 3,
        "bound": "exact",
        "members": [["p"], ["cyc-a", "cyc-b"], ["d"]],
    },
    roots=["p"],
)

# A drawn cycle the ACTIVE projection does not agree is one (round-9
# correctness f-001). `mix-a → mix-b` is live; `mix-b → mix-c` is inactive
# (its dependent completed) and `mix-c → mix-a` is inactive (its predecessor
# completed). Every dependency edge makes ONE SCC — the box the picture draws —
# while the active projection makes three nodes, where the longest chain is the
# two-step `mix-a → mix-b` the text states. The partitions differ legitimately,
# so the chain names its OWN membership and the canvas must read that.
MIXED_ACTIVE_CYCLE_PAYLOAD: dict = _payload(
    [
        _node("mix-a", cycle="mix-a", flagged=True),
        _node("mix-b", cycle="mix-a", flagged=True),
        _node("mix-c", status="completed", cycle="mix-a", flagged=True),
    ],
    [
        _edge("mix-a", "mix-b"),
        _edge("mix-b", "mix-c", state="inactive", reason="dependent_resolved"),
        _edge("mix-c", "mix-a", state="inactive", reason="satisfied"),
    ],
    cycles=[
        {
            "id": "mix-a",
            "members": ["mix-a", "mix-b", "mix-c"],
            "path": ["mix-a", "mix-b", "mix-c", "mix-a"],
            "scc": True,
            "flagged": True,
            "message": "Dependency cycle.",
        }
    ],
    longest_chain={
        "nodes": ["mix-a", "mix-b"],
        "length": 2,
        "bound": "exact",
        "members": [["mix-a"], ["mix-b"]],
    },
    roots=["mix-a"],
)

# An EPIC scope, where the isolated default is the other way round (D8): an
# epic's edge-less children are its progress, so they are shown.
EPIC_PAYLOAD: dict = _payload(
    [_node("child", isolated=True), _node("head"), _node("tail", layer=1)],
    [_edge("head", "tail")],
    kind="epic",
    key="loom-epic",
    isolated=True,
    roots=["head", "child"],
)

# A scope whose whole picture is isolates (round-8 correctness f-011): tasks
# with no edge between them at all, so "show isolated" is the only thing that
# puts anything on the canvas and folding them away again empties it outright.
# That is the boundary the partial-view notice has to survive — a real shape
# for a project whose open work has not been linked up yet.
ISOLATED_ONLY_PAYLOAD: dict = _payload(
    [_node(name, isolated=True) for name in ("alone", "apart", "aside")],
    [],
    roots=[],
)

# A cycle big enough to overflow a fixed row pitch (round-2 correctness f-001):
# `P → C0`, `C0 → C1 → … → C4 → C0`, `C0 → D`. The server condenses the five
# members into ONE layer-1 node, so the picture owes them one rank band however
# tall the stack inside it has to be. Nothing bounds an SCC below the 300-node
# scope guard, so this is a boundary, not a malformed payload.
BIG_CYCLE_MEMBERS = ["c0", "c1", "c2", "c3", "c4"]
BIG_CYCLE_PAYLOAD: dict = _payload(
    [_node("p")]
    + [_node(member, layer=1, cycle="c0", flagged=True) for member in BIG_CYCLE_MEMBERS]
    + [_node("d", layer=2)],
    [_edge("p", "c0")]
    + [
        _edge(member, BIG_CYCLE_MEMBERS[(index + 1) % len(BIG_CYCLE_MEMBERS)])
        for index, member in enumerate(BIG_CYCLE_MEMBERS)
    ]
    + [_edge("c0", "d")],
    cycles=[
        {
            "id": "c0",
            "members": BIG_CYCLE_MEMBERS,
            "path": [*BIG_CYCLE_MEMBERS, "c0"],
            "scc": True,
            "flagged": True,
            "message": "Dependency cycle.",
        }
    ],
    longest_chain={
        "nodes": ["p", "c0", "d"],
        "length": 3,
        "bound": "exact",
        "members": [["p"], BIG_CYCLE_MEMBERS, ["d"]],
    },
    roots=["p"],
)

# The same two tasks, two relations (round-2 correctness f-006): an active
# `blocks` edge that IS a step of the chain, and a `discovered_from` edge
# running beside it that is not — the chain is over the active projection, and
# a provenance link is not a blocking one however parallel it looks.
PARALLEL_OVERLAY_PAYLOAD: dict = _payload(
    [_node("up"), _node("down", layer=1)],
    [
        _edge("up", "down"),
        _edge("up", "down", "discovered_from", state=""),
        _edge("up", "down", "parent_child", state=""),
    ],
    longest_chain={"nodes": ["up", "down"], "length": 2, "bound": "exact"},
    roots=["up"],
)

# Task ids are arbitrary non-empty strings (`tasks.py`), and this payload says
# so out loud (round-2 correctness f-007): names that are `Object.prototype`
# members, a pair whose ids collide under a naive `from::to` key, and one
# spelled exactly like the compound parent a cycle representative would be
# given.
HOSTILE_IDS_PAYLOAD: dict = _payload(
    [
        _node("__proto__", cycle="__proto__", flagged=True),
        _node("constructor", cycle="__proto__", flagged=True),
        _node("toString", layer=1),
        _node("a::b", layer=1),
        _node("c", layer=2),
        _node("a", layer=1),
        _node("b::c", layer=2),
        _node("cycle::__proto__", layer=2),
    ],
    [
        _edge("__proto__", "constructor"),
        _edge("constructor", "__proto__"),
        _edge("__proto__", "toString"),
        # The pair a `from::to` key cannot tell apart.
        _edge("a::b", "c"),
        _edge("a", "b::c"),
        _edge("toString", "cycle::__proto__"),
    ],
    cycles=[
        {
            "id": "__proto__",
            "members": ["__proto__", "constructor"],
            "path": ["__proto__", "constructor", "__proto__"],
            "scc": True,
            "flagged": True,
            "message": "Dependency cycle.",
        }
    ],
    longest_chain={"nodes": [], "length": 0, "bound": "exact"},
    roots=["__proto__", "a::b", "a"],
)

# The chain step `a>b → c` and the off-chain dependency `a → b>c` (round-3
# correctness f-007): two different ordered pairs that a `from + ">" + to` key
# cannot tell apart. Task ids are arbitrary non-empty strings, so `>` is a
# character in one as readily as a separator between two.
AMBIGUOUS_STEP_PAYLOAD: dict = _payload(
    [
        _node("a>b"),
        _node("c", layer=1),
        _node("d", layer=2),
        _node("a"),
        _node("b>c", layer=1),
    ],
    [_edge("a>b", "c"), _edge("c", "d"), _edge("a", "b>c")],
    longest_chain={"nodes": ["a>b", "c", "d"], "length": 3, "bound": "exact"},
    roots=["a>b", "a"],
)

#: This harness's own address. Deliberately not the panel harness's
#: ``GRAPH_HREF`` above: that one carries overlays and an isolated toggle
#: already applied, and the canvas tests below start from the page's defaults.
GRAPH_CANVAS_HREF = "http://lens.test/tasks/graph?project=loom"
EPIC_CANVAS_HREF = "http://lens.test/tasks/graph?epic=loom-epic"


def _graph_run(
    actions: list[str],
    href: str = GRAPH_CANVAS_HREF,
    payload: dict | None = None,
    *,
    reduced_motion: bool = False,
    served_panel: str = "",
    panel_fetch: str = "ok",
    ready_state: str = "interactive",
    lifecycle: str = "DOMContentLoaded",
) -> dict:
    """Load tasks.js then graph.js against one embedded payload, run ``actions``.

    The order matters and is the page's own: ``graph.js`` opens the side panel
    through the API ``tasks.js`` publishes, so a harness that loaded only the
    second would be testing a graph page that cannot open a panel at all.

    ``served_panel`` is the id the SERVER already rendered into the panel host
    (D9's no-JS baseline); ``reduced_motion`` is the operator's motion
    preference, which the claimed-node pulse has to obey; ``panel_fetch`` is how
    the server answers a panel request — ``ok``, ``fail`` (non-OK), ``reject``
    (no connection) or ``body`` (the body never finishes reading), which are the
    three ways one can fail to arrive; ``ready_state`` is the document's state
    when the scripts run — ``interactive`` is the page's own, which is what a
    DEFERRED script sees, and the harness fires `DOMContentLoaded` itself once
    both files have loaded; ``lifecycle`` is which events it fires there (the
    page's own sequence is ``DOMContentLoaded,load``, and a file that arrived
    after the first of those sees only ``load``).
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
            str(CYTOSCAPE_JS),
            href,
            json.dumps(actions),
            json.dumps(payload or GRAPH_PAYLOAD),
            "1" if reduced_motion else "0",
            served_panel,
            panel_fetch,
            ready_state,
            lifecycle,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    # The LAST line: the library writes its own warnings to stdout, and a
    # harness that could not survive one would fail on a future version's.
    return json.loads(result.stdout.strip().splitlines()[-1])


def _rank(result: dict, task_id: str) -> float:
    return result["ranks"][task_id]


# ── What is drawn, and in what style ────────────────────────────────────


def test_the_default_canvas_draws_dependency_edges_with_an_arrowhead_on_each() -> None:
    """D8's default view: `blocks` and `waits_on_gate`, arrowheads throughout.

    The overlays are in the payload from the first render (D6 resolves them so
    a toggle needs no fetch) and must nevertheless be OFF, along with the
    context ghost that only a provenance edge anchors — otherwise the default
    picture is the hairball the overlay decision exists to prevent.
    """
    result = _graph_run([])
    final = result["final"]

    assert sorted(set(final["edges"])) == ["blocks", "waits_on_gate"]
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


def test_shape_is_type_and_colour_is_status_as_the_library_resolves_them() -> None:
    """D8's vocabulary, read back off the real Cytoscape rather than off the
    stylesheet this file could have mistyped: shape = type, colour = status,
    a node something in this graph blocks is tinted, and a ghost is dimmed."""
    styles = _graph_run([])["styles"]

    assert styles["schema"]["shape"] == "ellipse"
    assert styles["epic"]["shape"] == "round-rectangle"
    assert styles["gate"]["shape"] == "diamond"

    # One colour per status, all four distinct — a palette that collapsed two
    # of them would make the picture say less than the text.
    fills = {
        name: styles[name]["background"]
        for name in ("schema", "done", "stopped", "unread")
    }
    assert len(set(fills.values())) == 4, fills
    # And the unread ghost's status is visibly PROVISIONAL, not just another
    # colour: Lens asked for it and did not get it.
    assert styles["unread"]["borderStyle"] == "dashed"

    # Blocked is a TINT over the status, not a replacement for it: the fill
    # still says "open" and the tint sits on top of it. (Readiness itself stays
    # Lithos's — this only says that something in THIS graph points at it.)
    assert "blocked" in styles["ship"]["classes"]
    assert "blocked" not in styles["schema"]["classes"]
    assert styles["ship"]["background"] == styles["schema"]["background"]
    assert styles["ship"]["blacken"] != styles["schema"]["blacken"]

    # A ghost is dimmed, one hop outside the scope.
    assert styles["far"]["opacity"] < 1
    assert styles["schema"]["opacity"] == 1


def test_each_edge_type_and_state_draws_the_way_the_legend_says() -> None:
    """`blocks` solid, `waits_on_gate` dashed, hierarchy thin and light,
    provenance dotted (D8); inactive faded, unknown in the unknown style (D6).
    Every one of them keeps its arrowhead."""
    result = _graph_run([])
    blocks = _edge_style(result, "schema", "ship", "blocks")
    gate = _edge_style(result, "gate", "announce", "waits_on_gate")
    hierarchy = _edge_style(result, "epic", "schema", "parent_child")
    provenance = _edge_style(result, "source", "note", "discovered_from")
    inactive = _edge_style(result, "done", "ship", "blocks")
    unknown = _edge_style(result, "unread", "announce", "blocks")

    assert blocks["lineStyle"] == "solid"
    assert gate["lineStyle"] == "dashed"
    assert provenance["lineStyle"] == "dotted"
    # Hierarchy is the LIGHT one — the default view is dependency flow, and an
    # overlay that competed with it would be the hairball D8 rules out.
    assert hierarchy["lineColor"] != blocks["lineColor"]
    assert inactive["opacity"] < 1, "a satisfied edge is drawn faded"
    assert unknown["lineStyle"] == "dashed"
    assert unknown["lineColor"] != blocks["lineColor"]
    for edge in (blocks, gate, hierarchy, provenance, inactive, unknown):
        assert edge["arrow"] == "triangle"


def test_a_cycle_is_drawn_as_a_compound_parent_holding_its_own_members() -> None:
    """T1's cycle convention (D4/D8). The box has to CONTAIN the members —
    a parent node beside them would draw a cycle around nothing."""
    styles = _graph_run([])["styles"]

    assert styles["cycle-a"]["parent"] == "cycle::cycle-a"
    assert styles["cycle-b"]["parent"] == "cycle::cycle-a"
    assert styles["ship"]["parent"] == "", "a task outside the cycle is in its box"
    assert "graph-cycle" in styles["cycle::cycle-a"]["classes"]
    # Lithos's verdict marks the member rows themselves, whatever the box says.
    assert "flagged" in styles["cycle-a"]["classes"]


def test_a_claimed_task_breathes_and_stops_when_the_operator_asks_for_stillness() -> (
    None
):
    """ "In progress" is a claim, and a still frame cannot say it — so the node
    pulses. The operator's motion preference overrides that, which is the half
    a test asserting only the class would never see."""
    moving = _graph_run([], reduced_motion=False)
    still = _graph_run([], reduced_motion=True)

    assert moving["animating"] == ["schema"]
    assert still["animating"] == []
    # The resting ring is style, not animation, so it is there either way.
    assert still["styles"]["schema"]["overlayOpacity"] > 0
    assert "claimed" in still["styles"]["schema"]["classes"]


# ── The layout: the server's ranks, the library's order ─────────────────


def test_the_layout_is_one_directed_breadthfirst_from_the_servers_roots() -> None:
    """D8's layout contract, read off the options the library was handed.

    The overlay edges must NOT be among them: `breadthfirst` reads every edge
    it is given, so an epic's hierarchy would flatten the dependency chain the
    page is for into one row of its children.
    """
    result = _graph_run(["overlay:hierarchy", "event:ship"])

    assert len(result["layouts"]) == 1, "the graph laid itself out more than once"
    layout = result["layouts"][0]
    assert layout["name"] == "breadthfirst"
    assert layout["directed"] is True
    assert layout["animate"] is False
    assert layout["roots"] == sorted(GRAPH_PAYLOAD["roots"])
    assert "parent_child" not in layout["edgeTypes"]
    assert "discovered_from" not in layout["edgeTypes"]
    assert "blocks" in layout["edgeTypes"]


def test_a_node_is_ranked_by_the_servers_layer_not_by_the_shortest_path() -> None:
    """Regression (round-1 correctness f-001). The server layers by LONGEST
    path — `A → B → C → D` plus `A → D` puts D in layer 3 — while Cytoscape's
    breadth-first ranks by shortest path and would draw D level with B. A
    picture contradicting the layers printed under it is the one thing D3 does
    not allow."""
    result = _graph_run([], payload=DIAMOND_PAYLOAD)

    assert _rank(result, "b") > _rank(result, "a")
    assert _rank(result, "c") > _rank(result, "b")
    assert _rank(result, "d") > _rank(result, "c"), (
        "D was drawn level with B — the shortest-path rank, not the server's"
    )
    # Evenly, and in the server's own order: four layers, four ranks.
    assert len({_rank(result, node) for node in ("a", "b", "c", "d")}) == 4


def test_every_drawn_node_sits_in_the_rank_its_payload_layer_names() -> None:
    """The same contract over a scope with a cycle in it, which is where the
    library's own `maximal` option gives up and falls back to shortest path."""
    result = _graph_run([])

    by_layer: dict[int, set[float]] = {}
    for node in GRAPH_PAYLOAD["nodes"]:
        by_layer.setdefault(int(node["layer"]), set()).add(_rank(result, node["id"]))
    # A cycle's members stack inside one slot, so a layer holding one spans a
    # small band rather than a single line — but the bands stay ordered and
    # disjoint, which is what "the picture agrees with the layers" means.
    ordered = sorted(by_layer)
    for lower, upper in zip(ordered, ordered[1:], strict=False):
        assert max(by_layer[lower]) < min(by_layer[upper]), (
            f"layer {lower} is drawn below layer {upper}"
        )


# ── The longest chain (D7) ──────────────────────────────────────────────


def test_the_longest_chain_is_traced_across_its_whole_length() -> None:
    result = _graph_run([])
    styles = result["styles"]

    assert "chain" in _edge_style(result, "schema", "ship", "blocks")["classes"]
    assert "chain" in _edge_style(result, "ship", "far", "blocks")["classes"]
    assert "chain" in styles["schema"]["classes"]
    # And nothing off it is traced.
    assert "chain" not in _edge_style(result, "cycle-a", "cycle-b", "blocks")["classes"]
    assert "chain" not in styles["stranded"]["classes"]


def test_the_chain_trace_survives_a_cycle_boundary() -> None:
    """Regression (round-1 correctness f-003). The chain is a walk over the
    CONDENSED graph, so it names a cycle by its representative — the edge that
    actually enters the cycle can land on any member. Matching raw endpoints
    left the trace broken at exactly that step."""
    result = _graph_run([], payload=CYCLE_CHAIN_PAYLOAD)
    styles = result["styles"]

    assert "chain" in _edge_style(result, "p", "cyc-b", "blocks")["classes"], (
        "the edge entering the cycle was not traced"
    )
    assert "chain" in _edge_style(result, "cyc-a", "d", "blocks")["classes"]
    # Both members are on the chain, because the condensation is …
    assert "chain" in styles["cyc-a"]["classes"]
    assert "chain" in styles["cyc-b"]["classes"]
    # … and so is the box, or the trace would read as broken where it is drawn.
    assert "chain" in styles["cycle::cyc-a"]["classes"]
    # But the loop's own edges are not steps of it: the chain crosses the
    # condensation in ONE move and endorses neither direction round it.
    assert "chain" not in _edge_style(result, "cyc-a", "cyc-b", "blocks")["classes"]
    assert "chain" not in _edge_style(result, "cyc-b", "cyc-a", "blocks")["classes"]


def test_the_chain_is_traced_over_the_active_projections_own_condensation() -> None:
    """Regression (round-9 correctness f-001). A node's `cycle` is the all-edge
    SCC the PICTURE is drawn from; the chain condenses the ACTIVE projection,
    and the two partitions legitimately differ. Reading the chain through
    `cycle` called the chain's own step internal to a condensation and accented
    a completed task the chain never names."""
    result = _graph_run([], payload=MIXED_ACTIVE_CYCLE_PAYLOAD)
    styles = result["styles"]

    assert "chain" in _edge_style(result, "mix-a", "mix-b", "blocks")["classes"], (
        "the chain's own step was discarded as internal to the drawn cycle"
    )
    assert "chain" in styles["mix-a"]["classes"]
    assert "chain" in styles["mix-b"]["classes"]
    # The completed task is in the box, not on the chain — the text names two
    # nodes and the canvas may not name three.
    assert "chain" not in styles["mix-c"]["classes"]
    # The inactive edges are no part of it either, in either direction.
    assert "chain" not in _edge_style(result, "mix-b", "mix-c", "blocks")["classes"]
    assert "chain" not in _edge_style(result, "mix-c", "mix-a", "blocks")["classes"]
    # And the box the chain runs THROUGH is accented, as it is when the whole
    # box is one chain node: the trace may not read as broken where it is drawn.
    assert "chain" in styles["cycle::mix-a"]["classes"]


# ── Overlays (D8) ───────────────────────────────────────────────────────


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
    # The toolbar control is a real LINK (the no-JS page is the baseline), so
    # "no request" is only half the claim: a handler that skipped
    # `preventDefault()` would fetch nothing and still reload the whole page
    # through the browser's own default action.
    assert result["navigations"] == []
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
    assert result["navigations"] == []


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
    assert result["navigations"] == []


# ── Isolated tasks (D8) ─────────────────────────────────────────────────


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
    assert result["navigations"] == []


def test_an_epic_scope_shows_its_isolated_children_and_hides_them_on_request() -> None:
    """The other scope default, and the other direction of the toggle (D8):
    an epic graph is about an initiative's PROGRESS, and its edge-less children
    are half of that. Back then restores what the URL said."""
    loaded = _graph_run([], href=EPIC_CANVAS_HREF, payload=EPIC_PAYLOAD)["final"]
    result = _graph_run(
        ["isolated", "back"], href=EPIC_CANVAS_HREF, payload=EPIC_PAYLOAD
    )
    hidden, restored = result["states"][0], result["states"][1]

    # Shown from the first paint, with no `isolated=` in the URL at all.
    assert "child" in loaded["nodes"]
    assert loaded["disclosureOpen"] is True
    # The toggle goes the other way here, and takes the disclosure with it.
    assert result["pushed"] == ["/tasks/graph?epic=loom-epic&isolated=0"]
    assert "child" not in hidden["nodes"]
    assert hidden["disclosureOpen"] is False
    # And Back re-applies the epic's own default rather than the project's.
    assert "child" in restored["nodes"]
    assert restored["disclosureOpen"] is True
    assert result["navigations"] == []


def test_folding_every_isolate_away_takes_the_partial_view_notice_with_it() -> None:
    """Regression (round-8 correctness f-011). A scope that is nothing but
    isolates empties the canvas when they are folded away — and both halves of
    the partial-view report returned early on an empty collection, so the box
    kept `data-canvas-clipped="true"` and went on telling the operator to drag
    a graph that was no longer drawn at all."""
    result = _graph_run(
        ["isolated", "isolated", "isolated"], payload=ISOLATED_ONLY_PAYLOAD
    )
    shown, emptied, restored = result["states"]

    # The isolates are the whole picture, and it overflows the canvas — which
    # this harness's headless one always is, at 1×1: the partial view is the
    # state under test here, not the width that produced it (the real-browser
    # half of that is `e2e/`'s narrow-canvas tests).
    assert shown["nodes"] == ["alone", "apart", "aside"]
    assert shown["clipped"] == "true"
    assert shown["panHintHidden"] is False
    # … and folding them away leaves nothing to be out of view.
    assert emptied["nodes"] == []
    assert emptied["clipped"] == "false"
    assert emptied["panHintHidden"] is True
    # Restored with them, because the notice describes the CURRENT picture.
    assert restored["nodes"] == ["alone", "apart", "aside"]
    assert restored["clipped"] == "true"
    assert restored["panHintHidden"] is False
    # All of it client-side, from the payload the page already had.
    assert result["fetches"] == []
    assert result["navigations"] == []


# ── Selection, events, text ─────────────────────────────────────────────


def test_clicking_a_node_opens_that_task_s_panel_and_pushes_focus() -> None:
    """D9's promise, through D8's parameter: the node has no DOM row, so the
    tap travels through the API `tasks.js` publishes — one panel
    implementation, and `focus` is the graph page's only selection key."""
    result = _graph_run(["tap:ship"])

    assert result["fetches"] == ["/tasks/id?task_id=ship&fragment=panel"]
    assert result["final"]["panel"] == "panel:/tasks/id?task_id=ship&fragment=panel"
    assert result["pushed"] == ["/tasks/graph?project=loom&focus=ship"]
    assert result["final"]["focused"] == ["ship"]


def test_closing_the_panel_clears_the_focus_ring_with_the_url() -> None:
    """Regression (round-1 correctness f-002). Closing the panel clears `focus`
    with `pushState`, and `pushState` fires no `popstate` — so the canvas had
    no way to notice, and kept the node lit under a URL that no longer named
    it. The panel announces its transitions instead."""
    opened = _graph_run(["tap:ship"])["final"]
    closed = _graph_run(["tap:ship", "close"])["final"]
    escaped = _graph_run(["tap:ship", "escape"])["final"]

    assert opened["focused"] == ["ship"]
    assert closed["focused"] == [], "the node stayed lit after the panel closed"
    assert "focus=" not in closed["href"]
    assert closed["panel"] == ""
    # Escape is the same transition by another control, and must not differ.
    assert escaped["focused"] == []
    assert "focus=" not in escaped["href"]


def test_a_task_event_for_a_node_raises_the_pill_and_never_re_layouts() -> None:
    """D8: the graph stays still while it is read. The pill is the whole
    response to an event — a re-layout under the operator's cursor is the
    behaviour this replaces."""
    result = _graph_run(["event:ship"])

    assert result["final"]["pillHidden"] is False
    assert len(result["layouts"]) == 1, "the page laid itself out again"
    # And no reconcile: the graph page tells tasks.js not to re-render, or one
    # event would cost a whole graph assembly.
    assert result["fetches"] == []


def test_an_event_for_a_task_that_is_not_on_this_page_raises_nothing() -> None:
    """The pill says "this picture is out of date", so it may only fire when
    the event touches a node actually drawn from this payload."""
    result = _graph_run(["event:somewhere-else"])

    assert result["final"]["pillHidden"] is True


def test_the_server_built_links_follow_the_state_the_client_applied() -> None:
    """Regression (round-1 correctness f-004). The resolved toggle and the
    refresh pill are rendered by the server and never re-applied client-side,
    so they kept the URL the page LOADED on: the pill's own "this is a refresh"
    contract was false the moment an overlay or a focus had been set, and
    following either silently dropped them."""
    result = _graph_run(["overlay:hierarchy", "tap:ship", "event:ship"])
    hrefs = result["final"]["hrefs"]

    assert "overlays=hierarchy" in hrefs["pill"]
    assert "focus=ship" in hrefs["pill"]
    assert "overlays=hierarchy" in hrefs["resolved"]
    assert "focus=ship" in hrefs["resolved"]
    # The resolved link is still a NAVIGATION — resolved tasks are nodes the
    # server did not send — so it flips its own parameter and nothing else.
    assert "include_resolved=1" in hrefs["resolved"]


def test_show_as_text_collapses_the_baseline_and_leaves_it_in_the_dom() -> None:
    """D3: the canvas hides the text layers behind a toggle and never removes
    them — the text is the page for a screen reader and a PR screenshot."""
    result = _graph_run(["text", "text"])

    assert result["states"][0]["textHidden"] is False, "the toggle did not reveal it"
    assert result["states"][1]["textHidden"] is True, "the toggle does not close again"
    # Hidden, never detached: the section object is still the one the page
    # rendered, which is what "stays in the DOM" means.
    assert result["final"]["textHidden"] is True


def test_a_server_rendered_focus_panel_is_not_fetched_again() -> None:
    """D9's no-JS baseline is the SERVER's panel, and the client is enhancement
    over it: a page that arrives with the panel already up must not throw it
    away and re-request the same fragment."""
    result = _graph_run(
        [],
        href="http://lens.test/tasks/graph?project=loom&focus=ship",
        served_panel="ship",
    )

    assert result["fetches"] == []
    assert result["final"]["panel"] == "panel:server:ship"
    assert result["final"]["focused"] == ["ship"]


def test_a_focus_the_server_did_not_answer_is_opened_without_a_second_entry() -> None:
    """The remaining case: `focus=` in the URL with an empty host (the panel
    read failed). The address bar already names the selection, so re-pushing it
    would leave a twin entry the first Back appears to ignore."""
    result = _graph_run([], href="http://lens.test/tasks/graph?project=loom&focus=ship")

    assert result["fetches"] == ["/tasks/id?task_id=ship&fragment=panel"]
    assert result["pushed"] == []
    assert result["final"]["focused"] == ["ship"]


def test_a_double_click_uses_the_url_the_server_built_for_that_id() -> None:
    """The id `graph` collides with a page under `/tasks/`, so it can only be
    addressed through the query alias — `tasks.task_detail_path` owns that rule
    and the browser never restates it. A client that assembled `/tasks/<id>`
    would send the operator to the graph PAGE instead of the task."""
    result = _graph_run(["dbltap:graph"])

    assert result["final"]["href"] == "/tasks/id?task_id=graph"


def test_a_double_click_on_an_ordinary_node_still_opens_its_own_page() -> None:
    result = _graph_run(["dbltap:ship"])

    assert result["final"]["href"] == "/tasks/ship"
    # And it cost no panel on the way out: the tap that opened the gesture lit
    # the node and nothing more, so there is no fragment to throw away and no
    # `focus=` entry behind the page the operator actually asked for.
    assert result["fetches"] == []
    assert result["pushed"] == []


def test_a_click_inside_another_node_s_multi_click_window_opens_its_panel() -> None:
    """Regression (round-2 correctness f-002). Cytoscape's multi-click detector
    measures TIME and nothing else, so clicking one node and then another
    within 250ms is a `dbltap` on the second — and taken at face value it
    navigated away from a node the operator had clicked exactly once.

    Worse, the same debounce swallowed that node's `onetap`, so the panel the
    single click should have opened never came either: the page left for a
    detail page nobody asked for, from a click that meant "show me this".
    """
    crossed = _graph_run(["dbltap:schema~ship"])
    background = _graph_run(["dbltap:~ship"])

    for result, gesture in ((crossed, "another node"), (background, "the canvas")):
        final = result["final"]
        assert "/tasks/ship" not in final["href"], f"navigated after {gesture}"
        # The second click was a single click on `ship`, and is answered as one.
        assert result["fetches"] == ["/tasks/id?task_id=ship&fragment=panel"]
        assert final["panel"] == "panel:/tasks/id?task_id=ship&fragment=panel"
        assert "focus=ship" in final["href"]
        assert final["focused"] == ["ship"]


def test_the_first_click_of_a_double_click_opens_no_panel() -> None:
    """Regression (round-2 correctness f-001). The panel used to open on the
    raw `tap`, which is the first half of a double-click too: the fragment
    landed beside the canvas mid-gesture, the flex layout narrowed the canvas,
    the refit moved the node — and the second click hit the background, so the
    `dbltap` that should have left for `/tasks/{id}` was never emitted.

    So a tap the library has not yet settled may do nothing but light the node.
    `onetap` — the tap Cytoscape held for its 250ms multi-click window without
    a second one arriving — is what opens the panel."""
    pending = _graph_run(["firsttap:ship"])
    settled = _graph_run(["tap:ship"])

    # Lit immediately, because a click has to answer at once …
    assert pending["final"]["focused"] == ["ship"]
    # … and that is ALL it may do while the gesture could still be a double.
    assert pending["fetches"] == []
    assert pending["final"]["panel"] == ""
    assert pending["pushed"] == []
    # The same tap, once the window closes with no second click: the panel.
    assert settled["fetches"] == ["/tasks/id?task_id=ship&fragment=panel"]
    assert "focus=ship" in settled["final"]["href"]


def test_an_older_panel_request_cannot_land_inside_a_double_click() -> None:
    """Regression (round-2 correctness f-001, the second way in). The `onetap`
    debounce stops THIS gesture's own click from opening a panel mid-gesture,
    but not an open already in flight from an earlier one: a single click on
    one node settles and asks for its fragment, the operator then starts a
    double-click on another, and the older answer lands between the two
    clicks — inserting the panel beside the canvas, narrowing it, refitting
    it, and moving the node out from under a pointer that has not moved. The
    second click hits the background, no `dbltap` is emitted for the node, and
    the navigation the operator asked for never happens.

    So a tap that OPENS a gesture supersedes any panel open that has not yet
    painted."""
    result = _graph_run(
        ["tap:schema", "firsttap:ship", "release", "secondtap:ship"],
        panel_fetch="hold",
    )

    # The settled single click really did ask for `schema`'s panel …
    assert result["fetches"] == ["/tasks/id?task_id=schema&fragment=panel"]
    # … and the answer, landing after the double-click had begun, is dropped.
    assert result["states"][2]["panel"] == "", "a stale panel opened mid-gesture"
    assert result["pushed"] == [], "a stale open moved the URL mid-gesture"
    # Which leaves the gesture to finish as the operator made it.
    assert result["final"]["href"] == "/tasks/ship"


def test_a_settled_panel_survives_a_gesture_that_starts_elsewhere() -> None:
    """The other half of the rule: only a PENDING open is superseded. A panel
    that has arrived is on screen and was asked for — beginning a gesture on
    another node must not clear it, or every double-click would close the
    panel the operator opened before it."""
    result = _graph_run(["tap:schema", "release", "firsttap:ship"], panel_fetch="hold")
    final = result["final"]

    assert final["panel"] == "panel:/tasks/id?task_id=schema&fragment=panel"
    assert result["pushed"] == ["/tasks/graph?project=loom&focus=schema"]
    # The new gesture still lights its own node, which is all a raw tap does.
    assert final["focused"] == ["ship"]


def test_a_startup_panel_request_is_superseded_like_any_other() -> None:
    """Regression (round-3 correctness f-001). A page loaded with `focus=` that
    the SERVER could not render leaves the client to fetch that panel itself —
    and `tasks.js` has already seeded both its selection ids from the URL, so
    that request is in flight with the two equal. A supersession test that read
    the ids would call it settled and let its response land beside the canvas
    mid-gesture, which is the whole failure again.
    """
    result = _graph_run(
        ["firsttap:schema", "release", "secondtap:schema"],
        href=GRAPH_CANVAS_HREF + "&focus=ship",
        panel_fetch="hold",
    )

    # The fallback really did ask for the panel the server did not send …
    assert result["fetches"] == ["/tasks/id?task_id=ship&fragment=panel"]
    # … and the answer, landing after a gesture had begun, is dropped: no panel
    # beside the canvas, and the ring stays on the node being clicked.
    assert result["states"][1]["panel"] == "", "a stale panel opened mid-gesture"
    assert result["states"][1]["focused"] == ["schema"]
    assert result["final"]["href"] == "/tasks/schema"


def test_superseding_a_pending_open_leaves_the_panel_already_on_screen() -> None:
    """Regression (round-3 test-quality f-002). The load-bearing combination:
    one panel is OPEN, a second open is in flight, and a gesture starts on a
    third node. Only the pending one may be dropped — clearing the host as well
    would expand and refit the canvas between the two clicks, which is the
    reflow this supersession exists to prevent, caused by the cure."""
    result = _graph_run(
        [
            "tap:schema",  # settles, and its panel arrives
            "release",
            "tap:ship",  # settles too, but its panel is still in flight …
            "firsttap:announce",  # … when a double-click starts elsewhere
            "release",
            "secondtap:announce",
        ],
        panel_fetch="hold",
    )
    states = result["states"]

    assert result["fetches"] == [
        "/tasks/id?task_id=schema&fragment=panel",
        "/tasks/id?task_id=ship&fragment=panel",
    ]
    schema_panel = "panel:/tasks/id?task_id=schema&fragment=panel"
    # On screen before the gesture, and still there after the superseded
    # response lands — the operator did not ask for it to go.
    assert states[2]["panel"] == schema_panel
    assert states[3]["panel"] == schema_panel
    assert states[4]["panel"] == schema_panel, "the open panel was torn down"
    assert "focus=schema" in states[4]["href"]
    # `ship` never reached the URL: its open was superseded before it painted.
    assert result["pushed"] == ["/tasks/graph?project=loom&focus=schema"]
    # And the gesture finishes as the operator made it.
    assert result["final"]["href"] == "/tasks/announce"


# ── Regressions from round 2 ────────────────────────────────────────────


def _edge_style(result: dict, from_id: str, to_id: str, edge_type: str) -> dict:
    """One edge's resolved style, found by what it IS rather than by its id.

    Element ids are synthesised (and percent-encoded, so two payload edges
    cannot share one), which makes them an implementation detail no test should
    have to spell.
    """
    matches = [
        style
        for style in result["styles"].values()
        if style["kind"] == "edge"
        and style["source"] == from_id
        and style["target"] == to_id
        and style["type"] == edge_type
    ]
    assert len(matches) == 1, f"{from_id} -{edge_type}-> {to_id}: {len(matches)} drawn"
    return matches[0]


def test_a_cycle_of_any_size_still_occupies_exactly_one_rank_band() -> None:
    """Regression (round-2 correctness f-001). A rank was laid out at a FIXED
    pitch while a condensation's members stack inside it, so a five-member
    cycle — nothing bounds one below the scope guard — spilled 128px past its
    own band and put one member above the rank before it and another below the
    rank after. The band then says the opposite of the layer it is drawn for.
    """
    result = _graph_run([], payload=BIG_CYCLE_PAYLOAD)
    ranks = result["ranks"]

    members = [ranks[member] for member in BIG_CYCLE_MEMBERS]
    assert ranks["p"] < min(members), "a cycle member was drawn above layer 0"
    assert max(members) < ranks["d"], "a cycle member was drawn below layer 2"
    # And they are still one stack, in one place across the rank: a condensation
    # occupies ONE slot (D4), which is what the compound box is drawn around.
    columns = {
        round(position[0])
        for task_id, position in result["positions"].items()
        if task_id in BIG_CYCLE_MEMBERS
    }
    assert len(columns) == 1, columns


def test_an_overlay_edge_beside_a_chain_step_is_not_traced_as_one() -> None:
    """Regression (round-2 correctness f-006). The chain is the longest
    BLOCKING chain over the active projection (D7), so only an active
    dependency edge can be a step of it. Matching condensations alone gave the
    critical-path accent to a `discovered_from` edge running between the same
    two tasks — a provenance link drawn as if it blocked something."""
    result = _graph_run(
        ["overlay:hierarchy", "overlay:provenance"], payload=PARALLEL_OVERLAY_PAYLOAD
    )

    assert "chain" in _edge_style(result, "up", "down", "blocks")["classes"]
    assert (
        "chain" not in _edge_style(result, "up", "down", "discovered_from")["classes"]
    )
    assert "chain" not in _edge_style(result, "up", "down", "parent_child")["classes"]


def test_ids_the_contract_allows_do_not_break_the_canvas() -> None:
    """Regression (round-2 correctness f-007). A task id is an arbitrary
    non-empty string (`tasks.py`), and this page used them as keys in
    prototype-bearing objects and spliced them unescaped into synthetic element
    ids. A task called `__proto__` aborted the whole enhancement; `a::b → c` and
    `a → b::c` collapsed onto one id, so Cytoscape silently dropped an edge and
    its arrowhead; and a task named `cycle::<representative>` collided with the
    box synthesised for that cycle."""
    result = _graph_run([], payload=HOSTILE_IDS_PAYLOAD)
    final = result["final"]

    # It drew at all, which is the first half of the finding.
    assert final["nodes"], "the canvas gave up on a payload the contract allows"
    for task_id in ("__proto__", "constructor", "toString", "a::b", "a", "b::c"):
        assert task_id in final["nodes"], task_id
    # Both colliding edges survive, each with its arrowhead.
    assert _edge_style(result, "a::b", "c", "blocks")["arrow"] == "triangle"
    assert _edge_style(result, "a", "b::c", "blocks")["arrow"] == "triangle"
    assert final["arrowless"] == []
    # The cycle's box is still the cycle's, and the task that shares its
    # spelling is a node of its own rather than the box.
    styles = result["styles"]
    assert styles["__proto__"]["parent"] == styles["constructor"]["parent"] != ""
    assert styles["cycle::__proto__"]["parent"] == ""
    assert "graph-cycle" not in styles["cycle::__proto__"]["classes"]


def test_a_node_open_that_never_arrives_leaves_no_ring_and_no_focus() -> None:
    """The other half of the panel contract (round-2 test-quality f-006). A tap
    lights the node before the fetch, because the ring is the operator's own
    click answered immediately — so an open that fails has to walk it back, or
    the canvas claims a selection the URL never took and the panel never
    showed."""
    for outcome in ("fail", "reject", "body"):
        result = _graph_run(["tap:ship"], panel_fetch=outcome)
        final = result["final"]

        assert result["fetches"] == ["/tasks/id?task_id=ship&fragment=panel"], outcome
        assert final["focused"] == [], f"{outcome}: the node stayed lit"
        assert "focus=" not in final["href"], outcome
        assert result["pushed"] == [], outcome
        assert final["panel"] == "", outcome


# ── The styles those classes are for ────────────────────────────────────


def test_an_overlay_edge_is_drawn_lighter_than_the_dependency_flow() -> None:
    """D8 puts hierarchy behind a toggle because the default view is dependency
    flow; an overlay switched on must not compete with it. "Thin and light" is
    a width and a colour, and a rule that lost either would still carry the
    class this used to assert on."""
    result = _graph_run(["overlay:hierarchy", "overlay:provenance"])
    blocks = _edge_style(result, "schema", "ship", "blocks")
    hierarchy = _edge_style(result, "epic", "schema", "parent_child")
    provenance = _edge_style(result, "source", "note", "discovered_from")

    assert hierarchy["width"] < blocks["width"]
    assert hierarchy["lineColor"] != blocks["lineColor"]
    assert provenance["lineStyle"] == "dotted"
    # Still drawn, and still directed.
    assert hierarchy["display"] == "element"
    assert hierarchy["arrow"] == "triangle"


def test_the_cycle_box_carries_t1s_bracketed_styling() -> None:
    """The compound parent is the cycle CONVENTION, not just a container: T1
    draws a cycle bracketed, and the legend's own line explains that box. A
    parent with its styling deleted would still hold its members."""
    styles = _graph_run([])["styles"]
    box = styles["cycle::cycle-a"]
    ordinary = styles["ship"]

    assert box["borderStyle"] == "dashed"
    assert box["borderStyle"] != ordinary["borderStyle"]
    assert box["borderWidth"] > ordinary["borderWidth"]
    # Tinted rather than filled, so the members inside it stay readable.
    assert 0 < box["backgroundOpacity"] < 0.5


def test_the_traced_chain_is_visibly_distinct_from_everything_off_it() -> None:
    """D7's trace is a PICTURE, and a class with no style behind it traces
    nothing. The chain has to read differently from the edges beside it."""
    result = _graph_run([])
    on_chain = _edge_style(result, "schema", "ship", "blocks")
    off_chain = _edge_style(result, "cycle-a", "cycle-b", "blocks")
    styles = result["styles"]

    assert on_chain["width"] > off_chain["width"]
    assert on_chain["lineColor"] != off_chain["lineColor"]
    # The arrowhead is traced with the line: an accent edge ending in the
    # default grey head reads as the trace stopping one step short.
    assert on_chain["arrowColor"] == on_chain["lineColor"]
    assert off_chain["arrowColor"] == off_chain["lineColor"]
    # And the nodes on it are marked too, not only the edges between them.
    assert styles["schema"]["borderColor"] != styles["stranded"]["borderColor"]


def test_a_chain_step_is_an_ordered_pair_not_a_joined_string() -> None:
    """Regression (round-3 correctness f-007). The chain's steps were keyed by
    `from + ">" + to`, and a task id may contain `>` as readily as anything
    else — so the real step `a>b → c` and the unrelated dependency `a → b>c`
    produced the same key, and the second took the critical-path accent for a
    chain it is not on."""
    result = _graph_run([], payload=AMBIGUOUS_STEP_PAYLOAD)

    assert "chain" in _edge_style(result, "a>b", "c", "blocks")["classes"]
    assert "chain" in _edge_style(result, "c", "d", "blocks")["classes"]
    assert "chain" not in _edge_style(result, "a", "b>c", "blocks")["classes"], (
        "an off-chain edge was traced as part of the longest blocking chain"
    )


def test_the_event_stream_waits_for_every_deferred_script() -> None:
    """Regression (round-4 correctness f-008). This page's scripts load in
    order — `tasks.js`, a ~400KB Cytoscape bundle, `graph.js` — and `graph.js`
    is the one that subscribes for the pill. A stream opened at the end of
    `tasks.js` consumes a matching `task.updated` while the browser is still
    fetching the library, records its id in the dedup set, and has nothing to
    replay it to: the pill never appears for an event that really did land, and
    this page has no reconcile to cover for it.

    Deferred scripts all run before `DOMContentLoaded`, so waiting for that is
    exactly the guarantee "after every subscriber has registered" needs.
    """
    result = _graph_run(["event:ship"])

    assert result["streamBeforeGraph"] is False, (
        "the stream was open before the canvas could subscribe to it"
    )
    assert result["streamOpen"] is True, "the stream never opened at all"
    assert result["final"]["pillHidden"] is False


def test_a_script_that_arrives_after_the_document_connects_at_once() -> None:
    """The other branch, and the reason it is a branch: for a file injected
    once the document is parsed there is no later script to wait for, and a
    `DOMContentLoaded` that has already fired would never come again."""
    result = _graph_run(["event:ship"], ready_state="complete", lifecycle="")

    assert result["streamBeforeGraph"] is True
    assert result["eventSources"] == 1
    assert result["final"]["pillHidden"] is False


def test_the_page_s_own_lifecycle_opens_exactly_one_stream() -> None:
    """A normal page fires `DOMContentLoaded` AND `load`, and both start the
    stream — so without the idempotence guard the second call closes the first
    EventSource and opens a second, which is a dropped subscription and a second
    connection against the hub's per-process ceiling. "A stream exists"
    cannot tell one from two; the count can."""
    result = _graph_run(["event:ship"], lifecycle="DOMContentLoaded,load")

    assert result["eventSources"] == 1, "the page opened a second stream"
    assert result["final"]["pillHidden"] is False


def test_a_file_that_missed_dom_content_loaded_still_gets_its_stream() -> None:
    """The third entry, and the only one `DOMContentLoaded` cannot serve: a
    script injected between the two events runs at `"interactive"` — so it is
    not `"complete"` and connects on an event that has already fired and will
    not fire again. `load` is the backstop, and it has to open exactly one."""
    result = _graph_run(["event:ship"], lifecycle="load")

    assert result["eventSources"] == 1, "the stream never opened"
    assert result["streamOpen"] is True
    assert result["final"]["pillHidden"] is False
