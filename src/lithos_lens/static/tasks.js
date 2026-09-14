(function () {
  const config = window.LithosLensTasks || {};
  const eventsUrl = config.eventsUrl || "/tasks/events";
  // The HOST page's one selection parameter (REQUIREMENTS §5.5): `selected` on
  // the dashboard, `focus` on the graph page, which passes its own. The panel
  // below is one implementation for both, so the parameter is configuration
  // rather than a second copy of the panel code per page.
  const selectionParam = config.selectionParam || "selected";
  const autoRefreshIntervalMs = config.autoRefreshIntervalMs || 30000;
  const seenEvents = new Set();
  // Pages that CONSUME events without reconciling their own markup — the graph
  // page, which raises a "graph changed" pill instead of re-rendering (D8) —
  // subscribe here. One EventSource per tab either way: a second subscription
  // would mean a second connection and a second copy of the dedup below.
  const eventSubscribers = [];
  // Pages that render the selection somewhere ELSE than the panel — the graph
  // page's canvas, which lights the focused node — subscribe here. Every panel
  // transition is a `pushState`, and `pushState` fires no `popstate`, so a host
  // with its own view of the selection has no way to notice one without being
  // told: closing the panel would clear the URL and leave the node still lit.
  const panelSubscribers = [];
  let eventSource = null;
  let reconcileTimer = null;
  let pollTimer = null;
  let reconnectRefreshPending = false;
  let refreshInFlight = false;
  let refreshQueued = false;
  let gateRefreshTimer = null;
  let gateCountdownTimer = null;
  let lastGateRefreshAt = 0;
  // Which task the open panel is showing, "" when it is closed. Seeded from
  // the URL at load, because `?selected=` renders the panel server-side.
  let selectedTaskId = "";
  // Which task the panel is MEANT to be showing — set the moment an open
  // starts, cleared the moment one closes. Between it and `selectedTaskId`
  // sits an in-flight request, and that gap is where the URL and the panel can
  // disagree: a Forward back onto the selection already on screen still has to
  // supersede an open running under it, or that open's late response paints a
  // panel the URL has moved away from.
  let desiredTaskId = "";
  // Every panel INTENT — an open, a close — takes the next generation, and no
  // response may write the panel unless its generation is still the current
  // one. Without it the panel is whichever request happened to answer LAST
  // rather than whatever the operator asked for most recently: click row A
  // then row B, let B answer first, and A's older response would overwrite B
  // and push `selected=A` over it. The same counter supersedes an in-flight
  // open when the panel is closed under it (Back past a selection), and stops
  // a reconcile fetched for one selection from painting its panel over
  // another. Cancelling the fetch would not do: the response is already on its
  // way, and it is the WRITE that has to be ordered, not the read.
  let panelGeneration = 0;
  // The generation of an open that has STARTED and not yet settled — 0 when
  // none has. `desiredTaskId` cannot answer that question: a page loaded with
  // a selection seeds BOTH ids from the URL (below), so an open the client
  // then starts for it — the fallback for a `focus=` the server could not
  // render — runs with the two already equal, and a reader comparing them
  // would call that request settled while it is still in flight (round-3
  // correctness f-001).
  //
  // Only the NEWEST open can still write: every one before it has had the
  // generation moved past it and returns in silence. So this is one number,
  // and it needs clearing only where an open SETTLES — a superseded one is
  // cleared by the same bump that superseded it, because that bump is what
  // makes this no longer equal to `panelGeneration`.
  let pendingOpenGeneration = 0;
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
    // Opt-out for a host page whose markup this must not rebuild. The graph
    // page sets it: a reconcile there would re-fetch a whole graph assembly per
    // event and re-lay-out the canvas under the operator's cursor, which D8
    // forbids outright — that page shows a refresh pill and waits. Guarded HERE
    // rather than at each caller so the poll fallback, the debounced reconcile
    // and the gate timer are all covered by the one rule.
    if (config.liveRefresh === false) return;
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
    // Which selection this render is FOR. The reconcile fetches the live URL,
    // so the panel it comes back with is the one THAT URL named — which is not
    // always the selection the operator has by the time it answers. Both facts
    // are captured: the URL, because a click already in flight when this left
    // pushes a new one without touching the generation; and the generation,
    // because a close-and-reopen can land back on the same URL with a fresher
    // panel of its own.
    const refreshUrl = window.location.href;
    const panelGenerationAtFetch = panelGeneration;
    const response = await fetch(refreshUrl, {
      headers: { "X-Lithos-Lens-Refresh": "tasks" }
    });
    if (!response.ok) return;
    const text = await response.text();
    const doc = new DOMParser().parseFromString(text, "text/html");
    replaceFragment(doc, "dashboard-data");
    if (config.detailTaskId) {
      replaceFragment(doc, "detail");
    }
    // The open panel carries live blocker and dependent statuses, and the
    // reconcile already fetched this URL — which, after a row click, names the
    // selection. Swapped separately from the board so the panel is not torn
    // down and rebuilt under the cursor on every event, and only while the
    // selection is still the one it was rendered for (the board fragment above
    // does not depend on the selection, so it is applied either way).
    if (
      panelGeneration === panelGenerationAtFetch &&
      selectionIn(refreshUrl) === selectionIn(window.location.href)
    ) {
      replaceFragment(doc, "panel");
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
    if (!current || !next) return;
    current.replaceWith(next);
    // htmx wires hx-* attributes on nodes IT swapped and on the initial page;
    // it does not watch the DOM. These nodes were parsed out of a fetched
    // document and inserted by hand, so without this every blocker expander in
    // the detail fragment (T1-S8) goes inert at the first reconcile - roughly
    // 30s after the page loads, or sooner on any task event.
    if (window.htmx) window.htmx.process(next);
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
    // Before the board handlers, so a subscriber sees every event this tab
    // consumed whatever this page does with it afterwards.
    eventSubscribers.forEach(function (subscriber) { subscriber(message, type); });
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

  // ── The side panel (§5.5): fetch the fragment, push the selection ────────
  //
  // The panel is SERVER-rendered markup throughout — `?selected=<id>` renders
  // it into the board, and a row click fetches the same partial from
  // `/tasks/{id}?fragment=panel`. Nothing here builds panel HTML, so the no-JS
  // baseline and the clicked panel cannot drift.

  function panelHost() {
    return document.querySelector("[data-panel-host]");
  }

  // Announced AFTER the URL has moved, so a subscriber reading the address bar
  // sees the transition that has just completed rather than the one before it.
  function announceSelection() {
    panelSubscribers.forEach(function (subscriber) { subscriber(selectedTaskId); });
  }

  // The panel contract a row opts into, and the ONE selector both halves of
  // the interaction use: the id the click selects plus the server-built URL
  // its panel comes from. Every kind of board row carries the pair —
  // tasks/row.html, tasks/gate_row.html (a gate is a task, and §5.5 puts a
  // panel behind every row, not behind the workable ones only) and the
  // skeleton `insertSkeletonRow` builds below. Deliberately NOT
  // `[data-task-row]`: that attribute is the SSE handlers' hook for the rows
  // they may rewrite in place, and a gate row's chrome is not theirs to touch.
  // Requiring `data-task-id` as well keeps the panel HOST out of the match —
  // it carries a `data-panel-url` of its own for the selection it rendered.
  const PANEL_ROW = "[data-panel-url][data-task-id]";

  function panelRowFor(taskId) {
    return document.querySelector(
      `[data-panel-url][data-task-id="${cssEscape(taskId)}"]`
    );
  }

  // The query alias addresses EVERY id — the path form is what the ids that
  // collide with a page under `/tasks/` cannot use — and its path and key come
  // from the server rather than being spelled out here.
  function aliasPanelUrl(taskId) {
    const alias = new URLSearchParams();
    alias.set(config.panelAliasKey || "task_id", taskId);
    alias.set("fragment", "panel");
    return `${config.panelAliasPath || "/tasks/id"}?${alias.toString()}`;
  }

  function panelUrlFor(taskId) {
    // Both sources here are URLs the SERVER built, and that is the point: task
    // ids are arbitrary strings, and the id that collides with a page under
    // `/tasks/` (`graph`) must be addressed through the query alias or the
    // fetch lands on the graph PAGE and that page gets swapped into the panel.
    // `tasks.task_detail_path` owns that rule; the browser does not restate it.
    //
    // The row's `data-panel-url` also carries the board's preserved filters,
    // so the panel comes back with Expand and Close links inside the scope the
    // operator is browsing.
    const row = panelRowFor(taskId);
    if (row && row.dataset.panelUrl) return row.dataset.panelUrl;
    // No row for it: a deep link to a task the board's filters exclude. The
    // host keeps the URL the server built for the SELECTION it rendered, so
    // Back and Forward can reopen exactly that task — the case where there has
    // never been a row to read it off.
    const host = panelHost();
    if (host && host.dataset.panelSelected === taskId && host.dataset.panelUrl) {
      return host.dataset.panelUrl;
    }
    // Last resort, for a task this tab has no server-built URL for (its row
    // left the board on a reconcile): the query alias. The board's filters are
    // lost, which costs the panel's own links their scope; opening the right
    // task without them beats opening the wrong page with them.
    return aliasPanelUrl(taskId);
  }

  // Which task a URL selects, "" for none. One reading, shared by the load-time
  // seed, the popstate handler and the reconcile's staleness check — the three
  // places that have to agree on what the address bar currently means.
  function selectionIn(url) {
    return new URL(url, window.location.href).searchParams.get(selectionParam) || "";
  }

  // This page's URL with the selection parameter set to `taskId`, or removed
  // when it is empty. Built from the live URL rather than from a remembered
  // query string, so closing the panel preserves every filter, the epic scope
  // and the resolved-since window exactly as they arrived.
  //
  // The FRAGMENT is part of that state and is carried too. Every summary card
  // links to a section of the board (`task_card_url` appends
  // `#task-group-blocked`), so an operator can arrive at a board already
  // scrolled to one group; rebuilding the URL without its hash would clear
  // that on the first panel open and never give it back, which is exactly the
  // "closing clears the selection and nothing else" contract this is for.
  function selectionUrl(taskId) {
    const url = new URL(window.location.href);
    if (taskId) url.searchParams.set(selectionParam, taskId);
    else url.searchParams.delete(selectionParam);
    return url.pathname + url.search + url.hash;
  }

  async function openPanel(taskId, options) {
    const host = panelHost();
    if (!host || !taskId) return;
    const push = !options || options.push !== false;
    // Claimed BEFORE the fetch: from here on, anything that changes the
    // selection supersedes this request, whichever order the responses land in.
    panelGeneration += 1;
    desiredTaskId = taskId;
    const generation = panelGeneration;
    pendingOpenGeneration = generation;
    // `null` means "no panel to show", whatever went wrong — a transport
    // failure, a non-OK answer, or a body that never finished reading. The
    // three are one outcome here and share one recovery below; an EMPTY body
    // is deliberately not one of them, so a legitimately empty 200 still
    // renders as the empty panel it is.
    let markup = null;
    try {
      const response = await fetch(panelUrlFor(taskId), {
        headers: { "X-Lithos-Lens-Refresh": "panel" }
      });
      // Superseded while in flight — a newer click, a close, or a Back past
      // this selection. Dropped in silence: the newer intent already owns the
      // panel and the URL, and writing either here would undo it.
      if (generation !== panelGeneration) return;
      // The route answers an unknown id with the not-found PANEL at 200, so a
      // non-OK response is a transport-level failure, not "no such task".
      if (response.ok) markup = await response.text();
    } catch (error) {
      // Caught rather than propagated: nothing awaits this call (a click
      // handler and a popstate handler start it), so a rejection could only
      // become an unhandled one — and the recovery is the same either way.
      markup = null;
    }
    // Checked again after the second await: reading the body is a suspension
    // point of its own, and the gap between "headers arrived" and "body read"
    // is long enough for another click to land in it.
    if (generation !== panelGeneration) return;
    // Settled from here on, one way or the other: nothing this request does
    // afterwards is still pending.
    pendingOpenGeneration = 0;
    if (markup === null) {
      panelFetchFailed(push);
      return;
    }
    host.innerHTML = markup;
    // Same reason replaceFragment does it: these nodes were parsed out of a
    // fetched document, and htmx only wires the ones it swapped itself.
    if (window.htmx) window.htmx.process(host);
    selectedTaskId = taskId;
    // Pushed AFTER the swap, so a URL never claims a panel that failed to
    // open — and only when it MOVES the address bar. Opening the task the URL
    // already names is a real open (the panel may be absent, or stale, and the
    // fetch above has just answered it) but not a real navigation: clicking
    // the selected row again, or retrying a task whose popstate fetch failed
    // under its own URL, would otherwise stack a second identical entry. The
    // Back that should leave the task would then land on its twin, match
    // `desiredTaskId` in the popstate handler, and do nothing until pressed a
    // second time.
    if (push && selectionIn(window.location.href) !== taskId) {
      window.history.pushState({ selected: taskId }, "", selectionUrl(taskId));
    }
    announceSelection();
  }

  function panelFetchFailed(push) {
    // No panel arrived, and what that COSTS depends on who asked — because the
    // two callers differ on whether the URL has already moved.
    if (push) {
      // A click. Its URL is pushed only on success, so the address bar and the
      // panel still agree and both stay. Only the INTENT has to be walked back
      // to what is actually on screen: left claiming the task that failed, the
      // next Forward onto that very selection would match the intent, return
      // early, and leave the previous task's panel sitting under it.
      desiredTaskId = selectedTaskId;
      // Announced anyway: a host that lit the clicked node the moment the
      // click landed has to be told the open did NOT happen, or the canvas
      // keeps a node lit for a panel that never opened and a URL that never
      // moved.
      announceSelection();
      return;
    }
    // Back or forward. The browser moved the URL BEFORE this ran, so the panel
    // on screen already names a different task than the address bar does —
    // the one state the panel must never be left in. It is cleared rather than
    // kept: an empty panel under a selection the operator can retry (reload,
    // or navigate to it again) is a missing answer, while the previous task's
    // panel under this URL is a wrong one. The selection is dropped with it,
    // so navigating back here really does retry instead of matching a stale
    // intent and doing nothing.
    const host = panelHost();
    if (host) host.innerHTML = "";
    selectedTaskId = "";
    desiredTaskId = "";
    announceSelection();
  }

  function closePanel(options) {
    // Closing is an intent like any other, so it takes a generation too: an
    // open still in flight under it must not reopen the panel afterwards.
    panelGeneration += 1;
    desiredTaskId = "";
    const host = panelHost();
    if (host) host.innerHTML = "";
    selectedTaskId = "";
    if (!options || options.push !== false) {
      window.history.pushState({ selected: "" }, "", selectionUrl(""));
    }
    announceSelection();
  }

  // Drop a panel open that has not painted yet, and leave what IS on screen
  // alone. The graph page's canvas calls this the moment a node tap starts a
  // new gesture: an open from an EARLIER click may still be in flight, and its
  // response would insert the panel BESIDE the canvas (D9) — narrowing the
  // canvas, refitting it, and moving the node out from under a pointer that is
  // halfway through a double-click. That is the failure the `onetap` debounce
  // exists to prevent, reached through an older request instead of through
  // this gesture's own (round-2 correctness f-001).
  //
  // Only the INTENT is walked back, the same way a failed open walks it back,
  // because there is nothing on screen to undo. What is ALREADY on screen is
  // left exactly as it is — an operator who opened a panel and then began a
  // gesture elsewhere still has the panel they asked for, and a server-
  // rendered one is not disturbed either. Nothing is announced: the selection
  // has not changed, and announcing it would re-render the host's own view of
  // it in the middle of the gesture this exists to protect.
  //
  // The test is whether a request is PENDING, not whether the ids differ: they
  // are equal for a `focus=` the page loaded with, and the client's fallback
  // open for it is exactly a response that must not land mid-gesture.
  function supersedePendingOpen() {
    if (pendingOpenGeneration !== panelGeneration) return;
    panelGeneration += 1;
    desiredTaskId = selectedTaskId;
  }

  function handlePanelClick(event) {
    // Modified and non-primary clicks keep their browser meaning: open in a
    // new tab has to stay open in a new tab, on a row as much as on a link.
    if (event.defaultPrevented) return;
    if (event.button !== undefined && event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const target = event.target;
    if (!target || !target.closest) return;
    if (target.closest("[data-panel-close]")) {
      event.preventDefault();
      closePanel();
      return;
    }
    // Inside the panel, every link is an ordinary link — Expand navigates to
    // the full page, and a blocker or parent opens that task's own page.
    if (target.closest("[data-task-panel]")) return;
    // A <summary> is the browser's own control for the <details> it opens, and
    // a gate row carries one (the waiter list, which works with no JS at all).
    // Swallowing that click would trade the disclosure for a panel open and
    // the list could never be expanded again — the same reason the tag chips
    // below keep their navigation.
    if (target.closest("summary")) return;
    const row = target.closest(PANEL_ROW);
    if (!row) return;
    const link = target.closest("a[href]");
    // The title link IS the row click (§5.5: clicking a row opens the panel,
    // Expand navigates). A tag chip is its own filter link and keeps it.
    if (link && !link.classList.contains("task-title")) return;
    event.preventDefault();
    openPanel(row.dataset.taskId);
  }

  function handlePanelKeydown(event) {
    // `desiredTaskId` as well as the visible one: Escape during an open is a
    // cancel, and leaving that open to land afterwards would reopen a panel
    // the operator has just dismissed.
    if (event.key !== "Escape" || !(selectedTaskId || desiredTaskId)) return;
    closePanel();
  }

  function handlePanelPopstate() {
    // Back and forward walk the selection without a reload: the URL is the
    // state, so whatever it names now is what the panel shows.
    //
    // Compared against the INTENT, not against what is on screen. Forward onto
    // a selection still displayed — Back to A and straight Forward to B before
    // A has answered — leaves the screen already correct but an open for A
    // running under it, and a handler that returned early there would let A's
    // response land beneath B's URL. Re-entering the transition supersedes it,
    // which is the whole job here; the generation guard settles the rest.
    const taskId = selectionIn(window.location.href);
    if (taskId === desiredTaskId) return;
    if (taskId) openPanel(taskId, { push: false });
    else closePanel({ push: false });
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
    // The panel contract, on a row no template rendered. The alias URL is the
    // only one the browser may build for an arbitrary id, and it is also the
    // right one here: the guard above means no filter is active, so there is
    // no board scope for this panel to carry (the same reasoning as the bare
    // detail link below). The ~800ms reconcile replaces this row with the
    // server's, and its `data-panel-url` with the server's too.
    row.dataset.panelUrl = aliasPanelUrl(taskId);
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
  // The server already rendered the panel for whatever `?selected=` named, so
  // the client starts from the URL rather than from an empty selection — an
  // Escape on a deep-linked panel has to close the panel that is on screen.
  selectedTaskId = selectionIn(window.location.href);
  desiredTaskId = selectedTaskId;
  document.addEventListener("click", handlePanelClick);
  document.addEventListener("keydown", handlePanelKeydown);
  window.addEventListener("popstate", handlePanelPopstate);

  // The stream opens once every DEFERRED SCRIPT on the page has run, not at the
  // end of this one.
  //
  // The graph page loads this file, then a ~400KB Cytoscape bundle, then
  // `graph.js` — and `graph.js` is what subscribes below, for the "graph
  // changed" pill. A stream opened here would consume a matching `task.updated`
  // (and record its id in `seenEvents`, so a retry is deduplicated away) while
  // the browser was still fetching the library, with nothing to replay it to
  // when the subscriber finally arrives: the pill would never appear for an
  // event that really did land, and this page has no reconcile to cover for it.
  //
  // Deferred scripts all execute before `DOMContentLoaded`, which is exactly
  // the guarantee "after every subscriber has registered" needs — so the
  // browser's own ordering does the work, with no replay buffer to bound.
  //
  // `"interactive"` is the state a DEFERRED script runs in, not `"loading"`:
  // the parser sets it before working through the deferred list, and it stays
  // there until the load event. So both are "not ready yet", and only
  // `"complete"` means DOMContentLoaded is certainly past. `load` is the
  // backstop for the one entry neither covers — a file injected between the two
  // events, whose DOMContentLoaded will never come again.
  let streamStarted = false;
  function startStream() {
    if (streamStarted) return;
    streamStarted = true;
    connect();
  }

  if (document.readyState === "complete") {
    startStream();
  } else {
    document.addEventListener("DOMContentLoaded", startStream);
    window.addEventListener("load", startStream);
  }

  // The seam the graph page's canvas opens the panel through (D9: ONE panel
  // implementation for rows and nodes). A Cytoscape node is drawn on a canvas
  // and has no DOM row to click, so it cannot reach the delegated handler
  // above — but everything behind that handler, the URL push included, is the
  // same code either way. Published last, so a page that loads `graph.js`
  // after this file finds it ready.
  window.LithosLens = window.LithosLens || {};
  window.LithosLens.panel = {
    open: openPanel,
    close: closePanel,
    supersedePending: supersedePendingOpen,
    onChange: function (subscriber) { panelSubscribers.push(subscriber); }
  };
  window.LithosLens.events = {
    subscribe: function (subscriber) { eventSubscribers.push(subscriber); }
  };
})();
