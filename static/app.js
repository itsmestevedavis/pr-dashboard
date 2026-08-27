
const CONFIG = window.PR_DASHBOARD_CONFIG;
const GROUPS = ['re_requested', 'new_commits', 'author_replied', 'untouched'];
const HEADERS = {
  re_requested: 'Re-review requested',
  new_commits: 'New commits',
  author_replied: 'Author replied',
  untouched: 'New',
};

const TABS = {
  incoming: {
    title: '📋 PRs awaiting your review',
    endpoint: '/api/prs',
    groups: ['re_requested', 'new_commits', 'author_replied', 'untouched'],
    headers: HEADERS,
    render: renderIncomingPR,
  },
  mine: {
    title: '🚀 My open PRs',
    endpoint: '/api/prs/mine',
    groups: ['approved', 'has_comments', 'not_reviewed_yet', 'draft'],
    headers: {
      approved: 'Approved — ready to merge',
      has_comments: 'Has comments to address',
      not_reviewed_yet: 'Not reviewed yet',
      draft: 'Drafts — not ready for review',
    },
    render: renderMyPR,
    subgroup: ticketKey,
  },
  tickets: {
    title: '🎫 My Jira tickets',
    endpoint: '/api/tickets',
    // groups/headers are set dynamically in load() (one column per status).
    groups: [],
    headers: {},
    groupKey: 'status_label',
    render: renderTicket,
  },
};

const _params = new URLSearchParams(location.search);
// Pre-merge deep links (?tab=reliability-stg / -prod) map onto the single
// Reliability tab, carrying their environment over.
const _LEGACY_EMBED_TABS = { 'reliability-stg': 'stg', 'reliability-prod': 'prod' };
const _tab = _LEGACY_EMBED_TABS[_params.get('tab')] ? 'reliability' : _params.get('tab');
let currentTab = ['mine', 'deployed', 'status', 'settings', 'tickets', 'team', 'cleanup', 'reliability'].includes(_tab) ? _tab : 'incoming';
let embedEnv = ['stg', 'prod'].includes(_params.get('env'))
  ? _params.get('env')
  : (_LEGACY_EMBED_TABS[_params.get('tab')] || 'stg');
let deployedState = {};  // environments map from /api/deployed, populated when mine tab loads

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// Only allow http(s) in hrefs. escapeHtml does NOT stop a javascript:/data: scheme,
// and check URLs come from GitHub commit-status targetUrl (settable by CI/collaborators).
function safeUrl(u) {
  try {
    const p = new URL(u);
    return (p.protocol === 'https:' || p.protocol === 'http:') ? u : '';
  } catch { return ''; }
}

function relativeTime(iso) {
  const t = new Date(iso).getTime();
  if (!t) return '';
  const diff = (Date.now() - t) / 1000;
  if (diff < 60) return 'just now';
  if (diff < 3600) return Math.floor(diff/60) + 'm ago';
  if (diff < 86400) return Math.floor(diff/3600) + 'h ago';
  if (diff < 604800) return Math.floor(diff/86400) + 'd ago';
  return new Date(iso).toLocaleDateString();
}

// Extract a Jira ticket slug (e.g. "CGP-96") so PRs for the same ticket can be
// grouped. The slug lives in the branch name by convention; fall back to the
// title. Returns '' when no ticket is present so such PRs render ungrouped.
function ticketKey(p) {
  const m = /\b([A-Z][A-Z0-9]+-\d+)\b/.exec(`${p.headRefName || ''} ${p.title || ''}`);
  return m ? m[1] : '';
}

function render(prs) {
  const content = document.getElementById('content');
  const tab = TABS[currentTab];
  if (!prs.length) {
    const emptyMsg = currentTab === 'mine' ? '🎉 No open PRs.'
      : currentTab === 'tickets' ? '🎉 No open tickets.'
      : '🎉 No PRs waiting. Inbox zero.';
    content.innerHTML = '<div class="empty">' + emptyMsg + '</div>';
    return;
  }
  const gk = tab.groupKey || 'status';
  const grouped = {};
  for (const g of tab.groups) grouped[g] = [];
  for (const p of prs) {
    if (grouped[p[gk]]) grouped[p[gk]].push(p);
  }
  let html = '';
  for (const g of tab.groups) {
    if (!grouped[g].length) continue;
    html += `<div class="group-header">${escapeHtml(tab.headers[g])}</div>`;
    html += renderGroupBody(grouped[g], tab);
  }
  content.innerHTML = html;
  // PR-card buttons are handled by the delegated #content listener (see initDelegation).
}

// Render the cards within one status group. When the tab opts into subgrouping
// (tab.subgroup), PRs sharing a key (e.g. a Jira ticket) are clustered under a
// sub-header. Clusters appear at the position of their first member, preserving
// the backend ordering; singletons and keyless PRs render inline as before.
function renderGroupBody(prs, tab) {
  if (!tab.subgroup) return prs.map(tab.render).join('');
  const counts = {};
  for (const p of prs) {
    const k = tab.subgroup(p);
    if (k) counts[k] = (counts[k] || 0) + 1;
  }
  const rendered = new Set();
  let html = '';
  for (const p of prs) {
    const k = tab.subgroup(p);
    if (!k || counts[k] < 2) { html += tab.render(p); continue; }
    if (rendered.has(k)) continue;
    rendered.add(k);
    const members = prs.filter(q => tab.subgroup(q) === k);
    html += `<div class="ticket-group">`
      + `<div class="ticket-header">${escapeHtml(k)}${ticketLink(k)} <span class="ticket-count">${members.length} PRs</span></div>`
      + members.map(tab.render).join('')
      + `</div>`;
  }
  return html;
}

// Link a Jira ticket key to its issue when a Jira site is configured.
function ticketLink(key) {
  const site = CONFIG.jira_site || '';
  if (!site) return '';
  const url = safeUrl(`https://${site}/browse/${key}`);
  return url ? ` <a class="ticket-link" href="${escapeHtml(url)}" target="_blank" rel="noopener">↗</a>` : '';
}

function renderIncomingPR(p) {
  const detail = p.status_detail
    ? `<div class="pr-detail">${escapeHtml(p.status_detail)}</div>` : '';
  const isReReview = ['re_requested', 'new_commits', 'author_replied'].includes(p.status);
  const actionBtn = isReReview
    ? `<button class="btn-re-review" type="button" title="Check whether your previous comments were addressed">Re-review</button>`
    : `<button class="btn-review" type="button">Review</button>`;
  return `
  <div class="pr" data-number="${p.number}" data-repo="${escapeHtml(p.repository)}" data-url="${escapeHtml(p.url)}">
    <div class="pr-main">
      <div class="pr-meta">${escapeHtml(p.repository)} · #${p.number}<span class="badge badge-${p.status}">${escapeHtml(p.status_label)}</span></div>
      <div class="pr-title"><a href="${escapeHtml(safeUrl(p.url))}" target="_blank" rel="noopener">${escapeHtml(p.title)}</a></div>
      <div class="pr-sub">by ${escapeHtml(p.author)} · updated ${relativeTime(p.updatedAt)}</div>
      ${detail}
    </div>
    <div class="pr-actions">
      <a class="btn-open" href="${escapeHtml(safeUrl(p.url))}" target="_blank" rel="noopener">Open ↗</a>
      ${actionBtn}
    </div>
  </div>`;
}

// Compact per-check summary line: greens collapse to a count; pending/failed
// checks are named and linked to their logs. Returns '' when there are no checks.
function renderChecks(p) {
  const c = p.checks;
  if (!c) return '';
  const total = c.passed + (c.pending || []).length + (c.failed || []).length;
  if (!total) return '';
  const linkNames = (list) => list.map(ck => {
    const href = safeUrl(ck.url);
    return href
      ? `<a class="chk-name" href="${escapeHtml(href)}" target="_blank" rel="noopener">${escapeHtml(ck.name)}</a>`
      : `<span class="chk-name">${escapeHtml(ck.name)}</span>`;
  }).join(', ');
  const parts = [];
  if (c.passed) parts.push(`<span class="chk chk-pass">✅ ${c.passed}</span>`);
  if ((c.pending || []).length) parts.push(`<span class="chk chk-pending">🔄 ${linkNames(c.pending)}</span>`);
  if ((c.failed || []).length) parts.push(`<span class="chk chk-fail">❌ ${linkNames(c.failed)}</span>`);
  if (c.truncated) parts.push(`<span class="chk">…</span>`);
  return `<div class="pr-checks">${parts.join(' · ')}</div>`;
}

