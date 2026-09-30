// Frontend Logic for Cluster Toolkit Infrastructure Updater Dashboard

let isPollingLogs = false;

function switchTab(tabId, btnElement) {
  document.querySelectorAll('.tab-panel').forEach(panel => panel.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));

  const targetPanel = document.getElementById(tabId);
  if (targetPanel) {
    targetPanel.classList.add('active');
  }
  const btn = btnElement ? (btnElement.closest ? (btnElement.closest('.tab-btn') || btnElement) : btnElement) : null;
  if (btn && btn.classList) {
    btn.classList.add('active');
  }

  if (tabId === 'tab-diff') {
    fetchDiff();
  } else if (latestPackages && latestPackages.length > 0) {
    if (tabId === 'tab-packages') renderPackages(latestPackages, latestCandidates);
    else if (tabId === 'tab-instances') renderInstances(latestInstances);
    else if (tabId === 'tab-rules') renderRules(latestRules);
    else if (tabId === 'tab-candidates') renderCandidates(latestCandidates, latestPackages);
  }
}

function clearConsole() {
  const terminal = document.getElementById('terminal-body');
  if (terminal) {
    terminal.textContent = '';
  }
}

let latestPackages = [];
let latestInstances = [];
let latestCandidates = [];
let latestRules = [];
let lastRenderedSignature = "";

async function fetchState() {
  try {
    const res = await fetch('/api/state');
    if (!res.ok) {
      console.warn('GET /api/state returned HTTP', res.status);
      return;
    }
    const data = await res.json();
    latestPackages = data.packages || [];
    latestInstances = data.instances || [];
    latestCandidates = data.candidates || [];
    latestRules = data.rules || [];

    // 1. Update Metrics
    const stats = data.stats || {};
    const pendingCount = stats.pending_updates !== undefined ? stats.pending_updates : (stats.qualified_candidates || 0);
    const readyCount = stats.ready_updates !== undefined ? stats.ready_updates : (stats.applied_candidates || 0);

    const totalPkg = stats.total_packages !== undefined ? stats.total_packages : latestPackages.length;
    const totalInst = stats.total_instances !== undefined ? stats.total_instances : latestInstances.length;
    const totalRules = stats.total_rules !== undefined ? stats.total_rules : latestRules.length;

    const elPkg = document.getElementById('metric-packages');
    if (elPkg) elPkg.textContent = totalPkg;

    const elInst = document.getElementById('metric-instances');
    if (elInst) elInst.textContent = totalInst;

    const elRules = document.getElementById('metric-rules');
    if (elRules) elRules.textContent = totalRules;

    const elCand = document.getElementById('metric-candidates');
    if (elCand) elCand.textContent = pendingCount;
    
    const counterEl = document.getElementById('counter-candidates');
    if (counterEl) counterEl.textContent = pendingCount;

    // Persist to localStorage for instant restoration on page reloads
    try {
      localStorage.setItem('infra_updater_stats', JSON.stringify({
        total_packages: totalPkg,
        total_instances: totalInst,
        total_rules: totalRules,
        pending_updates: pendingCount
      }));
    } catch (e) {}

    const diffDot = document.getElementById('diff-dot');
    if (diffDot) diffDot.style.display = data.has_modifications ? 'inline-block' : 'none';

    const subEl = document.getElementById('metric-candidates-sub');
    if (subEl) {
      if (readyCount > 0) {
        subEl.textContent = `${readyCount} ready for review`;
      } else {
        subEl.textContent = 'Qualified & ready for review';
      }
    }

    // 2. Update Status Pill & Target Repo Badge
    if (data.config) {
      const badgeText = document.getElementById('repo-badge-text');
      if (badgeText) {
        if (data.config.is_fork && data.config.fork_owner) {
          badgeText.textContent = `${data.config.owner}/${data.config.repo_name} (${data.config.base_branch}) ← fork: ${data.config.fork_owner}`;
        } else {
          badgeText.textContent = `${data.config.owner}/${data.config.repo_name} (${data.config.base_branch})`;
        }
      }
    }

    const dot = document.getElementById('status-dot');
    const text = document.getElementById('status-text');
    if (dot && text) {
      if (data.is_running) {
        dot.className = 'status-dot running';
        text.textContent = `Running: ${data.current_action}...`;
      } else {
        dot.className = 'status-dot';
        text.textContent = data.last_status === 'FAILED' ? 'Last Run Failed' : 'System Idle';
      }
    }

    // Disable action buttons if running
    document.querySelectorAll('.btn').forEach(btn => {
      if (btn.id !== 'btn-clear') {
        btn.disabled = !!data.is_running;
      }
    });

    // 3. Skip full DOM rebuild if state hasn't changed (prevents DOM trashing & scroll jump)
    const currentSignature = JSON.stringify({
      p: latestPackages,
      i: latestInstances,
      c: latestCandidates,
      r: latestRules,
      mod: data.has_modifications
    });

    if (currentSignature !== lastRenderedSignature) {
      lastRenderedSignature = currentSignature;
      try { renderPackages(latestPackages, latestCandidates); } catch (e) { console.error('Error in renderPackages:', e); }
      try { renderInstances(latestInstances); } catch (e) { console.error('Error in renderInstances:', e); }
      try { renderRules(latestRules); } catch (e) { console.error('Error in renderRules:', e); }
      try { renderCandidates(latestCandidates, latestPackages); } catch (e) { console.error('Error in renderCandidates:', e); }
      try { renderDynamicApplyButtons(latestCandidates, latestPackages); } catch (e) { console.error('Error in renderDynamicApplyButtons:', e); }
    }

    if (data.is_running && !isPollingLogs) {
      startLogPolling();
    }
  } catch (err) {
    console.error('Error fetching state:', err);
  }
}

async function fetchDiff() {
  try {
    const res = await fetch('/api/diff');
    if (!res.ok) return;
    const data = await res.json();
    renderDiff(data.diff);
  } catch (err) {
    console.error('Error fetching diff:', err);
  }
}

function renderDiff(rawDiff) {
  const diffBody = document.getElementById('diff-body');
  if (!rawDiff || rawDiff.trim() === '') {
    diffBody.innerHTML = '<span style="color: var(--text-muted)">No uncommitted blueprint modifications in workspace. Apply a candidate update to view AST changes.</span>';
    return;
  }

  const lines = rawDiff.split('\n');
  const formatted = lines.map(line => {
    const escaped = escapeHtml(line);
    if (line.startsWith('+') && !line.startsWith('+++')) {
      return `<span class="diff-line-add">${escaped}</span>`;
    } else if (line.startsWith('-') && !line.startsWith('---')) {
      return `<span class="diff-line-del">${escaped}</span>`;
    } else if (line.startsWith('@@') || line.startsWith('diff --git') || line.startsWith('---') || line.startsWith('+++')) {
      return `<span class="diff-line-header">${escaped}</span>`;
    }
    return escaped;
  }).join('\n');

  diffBody.innerHTML = formatted;
}

// Multi-Blueprint Test Utilities
function getCandidateTests(candidate) {
  if (!candidate) return [];
  if (Array.isArray(candidate.tests) && candidate.tests.length > 0) {
    return candidate.tests;
  }
  if (candidate.build_url || candidate.test_name) {
    const isSuccess = candidate.test_status === 'SUCCESS' || candidate.status === 'READY_FOR_REVIEW';
    const isFailure = candidate.test_status === 'FAILURE' || candidate.status === 'TEST_FAILED';
    const isRunning = candidate.test_status === 'RUNNING' || candidate.status === 'TESTING';
    const status = isSuccess ? 'SUCCESS' : (isFailure ? 'FAILURE' : (isRunning ? 'RUNNING' : 'UNKNOWN'));
    return [{
      test_name: candidate.test_name || 'Integration Test',
      trigger_name: candidate.trigger_name || (candidate.test_name ? `PR-test-${candidate.test_name}` : 'Integration Test'),
      blueprint_path: candidate.blueprint_path || '',
      blueprint_paths: candidate.blueprint_paths || [],
      build_id: candidate.build_id || '',
      build_url: candidate.build_url || '',
      status: status
    }];
  }
  return [];
}

