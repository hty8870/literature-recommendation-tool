import { el, clear, reconcileChildren, setHidden, setText } from './dom.js';
import { WORKER_STATE, label } from './labels.js';
import { workersList } from './store.js';
import { libraryDisplayName } from './search-input.js';

const workerViews = new WeakMap();

export function resultStatus(store, agentRun = null) {
  if (['error', 'exhausted'].includes(store.connection)) return { text: '连接中断', level: 'error' };
  if (store.searchState === 'cancelled') return { text: '已取消', level: 'warn' };
  if (store.localOnly) {
    if (store.searchState === 'failed') return { text: '本地检索未完成', level: 'error' };
    if (!store.terminal) return { text: '仅检索本地快照' };
    const incomplete = [...store.workers.values()].some((worker) => worker.worker_state !== 'completed');
    return { text: incomplete ? '本地覆盖不全 · 未经模型核查' : '本地候选 · 未经模型核查', level: 'warn' };
  }
  const rankings = [...store.candidateRankings.values()];
  const failed = rankings.filter((ranking) => ['failed', 'unavailable'].includes(ranking.status));
  if (failed.length) return {
    text: 'LLM 重排未完成', level: 'error',
    detail: failed.map((ranking) => ranking.failure_code || 'RERANK_UNAVAILABLE').join(' · '),
  };
  if (store.searchState === 'failed') return { text: '检索未完成', level: 'error' };
  const pending = rankings.filter((ranking) => ranking.status === 'pending');
  if (pending.some((ranking) => ranking.phase === 'blind')) return { text: 'LLM 重排 · 盲排中' };
  if (pending.some((ranking) => ranking.phase === 'score_review')) return { text: 'LLM 重排 · 揭分复核中' };
  if (pending.length) return { text: '等待 LLM 重排' };
  if (rankings.some((ranking) => ranking.score_review?.status === 'failed')) return {
    text: 'LLM 重排 · 复核失败，保留盲排', level: 'warn',
  };
  if (store.searchState === 'partial') return { text: '部分完成', level: 'warn' };
  if (agentRun?.status === 'failed') return { text: 'Agent 未完成', level: 'error' };
  if (agentRun?.status === 'cancelled') return { text: '已取消', level: 'warn' };
  if (rankings.some((ranking) => ranking.status === 'completed' && ranking.score_review)) {
    return { text: 'LLM 重排完成' };
  }
  if (store.queryId && !store.terminal) return { text: '结果更新中' };
  if (['pending', 'running'].includes(agentRun?.status)) return { text: 'Agent 处理中' };
  return { text: '' };
}

export function workerCounts(worker) {
  if (worker.placeholder) return '等待任务开始';
  if (worker.source_agent_status && worker.source_agent_status !== 'disabled') {
    return `已发现 ${worker.candidates_seen ?? 0} · 已提交 ${worker.source_candidates_submitted ?? 0}`;
  }
  return `候选 ${worker.candidates_seen ?? 0} · 已处理 ${worker.candidates_verified ?? 0}`;
}

export function renderStatus(store, refs, agentRun = null) {
  let citable = 0;
  let evidenceOnly = 0;
  for (const rec of store.results.values()) {
    if (!rec.citable || rec.revoked) continue;
    if (rec.rankable) citable += 1;
    else evidenceOnly += 1;
  }
  const real = store.simulation === false;
  setText(refs.metricResults, store.queryId ? real ? store.candidates.size : citable + evidenceOnly : 0);
  refs.metricResults.title = store.queryId
    ? real ? '真实题录与摘要候选，不是可引用推荐'
      : `${citable} 条可排序${evidenceOnly ? ` · ${evidenceOnly} 条仅证据` : ''}`
    : '尚未创建检索';
  if (refs.resultsStatus) {
    const status = resultStatus(store, agentRun);
    setText(refs.resultsStatus, status.text);
    refs.resultsStatus.dataset.level = status.level || 'info';
    refs.resultsStatus.title = status.detail || '';
    setHidden(refs.resultsStatus, !status.text || !store.queryId);
  }
  setHidden(refs.workerList.closest('.source-progress') || refs.sourceProgress, !store.queryId);
  renderNotices(store, refs);
  renderWorkers(store, refs);
}

function renderNotices(store, refs) {
  const signature = store.notices.map((notice) => `${notice.level}:${notice.text}`).join('|');
  if (refs.noticeList.dataset.signature === signature) return;
  refs.noticeList.dataset.signature = signature;
  clear(refs.noticeList);
  for (const notice of store.notices) {
    refs.noticeList.append(el('li', { class: `notice notice--${notice.level}`, text: notice.text }));
  }
}

function renderWorkers(store, refs) {
  const workers = workersList(store);
  const total = store.session?.requested_library_ids?.length || workers.length;
  const completed = store.completedLibraryIds.size;
  refs.workerProgress.max = Math.max(1, total);
  refs.workerProgress.value = completed;
  refs.workerProgress.setAttribute('aria-label', `已完成数据源 ${completed}/${total}，不包含统一排序`);
  setHidden(refs.workerProgress, total === 0);
  setText(refs.workerProgressCount, `${completed}/${total}`);
  setHidden(refs.workersEmpty, workers.length > 0);
  let views = workerViews.get(refs.workerList);
  if (!views) {
    views = new Map();
    workerViews.set(refs.workerList, views);
  }
  const rows = [];
  for (const worker of workers) {
    let entry = views.get(worker.library_id);
    if (!entry) {
      const dot = el('span', { class: 'worker__status-dot', 'aria-hidden': 'true' });
      const name = el('span', { class: 'worker__name', title: worker.library_id });
      const state = el('span', { class: 'worker__state' });
      const counts = el('span', { class: 'worker__counts' });
      const error = el('span', { class: 'worker__error' });
      const row = el('li', { class: 'worker', dataset: { libraryId: worker.library_id } },
        el('div', { class: 'worker__head' }, dot, name, state), counts, error);
      entry = { row, dot, name, state, counts, error };
      views.set(worker.library_id, entry);
    }
    const state = worker.worker_state || 'pending';
    for (const node of [entry.row, entry.dot, entry.state]) node.dataset.state = state;
    setText(entry.name, libraryDisplayName(worker));
    setText(entry.state, label(WORKER_STATE, state));
    setText(entry.counts, workerCounts(worker));
    setText(entry.error, worker.last_error?.message || worker.last_error?.code);
    setHidden(entry.error, !worker.last_error);
    rows.push(entry.row);
  }
  reconcileChildren(refs.workerList, rows);
  for (const [id, entry] of views) {
    if (!rows.includes(entry.row)) views.delete(id);
  }
}

export function renderError(container, { title, message, actions = [] } = {}) {
  clear(container);
  if (!title && !message) {
    setHidden(container, true);
    return;
  }
  setHidden(container, false);
  if (title) container.append(el('strong', { text: `${title} ` }));
  if (message) container.append(el('span', { text: message }));
  for (const action of actions) {
    container.append(
      el('button', {
        type: 'button',
        class: `button ${action.primary ? 'button--primary' : 'button--quiet'}`,
        text: action.label,
        style: { marginLeft: '8px', marginTop: '6px' },
        onclick: action.onClick,
      }),
    );
  }
}

let announceTimer = null;
export function announce(container, text) {
  if (!container || !text) return null;
  container.textContent = text;
  if (announceTimer) window.clearTimeout(announceTimer);
  announceTimer = window.setTimeout(() => {
    container.textContent = '';
    announceTimer = null;
  }, 6000);
  return announceTimer;
}
