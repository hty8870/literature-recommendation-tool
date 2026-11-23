import { accountAction } from './api.js';
import { copyText } from './clipboard.js';

const id = (name) => document.getElementById(name);
const avatarSegments = typeof Intl.Segmenter === 'function' ? new Intl.Segmenter() : null;
const modes = {
  login: { title: '登录', action: '登录' },
  register: { title: '创建账户', action: '创建账户' },
  recover: { title: '用恢复码重设密码', action: '重设并登录' },
  profile: { title: '账户设置', action: '保存名称' },
  password: { title: '修改密码', action: '更新密码' },
  delete: { title: '永久注销账户？', action: '注销账户' },
};

function node(tag, text = '', className = '') {
  const element = document.createElement(tag);
  element.className = className;
  if (text) element.textContent = text;
  return element;
}

function button(text, action, className = 'button button--quiet') {
  const element = node('button', text, className);
  element.type = 'button';
  element.addEventListener('click', action);
  return element;
}

function field(form, label, name, options = {}) {
  const wrapper = node('label', label, 'form-field');
  const input = document.createElement('input');
  input.name = name;
  input.spellcheck = false;
  Object.assign(input, options);
  wrapper.append(input);
  form.append(wrapper);
  return input;
}

export class WorkspaceAccount {
  constructor({ onChange, onModel, onNotice }) {
    Object.assign(this, { onChange, onModel, onNotice });
    this.dialog = id('account-dialog');
    this.body = id('account-dialog-body');
    this.trigger = id('profile-button');
    this.menu = id('profile-menu');
    this.close = id('account-close');
    this.account = null;
    this.bind();
  }