function renderMyPR(p) {
  const commenters = p.active_commenters && p.active_commenters.length
    ? `<div class="pr-detail">From: ${escapeHtml(p.active_commenters.join(', '))}</div>`
    : '';
  // "Behind" comes from the server's compare-based behind_by, not from
  // mergeStateStatus: GitHub only reports BEHIND when an up-to-date-branch rule
  // makes it the governing blocker, so a branch that is behind *and* blocked by
  // reviews/checks would otherwise hide that it can still be rebased.
  const needsRebase = (p.behind_by || 0) > 0;
  const hasConflicts = p.merge_state_status === 'DIRTY';
  const ciBlocked = ['FAILURE', 'ERROR'].includes(p.check_state);
  const reviewBlocked = p.review_decision === 'CHANGES_REQUESTED';

  // The infra-fix button surfaces whenever the branch is behind/conflicted or CI
  // is failing — independent of review status, so a PR that also has comments
  // still offers a way to fix the pipeline. Rebase takes precedence: a behind or
  // conflicted branch must be rebased before its CI result means anything.
  let infraFixBtn = '';
  if (needsRebase || hasConflicts) {
    const why = needsRebase ? 'Needs rebase' : 'Has conflicts';
    infraFixBtn = `<button class="btn-rebase" type="button" title="Fix: ${escapeHtml(why)}">Rebase</button>`;
  } else if (ciBlocked) {
    infraFixBtn = `<button class="btn-fix-pipeline" type="button" title="Fix: CI failing">Fix pipeline</button>`;
  }

  let actionBtn = '';
  if (p.status === 'approved') {
    const blocked = ciBlocked || reviewBlocked || needsRebase || hasConflicts;
    const reasons = [
      ciBlocked && 'CI failing',
      reviewBlocked && 'Changes requested',
      needsRebase && 'Needs rebase',
      hasConflicts && 'Has conflicts',
    ].filter(Boolean);
    const blockReason = reasons.join(' · ');
    const mergeBtn = `<button class="btn-merge${blocked ? ' btn-merge-blocked' : ''}" type="button" ${blocked ? `disabled title="${escapeHtml(blockReason)}"` : ''}>Merge</button>`;
    actionBtn = infraFixBtn + mergeBtn;
  } else if (p.status === 'has_comments') {
    actionBtn = infraFixBtn + `<button class="btn-address" type="button">Address</button>`;
  } else {
    // not_reviewed_yet etc. — still expose an infra fix if the pipeline/branch is broken.
    actionBtn = infraFixBtn;
  }
  const deployTarget = CONFIG.deploy_target || '';
  const deployWorkflow = deployTarget && (CONFIG.deploy_targets || {})[p.repository]?.[deployTarget];
  // Deploys are preview-branch pushes, so the tracked run's branch is the
  // server-derived preview/… name (p.previewBranch), not the PR's own head branch.
  const alreadyDeployed = deployTarget && (deployedState[deployTarget] || [])
    .some(d => d.repo === p.repository && d.branch === p.previewBranch && d.conclusion === 'success');
  const deployControls = deployWorkflow
    ? (alreadyDeployed
        ? `<button class="btn-deploy btn-deploy-live" type="button" data-env="${escapeHtml(deployTarget)}" title="${escapeHtml(p.previewBranch)} is live — click to re-deploy">✅ ${escapeHtml(deployTarget.toUpperCase())}</button>`
        : `<button class="btn-deploy" type="button" data-env="${escapeHtml(deployTarget)}">Deploy to ${escapeHtml(deployTarget.toUpperCase())}</button>`)
    : '';

  // Nudge / #Channel buttons
  const mode = p.nudge_mode || '';
  const pickable = (CONFIG.fresh_reviewers || []);
  const defaultTemplate = mode === 're_review' ? 're_review' : 'fresh';
  const allTargets = (mode === 're_review' && (p.nudge_targets || []).length)
    ? p.nudge_targets
    : pickable;
  const templateBtns = `
    <div class="nudge-template" role="group" aria-label="Slack template">
      <button type="button" class="nudge-template-btn" data-template="fresh"
        aria-pressed="${defaultTemplate === 'fresh'}"
        title="Friendly first-look DM: could you take a look">Fresh</button>
      <button type="button" class="nudge-template-btn" data-template="re_review"
        aria-pressed="${defaultTemplate === 're_review'}"
        title="Re-review DM: I've addressed your comments">Re-review</button>
    </div>`;
  const targetBtns = pickable.length
    ? pickable.map(u => `<button class="btn-nudge-target" type="button" data-user="${escapeHtml(u)}">${escapeHtml(u)}</button>`).join('')
    : `<div class="nudge-section-label">No FRESH_REVIEWERS configured</div>`;
  const allBtn = allTargets.length
    ? `<button class="btn-nudge-all" type="button" data-targets="${escapeHtml(allTargets.join(','))}">DM all (${allTargets.length})</button>`
    : '';
  const nudgeBtn = `
    <div class="nudge-split">
      <button class="btn-nudge" type="button" aria-haspopup="true" aria-expanded="false" title="Pick who to DM on Slack">Nudge</button>
      <div class="nudge-menu" hidden>
        ${templateBtns}
        <div class="nudge-section-label">DM individually</div>
        ${targetBtns}
        ${allBtn}
      </div>
    </div>`;
  let teamsBtn = '';
  if (mode === 'fresh' && CONFIG.teams && CONFIG.teams.length > 0) {
    const teamItems = CONFIG.teams.map(t =>
      `<button class="menu-item btn-nudge-team" type="button"
         data-team-name="${escapeHtml(t.name)}"
         data-team-reviewers="${escapeHtml(JSON.stringify(t.reviewers))}"
         title="Ask ${escapeHtml(t.name)} reviewers on Slack">${escapeHtml(t.name)}</button>`
    ).join('');
    teamsBtn = `<div class="nudge-teams-split">
      <button class="btn-nudge-teams-caret" type="button" aria-label="Nudge a team" aria-haspopup="true" aria-expanded="false">Teams ▾</button>
      <div class="nudge-teams-menu" hidden>${teamItems}</div>
    </div>`;
  }
  const defaultChannelLabel = (CONFIG.fresh_reviewers || []).join(' and ') || 'the team';
  let channelBtn = '';
  if (CONFIG.team_channel_id) {
    if (CONFIG.teams && CONFIG.teams.length > 0) {
      const teamItems = CONFIG.teams.map(t =>
        `<button class="menu-item btn-channel-team" type="button"
           data-team-name="${escapeHtml(t.name)}"
           data-team-channel-id="${escapeHtml(t.channel_id)}"
           data-team-reviewers="${escapeHtml(JSON.stringify(t.reviewers))}"
           title="Post in ${escapeHtml(t.name)} channel">${escapeHtml(t.name)}</button>`
      ).join('');
      channelBtn = `<div class="channel-split">
        <button class="btn-channel" type="button" title="Post in team channel tagging ${escapeHtml(defaultChannelLabel)}">#dev channel</button>
        <button class="btn-channel-caret" type="button" aria-label="Post in a different team's channel" aria-haspopup="true" aria-expanded="false">▾</button>
        <div class="channel-menu" hidden>${teamItems}</div>
      </div>`;
    } else {
      channelBtn = `<button class="btn-channel" type="button" title="Post in team channel tagging ${escapeHtml(defaultChannelLabel)}">#dev channel</button>`;
    }
  }

  return `
  <div class="pr"
       data-number="${p.number}"
       data-repo="${escapeHtml(p.repository)}"
       data-url="${escapeHtml(p.url)}"
       data-title="${escapeHtml(p.title)}"
       data-head="${escapeHtml(p.headRefName)}"
       data-base="${escapeHtml(p.baseRefName)}"
       data-method="${escapeHtml(p.defaultMergeMethod)}">
    <div class="pr-main">
      <div class="pr-meta">${escapeHtml(p.repository)} · #${p.number}<span class="badge badge-${p.status}">${escapeHtml(p.status_label)}</span>${needsRebase ? '<span class="badge-warning">⚠ Needs rebase</span>' : hasConflicts ? '<span class="badge-warning">⚠ Has conflicts</span>' : ''}</div>
      <div class="pr-title"><a href="${escapeHtml(safeUrl(p.url))}" target="_blank" rel="noopener">${escapeHtml(p.title)}</a></div>
      <div class="pr-sub">updated ${relativeTime(p.updatedAt)}</div>
      ${renderChecks(p)}
      ${commenters}
    </div>
    <div class="pr-actions">
      <a class="btn-open" href="${escapeHtml(safeUrl(p.url))}" target="_blank" rel="noopener">Open ↗</a>
      ${deployControls}
      ${p.status === 'draft' ? '' : channelBtn + nudgeBtn + teamsBtn}
      ${actionBtn}
    </div>
  </div>`;
}

// Distinct status names present, ordered by category (To Do before In Progress)
// then alphabetically — used as the Tickets columns when no filter is saved.
function ticketStatusOrder(tickets) {
  const catByName = {};
  for (const t of tickets) catByName[t.status_label] = t.status_category;
  const rank = { new: 0, indeterminate: 1 };
  return Object.keys(catByName).sort((a, b) =>
    ((rank[catByName[a]] ?? 2) - (rank[catByName[b]] ?? 2)) || a.localeCompare(b));
}

function renderTicket(t) {
  const priority = t.priority ? `<span class="badge-meta">${escapeHtml(t.priority)}</span>` : '';
  const type = t.type ? `<span class="badge-meta">${escapeHtml(t.type)}</span>` : '';
  return `
  <div class="pr" data-key="${escapeHtml(t.key)}" data-url="${escapeHtml(t.url)}">
    <div class="pr-main">
      <div class="pr-meta">${escapeHtml(t.key)}<span class="badge badge-${escapeHtml(t.status_category)}">${escapeHtml(t.status_label)}</span>${type}${priority}</div>
      <div class="pr-title"><a href="${escapeHtml(safeUrl(t.url))}" target="_blank" rel="noopener">${escapeHtml(t.summary)}</a></div>
      <div class="pr-sub">updated ${relativeTime(t.updatedAt)}</div>
    </div>
    <div class="pr-actions">
      <a class="btn-open" href="${escapeHtml(safeUrl(t.url))}" target="_blank" rel="noopener">Open ↗</a>
      <button class="btn-move" type="button" title="Move to another status">Move ▾</button>
    </div>
  </div>`;
}

// ---- PR-card action handlers ----------------------------------------------
// Buttons are wired via one delegated listener on #content (see initDelegation),
// so every handler receives the clicked button and re-rendered buttons (e.g. the
// "…again" buttons in stopped branches) work without re-attaching listeners.