function renderTestPillHtml(candidate, isSmall = false) {
  const tests = getCandidateTests(candidate);
  if (!tests || tests.length === 0) return '';

  const total = tests.length;
  const passed = tests.filter(t => t.status === 'SUCCESS').length;
  const failed = tests.filter(t => ['FAILURE', 'ERROR', 'TIMEOUT'].includes(t.status)).length;
  const running = tests.filter(t => ['RUNNING', 'TRIGGERED', 'PENDING', 'QUEUED'].includes(t.status)).length;

  let pillClass = 'test-pill-all-passed';
  let pillText = '';
  let pillIcon = '';
  let tooltip = '';

  if (running > 0) {
    pillClass = 'test-pill-running';
    pillIcon = `<span class="spinner-sm" style="width: 10px; height: 10px; margin-right: 2px;"></span>`;
    if (isSmall) {
      pillText = total === 1 ? '1 running' : `${running}/${total} run`;
    } else {
      if (total === 1) {
        pillText = '1 running';
      } else if (passed > 0) {
        pillText = `${passed}/${total} passed (${running} running)`;
      } else {
        pillText = `${running}/${total} running`;
      }
    }
    tooltip = `Click to monitor test execution (${running} running, ${passed} passed, ${failed} failed)`;
  } else if (failed > 0) {
    pillClass = 'test-pill-partial-failed';
    pillIcon = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>`;
    if (isSmall) {
      pillText = total === 1 ? '0/1 passed' : `${passed}/${total} passed`;
    } else {
      if (total === 1) {
        pillText = '0/1 passed (1 failed)';
      } else {
        pillText = `${passed}/${total} passed (${failed} failed)`;
      }
    }
    tooltip = `Click to view test failure logs (${failed} failed, ${passed} passed)`;
  } else {
    pillClass = 'test-pill-all-passed';
    pillIcon = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>`;
    if (isSmall) {
      pillText = total === 1 ? '1/1 passed' : `${passed}/${total} passed`;
    } else {
      pillText = total === 1 ? '1/1 test passed' : `${passed}/${total} tests passed`;
    }
    tooltip = `Click to view all ${total} passing Cloud Build test logs`;
  }

  const smClass = isSmall ? 'btn-test-pill-sm' : '';
  const candId = escapeHtml(candidate.candidate_id || '');

  return `
    <button class="btn-test-pill ${pillClass} ${smClass}" onclick="openTestResultsModal('${candId}', event)" title="${escapeHtml(tooltip)}">
      ${pillIcon}
      <span>${pillText}</span>
      <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="opacity: 0.8; margin-left: 1px;"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
    </button>
  `;
}

