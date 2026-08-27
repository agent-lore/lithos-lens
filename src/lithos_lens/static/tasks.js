(function () {
  const config = window.LithosLensTasks || {};
  const eventsUrl = config.eventsUrl || "/tasks/events";
  const autoRefreshIntervalMs = config.autoRefreshIntervalMs || 30000;
  const seenEvents = new Set();
  const detailTaskId = config.detailTaskId || "";
  // Two bounds on how often an EVENT STREAM may make this page render, because
  // the stream is driven by clients that need no access to Lens at all
  // (lithos_finding_post takes {task_id, agent, summary}, uncredentialed) and
  // every event type carries requires_refresh.
  //
  // The FLOOR is per refresh path: never more than one run per interval, so
  // the event rate cannot become the render rate. The detail page's whole-page
  // reconcile is the expensive one (the graph fan-out behind /tasks/{id}), so
  // it gets the long floor; the findings fragment is ~26 upstream calls, so it
  // gets a short one. The dashboard keeps its ~800ms cadence — its reconcile
  // is a list render, not a fan-out. Rate is the ONLY thing bounded: no event
  // is dropped and no refresh path is skipped, because deciding that a given
  // event "changes nothing else on this page" is a claim about every element
  // the page renders, and this file is the wrong place to make it.
  //
  // The CEILING is the answer to the other direction: a debounce that re-arms
  // on every event is starved by a stream faster than the debounce, so a
  // pending refresh may be deferred, but never past this long from the FIRST
  // event that deferred it. Without it "it lands later" would mean "it lands
  // never, while the burst continues" — the board looking live and holding
  // stale blockers, claims and statuses for as long as an agent keeps posting.
  const DETAIL_RECONCILE_MIN_INTERVAL_MS = 5000;
  const FINDINGS_MIN_INTERVAL_MS = 1000;
  const MAX_DEFER_MS = 5000;
  let eventSource = null;
  let pollTimer = null;
  let reconnectRefreshPending = false;
  let latestRefreshToken = 0;
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

  // One debounced refresh path: `run` at most once per `minIntervalMs`, and
  // never deferred past MAX_DEFER_MS from the first event that deferred it.
  // `lastRunAt` is stamped by the run itself, so a refresh issued any other
  // way (the polling fallback, a reconnect) counts against the floor too.
  const reconcile = {
    run: refreshFragments,
    // The floor is the DETAIL page's: on the dashboard this path is cheap.
    minIntervalMs: detailTaskId ? DETAIL_RECONCILE_MIN_INTERVAL_MS : 0,
    timer: null,
    deferredSince: 0,
    lastRunAt: 0
  };
  const findings = {
    run: refreshFindings,
    minIntervalMs: FINDINGS_MIN_INTERVAL_MS,
    timer: null,
    deferredSince: 0,
    lastRunAt: 0,
    inFlight: null
  };

  function scheduleRefresh(path, delay) {
    const now = Date.now();
    if (!path.deferredSince) path.deferredSince = now;
    const at = Math.max(
      path.lastRunAt + path.minIntervalMs,
      Math.min(now + delay, path.deferredSince + MAX_DEFER_MS)
    );
    window.clearTimeout(path.timer);
    path.timer = window.setTimeout(function () {
      path.deferredSince = 0;
      path.run();
    }, Math.max(at - now, 0));
  }

  function scheduleReconcile(delay) {
    scheduleRefresh(reconcile, delay || 800);
  }

  // The findings timeline on its own, from the endpoint built for exactly this
  // (/tasks/{id}/findings): one lithos_finding_list plus a bounded page of
  // title reads, instead of the whole page's blocker/provenance/children
  // fan-out. A finding event takes this path AND the reconcile, so the
  // timeline lands on the short floor while the rest of the page — header
  // badges, Resolution, the reopen marker — lands on the long one. What the
  // cheap render buys is prompt timeline updates, not permission to skip.
  function scheduleFindingsRefresh(delay) {
    scheduleRefresh(findings, delay || 800);
  }

  async function refreshFindings() {
    if (!detailTaskId) return;
    findings.lastRunAt = Date.now();
    // One findings fetch in flight per tab. The floor bounds how often this is
    // ISSUED; without an abort nothing bounded how many were OUTSTANDING — a
    // slow Lithos (up to the server's 20s render budget) let a tab stack
    // fetches past the browser's ~6-connection pool for the origin and queue
    // everything else the page needs behind them. Superseding the request also
    // makes the response-ordering guard unnecessary: at most one can answer.
    if (findings.inFlight) findings.inFlight.abort();
    const controller = new AbortController();
    findings.inFlight = controller;
    try {
      const response = await fetch(`/tasks/${encodeURIComponent(detailTaskId)}/findings`, {
        headers: { "X-Lithos-Lens-Refresh": "findings" },
        signal: controller.signal
      });
      if (!response.ok) return;
      const text = await response.text();
      const doc = new DOMParser().parseFromString(text, "text/html");
      replaceFragment(doc, "findings");
    } catch (error) {
      // An abort is this function superseding itself, not a failure.
      if (error.name !== "AbortError") throw error;
    } finally {
      if (findings.inFlight === controller) findings.inFlight = null;
    }
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = window.setInterval(refreshFragments, autoRefreshIntervalMs);
  }

  function stopPolling() {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }

  async function refreshFragments() {
    const token = ++latestRefreshToken;
    reconcile.lastRunAt = Date.now();
    const response = await fetch(window.location.href, {
      headers: { "X-Lithos-Lens-Refresh": "tasks" }
    });
    if (!response.ok || token !== latestRefreshToken) return;
    const text = await response.text();
    const doc = new DOMParser().parseFromString(text, "text/html");
    replaceFragment(doc, "dashboard-data");
    if (detailTaskId) {
      replaceFragment(doc, "detail");
    }
    setupDatePickers();
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
    if (type === "finding.posted") handleFinding(message);
    // Every event with requires_refresh reconciles, findings included. A
    // finding is NOT confined to the timeline fragment: `[Reopened]` findings
    // drive `detail.reopen_report`, whose marker renders in the header's
    // detail-meta, and a reopen also clears the status, resolved_at and
    // outcome the page shows — none of which the fragment swaps. Skipping the
    // reconcile here (T1-S7 round 2) left an open detail page reading
    // "completed" with a stale Resolution block indefinitely, while its
    // timeline showed the reopen and the live chip read connected. The FLOOR
    // above is what bounds the cost; the skip was one step further than that
    // needed, and it cost correctness.
    if (message.requires_refresh) scheduleReconcile();
  }

  function rowFor(taskId) {
    return document.querySelector(`[data-task-row][data-task-id="${cssEscape(taskId)}"]`);
  }

  function insertSkeletonRow(message) {
    const taskId = message.task_id;
    if (!taskId || rowFor(taskId)) return;
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
    // The timeline of the open task gets the fast path (its own floor, ~26
    // upstream calls); everything else a finding can change on this page rides
    // the reconcile the caller schedules.
    if (detailTaskId && detailTaskId === message.task_id) {
      scheduleFindingsRefresh(100);
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
    ["task.created", "task.claimed", "task.released", "task.completed", "task.cancelled", "finding.posted"].forEach(function (type) {
      eventSource.addEventListener(type, handleEvent);
    });
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
  connect();
})();
