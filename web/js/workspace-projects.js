import { createProject, getProject, listProjects, updateProject } from './api.js';

export const OTHER_PROJECT = 'other';
const byId = (id) => document.getElementById(id);

export class WorkspaceProjects {
  constructor({ onDraft, onScope, onNotice }) {
    Object.assign(this, { onDraft, onScope, onNotice });
    this.controls = byId('projects-control');
    this.picker = byId('project-picker');
    this.settings = byId('project-settings');
    this.chatButton = byId('chat-project');
    this.dialog = byId('project-dialog');
    this.form = byId('project-form');
    this.name = byId('project-name');
    this.nameField = byId('project-name-field');
    this.nameSave = byId('project-save');
    this.title = byId('project-dialog-title');
    this.share = byId('project-share');
    this.shareRow = byId('project-share-row');
    this.learn = byId('project-learn');
    this.learnRow = byId('project-learn-row');
    this.archive = byId('project-archive');
    this.error = byId('project-error');
    this.scope = null;
    this.target = OTHER_PROJECT;
    this.chatId = null;
    this.items = [];
    this.revision = 0;
    this.picker.addEventListener('change', () => {
      if (this.picker.value === 'create') {
        this.picker.value = this.target;
        this.open('create');
      } else {
        void this.select(this.picker.value);
      }
    });
    this.settings.addEventListener('click', () => this.open(this.target));
    this.chatButton.addEventListener('click', () => {
      if (this.chatId) this.open(this.chatId);
    });
    this.form.addEventListener('submit', (e) => { e.preventDefault(); void this.saveName(); });
    this.share.addEventListener('click', () => void this.change('share_sources'));
    this.learn.addEventListener('click', () => void this.change('learning_enabled'));
    this.archive.addEventListener('click', () => void this.change('archived'));
    byId('project-close').addEventListener('click', () => this.dialog.close());
    this.dialog.addEventListener('cancel', (e) => { if (this.editBusy) e.preventDefault(); });
  }

  get enabled() { return Boolean(this.scope); }
  get chatArchived() { return this.items.some((item) => item.project_id === this.chatId && item.archived); }

  setIdentity(accountId) {
    if (this.scope?.accountId === accountId || (!this.scope && !accountId)) return;
    this.scope?.controller.abort();
    this.scope = accountId ? { accountId, controller: new AbortController() } : null;
    if (this.dialog.open) this.dialog.close();
    this.controls.hidden = !accountId;
    this.items = [];
    this.draft = null;
    this.setChat(null);
    this.select(OTHER_PROJECT);
    if (this.scope) void this.load();
  }

  async load() {
    const scope = this.scope;
    if (!scope) return;
    try {
      const data = await listProjects(scope.controller.signal);
      if (scope !== this.scope) return;
      this.items = data.items;
      this.learning = data.learning_available === true;
      this.renderPicker();
      this.onScope(this.learning);
    } catch (error) { this.report(error, scope); }
  }

  renderPicker() {
    const items = this.items.length ? this.items : [{ project_id: OTHER_PROJECT, name: '其它' }];
    this.picker.replaceChildren(...items.map((item) => new Option(
      `${item.name}${item.archived ? '（已归档）' : ''}`, item.project_id,
    )), new Option('＋ 新建项目…', 'create'));
    this.picker.value = this.target;
    this.renderChat();
  }

  setChat(id) {
    this.chatId = this.scope && id ? id : null;
    this.renderChat();
  }

  renderChat() {
    this.chatButton.hidden = !this.chatId;
    const item = this.items.find((row) => row.project_id === this.chatId);
    this.chatButton.textContent = item?.name || (this.chatId === OTHER_PROJECT ? '其它' : '项目设置');
    this.chatButton.title = item?.name || '项目设置';
    this.chatButton.setAttribute('aria-label', `项目设置：${this.chatButton.textContent}`);
  }

  async select(id, { preserve = false } = {}) {
    const scope = this.scope;
    const version = ++this.revision;
    this.target = scope ? id : OTHER_PROJECT;
    this.picker.value = this.target;
    if (!scope) { this.onDraft(null, false, false, false); return; }
    this.pending = true;
    if (!preserve) this.draft = null;
    this.onDraft(this.draft, true, false, preserve);
    try {
      const project = await getProject(id, scope.controller.signal);
      if (scope !== this.scope || version !== this.revision) return;
      this.draft = project;
      this.onDraft(project, false, project.archived, preserve);
    } catch (error) {
      if (scope !== this.scope || version !== this.revision) return;
      this.onDraft(null, false, true, false);
      this.report(error, scope);
    } finally {
      if (scope === this.scope && version === this.revision) this.pending = false;
    }
  }