function renderCandidates(candidates, packages) {
  const container = document.getElementById('candidates-container');
  const pkgMap = {};
  (packages || latestPackages || []).forEach(p => { pkgMap[p.package_id] = p; });

  const activeCandidates = (candidates || []).filter(c => {
    if (c.status === 'MERGED' || c.status === 'CANCELLED' || c.status === 'SUPERSEDED') return false;
    return true;
  });

  if (!activeCandidates || activeCandidates.length === 0) {
    container.innerHTML = `
      <div class="empty-state">
        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        <div style="font-weight: 500; margin-top: 8px;">No pending updates qualified</div>
        <div style="font-size: 12px; color: var(--text-muted); margin-top: 4px;">Click "Check for Updates" to scan upstream releases and evaluate compatibility.</div>
      </div>
    `;
    return;
  }

  container.innerHTML = activeCandidates.map(c => {
    const isTesting = c.status === 'TESTING' || c.test_status === 'RUNNING';
    const testPassed = c.test_status === 'SUCCESS';
    const testFailed = c.test_status === 'FAILURE' || c.status === 'TEST_FAILED';
    const isReady = (c.status === 'READY_FOR_REVIEW' || c.status === 'APPLIED') && !isTesting;
    const isSnoozed = c.status === 'SNOOZED';
    const isBlocked = c.status === 'BLOCKED';

    let statusBadge = '<span class="badge badge-blue">UPDATE_FOUND</span>';
    if (isTesting) {
      statusBadge = '<span class="badge badge-amber"><span class="spinner-sm"></span> TESTING</span>';
    } else if (testPassed) {
      statusBadge = '<span class="badge badge-green">READY_FOR_REVIEW</span> <span class="badge badge-green" style="margin-left: 6px; font-size: 10px;">✓ TESTS PASSED</span>';
    } else if (testFailed) {
      statusBadge = `<span class="badge badge-red">✗ TEST FAILED</span> <span class="badge badge-amber" style="margin-left: 4px; font-size: 10px;">${escapeHtml(c.tests_summary || '')}</span>`;
    } else if (isReady) {
      statusBadge = '<span class="badge badge-green">READY_FOR_REVIEW</span>';
    } else if (isBlocked) {
      statusBadge = '<span class="badge badge-red">BLOCKED</span>';
    } else if (isSnoozed) {
      statusBadge = '<span class="badge badge-amber">SNOOZED</span>';
    }

    const pkg = pkgMap[c.package_id] || {};
    const currVer = pkg.current_version || '-';

    const instances = (latestInstances || []).filter(inst => inst.package_id === c.package_id);
    const totalCount = instances.length;
    const selectedCount = instances.filter(inst => inst.enabled !== false).length;
    let blueprintPillHtml = '';
    if (totalCount > 0) {
      let pillClass = 'pill-all-selected';
      if (selectedCount === 0) {
        pillClass = 'pill-none-selected';
      } else if (selectedCount < totalCount) {
        pillClass = 'pill-partial-selected';
      }
      blueprintPillHtml = `
        <button class="btn-blueprint-pill ${pillClass}" onclick="openBlueprintModal('${escapeHtml(c.package_id)}')" title="Click to view & select blueprints for PR update (${selectedCount}/${totalCount} selected)">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
          <span>${selectedCount}/${totalCount} selected</span>
        </button>
      `;
    }

    const testPillHtml = renderTestPillHtml(c);

    let actionButtonsHtml = '';
    if (isSnoozed || isBlocked) {
      actionButtonsHtml = `
        <button class="btn btn-secondary" style="color: #22c55e; border-color: rgba(34, 197, 94, 0.4); display: inline-flex; align-items: center; gap: 6px;" onclick="triggerUnblock('${escapeHtml(c.package_id)}')" title="Unblock / Resume updates">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
          <span>Unblock</span>
        </button>
      `;
    } else if (isTesting) {
      actionButtonsHtml = `
        ${c.pr_url ? `
          <a href="${escapeHtml(c.pr_url)}" target="_blank" class="btn btn-secondary" style="color: #22c55e; border-color: rgba(34, 197, 94, 0.4); text-decoration: none; display: inline-flex; align-items: center; gap: 6px;" title="View Pull Request on GitHub">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M13 6h3a2 2 0 0 1 2 2v7"/><line x1="6" y1="9" x2="6" y2="21"/></svg>
            <span>View PR #${escapeHtml(String(c.pr_url).split('/').pop())}</span>
          </a>
        ` : ''}
        ${testPillHtml || (c.build_url ? `
          <a href="${escapeHtml(c.build_url)}" target="_blank" class="btn btn-secondary" style="color: #f59e0b; border-color: rgba(245, 158, 11, 0.4); text-decoration: none; display: inline-flex; align-items: center; gap: 6px;" title="View Cloud Build Execution Log">
            <span class="spinner-sm"></span>
            <span>Build Log</span>
          </a>
        ` : '')}
        <button class="btn btn-secondary" disabled style="opacity: 0.85;">
          <span>Testing ${escapeHtml(c.test_name || 'Blueprint')}...</span>
        </button>
      `;
    } else {
      actionButtonsHtml = `
        ${c.pr_url ? `
          <a href="${escapeHtml(c.pr_url)}" target="_blank" class="btn btn-secondary" style="color: #22c55e; border-color: rgba(34, 197, 94, 0.4); text-decoration: none; display: inline-flex; align-items: center; gap: 6px;" title="View Pull Request on GitHub">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M13 6h3a2 2 0 0 1 2 2v7"/><line x1="6" y1="9" x2="6" y2="21"/></svg>
            <span>View PR #${escapeHtml(String(c.pr_url).split('/').pop())}</span>
          </a>
        ` : ''}
        ${testPillHtml || (c.build_url ? `
          <a href="${escapeHtml(c.build_url)}" target="_blank" class="btn btn-secondary" style="color: ${testPassed ? '#22c55e' : (testFailed ? 'var(--accent-red)' : '#f59e0b')}; border-color: rgba(255, 255, 255, 0.15); text-decoration: none; display: inline-flex; align-items: center; gap: 6px;" title="View Cloud Build Test Log">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
            <span>${testPassed ? 'Test Log (Passed)' : (testFailed ? 'Test Log (Failed)' : 'Build Log')}</span>
          </a>
        ` : '')}
        ${testFailed ? `
          <button class="btn btn-secondary" style="color: var(--accent-blue); border-color: rgba(59, 130, 246, 0.4);" onclick="triggerAction('test', '${escapeHtml(c.package_id)}')" title="Re-trigger blueprint tests on Cloud Build">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
            <span>Re-run Tests</span>
          </button>
        ` : ''}
        <button class="btn btn-secondary" onclick="openSnoozeModal('${escapeHtml(c.package_id)}', '${escapeHtml(c.version)}')" title="Snooze updates for this version (default: 30 days)">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
          <span>Snooze</span>
        </button>
        <button class="btn btn-secondary" style="color: var(--accent-red); border-color: rgba(239, 68, 68, 0.35);" onclick="confirmBlockPackage('${escapeHtml(c.package_id)}', '${escapeHtml(c.version)}')" title="Block updates for this version until manually unblocked">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>
          <span>Block</span>
        </button>
        <button class="btn ${isReady ? 'btn-secondary' : 'btn-primary'}" onclick="triggerAction('apply', '${escapeHtml(c.package_id)}')" ${isReady ? 'disabled' : ''}>
          ${isReady ? (testPassed ? 'Applied &bull; Tests Passed' : (testFailed ? 'Applied &bull; Test Failed' : 'Applied &bull; Ready for Review')) : 'Review &amp; Apply Update'}
        </button>
      `;
    }

    return `
      <div class="candidate-card">
        <div class="candidate-header">
          <div class="candidate-title-group">
            <span class="candidate-name">${escapeHtml(c.package_id)}</span>
            <span class="code-pill" style="color: var(--accent-blue)">${escapeHtml(pkg.name || c.package_id)}</span>
            <span class="badge badge-blue">GA Production</span>
            ${blueprintPillHtml}
          </div>
          <div class="candidate-action-group">
            ${actionButtonsHtml}
          </div>
        </div>

        <div class="version-banner">
          <div class="version-item">
            <span class="version-label">Current:</span>
            <span class="code-pill">${escapeHtml(currVer)}</span>
          </div>
          <span class="version-arrow">→</span>
          <div class="version-item">
            <span class="version-label">Target:</span>
            <span class="code-pill version-target">${escapeHtml(c.version)}</span>
          </div>
          <div class="version-badges">
            ${statusBadge}
          </div>
        </div>

        ${(totalCount > 0 && selectedCount === 0) ? `
          <div style="background: rgba(239, 68, 68, 0.12); border: 1px solid rgba(239, 68, 68, 0.3); border-radius: 6px; padding: 6px 10px; font-size: 11.5px; color: #fca5a5; display: flex; align-items: center; gap: 6px; margin: 4px 0 8px 0;">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
            <span>All ${totalCount} blueprints are deselected. Click the blueprint pill above to select blueprints for PR updates.</span>
          </div>
        ` : ''}

        ${(c.summary || c.changelog_summary) ? `
          <div style="font-size: 13px; color: var(--text-secondary); line-height: 1.5; padding: 4px 0 8px 0;">
            ${escapeHtml(c.summary || c.changelog_summary)}
          </div>
        ` : ''}

        <div class="candidate-footer">
          <div>
            <span style="color: var(--text-muted); font-size: 11px; text-transform: uppercase; font-weight: 600; margin-right: 6px;">Artifact URL:</span>
            <a href="${escapeHtml(c.download_url)}" target="_blank" class="candidate-url">${escapeHtml(c.download_url)}</a>
          </div>
          ${totalCount > 0 ? `
            <button class="btn-text-link" onclick="openBlueprintModal('${escapeHtml(c.package_id)}')" title="Inspect target blueprint files and variable couplings">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
              <span>View ${totalCount} Affected Blueprint${totalCount === 1 ? '' : 's'} &rarr;</span>
            </button>
          ` : ''}
        </div>
      </div>
    `;
  }).join('');
}