function cardCtx(btn) {
  const card = btn.closest('.pr');
  return {
    card,
    number: parseInt(card.dataset.number, 10),
    repo: card.dataset.repo,
    url: card.dataset.url,
  };
}

// POST to `endpoint`; on success switch the card to its running state and stream.
// Shows a toast and bails on failure. Shared by every job-starting handler.
async function startJob(ctx, endpoint, body, kind, runningLabel, finish) {
  try {
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const b = await res.json().catch(() => ({}));
      throw new Error(b.error || ('HTTP ' + res.status));
    }
  } catch (e) {
    toast(`Failed to start: ${e.message}`, true);
    return;
  }
  setRunning(ctx.card, runningLabel, kind);
  streamJob(ctx.card, kind, ctx.repo, ctx.number, ctx.url, finish);
}

function onReview(btn) {
  const ctx = cardCtx(btn);
  startJob(ctx, '/api/review', { number: ctx.number, repo: ctx.repo },
    'review', 'Reviewing…', finishReview);
}

function onReReview(btn) {
  const ctx = cardCtx(btn);
  startJob(ctx, '/api/re-review', { number: ctx.number, repo: ctx.repo },
    're_review', 'Re-reviewing…', finishReReview);
}

function onMerge(btn) {
  const ctx = cardCtx(btn);
  if (!confirm(`Merge ${ctx.repo} #${ctx.number}?`)) return;
  startJob(ctx, '/api/merge',
    { number: ctx.number, repo: ctx.repo, defaultMergeMethod: ctx.card.dataset.method },
    'merge', 'Merging…', finishMerge);
}

function onAddress(btn) {
  const ctx = cardCtx(btn);
  startJob(ctx, '/api/address',
    { number: ctx.number, repo: ctx.repo, headRefName: ctx.card.dataset.head },
    'address', 'Addressing…', finishAddress);
}

function onFixPipeline(btn) {
  const ctx = cardCtx(btn);
  startJob(ctx, '/api/fix-pipeline',
    { number: ctx.number, repo: ctx.repo, headRefName: ctx.card.dataset.head },
    'fix_pipeline', 'Fixing pipeline…', finishFixPipeline);
}

// ── Nudge / #Channel menu helpers ──────────────────────────────────────────

function closeAllNudgeMenus() {
  for (const menu of document.querySelectorAll('.nudge-menu')) {
    menu.hidden = true;
  }
  for (const btn of document.querySelectorAll('.btn-nudge')) {
    btn.setAttribute('aria-expanded', 'false');
  }
}

function closeAllNudgeTeamsMenus() {
  for (const menu of document.querySelectorAll('.nudge-teams-menu')) menu.hidden = true;
  for (const caret of document.querySelectorAll('.btn-nudge-teams-caret')) caret.setAttribute('aria-expanded', 'false');
}

function closeAllChannelMenus() {
  for (const menu of document.querySelectorAll('.channel-menu')) menu.hidden = true;
  for (const caret of document.querySelectorAll('.btn-channel-caret')) caret.setAttribute('aria-expanded', 'false');
}

function onNudgeTeamsCaret(btn) {
  const menu = btn.parentElement.querySelector('.nudge-teams-menu');
  const willOpen = menu.hidden;
  closeAllNudgeMenus();
  closeAllChannelMenus();
  closeAllNudgeTeamsMenus();
  if (willOpen) {
    menu.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
  }
}

function onChannelCaret(btn) {
  const menu = btn.parentElement.querySelector('.channel-menu');
  const willOpen = menu.hidden;
  closeAllNudgeMenus();
  closeAllNudgeTeamsMenus();
  closeAllChannelMenus();
  if (willOpen) {
    menu.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
  }
}

// ── Nudge / #Channel core handlers (delegation-adapted: take btn element) ──

function onNudge(btn) {
  const menu = btn.parentElement.querySelector('.nudge-menu');
  if (!menu) return;
  const willOpen = menu.hidden;
  closeAllNudgeTeamsMenus();
  closeAllChannelMenus();
  closeAllNudgeMenus();
  if (willOpen) {
    menu.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
  }
}

function onNudgeTemplate(btn) {
  const menu = btn.closest('.nudge-menu');
  if (!menu) return;
  for (const b of menu.querySelectorAll('.nudge-template-btn')) {
    b.setAttribute('aria-pressed', b === btn ? 'true' : 'false');
  }
}

function selectedTemplate(card) {
  const pressed = card.querySelector('.nudge-template-btn[aria-pressed="true"]');
  return pressed ? pressed.dataset.template : 'fresh';
}

async function fireNudge(card, reviewers, mode, runningLabel) {
  const number = parseInt(card.dataset.number, 10);
  const repo = card.dataset.repo;
  const url = card.dataset.url;
  const title = card.dataset.title || '';
  try {
    const res = await fetch('/api/nudge', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ number, repo, url, title, reviewers, mode }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || ('HTTP ' + res.status));
    }
  } catch (e) {
    toast(`Failed to start: ${e.message}`, true);
    return;
  }
  setRunning(card, runningLabel, 'nudge');
  streamJob(card, 'nudge', repo, number, url, finishNudge);
}

async function onNudgeTarget(btn) {
  const card = btn.closest('.pr');
  const user = btn.dataset.user;
  if (!user) return;
  const mode = selectedTemplate(card);
  const label = mode === 're_review'
    ? `Nudge ${user} on Slack to re-review?`
    : `Ask ${user} on Slack to review this PR?`;
  if (!confirm(label)) return;
  closeAllNudgeMenus();
  await fireNudge(card, [user], mode, `Nudging ${user}…`);
}

async function onNudgeAll(btn) {
  const card = btn.closest('.pr');
  const targets = (btn.dataset.targets || '').split(',').map(s => s.trim()).filter(Boolean);
  if (!targets.length) {
    toast('No one to nudge.');
    return;
  }
  const mode = selectedTemplate(card);
  const label = mode === 're_review'
    ? `Nudge on Slack to re-review: ${targets.join(', ')}?`
    : `Ask ${targets.join(' and ')} on Slack to review this PR?`;
  if (!confirm(label)) return;
  closeAllNudgeMenus();
  await fireNudge(card, targets, mode, 'Nudging…');
}

function finishNudge(card, url, data) {
  const actions = card.querySelector('.pr-actions');
  let cls = 'failed', label = '❌ Failed';
  if (data.status === 'done') {
    if (data.result === 'No DMs sent' || data.result === 'Channel post failed') {
      cls = 'commented'; label = 'ℹ ' + data.result;
    } else {
      cls = 'approved'; label = '✅ ' + data.result;
    }
  }
  actions.innerHTML = `
    <span class="review-status ${cls}">${escapeHtml(label)}</span>
    <a class="btn-open" href="${escapeHtml(url)}" target="_blank" rel="noopener">Open PR ↗</a>
  `;
}

async function onChannelPing(btn) {
  const card = btn.closest('.pr');
  const number = parseInt(card.dataset.number, 10);
  const repo = card.dataset.repo;
  const url = card.dataset.url;
  const title = card.dataset.title || '';
  const targets = (CONFIG.fresh_reviewers || []).slice();
  if (!targets.length) {
    toast('No FRESH_REVIEWERS configured — set them in .env.', true);
    return;
  }
  if (!CONFIG.team_channel_id) {
    toast('No TEAM_CHANNEL_ID configured — set it in .env.', true);
    return;
  }
  if (!confirm(`Post in team channel tagging ${targets.join(' and ')} to review this PR?`)) return;
  try {
    const res = await fetch('/api/nudge', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ number, repo, url, title, reviewers: targets, mode: 'channel' }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || ('HTTP ' + res.status));
    }
  } catch (e) {
    toast(`Failed to start: ${e.message}`, true);
    return;
  }
  setRunning(card, 'Posting in channel…', 'nudge');
  streamJob(card, 'nudge', repo, number, url, finishNudge);
}

async function onNudgeTeam(btn) {
  const card = btn.closest('.pr');
  const number = parseInt(card.dataset.number, 10);
  const repo = card.dataset.repo;
  const url = card.dataset.url;
  const title = card.dataset.title || '';
  const teamName = btn.dataset.teamName || 'team';
  const reviewers = JSON.parse(btn.dataset.teamReviewers || '[]');
  closeAllNudgeMenus();
  closeAllNudgeTeamsMenus();
  if (!reviewers.length) {
    toast(`No reviewers configured for team "${teamName}".`, true);
    return;
  }
  if (!confirm(`Ask ${reviewers.join(' and ')} (${teamName}) on Slack to review this PR?`)) return;
  try {
    const res = await fetch('/api/nudge', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ number, repo, url, title, reviewers, mode: 'fresh' }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || ('HTTP ' + res.status));
    }
  } catch (e) {
    toast(`Failed to start: ${e.message}`, true);
    return;
  }
  setRunning(card, 'Nudging…', 'nudge');
  streamJob(card, 'nudge', repo, number, url, finishNudge);
}

async function onChannelTeam(btn) {
  const card = btn.closest('.pr');
  const number = parseInt(card.dataset.number, 10);
  const repo = card.dataset.repo;
  const url = card.dataset.url;
  const title = card.dataset.title || '';
  const teamName = btn.dataset.teamName || 'team';
  const channelId = btn.dataset.teamChannelId || '';
  const reviewers = JSON.parse(btn.dataset.teamReviewers || '[]');
  closeAllChannelMenus();
  closeAllNudgeTeamsMenus();
  if (!channelId) {
    toast(`No channel_id configured for team "${teamName}".`, true);
    return;
  }
  if (!reviewers.length) {
    toast(`No reviewers configured for team "${teamName}".`, true);
    return;
  }
  if (!confirm(`Post in ${teamName} channel tagging ${reviewers.join(' and ')}?`)) return;
  try {
    const res = await fetch('/api/nudge', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ number, repo, url, title, reviewers, mode: 'channel', channel_id: channelId }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || ('HTTP ' + res.status));
    }
  } catch (e) {
    toast(`Failed to start: ${e.message}`, true);
    return;
  }
  setRunning(card, 'Posting in channel…', 'nudge');
  streamJob(card, 'nudge', repo, number, url, finishNudge);
}