  async useSources(ids) {
    const scope = this.scope;
    const project = this.draft;
    const ticket = this.revision;
    if (!scope || !project?.share_sources || this.pending) return;
    if (!ids.length) {
      this.onDraft(project, false, false, false);
      this.onNotice('项目至少保留一个数据源。', { level: 'error' });
      return;
    }
    this.pending = true;
    this.onDraft(project, true, false, true);
    try {
      const updated = await updateProject(project.project_id, {
        revision: project.revision, library_ids: ids,
      }, scope.controller.signal);
      if (scope !== this.scope || ticket !== this.revision) return;
      this.draft = updated;
      this.onDraft(updated, false, false, false);
    } catch (error) {
      if (scope !== this.scope || ticket !== this.revision) return;
      this.report(error, scope);
      await this.select(project.project_id);
    } finally {
      if (scope === this.scope && ticket === this.revision) this.pending = false;
    }
  }

  async open(id) {
    const scope = this.scope;
    if (!scope || document.querySelector('dialog[open]')) return;
    const editor = { id, scope, value: null };
    this.editor = editor;
    this.title.textContent = id === 'create' ? '新建项目' : '项目设置';
    this.name.value = '';
    this.error.hidden = true;
    this.dialog.showModal();
    this.renderForm(editor);
    if (id === 'create') { this.name.focus(); return; }
    try {
      editor.value = await getProject(id, scope.controller.signal);
      if (this.editor === editor && this.dialog.open) this.renderForm(editor);
    } catch (error) { this.formError(error, editor); }
  }

  renderForm(editor) {
    const { id, value } = editor;
    const creating = id === 'create';
    this.title.textContent = value ? `${value.name} · 项目设置` : creating ? '新建项目' : '项目设置';
    this.name.value = value?.name || this.name.value;
    this.nameField.hidden = !(creating || (value && id !== OTHER_PROJECT));
    this.nameSave.hidden = this.nameField.hidden;
    this.nameSave.textContent = creating ? '新建' : '保存名称';
    this.shareRow.hidden = !value || id === OTHER_PROJECT || value.archived;
    this.learnRow.hidden = !value || !this.learning || value.archived;
    this.archive.hidden = !value || id === OTHER_PROJECT;
    this.archive.textContent = value?.archived ? '恢复项目' : '归档项目';
    this.share.setAttribute('aria-checked', String(Boolean(value?.share_sources)));
    this.learn.setAttribute('aria-checked', String(Boolean(value?.learning_enabled)));
  }

  saveName() {
    const value = this.name.value.trim();
    if (!value) { this.formError(new Error('请输入项目名称。'), this.editor); this.name.focus(); return; }
    return this.mutate({ name: value });
  }

  change(key) {
    return this.editor?.value && this.mutate({ [key]: !this.editor.value[key] });
  }

  async mutate(patch) {
    const editor = this.editor;
    if (!editor || this.editBusy) return;
    const { id, scope, value } = editor;
    this.editBusy = true;
    this.error.hidden = true;
    const inputs = [...this.form.querySelectorAll('button:not(#project-close), input')];
    for (const input of inputs) input.disabled = true;
    this.form.setAttribute('aria-busy', 'true');
    try {
      const result = id === 'create' ? await createProject(patch.name, scope.controller.signal)
        : await updateProject(id, { ...patch, revision: value.revision }, scope.controller.signal);
      if (scope !== this.scope || this.editor !== editor) return;
      editor.value = result;
      if (patch.name) this.dialog.close();
      else this.renderForm(editor);
      if (id === 'create' || patch.name || 'archived' in patch) await this.load();
      if (scope !== this.scope) return;
      if (id === 'create' || this.target === id) await this.select(result.project_id);
      if (scope === this.scope && 'archived' in patch) this.onNotice(result.archived
        ? '项目已归档，旧会话仍可查看。' : '项目已恢复。');
    } catch (error) {
      this.formError(error, editor);
      if (error.code === 'PROJECT_CHANGED' && this.scope === scope && this.dialog.open) {
        try {
          editor.value = await getProject(id, scope.controller.signal);
          if (this.editor === editor && scope === this.scope) this.renderForm(editor);
        } catch (readError) { this.report(readError, scope); }
      }
    } finally {
      this.editBusy = false;
      for (const input of inputs) input.disabled = false;
      this.form.removeAttribute('aria-busy');
    }
  }

  formError(error, editor) {
    if (editor !== this.editor || !this.dialog.open || error.name === 'AbortError') return;
    this.error.textContent = error.message || '未能保存项目。';
    this.error.hidden = false;
  }

  report(error, scope) {
    if (scope !== this.scope || error.name === 'AbortError') return;
    this.onNotice(error.message || '暂时无法读取项目。', { level: 'error' });
  }
}
