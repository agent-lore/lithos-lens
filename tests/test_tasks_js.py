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

const [sourcePath, nowRaw, readyAt, pollMs] = process.argv.slice(1);
let currentNow = Number(nowRaw);
const timers = [];
const fetches = [];

const board = { dataset: { gatesNextReadyAt: readyAt } };
const document = {
  querySelector(selector) {
    if (selector === "[data-gates-next-ready-at]") return readyAt ? board : null;
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

console.log(JSON.stringify({
  delays: timers.map((timer) => timer.delay),
  fetches: fetches.length,
}));
"""

MAX_TIMER_DELAY_MS = 2147483647
GRACE_MS = 500
POLL_MS = 30000
NOW_MS = 1_800_000_000_000


def _run(ready_at: str) -> dict:
    """Load tasks.js against a board carrying ``ready_at``; fire the last timer."""
    assert NODE is not None
    result = subprocess.run(
        [NODE, "-e", HARNESS, "--", str(TASKS_JS), str(NOW_MS), ready_at, str(POLL_MS)],
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

    assert result["delays"] == [two_hours + GRACE_MS]
    # Firing it refreshes the board (one fetch), rather than re-arming.
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

    assert result["delays"] == [POLL_MS]


def test_no_timer_gate_arms_no_refresh() -> None:
    result = _run("")

    assert result["delays"] == []
    assert result["fetches"] == 0