function onRebase(btn) {
  const ctx = cardCtx(btn);
  startJob(ctx, '/api/rebase',
    { number: ctx.number, repo: ctx.repo,
      headRefName: ctx.card.dataset.head, baseRefName: ctx.card.dataset.base },
    'rebase', 'Rebasing…', finishRebase);
}

// Shared by the My-PRs Deploy button and the Deployed tab. The server derives
// the pushed preview/… branch (app/deploy.py preview_branch) and returns it.
function confirmDeploy(repo, headRef, env) {
  return confirm(`Deploy ${repo} (${headRef}) to ${env.toUpperCase()}?\n\nPushes a preview/… branch — replaces whatever is on the box.`);
}

async function postDeploy(repo, headRef, env) {
  const res = await fetch('/api/deploy', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ repo, env, head_ref: headRef }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || ('HTTP ' + res.status));
  return body;
}

async function onDeploy(btn) {
  const card = btn.closest('.pr');
  const repo = card.dataset.repo;
  const headRef = card.dataset.head;
  const env = btn.dataset.env;

  if (!confirmDeploy(repo, headRef, env)) return;

  btn.disabled = true;
  btn.textContent = 'Pushing…';
  try {
    const body = await postDeploy(repo, headRef, env);
    btn.textContent = 'Pushed ✓';
    btn.title = `${body.branch || ''} pushed`;
    btn.style.cssText = 'background:#238636;color:#fff;';
    setTimeout(() => { btn.textContent = 'Deploy'; btn.style.cssText = ''; btn.disabled = false; }, 4000);
  } catch (e) {
    toast(`Deploy failed: ${e.message}`, true);
    btn.textContent = 'Deploy';
    btn.disabled = false;
  }
}

// ---- card terminal-state rendering ----------------------------------------

// Final (done/failed) state: a status pill + Open-PR link.
function setFinalStatus(card, cls, label, url) {
  card.querySelector('.pr-actions').innerHTML =
    `<span class="review-status ${cls}">${escapeHtml(label)}</span>`
    + `<a class="btn-open" href="${escapeHtml(safeUrl(url))}" target="_blank" rel="noopener">Open PR ↗</a>`;
}

// Stopped state: pill + optional "…again" button (delegation re-wires it) + Open-PR.
function setStoppedStatus(card, url, againBtn) {
  card.querySelector('.pr-actions').innerHTML =
    `<span class="review-status stopped">⏹ Stopped</span>`
    + (againBtn || '')
    + `<a class="btn-open" href="${escapeHtml(safeUrl(url))}" target="_blank" rel="noopener">Open PR ↗</a>`;
}

function finishMerge(card, url, data) {
  const ok = data.status === 'done' && data.result === 'merged';
  setFinalStatus(card, ok ? 'merged' : 'failed', ok ? '✅ Merged' : '❌ Merge failed', url);
}

function finishAddress(card, url, data) {
  if (data.status === 'stopped') {
    setStoppedStatus(card, url, `<button class="btn-address" type="button">Address again</button>`);
    return;
  }
  let cls = 'failed', label = '❌ Failed';
  if (data.status === 'done') {
    if (data.result === 'No action') { cls = 'commented'; label = 'ℹ No action'; }
    else if (data.result === 'Replied only') { cls = 'commented'; label = '💬 Replied only'; }
    else { cls = 'approved'; label = '✅ ' + data.result; }
  }
  setFinalStatus(card, cls, label, url);
}

function finishFixPipeline(card, url, data) {
  if (data.status === 'stopped') { setStoppedStatus(card, url); return; }
  const ok = data.status === 'done';
  setFinalStatus(card, ok ? 'approved' : 'failed', ok ? '✅ Fix pushed' : '❌ Failed', url);
}

function finishRebase(card, url, data) {
  if (data.status === 'stopped') { setStoppedStatus(card, url); return; }
  const ok = data.status === 'done';
  setFinalStatus(card, ok ? 'approved' : 'failed', ok ? '✅ Rebased & pushed' : '❌ Rebase failed', url);
}

// review and re-review share identical terminal logic (only the "again" button differs).
function finishReviewLike(card, url, data, againBtn) {
  if (data.status === 'stopped') {
    setStoppedStatus(card, url, againBtn);
    return;
  }
  let cls = 'failed', label = '❌ Failed';
  if (data.status === 'done') {
    if (data.result === 'approved') {
      cls = 'approved'; label = '✅ Approved';
    } else if ((data.result || '').startsWith('commented:')) {
      const n = data.result.split(':')[1];
      cls = 'commented';
      label = `💬 ${n} pending comment${n === '1' ? '' : 's'} left`;
    } else {
      cls = 'commented'; label = 'ℹ Done';
    }
  }
  setFinalStatus(card, cls, label, url);
}

function finishReview(card, url, data) {
  finishReviewLike(card, url, data, `<button class="btn-review" type="button">Review again</button>`);
}

function finishReReview(card, url, data) {
  finishReviewLike(card, url, data, `<button class="btn-re-review" type="button">Re-review again</button>`);
}

function setRunning(card, label, kind) {
  const main = card.querySelector('.pr-main');
  const actions = card.querySelector('.pr-actions');
  let panel = main.querySelector('.review-log');
  if (!panel) {
    panel = document.createElement('div');
    panel.className = 'review-log';
    main.appendChild(panel);
  } else {
    panel.innerHTML = '';
  }
  // label and kind are client-supplied literals; escaped defensively before insertion.
  // The Stop button is wired via the delegated #content listener.
  const stopBtn = kind
    ? `<button class="btn-stop" data-kind="${escapeHtml(kind)}">Stop</button>`
    : '';
  actions.innerHTML = `<span class="review-status running"><span class="spinner"></span>${escapeHtml(label)}</span>${stopBtn}`;
}

function appendLogLine(card, text, cls) {
  const panel = card.querySelector('.review-log');
  if (!panel) return;
  const line = document.createElement('div');
  line.className = 'review-log-line' + (cls ? ' ' + cls : '');
  line.textContent = text;
  panel.appendChild(line);
  panel.scrollTop = panel.scrollHeight;
}

function streamJob(card, kind, repo, number, url, finishLabel) {
  const params = new URLSearchParams({ kind, repo, number: String(number) });
  const es = new EventSource(`/api/job/stream?${params}`);
  es.addEventListener('message', (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }
    if (data.type === 'done') {
      es.close();
      finishLabel(card, url, data);
      return;
    }
    if (data.type === 'line' && data.text) {
      appendLogLine(card, data.text);
    }
  });
  es.addEventListener('error', () => {
    es.close();
    const actions = card.querySelector('.pr-actions');
    if (actions && !actions.querySelector('.btn-open')) {
      setFinalStatus(card, 'failed', '⚠ Stream lost', url);
    }
  });
}

async function onStop(btn) {
  const card = btn.closest('.pr');
  const number = parseInt(card.dataset.number, 10);
  const repo = card.dataset.repo;
  const kind = btn.dataset.kind;
  btn.disabled = true;
  btn.textContent = 'Stopping…';
  try {
    await fetch('/api/job/stop', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ number, repo, kind }),
    });
  } catch (e) {
    btn.disabled = false;
    btn.textContent = 'Stop';
  }
}

// Tickets: fetch the issue's available transitions, swap the Move button for a
// <select>, and POST the chosen transition. Refreshes the list on success.
async function onMove(btn) {
  const card = btn.closest('.pr');
  const key = card.dataset.key;
  btn.disabled = true;
  let transitions;
  try {
    const res = await fetch('/api/tickets/transitions?key=' + encodeURIComponent(key));
    if (!res.ok) throw new Error('HTTP ' + res.status);
    transitions = (await res.json()).transitions || [];
  } catch (e) {
    toast('Failed to load transitions: ' + e.message, true);
    btn.disabled = false;
    return;
  }
  if (!transitions.length) {
    toast('No transitions available', true);
    btn.disabled = false;
    return;
  }
  const sel = document.createElement('select');
  sel.className = 'ticket-move';
  sel.innerHTML = '<option value="" disabled selected>Move to…</option>'
    + transitions.map(t => `<option value="${escapeHtml(t.id)}">${escapeHtml(t.name)}</option>`).join('');
  let submitting = false;
  sel.addEventListener('change', async () => {
    const transitionId = sel.value;
    if (!transitionId || submitting) return;
    submitting = true;
    sel.disabled = true;
    try {
      const res = await fetch('/api/tickets/transition', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ key, transitionId }),
      });
      if (!res.ok) {
        const b = await res.json().catch(() => ({}));
        throw new Error(b.error || ('HTTP ' + res.status));
      }
    } catch (e) {
      toast('Transition failed: ' + e.message, true);
      submitting = false;
      sel.disabled = false;
      return;
    }
    toast('Moved ' + key);
    load(true);
  });
  btn.replaceWith(sel);
}

