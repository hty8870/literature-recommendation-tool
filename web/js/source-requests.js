import { decideSource, listSourceRequests } from './api.js';
import { el } from './dom.js';

const byId = (id) => document.getElementById(id);
const STATES = {
  pending: '待批准', queued: '等待接入', testing: '正在验证公开样例',
  ready: '已接入 · 默认关闭', failed: '接入失败', outdated: '方案已更新',
};
const ERRORS = {
  SOURCE_RATE_LIMIT: '来源正在限流，稍后可重试。',
  SOURCE_UNAVAILABLE: '暂时无法连接到来源。',
  CONNECTOR_EMPTY_PROBE: '公开样例未返回可验证的题录。',
  INSTALL_INTERRUPTED: '接入过程被中断。',
  SOURCE_INVALID_RESPONSE: '返回的题录格式不符合预期。',
};

export class SourceRequests {
  constructor({ onOpenHistory, onReady }) {
    Object.assign(this, { onOpenHistory, onReady });
    this.dock = byId('source-request-dock');
    this.toggle = byId('source-requests-toggle');
    this.dialog = byId('source-requests-dialog');
    this.list = byId('source-request-list');
    this.error = byId('source-request-error');
    this.toggle.addEventListener('click', () => {
      if (document.querySelector('dialog[open]')) return;
      this.dialog.showModal();
      void this.poll(true);
    });
    byId('source-requests-close').addEventListener('click', () => this.dialog.close());
  }

  setIdentity(owner, available) {
    if (this.scope?.owner === owner && this.scope?.available === available) return;
    if (!this.scope && !available) return;
    this.scope?.controller.abort();
    this.scope = owner && available ? { owner, available, controller: new AbortController() } : null;
    if (this.dialog.open) this.dialog.close();
    this.mutating = null;
    this.signature = '';
    this.items = [];
    this.error.hidden = true;
    this.list.replaceChildren();
    this.next = 0;
    this.syncDock();
    if (this.scope) void this.poll(true);
  }

  setView(view) {
    const composer = byId('chat-form');
    if (view === 'results') composer.before(this.dock);
    else byId('main-content').append(this.dock);
  }

  async poll(force = false) {
    const scope = this.scope;
    if (!scope || this.polling === scope || this.mutating || (!force && Date.now() < this.next)) return;
    this.polling = scope;
    this.next = Date.now() + (this.items?.some((item) => ['queued', 'testing'].includes(item.status)) ? 2500 : 10000);
    try {
      const result = await listSourceRequests(scope.controller.signal);
      if (scope !== this.scope || this.mutating) return;
      const visible = result.items.filter((item) => Object.hasOwn(STATES, item.status));
      const ready = visible.filter((item) => item.status === 'ready' && !this.items.some((old) =>
        old.application_id === item.application_id && old.status === 'ready'));
      this.items = visible;
      this.render();
      for (const item of ready) this.onReady(item);
    } catch (error) {
      if (scope === this.scope && error.name !== 'AbortError' && this.dialog.open) this.showError(error);
    } finally {
      if (this.polling === scope) this.polling = null;
    }
  }

  syncDock() {
    const count = this.items?.length || 0;
    this.dock.hidden = count === 0;
    this.toggle.textContent = `数据源申请${count ? ` · ${count}` : ''}`;
  }

  render() {
    this.syncDock();
    const signature = JSON.stringify(this.items);
    if (signature === this.signature) return;
    this.signature = signature;
    const focused = document.activeElement?.dataset?.sourceAction;
    this.list.replaceChildren(...this.items.map((item) => this.row(item)));
    if (focused && this.dialog.open) {
      const restored = [...this.list.querySelectorAll('button')].find((button) => button.dataset.sourceAction === focused);
      (restored || byId('source-requests-close')).focus({ preventScroll: true });
    }
    if (!this.items.length && this.dialog.open) this.dialog.close();
  }

  row(item) {
    const { connector, status } = item;
    const card = el('article', { class: 'source-request-item' });
    card.append(el('div', { class: 'source-request-meta', text: `${item.project_name} · ${STATES[status]}` }),
      el('h3', { text: connector.name }));
    if (status === 'pending' || status === 'failed') {
      card.append(el('p', { text: item.reason }), el('p', { class: 'field-help', text: connector.scope }));
    }
    if (status === 'pending') {
      card.append(el('p', { class: 'field-help', text: '批准只用公开样例试接。日后选用此库时，检索词会发送给该库。' }));
    } else if (status === 'ready') {
      card.append(el('p', { text: `已验证 ${item.probe.records} 条公开题录；可在新会话的数据源中选用。` }));
    } else if (status === 'failed') {
      card.append(el('p', { class: 'field-help', text: ERRORS[item.error_code] || '本次未取得合格回执，未添加数据源。' }));
    }
    if (status === 'pending') {
      const links = el('div', { class: 'source-request-links' });
      for (const [url, title] of [[connector.docs, '接入文档'], [connector.terms, '使用条款']]) {
        if (typeof url === 'string' && url.startsWith('https://')) {
          links.append(el('a', { href: url, target: '_blank', rel: 'noopener noreferrer', text: title }));
        }
      }
      if (item.conversation_id) {
        const context = el('button', { type: 'button', class: 'text-button', text: '查看来源对话' });
        context.addEventListener('click', () => { this.dialog.close(); void this.onOpenHistory(item.conversation_id); });
        links.append(context);
      }
      card.append(links);
    }
    const actions = status === 'pending' ? [['decline', '不接入'], ['approve', '批准接入']]
      : status === 'failed' ? [['close', '关闭'], ['retry', '重试接入']]
        : ['ready', 'outdated'].includes(status) ? [['close', '知道了']] : [];
    if (actions.length) {
      const footer = el('div', { class: 'source-request-actions' });
      for (const [action, text] of actions) {
        const button = el('button', {
          type: 'button', class: `button ${action === 'approve' || action === 'retry' ? 'button--primary' : 'button--quiet'}`,
          text, dataset: { sourceAction: `${item.application_id}:${action}` },
        });
        button.addEventListener('click', () => void this.decide(item, action));
        footer.append(button);
      }
      card.append(footer);
    }
    return card;
  }

  async decide(item, action) {
    const scope = this.scope;
    if (!scope || this.mutating) return;
    const mutation = {};
    this.mutating = mutation;
    this.error.hidden = true;
    const actions = [...this.list.querySelectorAll('.source-request-actions button')];
    for (const button of actions) button.disabled = true;
    try {
      const result = await decideSource(item, action, scope.controller.signal);
      if (scope !== this.scope) return;
      this.items = result.closed ? this.items.filter((old) => old.application_id !== item.application_id)
        : this.items.map((old) => old.application_id === item.application_id ? result : old);
      this.render();
    } catch (error) {
      if (scope === this.scope && error.name !== 'AbortError') this.showError(error);
    } finally {
      if (this.mutating === mutation) this.mutating = null;
      for (const button of actions) button.disabled = false;
      if (scope === this.scope) this.next = 0;
    }
  }

  showError(error) {
    this.error.textContent = error.message || '暂时无法更新申请。';
    this.error.hidden = false;
  }
}
