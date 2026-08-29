(function () {
  const config = window.LithosLensTasks || {};
  const eventsUrl = config.eventsUrl || "/tasks/events";
  const autoRefreshIntervalMs = config.autoRefreshIntervalMs || 30000;
  const seenEvents = new Set();
  let eventSource = null;
  let reconcileTimer = null;
  let pollTimer = null;
  let reconnectRefreshPending = false;
  let refreshInFlight = false;
  let refreshQueued = false;
  let gateRefreshTimer = null;
  let gateCountdownTimer = null;
  let lastGateRefreshAt = 0;
  // setTimeout stores its delay in a signed 32-bit int: anything larger wraps
  // and fires (near) immediately, so a gate more than ~24.8 days out must be
  // reached by chaining sleeps rather than by one oversized timeout.
  const MAX_TIMER_DELAY_MS = 2147483647;
  // Floor on the gap between two SELF-TRIGGERED refreshes. A hard bound is
  // needed — the instant is server-written, and a stream of near-now stamps
  // must not let a tab hammer /tasks — but it is a rate bound, not the polling
  // cadence: at 30s it also delayed every legitimately consecutive gate. Worst
  // case here is one self-triggered refresh per tab per 2s, reachable only from
  // a server publishing gates that close that fast.
  const GATE_REFRESH_MIN_SPACING_MS = 2000;
  // Refresh a beat AFTER the instant, so a slightly fast browser clock does
  // not ask Lithos before the gate has lapsed there.
  const GATE_REFRESH_GRACE_MS = 500;
  let currentLiveState = "paused";
  let currentLiveDetail = "Reconnecting; polling fallback is active";

  function setLiveStatus(status, detail) {
    const root = document.querySelector("[data-live-status]");
    const label = document.querySelector("[data-live-status-label]");
    const description = document.querySelector("[data-live-status-detail]");
    if (!root || !label || !description) return;
    currentLiveState = status;
    currentLiveDetail = detail;
    root.dataset.liveState = status;
    label.textContent = status === "live" ? "Live updates connected" : "Live updates paused";
    description.textContent = detail;
  }

  function scheduleReconcile(delay) {
    window.clearTimeout(reconcileTimer);
    reconcileTimer = window.setTimeout(refreshFragments, delay || 800);
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = window.setInterval(refreshFragments, autoRefreshIntervalMs);
  }

  function stopPolling() {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }

  // ONE render in flight per tab, ever.
  //
  // This used to be a `latestRefreshToken` guard, which discarded a stale
  // RESULT — after the server had already rendered it. That is not the same
  // property. Every per-request bound on the server is per-INVOCATION, so two
  // overlapping reconciles each get their own full allowance: on the task
  // detail page a 25-slot fan-out becomes 25 x overlapping renders, capped in
  // practice only by the browser's ~6 connections per origin. LithosClient
  // holds ONE MCP session for the whole process, so that contention degrades
  // every surface — the dashboard, /knowledge, /health — not just the page
  // being viewed. Discarding the response afterwards saves none of it.
  //
  // An AbortController was the other option the finding offered. It is weaker
  // here: cancelling the fetch tears down the client side, but a server-side
  // render already in progress runs to completion — Starlette only notices a
  // disconnect when it writes. Not issuing the second request is what actually
  // bounds the work.
  //
  // Coalesced rather than dropped: a refresh asked for while one is running
  // sets a flag and gets exactly ONE more pass afterwards, however many
  // arrived. So the board still converges on the latest state, and a burst of
  // events costs two renders rather than N.
  async function refreshFragments() {
    if (refreshInFlight) {
      refreshQueued = true;
      return;
    }
    refreshInFlight = true;
    try {
      do {
        refreshQueued = false;
        await runRefresh();
      } while (refreshQueued);
    } finally {
      refreshInFlight = false;
      // A REJECTED render jumps straight here, past the `while (refreshQueued)`
      // check — so a reconcile asked for during a render that then failed was
      // dropped, and the board sat stale until the 30s poll. Coalescing has to
      // survive failure or it is only half a guarantee.
      //
      // Handed back to the DEBOUNCED path rather than retried inline: an
      // inline retry against a server that is failing fast would spin at
      // whatever rate events arrive, which is the shape of problem this whole
      // function exists to prevent.
      if (refreshQueued) {
        refreshQueued = false;
        scheduleReconcile();
      }
    }
  }

  async function runRefresh() {
    const response = await fetch(window.location.href, {
      headers: { "X-Lithos-Lens-Refresh": "tasks" }
    });
    if (!response.ok) return;
    const text = await response.text();
    const doc = new DOMParser().parseFromString(text, "text/html");
    replaceFragment(doc, "dashboard-data");
    if (config.detailTaskId) {
      replaceFragment(doc, "detail");
    }
    setupDatePickers();
    // The replaced fragment carries a fresh board (new gate rows, a new
    // ready_at, or none), so the countdown text and the one-shot timer are
    // re-armed against it rather than left pointing at the discarded DOM.
    renderGateCountdowns();
    scheduleGateRefresh();
    setLiveStatus(currentLiveState, currentLiveDetail);
  }

  function replaceFragment(doc, name) {
    const current = document.querySelector(`[data-refresh-fragment="${name}"]`);
    const next = doc.querySelector(`[data-refresh-fragment="${name}"]`);
    if (current && next) current.replaceWith(next);
  }

  function handleEvent(event) {
    // An EventSource MessageEvent exposes the SSE `id:` field as
    // lastEventId — `event.id` does not exist, and reading it dropped every
    // event at this dedup guard (caught by the task.created browser test).
    const eventId = event.lastEventId;
    if (!eventId || seenEvents.has(eventId)) return;
    seenEvents.add(eventId);
    if (seenEvents.size > 500) {
      seenEvents.delete(seenEvents.values().next().value);
    }
    const message = JSON.parse(event.data);
    const type = message.type || event.type;
    if (type === "task.created") insertSkeletonRow(message);
    if (type === "task.claimed") updateClaim(message, true);
    if (type === "task.released") updateClaim(message, false);
    if (type === "task.completed") closeTask(message, "completed");
    if (type === "task.cancelled") closeTask(message, "cancelled");
    if (type === "task.reopened") reopenTask(message);
    if (type === "finding.posted") handleFinding(message);
    // task.updated carries only a task_id and lens.refresh carries nothing at
    // all, so both are served by the requires_refresh reconcile below - as is
    // any type this build does not know yet.
    if (message.requires_refresh) scheduleReconcile();
  }

  function rowFor(taskId) {
    return document.querySelector(`[data-task-row][data-task-id="${cssEscape(taskId)}"]`);
  }

  function insertSkeletonRow(message) {
    const taskId = message.task_id;
    if (!taskId || rowFor(taskId)) return;
    // Not on a narrowed board. The `task.created` payload carries no tags, no
    // project and no creator, so there is nothing here to evaluate the new
    // task against the active scope — inserting anyway puts a row that ASSERTS
    // membership onto a board that never checked it, and if the ~800ms
    // reconcile then fails, it persists with nothing to say it is wrong. A
    // cross-project tag board is exactly where an unrelated task appearing is
    // both likely and confusing. `boardFiltered` is decided server-side so the
    // preserved-key list has one definition (request_filters.board_is_filtered).
    if (config.boardFiltered) return;
    // Which is also why the link below carries NO query string. Every other
    // detail link goes through `task_detail_url`, which re-emits the preserved
    // filters through an allowlist — so a retired param like `claimed_state`
    // stops at the link rather than propagating (pinned by
    // test_legacy_claimed_state_bookmark_does_not_propagate_through_navigation).
    // Appending `window.location.search` raw would have made this the one link
    // that leaks it. And the allowlisted set is necessarily EMPTY here: the
    // guard above means no preserved filter is active when this row renders.
    // So "preserve the filters" and "emit a bare task URL" are the same link.
    // A just-created task has no known section yet (its frontier membership
    // arrives with the ~800ms reconciliation), so the skeleton lands in the
    // dedicated pending strip at the top of the board; the reconcile's
    // fragment replace then re-renders it in its real section.
    const list = document.querySelector('[data-task-list="pending"]');
    if (!list) return;
    const title = message.payload && message.payload.title ? message.payload.title : `Task ${taskId}`;
    const row = document.createElement("article");
    row.className = "task-row task-row-skeleton";
    row.id = `task-row-${taskId}`;
    row.dataset.taskRow = "";
    row.dataset.taskId = taskId;
    row.dataset.taskStatus = "open";
    row.innerHTML = `
      <div><a class="task-title" href="/tasks/${encodeURIComponent(taskId)}">${escapeHtml(title)}</a><p>Loading full task details...</p></div>
      <div class="task-row-meta"><span class="badge badge-open">open</span><span class="claim-chip claim-chip-unknown" data-claim-summary>claims unknown</span></div>
      <div class="claim-list" data-claim-list hidden></div>
    `;
    list.prepend(row);
  }

  function updateClaim(message, claimed) {
    const row = rowFor(message.task_id);
    if (!row) return;
    const payload = message.payload || {};
    const aspect = payload.aspect || "claim";
    const agent = payload.agent || "unknown";
    const claimList = row.querySelector("[data-claim-list]");
    if (!claimList) return;
    const existing = claimList.querySelector(`[data-claim-aspect="${cssEscape(aspect)}"]`);
    if (claimed) {
      if (existing) existing.textContent = `${aspect} - ${agent}`;
      if (!existing) {
        const chip = document.createElement("span");
        chip.dataset.claimAspect = aspect;
        chip.textContent = `${aspect} - ${agent}`;
        claimList.appendChild(chip);
      }
      claimList.hidden = false;
      setClaimSummary(row, "claimed");
    } else {
      if (existing) existing.remove();
      if (!claimList.children.length) claimList.hidden = true;
      setClaimSummary(row, claimList.children.length ? "claimed" : "unclaimed");
    }
  }

  function setClaimSummary(row, state) {
    let summary = row.querySelector("[data-claim-summary]");
    if (!summary) {
      summary = document.createElement("span");
      summary.className = "claim-chip";
      summary.dataset.claimSummary = "";
      row.querySelector(".task-row-meta").appendChild(summary);
    }
    const count = row.querySelectorAll("[data-claim-aspect]").length;
    summary.className = state === "unclaimed" ? "claim-chip claim-chip-open" : "claim-chip";
    summary.textContent = state === "unclaimed" ? "unclaimed" : `${count || 1} claim${count === 1 ? "" : "s"}`;
  }

  function closeTask(message, status) {
    const row = rowFor(message.task_id);
    if (!row) return;
    row.dataset.taskStatus = status;
    const badge = row.querySelector(".badge");
    if (badge) {
      badge.className = `badge badge-${status}`;
      badge.textContent = status;
    }
    const target = document.querySelector(`[data-task-list="${status}"]`);
    if (target) target.prepend(row);
    if (!target) row.remove();
  }

  function reopenTask(message) {
    const row = rowFor(message.task_id);
    if (!row) return;
    row.dataset.taskStatus = "open";
    const badge = row.querySelector(".badge");
    if (badge) {
      badge.className = "badge badge-open";
      badge.textContent = "open";
    }
    // Which workable section the task belongs to now is the frontier's answer,
    // not ours, so the row waits in the pending strip until the reconcile
    // re-renders the board - the same reason a just-created task lands there.
    //
    // But NOT onto a board whose status filter excludes open rows. The pending
    // strip renders on EVERY board, so parking the row there puts it back on
    // screen under a filter that no longer admits it - and if the ~800ms
    // reconcile then fails, it persists with nothing to say it is wrong. Drop
    // it instead: a row missing for ~800ms is recoverable, one stuck out of
    // scope is not. `closeTask` above is conservative the same way, removing a
    // row whose new status has no list on this board rather than parking it.
    //
    // `boardAdmitsOpen`, not `boardFiltered`: this row was SERVER-RENDERED
    // here, so it already passed every filter, and reopening changes only its
    // status. A `since` or `tag` board still holds it (see
    // request_filters.board_admits_open).
    const list = config.boardAdmitsOpen
      ? document.querySelector('[data-task-list="pending"]')
      : null;
    if (list) list.prepend(row);
    if (!list) row.remove();
  }

  function handleFinding(message) {
    const row = rowFor(message.task_id);
    if (row) {
      const chip = row.querySelector("[data-finding-count]");
      if (chip) {
        const count = Number(chip.dataset.count || "0") + 1;
        chip.dataset.count = String(count);
        chip.hidden = false;
        chip.textContent = `${count} new finding${count === 1 ? "" : "s"}`;
      }
    }
    if (config.detailTaskId && config.detailTaskId === message.task_id) {
      scheduleReconcile(100);
    }
  }

  function connect() {
    if (eventSource) eventSource.close();
    eventSource = new EventSource(eventsUrl);
    eventSource.addEventListener("open", function () {
      setLiveStatus("live", "Listening for Lithos task events");
      stopPolling();
      if (reconnectRefreshPending) {
        reconnectRefreshPending = false;
        refreshFragments();
      }
    });
    eventSource.addEventListener("error", function () {
      setLiveStatus("paused", "Reconnecting; polling fallback is active");
      reconnectRefreshPending = true;
      startPolling();
    });
    // agent.registered is deliberately absent: it is system-scoped, carries
    // requires_refresh=false, and must not move the board.
    ["task.created", "task.claimed", "task.released", "task.completed", "task.cancelled", "task.updated", "task.reopened", "finding.posted", "lens.refresh"].forEach(function (type) {
      eventSource.addEventListener(type, handleEvent);
    });
  }

  // Timer gates: Lithos resolves them at query time and emits NO event when
  // one lapses, so the board schedules its own single refresh at the earliest
  // still-future ready_at the server put on the task board.
  //
  // Two guards keep this a one-shot rather than a refresh loop, because
  // ready_at is server metadata any Lithos writer can set:
  //   - a delay beyond the 32-bit timer range is CHAINED, not truncated (an
  //     oversized setTimeout fires immediately and would re-arm every render);
  //   - a stamp already in the past on arrival means the browser and Lens
  //     clocks disagree — the server only publishes future stamps — so it
  //     backs off to the poll interval instead of hammering /tasks.
  // Whatever the board says, self-triggered refreshes stay at least one poll
  // interval apart: no server value can make a tab outpace its own polling.
  function scheduleGateRefresh() {
    window.clearTimeout(gateRefreshTimer);
    gateRefreshTimer = null;
    const board = document.querySelector("[data-gates-next-ready-at]");
    if (!board) return;
    const readyAt = Date.parse(board.dataset.gatesNextReadyAt);
    if (!readyAt) return;
    const remaining = readyAt - Date.now();
    let delay;
    if (remaining > 0) {
      // A still-future deadline the server just published. Honour it, floored
      // only by the minimum SPACING between self-triggered refreshes — the
      // floor's job is to bound the rate, and borrowing the poll interval for
      // it swallowed the next real deadline: gates fall due one after another,
      // and a second gate 3.5s behind the first was pushed out a full 30s,
      // ticking "ready now" for the rest of it.
      delay = remaining + GATE_REFRESH_GRACE_MS;
      if (lastGateRefreshAt) {
        delay = Math.max(delay, lastGateRefreshAt + GATE_REFRESH_MIN_SPACING_MS - Date.now());
      }
    } else {
      // Past on arrival: the server only publishes future stamps, so either the
      // clocks disagree or the refresh did not clear the gate. Nothing to be
      // on time for, and re-requesting immediately would spin — back off to the
      // poll interval, which is what that bound is actually for.
      delay = autoRefreshIntervalMs;
      if (lastGateRefreshAt) {
        delay = Math.max(delay, lastGateRefreshAt + autoRefreshIntervalMs - Date.now());
      }
    }
    if (delay > MAX_TIMER_DELAY_MS) {
      gateRefreshTimer = window.setTimeout(scheduleGateRefresh, MAX_TIMER_DELAY_MS);
      return;
    }
    gateRefreshTimer = window.setTimeout(runGateRefresh, delay);
  }

  function runGateRefresh() {
    lastGateRefreshAt = Date.now();
    // Re-armed on BOTH outcomes: a render that succeeded already re-armed
    // itself from the fresh fragment (scheduleGateRefresh clears first, so the
    // second call is idempotent), and one that failed must not leave the board
    // with no schedule at all — the lapsed gate would sit there until an event
    // or the poll fallback happened along.
    const rearm = function () { scheduleGateRefresh(); };
    refreshFragments().then(rearm, rearm);
  }

  function renderGateCountdowns() {
    const nodes = document.querySelectorAll("[data-gate-ready-at]");
    if (!nodes.length) {
      // The refreshed board has no timer gate left to tick.
      window.clearInterval(gateCountdownTimer);
      gateCountdownTimer = null;
      return;
    }
    nodes.forEach(function (node) {
      const readyAt = Date.parse(node.dataset.gateReadyAt);
      // Unparseable server metadata keeps the server-rendered absolute stamp
      // rather than being blanked into an empty chip.
      if (!readyAt) return;
      node.textContent = formatCountdown(readyAt - Date.now());
    });
    if (!gateCountdownTimer) {
      gateCountdownTimer = window.setInterval(renderGateCountdowns, 1000);
    }
  }

  function formatCountdown(remainingMs) {
    if (remainingMs <= 0) return "ready now";
    const seconds = Math.floor(remainingMs / 1000);
    const parts = [
      [Math.floor(seconds / 86400), "d"],
      [Math.floor((seconds % 86400) / 3600), "h"],
      [Math.floor((seconds % 3600) / 60), "m"],
      [seconds % 60, "s"]
    ];
    // Two units are enough to read at a glance ("2d 3h", "4m 20s"); leading
    // zero units are dropped so a short wait doesn't render as "0d 0h".
    const shown = parts.filter(function (part) { return part[0] > 0; }).slice(0, 2);
    if (!shown.length) return "ready now";
    return "ready in " + shown.map(function (part) { return part[0] + part[1]; }).join(" ");
  }

  function cssEscape(value) {
    if (window.CSS && window.CSS.escape) return window.CSS.escape(value);
    return String(value).replace(/"/g, '\\"');
  }

  function escapeHtml(value) {
    const element = document.createElement("span");
    element.textContent = value;
    return element.innerHTML;
  }

  function setupDatePickers() {
    document.querySelectorAll(".date-picker-control").forEach(function (control) {
      if (control.dataset.datePickerBound === "true") return;
      control.dataset.datePickerBound = "true";
      const display = control.querySelector("[data-display-date]");
      const native = control.querySelector("[data-native-date]");
      const button = control.querySelector("[data-open-date-picker]");
      if (!display || !native || !button) return;
      button.addEventListener("click", function () {
        if (native.showPicker) {
          native.showPicker();
        } else {
          native.focus();
          native.click();
        }
      });
      native.addEventListener("change", function () {
        display.value = isoToUkDate(native.value);
        display.dispatchEvent(new Event("input", { bubbles: true }));
      });
      display.addEventListener("change", function () {
        const iso = ukToIsoDate(display.value);
        if (iso) native.value = iso;
      });
    });
  }

  function isoToUkDate(value) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
    if (!match) return "";
    return `${match[3]}/${match[2]}/${match[1]}`;
  }

  function ukToIsoDate(value) {
    const match = /^(\d{2})\/(\d{2})\/(\d{4})$/.exec(value || "");
    if (!match) return "";
    return `${match[3]}-${match[2]}-${match[1]}`;
  }

  const liveRoot = document.querySelector("[data-live-status]");
  if (liveRoot) {
    currentLiveState = liveRoot.dataset.liveState || currentLiveState;
  }
  setupDatePickers();
  renderGateCountdowns();
  scheduleGateRefresh();
  connect();
})();