function renderPackageTableRow(p, { effectiveStatus, upstreamVersion, summary, candidate, isSnoozedOrBlockedRow }) {
  let badgeClass = 'badge-blue';
  let statusLabel = effectiveStatus;
  if (effectiveStatus === 'UPDATE_FOUND') {
    badgeClass = 'badge-blue';
    statusLabel = 'UPDATE_FOUND';
  } else if (effectiveStatus === 'READY_FOR_REVIEW') {
    badgeClass = 'badge-green';
    statusLabel = (candidate && candidate.test_status === 'SUCCESS')
      ? 'READY_FOR_REVIEW <span style="font-size: 9px; padding: 1px 4px; background: rgba(35, 134, 54, 0.4); border-radius: 3px; margin-left: 3px;">✓ PASSED</span>'
      : 'READY_FOR_REVIEW';
  } else if (effectiveStatus === 'TESTING') {
    badgeClass = 'badge-amber';
    statusLabel = '<span class="spinner-sm"></span> TESTING';
  } else if (effectiveStatus === 'TEST_FAILED') {
    badgeClass = 'badge-red';
    statusLabel = 'TEST_FAILED';
  } else if (effectiveStatus === 'UP_TO_DATE') {
    badgeClass = 'badge-green';
    statusLabel = 'UP-TO-DATE';
  } else if (effectiveStatus === 'BLOCKED' || effectiveStatus === 'BLOCKED_BY_RULE') {
    badgeClass = 'badge-red';
    statusLabel = 'BLOCKED';
  } else if (effectiveStatus === 'ERROR') {
    badgeClass = 'badge-red';
    statusLabel = 'ERROR';
  } else if (effectiveStatus === 'SNOOZED') {
    badgeClass = 'badge-amber';
    statusLabel = p.snooze_until ? `SNOOZED (${p.snooze_until.substring(5, 10)})` : 'SNOOZED';
  } else if (effectiveStatus === 'OBSOLETE') {
    badgeClass = 'badge-amber';
    statusLabel = 'OBSOLETE';
  } else {
    badgeClass = 'badge-blue';
    statusLabel = 'REGISTERED';
  }

  const upVerHtml = (upstreamVersion && upstreamVersion !== '-')
    ? `<span class="code-pill">${escapeHtml(upstreamVersion)}</span>`
    : `<span style="color: var(--text-muted)">-</span>`;

  const instances = (latestInstances || []).filter(inst => inst.package_id === p.package_id);
  const totalCount = instances.length;
  const selectedCount = instances.filter(inst => inst.enabled !== false).length;
  let blueprintBtnHtml = `<span style="color: var(--text-muted); font-size: 11px;">None</span>`;
  if (totalCount > 0) {
    let pillClass = 'pill-all-selected';
    if (selectedCount === 0) {
      pillClass = 'pill-none-selected';
    } else if (selectedCount < totalCount) {
      pillClass = 'pill-partial-selected';
    }
    blueprintBtnHtml = `
      <button class="btn-blueprint-pill ${pillClass}" onclick="openBlueprintModal('${escapeHtml(p.package_id)}')" title="Click to view & select blueprints for PR updates (${selectedCount}/${totalCount} selected)">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
        <span>${selectedCount}/${totalCount} selected</span>
      </button>
    `;
  }

  const rawSummary = summary || p.qualification_summary || 'Baseline registered.';
  const isError = effectiveStatus === 'ERROR' || rawSummary.toLowerCase().includes('failed') || rawSummary.toLowerCase().includes('error:');
  let displaySummary = rawSummary;
  if (displaySummary.length > 95) {
    displaySummary = displaySummary.substring(0, 92) + '...';
  }

  const summaryTdHtml = isError
    ? `<td style="font-size: 12px; line-height: 1.4; max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeHtml(rawSummary)}">
         <span class="badge badge-red" style="font-size: 9px; padding: 1px 5px; margin-right: 4px; vertical-align: middle;">ERROR</span>
         <span style="color: var(--accent-red); vertical-align: middle;">${escapeHtml(displaySummary)}</span>
       </td>`
    : `<td style="font-size: 12px; color: var(--text-secondary); line-height: 1.4; max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${escapeHtml(rawSummary)}">
         ${escapeHtml(displaySummary)}
       </td>`;

  const targetVer = (upstreamVersion && upstreamVersion !== '-') ? upstreamVersion : p.current_version;
  const actionsTdHtml = isSnoozedOrBlockedRow
    ? `<td style="text-align: right; white-space: nowrap;">
         <button class="btn btn-secondary" style="padding: 4px 10px; font-size: 11px; color: #22c55e; border-color: rgba(34, 197, 94, 0.4); display: inline-flex; align-items: center; gap: 4px;" onclick="triggerUnblock('${escapeHtml(p.package_id)}')" title="Unblock / Resume updates">
           <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
           <span>Unblock</span>
         </button>
       </td>`
    : `<td style="text-align: right; white-space: nowrap;">
         <div style="display: inline-flex; align-items: center; justify-content: flex-end; gap: 4px; flex-wrap: nowrap;">
           ${candidate && candidate.pr_url ? `
             <a href="${escapeHtml(candidate.pr_url)}" target="_blank" class="btn btn-secondary" style="padding: 3px 7px; font-size: 11px; color: #22c55e; border-color: rgba(34, 197, 94, 0.4); text-decoration: none; display: inline-flex; align-items: center; gap: 3px; flex-shrink: 0;" title="View PR">
               <span>PR #${escapeHtml(String(candidate.pr_url).split('/').pop())}</span>
             </a>
           ` : ''}
           ${candidate ? renderTestPillHtml(candidate, true) : ''}
           <button class="btn btn-secondary" style="padding: 4px 8px; font-size: 11px; flex-shrink: 0;" onclick="openSnoozeModal('${escapeHtml(p.package_id)}', '${escapeHtml(targetVer)}')" title="Snooze updates">
             <span>Snooze</span>
           </button>
           <button class="btn btn-secondary" style="padding: 4px 8px; font-size: 11px; color: var(--accent-red); border-color: rgba(239, 68, 68, 0.3); flex-shrink: 0;" onclick="confirmBlockPackage('${escapeHtml(p.package_id)}', '${escapeHtml(targetVer)}')" title="Block updates">
             <span>Block</span>
           </button>
         </div>
       </td>`;

  return `
    <tr>
      <td><span class="code-pill">${escapeHtml(p.package_id)}</span></td>
      <td><strong>${escapeHtml(p.name)}</strong></td>
      <td><span class="code-pill" style="color: var(--accent-green-light)">${escapeHtml(p.current_version)}</span></td>
      <td>${upVerHtml}</td>
      <td><span class="code-pill" style="color: var(--accent-amber)">${escapeHtml(p.upstream_type || 'github_release')}</span></td>
      <td><span class="badge ${badgeClass}">${statusLabel}</span></td>
      <td>${blueprintBtnHtml}</td>
      ${summaryTdHtml}
      <td style="max-width: 160px;"><a href="${escapeHtml(p.source_url)}" target="_blank" style="color: var(--accent-blue); text-decoration: none; font-size: 12px; display: inline-block; max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: middle;" title="${escapeHtml(p.source_url)}">${escapeHtml(p.source_url ? p.source_url.replace(/^https?:\/\//, '') : '')}</a></td>
      ${actionsTdHtml}
    </tr>
  `;
}

function renderPackages(packages, candidates) {
  const tbody = document.getElementById('packages-table-body');
  if (!tbody) return;
  const list = packages || latestPackages || [];
  if (list.length === 0) {
    tbody.innerHTML = '<tr><td colspan="10" style="color: var(--text-muted); text-align: center; padding: 24px;">No packages registered in datastore.</td></tr>';
    return;
  }
  const candList = candidates || latestCandidates || [];

  const rows = [];
  list.forEach(p => {
    const pkgCands = candList.filter(c => c.package_id === p.package_id);
    const snoozedOrBlockedCand = pkgCands.find(c => c.status === 'SNOOZED' || c.status === 'BLOCKED');
    const activeCand = pkgCands.find(c => ['UPDATE_FOUND', 'READY_FOR_REVIEW', 'TESTING', 'TEST_FAILED', 'QUALIFIED'].includes(c.status));

    const isSnoozedOrBlocked = (p.status === 'SNOOZED' || p.status === 'BLOCKED' || !!snoozedOrBlockedCand || (p.snoozed_version && (!p.snooze_until || new Date(p.snooze_until) > new Date())) || !!p.blocked_version);

    if (isSnoozedOrBlocked && activeCand) {
      // 1. Render the Snoozed/Blocked row (preserves the snoozed/blocked version display)
      const isBlocked = !!p.blocked_version || (snoozedOrBlockedCand && snoozedOrBlockedCand.status === 'BLOCKED') || p.status === 'BLOCKED';
      const sbStatus = isBlocked ? 'BLOCKED' : 'SNOOZED';
      const sbVersion = p.blocked_version || p.snoozed_version || (snoozedOrBlockedCand ? snoozedOrBlockedCand.version : p.upstream_version);
      const sbSummary = snoozedOrBlockedCand?.summary || (isBlocked 
        ? `Package is BLOCKED for version ${sbVersion} (manual unblock required from dashboard).` 
        : `Package is SNOOZED for version ${sbVersion} until ${p.snooze_until ? p.snooze_until.substring(0, 10) : 'active period'}.`);

      rows.push(renderPackageTableRow(p, {
        effectiveStatus: sbStatus,
        upstreamVersion: sbVersion,
        summary: sbSummary,
        candidate: snoozedOrBlockedCand,
        isSnoozedOrBlockedRow: true
      }));

      // 2. Render the New Upstream Candidate row
      rows.push(renderPackageTableRow(p, {
        effectiveStatus: activeCand.status,
        upstreamVersion: activeCand.version || p.upstream_version,
        summary: activeCand.summary || p.qualification_summary,
        candidate: activeCand,
        isSnoozedOrBlockedRow: false
      }));
    } else {
      let effectiveStatus = p.status || 'REGISTERED';
      if (p.status === 'SNOOZED' || p.status === 'BLOCKED') {
        effectiveStatus = p.status;
      } else if (activeCand) {
        effectiveStatus = activeCand.status;
      } else if (snoozedOrBlockedCand) {
        effectiveStatus = snoozedOrBlockedCand.status;
      }
      rows.push(renderPackageTableRow(p, {
        effectiveStatus: effectiveStatus,
        upstreamVersion: p.upstream_version,
        summary: p.qualification_summary,
        candidate: activeCand || snoozedOrBlockedCand,
        isSnoozedOrBlockedRow: effectiveStatus === 'SNOOZED' || effectiveStatus === 'BLOCKED'
      }));
    }
  });

  tbody.innerHTML = rows.join('');
}

