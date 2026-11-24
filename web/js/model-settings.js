const STORAGE_KEY = 'citation.model-selection.v1';
const PROTOCOL_PATHS = { chat_completions: '/chat/completions', responses: '/responses' };
const OFFICIAL = {
  deepseek: ['https://api.deepseek.com', 'chat_completions'],
  moonshot: ['https://api.moonshot.cn/v1', 'chat_completions'],
  openai: ['https://api.openai.com/v1', 'responses'],
};

function keyFor(scope) {
  return scope && scope !== 'guest' ? `${STORAGE_KEY}:${scope}` : STORAGE_KEY;
}

function normalizeUrl(value, protocol) {
  const urlError = '请输入公网 HTTPS 接口地址，不带凭据、查询参数或片段。';
  let url;
  if (typeof value !== 'string' || /[\s@%?#\\]/.test(value.trim())) throw new Error(urlError);
  try { url = new URL(value.trim()); } catch { throw new Error(urlError); }
  if (url.protocol !== 'https:' || url.port || !url.hostname) throw new Error(urlError);
  if (!url.hostname.includes('.') && !url.hostname.startsWith('[')) throw new Error(urlError);
  let path = url.pathname.replace(/\/+$/, '');
  for (const [type, suffix] of Object.entries(PROTOCOL_PATHS)) {
    if (!path.endsWith(suffix)) continue;
    if (protocol !== type) throw new Error('接口地址末尾与当前选择的 API 协议不一致。');
    path = path.slice(0, -suffix.length);
    break;
  }
  return `${url.origin}${path}`;
}

function validated(selection, providers, protocols) {
  if (!selection || typeof selection !== 'object') throw new Error('模型配置不完整。');
  const model = typeof selection.model === 'string' ? selection.model.trim() : '';
  const apiKey = typeof selection.api_key === 'string' ? selection.api_key.trim() : '';
  if (!model || model.length > 128 || !apiKey || apiKey.length > 512) {
    throw new Error('请输入有效的模型名称和 API Key。');
  }
  if (selection.api_type) {
    if (!protocols.includes(selection.api_type)) throw new Error('该 API 协议不可用。');
    if (!/^[!-~]+$/.test(apiKey)) throw new Error('API Key 不能含空格或控制字符。');
    return { api_type: selection.api_type, base_url: normalizeUrl(selection.base_url, selection.api_type), model, api_key: apiKey };
  }
  if (!providers.includes(selection.provider)) throw new Error('不支持该模型提供方。');
  return { provider: selection.provider, model, api_key: apiKey };
}

export function readModelSelection(storage, providers, protocols = [], scope) {
  try {
    const raw = storage?.getItem(keyFor(scope));
    return raw ? validated(JSON.parse(raw), providers, protocols) : null;
  } catch {
    try { storage?.removeItem(keyFor(scope)); } catch { /* current tab storage unavailable */ }
    return null;
  }
}

export function writeModelSelection(storage, selection, providers, protocols = [], scope) {
  if (selection === null) {
    try { storage?.removeItem(keyFor(scope)); } catch { /* current tab storage unavailable */ }
    return null;
  }
  const clean = validated(selection, providers, protocols);
  try { storage?.setItem(keyFor(scope), JSON.stringify(clean)); } catch { /* use current page memory */ }
  return clean;
}

export function modelRequestPayload(selection) {
  if (!selection) return null;
  const identity = selection.api_type ? { api_type: selection.api_type, base_url: selection.base_url }
    : { provider: selection.provider };
  return { ...identity, model: selection.model, api_key: selection.api_key };
}

export class ModelSettingsDialog {
  constructor({ onChange, getSettings, putSettings }) {
    const id = (name) => document.getElementById(name);
    this.dialog = id('model-dialog');
    this.form = id('model-form');
    this.provider = id('model-provider');
    this.protocol = id('model-protocol');
    this.url = id('model-url');
    this.name = id('model-name');
    this.key = id('model-api-key');
    this.fields = id('model-custom-fields');
    this.keyNote = id('model-key-note');
    this.error = id('model-error');
    this.saveButton = id('save-model-button');
    this.onChange = onChange;
    this.getSettings = getSettings;
    this.putSettings = putSettings;
    this.selection = null;
    this.saved = null;
    this.sync = false;
    this.refreshing = null;
    this.saving = null;
    this.providers = [];
    this.protocols = [];
    try { this.storage = window.sessionStorage; } catch { this.storage = null; }
    this.provider.addEventListener('change', () => this.showFields());
    this.form.addEventListener('submit', (event) => { event.preventDefault(); void this.save(); });
    this.dialog.addEventListener('click', (event) => {
      if (!this.saving && event.target.closest('button[value="cancel"]')) this.dialog.close('cancel');
    });
    this.dialog.addEventListener('cancel', (event) => { if (this.saving) event.preventDefault(); });
    this.dialog.addEventListener('close', () => {
      if (this.dialog.open) return;
      this.key.value = '';
      id('profile-button').focus({ preventScroll: true });
    });
  }

  setProviders(providers, protocols, defaultModel) {
    this.providers = providers.map((item) => item.provider);
    this.protocols = protocols.filter((item) => Object.hasOwn(PROTOCOL_PATHS, item.id)).map((item) => item.id);
    this.provider.length = 1;
    this.provider.options[0].text = defaultModel ? `服务端默认（${defaultModel}）` : '服务端默认模型';
    this.protocol.replaceChildren();
    if (this.protocols.length) {
      this.provider.add(new Option('自定义模型', 'custom'));
      for (const item of protocols) {
        if (this.protocols.includes(item.id)) this.protocol.add(new Option(item.display_name, item.id));
      }
    } else {
      for (const item of providers) this.provider.add(new Option(item.display_name, item.provider));
    }
    this.load();
  }

  get available() { return this.protocols.length > 0 || this.providers.length > 0; }
  get ready() { return !this.sync || this.saved !== null; }
  get isCustom() { return Boolean(this.sync ? this.saved?.model : this.selection); }
  payload() { return this.sync ? null : modelRequestPayload(this.selection); }

  load() {
    this.selection = readModelSelection(this.storage, this.providers, this.protocols, this.scope);
    this.onChange();
  }

  setScope(scope, { forget = false, sync = false } = {}) {
    if (scope === this.scope && sync === this.sync) return;
    this.resetRequests();
    if (forget) this.forgetLocalSelection();
    this.scope = scope;
    this.sync = sync;
    this.load();
  }

  forgetLocalSelection() {
    this.selection = writeModelSelection(this.storage, null, this.providers, this.protocols, this.scope);
  }

  resetRequests() {
    this.refreshing?.abort();
    this.saving?.abort();
    this.refreshing = this.saving = this.saved = null;
    this.key.value = '';
    if (this.dialog.open) this.dialog.close();
  }

  clear() {
    this.resetRequests();
    this.forgetLocalSelection();
    this.onChange();
  }

  async refresh({ opening = false } = {}) {
    if (!this.sync || (!opening && (this.dialog.open || this.refreshing))) return;
    this.refreshing?.abort();
    const controller = new AbortController();
    this.refreshing = controller;
    if (opening) this.showFields();
    try {
      const saved = await this.getSettings(controller.signal);
      if (this.refreshing !== controller) return;
      this.saved = saved;
      if (saved.model) this.forgetLocalSelection();
      this.onChange();
    } catch (error) {
      if (this.refreshing !== controller || error.name === 'AbortError') return;
      if (opening && this.dialog.open) this.showError(error);
    } finally {
      if (this.refreshing === controller) {
        this.refreshing = null;
        if (opening && this.dialog.open) {
          this.fillForm();
          if (document.activeElement === this.dialog) this.provider.focus();
        }
      }
    }
  }

  fillForm() {
    const current = this.saved?.model || this.selection;
    const preset = OFFICIAL[current?.provider];
    this.provider.value = current ? (this.protocols.length ? 'custom' : current.provider) : '';
    this.protocol.value = current?.api_type || preset?.[1] || this.protocols[0] || '';
    this.url.value = current?.base_url || preset?.[0] || '';
    this.name.value = current?.model || '';
    this.key.value = '';
    this.key.placeholder = current ? '留空保留原 Key（同地址、同协议）' : '输入 API Key';
    this.showFields();
  }

  open() {
    if (document.querySelector('dialog[open]')) return;
    this.error.hidden = true;
    this.fillForm();
    this.dialog.showModal();
    this.provider.focus();
    void this.refresh({ opening: true });
  }

  showFields() {
    const custom = Boolean(this.provider.value);
    const busy = Boolean(this.saving || this.refreshing);
    this.fields.hidden = !custom;
    this.protocol.parentElement.hidden = this.provider.value !== 'custom';
    this.url.parentElement.hidden = this.provider.value !== 'custom';
    for (const field of [this.provider, this.protocol, this.url, this.name, this.key]) field.disabled = busy;
    this.saveButton.disabled = busy || !this.ready;
    this.saveButton.textContent = this.saving ? '正在保存…' : this.sync && custom ? '同步到账号' : '应用到后续请求';
    this.keyNote.textContent = this.sync
      ? custom ? (this.selection && !this.saved?.model
        ? '可沿用当前标签的旧 Key，保存后其他设备登录即可使用。'
        : 'Key 加密保存，其他设备登录后即可使用。')
        : this.saved?.model ? '应用后会移除此账号保存的 Key。' : ''
      : custom ? 'Key 仅保留在此标签页。请只填写信任的服务。' : '';
    this.keyNote.hidden = !this.keyNote.textContent;
  }

  showError(error) {
    this.error.textContent = error.message;
    this.error.hidden = false;
  }

  buildDraft() {
    const mode = this.provider.value;
    if (!mode) return null;
    const previous = this.saved?.model || this.selection;
    if (mode !== 'custom') {
      return { provider: mode, model: this.name.value,
        api_key: this.key.value || (mode === previous?.provider ? previous.api_key : '') };
    }
    const apiType = this.protocol.value;
    const base = normalizeUrl(this.url.value, apiType);
    const preset = OFFICIAL[previous?.provider];
    const sameEndpoint = (previous?.base_url || preset?.[0]) === base
      && (previous?.api_type || preset?.[1]) === apiType;
    const draft = { api_type: apiType, base_url: base, model: this.name.value };
    if (!this.sync || this.key.value.trim() || sameEndpoint && previous?.api_key) {
      draft.api_key = this.key.value.trim() || (sameEndpoint ? previous?.api_key : '');
    }
    return draft;
  }

  async save() {
    if (this.saving || this.refreshing || !this.ready) return;
    this.error.hidden = true;
    let controller;
    try {
      const draft = this.buildDraft();
      if (this.sync) {
        controller = new AbortController();
        this.saving = controller;
        this.showFields();
        const saved = await this.putSettings({ revision: this.saved.revision, model: draft }, controller.signal);
        if (this.saving !== controller) return;
        this.saved = saved;
        this.forgetLocalSelection();
      } else {
        this.selection = writeModelSelection(this.storage, draft, this.providers, this.protocols, this.scope);
      }
      this.onChange();
      this.dialog.close('saved');
    } catch (error) {
      if (error.name !== 'AbortError' && (!controller || this.saving === controller)) this.showError(error);
    } finally {
      if (controller && this.saving === controller) {
        this.saving = null;
        this.showFields();
      }
    }
  }
}