  bind() {
    this.trigger.addEventListener('click', () => this.toggleMenu(this.menu.hidden));
    this.trigger.addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowDown') return;
      event.preventDefault();
      this.toggleMenu(true);
      this.menu.querySelector('button:not([hidden])')?.focus();
    });
    document.addEventListener('pointerdown', (event) => {
      if (!this.menu.hidden && !id('profile-control').contains(event.target)) this.toggleMenu(false);
    });
    id('profile-control').addEventListener('focusout', (event) => {
      if (!id('profile-control').contains(event.relatedTarget)) this.toggleMenu(false);
    });
    id('profile-control').addEventListener('keydown', (event) => {
      if (event.key !== 'Escape' || this.menu.hidden) return;
      event.preventDefault();
      this.toggleMenu(false);
      this.trigger.focus();
    });
    id('profile-login').addEventListener('click', () => this.open('login'));
    id('profile-settings').addEventListener('click', () => this.open('profile'));
    id('profile-logout').addEventListener('click', () => void this.logout());
    id('model-settings-button').addEventListener('click', () => {
      this.toggleMenu(false);
      this.onModel();
    });
    this.close.addEventListener('click', () => { if (!this.busy) this.dialog.close(); });
    this.dialog.addEventListener('cancel', (event) => { if (this.busy) event.preventDefault(); });
    this.dialog.addEventListener('close', () => {
      if (this.dialog.open) return;
      this.body.replaceChildren();
      this.trigger.focus({ preventScroll: true });
    });
  }

  toggleMenu(open) {
    this.menu.hidden = !open;
    this.trigger.setAttribute('aria-expanded', String(open));
  }

  setIdentity(account, { enabled, models }) {
    this.account = account;
    id('profile-control').hidden = !enabled && !models;
    id('profile-login').hidden = !enabled || Boolean(account);
    id('profile-settings').hidden = !account;
    id('profile-logout').hidden = !account;
    id('model-settings-button').hidden = !models;
    id('profile-label').textContent = account?.display_name || '公共工作区';
    id('profile-detail').textContent = account ? `@${account.username}` : '访客 · 不同步对话';
    const name = account?.display_name?.trim() || account?.username || '';
    const initial = avatarSegments?.segment(name).containing(0)?.segment || Array.from(name)[0] || '';
    id('profile-initial').textContent = initial.replace(/^[a-z]$/, (letter) => letter.toUpperCase());
    id('profile-initial').hidden = !initial;
    id('profile-icon').toggleAttribute('hidden', Boolean(initial));
    this.trigger.setAttribute('aria-label', account ? `用户菜单：${name}` : '用户菜单：公共工作区');
  }

  reset() {
    this.toggleMenu(false);
    if (this.dialog.open) this.dialog.close();
  }

  open(mode) {
    if (this.busy || (!this.dialog.open && document.querySelector('dialog[open]'))) return;
    this.toggleMenu(false);
    const config = modes[mode];
    id('account-dialog-title').textContent = config.title;
    const form = node('form', '', 'account-form');
    form.method = 'dialog';
    let first;
    if (mode === 'profile') {
      form.append(node('p', `@${this.account.username}`, 'field-help'));
      first = field(form, '显示名称', 'display_name', { required: true, maxLength: 48, value: this.account.display_name });
    } else if (mode === 'delete') {
      form.append(node('p', '此账户及其对话、榜单和稿件将从本服务删除，无法恢复；不影响公共工作区、备份或模型提供方保存的记录。', 'field-help'));
      first = field(form, '输入当前密码确认', 'current_password', { type: 'password', required: true, autoComplete: 'current-password', maxLength: 128 });
    } else if (mode === 'password') {
      first = field(form, '当前密码', 'current_password', { type: 'password', required: true, autoComplete: 'current-password', maxLength: 128 });
      field(form, '新密码（至少 12 个字符）', 'new_password', { type: 'password', required: true, autoComplete: 'new-password', minLength: 12, maxLength: 128 });
      form.append(node('p', '更新密码会注销其他设备，并更换恢复码。', 'field-help'));
    } else {
      first = field(form, '账户名', 'username', { required: true, maxLength: 32, autoComplete: 'username', autocapitalize: 'none' });
      if (mode === 'register') {
        first.placeholder = '3–32 位字母、数字或 ._-';
        first.minLength = 3;
        first.pattern = '[A-Za-z0-9][A-Za-z0-9._\\-]{2,31}';
        field(form, '显示名称（可选）', 'display_name', { maxLength: 48 });
      }
      if (mode === 'recover') field(form, '一次性恢复码', 'recovery_code', { required: true, maxLength: 128, autoComplete: 'off' });
      field(form, mode === 'login' ? '密码' : '新密码（至少 12 个字符）', 'password', {
        type: 'password', required: true, minLength: mode === 'login' ? 1 : 12,
        maxLength: 128, autoComplete: mode === 'login' ? 'current-password' : 'new-password',
      });
    }
    const error = node('p', '', 'form-error');
    error.setAttribute('role', 'alert');
    error.hidden = true;
    form.append(error);
    const actions = node('div', '', 'form-actions account-actions');
    const submit = node('button', config.action, `button button--primary${mode === 'delete' ? ' button--danger' : ''}`);
    submit.type = 'submit';
    if (mode === 'login') actions.append(button('创建账户', () => this.open('register')), button('忘记密码', () => this.open('recover'), 'text-button'));
    else if (mode === 'profile') actions.append(button('修改密码', () => this.open('password')));
    else {
      const fromSettings = ['delete', 'password'].includes(mode);
      actions.append(button(fromSettings ? '返回设置' : '返回登录', () => this.open(fromSettings ? 'profile' : 'login')));
    }
    actions.append(submit);
    form.append(actions);
    if (mode === 'profile') form.append(button('注销账户', () => this.open('delete'), 'text-button account-delete-link'));
    if (mode === 'login') form.append(node('p', '登录只用于保存个人历史。关闭弹窗即可继续在公共工作区检索。', 'field-help account-hint'));
    if (mode === 'register') form.append(node('p', '原有访客对话不会迁入个人账户。创建后请保存一次性恢复码。', 'field-help account-hint'));
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      void this.submit(mode, form, error);
    });
    this.body.replaceChildren(form);
    if (!this.dialog.open) this.dialog.showModal();
    first?.focus();
  }

  async submit(mode, form, error) {
    if (this.busy) return;
    const payload = Object.fromEntries(new FormData(form));
    this.busy = true;
    this.close.disabled = true;
    error.hidden = true;
    form.setAttribute('aria-busy', 'true');
    for (const control of form.querySelectorAll('button')) control.disabled = true;
    try {
      const result = await accountAction(mode, payload);
      await this.onChange(result?.account || null, mode !== 'profile');
      if (result?.recovery_code) this.showRecovery(result.recovery_code);
      else {
        if (this.dialog.open) this.dialog.close();
        this.onNotice(mode === 'delete' ? '账户已注销。' : (mode === 'profile' ? '名称已保存。' : '已登录。'));
      }
    } catch (failure) {
      if (!form.isConnected) return;
      error.textContent = failure.message;
      error.hidden = false;
      form.querySelector('input[type="password"]')?.focus();
    } finally {
      this.busy = false;
      this.close.disabled = false;
      form.removeAttribute('aria-busy');
      for (const control of form.querySelectorAll('button')) control.disabled = false;
    }
  }

  showRecovery(code) {
    id('account-dialog-title').textContent = '保存一次性恢复码';
    const hint = node('p', '忘记密码时可用它重设。这是唯一一次显示，请妥善保存。', 'field-help');
    const secret = node('code', code, 'recovery-code');
    const actions = node('div', '', 'form-actions');
    const copy = button('复制恢复码', async () => {
      try { await copyText(code); this.onNotice('恢复码已复制。'); }
      catch { this.onNotice('无法复制，请手动选中恢复码。', { level: 'error' }); }
    });
    const done = button('我已保存', () => this.dialog.close(), 'button button--primary');
    actions.append(copy, done);
    this.body.replaceChildren(hint, secret, actions);
    if (!this.dialog.open) this.dialog.showModal();
    copy.focus();
  }

  async logout() {
    if (this.busy) return;
    this.toggleMenu(false);
    this.trigger.focus();
    this.busy = true;
    try {
      await accountAction('logout');
      await this.onChange(null, true);
      this.onNotice('已退出，当前为公共工作区。');
    } catch (error) {
      this.onNotice(error.message, { level: 'error' });
    } finally { this.busy = false; }
  }
}