// One delegated click listener for all PR-card buttons (attached once to #content).
// Class tokens are exact-match, so .btn-review and .btn-re-review never collide.
const CARD_ACTIONS = {
  'btn-review': onReview,
  'btn-re-review': onReReview,
  'btn-merge': onMerge,
  'btn-address': onAddress,
  'btn-fix-pipeline': onFixPipeline,
  'btn-rebase': onRebase,
  'btn-deploy': onDeploy,
  'btn-stop': onStop,
  'btn-move': onMove,
  'btn-nudge': onNudge,
  'btn-nudge-target': onNudgeTarget,
  'btn-nudge-all': onNudgeAll,
  'nudge-template-btn': onNudgeTemplate,
  'btn-channel': onChannelPing,
  'btn-nudge-teams-caret': onNudgeTeamsCaret,
  'btn-channel-caret': onChannelCaret,
  'btn-nudge-team': onNudgeTeam,
  'btn-channel-team': onChannelTeam,
  'btn-standup-generate': onGenerateStandup,
  'embed-env-btn': onEmbedEnv,
};

function onContentClick(ev) {
  for (const cls in CARD_ACTIONS) {
    const btn = ev.target.closest('.' + cls);
    if (btn) { CARD_ACTIONS[cls](btn); return; }
  }
}

function toast(msg, error) {
  const el = document.createElement('div');
  el.className = 'toast' + (error ? ' error' : '');
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, 3000);
}

const TAB_TITLES = { incoming: '📋 PRs awaiting your review', mine: '🚀 My open PRs', deployed: '🚢 Currently deployed', status: '⚙️ App status', settings: '⚙️ Settings', tickets: '🎫 My Jira tickets', team: '👥 Team', cleanup: '🧹 Branch cleanup', reliability: '📈 Reliability' };

// Reliability dashboards embedded as iframes, served same-origin via the
// server's /embed/ reverse proxy (app/http/embed_proxy.py). A direct http://
// iframe would be upgraded to https:// by Firefox's HTTPS-First and hang —
// these hosts have no TLS listener. One tab; env picked by the toggle.
const EMBED_ENV_URLS = {
  stg: '/embed/reliability-stg/',
  prod: '/embed/reliability-prod/index.html',
};

function renderEmbed() {
  const buttons = Object.keys(EMBED_ENV_URLS).map(env =>
    `<button class="embed-env-btn" type="button" data-env="${env}" aria-pressed="${env === embedEnv}">${env.toUpperCase()}</button>`
  ).join('');
  document.getElementById('content').innerHTML =
    `<div class="embed-toolbar" role="group" aria-label="Environment">${buttons}</div>`
    + `<iframe class="embed-frame" src="${escapeHtml(EMBED_ENV_URLS[embedEnv])}" title="Reliability — ${embedEnv}"></iframe>`;
}

function onEmbedEnv(btn) {
  const env = btn.dataset.env;
  if (!EMBED_ENV_URLS[env] || env === embedEnv) return;
  embedEnv = env;
  const url = new URL(location.href);
  if (env === 'stg') url.searchParams.delete('env');
  else url.searchParams.set('env', env);
  history.replaceState({}, '', url);
  renderEmbed();
}

function setActiveTab(tab) {
  currentTab = tab;
  for (const el of document.querySelectorAll('.tab')) {
    el.classList.toggle('active', el.dataset.tab === tab);
  }
  document.getElementById('pageTitle').textContent = TAB_TITLES[tab] || '';
  const url = new URL(location.href);
  if (tab === 'incoming') url.searchParams.delete('tab');
  else url.searchParams.set('tab', tab);
  // env only means something on the Reliability tab; stg is the default.
  if (tab === 'reliability' && embedEnv !== 'stg') url.searchParams.set('env', embedEnv);
  else url.searchParams.delete('env');
  history.replaceState({}, '', url);
}

async function onOpenEditor() {
  const btn = document.getElementById('openEditorBtn');
  btn.disabled = true;
  btn.textContent = 'Opening…';
  try {
    const res = await fetch('/api/open-dir', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ path: CONFIG.workflow_dir }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || 'HTTP ' + res.status);
    btn.textContent = 'Opened ✓';
    setTimeout(() => { btn.textContent = 'Open config folder ↗'; btn.disabled = false; }, 2000);
  } catch (e) {
    toast(`Failed to open: ${e.message}`, true);
    btn.textContent = 'Open config folder ↗';
    btn.disabled = false;
  }
}

function renderStatus(checks) {
  const content = document.getElementById('content');
  const items = checks.map(c => {
    if (c.separator) return '<li role="separator" class="status-separator"></li>';
    const excerpt = c.excerpt
      ? `<div class="status-excerpt">${escapeHtml(c.excerpt)}</div>`
      : '';
    let fixHtml = '';
    if (!c.ok && c.fix) {
      if (c.fix.action === 'create_dir') {
        fixHtml = `<div class="fix-row"><button class="btn-fix" data-action="create_dir" data-path="${escapeHtml(c.fix.path)}">Create directory</button></div>`;
      } else if (c.fix.action === 'set_env') {
        fixHtml = `<div class="fix-row"><input class="fix-input" type="text" placeholder="${escapeHtml(c.fix.placeholder)}" data-key="${escapeHtml(c.fix.key)}"><button class="btn-fix" data-action="set_env" data-key="${escapeHtml(c.fix.key)}">Save</button></div>`;
      } else if (c.fix.action === 'create_file') {
        fixHtml = `<div class="fix-row"><button class="btn-fix" data-action="create_file" data-path="${escapeHtml(c.fix.path)}">Create file</button></div>`;
      }
    }
    return `
    <li class="status-item">
      <details>
        <summary>
          <span class="status-icon">${c.ok ? '✅' : '❌'}</span>
          <div>
            <div class="status-name">${escapeHtml(c.name)}</div>
            <div class="status-desc">${escapeHtml(c.description)}</div>
          </div>
          <span class="status-chevron">▶</span>
        </summary>
        ${excerpt}
        ${fixHtml}
      </details>
    </li>`;
  }).join('');
  content.innerHTML = `
    <div class="status-toolbar">
      <button class="btn-open-editor" id="openEditorBtn">Open config folder ↗</button>
    </div>
    <ul class="status-list">${items}</ul>`;
  document.getElementById('openEditorBtn').addEventListener('click', onOpenEditor);
  for (const btn of content.querySelectorAll('.btn-fix')) {
    btn.addEventListener('click', onFix);
  }
}

async function onFix(ev) {
  const btn = ev.currentTarget;
  const action = btn.dataset.action;
  btn.disabled = true;
  try {
    if (action === 'create_dir') {
      const res = await fetch('/api/status/create-dir', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ path: btn.dataset.path }),
      });
      if (!res.ok) { const b = await res.json().catch(() => ({})); throw new Error(b.error || 'HTTP ' + res.status); }
    } else if (action === 'set_env') {
      const row = btn.closest('.fix-row');
      const value = row.querySelector('.fix-input').value.trim();
      if (!value) { btn.disabled = false; return; }
      const res = await fetch('/api/status/set-env', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ key: btn.dataset.key, value }),
      });
      if (!res.ok) { const b = await res.json().catch(() => ({})); throw new Error(b.error || 'HTTP ' + res.status); }
    } else if (action === 'create_file') {
      const res = await fetch('/api/status/create-file', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ path: btn.dataset.path }),
      });
      if (!res.ok) { const b = await res.json().catch(() => ({})); throw new Error(b.error || 'HTTP ' + res.status); }
    }
    load(false);
  } catch (e) {
    toast(`Fix failed: ${e.message}`, true);
    btn.disabled = false;
  }
}

const CLEANUP_KIND_LABEL = {
  local_gone: 'local · upstream gone',
  local_merged: 'local · merged',
  worktree: 'worktree',
  remote_merged: 'remote · merged',
};

function renderCleanup(data) {
  const content = document.getElementById('content');
  const repos = (data && data.repos) || [];
  if (!repos.length) {
    content.innerHTML = '<div class="empty">No repos to scan. Add paths in <code>CLEANUP_REPOS</code> on the Settings tab (the agent-clone cache is scanned automatically).</div>';
    return;
  }
  let total = 0;
  let body = '';
  for (const repo of repos) {
    body += `<div class="cl-repo"><div class="group-header">${escapeHtml(repo.label)} <span class="badge-meta">${escapeHtml(repo.kind)}</span></div>`;
    if (!repo.ok) {
      body += `<div class="cl-note">${escapeHtml(repo.error || 'scan failed')}</div></div>`;
      continue;
    }
    if (!repo.candidates.length) {
      body += '<div class="cl-note">Nothing to clean up. 🎉</div></div>';
      continue;
    }
    for (const c of repo.candidates) {
      total++;
      const key = repo.path + '|' + c.kind + '|' + c.name;
      const forceable = c.kind !== 'remote_merged';
      body += `
        <div class="cl-row" data-key="${escapeHtml(key)}" data-mine="${c.mine ? '1' : '0'}">
          <label class="cl-main">
            <input type="checkbox" class="cl-pick"
              data-repo="${escapeHtml(repo.path)}" data-kind="${escapeHtml(c.kind)}"
              data-name="${escapeHtml(c.name)}" data-wt="${escapeHtml(c.worktree_path || '')}"
              data-remote="${c.kind === 'remote_merged' ? '1' : ''}">
            <span class="cl-name">${escapeHtml(c.name)}</span>
            <span class="badge-meta">${escapeHtml(CLEANUP_KIND_LABEL[c.kind] || c.kind)}</span>
            <span class="cl-reason">${escapeHtml(c.reason || '')}${c.author && !c.mine ? ' · ' + escapeHtml(c.author) : ''}</span>
          </label>
          ${forceable ? '<label class="cl-force" title="Force: override git safety refusal — may discard unmerged work"><input type="checkbox" class="cl-forcebox"> force</label>' : ''}
          <span class="cl-result"></span>
        </div>`;
    }
    body += '</div>';
  }
  content.innerHTML = `
    <div class="cl-bar">
      <button class="btn-settings-save" id="cleanupDeleteBtn">Delete selected</button>
      <label class="status-check"><input type="checkbox" id="cleanupMineOnly" checked> Only my branches</label>
      <span class="cl-hint">${total} candidate${total === 1 ? '' : 's'}. Tick items, then Delete. Use Refresh (top-right) to fetch &amp; prune first.</span>
    </div>
    <div class="cleanup mine-only" id="cleanupList">${body}</div>`;
  const btn = document.getElementById('cleanupDeleteBtn');
  if (btn) btn.addEventListener('click', onCleanupDelete);
  const mineOnly = document.getElementById('cleanupMineOnly');
  if (mineOnly) mineOnly.addEventListener('change', e => {
    document.getElementById('cleanupList').classList.toggle('mine-only', e.target.checked);
  });
}