function renderInstances(instances) {
  const tbody = document.getElementById('instances-table-body');
  if (!tbody) return;
  const list = instances || latestInstances || [];
  if (list.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" style="color: var(--text-muted); text-align: center; padding: 24px;">No blueprint instances registered in datastore.</td></tr>';
    return;
  }
  tbody.innerHTML = list.map(inst => {
    let coupled = [];
    if (Array.isArray(inst.coupled_vars)) {
      coupled = inst.coupled_vars;
    } else if (typeof inst.coupled_vars === 'string') {
      try { coupled = JSON.parse(inst.coupled_vars); } catch(e) { coupled = []; }
    }
    const coupledDesc = coupled.length > 0 
      ? coupled.map(c => `<span class="code-pill" style="color: var(--accent-amber)">${escapeHtml(c.variable_name || '')} (${escapeHtml(c.pattern || '{filename}')})</span>`).join(', ')
      : '<span style="color: var(--text-muted)">Direct</span>';
    const statusBadge = inst.enabled !== false
      ? `<span class="badge badge-green" style="font-size: 10px; margin-left: 6px;">Active</span>`
      : `<span class="badge badge-gray" style="font-size: 10px; margin-left: 6px; color: var(--text-muted); border-color: rgba(255,255,255,0.1);">Deselected</span>`;

    return `
      <tr>
        <td>
          <div style="display: flex; align-items: center; gap: 4px;">
            <span class="code-pill">${escapeHtml(inst.instance_id)}</span>
            ${statusBadge}
          </div>
        </td>
        <td><span class="code-pill">${escapeHtml(inst.package_id)}</span></td>
        <td><span class="code-pill" style="color: #58a6ff">${escapeHtml(inst.variable_name)}</span></td>
        <td>${coupledDesc}</td>
        <td><span style="font-family: var(--font-mono); font-size: 12px; color: var(--text-secondary);">${escapeHtml(inst.blueprint_path)}</span></td>
      </tr>
    `;
  }).join('');
}

function renderRules(rules) {
  const tbody = document.getElementById('rules-table-body');
  if (!tbody) return;
  const list = rules || latestRules || [];
  if (list.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" style="color: var(--text-muted); text-align: center; padding: 24px;">No policy rules configured in datastore.</td></tr>';
    return;
  }
  tbody.innerHTML = list.map(r => `
    <tr>
      <td><span class="code-pill">${escapeHtml(r.rule_id)}</span></td>
      <td><span class="code-pill">${escapeHtml(r.package_id)}</span></td>
      <td><span class="badge badge-amber">${escapeHtml(r.rule_type)}</span></td>
      <td><span class="code-pill" style="color: var(--accent-red); font-weight: 600;">${escapeHtml(r.version_constraint)}</span></td>
      <td><span class="badge badge-red">${escapeHtml(r.action)}</span></td>
      <td style="color: var(--text-secondary); font-size: 12.5px;">${escapeHtml(r.reason)}</td>
    </tr>
  `).join('');
}



function renderDynamicApplyButtons(candidates, packages) {
  const container = document.getElementById('dynamic-apply-buttons');
  if (!container) return;

  const pkgMap = {};
  (packages || latestPackages || []).forEach(p => { pkgMap[p.package_id] = p; });

  const pendingCandidates = (candidates || []).filter(c => {
    return c.status === 'UPDATE_FOUND' || c.status === 'QUALIFIED';
  });

  if (pendingCandidates.length === 0) {
    container.innerHTML = '<span style="font-size: 11px; color: var(--text-muted); line-height: 1.4; display: block;">No pending updates. Run "Check for Updates" above.</span>';
    return;
  }

  container.innerHTML = pendingCandidates.map(c => `
    <button class="btn btn-blue" onclick="triggerAction('apply', '${escapeHtml(c.package_id)}')">
      Apply ${escapeHtml(c.package_id)} (${escapeHtml(c.version)})
    </button>
  `).join('');
}

async function triggerAction(action, packageId = null) {
  lastRenderedSignature = "";
  // If running full pipeline or reset, open console tab
  if (action === 'end_to_end' || action === 'reset') {
    const consoleBtn = Array.from(document.querySelectorAll('.tab-btn')).find(b => b.textContent.includes('Console'));
    if (consoleBtn) switchTab('tab-console', consoleBtn);
  }

  const terminal = document.getElementById('terminal-body');
  terminal.textContent += `\n[ACTION TRIGGERED] ${action} (target=${packageId || 'all'})...\n`;
  terminal.scrollTop = terminal.scrollHeight;

  try {
    const res = await fetch('/api/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: action, package_id: packageId })
    });

    if (res.status === 409) {
      alert('An action is already in progress. Please wait for it to complete.');
      return;
    }

    startLogPolling();
  } catch (err) {
    console.error('Error triggering action:', err);
  }
}

