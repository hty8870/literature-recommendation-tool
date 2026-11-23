import { deleteConversation, listConversations, renameConversation } from './api.js';
import { icon } from './icons.js';

const byId = (id) => document.getElementById(id);
const dateFormat = new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric' });

function makeButton(text, className = '') {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = className;
  button.textContent = text;
  return button;
}

export class WorkspaceHistory {
  constructor({ onNew, onOpen, onLogin, onDeleted, onNotice, activeId }) {
    Object.assign(this, { onNew, onOpen, onLogin, onDeleted, onNotice, activeId });
    this.panel = byId('history-panel');
    this.trigger = byId('new-search-button');
    this.label = byId('session-label');
    this.chevron = byId('session-chevron');
    this.newButton = byId('history-new');
    this.cluster = byId('history-cluster');
    this.list = byId('history-items');
    this.state = byId('history-state');
    this.more = byId('history-more');
    this.editor = byId('history-editor');
    this.editorForm = byId('history-editor-form');
    this.title = byId('history-editor-title');
    this.name = byId('history-editor-name');
    this.message = byId('history-editor-message');
    this.error = byId('history-editor-error');
    this.confirm = byId('history-editor-confirm');
    this.accountId = null;
    this.view = 'home';
    this.items = [];
    this.cursor = null;
    this.projectId = null;
    this.enabled = false;
    this.pinned = false;
    this.bind();
  }

  get popoverOpen() { return this.view === 'results' && !this.panel.hidden; }

