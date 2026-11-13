/* quoin workflow dashboard — vanilla JS SPA, no CDN, no build step */
/* Polling: conditional polling via ETag/If-None-Match; full re-fetch skipped on 304. */

(function () {
  'use strict';

  // ---------------------------------------------------------------------------
  // T-04: normalizePhase() — port of _PHASE_TO_NODE from status_graph.py
  // Keys are raw backend `phase` field values produced by detect_phase().
  // ---------------------------------------------------------------------------
  var PHASE_TO_NODE = {
    'discover':        'discover',
    'architecture':    'architect',
    'planning':        'thorough_plan',
    'plan-gated':      'thorough_plan',
    'implement':       'implement',
    'implement-gated': 'implement',
    'review':          'review',
    'review-gated':    'review',
    'committed':       'end_of_task',
    'done':            'end_of_task',
  };

  function normalizePhase(phase) {
    return PHASE_TO_NODE[phase] || 'discover';
  }

  // ---------------------------------------------------------------------------
  // T-04: 6 canonical pipeline nodes (must match CSS class names)
  // ---------------------------------------------------------------------------
  var PIPELINE_NODES = ['discover', 'architect', 'thorough_plan', 'implement', 'review', 'end_of_task'];

  // Human-friendly display labels for pipeline rail nodes
  var NODE_DISPLAY = {
    'discover':      'discover',
    'architect':     'architect',
    'thorough_plan': 'Plan',
    'implement':     'implement',
    'review':        'review',
    'end_of_task':   'Done',
  };

  // ---------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------
  var state = {
    includeFinalized: false,
    selectedTask: null,
    pollInterval: null,
    tasks: [],
    activeTask: null,
    searchQuery: '',
    detailLoaded: false,       // T-07: true after first successful detail render
    lastDetail: null,          // T-07/T-08: cached detail payload for tab re-renders
    selectedStage: null,       // T-08: selected stage tab (stage n value)
    lastCardFingerprint: null, // T-07b: skip DOM mutation when structure unchanged on poll
    etags: {},                 // IVG-76: ETag cache keyed by request URL
    spend: null,               // T-06: latest /api/spend snapshot
  };

  // ---------------------------------------------------------------------------
  // API helpers
  // ---------------------------------------------------------------------------

  function tasksUrl(includeFinalized) {
    return '/api/tasks?include_finalized=' + (includeFinalized ? 'true' : 'false');
  }

  function taskDetailUrl(name) {
    return '/api/tasks/' + encodeURIComponent(name);
  }

  function fetchJSON(url, cb) {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', url);
    // IVG-76: send cached ETag as If-None-Match for conditional polling
    if (state.etags[url]) {
      xhr.setRequestHeader('If-None-Match', state.etags[url]);
    }
    xhr.onload = function () {
      // IVG-76: 304 Not Modified — resource unchanged; skip parse and DOM update
      if (xhr.status === 304) {
        cb(null, null);
        return;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        // IVG-76: store ETag from response for next poll
        var et = xhr.getResponseHeader('ETag');
        if (et) { state.etags[url] = et; }
        try {
          cb(null, JSON.parse(xhr.responseText));
        } catch (e) {
          cb(new Error('JSON parse error: ' + e.message));
        }
      } else {
        cb(new Error('HTTP ' + xhr.status));
      }
    };
    xhr.onerror = function () { cb(new Error('network error')); };
    xhr.send();
  }

  // ---------------------------------------------------------------------------
  // HTML escape
  // ---------------------------------------------------------------------------

  function escHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // ---------------------------------------------------------------------------
  // T-06: Relative time
  // Note: server emits local-time ISO strings (no tz suffix, from
  // datetime.fromtimestamp().isoformat()). new Date(iso) parses as local time
  // in browsers — correct match. If server gains tz-aware strings later,
  // revisit this assumption to avoid silent skew.
  // ---------------------------------------------------------------------------

  function relativeTime(iso) {
    if (!iso) return '—';
    try {
      var now = Date.now();
      var then = new Date(iso).getTime();
      if (isNaN(then)) return iso;
      var diffMs = now - then;
      if (diffMs < 0) diffMs = 0;
      var s = Math.floor(diffMs / 1000);
      if (s < 60) return 'just now';
      var m = Math.floor(s / 60);
      if (m < 60) return m + 'm ago';
      var h = Math.floor(m / 60);
      if (h < 24) return h + 'h ago';
      var d = Math.floor(h / 24);
      if (d < 30) return d + 'd ago';
      var mo = Math.floor(d / 30);
      return mo + 'mo ago';
    } catch (e) {
      return iso;
    }
  }

  function fmtDate(iso) {
    if (!iso) return '—';
    try {
      return new Date(iso).toLocaleString();
    } catch (e) {
      return iso;
    }
  }

  // ---------------------------------------------------------------------------
  // T-04: Phase chip helper (for top-level task/detail objects)
  // phase      = raw backend key (for normalizePhase → CSS class)
  // phaseLabel = display string (task.phase_label or detail.phase_label)
  // ---------------------------------------------------------------------------

  function phaseChip(phase, phaseLabel) {
    var node = normalizePhase(phase);
    var cls = 'phase-chip phase-' + node;
    return '<span class="' + cls + '">' + escHtml(phaseLabel || phase || '—') + '</span>';
  }

  // T-04: Stage pill — stage objects have only `phase` (raw key), no phase_label
  function stagePill(stPhase) {
    var node = normalizePhase(stPhase);
    var cls = 'phase-chip phase-' + node;
    return '<span class="' + cls + '">' + escHtml(stPhase || '—') + '</span>';
  }

  // ---------------------------------------------------------------------------
  // T-06: Grammar helpers
  // ---------------------------------------------------------------------------

  function pluralEvents(n) {
    return n === 1 ? '1 event' : n + ' events';
  }

  function pluralSessions(n) {
    return n === 1 ? '1 session' : n + ' sessions';
  }

  // ---------------------------------------------------------------------------
  // T-03: Task card rendering (replaces old 4-col table)
  // ---------------------------------------------------------------------------

  function renderCards(data, silent) {
    state.tasks = data.tasks || [];
    state.activeTask = data.active_task || null;

    var container = document.getElementById('task-card-list');
    if (!container) return;

    // T-06: filter by search query
    var query = state.searchQuery.toLowerCase().trim();
    var visible = state.tasks.filter(function (t) {
      return !query || t.name.toLowerCase().indexOf(query) !== -1;
    });

    if (state.tasks.length === 0) {
      state.lastCardFingerprint = null;
      container.innerHTML = '<p class="empty-msg">No tasks found under .workflow_artifacts/</p>';
      return;
    }

    if (visible.length === 0) {
      state.lastCardFingerprint = null;
      container.innerHTML = '<p class="empty-msg">No tasks match "' + escHtml(query) + '"</p>';
      return;
    }

    // T-07b: structural fingerprint — excludes relative time (changes every ~60s).
    // If structure is unchanged on a silent poll, only update time spans in-place.
    var fingerprint = query + '|' + JSON.stringify(visible.map(function (t) {
      return [t.name, t.phase, t.phase_label, t.finalized, t.stage,
              t.cost && t.cost.total, t.name === state.selectedTask];
    }));

    if (silent && fingerprint === state.lastCardFingerprint) {
      // Nothing structural changed — update only the relative-time spans in-place
      var existingCards = container.querySelectorAll('.task-card[data-name]');
      for (var ei = 0; ei < existingCards.length; ei++) {
        var eName = existingCards[ei].getAttribute('data-name');
        for (var eti = 0; eti < state.tasks.length; eti++) {
          if (state.tasks[eti].name === eName) {
            var metaEl = existingCards[ei].querySelector('.task-card-meta');
            if (metaEl) metaEl.textContent = relativeTime(state.tasks[eti].last_activity);
            break;
          }
        }
      }
      return;
    }

    state.lastCardFingerprint = fingerprint;

    // T-07: save scroll position before wiping innerHTML
    var _savedScroll = container.scrollTop;

    var html = '';
    for (var i = 0; i < visible.length; i++) {
      var t = visible[i];
      var isFinalized = t.finalized || false;
      var isSelected = t.name === state.selectedTask;

      // T-07b: card-entering (animation) only on user-triggered renders, not polls
      var cardCls = 'task-card' +
        (silent ? '' : ' card-entering') +
        (isFinalized ? ' finalized-card' : '') +
        (isSelected ? ' selected-card' : '');

      // T-03: cost badge — show USD or tokens when available, fall back to row count
      // MINOR-5/D-8 (stage 4): usd gate relaxed to usd!=null (drop >0) so a
      // genuine resolved $0.00 renders as $0.00, not a row-count fallback.
      var costBadgeText = null;
      if (t.cost) {
        if (t.cost.mode === 'usd' && t.cost.usd != null) {
          costBadgeText = '$' + t.cost.usd.toFixed(2);
        } else if (t.cost.mode === 'tokens' && t.cost.tokens != null && t.cost.tokens > 0) {
          costBadgeText = (t.cost.tokens / 1e6).toFixed(1) + 'M tok';
        } else if (t.cost.total != null) {
          costBadgeText = pluralEvents(t.cost.total);
        }
      }
      // Stage 4 (MAJOR-R1): surface the partial signal at the render surface —
      // mirrors the existing spend-monitor (partial) idiom (line ~670).
      var partialBadge = (t.cost && t.cost.partial)
        ? ' <span class="spend-unattrib">(partial)</span>'
        : '';
      var evtBadge = costBadgeText != null
        ? '<span class="cost-badge">' + escHtml(costBadgeText) + '</span>' + partialBadge
        : '';

      // T-03: active stage label (multi-stage only)
      var stageHtml = '';
      if (t.is_multi_stage && t.stage != null) {
        stageHtml = '<span class="task-card-stage">stage ' + escHtml(String(t.stage)) + '</span>';
      }

      // T-04: phase pill — CSS class from normalizePhase(task.phase), display from task.phase_label
      var chipHtml = phaseChip(t.phase, t.phase_label);

      // T-06: relative time
      var timeHtml = '<span class="task-card-meta">' + escHtml(relativeTime(t.last_activity)) + '</span>';

      // T-06: staggered animation via CSS custom property --i
      html += '<div class="' + cardCls + '" role="listitem" data-name="' + escHtml(t.name) + '"' +
        ' style="--i:' + i + '" tabindex="0">' +
        '<div class="task-card-name" title="' + escHtml(t.name) + '">' + escHtml(t.name) + '</div>' +
        '<div class="task-card-row">' +
          chipHtml +
          stageHtml +
          evtBadge +
          timeHtml +
        '</div>' +
        '</div>';
    }

    container.innerHTML = html;
    // T-07: restore scroll so poll updates don't jump to top
    container.scrollTop = _savedScroll;

    // Attach click + keyboard handlers
    var cards = container.querySelectorAll('.task-card');
    for (var j = 0; j < cards.length; j++) {
      cards[j].addEventListener('click', onCardClick);
      cards[j].addEventListener('keydown', onCardKeydown);
    }
  }

  function onCardClick(e) {
    var card = e.currentTarget;
    var name = card.getAttribute('data-name');
    if (!name) return;
    state.selectedTask = name;
    state.selectedStage = null;       // T-08: reset tab selection on task switch
    state.lastCardFingerprint = null; // T-07b: force re-render to update selected-card highlight
    refreshTaskList(false);           // non-silent — animate, update selection highlight
    loadTaskDetail(name, false);      // T-07: non-silent — show spinner, reset scroll
  }

  function onCardKeydown(e) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onCardClick(e);
    }
  }

  // ---------------------------------------------------------------------------
  // T-05: Detail pane rendering
  // ---------------------------------------------------------------------------

  function renderDetailPane(detail) {
    var pane = document.getElementById('detail-pane');
    if (!pane) return;

    var html = '';

    // T-05: Hero — large task name + phase pill + dates
    html += '<div class="detail-hero">';
    html += '<div class="detail-hero-name">' + escHtml(detail.name || '—') + '</div>';
    html += '<div class="detail-hero-row">' +
      phaseChip(detail.phase, detail.phase_label) +
    '</div>';
    var firstAct = detail.dates && detail.dates.first_activity;
    var lastAct  = detail.dates && detail.dates.last_activity;
    html += '<div class="detail-hero-dates">' +
      'First: ' + escHtml(fmtDate(firstAct)) +
      ' &nbsp;·&nbsp; Last: ' + escHtml(fmtDate(lastAct)) +
    '</div>';
    html += '</div>'; // end .detail-hero

    // T-05: Stats grid — sessions (UUID-deduped), critic rounds, review rounds
    var ledgerRows = detail.ledger_rows || [];
    var sessionCount;
    var sessionLabel;
    if (ledgerRows.length > 0) {
      // True session count: unique UUIDs across ledger rows
      var uuidSet = {};
      for (var ri = 0; ri < ledgerRows.length; ri++) {
        var uuid = ledgerRows[ri].uuid;
        if (uuid) uuidSet[uuid] = true;
      }
      sessionCount = Object.keys(uuidSet).length;
      sessionLabel = pluralSessions(sessionCount);
    } else {
      // Fallback: use cost.total row count, label as "events"
      var totalRows = (detail.cost && detail.cost.total != null) ? detail.cost.total : 0;
      sessionCount = totalRows;
      sessionLabel = pluralEvents(totalRows);
    }

    var criticRounds = detail.critic_rounds != null ? detail.critic_rounds : '—';
    var reviewRounds = detail.review_rounds != null ? detail.review_rounds : '—';

    // Cost summary for stats-grid: show USD or tokens when available
    // MINOR-5/D-8 (stage 4): usd gate relaxed to usd!=null (drop >0) — a
    // genuine resolved $0.00 renders as $0.00.
    var costStatValue = '—';
    var costStatLabel = 'cost';
    if (detail.cost) {
      if (detail.cost.mode === 'usd' && detail.cost.usd != null) {
        costStatValue = '$' + detail.cost.usd.toFixed(2);
        costStatLabel = 'cost (USD)';
      } else if (detail.cost.mode === 'tokens' && detail.cost.tokens != null && detail.cost.tokens > 0) {
        costStatValue = (detail.cost.tokens / 1e6).toFixed(1) + 'M';
        costStatLabel = 'tokens';
      } else if (detail.cost.total != null) {
        costStatValue = String(detail.cost.total);
        costStatLabel = 'ledger rows';
      }
    }
    // Stage 4 (MAJOR-R1): partial marker, mirroring the spend-monitor idiom.
    var costStatPartialHtml = (detail.cost && detail.cost.partial)
      ? ' <span class="spend-unattrib">(partial)</span>'
      : '';

    html += '<div class="stats-grid">' +
      '<div class="stat-cell"><div class="stat-value">' + escHtml(String(sessionCount)) + '</div>' +
        '<div class="stat-label">' + (ledgerRows.length > 0 ? 'sessions' : 'events') + '</div></div>' +
      '<div class="stat-cell"><div class="stat-value">' + escHtml(String(criticRounds)) + '</div>' +
        '<div class="stat-label">critic rounds</div></div>' +
      '<div class="stat-cell"><div class="stat-value">' + escHtml(String(reviewRounds)) + '</div>' +
        '<div class="stat-label">review rounds</div></div>' +
      '<div class="stat-cell"><div class="stat-value">' + escHtml(costStatValue) + costStatPartialHtml + '</div>' +
        '<div class="stat-label">' + escHtml(costStatLabel) + '</div></div>' +
    '</div>';

    // T-04 / T-05: Pipeline graph — highlight node via normalizePhase(detail.phase)
    html += '<div class="detail-section"><h3>Pipeline</h3><div class="phase-graph">';
    var activeNode = normalizePhase(detail.phase);
    for (var pi = 0; pi < PIPELINE_NODES.length; pi++) {
      var node = PIPELINE_NODES[pi];
      var isCurrent = node === activeNode;
      var displayLabel = NODE_DISPLAY[node] || node;
      html += '<div class="phase-graph-item">' +
        '<div class="phase-graph-chip phase-' + node + (isCurrent ? ' current' : '') + '">' +
          escHtml(displayLabel) +
        '</div>' +
      '</div>';
      if (pi < PIPELINE_NODES.length - 1) {
        html += '<span class="phase-graph-arrow">›</span>';
      }
    }
    html += '</div></div>';

    // T-08: Stage tabs — clickable navigation; replaces flat stage list
    var isMultiStage = detail.stages && detail.stages.length > 0;
    if (isMultiStage) {
      // Auto-select first stage when no tab is selected yet
      if (state.selectedStage === null) {
        state.selectedStage = detail.stages[0].n != null ? detail.stages[0].n : 1;
      }

      html += '<div class="detail-section"><h3>Stages</h3>';
      html += '<div class="stage-tabs">';
      for (var si = 0; si < detail.stages.length; si++) {
        var st = detail.stages[si];
        var stNum = st.n != null ? st.n : si + 1;
        var isActive = stNum === state.selectedStage;
        html += '<button class="stage-tab' + (isActive ? ' active' : '') + '"' +
          ' data-stage-n="' + escHtml(String(stNum)) + '">' +
          'Stage ' + escHtml(String(stNum)) + ': ' + escHtml(st.name || '—') +
          '</button>';
      }
      html += '</div>'; // end .stage-tabs

      // Find the selected stage and render its detail panel
      var selSt = null;
      for (var ssi = 0; ssi < detail.stages.length; ssi++) {
        var ssn = detail.stages[ssi].n != null ? detail.stages[ssi].n : ssi + 1;
        if (ssn === state.selectedStage) { selSt = detail.stages[ssi]; break; }
      }

      if (selSt !== null) {
        html += '<div class="stage-detail-panel">';
        html += '<div class="stage-detail-header">' +
          '<strong>' + escHtml(selSt.name || 'Stage ' + state.selectedStage) + '</strong>' +
          '&nbsp;&nbsp;' + stagePill(selSt.phase) +
          '</div>';

        // Cost table (all stages combined — per-stage breakdown requires schema change)
        var sCost = detail.cost;
        if (sCost && sCost.by_phase && Object.keys(sCost.by_phase).length > 0) {
          // Compute attributed spend for this task's phase entries (USD mode only)
          var sAttribTotal = null;
          if (sCost.mode === 'usd') {
            var sByPhaseVals = sCost.by_phase;
            var sAttrib = 0;
            var sBpKeys = Object.keys(sByPhaseVals);
            for (var sbk = 0; sbk < sBpKeys.length; sbk++) {
              var sbv = sByPhaseVals[sBpKeys[sbk]];
              sAttrib += (sbv && sbv.usd != null) ? sbv.usd : 0;
            }
            sAttribTotal = sAttrib;
          }
          var sCaption = (sCost.mode === 'counts')
            ? 'Activity — all stages combined (no token/USD data)'
            : 'Cost breakdown — all stages combined';
          if (sAttribTotal !== null) {
            sCaption = 'By phase (of $' + sAttribTotal.toFixed(2) + ' attributed spend) — all stages combined';
          }
          html += '<p class="stage-cost-note">' + sCaption + '</p>';
          var sColHdr = (sCost.mode === 'counts') ? 'Events' : 'Cost';
          html += '<table class="by-phase-table"><thead><tr><th>Phase</th><th>' + sColHdr + '</th></tr></thead><tbody>';
          var sByPhase = sCost.by_phase;
          var sPhases = Object.keys(sByPhase).sort();
          for (var spi = 0; spi < sPhases.length; spi++) {
            var sph = sPhases[spi];
            var sval = sByPhase[sph];
            var scell;
            if (sCost.mode === 'counts') {
              scell = (typeof sval === 'number' ? sval : '?') + ' events';
            } else if (sCost.mode === 'usd') {
              scell = '$' + ((sval && sval.usd != null) ? sval.usd.toFixed(2) : '?');
            } else if (sCost.mode === 'tokens') {
              scell = ((sval && sval.tokens != null) ? (sval.tokens / 1e6).toFixed(2) : '?') + 'M tokens';
            } else {
              scell = JSON.stringify(sval);
            }
            html += '<tr><td>' + escHtml(sph) + '</td><td>' + escHtml(scell) + '</td></tr>';
          }
          html += '</tbody></table>';
        }

        html += '</div>'; // end .stage-detail-panel
      }

      html += '</div>'; // end .detail-section
    }

    // T-05: Cost by phase breakdown — shown for single-stage tasks only
    // (multi-stage tasks show cost inside the stage detail panel above)
    var cost = detail.cost;
    if (!isMultiStage && cost && cost.by_phase && Object.keys(cost.by_phase).length > 0) {
      var ssHdr = (cost.mode === 'counts') ? 'Activity by phase' : 'Cost by phase';
      html += '<div class="detail-section"><h3>' + ssHdr + '</h3>';
      if (cost.mode === 'counts') {
        html += '<p class="stage-cost-note">Activity (no token/USD data available)</p>';
      } else if (cost.mode === 'usd') {
        // Compute attributed spend as sum of by_phase usd values for this task
        var ssAttrib = 0;
        var ssBpKeys = Object.keys(cost.by_phase);
        for (var ssbk = 0; ssbk < ssBpKeys.length; ssbk++) {
          var ssbv = cost.by_phase[ssBpKeys[ssbk]];
          ssAttrib += (ssbv && ssbv.usd != null) ? ssbv.usd : 0;
        }
        // Stage 4 (MAJOR-R1): flag the attributed-spend line as incomplete
        // when the task carries an unresolvable slice.
        var ssPartialHtml = (cost.partial)
          ? ' <span class="spend-unattrib">(partial)</span>'
          : '';
        html += '<p class="stage-cost-note">By phase (of $' + ssAttrib.toFixed(2) + ' attributed spend)' + ssPartialHtml + ':</p>';
      }
      var ssColHdr = (cost.mode === 'counts') ? 'Events' : 'Cost';
      html += '<table class="by-phase-table"><thead><tr><th>Phase</th><th>' + ssColHdr + '</th></tr></thead><tbody>';
      var byPhase = cost.by_phase;
      var phases = Object.keys(byPhase).sort();
      for (var bpi = 0; bpi < phases.length; bpi++) {
        var bph = phases[bpi];
        var val = byPhase[bph];
        var cellText;
        if (cost.mode === 'counts') {
          // val is plain int (flat shape in counts mode)
          cellText = (typeof val === 'number' ? val : '?') + ' events';
        } else if (cost.mode === 'usd') {
          // val is {"usd": float} (nested shape)
          var usdVal = (val && val.usd != null) ? val.usd.toFixed(2) : '?';
          cellText = '$' + usdVal;
        } else if (cost.mode === 'tokens') {
          // val is {"tokens": int} (nested shape)
          var tokVal = (val && val.tokens != null) ? (val.tokens / 1e6).toFixed(2) : '?';
          cellText = tokVal + 'M tokens';
        } else {
          cellText = JSON.stringify(val);
        }
        html += '<tr><td>' + escHtml(bph) + '</td><td>' + escHtml(cellText) + '</td></tr>';
      }
      html += '</tbody></table></div>';
    }

    // T-05: Sessions / ledger rows table — uses model_or_effort (NOT model); P6 fix: not duplicated
    if (ledgerRows.length > 0) {
      html += '<div class="detail-section"><h3>Sessions (' + escHtml(pluralSessions(sessionCount)) + ', ' +
        ledgerRows.length + ' rows)</h3>';
      html += '<table class="sessions-table"><thead><tr>' +
        '<th>Date</th><th>Phase</th><th>Model/effort</th><th>Note</th>' +
      '</tr></thead><tbody>';
      var maxRows = Math.min(ledgerRows.length, 20);
      for (var li = 0; li < maxRows; li++) {
        var row = ledgerRows[li];
        // model_or_effort is the correct API field (not model)
        html += '<tr>' +
          '<td>' + escHtml(row.date || '—') + '</td>' +
          '<td>' + escHtml(row.phase || '—') + '</td>' +
          '<td class="mono">' + escHtml(row.model_or_effort || '—') + '</td>' +
          '<td>' + escHtml(row.note || '') + '</td>' +
        '</tr>';
      }
      if (ledgerRows.length > 20) {
        html += '<tr><td colspan="4" style="color:var(--text-muted);font-style:italic">… and ' +
          (ledgerRows.length - 20) + ' more rows</td></tr>';
      }
      html += '</tbody></table></div>';
    }

    pane.innerHTML = html;
  }

  // ---------------------------------------------------------------------------
  // Data loading + polling (conditional polling via ETag/If-None-Match; full re-fetch skipped on 304)
  // ---------------------------------------------------------------------------

  function refreshTaskList(silent) {
    fetchJSON(tasksUrl(state.includeFinalized), function (err, data) {
      if (err) {
        console.warn('dashboard: task list fetch error:', err.message);
        var container = document.getElementById('task-card-list');
        if (container && !state.tasks.length) {
          container.innerHTML = '<p class="error-msg">Error loading tasks: ' +
            escHtml(err.message) + '</p>';
        }
        return;
      }
      // IVG-76: data === null means 304 Not Modified — skip all DOM work,
      // preserve existing scroll and card state exactly as-is.
      if (data === null) { return; }
      renderCards(data, silent);
    });
  }

  // T-07: silent=true on poll (no spinner, scroll preserved); false on explicit selection
  function loadTaskDetail(name, silent) {
    var pane = document.getElementById('detail-pane');
    // T-07: save scroll before fetch (innerHTML not changed yet on silent path)
    var _paneScroll = (silent && pane) ? pane.scrollTop : 0;
    if (!silent && pane) pane.innerHTML = '<p class="loading">Loading…</p>';

    fetchJSON(taskDetailUrl(name), function (err, data) {
      if (err) {
        if (pane) pane.innerHTML = '<p class="error-msg">Error loading task: ' +
          escHtml(err.message) + '</p>';
        return;
      }
      // IVG-76: data === null means 304 Not Modified — skip all DOM work,
      // preserve existing scroll and detail pane state exactly as-is.
      if (data === null) { return; }
      state.lastDetail = data;        // T-08: cache for tab re-renders
      renderDetailPane(data);
      // T-08: reattach stage tab click handlers after every innerHTML replacement
      _attachStageTabHandlers();
      // T-07: restore scroll position on silent poll updates
      if (silent && pane) pane.scrollTop = _paneScroll;
      state.detailLoaded = true;
    });
  }

  // T-08: attach click handlers on .stage-tab buttons inside the detail pane
  function _attachStageTabHandlers() {
    var pane = document.getElementById('detail-pane');
    if (!pane) return;
    var tabs = pane.querySelectorAll('.stage-tab');
    for (var ti = 0; ti < tabs.length; ti++) {
      tabs[ti].addEventListener('click', onStageTabClick);
    }
  }

  // T-08: handle stage tab click — update selected stage and re-render in place
  function onStageTabClick(e) {
    var btn = e.currentTarget;
    var n = parseInt(btn.getAttribute('data-stage-n'), 10);
    if (!isNaN(n) && state.lastDetail) {
      state.selectedStage = n;
      var pane = document.getElementById('detail-pane');
      var _scroll = pane ? pane.scrollTop : 0;
      renderDetailPane(state.lastDetail);
      _attachStageTabHandlers();
      // preserve scroll when switching tabs
      if (pane) pane.scrollTop = _scroll;
    }
  }

  // ---------------------------------------------------------------------------
  // T-06: Today's spend section
  // ---------------------------------------------------------------------------

  function fetchSpend() {
    fetchJSON('/api/spend', function (err, data) {
      if (err || data === null) return;  // 304 or error — keep existing state
      state.spend = data;
      renderSpendSection();
    });
  }

  function renderSpendSection() {
    var el = document.getElementById('spend-section');
    if (!el) return;
    var snap = state.spend;
    if (!snap) { el.innerHTML = ''; return; }

    var usd = typeof snap.today_usd === 'number' ? snap.today_usd : null;
    if (usd === null) { el.innerHTML = ''; return; }

    // Total line
    var html = 'Today: <span class="spend-today">$' + usd.toFixed(2) + '</span>';
    if (snap.by_phase_partial || snap.by_task_partial) {
      html += ' <span class="spend-unattrib">(partial)</span>';
    }

    // By-phase breakdown table
    var byPhase = snap.by_phase;
    if (byPhase && Object.keys(byPhase).length > 0) {
      var phases = Object.keys(byPhase).sort();
      // Use server-computed attributed/unattributed if available; fall back to local sum
      var attributed = typeof snap.attributed_usd === 'number'
        ? snap.attributed_usd
        : phases.reduce(function (acc, ph) {
            var v = byPhase[ph];
            return acc + (typeof v === 'number' ? v : 0);
          }, 0);
      var unattributed = typeof snap.unattributed_usd === 'number'
        ? snap.unattributed_usd
        : Math.max(0, usd - attributed);

      html += '<div class="spend-phase-breakdown">';
      html += '<div class="spend-phase-label">By phase:</div>';
      html += '<table class="spend-phase-table"><tbody>';
      for (var spi = 0; spi < phases.length; spi++) {
        var sph = phases[spi];
        var sval = byPhase[sph];
        var sstr = typeof sval === 'number' ? '$' + sval.toFixed(2) : '?';
        html += '<tr><td>' + escHtml(sph) + '</td><td>' + escHtml(sstr) + '</td></tr>';
      }
      html += '</tbody></table>';
      if (unattributed > 0.001) {
        html += '<div class="spend-unattrib-line">Unattributed: ' +
          '<span>$' + unattributed.toFixed(2) + '</span></div>';
      }
      html += '</div>';
    }

    el.innerHTML = html;
  }

  function startPolling() {
    if (state.pollInterval) clearInterval(state.pollInterval);
    state.pollInterval = setInterval(function () {
      refreshTaskList(true);  // T-07b: silent — no animation, no DOM mutation if unchanged
      if (state.selectedTask) {
        loadTaskDetail(state.selectedTask, true);  // T-07: silent — preserve scroll
      }
      fetchSpend();           // T-06: refresh spend display
    }, 3000);
  }

  // ---------------------------------------------------------------------------
  // Init
  // ---------------------------------------------------------------------------

  function init() {
    // Finalized toggle
    var toggle = document.getElementById('show-finalized');
    if (toggle) {
      toggle.addEventListener('change', function () {
        state.includeFinalized = toggle.checked;
        state.lastCardFingerprint = null; // T-07b: force full re-render on filter change
        refreshTaskList(false);           // user-triggered — show animation
      });
    }

    // T-06: Search input
    var search = document.getElementById('task-search');
    if (search) {
      search.addEventListener('input', function () {
        state.searchQuery = search.value;
        // Re-render current tasks with filter applied (no new fetch needed)
        state.lastCardFingerprint = null; // T-07b: search change forces full re-render
        renderCards({ tasks: state.tasks, active_task: state.activeTask }, false);
      });
    }

    // Initial load (non-silent — animate cards on first render)
    refreshTaskList(false);

    // T-06: initial spend load
    fetchSpend();

    // Start polling (~3s, unconditional — D-10)
    startPolling();
  }

  // Run after DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