function startLogPolling() {
  if (isPollingLogs) return;
  isPollingLogs = true;

  const terminal = document.getElementById('terminal-body');
  const pollInterval = setInterval(async () => {
    try {
      const res = await fetch('/api/logs');
      if (!res.ok) return;
      const data = await res.json();

      terminal.textContent = data.logs;
      terminal.scrollTop = terminal.scrollHeight;

      if (!data.is_running) {
        clearInterval(pollInterval);
        isPollingLogs = false;
        fetchState();
        fetchDiff();
      }
    } catch (e) {
      clearInterval(pollInterval);
      isPollingLogs = false;
    }
  }, 700);
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

let activeModalPackageId = null;

function updateModalSelectionStats(packageId) {
  const instances = (latestInstances || []).filter(inst => inst.package_id === packageId);
  const totalCount = instances.length;
  const selectedCount = instances.filter(inst => inst.enabled !== false).length;

  const countBadge = document.getElementById('modal-selection-count-badge');
  if (countBadge) {
    let badgeClass = 'badge-green';
    if (selectedCount === 0) badgeClass = 'badge-red';
    else if (selectedCount < totalCount) badgeClass = 'badge-amber';
    countBadge.className = `badge ${badgeClass}`;
    countBadge.textContent = `${selectedCount}/${totalCount} Selected`;
  }
  const countEl = document.getElementById('modal-instance-count');
  if (countEl) {
    countEl.textContent = `${selectedCount} of ${totalCount} blueprint${totalCount === 1 ? '' : 's'} selected for automated PR updates`;
  }
}

function openBlueprintModal(packageId) {
  activeModalPackageId = packageId;
  const pkg = latestPackages.find(p => p.package_id === packageId) || { package_id: packageId, name: packageId };
  const instances = (latestInstances || []).filter(inst => inst.package_id === packageId);
  const totalCount = instances.length;

  const cand = (latestCandidates || []).find(c => c.package_id === packageId && c.status !== 'CANCELLED' && c.status !== 'SUPERSEDED');
  const candTests = cand ? getCandidateTests(cand) : [];

  const titleEl = document.getElementById('modal-package-title');
  const subtitleEl = document.getElementById('modal-package-subtitle');
  const listEl = document.getElementById('modal-blueprint-list');
  const selBar = document.getElementById('modal-selection-bar');

  if (titleEl) {
    titleEl.textContent = `Associated Blueprints: ${pkg.package_id}`;
  }
  if (subtitleEl) {
    subtitleEl.innerHTML = `<strong>${escapeHtml(pkg.name || pkg.package_id)}</strong> &bull; Current version: <code class="code-pill">${escapeHtml(pkg.current_version || '-')}</code>`;
  }
  if (selBar) {
    selBar.style.display = totalCount > 0 ? 'flex' : 'none';
  }

  updateModalSelectionStats(packageId);

  if (!instances || instances.length === 0) {
    listEl.innerHTML = `
      <div style="text-align: center; padding: 32px 20px; color: var(--text-muted);">
        No registered blueprint instances found for <code>${escapeHtml(packageId)}</code>.
      </div>
    `;
  } else {
    listEl.innerHTML = instances.map(inst => {
      const isSelected = inst.enabled !== false;
      const coupled = Array.isArray(inst.coupled_vars) ? inst.coupled_vars : [];
      let coupledHtml = '';
      if (coupled.length > 0) {
        coupledHtml = `
          <div class="coupled-box">
            <div class="coupled-title">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
              Coupled Sibling Variables (Atomic Synchronization)
            </div>
            <div class="coupled-list">
              ${coupled.map(c => `
                <div class="coupled-item">
                  <span class="code-pill var-pill">${escapeHtml(c.variable_name)}</span>
                  <span class="coupled-pattern">Pattern: <code>${escapeHtml(c.pattern || '{filename}')}</code></span>
                </div>
              `).join('')}
            </div>
          </div>
        `;
      } else {
        coupledHtml = `
          <div class="detail-row">
            <span class="detail-label">Coupled Vars:</span>
            <span style="color: var(--text-muted); font-size: 12px;">None (Standalone variable)</span>
          </div>
        `;
      }

      let testInfoHtml = '';
      if (candTests.length > 0) {
        const matchingTest = candTests.find(t => {
          return t.blueprint_path === inst.blueprint_path ||
                 (Array.isArray(t.blueprint_paths) && t.blueprint_paths.includes(inst.blueprint_path)) ||
                 (inst.blueprint_path && t.blueprint_path && (inst.blueprint_path.endsWith(t.blueprint_path) || t.blueprint_path.endsWith(inst.blueprint_path)));
        });
        if (matchingTest) {
          const isPass = matchingTest.status === 'SUCCESS';
          const isFail = ['FAILURE', 'ERROR', 'TIMEOUT'].includes(matchingTest.status);
          const badgeCls = isPass ? 'badge-green' : (isFail ? 'badge-red' : 'badge-blue');
          testInfoHtml = `
            <div class="detail-row">
              <span class="detail-label">Integration Test:</span>
              <span class="code-pill">${escapeHtml(matchingTest.test_name)}</span>
              <span class="badge ${badgeCls}" style="font-size: 10px;">${escapeHtml(matchingTest.status)}</span>
              ${matchingTest.build_url ? `
                <a href="${escapeHtml(matchingTest.build_url)}" target="_blank" style="color: var(--accent-blue); text-decoration: none; font-size: 11.5px; display: inline-flex; align-items: center; gap: 3px;" title="View Cloud Build log for this test">
                  <span>View Log</span>
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
                </a>
              ` : ''}
            </div>
          `;
        }
      }

      return `
        <div class="blueprint-card ${isSelected ? 'bp-card-selected' : 'bp-card-deselected'}" id="bp-card-${escapeHtml(inst.instance_id)}">
          <div class="blueprint-card-header">
            <label class="bp-checkbox-label" title="${isSelected ? 'Deselect blueprint to exclude from automated PR updates' : 'Select blueprint to include in automated PR updates'}">
              <input type="checkbox"
                     class="bp-toggle-checkbox"
                     id="bp-check-${escapeHtml(inst.instance_id)}"
                     ${isSelected ? 'checked' : ''}
                     onchange="toggleBlueprintSelection('${escapeHtml(packageId)}', '${escapeHtml(inst.instance_id)}', this.checked)">
              <span class="bp-toggle-text">${isSelected ? 'Included in PR Updates' : 'Excluded from Updates'}</span>
            </label>
            <button class="btn-copy-path" onclick="copyBlueprintPath(this, '${escapeHtml(inst.blueprint_path)}')" title="Copy relative path to clipboard">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
              <span>Copy</span>
            </button>
          </div>
          <div class="blueprint-path-box">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
            <span>${escapeHtml(inst.blueprint_path)}</span>
          </div>
          <div class="blueprint-card-details">
            <div class="detail-row">
              <span class="detail-label">Instance ID:</span>
              <span class="code-pill">${escapeHtml(inst.instance_id)}</span>
            </div>
            <div class="detail-row">
              <span class="detail-label">Target Variable:</span>
              <span class="code-pill var-pill">${escapeHtml(inst.variable_name)}</span>
            </div>
            ${coupledHtml}
            ${testInfoHtml}
          </div>
        </div>
      `;
    }).join('');
  }

  const modal = document.getElementById('blueprint-modal');
  if (modal) {
    modal.classList.add('active');
    document.body.style.overflow = 'hidden';
  }
}

async function toggleBlueprintSelection(packageId, instanceId, isChecked) {
  // 1. Optimistic in-memory update
  const inst = (latestInstances || []).find(i => i.package_id === packageId && i.instance_id === instanceId);
  if (inst) {
    inst.enabled = isChecked;
  }
  const pkg = (latestPackages || []).find(p => p.package_id === packageId);
  if (pkg && pkg.blueprints) {
    const bp = pkg.blueprints.find(b => b.instance_id === instanceId);
    if (bp) bp.enabled = isChecked;
  }

  // 2. Optimistic DOM update
  const card = document.getElementById(`bp-card-${instanceId}`);
  if (card) {
    card.classList.toggle('bp-card-selected', isChecked);
    card.classList.toggle('bp-card-deselected', !isChecked);
    const txt = card.querySelector('.bp-toggle-text');
    if (txt) {
      txt.textContent = isChecked ? 'Included in PR Updates' : 'Excluded from Updates';
    }
  }

  // 3. Update counter badges in modal, package table, and candidate cards
  updateModalSelectionStats(packageId);
  renderPackages(latestPackages);
  renderCandidates(latestCandidates, latestPackages);

  // 4. Send API action
  try {
    const res = await fetch('/api/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action: 'toggle_blueprint',
        package_id: packageId,
        instance_id: instanceId,
        enabled: isChecked
      })
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.error || err.message || 'Failed to save blueprint selection');
    }
  } catch (err) {
    console.error('Error toggling blueprint selection:', err);
    // Revert state on failure
    if (inst) inst.enabled = !isChecked;
    if (pkg && pkg.blueprints) {
      const bp = pkg.blueprints.find(b => b.instance_id === instanceId);
      if (bp) bp.enabled = !isChecked;
    }
    const chk = document.getElementById(`bp-check-${instanceId}`);
    if (chk) chk.checked = !isChecked;
    if (card) {
      card.classList.toggle('bp-card-selected', !isChecked);
      card.classList.toggle('bp-card-deselected', isChecked);
      const txt = card.querySelector('.bp-toggle-text');
      if (txt) txt.textContent = !isChecked ? 'Included in PR Updates' : 'Excluded from Updates';
    }
    updateModalSelectionStats(packageId);
    renderPackages(latestPackages);
    renderCandidates(latestCandidates, latestPackages);
    alert(`Failed to save blueprint selection: ${err.message}`);
  }
}

