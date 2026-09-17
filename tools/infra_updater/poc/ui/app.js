// Frontend Logic for Cluster Toolkit Infrastructure Updater POC Dashboard

let isPollingLogs = false;

function switchTab(tabId, btnElement) {
  document.querySelectorAll('.tab-panel').forEach(panel => panel.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));

  const targetPanel = document.getElementById(tabId);
  if (targetPanel) {
    targetPanel.classList.add('active');
  }
  if (btnElement) {
    btnElement.classList.add('active');
  }

  if (tabId === 'tab-diff') {
    fetchDiff();
  }
}

function clearConsole() {
  const terminal = document.getElementById('terminal-body');
  if (terminal) {
    terminal.textContent = '';
  }
}

let latestPackages = [];

async function fetchState() {
  try {
    const res = await fetch('/api/state');
    if (!res.ok) return;
    const data = await res.json();
    latestPackages = data.packages || [];

    // 1. Update Metrics
    const pendingCount = data.stats.pending_updates !== undefined ? data.stats.pending_updates : (data.stats.qualified_candidates || 0);
    const readyCount = data.stats.ready_updates !== undefined ? data.stats.ready_updates : (data.stats.applied_candidates || 0);

    document.getElementById('metric-packages').textContent = data.stats.total_packages;
    document.getElementById('metric-instances').textContent = data.stats.total_instances;
    document.getElementById('metric-rules').textContent = data.stats.total_rules;
    document.getElementById('metric-candidates').textContent = pendingCount;
    
    const counterEl = document.getElementById('counter-candidates');
    if (counterEl) counterEl.textContent = pendingCount;

    const diffDot = document.getElementById('diff-dot');
    if (diffDot) diffDot.style.display = data.has_modifications ? 'inline-block' : 'none';

    if (readyCount > 0) {
      document.getElementById('metric-candidates-sub').textContent = `${readyCount} ready for review`;
    } else {
      document.getElementById('metric-candidates-sub').textContent = 'Qualified & ready for review';
    }

    // 2. Update Status Pill
    const dot = document.getElementById('status-dot');
    const text = document.getElementById('status-text');
    if (data.is_running) {
      dot.className = 'status-dot running';
      text.textContent = `Running: ${data.current_action}...`;
    } else {
      dot.className = 'status-dot';
      text.textContent = data.last_status === 'FAILED' ? 'Last Run Failed' : 'System Idle';
    }

    // Disable action buttons if running
    document.querySelectorAll('.btn').forEach(btn => {
      if (btn.id !== 'btn-clear') {
        btn.disabled = data.is_running;
      }
    });

    // 3. Render Views
    renderPackages(data.packages);
    renderInstances(data.instances);
    renderRules(data.rules);
    renderCandidates(data.candidates, data.packages);
    renderBenchmarks(data.benchmarks);
    renderDynamicApplyButtons(data.candidates, data.packages);

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

function renderCandidates(candidates, packages) {
  const container = document.getElementById('candidates-container');
  if (!candidates || candidates.length === 0) {
    container.innerHTML = `
      <div class="empty-state">
        <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        <div style="font-weight: 500; margin-top: 8px;">No pending updates qualified</div>
        <div style="font-size: 12px; color: var(--text-muted); margin-top: 4px;">Click "Check for Updates" to scan upstream releases and evaluate compatibility.</div>
      </div>
    `;
    return;
  }

  const pkgMap = {};
  (packages || latestPackages || []).forEach(p => { pkgMap[p.package_id] = p; });

  container.innerHTML = candidates.map(c => {
    const isReady = c.status === 'READY_FOR_REVIEW' || c.status === 'APPLIED';
    const statusBadge = isReady 
      ? '<span class="badge badge-green">READY_FOR_REVIEW</span>' 
      : '<span class="badge badge-blue">UPDATE_FOUND</span>';

    const verdictBadge = c.compatibility_verdict === 'COMPATIBLE' 
      ? '<span class="badge badge-green">COMPATIBLE</span>' 
      : `<span class="badge badge-amber">${escapeHtml(c.compatibility_verdict)}</span>`;

    const pkg = pkgMap[c.package_id] || {};
    const currVer = pkg.current_version || '-';

    return `
      <div class="candidate-card">
        <div class="candidate-header">
          <div class="candidate-title-group">
            <span class="candidate-name">${escapeHtml(c.package_id)}</span>
            <span class="code-pill" style="color: var(--accent-blue)">${escapeHtml(pkg.name || c.package_id)}</span>
            <span class="badge badge-blue">GA Production</span>
          </div>
          <div class="candidate-action-group">
            <button class="btn ${isReady ? 'btn-secondary' : 'btn-primary'}" onclick="triggerAction('apply', '${escapeHtml(c.package_id)}')" ${isReady ? 'disabled' : ''}>
              ${isReady ? 'Applied &bull; Ready for Review' : 'Review &amp; Apply Update'}
            </button>
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
            ${verdictBadge}
          </div>
        </div>

        <div class="triage-section">
          <div class="triage-header">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 14 14"/></svg>
            Gemini Compatibility Triage
          </div>
          <div class="triage-summary">${escapeHtml(c.changelog_summary)}</div>
        </div>

        <div class="candidate-footer">
          <div>
            <span style="color: var(--text-muted); font-size: 11px; text-transform: uppercase; font-weight: 600; margin-right: 6px;">Artifact URL:</span>
            <a href="${escapeHtml(c.download_url)}" target="_blank" class="candidate-url">${escapeHtml(c.download_url)}</a>
          </div>
        </div>
      </div>
    `;
  }).join('');
}

function renderPackages(packages) {
  const tbody = document.getElementById('packages-table-body');
  tbody.innerHTML = packages.map(p => {
    let badgeClass = 'badge-blue';
    let statusLabel = p.status || 'REGISTERED';
    if (p.status === 'REGISTERED') {
      badgeClass = 'badge-blue';
      statusLabel = 'REGISTERED';
    } else if (p.status === 'UPDATE_FOUND') {
      badgeClass = 'badge-blue';
      statusLabel = 'UPDATE_FOUND';
    } else if (p.status === 'READY_FOR_REVIEW') {
      badgeClass = 'badge-green';
      statusLabel = 'READY_FOR_REVIEW';
    } else if (p.status === 'UP_TO_DATE') {
      badgeClass = 'badge-green';
      statusLabel = 'UP-TO-DATE';
    } else if (p.status === 'BLOCKED' || p.status === 'BLOCKED_BY_RULE') {
      badgeClass = 'badge-red';
      statusLabel = 'BLOCKED';
    } else if (p.status === 'SNOOZED') {
      badgeClass = 'badge-amber';
      statusLabel = 'SNOOZED';
    } else if (p.status === 'OBSOLETE') {
      badgeClass = 'badge-amber';
      statusLabel = 'OBSOLETE';
    }

    const upVerHtml = (p.upstream_version && p.upstream_version !== '-')
      ? `<span class="code-pill">${escapeHtml(p.upstream_version)}</span>`
      : `<span style="color: var(--text-muted)">-</span>`;

    return `
      <tr>
        <td><span class="code-pill">${escapeHtml(p.package_id)}</span></td>
        <td><strong>${escapeHtml(p.name)}</strong></td>
        <td><span class="code-pill" style="color: var(--accent-green-light)">${escapeHtml(p.current_version)}</span></td>
        <td>${upVerHtml}</td>
        <td><span class="code-pill" style="color: var(--accent-amber)">${escapeHtml(p.upstream_type || 'github_release')}</span></td>
        <td><span class="badge ${badgeClass}">${escapeHtml(statusLabel)}</span></td>
        <td style="font-size: 12px; color: var(--text-secondary); line-height: 1.4; max-width: 380px;">${escapeHtml(p.qualification_summary || 'Baseline registered.')}</td>
        <td><a href="${escapeHtml(p.source_url)}" target="_blank" style="color: var(--accent-blue); text-decoration: none; font-size: 12px; word-break: break-all;">${escapeHtml(p.source_url)}</a></td>
      </tr>
    `;
  }).join('');
}

function renderInstances(instances) {
  const tbody = document.getElementById('instances-table-body');
  tbody.innerHTML = instances.map(inst => {
    const coupledDesc = inst.coupled_vars.length > 0 
      ? inst.coupled_vars.map(c => `<span class="code-pill" style="color: var(--accent-amber)">${c.variable_name} (${c.pattern})</span>`).join(', ')
      : '<span style="color: var(--text-muted)">Direct</span>';

    return `
      <tr>
        <td><span class="code-pill">${escapeHtml(inst.instance_id)}</span></td>
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
  tbody.innerHTML = rules.map(r => `
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

function renderBenchmarks(benchmarks) {
  const tbody = document.getElementById('benchmarks-table-body');
  if (!tbody) return;
  if (!benchmarks || benchmarks.length === 0) {
    tbody.innerHTML = '<tr><td colspan="6" style="color: var(--text-muted); text-align: center;">No benchmark test cases loaded from database.</td></tr>';
    return;
  }

  tbody.innerHTML = benchmarks.map(b => {
    const isPass = b.expected_verdict === 'PASSED' || b.expected_verdict === 'COMPATIBLE';
    const badgeClass = isPass ? 'badge-green' : 'badge-amber';
    return `
      <tr>
        <td><span class="code-pill">${escapeHtml(b.case_id)}</span></td>
        <td><span class="badge badge-blue">${escapeHtml(b.category)}</span></td>
        <td><span class="code-pill">${escapeHtml(b.package_id)}</span></td>
        <td><span class="code-pill" style="color: var(--accent-green-light)">${escapeHtml(b.test_version)}</span></td>
        <td><span class="badge ${badgeClass}">${escapeHtml(b.expected_verdict)}</span></td>
        <td style="color: var(--text-secondary); font-size: 12.5px;">${escapeHtml(b.description)}</td>
      </tr>
    `;
  }).join('');
}

function renderDynamicApplyButtons(candidates, packages) {
  const container = document.getElementById('dynamic-apply-buttons');
  if (!container) return;

  const pendingCandidates = (candidates || []).filter(c => c.status === 'UPDATE_FOUND' || c.status === 'QUALIFIED');
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

// Initial setup
document.addEventListener('DOMContentLoaded', () => {
  fetchState();
  setInterval(fetchState, 3000);
});
