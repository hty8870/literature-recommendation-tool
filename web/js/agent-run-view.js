import { clear, el, setHidden } from './dom.js';
import { artifactCount } from './store.js';

export function renderAttemptTabs(session, container) {
  const queries = session.queryList;
  const signature = [
    session.activeId,
    ...queries.map((query) => `${query.queryId}:${artifactCount(session.storeForQuery(query.queryId))}`),
  ].join('|');
  if (container.dataset.signature === signature) return;
  container.dataset.signature = signature;
  clear(container);
  setHidden(container, queries.length < 2);
  queries.forEach((query, index) => {
    const claim = query.created?.claims?.[0];
    const count = artifactCount(session.storeForQuery(query.queryId));
    const button = el('button', {
      type: 'button',
      class: 'source-pill',
      dataset: { queryId: query.queryId },
      text: `检索 ${index + 1}`,
      title: `${claim?.claim_text || query.queryId}${count ? `（${count} 条）` : ''}`,
      'aria-pressed': String(query.queryId === session.activeId),
      'aria-label': `切换到检索 ${index + 1}${count ? `，${count} 条结果` : ''}`,
    });
    container.append(button);
  });
  for (const button of container.querySelectorAll('button[data-query-id]')) {
    button.classList.toggle('is-selected', button.dataset.queryId === session.activeId);
  }
}

export function agentOutcome(run) {
  if (run.error) return `${run.error.code || 'AGENT_ERROR'}：${run.error.message || 'Agent 未能完成。'}`;
  return run.summary || 'Agent 尚未给出最终说明；已提交结果不受影响。';
}