async function bulkToggleModalBlueprints(shouldSelectAll) {
  if (!activeModalPackageId) return;
  const packageId = activeModalPackageId;

  // 1. Optimistic in-memory update
  (latestInstances || []).forEach(inst => {
    if (inst.package_id === packageId) {
      inst.enabled = shouldSelectAll;
    }
  });
  const pkg = (latestPackages || []).find(p => p.package_id === packageId);
  if (pkg && pkg.blueprints) {
    pkg.blueprints.forEach(bp => { bp.enabled = shouldSelectAll; });
  }

  // 2. Optimistic DOM update for all cards in modal
  const instances = (latestInstances || []).filter(i => i.package_id === packageId);
  instances.forEach(inst => {
    const card = document.getElementById(`bp-card-${inst.instance_id}`);
    if (card) {
      card.classList.toggle('bp-card-selected', shouldSelectAll);
      card.classList.toggle('bp-card-deselected', !shouldSelectAll);
      const txt = card.querySelector('.bp-toggle-text');
      if (txt) {
        txt.textContent = shouldSelectAll ? 'Included in PR Updates' : 'Excluded from Updates';
      }
    }
    const chk = document.getElementById(`bp-check-${inst.instance_id}`);
    if (chk) chk.checked = shouldSelectAll;
  });

  // 3. Update counter badges in modal, package table, and candidate cards
  updateModalSelectionStats(packageId);
  renderPackages(latestPackages);
  renderCandidates(latestCandidates, latestPackages);

  // 4. Send API action
  try {
    const res = await fetch('/api/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action: shouldSelectAll ? 'select_all_blueprints' : 'deselect_all_blueprints',
        package_id: packageId
      })
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.error || err.message || 'Failed to save bulk selection');
    }
  } catch (err) {
    console.error('Error bulk updating blueprint selection:', err);
    alert(`Failed to save bulk selection: ${err.message}`);
    openBlueprintModal(packageId);
  }
}

function closeBlueprintModal(event) {
  if (event && event.target && event.target.id !== 'blueprint-modal' && !event.target.classList.contains('modal-close-btn') && !event.target.closest('.modal-close-btn') && event.target.tagName !== 'BUTTON') {
    return;
  }
  activeModalPackageId = null;
  const modal = document.getElementById('blueprint-modal');
  if (modal) {
    modal.classList.remove('active');
    document.body.style.overflow = '';
  }
}

function copyBlueprintPath(btn, path) {
  if (!navigator.clipboard) {
    prompt('Copy blueprint path:', path);
    return;
  }
  navigator.clipboard.writeText(path).then(() => {
    const origHtml = btn.innerHTML;
    btn.innerHTML = `
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="#3fb950" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>
      <span style="color: #3fb950; font-weight: 600;">Copied!</span>
    `;
    setTimeout(() => {
      btn.innerHTML = origHtml;
    }, 1800);
  }).catch(err => {
    console.error('Failed to copy text:', err);
  });
}

function openSnoozeModal(packageId, version) {
  const pkgInput = document.getElementById('snooze-package-id');
  const verInput = document.getElementById('snooze-version-val');
  const displayPkg = document.getElementById('snooze-display-pkg');
  const displayVer = document.getElementById('snooze-display-ver');

  if (pkgInput) pkgInput.value = packageId;
  if (verInput) verInput.value = version || '';
  if (displayPkg) displayPkg.textContent = packageId;
  if (displayVer) displayVer.textContent = version || 'Latest';

  const modal = document.getElementById('snooze-modal');
  if (modal) {
    modal.classList.add('active');
    document.body.style.overflow = 'hidden';
  }
}

function closeSnoozeModal(event) {
  if (event && event.target && event.target.id !== 'snooze-modal' && !event.target.classList.contains('modal-close-btn') && !event.target.closest('.modal-close-btn') && event.target.tagName !== 'BUTTON') {
    return;
  }
  const modal = document.getElementById('snooze-modal');
  if (modal) {
    modal.classList.remove('active');
    document.body.style.overflow = '';
  }
}

async function submitSnooze() {
  const packageId = document.getElementById('snooze-package-id')?.value;
  const version = document.getElementById('snooze-version-val')?.value;
  const days = parseInt(document.getElementById('snooze-days-select')?.value || '30', 10);

  if (!packageId) return;

  closeSnoozeModal();

  try {
    const res = await fetch('/api/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action: 'snooze',
        package_id: packageId,
        version: version,
        days: days
      })
    });
    const result = await res.json();
    if (res.ok) {
      lastRenderedSignature = "";
      await fetchState();
    } else {
      alert(`Snooze failed: ${result.error || result.message}`);
    }
  } catch (err) {
    console.error('Error submitting snooze:', err);
  }
}

async function confirmBlockPackage(packageId, version) {
  const confirmMsg = `Are you sure you want to BLOCK updates for '${packageId}' (version: ${version || 'all'})?\n\nNo PRs will be created for this version unless manually unblocked from the dashboard or a newer upstream version is released.`;
  if (!confirm(confirmMsg)) return;

  try {
    const res = await fetch('/api/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action: 'block',
        package_id: packageId,
        version: version
      })
    });
    const result = await res.json();
    if (res.ok) {
      lastRenderedSignature = "";
      await fetchState();
    } else {
      alert(`Block failed: ${result.error || result.message}`);
    }
  } catch (err) {
    console.error('Error blocking package:', err);
  }
}

async function triggerUnblock(packageId) {
  try {
    const res = await fetch('/api/action', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        action: 'unblock',
        package_id: packageId
      })
    });
    const result = await res.json();
    if (res.ok) {
      lastRenderedSignature = "";
      await fetchState();
    } else {
      alert(`Unblock failed: ${result.error || result.message}`);
    }
  } catch (err) {
    console.error('Error unblocking package:', err);
  }
}

let activeTestModalCandidateId = null;

