import { citationPreparationNote, runProgressText } from './chat-model.js';
import { el, reconcileChildren, setHidden, setText } from './dom.js';
import { artifactCount } from './store.js';

const PROGRESS_KEY = '__progress__';

export class ChatView {
  constructor({ root }) {
    this.root = root;
    this.nodes = new Map();
  }

  reset() {
    this.root.replaceChildren();
    this.nodes.clear();
    this.root.scrollTop = 0;
  }

  scrollToBottom() {
    this.root.scrollTop = this.root.scrollHeight;
  }

  update(session) {
    const scroller = this.root;
    const wasEmpty = this.nodes.size === 0;
    const atBottom = wasEmpty
      || scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 36;
    const previousTop = scroller.scrollTop;
    const rows = [];
    let previousSide = null;

    for (const message of session.chatMessages()) {
      const key = message.message_id || `row-${rows.length}`;
      let entry = this.nodes.get(key);
      if (!entry) {
        entry = this.#createMessage(message);
        this.nodes.set(key, entry);
      }
      const side = message.role === 'user' ? 'user' : 'assistant';
      const grouped = previousSide === side;
      this.#updateMessage(entry, message, session, grouped);
      rows.push(entry.row);
      previousSide = side;
    }

    const activeRun = session.activeRun;
    if (activeRun) {
      let entry = this.nodes.get(PROGRESS_KEY);
      if (!entry) {
        entry = this.#createProgress();
        this.nodes.set(PROGRESS_KEY, entry);
      }
      const hasQuery = session.queriesForRun(activeRun.run_id).length > 0;
      setText(entry.text, runProgressText(activeRun, hasQuery));
      rows.push(entry.row);
      previousSide = 'assistant';
    }

    reconcileChildren(this.root, rows);
    for (const [key, entry] of this.nodes) {
      if (!rows.includes(entry.row)) {
        entry.row.remove();
        this.nodes.delete(key);
      }
    }

    if (wasEmpty || atBottom) scroller.scrollTop = scroller.scrollHeight;
    else scroller.scrollTop = previousTop;
  }

  #createMessage(message) {
    const row = el('div', { class: 'chat-turn' });
    const msgRow = el('div', { class: 'chat-msg-row' });
    const bubble = el('div', { class: 'chat-bubble' });
    const sr = el('span', { class: 'sr-only' });
    const text = el('span', { class: 'chat-text' });
    const pills = el('div', { class: 'chat-pills' });
    bubble.append(sr, text, pills);
    msgRow.append(bubble);
    const meta = el('p', { class: 'chat-meta' });
    const failure = el('div', { class: 'chat-failure' });
    const failureText = el('p', { class: 'chat-failure__text' });
    const retry = el('button', {
      type: 'button',
      class: 'chat-retry',
      'data-action': 'retry-message',
      text: '重试',
    });
    failure.append(failureText, retry);
    row.append(msgRow, meta, failure);
    return {
      row,
      bubble,
      sr,
      text,
      pills,
      meta,
      failure,
      failureText,
      retry,
      pillNodes: new Map(),
    };
  }

  #updateMessage(entry, message, session, grouped) {
    const side = message.role === 'user' ? 'user' : 'assistant';
    entry.row.className = [
      'chat-turn',
      `chat-turn--${side}`,
      grouped ? 'is-group' : '',
      message.pending ? 'is-pending' : '',
      message.failed ? 'is-failed' : '',
    ].filter(Boolean).join(' ');
    entry.row.dataset.messageId = message.message_id || '';
    entry.sr.textContent = side === 'user'
      ? '你说：'
      : message.kind === 'status' ? '系统：' : '助手：';
    setText(entry.text, message.content || '');
    entry.bubble.className = 'chat-bubble';
    if (message.kind === 'status') {
      entry.bubble.classList.add('chat-bubble--status', `chat-bubble--${message.level || 'warn'}`);
    }
    if (message.failed) entry.bubble.classList.add('chat-bubble--failed');
    this.#updatePills(entry, message, session, side);
    this.#updateMeta(entry, message, side, session.runs.get(message.run_id));
  }

  #updatePills(entry, message, session, side) {
    const queryIds = side === 'assistant'
      ? [...new Set((message.query_ids || []).filter((id) => session.queries.has(id)))]
      : [];
    const buttons = [];
    for (const queryId of queryIds) {
      let button = entry.pillNodes.get(queryId);
      if (!button) {
        button = el('button', {
          type: 'button',
          class: 'chat-pill',
          dataset: { queryId },
        });
        button.append(el('span', { text: '' }), el('span', { class: 'chat-pill__count' }));
        entry.pillNodes.set(queryId, button);
      }
      const index = session.queryOrder.indexOf(queryId) + 1;
      const count = artifactCount(session.storeForQuery(queryId));
      setText(button.firstChild, `检索 ${index}`);
      setText(button.lastChild, count > 0 ? `${count} 条` : '查看');
      const active = queryId === session.activeId;
      button.setAttribute('aria-pressed', String(active));
      button.setAttribute('aria-label', `在结果区查看检索 ${index}${count ? `，${count} 条` : ''}`);
      button.title = count > 0 ? '在结果区查看这轮结果' : '在结果区查看这轮检索';
      button.classList.toggle('is-on', active);
      buttons.push(button);
    }
    reconcileChildren(entry.pills, buttons);
    for (const queryId of entry.pillNodes.keys()) {
      if (!queryIds.includes(queryId)) entry.pillNodes.delete(queryId);
    }
    setHidden(entry.pills, buttons.length === 0);
  }

  #updateMeta(entry, message, side, run) {
    const isLocal = side === 'user' && message.local;
    if (isLocal && message.failed) {
      setText(entry.failureText, `发送失败：${message.failure || '请重试。'}`);
      entry.retry.dataset.pendingId = message.message_id;
      entry.failure.hidden = false;
      entry.meta.hidden = true;
      return;
    }
    entry.failure.hidden = true;
    if (isLocal && message.pending) {
      entry.meta.textContent = '发送中…';
      entry.meta.hidden = false;
      return;
    }
    const notice = side === 'assistant' ? citationPreparationNote(run) : '';
    setText(entry.meta, notice);
    setHidden(entry.meta, !notice);
  }

  #createProgress() {
    const row = el('div', { class: 'chat-turn chat-turn--assistant chat-turn--progress' });
    const bubble = el('div', { class: 'chat-bubble chat-bubble--status chat-bubble--progress' });
    const sr = el('span', { class: 'sr-only', text: '系统：' });
    const text = el('span');
    bubble.append(sr, text);
    row.append(el('div', { class: 'chat-msg-row' }, bubble));
    return { row, text };
  }
}
