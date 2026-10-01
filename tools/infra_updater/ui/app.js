// Frontend for the Cluster Toolkit Infrastructure Updater dashboard.
'use strict';

// ===================================================================== state

const state = {
  packages: [],
  candidates: [],
  rules: [],
  instances: [],      // every package's blueprint instances (the same objects as package.blueprints)
  byPkg: {},          // package_id -> blueprint instances
  pkgMap: {},         // package_id -> package
  candByPkg: {},      // package_id -> candidates (newest first)
  config: { trigger_prefix: 'PR-test-', snooze_days_default: 30 },
  isRunning: false,
  etag: null,
};

// Status vocabulary served by the backend (statuses.py); set from the first /api/state response.
let PS = {}, CS = {}, TS = {};
let ACTIVE = new Set(), POLICY = new Set(), TEST_FAILED = new Set(), TEST_TERMINAL = new Set();
let BADGE_CLASS = {};

function setStatuses(st) {
  PS = st.package; CS = st.candidate; TS = st.test;
  ACTIVE = new Set(st.active_candidate);
  POLICY = new Set(st.policy);
  TEST_FAILED = new Set(st.test_failed);
  TEST_TERMINAL = new Set(st.test_terminal);
  BADGE_CLASS = {
    [PS.UPDATE_FOUND]: 'badge-blue', [PS.REGISTERED]: 'badge-blue',
    [PS.READY_FOR_REVIEW]: 'badge-green', [PS.UP_TO_DATE]: 'badge-green', [CS.MERGED]: 'badge-green',
    [PS.TESTING]: 'badge-amber', [PS.SNOOZED]: 'badge-amber', [PS.OBSOLETE]: 'badge-amber',
    [PS.TEST_FAILED]: 'badge-red', [PS.BLOCKED]: 'badge-red', [PS.ERROR]: 'badge-red', [PS.UNREACHABLE]: 'badge-red',
  };
}

let activeModalPackageId = null;
let activeTestModalCandidateId = null;

// =================================================================== helpers

function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

const ICON_PATHS = {
  search: '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>',
  pr: '<circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M13 6h3a2 2 0 0 1 2 2v7"/><line x1="6" y1="9" x2="6" y2="21"/>',
  external: '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>',
  check: '<polyline points="20 6 9 17 4 12"/>',
  xCircle: '<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>',
  refresh: '<polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>',
  clock: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
  ban: '<circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/>',
  pulse: '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
  alert: '<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
};

function icon(name, size = 12, cls = '') {
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"${cls ? ` class="${cls}"` : ''}>${ICON_PATHS[name]}</svg>`;
}

function prNumber(prUrl) {
  return String(prUrl || '').replace(/\/$/, '').split('/').pop();
}

function triggerNameFor(testName) {
  return testName ? `${state.config.trigger_prefix}${testName}` : 'Integration Test';
}

function emptyRow(colspan, text) {
  return `<tr><td colspan="${colspan}" class="empty-cell">${escapeHtml(text)}</td></tr>`;
}

