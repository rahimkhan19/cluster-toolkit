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

    const elPkg = document.getElementById('metric-packages');
    if (elPkg) elPkg.textContent = stats.total_packages || latestPackages.length;

    const elInst = document.getElementById('metric-instances');
    if (elInst) elInst.textContent = stats.total_instances || latestInstances.length;

    const elRules = document.getElementById('metric-rules');
    if (elRules) elRules.textContent = stats.total_rules || latestRules.length;

    const elCand = document.getElementById('metric-candidates');
    if (elCand) elCand.textContent = pendingCount;
    
    const counterEl = document.getElementById('counter-candidates');
    if (counterEl) counterEl.textContent = pendingCount;

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
        badgeText.textContent = `${data.config.owner}/${data.config.repo_name} (${data.config.base_branch})`;
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

    const pkg = pkgMap[c.package_id] || {};
    const currVer = pkg.current_version || '-';

    const instances = (latestInstances || []).filter(inst => inst.package_id === c.package_id);
    const instCount = instances.length;
    const blueprintPillHtml = instCount > 0
      ? `<button class="btn-blueprint-pill" onclick="openBlueprintModal('${escapeHtml(c.package_id)}')" title="Click to view ${instCount} associated blueprint${instCount === 1 ? '' : 's'}">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
          <span>${instCount} Blueprint${instCount === 1 ? '' : 's'}</span>
        </button>`
      : '';

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
            ${c.pr_url ? `
              <a href="${escapeHtml(c.pr_url)}" target="_blank" class="btn btn-secondary" style="color: #22c55e; border-color: rgba(34, 197, 94, 0.4); text-decoration: none; display: inline-flex; align-items: center; gap: 6px;" title="View Pull Request on GitHub">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M13 6h3a2 2 0 0 1 2 2v7"/><line x1="6" y1="9" x2="6" y2="21"/></svg>
                <span>View PR #${escapeHtml(String(c.pr_url).split('/').pop())}</span>
              </a>
            ` : ''}
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
          </div>
        </div>

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
          ${instCount > 0 ? `
            <button class="btn-text-link" onclick="openBlueprintModal('${escapeHtml(c.package_id)}')" title="Inspect target blueprint files and variable couplings">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
              <span>View ${instCount} Affected Blueprint${instCount === 1 ? '' : 's'} &rarr;</span>
            </button>
          ` : ''}
        </div>
      </div>
    `;
  }).join('');
}

function renderPackages(packages, candidates) {
  const tbody = document.getElementById('packages-table-body');
  if (!tbody) return;
  const list = packages || latestPackages || [];
  if (list.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" style="color: var(--text-muted); text-align: center; padding: 24px;">No packages registered in datastore.</td></tr>';
    return;
  }
  const candList = candidates || latestCandidates || [];
  tbody.innerHTML = list.map(p => {
    // Determine single effective status for the package
    const activeCand = candList.find(c => c.package_id === p.package_id && ['UPDATE_FOUND', 'TESTING', 'READY_FOR_REVIEW'].includes(c.status));
    let effectiveStatus = p.status || 'REGISTERED';
    if (activeCand) {
      effectiveStatus = activeCand.status;
    }

    let badgeClass = 'badge-blue';
    let statusLabel = effectiveStatus;
    if (effectiveStatus === 'UPDATE_FOUND') {
      badgeClass = 'badge-blue';
      statusLabel = 'UPDATE_FOUND';
    } else if (effectiveStatus === 'READY_FOR_REVIEW') {
      badgeClass = 'badge-green';
      statusLabel = 'READY_FOR_REVIEW';
    } else if (effectiveStatus === 'TESTING') {
      badgeClass = 'badge-amber';
      statusLabel = 'TESTING';
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
      statusLabel = 'SNOOZED';
    } else if (effectiveStatus === 'OBSOLETE') {
      badgeClass = 'badge-amber';
      statusLabel = 'OBSOLETE';
    } else {
      badgeClass = 'badge-blue';
      statusLabel = 'REGISTERED';
    }

    const upVerHtml = (p.upstream_version && p.upstream_version !== '-')
      ? `<span class="code-pill">${escapeHtml(p.upstream_version)}</span>`
      : `<span style="color: var(--text-muted)">-</span>`;

    const instances = (latestInstances || []).filter(inst => inst.package_id === p.package_id);
    const instCount = instances.length;
    const blueprintBtnHtml = instCount > 0
      ? `<button class="btn-blueprint-pill" onclick="openBlueprintModal('${escapeHtml(p.package_id)}')" title="View ${instCount} associated blueprint${instCount === 1 ? '' : 's'}">
           <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
           <span>${instCount} blueprint${instCount === 1 ? '' : 's'}</span>
         </button>`
      : `<span style="color: var(--text-muted); font-size: 11px;">None</span>`;

    const rawSummary = p.qualification_summary || 'Baseline registered.';
    const isError = effectiveStatus === 'ERROR' || rawSummary.toLowerCase().includes('failed') || rawSummary.toLowerCase().includes('error:');
    let displaySummary = rawSummary;
    if (displaySummary.length > 95) {
      displaySummary = displaySummary.substring(0, 92) + '...';
    }

    const summaryTdHtml = isError
      ? `<td style="font-size: 12px; line-height: 1.4; max-width: 340px;" title="${escapeHtml(rawSummary)}">
           <span class="badge badge-red" style="font-size: 9px; padding: 1px 5px; margin-right: 4px; vertical-align: middle;">ERROR</span>
           <span style="color: var(--accent-red); vertical-align: middle;">${escapeHtml(displaySummary)}</span>
         </td>`
      : `<td style="font-size: 12px; color: var(--text-secondary); line-height: 1.4; max-width: 340px;" title="${escapeHtml(rawSummary)}">
           ${escapeHtml(displaySummary)}
         </td>`;

    return `
      <tr>
        <td><span class="code-pill">${escapeHtml(p.package_id)}</span></td>
        <td><strong>${escapeHtml(p.name)}</strong></td>
        <td><span class="code-pill" style="color: var(--accent-green-light)">${escapeHtml(p.current_version)}</span></td>
        <td>${upVerHtml}</td>
        <td><span class="code-pill" style="color: var(--accent-amber)">${escapeHtml(p.upstream_type || 'github_release')}</span></td>
        <td><span class="badge ${badgeClass}">${escapeHtml(statusLabel)}</span></td>
        <td>${blueprintBtnHtml}</td>
        ${summaryTdHtml}
        <td><a href="${escapeHtml(p.source_url)}" target="_blank" style="color: var(--accent-blue); text-decoration: none; font-size: 12px; word-break: break-all;">${escapeHtml(p.source_url)}</a></td>
      </tr>
    `;
  }).join('');
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

function openBlueprintModal(packageId) {
  const pkg = latestPackages.find(p => p.package_id === packageId) || { package_id: packageId, name: packageId };
  const instances = (latestInstances || []).filter(inst => inst.package_id === packageId);

  const titleEl = document.getElementById('modal-package-title');
  const subtitleEl = document.getElementById('modal-package-subtitle');
  const listEl = document.getElementById('modal-blueprint-list');
  const countEl = document.getElementById('modal-instance-count');

  if (titleEl) {
    titleEl.textContent = `Associated Blueprints: ${pkg.package_id}`;
  }
  if (subtitleEl) {
    subtitleEl.innerHTML = `<strong>${escapeHtml(pkg.name || pkg.package_id)}</strong> &bull; Current version: <code class="code-pill">${escapeHtml(pkg.current_version || '-')}</code>`;
  }
  if (countEl) {
    countEl.textContent = `${instances.length} blueprint instance${instances.length === 1 ? '' : 's'} configured`;
  }

  if (!instances || instances.length === 0) {
    listEl.innerHTML = `
      <div style="text-align: center; padding: 32px 20px; color: var(--text-muted);">
        No registered blueprint instances found for <code>${escapeHtml(packageId)}</code>.
      </div>
    `;
  } else {
    listEl.innerHTML = instances.map(inst => {
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

      return `
        <div class="blueprint-card">
          <div class="blueprint-card-header">
            <div class="blueprint-path-box">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
              <span>${escapeHtml(inst.blueprint_path)}</span>
            </div>
            <button class="btn-copy-path" onclick="copyBlueprintPath(this, '${escapeHtml(inst.blueprint_path)}')" title="Copy relative path to clipboard">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
              <span>Copy</span>
            </button>
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

function closeBlueprintModal(event) {
  if (event && event.target && event.target.id !== 'blueprint-modal' && !event.target.classList.contains('modal-close-btn') && !event.target.closest('.modal-close-btn') && event.target.tagName !== 'BUTTON') {
    return;
  }
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

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    const modal = document.getElementById('blueprint-modal');
    if (modal && modal.classList.contains('active')) {
      closeBlueprintModal();
    }
  }
});

// Initial setup
document.addEventListener('DOMContentLoaded', () => {
  fetchState();
  setInterval(fetchState, 3000);
});