// Map git's (often very verbose) stderr to a short row message; full text -> title.
function cleanupShortError(err) {
  if (!err) return 'failed';
  if (/not fully merged/i.test(err)) return 'not fully merged — tick Force to delete';
  if (/not clean|is dirty|contains modified|locked working tree|use .*--force/i.test(err)) return 'worktree not clean — tick Force';
  if (/protected branch/i.test(err)) return 'protected branch';
  if (/remote ref does not exist/i.test(err)) return 'already deleted on origin — Refresh to prune';
  if (/not a current cleanup candidate/i.test(err)) return 'no longer a candidate — Refresh';
  return err.length > 80 ? err.slice(0, 80) + '…' : err;
}

async function onCleanupDelete() {
  const picks = [...document.querySelectorAll('.cl-pick:checked')];
  if (!picks.length) { toast('Nothing selected', true); return; }
  const actions = picks.map(p => {
    const row = p.closest('.cl-row');
    return {
      repo_path: p.dataset.repo, kind: p.dataset.kind, name: p.dataset.name,
      worktree_path: p.dataset.wt || undefined,
      force: !!row.querySelector('.cl-forcebox:checked'),
      remote: p.dataset.remote === '1',
    };
  });
  const remoteCount = actions.filter(a => a.remote).length;
  const forceCount = actions.filter(a => a.force).length;
  let msg = `Delete ${actions.length} item${actions.length === 1 ? '' : 's'}?`;
  if (remoteCount) msg += `\n• ${remoteCount} REMOTE branch deletion(s) — pushed to origin, not easily undone.`;
  if (forceCount) msg += `\n• ${forceCount} forced deletion(s) — may discard unmerged work.`;
  if (!confirm(msg)) return;
  const btn = document.getElementById('cleanupDeleteBtn');
  btn.disabled = true; btn.textContent = 'Deleting…';
  let results;
  try {
    const res = await fetch('/api/cleanup/delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ actions: actions.map(({ remote, ...a }) => a) }),
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(json.error || 'HTTP ' + res.status);
    results = json.results || [];
  } catch (e) {
    toast('Delete failed: ' + e.message, true);
    btn.disabled = false; btn.textContent = 'Delete selected';
    return;
  }
  const rowsByKey = {};
  for (const row of document.querySelectorAll('.cl-row')) rowsByKey[row.dataset.key] = row;
  let okCount = 0;
  for (const r of results) {
    const row = rowsByKey[r.repo_path + '|' + r.kind + '|' + r.name];
    if (!row) continue;
    const out = row.querySelector('.cl-result');
    const pick = row.querySelector('.cl-pick');
    if (r.ok) {
      okCount++;
      row.classList.add('cl-done');
      if (pick) { pick.checked = false; pick.disabled = true; }
      if (out) out.textContent = '✓ removed';
    } else {
      if (out) { out.textContent = '✗ ' + cleanupShortError(r.error); out.title = r.error || ''; }
      // Reveal the force toggle so the user can retry a refused safe delete.
      const fb = row.querySelector('.cl-force');
      if (fb) fb.classList.add('cl-force-show');
    }
  }
  btn.disabled = false; btn.textContent = 'Delete selected';
  toast(`${okCount}/${results.length} removed`);
}

// Inner content of the standup card, for the stable #standup-card wrapper. `s` is
// the /api/team/standup payload: {configured, cached, generated_at, me, error}.
// Every interpolated value is escaped (values ultimately come from claude output).
function standupInner(s) {
  const hasSummary = !!s.me;
  const meta = s.generated_at
    ? `<span class="standup-meta team-muted">generated ${escapeHtml(relativeTime(s.generated_at))}</span>`
    : '';
  let body;
  if (s.error) {
    body = `<div class="standup-error">Couldn't generate — ${escapeHtml(s.error)}</div>`;
  } else if (hasSummary) {
    body = `
      <div class="standup-block">
        <div class="standup-text">${escapeHtml(s.me)}</div>
      </div>`;
  } else {
    body = `<div class="standup-empty team-muted">No standup summary yet — click Generate to draft one from your assigned tickets and open PRs.</div>`;
  }
  return `
    <div class="standup-head">
      <span class="standup-title">🗣️ Daily standup</span>
      ${meta}
      <button class="btn-standup-generate standup-btn">${hasSummary ? 'Refresh' : 'Generate'}</button>
    </div>
    ${body}`;
}

// Generation is button-only (POST); the load-time fetch only renders cached state.
function renderStandup(standup) {
  return `<div class="standup-card" id="standup-card">${standupInner(standup || {})}</div>`;
}

async function onGenerateStandup(btn) {
  const card = document.getElementById('standup-card');
  btn.disabled = true;
  btn.textContent = 'Generating…';
  try {
    const res = await fetch('/api/team/standup', { method: 'POST' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    if (card) card.innerHTML = standupInner(data);
    if (data.error) toast('Standup generation failed', true);
  } catch (e) {
    btn.disabled = false;
    btn.textContent = 'Refresh';
    toast(`Standup failed: ${e.message}`, true);
  }
}

function renderTeam(data, standup) {
  const content = document.getElementById('content');
  if (!data.configured) {
    const missing = [];
    if (!data.jira_configured) missing.push('Jira credentials (JIRA_SITE, JIRA_EMAIL, JIRA_API_TOKEN)');
    if (!data.board_id_set) missing.push('a board ID (JIRA_BOARD_ID)');
    if (!data.team_set) missing.push('at least one teammate (JIRA_TEAM)');
    content.innerHTML =
      `<div class="empty">Configure the Team tab in <strong>Settings</strong>: add ${escapeHtml(missing.join(', '))}, then refresh.</div>`;
    return;
  }

  const s = data.sprint;
  const sprintLine = s
    ? `<div class="team-sprint"><span class="team-sprint-name">${escapeHtml(s.name)}</span>` +
      (s.goal ? ` · Goal: ${escapeHtml(s.goal)}` : ' · <span class="team-muted">no goal set</span>') + '</div>'
    : '<div class="team-sprint team-muted">No active sprint</div>';
  const epics = (data.epics || []).map(e => {
    const u = safeUrl(e.url);
    const label = `${escapeHtml(e.key)} ${escapeHtml(e.summary)}`;
    return u ? `<a class="team-epic" href="${u}" target="_blank" rel="noopener">${label}</a>`
             : `<span class="team-epic">${label}</span>`;
  }).join('');
  const epicsLine = epics ? `<div class="team-epics"><span class="team-muted">Epics:</span> ${epics}</div>` : '';

  const people = (data.people || []).map(p => {
    if (p.unresolved) {
      return `<details class="team-person team-person-unresolved">
        <summary><span class="team-person-name">${escapeHtml(p.email)}</span> <span class="team-warn">⚠ not found in Jira</span></summary>
        <div class="team-person-body"><div class="team-muted">Couldn't resolve this email to a Jira account — check it in Settings → Resolve members.</div></div>
      </details>`;
    }
    const tickets = p.tickets || [];
    const body = tickets.length
      ? tickets.map(renderTeamTicket).join('')
      : '<div class="team-muted team-person-empty">No tickets in the current sprint.</div>';
    return `<details class="team-person" open>
      <summary><span class="team-person-name">${escapeHtml(p.displayName || p.email)}</span> <span class="team-count">(${tickets.length})</span></summary>
      <div class="team-person-body">${body}</div>
    </details>`;
  }).join('');

  content.innerHTML = `
    ${renderStandup(standup)}
    <div class="team-banner">${sprintLine}${epicsLine}</div>
    <div class="team-people">${people || '<div class="empty">No teammates configured.</div>'}</div>`;
}

function renderTeamTicket(t) {
  const u = safeUrl(t.url);
  const key = escapeHtml(t.key);
  const keyEl = u ? `<a class="team-ticket-key" href="${u}" target="_blank" rel="noopener">${key}</a>`
                  : `<span class="team-ticket-key">${key}</span>`;
  const epic = t.epic ? `<span class="team-ticket-epic" title="${escapeHtml(t.epic.summary)}">${escapeHtml(t.epic.key)}</span>` : '';
  const cat = escapeHtml(t.status_category || 'new');
  return `<div class="team-ticket">
    ${keyEl}
    <span class="team-ticket-status team-status-${cat}">${escapeHtml(t.status_label || '')}</span>
    <span class="team-ticket-summary">${escapeHtml(t.summary || '')}</span>
    ${epic}
  </div>`;
}

function renderSettings() {
  const c = CONFIG;
  const fields = [
    { key: 'DEPLOY_TARGET', label: 'Deploy target environment', type: 'text',
      desc: 'Default environment for the Deploy button on each PR card (e.g. dev-box). Deploys push a preview/… branch, which triggers the tracked workflow.',
      value: c.deploy_target || '' },
    { key: 'DEV_BOX_URL', label: 'Dev box URL', type: 'text',
      desc: 'Where the dev box serves the deployed app (VPN), e.g. https://<github-login>.internal.cognota.com. Linked from the Deployed tab; blank hides the link.',
      value: c.dev_box_url || '' },
    { key: 'JIRA_SITE', label: 'Jira site', type: 'text',
      desc: 'Atlassian site host for the Tickets tab, e.g. your-org.atlassian.net.',
      value: c.jira_site || '' },
    { key: 'JIRA_EMAIL', label: 'Jira email', type: 'text',
      desc: 'Atlassian account email used with the API token for the Tickets tab.',
      value: c.jira_email || '' },
    { key: 'JIRA_API_TOKEN', label: 'Jira API token', type: 'password',
      desc: 'API token from id.atlassian.com/manage-profile/security/api-tokens. '
        + (c.jira_token_set ? 'Currently set — leave blank to keep it.' : 'Not set.'),
      value: '' },
    { key: 'JIRA_TEAM', label: 'Team members (emails)', type: 'text',
      desc: 'Comma-separated teammate emails for the Team tab. Resolved to Jira accounts (cached). Use “Resolve members” below to preview the mapping.',
      value: c.jira_team || '' },
    { key: 'JIRA_BOARD_ID', label: 'Team: board ID', type: 'text',
      desc: 'Numeric board id the Team tab reads from (active sprint on Scrum boards; open board tickets on team-managed boards). From the board URL: .../boards/123 → 123.',
      value: c.jira_board_id || '' },
    { key: 'CACHE_TTL', label: 'Cache TTL (seconds)', type: 'number',
      desc: 'How long per-PR detail data is cached before a background refresh.',
      value: c.cache_ttl ?? 30 },
    { key: 'CLEANUP_REPOS', label: 'Cleanup repo paths', type: 'text',
      desc: 'Local repo paths the Cleanup tab scans (comma-separated, e.g. ~/git/app,~/git/api). The agent-clone cache is always scanned too.',
      value: (c.cleanup_repos || []).join(',') },
    { key: 'CLEANUP_AUTHOR_EMAIL', label: 'Cleanup: my email', type: 'text',
      desc: "Email treated as \"me\" for the Cleanup tab's \"Only my branches\" filter. Leave blank to use each repo's git config user.email.",
      value: c.cleanup_author_email || '' },
    { key: 'FRESH_REVIEWERS', label: 'Nudge: fresh reviewers', type: 'text',
      desc: 'Comma-separated GitHub logins to DM (fresh mode) and tag (#Channel) when nobody has reviewed your PR yet. Blank hides the fresh-mode targets.',
      value: (c.fresh_reviewers || []).join(',') },
    { key: 'TEAM_CHANNEL_ID', label: 'Nudge: team channel ID', type: 'text',
      desc: 'Slack channel ID (e.g. C01ABCDEF) the #Channel button posts in. Blank hides the #Channel button.',
      value: c.team_channel_id || '' },
    { key: 'SLACK_IDS', label: 'Nudge: GitHub→Slack IDs', type: 'text',
      desc: 'Maps each GitHub login to a Slack member ID (U…), or an @handle, so nudges DM the right person without a lookup. Format: githubid:U01ABC,login2:U02DEF (comma-separated).',
      value: c.slack_ids || '' },
    { key: 'TEAMS', label: 'Nudge: extra teams (JSON)', type: 'text',
      desc: 'Optional JSON array of extra teams for the Nudge/#Channel dropdowns, e.g. [{"name":"Platform","channel_id":"C0DEF456","reviewers":["charlie"]}]. Blank for none.',
      value: (c.teams && c.teams.length) ? JSON.stringify(c.teams) : '' },
    { key: 'EDITOR_CMD', label: 'Editor command', type: 'text',
      desc: 'Command used by "Open config folder ↗" on the Status tab. E.g. "code", "cursor", "subl". Leave blank to auto-detect (VS Code → system default).',
      value: c.editor_cmd || '' },
    { key: 'HOST', label: 'Bind host', type: 'text',
      desc: 'Local address to bind the server to.', restart: true,
      value: c.host || '127.0.0.1' },
    { key: 'PORT', label: 'Port', type: 'number',
      desc: 'Port to listen on.', restart: true,
      value: c.port ?? 8765 },
  ];
  const rows = fields.map(f => `
    <div class="settings-row">
      <label class="settings-label" for="s-${f.key}">${escapeHtml(f.label)}</label>
      <div class="settings-desc">${escapeHtml(f.desc)}</div>
      ${f.restart ? '<div class="settings-restart">⚠ Requires server restart to take effect.</div>' : ''}
      <input class="settings-input" id="s-${f.key}" data-key="${escapeHtml(f.key)}"
             type="${f.type}" value="${escapeHtml(String(f.value))}">
    </div>`).join('');
  document.getElementById('content').innerHTML = `
    <div class="settings-form">
      ${rows}
      <div class="settings-row">
        <label class="settings-label">Tickets: visible statuses</label>
        <div class="settings-desc">Only these statuses show on the Tickets tab (one column each). None checked = show all. Fetch the list from Jira, then check the ones you want.</div>
        <button class="btn-fetch-statuses" id="fetchStatusesBtn" type="button">Fetch statuses from Jira</button>
        <div id="statusChecks" class="status-checks"></div>
      </div>
      <div class="settings-row">
        <label class="settings-label">Team: resolve members</label>
        <div class="settings-desc">Check that each email in “Team members” maps to a Jira account. Resolves and caches the mapping.</div>
        <button class="btn-fetch-statuses" id="resolveMembersBtn" type="button">Resolve members</button>
        <div id="teamMembers" class="status-checks"></div>
      </div>
      <div><button class="btn-settings-save" id="settingsSaveBtn">Save &amp; Reload</button></div>
    </div>`;
  // Pre-render the saved selection (all checked) so it shows without fetching.
  renderStatusChecks(c.jira_status_filter || [], c.jira_status_filter || []);
  document.getElementById('fetchStatusesBtn').addEventListener('click', fetchStatuses);
  document.getElementById('resolveMembersBtn').addEventListener('click', resolveMembers);
  document.getElementById('settingsSaveBtn').addEventListener('click', saveSettings);
}

async function resolveMembers() {
  const btn = document.getElementById('resolveMembersBtn');
  const input = document.getElementById('s-JIRA_TEAM');
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = 'Resolving…';
  try {
    const res = await fetch('/api/team/resolve', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ emails: input ? input.value : '' }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || 'HTTP ' + res.status);
    const el = document.getElementById('teamMembers');
    const members = body.members || [];
    el.innerHTML = members.length
      ? members.map(m => `<div class="team-resolve-row">` + (m.unresolved
          ? `<span class="team-warn">⚠ ${escapeHtml(m.email)} — not found</span>`
          : `<span class="team-ok">✓ ${escapeHtml(m.displayName || m.email)}</span> <span class="team-muted">${escapeHtml(m.email)}</span>`) + `</div>`).join('')
      : '<div class="settings-desc">No emails entered.</div>';
  } catch (e) {
    toast('Failed to resolve members: ' + e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

// Render a checkbox per status name; `checked` is the subset that starts ticked.
function renderStatusChecks(names, checked) {
  const on = new Set(checked);
  const el = document.getElementById('statusChecks');
  if (!el) return;
  if (!names.length) {
    el.innerHTML = '<div class="settings-desc">No statuses yet — click “Fetch statuses from Jira”.</div>';
    return;
  }
  el.innerHTML = names.map(n => `
    <label class="status-check">
      <input type="checkbox" value="${escapeHtml(n)}" ${on.has(n) ? 'checked' : ''}>
      ${escapeHtml(n)}
    </label>`).join('');
}

async function fetchStatuses() {
  const btn = document.getElementById('fetchStatusesBtn');
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = 'Fetching…';
  try {
    const res = await fetch('/api/jira/statuses');
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || 'HTTP ' + res.status);
    // Keep whatever is currently ticked checked in the refreshed list.
    const stillChecked = [...document.querySelectorAll('#statusChecks input:checked')].map(b => b.value);
    const saved = CONFIG.jira_status_filter || [];
    renderStatusChecks(body.statuses || [], stillChecked.length ? stillChecked : saved);
  } catch (e) {
    toast('Failed to fetch statuses: ' + e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

async function saveSettings() {
  const btn = document.getElementById('settingsSaveBtn');
  btn.disabled = true;
  btn.textContent = 'Saving…';
  const settings = {};
  for (const input of document.querySelectorAll('.settings-input')) {
    settings[input.dataset.key] = input.value.trim();
  }
  // Status filter: serialize the checked boxes. Only send the key when boxes are
  // present, so an unfetched/empty list never clobbers a saved filter — but an
  // explicit "none checked" (boxes present, none ticked) clears it.
  const statusBoxes = document.querySelectorAll('#statusChecks input[type=checkbox]');
  if (statusBoxes.length) {
    settings['JIRA_STATUS_FILTER'] =
      [...statusBoxes].filter(b => b.checked).map(b => b.value).join(',');
  }
  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ settings }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.error || 'HTTP ' + res.status);
    location.reload();
  } catch (e) {
    toast(`Save failed: ${e.message}`, true);
    btn.disabled = false;
    btn.textContent = 'Save & Reload';
  }
}

function deployedStatusIcon(item) {
  if (item.error) return '❓';
  if (item.status === 'in_progress') return '🔄';
  if (item.status === 'queued') return '⏳';
  if (item.conclusion === 'success') return '✅';
  if (item.conclusion === 'failure') return '❌';
  if (item.conclusion === 'cancelled') return '🚫';
  return '❓';
}

function renderDeployed(data) {
  const content = document.getElementById('content');
  const envs = data.environments || {};
  const envKeys = Object.keys(envs).sort();
  if (!envKeys.length) {
    const hint = data.target_env
      ? `No deploy targets found for <strong>${escapeHtml(data.target_env)}</strong> in <code>deploy_targets.json</code>.`
      : 'No deploy targets configured. Add a <code>deploy_targets.json</code> via the Status tab.';
    content.innerHTML = `<div class="empty">${hint}</div>`;
    return;
  }
  const deployTargets = CONFIG.deploy_targets || {};
  const boxUrl = safeUrl(CONFIG.dev_box_url || '');
  let html = boxUrl
    ? `<div class="deployed-box"><a class="deployed-box-link" href="${escapeHtml(boxUrl)}" target="_blank" rel="noopener" title="Open the dev box (VPN required)">${escapeHtml(boxUrl.replace(/^https?:\/\//, ''))}</a></div>`
    : '';
  for (const env of envKeys) {
    const items = (envs[env] || []).slice().sort((a, b) => (a.repo || '').localeCompare(b.repo || ''));
    html += `<details class="deployed-section" open>
      <summary class="deployed-env-header">
        <span class="deployed-env-label">${escapeHtml(env)}</span>
        <span class="deployed-env-count">${items.length}</span>
        <span class="deployed-chevron">▶</span>
      </summary>
      <div class="deployed-items">`;
    for (const item of items) {
      const icon = deployedStatusIcon(item);
      const repoShort = (item.repo || '').replace(/^[^/]+\//, '');
      const branchHtml = item.branch
        ? `<span class="deployed-branch" title="${escapeHtml(item.branch)}">${escapeHtml(item.branch)}</span>` : '';
      const metaHtml = item.error
        ? `<span class="deployed-error">${escapeHtml(item.error)}</span>`
        : `<span class="deployed-meta">${item.createdAt ? relativeTime(item.createdAt) : ''}${item.displayTitle ? ' · ' + escapeHtml(item.displayTitle) : ''}</span>`;
      const hasWorkflow = !!((deployTargets[item.repo] || {})[env]);
      const deployRow = hasWorkflow
        ? `<div class="deployed-deploy-row">
          <select class="deployed-branch-select" data-repo="${escapeHtml(item.repo)}" data-env="${escapeHtml(env)}" disabled>
            <option value="">Loading branches…</option>
          </select>
          <button class="deployed-deploy-btn" data-repo="${escapeHtml(item.repo)}" data-env="${escapeHtml(env)}" disabled>Deploy</button>
        </div>`
        : '';
      html += `<div class="deployed-item">
        <span class="deployed-icon">${icon}</span>
        <span class="deployed-repo" title="${escapeHtml(item.repo || '')}">${escapeHtml(repoShort)}</span>
        ${branchHtml}
        ${metaHtml}
      </div>${deployRow}`;
    }
    html += '</div></details>';
  }
  content.innerHTML = html;
  for (const btn of content.querySelectorAll('.deployed-deploy-btn')) {
    btn.addEventListener('click', onDeployFromDeployed);
  }
  loadDeployedBranches(content);
}

async function loadDeployedBranches(container) {
  const selects = [...container.querySelectorAll('.deployed-branch-select')];
  const repos = [...new Set(selects.map(s => s.dataset.repo))];
  await Promise.all(repos.map(async repo => {
    let branches = [], baseBranch = '';
    try {
      const res = await fetch(`/api/branches?repo=${encodeURIComponent(repo)}`);
      const json = res.ok ? await res.json() : {};
      branches = json.branches || [];
      baseBranch = json.base_branch || '';
    } catch (_) {}
    const hasOptions = baseBranch || branches.length;
    for (const sel of selects.filter(s => s.dataset.repo === repo)) {
      if (hasOptions) {
        let opts = '<option value="">— select branch —</option>';
        if (baseBranch) {
          opts += `<optgroup label="Base branch"><option value="${escapeHtml(baseBranch)}">${escapeHtml(baseBranch)}</option></optgroup>`;
        }
        if (branches.length) {
          opts += `<optgroup label="My branches">${branches.map(b => `<option value="${escapeHtml(b)}">${escapeHtml(b)}</option>`).join('')}</optgroup>`;
        }
        sel.innerHTML = opts;
        sel.disabled = false;
        sel.addEventListener('change', () => {
          const btn = sel.closest('.deployed-deploy-row').querySelector('.deployed-deploy-btn');
          if (btn) btn.disabled = !sel.value;
        });
      } else {
        sel.innerHTML = '<option value="">No branches found</option>';
      }
    }
  }));
}

async function onDeployFromDeployed(ev) {
  const btn = ev.currentTarget;
  const repo = btn.dataset.repo;
  const env = btn.dataset.env;
  const sel = btn.closest('.deployed-deploy-row').querySelector('.deployed-branch-select');
  const headRef = sel?.value;
  if (!headRef) return;
  if (!confirmDeploy(repo, headRef, env)) return;
  btn.disabled = true;
  btn.textContent = 'Pushing…';
  try {
    const body = await postDeploy(repo, headRef, env);
    btn.textContent = 'Pushed ✓';
    btn.title = `${body.branch || ''} pushed`;
    btn.style.background = '#1a7f37';
    setTimeout(() => {
      btn.textContent = 'Deploy';
      btn.style.cssText = '';
      btn.disabled = !sel?.value;
    }, 4000);
  } catch (e) {
    toast(`Deploy failed: ${e.message}`, true);
    btn.textContent = 'Deploy';
    btn.disabled = !sel?.value;
  }
}

async function load(fresh) {
  const btn = document.getElementById('refreshBtn');
  btn.disabled = true;
  document.getElementById('content').innerHTML = '<div class="empty">Loading…</div>';
  try {
    if (currentTab === 'settings') {
      renderSettings();
    } else if (currentTab === 'reliability') {
      renderEmbed();
    } else if (currentTab === 'status') {
      const res = await fetch('/api/status');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      renderStatus(await res.json());
    } else if (currentTab === 'deployed') {
      const res = await fetch('/api/deployed');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      renderDeployed(await res.json());
    } else if (currentTab === 'cleanup') {
      const res = await fetch('/api/cleanup' + (fresh ? '?fresh=1' : ''));
      if (!res.ok) throw new Error('HTTP ' + res.status);
      renderCleanup(await res.json());
    } else if (currentTab === 'team') {
      const [teamRes, standupRes] = await Promise.all([
        fetch('/api/team' + (fresh ? '?fresh=1' : '')),
        fetch('/api/team/standup'),
      ]);
      if (!teamRes.ok) throw new Error('HTTP ' + teamRes.status);
      const standupData = standupRes.ok ? await standupRes.json() : { error: 'HTTP ' + standupRes.status };
      renderTeam(await teamRes.json(), standupData);
    } else if (currentTab === 'tickets') {
      const res = await fetch('/api/tickets' + (fresh ? '?fresh=1' : ''));
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      if (!data.configured) {
        document.getElementById('content').innerHTML =
          '<div class="empty">Configure Jira in <code>.env</code>: set JIRA_SITE, JIRA_EMAIL, and JIRA_API_TOKEN, then refresh.</div>';
        return;
      }
      const filter = CONFIG.jira_status_filter || [];
      let tickets = data.tickets;
      if (filter.length) {
        const allow = new Set(filter);
        tickets = tickets.filter(t => allow.has(t.status_label));
      }
      // One column per status: saved-filter order if set, else statuses present
      // ordered by category (To Do before In Progress) then name.
      const order = filter.length ? filter.slice() : ticketStatusOrder(tickets);
      TABS.tickets.groups = order;
      TABS.tickets.headers = Object.fromEntries(order.map(s => [s, s]));
      render(tickets);
    } else {
      const tab = TABS[currentTab];
      const url = tab.endpoint + (fresh ? '?fresh=1' : '');
      if (currentTab === 'mine') {
        const [prsRes, depRes] = await Promise.all([fetch(url), fetch('/api/deployed')]);
        if (!prsRes.ok) throw new Error('HTTP ' + prsRes.status);
        deployedState = depRes.ok ? (await depRes.json()).environments || {} : {};
        render(await prsRes.json());
      } else {
        const res = await fetch(url);
        if (!res.ok) throw new Error('HTTP ' + res.status);
        render(await res.json());
      }
    }
  } catch (e) {
    document.getElementById('content').innerHTML =
      `<div class="empty">Error: ${escapeHtml(e.message)}</div>`;
  } finally {
    btn.disabled = false;
  }
}

document.getElementById('refreshBtn').addEventListener('click', () => load(true));
// Single delegated listener for all PR-card buttons across re-renders.
document.getElementById('content').addEventListener('click', onContentClick);
// Close nudge/channel menus when clicking outside them; also on Escape.
document.addEventListener('click', (ev) => {
  if (!ev.target.closest('.nudge-split')) closeAllNudgeMenus();
  if (!ev.target.closest('.nudge-teams-split')) closeAllNudgeTeamsMenus();
  if (!ev.target.closest('.channel-split')) closeAllChannelMenus();
});
document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape') {
    closeAllNudgeMenus();
    closeAllNudgeTeamsMenus();
    closeAllChannelMenus();
  }
});
for (const el of document.querySelectorAll('.tab')) {
  el.addEventListener('click', () => {
    if (el.dataset.tab === currentTab) return;
    setActiveTab(el.dataset.tab);
    load(false);
  });
}
setActiveTab(currentTab);
load(false);