async function postAction(body) {
  const res = await fetch('/api/action', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  let data;
  try {
    data = await res.json();
  } catch (_) {
    data = {};  // non-JSON (e.g. an HTML error page)
  }
  if (!res.ok) {
    const err = new Error(data.error || data.message || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function openModal(id) {
  document.getElementById(id)?.classList.add('active');
  document.body.style.overflow = 'hidden';
}

function closeModal(id) {
  document.getElementById(id)?.classList.remove('active');
  if (id === 'blueprint-modal') activeModalPackageId = null;
  if (id === 'test-results-modal') activeTestModalCandidateId = null;
  if (!document.querySelector('.modal-overlay.active')) document.body.style.overflow = '';
}

// --------------------------------------------------------------- tests

function testCounts(tests) {
  const total = tests.length;
  const passed = tests.filter(t => t.status === TS.SUCCESS).length;
  const failed = tests.filter(t => TEST_FAILED.has(t.status)).length;
  return { total, passed, failed, running: total - passed - failed };
}

function renderTestPill(c, small = false) {
  const tests = c.tests || [];
  if (!tests.length) return '';
  const { total, passed, failed, running } = testCounts(tests);
  let cls, lead, text, tip;
  if (running > 0) {
    cls = 'test-pill-running';
    lead = '<span class="spinner-sm spinner-xs"></span>';
    if (total === 1) text = '1 running';
    else if (small) text = `${running}/${total} run`;
    else text = passed > 0 ? `${passed}/${total} passed (${running} running)` : `${running}/${total} running`;
    tip = `Click to monitor test execution (${running} running, ${passed} passed, ${failed} failed)`;
  } else if (failed > 0) {
    cls = 'test-pill-partial-failed';
    lead = icon('xCircle');
    text = small ? `${passed}/${total} passed` : `${passed}/${total} passed (${failed} failed)`;
    tip = `Click to view test failure logs (${failed} failed, ${passed} passed)`;
  } else {
    cls = 'test-pill-all-passed';
    lead = icon('check');
    text = (small || total === 1) ? `${passed}/${total} passed` : `${passed}/${total} tests passed`;
    tip = `Click to view all ${total} passing Cloud Build test logs`;
  }
  return `
    <button class="btn-test-pill ${cls} ${small ? 'btn-test-pill-sm' : ''}" onclick="openTestResultsModal('${escapeHtml(c.candidate_id)}', event)" title="${escapeHtml(tip)}">
      ${lead}<span>${text}</span>${icon('external', 10, 'pill-trailing-icon')}
    </button>`;
}

// ---------------------------------------------------------- shared bits

function selectionCounts(packageId) {
  const insts = state.byPkg[packageId] || [];
  return { total: insts.length, selected: insts.filter(i => i.enabled !== false).length };
}

function renderBlueprintPill(packageId) {
  const { total, selected } = selectionCounts(packageId);
  if (!total) return '';
  const cls = selected === 0 ? 'pill-none-selected' : (selected < total ? 'pill-partial-selected' : 'pill-all-selected');
  return `
    <button class="btn-blueprint-pill ${cls}" onclick="openBlueprintModal('${escapeHtml(packageId)}')" title="Click to view &amp; select blueprints for PR updates (${selected}/${total} selected)">
      ${icon('file')}<span>${selected}/${total} selected</span>
    </button>`;
}

function renderPrLink(prUrl, { small = false, label = 'View PR' } = {}) {
  if (!prUrl) return '';
  return `
    <a href="${escapeHtml(prUrl)}" target="_blank" rel="noopener" class="btn btn-secondary btn-success-outline ${small ? 'btn-xs' : ''}" title="View Pull Request on GitHub">
      ${small ? '' : icon('pr', 13)}<span>${escapeHtml(label)} #${escapeHtml(prNumber(prUrl))}</span>
    </a>`;
}

function renderUnblockButton(packageId, small = false) {
  return `
    <button class="btn btn-secondary btn-success-outline ${small ? 'btn-xs' : ''}" onclick="triggerUnblock('${escapeHtml(packageId)}')" title="Unblock / Resume updates">
      ${icon('refresh', small ? 11 : 12)}<span>Unblock</span>
    </button>`;
}

function renderPolicyButtons(packageId, version, small = false) {
  const size = small ? 'btn-xs' : '';
  return `
    <button class="btn btn-secondary ${size}" onclick="openSnoozeModal('${escapeHtml(packageId)}', '${escapeHtml(version)}')" title="Snooze updates for this version (default: ${state.config.snooze_days_default} days)">
      ${small ? '' : icon('clock')}<span>Snooze</span>
    </button>
    <button class="btn btn-secondary btn-danger-outline ${size}" onclick="confirmBlockPackage('${escapeHtml(packageId)}', '${escapeHtml(version)}')" title="Block updates for this version until manually unblocked">
      ${small ? '' : icon('ban')}<span>Block</span>
    </button>`;
}

function statusBadge(status, label = null) {
  const spinner = status === CS.TESTING ? '<span class="spinner-sm"></span> ' : '';
  return `<span class="badge ${BADGE_CLASS[status] || 'badge-blue'}">${spinner}${label ?? escapeHtml(status)}</span>`;
}

// Blueprints of a package pinned at different versions (selected blueprints only).
function versionSpread(packageId) {
  const counts = {};
  (state.byPkg[packageId] || [])
    .filter(i => i.enabled !== false && i.current_version)
    .forEach(i => { counts[i.current_version] = (counts[i.current_version] || 0) + 1; });
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  return { outOfSync: entries.length > 1, label: entries.map(([v, n]) => `${n} × ${v}`).join(', ') };
}

function outOfSyncBadge(packageId) {
  const spread = versionSpread(packageId);
  return spread.outOfSync
    ? `<span class="badge badge-amber out-of-sync-badge" title="Blueprints pin different versions: ${escapeHtml(spread.label)}">⚠ out of sync</span>`
    : '';
}

// ================================================================ fetching

function indexState(data) {
  state.packages = data.packages || [];
  state.candidates = data.candidates || [];
  state.rules = data.rules || [];
  state.pkgMap = {};
  state.byPkg = {};
  state.candByPkg = {};
  state.instances = [];
  for (const p of state.packages) {
    const bps = p.blueprints || [];
    bps.forEach(b => { b.package_id = p.package_id; });
    state.pkgMap[p.package_id] = p;
    state.byPkg[p.package_id] = bps;
    state.instances.push(...bps);
  }
  for (const c of state.candidates) {
    (state.candByPkg[c.package_id] = state.candByPkg[c.package_id] || []).push(c);
  }
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function applyState(data) {
  if (data.statuses) setStatuses(data.statuses);
  indexState(data);
  state.isRunning = !!data.is_running;

  const stats = data.stats || {};
  const pending = stats.pending_updates || 0;
  setText('metric-packages', stats.total_packages ?? state.packages.length);
  setText('metric-instances', stats.total_instances ?? state.instances.length);
  setText('metric-rules', stats.total_rules ?? state.rules.length);
  setText('metric-candidates', pending);
  setText('counter-candidates', pending);
  setText('metric-candidates-sub', stats.ready_updates > 0 ? `${stats.ready_updates} ready for review` : 'Qualified & ready for review');
  try {
    localStorage.setItem('infra_updater_stats', JSON.stringify({
      total_packages: stats.total_packages, total_instances: stats.total_instances,
      total_rules: stats.total_rules, pending_updates: pending,
    }));
  } catch (_) { /* storage disabled: the counters just are not restored on reload */ }

  const diffDot = document.getElementById('diff-dot');
  if (diffDot) diffDot.hidden = !data.has_modifications;

  if (data.config) {
    state.config = { ...state.config, ...data.config };
    applySnoozeDefault(state.config.snooze_days_default);
    const cfg = data.config;
    setText('test-modal-project-id', cfg.cloud_build_project_id || cfg.project_id || '-');
    const fork = cfg.is_fork && cfg.fork_owner ? ` ← fork: ${cfg.fork_owner}` : '';
    setText('repo-badge-text', `${cfg.owner}/${cfg.repo_name} (${cfg.base_branch})${fork}`);
  }

  const dot = document.getElementById('status-dot');
  if (dot) dot.className = data.is_running ? 'status-dot running' : 'status-dot';
  setText('status-text', data.is_running ? `Running: ${data.current_action}...`
    : (data.last_status === 'FAILED' ? 'Last Run Failed' : 'System Idle'));

  renderActive();
  if (activeModalPackageId) updateModalSelectionStats(activeModalPackageId);
  if (data.is_running) startLogPolling();
}

const STATE_INTERVAL_MS = 3000;
let stateTimer = null;
let stateInFlight = false;
let statePending = false;
let stateFailures = 0;

async function fetchState() {
  if (stateInFlight) {
    statePending = true;
    return;
  }
  stateInFlight = true;
  try {
    const res = await fetch('/api/state', { headers: state.etag ? { 'If-None-Match': state.etag } : {} });
    if (res.status !== 304) {  // 304: nothing changed since the last render
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      state.etag = res.headers.get('ETag');
      applyState(data);
    }
    stateFailures = 0;
  } catch (err) {
    stateFailures++;
    console.warn('GET /api/state failed:', err);
  } finally {
    stateInFlight = false;
    if (statePending) {
      statePending = false;
      fetchState();
    }
  }
}

// Polls while the tab is visible; backs off exponentially (up to 30s) while the server is unreachable.
async function pollState() {
  clearTimeout(stateTimer);
  if (document.hidden) return;  // resumed by the visibilitychange listener
  await fetchState();
  const delay = stateFailures ? Math.min(30000, STATE_INTERVAL_MS * 2 ** stateFailures) : STATE_INTERVAL_MS;
  clearTimeout(stateTimer);  // a visibility change may have started another chain meanwhile
  stateTimer = setTimeout(pollState, delay);
}

async function fetchDiff() {
  try {
    const res = await fetch('/api/diff');
    if (!res.ok) return;
    renderDiff((await res.json()).diff);
  } catch (err) {
    console.error('Error fetching diff:', err);
  }
}

// =============================================================== rendering

const RENDERERS = {
  'tab-candidates': renderCandidates,
  'tab-packages': renderPackages,
  'tab-instances': renderInstances,
  'tab-rules': renderRules,
  'tab-console': renderDynamicApplyButtons,
};

// Only the visible tab is rendered; switching tabs renders the newly shown one.
function renderActive() {
  if (!PS.UPDATE_FOUND) return;  // no state yet
  const tab = document.querySelector('.tab-panel.active')?.id;
  try {
    RENDERERS[tab]?.();
  } catch (e) {
    console.error(`Error rendering ${tab}:`, e);
  }
  applyBusyState();
}

// While an action runs, disable action buttons and remember which ones; afterwards re-enable only
// those, so buttons that are disabled by design stay disabled.
function applyBusyState() {
  document.querySelectorAll('.btn:not([data-nobusy])').forEach(btn => {
    if (state.isRunning) {
      if (!btn.disabled) { btn.disabled = true; btn.dataset.busy = '1'; }
    } else if (btn.dataset.busy) {
      btn.disabled = false;
      delete btn.dataset.busy;
    }
  });
}

function switchTab(tabId) {
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === tabId));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tabId));
  if (tabId === 'tab-diff') fetchDiff();
  else renderActive();
}

function clearConsole() {
  setText('terminal-body', '');
}

function applySnoozeDefault(days) {
  const select = document.getElementById('snooze-days-select');
  if (!select || select.dataset.defaultDays === String(days)) return;
  if (![...select.options].some(o => o.value === String(days))) {
    select.add(new Option(`${days} Days`, String(days)));
    [...select.options].sort((a, b) => Number(a.value) - Number(b.value)).forEach(o => select.add(o));
  }
  [...select.options].forEach(o => {
    o.textContent = `${o.value} Days${o.value === String(days) ? ' (Default)' : ''}`;
    o.selected = o.value === String(days);
  });
  select.dataset.defaultDays = String(days);
}

function renderDiff(rawDiff) {
  const diffBody = document.getElementById('diff-body');
  if (!rawDiff || !rawDiff.trim()) {
    diffBody.innerHTML = '<span class="text-muted">No uncommitted blueprint modifications in workspace. Apply a candidate update to see its blueprint changes.</span>';
    return;
  }
  diffBody.innerHTML = rawDiff.split('\n').map(line => {
    const escaped = escapeHtml(line);
    if (line.startsWith('+') && !line.startsWith('+++')) return `<span class="diff-line-add">${escaped}</span>`;
    if (line.startsWith('-') && !line.startsWith('---')) return `<span class="diff-line-del">${escaped}</span>`;
    if (/^(@@|diff --git|---|\+\+\+)/.test(line)) return `<span class="diff-line-header">${escaped}</span>`;
    return escaped;
  }).join('\n');
}

// ------------------------------------------------------------ candidates

function candidateView(c) {
  const testing = c.status === CS.TESTING || c.test_status === TS.RUNNING;
  return {
    held: POLICY.has(c.status),
    testing,
    passed: c.test_status === TS.SUCCESS,
    failed: c.test_status === TS.FAILURE || c.status === CS.TEST_FAILED,
    ready: c.status === CS.READY_FOR_REVIEW && !testing,
  };
}

function candidateStatusBadge(c, v) {
  if (v.held) return statusBadge(c.status);
  if (v.testing) return statusBadge(CS.TESTING);
  if (v.passed) return `${statusBadge(CS.READY_FOR_REVIEW)} <span class="badge badge-green badge-xs">✓ TESTS PASSED</span>`;
  if (v.failed) return `${statusBadge(CS.TEST_FAILED, '✗ TEST FAILED')} <span class="badge badge-amber badge-xs">${escapeHtml(c.tests_summary || '')}</span>`;
  return statusBadge(v.ready ? CS.READY_FOR_REVIEW : CS.UPDATE_FOUND);
}

function candidateActions(c, v) {
  const pkgId = c.package_id;
  if (v.held) return renderUnblockButton(pkgId);
  const testPill = renderTestPill(c);
  if (v.testing) {
    return `${renderPrLink(c.pr_url)}${testPill}
      <button class="btn btn-secondary" disabled><span>Tests running…</span></button>`;
  }
  let applyLabel = 'Review &amp; Apply Update';
  if (v.ready) applyLabel = v.passed ? 'Applied &bull; Tests Passed' : (v.failed ? 'Applied &bull; Test Failed' : 'Applied &bull; Ready for Review');
  return `
    ${renderPrLink(c.pr_url)}
    ${testPill}
    ${v.failed ? `
      <button class="btn btn-secondary btn-info-outline" onclick="triggerAction('test', '${escapeHtml(pkgId)}')" title="Re-trigger blueprint tests on Cloud Build">
        ${icon('refresh')}<span>Re-run Tests</span>
      </button>` : ''}
    ${renderPolicyButtons(pkgId, c.version)}
    <button class="btn ${v.ready ? 'btn-secondary' : 'btn-primary'}" onclick="triggerAction('apply', '${escapeHtml(pkgId)}')" ${v.ready ? 'disabled' : ''}>${applyLabel}</button>`;
}

function renderCandidates() {
  const container = document.getElementById('candidates-container');
  const active = state.candidates.filter(c => c.status !== CS.MERGED);
  if (!active.length) {
    container.innerHTML = `
      <div class="empty-state">
        ${icon('search', 32)}
        <div class="empty-title">No pending updates qualified</div>
        <div class="empty-hint">Click "Check for Updates" to scan upstream releases.</div>
      </div>`;
    return;
  }
  container.innerHTML = active.map(c => {
    const v = candidateView(c);
    const pkg = state.pkgMap[c.package_id] || {};
    const { total, selected } = selectionCounts(c.package_id);
    return `
      <div class="candidate-card">
        <div class="candidate-header">
          <div class="candidate-title-group">
            <span class="candidate-name">${escapeHtml(c.package_id)}</span>
            <span class="code-pill pill-blue">${escapeHtml(pkg.name || c.package_id)}</span>
            ${renderBlueprintPill(c.package_id)}
          </div>
          <div class="candidate-action-group">${candidateActions(c, v)}</div>
        </div>

        <div class="version-banner">
          <div class="version-item">
            <span class="version-label">Current:</span>
            <span class="code-pill">${escapeHtml(pkg.current_version || '-')}</span>
            ${outOfSyncBadge(c.package_id)}
          </div>
          <span class="version-arrow">→</span>
          <div class="version-item">
            <span class="version-label">Target:</span>
            <span class="code-pill version-target">${escapeHtml(c.version)}</span>
          </div>
          <div class="version-badges">${candidateStatusBadge(c, v)}</div>
        </div>

        ${total > 0 && selected === 0 ? `
          <div class="alert-danger">
            ${icon('alert', 14)}
            <span>All ${total} blueprints are deselected. Click the blueprint pill above to select blueprints for PR updates.</span>
          </div>` : ''}

        ${c.summary ? `<div class="candidate-summary">${escapeHtml(c.summary)}</div>` : ''}

        <div class="candidate-footer">
          <div>
            <span class="field-label">Artifact URL:</span>
            <a href="${escapeHtml(c.download_url)}" target="_blank" rel="noopener" class="candidate-url">${escapeHtml(c.download_url)}</a>
          </div>
          ${total > 0 ? `
            <button class="btn-text-link" onclick="openBlueprintModal('${escapeHtml(c.package_id)}')" title="Inspect target blueprint files">
              ${icon('file', 13)}<span>View ${total} Affected Blueprint${total === 1 ? '' : 's'} &rarr;</span>
            </button>` : ''}
        </div>
      </div>`;
  }).join('');
}

// -------------------------------------------------------------- packages

function renderPackageTableRow(p, { status, upstreamVersion, summary, candidate, held }) {
  let label = escapeHtml(status || PS.REGISTERED);
  if (status === PS.READY_FOR_REVIEW && candidate?.test_status === TS.SUCCESS) label += ' <span class="badge-inline-pass">✓ PASSED</span>';
  else if (status === PS.UP_TO_DATE) label = 'UP-TO-DATE';
  else if (status === PS.SNOOZED && p.snooze_until) label = `SNOOZED (${escapeHtml(p.snooze_until.substring(5, 10))})`;

  const rawSummary = summary || p.qualification_summary || 'Baseline registered.';
  const isError = status === PS.ERROR || /failed|error:/i.test(rawSummary);
  const shortSummary = rawSummary.length > 95 ? `${rawSummary.substring(0, 92)}...` : rawSummary;
  const targetVer = (upstreamVersion && upstreamVersion !== '-') ? upstreamVersion : p.current_version;

  const actions = held ? renderUnblockButton(p.package_id, true) : `
    ${renderPrLink(candidate?.pr_url, { small: true, label: 'PR' })}
    ${candidate ? renderTestPill(candidate, true) : ''}
    ${renderPolicyButtons(p.package_id, targetVer, true)}`;

  return `
    <tr>
      <td class="pkg-cell"><span class="code-pill">${escapeHtml(p.package_id)}</span><div class="pkg-name">${escapeHtml(p.name)}</div></td>
      <td><span class="code-pill pill-green">${escapeHtml(p.current_version)}</span>${outOfSyncBadge(p.package_id)}</td>
      <td>${upstreamVersion && upstreamVersion !== '-' ? `<span class="code-pill">${escapeHtml(upstreamVersion)}</span>` : '<span class="text-muted">-</span>'}</td>
      <td>${statusBadge(status || PS.REGISTERED, label)}</td>
      <td>${renderBlueprintPill(p.package_id) || '<span class="small-muted">None</span>'}</td>
      <td class="summary-cell ${isError ? 'is-error' : ''}" title="${escapeHtml(rawSummary)}">
        ${isError ? '<span class="badge badge-red badge-tiny">ERROR</span>' : ''}${escapeHtml(shortSummary)}
      </td>
      <td class="source-cell"><a href="${escapeHtml(p.source_url)}" target="_blank" rel="noopener" class="source-link" title="${escapeHtml(p.source_url)}">${escapeHtml((p.source_url || '').replace(/^https?:\/\//, ''))}</a><div><span class="code-pill pill-amber pill-xs">${escapeHtml(p.upstream_type)}</span></div></td>
      <td class="actions-cell"><div class="actions-inline">${actions}</div></td>
    </tr>`;
}

function renderPackages() {
  const tbody = document.getElementById('packages-table-body');
  if (!tbody) return;
  if (!state.packages.length) {
    tbody.innerHTML = emptyRow(8, 'No packages registered in datastore.');
    return;
  }
  const rows = [];
  for (const p of state.packages) {
    const cands = state.candByPkg[p.package_id] || [];
    const heldCand = cands.find(c => POLICY.has(c.status));
    const activeCand = cands.find(c => ACTIVE.has(c.status));
    const snoozeActive = p.snoozed_version && (!p.snooze_until || new Date(p.snooze_until) > new Date());
    const isHeld = POLICY.has(p.status) || !!heldCand || snoozeActive || !!p.blocked_version;

    if (isHeld && activeCand) {
      // A held version plus a newer active candidate: one row each.
      const blocked = !!p.blocked_version || heldCand?.status === CS.BLOCKED || p.status === PS.BLOCKED;
      const heldVersion = p.blocked_version || p.snoozed_version || heldCand?.version || p.upstream_version;
      const heldSummary = heldCand?.summary || (blocked
        ? `Package is BLOCKED for version ${heldVersion} (manual unblock required from dashboard).`
        : `Package is SNOOZED for version ${heldVersion} until ${p.snooze_until ? p.snooze_until.substring(0, 10) : 'active period'}.`);
      rows.push(renderPackageTableRow(p, {
        status: blocked ? PS.BLOCKED : PS.SNOOZED, upstreamVersion: heldVersion,
        summary: heldSummary, candidate: heldCand, held: true,
      }));
      rows.push(renderPackageTableRow(p, {
        status: activeCand.status, upstreamVersion: activeCand.version || p.upstream_version,
        summary: activeCand.summary || p.qualification_summary, candidate: activeCand, held: false,
      }));
    } else {
      let status = p.status || PS.REGISTERED;
      if (!POLICY.has(p.status)) status = (activeCand || heldCand)?.status || status;
      rows.push(renderPackageTableRow(p, {
        status, upstreamVersion: p.upstream_version, summary: p.qualification_summary,
        candidate: activeCand || heldCand, held: POLICY.has(status),
      }));
    }
  }
  tbody.innerHTML = rows.join('');
}

// ------------------------------------------------- instances, rules, console

function renderInstances() {
  const tbody = document.getElementById('instances-table-body');
  if (!tbody) return;
  if (!state.instances.length) {
    tbody.innerHTML = emptyRow(5, 'No blueprint instances registered in datastore.');
    return;
  }
  tbody.innerHTML = state.instances.map(inst => {
    const coupled = inst.coupled_vars || [];
    const selected = inst.enabled !== false;
    return `
      <tr>
        <td>
          <div class="flex-row">
            <span class="mono-path">${escapeHtml(inst.blueprint_path)}</span>
            <span class="badge ${selected ? 'badge-green' : 'badge-gray'} badge-xs">${selected ? 'Active' : 'Deselected'}</span>
          </div>
        </td>
        <td><span class="code-pill">${escapeHtml(inst.package_id)}</span></td>
        <td><span class="code-pill pill-green">${escapeHtml(inst.current_version || '-')}</span></td>
        <td><span class="code-pill pill-blue">${escapeHtml(inst.variable_name)}</span></td>
        <td>${coupled.length
          ? coupled.map(c => `<span class="code-pill pill-amber">${escapeHtml(c.variable_name)}</span>`).join(', ')
          : '<span class="text-muted">-</span>'}</td>
      </tr>`;
  }).join('');
}

function renderRules() {
  const tbody = document.getElementById('rules-table-body');
  if (!tbody) return;
  if (!state.rules.length) {
    tbody.innerHTML = emptyRow(6, 'No policy rules configured in datastore.');
    return;
  }
  tbody.innerHTML = state.rules.map(r => `
    <tr>
      <td><span class="code-pill">${escapeHtml(r.rule_id)}</span></td>
      <td><span class="code-pill">${escapeHtml(r.package_id)}</span></td>
      <td><span class="badge badge-amber">${escapeHtml(r.rule_type)}</span></td>
      <td><span class="code-pill pill-red-strong">${escapeHtml(r.version_constraint)}</span></td>
      <td><span class="badge badge-red">${escapeHtml(r.action)}</span></td>
      <td class="cell-small">${escapeHtml(r.reason)}</td>
    </tr>`).join('');
}

function renderDynamicApplyButtons() {
  const container = document.getElementById('dynamic-apply-buttons');
  if (!container) return;
  const pending = state.candidates.filter(c => c.status === CS.UPDATE_FOUND);
  container.innerHTML = pending.length
    ? pending.map(c => `
        <button class="btn btn-blue" onclick="triggerAction('apply', '${escapeHtml(c.package_id)}')">
          Apply ${escapeHtml(c.package_id)} (${escapeHtml(c.version)})
        </button>`).join('')
    : '<span class="small-muted">No pending updates. Run "Check for Updates" above.</span>';
}

// ================================================================= actions

function appendTerminal(text) {
  const terminal = document.getElementById('terminal-body');
  terminal.textContent += text;
  terminal.scrollTop = terminal.scrollHeight;
}

async function triggerAction(action, packageId = null) {
  if (action === 'end_to_end' || action === 'reset') switchTab('tab-console');
  appendTerminal(`\n[ACTION TRIGGERED] ${action} (target=${packageId || 'all'})...\n`);
  try {
    await postAction({ action, package_id: packageId });
    startLogPolling();
    fetchState();
  } catch (err) {
    if (err.status === 409) alert('An action is already in progress. Please wait for it to complete.');
    else alert(`Action '${action}' failed: ${err.message}`);
  }
}

// Streams the running action's output: each poll sends a cursor and receives only new lines.
let logTimer = null;
let logRun = -1;
let logOffset = 0;

function startLogPolling() {
  if (logTimer) return;
  logTimer = setTimeout(pollLogs, 0);
}

async function pollLogs() {
  let running = true;
  try {
    const res = await fetch(`/api/logs?run=${logRun}&offset=${logOffset}`);
    if (res.ok) {
      const d = await res.json();
      const terminal = document.getElementById('terminal-body');
      if (d.reset) terminal.textContent = d.logs;
      else if (d.logs) terminal.textContent += d.logs;
      if (d.reset || d.logs) terminal.scrollTop = terminal.scrollHeight;
      logRun = d.run_id;
      logOffset = d.next;
      running = d.is_running;
    }
  } catch (e) {
    console.warn('GET /api/logs failed:', e);
  }
  if (running) {
    logTimer = setTimeout(pollLogs, 700);
    return;
  }
  logTimer = null;
  fetchState();
  fetchDiff();
}

async function runPolicyAction(body, what) {
  try {
    await postAction(body);
    fetchState();
  } catch (err) {
    alert(`${what} failed: ${err.message}`);
  }
}

function openSnoozeModal(packageId, version) {
  document.getElementById('snooze-package-id').value = packageId;
  document.getElementById('snooze-version-val').value = version || '';
  setText('snooze-display-pkg', packageId);
  setText('snooze-display-ver', version || 'Latest');
  openModal('snooze-modal');
}

function submitSnooze() {
  const packageId = document.getElementById('snooze-package-id').value;
  const version = document.getElementById('snooze-version-val').value;
  const days = parseInt(document.getElementById('snooze-days-select').value || state.config.snooze_days_default, 10);
  if (!packageId) return;
  closeModal('snooze-modal');
  runPolicyAction({ action: 'snooze', package_id: packageId, version, days }, 'Snooze');
}

function confirmBlockPackage(packageId, version) {
  const msg = `Are you sure you want to BLOCK updates for '${packageId}' (version: ${version || 'all'})?\n\n`
    + 'No PRs will be created for this version unless manually unblocked from the dashboard or a newer upstream version is released.';
  if (confirm(msg)) runPolicyAction({ action: 'block', package_id: packageId, version }, 'Block');
}

function triggerUnblock(packageId) {
  runPolicyAction({ action: 'unblock', package_id: packageId }, 'Unblock');
}

// ======================================================= blueprint modal

function updateModalSelectionStats(packageId) {
  const { total, selected } = selectionCounts(packageId);
  const badge = document.getElementById('modal-selection-count-badge');
  if (badge) {
    badge.className = `badge ${selected === 0 ? 'badge-red' : (selected < total ? 'badge-amber' : 'badge-green')}`;
    badge.textContent = `${selected}/${total} Selected`;
  }
  setText('modal-instance-count', `${selected} of ${total} blueprint${total === 1 ? '' : 's'} selected for automated PR updates`);
  const pkg = state.pkgMap[packageId] || { package_id: packageId };
  const subtitle = document.getElementById('modal-package-subtitle');
  if (subtitle) {
    subtitle.innerHTML = `<strong>${escapeHtml(pkg.name || packageId)}</strong> &bull; Current version: `
      + `<code class="code-pill">${escapeHtml(pkg.current_version || '-')}</code> ${outOfSyncBadge(packageId)}`;
  }
}

function renderBlueprintCard(packageId, inst, tests) {
  const selected = inst.enabled !== false;
  const coupled = inst.coupled_vars || [];
  const id = escapeHtml(inst.instance_id);
  const test = tests.find(t => t.blueprint_path === inst.blueprint_path || (t.blueprint_paths || []).includes(inst.blueprint_path));
  const testCls = !test ? '' : (test.status === TS.SUCCESS ? 'badge-green' : (TEST_FAILED.has(test.status) ? 'badge-red' : 'badge-blue'));
  return `
    <div class="blueprint-card ${selected ? 'bp-card-selected' : 'bp-card-deselected'}" id="bp-card-${id}">
      <div class="blueprint-card-header">
        <label class="bp-checkbox-label" title="Include or exclude this blueprint from automated PR updates">
          <input type="checkbox" class="bp-toggle-checkbox" id="bp-check-${id}" ${selected ? 'checked' : ''}
                 onchange="toggleBlueprintSelection('${escapeHtml(packageId)}', '${id}', this.checked)">
          <span class="bp-toggle-text">${selected ? 'Included in PR Updates' : 'Excluded from Updates'}</span>
        </label>
        <button class="btn-copy-path" onclick="copyBlueprintPath(this, '${escapeHtml(inst.blueprint_path)}')" title="Copy relative path to clipboard">
          ${icon('copy')}<span>Copy</span>
        </button>
      </div>
      <div class="blueprint-path-box">${icon('file', 14)}<span>${escapeHtml(inst.blueprint_path)}</span></div>
      <div class="blueprint-card-details">
        <div class="detail-row">
          <span class="detail-label">Pinned Version:</span>
          <span class="code-pill">${escapeHtml(inst.current_version || '-')}</span>
        </div>
        <div class="detail-row">
          <span class="detail-label">Target Variable:</span>
          <span class="code-pill var-pill">${escapeHtml(inst.variable_name)}</span>
        </div>
        ${coupled.length ? `
          <div class="coupled-box">
            <div class="coupled-title">${icon('link')} Also Updated (same pinned version)</div>
            <div class="coupled-list">
              ${coupled.map(c => `<div class="coupled-item"><span class="code-pill var-pill">${escapeHtml(c.variable_name)}</span></div>`).join('')}
            </div>
          </div>` : `
          <div class="detail-row">
            <span class="detail-label">Also Updated:</span>
            <span class="small-muted">-</span>
          </div>`}
        ${test ? `
          <div class="detail-row">
            <span class="detail-label">Integration Test:</span>
            <span class="code-pill">${escapeHtml(test.test_name)}</span>
            <span class="badge ${testCls} badge-tiny">${escapeHtml(test.status)}</span>
            ${test.build_url ? `<a href="${escapeHtml(test.build_url)}" target="_blank" rel="noopener" class="log-link" title="View Cloud Build log for this test"><span>View Log</span>${icon('external', 10)}</a>` : ''}
          </div>` : ''}
      </div>
    </div>`;
}

function openBlueprintModal(packageId) {
  activeModalPackageId = packageId;
  const instances = state.byPkg[packageId] || [];
  const cand = (state.candByPkg[packageId] || []).find(c => c.status !== CS.MERGED);
  const tests = cand?.tests || [];

  setText('modal-package-title', `Associated Blueprints: ${packageId}`);
  const selBar = document.getElementById('modal-selection-bar');
  if (selBar) selBar.hidden = !instances.length;
  updateModalSelectionStats(packageId);

  document.getElementById('modal-blueprint-list').innerHTML = instances.length
    ? instances.map(inst => renderBlueprintCard(packageId, inst, tests)).join('')
    : `<div class="modal-empty">No registered blueprint instances found for <code>${escapeHtml(packageId)}</code>.</div>`;
  openModal('blueprint-modal');
}

function syncBlueprintCard(inst) {
  const selected = inst.enabled !== false;
  const card = document.getElementById(`bp-card-${inst.instance_id}`);
  if (card) {
    card.classList.toggle('bp-card-selected', selected);
    card.classList.toggle('bp-card-deselected', !selected);
    const txt = card.querySelector('.bp-toggle-text');
    if (txt) txt.textContent = selected ? 'Included in PR Updates' : 'Excluded from Updates';
  }
  const chk = document.getElementById(`bp-check-${inst.instance_id}`);
  if (chk) chk.checked = selected;
}

// Optimistically applies the selection, persists it, and rolls back if the server rejects it.
async function setBlueprintSelection(packageId, instanceIds, selected, body) {
  const insts = (state.byPkg[packageId] || []).filter(i => instanceIds.includes(i.instance_id));
  const previous = insts.map(i => i.enabled);
  const apply = values => {
    insts.forEach((inst, k) => { inst.enabled = values[k]; syncBlueprintCard(inst); });
    updateModalSelectionStats(packageId);
    renderActive();
  };
  apply(insts.map(() => selected));
  try {
    await postAction(body);
    fetchState();  // picks up the recomputed current version
  } catch (err) {
    apply(previous);
    alert(`Failed to save blueprint selection: ${err.message}`);
  }
}

function toggleBlueprintSelection(packageId, instanceId, checked) {
  setBlueprintSelection(packageId, [instanceId], checked,
    { action: 'toggle_blueprint', package_id: packageId, instance_id: instanceId, enabled: checked });
}

function bulkToggleModalBlueprints(selectAll) {
  const packageId = activeModalPackageId;
  if (!packageId) return;
  const ids = (state.byPkg[packageId] || []).map(i => i.instance_id);
  setBlueprintSelection(packageId, ids, selectAll,
    { action: selectAll ? 'select_all_blueprints' : 'deselect_all_blueprints', package_id: packageId });
}

function copyBlueprintPath(btn, path) {
  if (!navigator.clipboard) {
    prompt('Copy blueprint path:', path);
    return;
  }
  navigator.clipboard.writeText(path).then(() => {
    const original = btn.innerHTML;
    btn.innerHTML = `${icon('check')}<span>Copied!</span>`;
    btn.classList.add('copied');
    setTimeout(() => { btn.innerHTML = original; btn.classList.remove('copied'); }, 1800);
  }).catch(err => console.error('Failed to copy text:', err));
}

// ===================================================== test results modal

function renderTestCard(t) {
  const passed = t.status === TS.SUCCESS;
  const failed = TEST_FAILED.has(t.status);
  const cardCls = passed ? 'test-status-success' : (failed ? 'test-status-failure' : 'test-status-running');
  const badgeCls = passed ? 'badge-green' : (failed ? 'badge-red' : 'badge-blue');
  const label = passed ? 'PASSED' : (failed ? (t.status === TS.TIMEOUT ? 'TIMEOUT' : 'FAILED') : (t.status || TS.RUNNING));
  const bps = (t.blueprint_paths && t.blueprint_paths.length) ? t.blueprint_paths : (t.blueprint_path ? [t.blueprint_path] : []);
  return `
    <div class="test-item-card ${cardCls}">
      <div class="test-item-header">
        <div class="test-name-box">
          <span class="test-title">${escapeHtml(t.test_name)}</span>
          <span class="code-pill trigger-pill">${escapeHtml(t.trigger_name || triggerNameFor(t.test_name))}</span>
        </div>
        <span class="badge ${badgeCls} badge-strong">${escapeHtml(label)}</span>
      </div>
      <div class="test-item-details">
        <div class="test-blueprint-row">
          <span class="test-row-label">Blueprint(s):</span>
          <div class="test-bp-list">${bps.length
            ? bps.map(b => `<div class="test-blueprint-path">&bull; ${escapeHtml(b)}</div>`).join('')
            : '<span class="small-muted">Cluster Toolkit test harness</span>'}</div>
        </div>
        ${t.build_id ? `
          <div class="test-blueprint-row">
            <span class="test-row-label">Cloud Build ID:</span>
            <span class="code-pill trigger-pill">${escapeHtml(t.build_id)}</span>
          </div>` : ''}
        ${t.failure_reason || t.message ? `
          <div class="test-blueprint-row test-failure-row">
            <span class="test-row-label">${t.failure_reason ? 'Failure Reason:' : 'Message:'}</span>
            <span>${escapeHtml(t.failure_reason || t.message)}</span>
          </div>` : ''}
      </div>
      <div class="test-item-actions">
        ${t.build_url ? `
          <a href="${escapeHtml(t.build_url)}" target="_blank" rel="noopener" class="btn-test-log-link" title="Open Cloud Build Console execution log">
            ${icon('pulse', 13)}<span>View Cloud Build Log</span>${icon('external', 10)}
          </a>` : '<span class="italic-muted">Log will be available once triggered</span>'}
      </div>
    </div>`;
}

function openTestResultsModal(candidateId, event) {
  if (event) {
    event.stopPropagation();
    event.preventDefault();
  }
  const cand = state.candidates.find(c => c.candidate_id === candidateId);
  if (!cand) return;
  activeTestModalCandidateId = candidateId;
  const pkg = state.pkgMap[cand.package_id] || {};
  const tests = cand.tests || [];
  const { total, passed, failed, running } = testCounts(tests);

  setText('test-modal-title', `Test Matrix: ${pkg.name || cand.package_id}`);
  const prPart = cand.pr_url ? ` &bull; Pull Request #${escapeHtml(prNumber(cand.pr_url))}` : '';
  document.getElementById('test-modal-subtitle').innerHTML =
    `Candidate <code class="code-pill">${escapeHtml(cand.version || '-')}</code> &bull; Package: <code class="code-pill">${escapeHtml(cand.package_id)}</code>${prPart}`;

  const summary = document.getElementById('test-chip-summary');
  summary.textContent = cand.tests_summary || `${passed}/${total} Tests Passed`;
  summary.className = `test-chip-pill ${failed ? 'chip-failed' : (running ? 'chip-running' : 'chip-passed')}`;
  setText('test-chip-passed', `✓ ${passed} Passed`);
  setText('test-chip-failed', `✗ ${failed} Failed`);
  setText('test-chip-running', `⟳ ${running} Running`);
  document.getElementById('test-chip-failed').hidden = !failed;
  document.getElementById('test-chip-running').hidden = !running;

  document.getElementById('test-modal-pr-link-box').innerHTML = cand.pr_url
    ? `<a href="${escapeHtml(cand.pr_url)}" target="_blank" rel="noopener" class="btn btn-secondary btn-sm btn-success-outline" data-nobusy>
         ${icon('pr')}<span>View GitHub PR #${escapeHtml(prNumber(cand.pr_url))}</span>${icon('external', 10)}
       </a>`
    : '';

  document.getElementById('test-modal-list').innerHTML = tests.length
    ? tests.map(renderTestCard).join('')
    : '<div class="modal-empty">No integration tests mapped or executed for this candidate update yet.</div>';
  openModal('test-results-modal');
}

function rerunTestsFromModal() {
  const cand = state.candidates.find(c => c.candidate_id === activeTestModalCandidateId);
  if (!cand) return;
  closeModal('test-results-modal');
  triggerAction('test', cand.package_id);
}

// ==================================================================== init

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') document.querySelectorAll('.modal-overlay.active').forEach(m => closeModal(m.id));
});

document.addEventListener('visibilitychange', () => {
  if (!document.hidden) pollState();
});

document.addEventListener('DOMContentLoaded', pollState);