function openTestResultsModal(candidateId, event) {
  if (event) {
    event.stopPropagation();
    event.preventDefault();
  }

  activeTestModalCandidateId = candidateId;
  const cand = (latestCandidates || []).find(c => c.candidate_id === candidateId);
  if (!cand) {
    console.warn(`Candidate not found for id: ${candidateId}`);
    return;
  }

  const pkg = (latestPackages || []).find(p => p.package_id === cand.package_id) || { package_id: cand.package_id, name: cand.package_id };
  const tests = getCandidateTests(cand);

  const titleEl = document.getElementById('test-modal-title');
  const subtitleEl = document.getElementById('test-modal-subtitle');
  const listEl = document.getElementById('test-modal-list');
  const prLinkBox = document.getElementById('test-modal-pr-link-box');

  const summaryChip = document.getElementById('test-chip-summary');
  const passedChip = document.getElementById('test-chip-passed');
  const failedChip = document.getElementById('test-chip-failed');
  const runningChip = document.getElementById('test-chip-running');

  if (titleEl) {
    titleEl.textContent = `Test Matrix: ${pkg.name || cand.package_id}`;
  }
  if (subtitleEl) {
    const prPart = cand.pr_url ? ` &bull; Pull Request #${escapeHtml(String(cand.pr_url).split('/').pop())}` : '';
    subtitleEl.innerHTML = `Candidate <code class="code-pill">${escapeHtml(cand.version || '-')}</code> &bull; Package: <code class="code-pill">${escapeHtml(cand.package_id)}</code>${prPart}`;
  }

  const total = tests.length;
  const passed = tests.filter(t => t.status === 'SUCCESS').length;
  const failed = tests.filter(t => ['FAILURE', 'ERROR', 'TIMEOUT'].includes(t.status)).length;
  const running = tests.filter(t => ['RUNNING', 'TRIGGERED', 'PENDING', 'QUEUED'].includes(t.status)).length;

  if (summaryChip) {
    summaryChip.textContent = cand.tests_summary || `${passed}/${total} Tests Passed`;
    if (failed > 0) {
      summaryChip.style.background = 'rgba(239, 68, 68, 0.2)';
      summaryChip.style.color = '#f87171';
    } else if (running > 0) {
      summaryChip.style.background = 'rgba(59, 130, 246, 0.2)';
      summaryChip.style.color = '#60a5fa';
    } else {
      summaryChip.style.background = 'rgba(34, 197, 94, 0.2)';
      summaryChip.style.color = '#4ade80';
    }
  }

  if (passedChip) passedChip.textContent = `✓ ${passed} Passed`;
  if (failedChip) {
    failedChip.textContent = `✗ ${failed} Failed`;
    failedChip.style.display = failed > 0 ? 'inline-flex' : 'none';
  }
  if (runningChip) {
    runningChip.textContent = `⟳ ${running} Running`;
    runningChip.style.display = running > 0 ? 'inline-flex' : 'none';
  }

  if (prLinkBox) {
    if (cand.pr_url) {
      prLinkBox.innerHTML = `
        <a href="${escapeHtml(cand.pr_url)}" target="_blank" class="btn btn-secondary btn-sm" style="color: #22c55e; border-color: rgba(34, 197, 94, 0.4); text-decoration: none; display: inline-flex; align-items: center; gap: 6px;">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M13 6h3a2 2 0 0 1 2 2v7"/><line x1="6" y1="9" x2="21"/></svg>
          <span>View GitHub PR #${escapeHtml(String(cand.pr_url).split('/').pop())}</span>
          <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
        </a>
      `;
    } else {
      prLinkBox.innerHTML = '';
    }
  }

  if (listEl) {
    if (tests.length === 0) {
      listEl.innerHTML = `
        <div style="text-align: center; padding: 36px 20px; color: var(--text-muted);">
          No integration tests mapped or executed for this candidate update yet.
        </div>
      `;
    } else {
      listEl.innerHTML = tests.map(t => {
        const isSuccess = t.status === 'SUCCESS';
        const isFailure = ['FAILURE', 'ERROR', 'TIMEOUT'].includes(t.status);

        let cardClass = 'test-status-running';
        let badgeClass = 'badge-blue';
        let statusLabel = t.status || 'RUNNING';

        if (isSuccess) {
          cardClass = 'test-status-success';
          badgeClass = 'badge-green';
          statusLabel = 'PASSED';
        } else if (isFailure) {
          cardClass = 'test-status-failure';
          badgeClass = 'badge-red';
          statusLabel = t.status === 'TIMEOUT' ? 'TIMEOUT' : 'FAILED';
        }

        const bps = Array.isArray(t.blueprint_paths) && t.blueprint_paths.length > 0 ? t.blueprint_paths : (t.blueprint_path ? [t.blueprint_path] : []);
        const bpDisplay = bps.length > 0 
          ? bps.map(b => `<div class="test-blueprint-path">&bull; ${escapeHtml(b)}</div>`).join('')
          : `<span style="color: var(--text-muted); font-size: 11.5px;">Cluster Toolkit test harness</span>`;

        return `
          <div class="test-item-card ${cardClass}">
            <div class="test-item-header">
              <div class="test-name-box">
                <span class="test-title">${escapeHtml(t.test_name)}</span>
                <span class="code-pill" style="font-size: 11px; color: var(--text-secondary);">${escapeHtml(t.trigger_name || `PR-test-${t.test_name}`)}</span>
              </div>
              <span class="badge ${badgeClass}" style="font-weight: 700; letter-spacing: 0.5px;">${escapeHtml(statusLabel)}</span>
            </div>

            <div class="test-item-details">
              <div class="test-blueprint-row">
                <span style="min-width: 95px; font-weight: 500; color: var(--text-muted);">Blueprint(s):</span>
                <div style="display: flex; flex-direction: column; gap: 2px;">${bpDisplay}</div>
              </div>

              ${t.build_id ? `
                <div class="test-blueprint-row">
                  <span style="min-width: 95px; font-weight: 500; color: var(--text-muted);">Cloud Build ID:</span>
                  <span class="code-pill" style="font-size: 11px;">${escapeHtml(t.build_id)}</span>
                </div>
              ` : ''}

              ${t.failure_reason ? `
                <div class="test-blueprint-row" style="color: var(--accent-red);">
                  <span style="min-width: 95px; font-weight: 600;">Failure Reason:</span>
                  <span>${escapeHtml(t.failure_reason)}</span>
                </div>
              ` : ''}
            </div>

            <div class="test-item-actions">
              ${t.build_url ? `
                <a href="${escapeHtml(t.build_url)}" target="_blank" class="btn-test-log-link" title="Open Cloud Build Console execution log">
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
                  <span>View Cloud Build Log</span>
                  <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
                </a>
              ` : `
                <span style="font-size: 11.5px; color: var(--text-muted); font-style: italic;">Log will be available once triggered</span>
              `}
            </div>
          </div>
        `;
      }).join('');
    }
  }

  const modal = document.getElementById('test-results-modal');
  if (modal) {
    modal.classList.add('active');
    document.body.style.overflow = 'hidden';
  }
}

function closeTestResultsModal(event) {
  if (event && event.target && event.target.id !== 'test-results-modal' && !event.target.classList.contains('modal-close-btn') && !event.target.closest('.modal-close-btn') && event.target.tagName !== 'BUTTON') {
    return;
  }
  activeTestModalCandidateId = null;
  const modal = document.getElementById('test-results-modal');
  if (modal) {
    modal.classList.remove('active');
    document.body.style.overflow = '';
  }
}

function rerunTestsFromModal() {
  if (!activeTestModalCandidateId) return;
  const cand = (latestCandidates || []).find(c => c.candidate_id === activeTestModalCandidateId);
  if (!cand) return;
  const pkgId = cand.package_id;
  closeTestResultsModal();
  triggerAction('test', pkgId);
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    const bpModal = document.getElementById('blueprint-modal');
    if (bpModal && bpModal.classList.contains('active')) {
      closeBlueprintModal();
    }
    const snzModal = document.getElementById('snooze-modal');
    if (snzModal && snzModal.classList.contains('active')) {
      closeSnoozeModal();
    }
    const testModal = document.getElementById('test-results-modal');
    if (testModal && testModal.classList.contains('active')) {
      closeTestResultsModal();
    }
  }
});

function restoreCachedStats() {
  try {
    const raw = localStorage.getItem('infra_updater_stats');
    if (!raw) return;
    const stats = JSON.parse(raw);
    const elPkg = document.getElementById('metric-packages');
    if (elPkg && stats.total_packages !== undefined) elPkg.textContent = stats.total_packages;
    const elInst = document.getElementById('metric-instances');
    if (elInst && stats.total_instances !== undefined) elInst.textContent = stats.total_instances;
    const elRules = document.getElementById('metric-rules');
    if (elRules && stats.total_rules !== undefined) elRules.textContent = stats.total_rules;
    const elCand = document.getElementById('metric-candidates');
    if (elCand && stats.pending_updates !== undefined) elCand.textContent = stats.pending_updates;
    const counterEl = document.getElementById('counter-candidates');
    if (counterEl && stats.pending_updates !== undefined) counterEl.textContent = stats.pending_updates;
  } catch (e) {}
}

// Initial setup
document.addEventListener('DOMContentLoaded', () => {
  restoreCachedStats();
  fetchState();
  setInterval(fetchState, 3000);
});
