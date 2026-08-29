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
};

class EventSource {
  addEventListener() {}
  close() {}
}

const sandbox = {
  document,
  EventSource,
  console,
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
