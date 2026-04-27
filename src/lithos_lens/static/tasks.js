(function () {
  const config = window.LithosLensTasks || {};
  const eventsUrl = config.eventsUrl || "/tasks/events";
  const autoRefreshIntervalMs = config.autoRefreshIntervalMs || 30000;
  const seenEvents = new Set();
  let eventSource = null;
  let reconcileTimer = null;
  let pollTimer = null;
  let reconnectRefreshPending = false;
  let latestRefreshToken = 0;

  function setLiveStatus(status, detail) {
    const root = document.querySelector("[data-live-status]");
    const label = document.querySelector("[data-live-status-label]");
    const description = document.querySelector("[data-live-status-detail]");
    if (!root || !label || !description) return;
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

  async function refreshFragments() {
    const token = ++latestRefreshToken;
    const response = await fetch(window.location.href, {
      headers: { "X-Lithos-Lens-Refresh": "tasks" }
    });
    if (!response.ok || token !== latestRefreshToken) return;
    const text = await response.text();
    const doc = new DOMParser().parseFromString(text, "text/html");
    replaceFragment(doc, "situation");
    replaceFragment(doc, "tasks");
    if (config.detailTaskId) {
      replaceDetailFindings(doc);
    }
  }

  function replaceFragment(doc, name) {
    const current = document.querySelector(`[data-refresh-fragment="${name}"]`);
    const next = doc.querySelector(`[data-refresh-fragment="${name}"]`);
    if (current && next) current.replaceWith(next);
  }

  function replaceDetailFindings(doc) {
    const current = document.querySelector(".findings-timeline") || document.querySelector(".detail-panel section:last-child");
    const next = doc.querySelector(".findings-timeline") || doc.querySelector(".detail-panel section:last-child");
    if (current && next) current.replaceWith(next);
  }

  function handleEvent(event) {
    if (!event.id || seenEvents.has(event.id)) return;
    seenEvents.add(event.id);
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
    if (message.requires_refresh) scheduleReconcile();
  }

  function rowFor(taskId) {
    return document.querySelector(`[data-task-row][data-task-id="${cssEscape(taskId)}"]`);
  }

  function insertSkeletonRow(message) {
    const taskId = message.task_id;
    if (!taskId || rowFor(taskId)) return;
    const list = document.querySelector('[data-task-list="open"]');
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

  connect();
})();