  bind() {
    this.trigger.addEventListener('click', () => {
      if (!this.enabled) this.onNew();
      else if (!this.panel.hidden && this.pinned) this.close(true);
      else this.open(true);
    });
    this.trigger.addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowDown' || !this.enabled) return;
      event.preventDefault();
      this.open(true);
      if (!this.panel.hidden) this.newButton.focus({ preventScroll: true });
    });
    this.newButton.addEventListener('click', () => { this.close(true); this.onNew(); });
    this.cluster.addEventListener('pointerenter', (event) => {
      if (event.pointerType !== 'mouse' || !this.enabled || this.view !== 'results') return;
      clearTimeout(this.closeTimer);
      if (this.panel.hidden) this.openTimer = setTimeout(() => this.open(false), 140);
    });
    this.cluster.addEventListener('pointerleave', () => {
      clearTimeout(this.openTimer);
      if (!this.popoverOpen || this.pinned) return;
      this.closeTimer = setTimeout(() => {
        if (this.popoverOpen && !this.pinned && !this.panel.contains(document.activeElement)) this.close();
      }, 180);
    });
    const dismissOutside = (event) => {
      if (this.editor.open) return;
      if (this.rowMenu && !this.rowMenu.menu.contains(event.target) && !this.rowMenu.trigger.contains(event.target)) this.closeRowMenu();
      if (this.view === 'results' && !this.cluster.contains(event.target)) this.close();
    };
    document.addEventListener('pointerdown', dismissOutside);
    document.addEventListener('focusin', dismissOutside);
    document.addEventListener('keydown', (event) => {
      if (event.defaultPrevented || event.key !== 'Escape' || this.panel.hidden || document.querySelector('dialog[open]')) return;
      if (this.rowMenu) this.closeRowMenu(true);
      else if (this.view === 'results') this.close(this.panel.contains(document.activeElement));
      else return;
      event.preventDefault();
    });
    this.more.addEventListener('click', () => void this.load(Boolean(this.cursor)));
    this.editorForm.addEventListener('submit', (event) => { event.preventDefault(); void this.commit(); });
    byId('history-editor-cancel').addEventListener('click', () => this.editor.close());
    this.editor.addEventListener('cancel', (event) => { if (this.confirm.disabled) event.preventDefault(); });
    this.editor.addEventListener('close', () => { this.editing = null; });
  }

  setIdentity(enabled, account) {
    const id = account?.id ?? null;
    if (enabled === this.enabled && id === this.accountId) return;
    this.close();
    if (this.editor.open) this.editor.close();
    this.items = [];
    this.cursor = null;
    this.list.replaceChildren();
    this.enabled = enabled;
    this.accountId = id;
    this.syncView();
  }

  setProject(projectId) {
    if (this.projectId === projectId) return;
    this.projectId = projectId;
    this.cursor = null;
    this.items = [];
    this.closeRowMenu();
    this.list.replaceChildren();
    if (!this.panel.hidden) void this.load();
  }

  setView(view) {
    if (view === this.view) return;
    this.close();
    this.view = view;
    this.syncView();
  }

  syncView() {
    const results = this.view === 'results';
    const disclosure = results && this.enabled;
    const parent = results ? this.cluster : byId('main-content');
    if (this.panel.parentElement !== parent) parent.append(this.panel);
    this.trigger.hidden = !results;
    this.label.textContent = disclosure ? '会话' : '新建会话';
    this.chevron.toggleAttribute('hidden', !disclosure);
    this.newButton.hidden = !disclosure;
    if (disclosure) {
      this.trigger.setAttribute('aria-controls', this.panel.id);
      this.trigger.setAttribute('aria-expanded', 'false');
    } else {
      this.trigger.removeAttribute('aria-controls');
      this.trigger.removeAttribute('aria-expanded');
    }
    if (this.view === 'home' && this.enabled) this.show();
  }

  show() {
    if (!this.panel.hidden) return;
    this.panel.hidden = false;
    this.items = [];
    this.cursor = null;
    this.list.replaceChildren();
    void this.load();
  }

  open(pinned) {
    if (!this.enabled || this.view !== 'results' || document.querySelector('dialog[open]')) return;
    if (!pinned && !byId('profile-menu').hidden) return;
    this.pinned ||= pinned;
    this.trigger.setAttribute('aria-expanded', 'true');
    this.show();
  }

  close(focus = false) {
    clearTimeout(this.openTimer);
    clearTimeout(this.closeTimer);
    this.loader?.abort();
    this.loader = null;
    this.pinned = false;
    this.closeRowMenu();
    if (this.trigger.hasAttribute('aria-expanded')) this.trigger.setAttribute('aria-expanded', 'false');
    if (focus && !this.trigger.hidden) this.trigger.focus({ preventScroll: true });
    this.panel.hidden = true;
  }

  async load(append = false) {
    this.loader?.abort();
    const owner = this.accountId;
    if (!owner) {
      this.state.hidden = true;
      const button = makeButton('登录查看历史', 'text-button history-panel__login');
      button.addEventListener('click', () => {
        if (this.view === 'results') this.close(true);
        this.onLogin();
      });
      const item = document.createElement('li');
      item.append(button);
      this.list.replaceChildren(item);
      this.more.hidden = true;
      return;
    }
    const requestedCursor = append ? this.cursor : null;
    const loader = new AbortController();
    this.loader = loader;
    this.more.hidden = true;
    this.state.hidden = false;
    this.state.textContent = append ? '正在加载更多…' : '正在读取对话…';
    try {
      const page = await listConversations(requestedCursor, loader.signal, this.projectId);
      if (loader.signal.aborted || owner !== this.accountId) return;
      this.items = append ? [...this.items, ...page.items.filter((row) => !this.items.some((old) => old.conversation_id === row.conversation_id))] : page.items;
      this.cursor = page.next_cursor || null;
      this.render();
    } catch (error) {
      if (loader.signal.aborted || error.name === 'AbortError') return;
      this.state.textContent = '暂时无法读取对话历史。';
      this.more.textContent = '重试';
      this.more.hidden = false;
    } finally {
      if (this.loader === loader) this.loader = null;
    }
  }

  render() {
    this.closeRowMenu();
    this.state.hidden = this.items.length > 0;
    this.state.textContent = '暂无对话';
    this.more.hidden = !this.cursor;
    this.more.textContent = '加载更多';
    this.list.replaceChildren(...this.items.map((row) => this.row(row)));
  }

  row(row) {
    const item = document.createElement('li');
    item.className = 'history-row';
    const open = makeButton('', 'history-row__open');
    const title = document.createElement('span');
    title.textContent = row.title;
    title.title = row.title;
    const date = document.createElement('time');
    date.dateTime = row.updated_at;
    const time = new Date(row.updated_at);
    date.textContent = Number.isNaN(time.getTime()) ? '' : dateFormat.format(time);
    open.append(title, date);
    if (row.conversation_id === this.activeId()) open.setAttribute('aria-current', 'true');
    open.addEventListener('click', () => {
      if (this.view === 'results') this.close(true);
      else this.closeRowMenu();
      void this.onOpen(row.conversation_id);
    });
    const more = makeButton('', 'history-row__more');
    more.setAttribute('aria-label', `操作：${row.title}`);
    more.setAttribute('aria-expanded', 'false');
    more.append(icon('ellipsis'));
    more.addEventListener('click', () => this.openRowMenu(item, more, row));
    item.append(open, more);
    return item;
  }

  openRowMenu(item, trigger, row) {
    const wasOpen = this.rowMenu?.trigger === trigger;
    this.closeRowMenu();
    if (wasOpen) return;
    const menu = document.createElement('div');
    menu.className = 'history-row__actions';
    const rename = makeButton('重命名');
    const remove = makeButton('删除对话', 'danger-action');
    rename.addEventListener('click', () => this.edit(row, 'rename'));
    remove.addEventListener('click', () => this.edit(row, 'delete'));
    menu.append(rename, remove);
    item.append(menu);
    this.rowMenu = { menu, trigger };
    trigger.setAttribute('aria-expanded', 'true');
    rename.focus();
  }

  closeRowMenu(focus = false) {
    if (!this.rowMenu) return;
    this.rowMenu.trigger.setAttribute('aria-expanded', 'false');
    if (focus) this.rowMenu.trigger.focus();
    this.rowMenu.menu.remove();
    this.rowMenu = null;
  }

  edit(row, action) {
    this.closeRowMenu(true);
    this.pinned = true;
    this.editing = { row, action, accountId: this.accountId };
    this.title.textContent = action === 'delete' ? '删除这段对话？' : '重命名对话';
    this.message.hidden = action !== 'delete';
    this.message.textContent = `「${row.title}」的消息和专属榜单将删除；不会删除论文库或上传的原稿。`;
    this.name.parentElement.hidden = action === 'delete';
    this.name.value = row.title;
    this.error.hidden = true;
    this.confirm.disabled = false;
    this.confirm.textContent = action === 'delete' ? '删除对话' : '保存';
    this.confirm.classList.toggle('button--danger', action === 'delete');
    this.editor.showModal();
    (action === 'delete' ? byId('history-editor-cancel') : this.name).focus();
    if (action === 'rename') this.name.select();
  }

  async commit() {
    const editing = this.editing;
    if (!editing || this.confirm.disabled) return;
    this.confirm.disabled = true;
    this.error.hidden = true;
    try {
      const id = editing.row.conversation_id;
      if (editing.action === 'delete') await deleteConversation(id);
      else await renameConversation(id, this.name.value.trim());
      if (editing !== this.editing || editing.accountId !== this.accountId) return;
      this.editor.close();
      if (editing.action === 'delete') {
        this.onDeleted(id);
        this.onNotice('对话已删除。');
      }
      if (!this.panel.hidden) void this.load();
    } catch (error) {
      if (editing !== this.editing || error.name === 'AbortError') return;
      this.error.textContent = error.message;
      this.error.hidden = false;
    } finally {
      this.confirm.disabled = false;
    }
  }
}
