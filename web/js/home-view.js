import { clear, el, setHidden, setText } from './dom.js';
import { failureMessage } from './chat-model.js';
import {
  DEFAULT_UPLOAD_BYTES,
  INPUT_MODES,
  InputError,
  buildSearchPayload,
  libraryPillLabel,
  uploadLimitLabel,
  validateManuscriptFile,
} from './search-input.js';

const byId = (id) => document.getElementById(id);

const PARSE_LABELS = {
  queued: '排队等待解析',
  parsing: '正在解析',
  ready: '解析完成',
  failed: '解析失败',
};

export class HomeView {
  constructor({ onSearch, onSources, uploadManuscript, getManuscript, getUploadCapability, createUploadKey }) {
    this.onSources = onSources;
    this.createUploadKey = createUploadKey || (() => globalThis.crypto.randomUUID());
    this.onSearch = onSearch;
    this.uploadManuscript = uploadManuscript;
    this.getManuscript = getManuscript;
    this.getUploadCapability = getUploadCapability;
    this.form = byId('search-form');
    this.input = byId('keywords');
    this.modePicker = byId('mode-picker');
    this.modeTrigger = byId('mode-summary');
    this.modeMenu = byId('mode-menu');
    this.modeLabel = byId('mode-label');
    this.textInputWrap = byId('text-input-wrap');
    this.uploadControl = byId('upload-control');
    this.fileInput = byId('manuscript-file');
    this.uploadZone = byId('upload-zone');
    this.clearFileButton = byId('clear-file');
    this.startButton = byId('start-button');
    this.libraryList = byId('library-list');
    this.libraryNote = byId('library-note');
    this.formError = byId('form-error');
    this.fullTextNote = byId('full-text-note');
    this.manuscriptStatus = byId('manuscript-status');
    this.modeOptions = [...this.modeMenu.querySelectorAll('button[data-mode]')];
    this.mode = 'keywords';
    this.libraries = [];
    this.selectedLibraryIds = new Set();
    this.uploadEnabled = false;
    this.scanFailure = null;
    this.maxUploadBytes = DEFAULT_UPLOAD_BYTES;
    this.ocrEnabled = null;
    this.manuscript = null;
    this.selectedFile = null;
    this.uploadRetry = byId('upload-retry');
    this.retryUploadAction = null;
    this.uploadToken = 0;
    this.uploadController = null;
    this.ready = false;
    this.busy = false;
    this.bind();
    this.setMode('keywords');
  }

  bind() {
    this.startButton.addEventListener('click', () => this.form.requestSubmit());
    this.form.addEventListener('submit', (event) => {
      event.preventDefault();
      this.submit();
    });
    this.modeTrigger.addEventListener('click', () => this.toggleModeMenu(this.modeMenu.hidden));
    this.modePicker.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        this.toggleModeMenu(false);
        this.modeTrigger.focus();
      }
    });
    for (const button of this.modeOptions) {
      button.addEventListener('click', () => {
        this.setMode(button.dataset.mode);
        this.toggleModeMenu(false);
      });
    }
    document.addEventListener('click', (event) => {
      if (!this.modePicker.contains(event.target)) this.toggleModeMenu(false);
    });
    this.input.addEventListener('input', () => {
      this.resizeInput();
      this.clearError();
    });
    this.input.addEventListener('keydown', (event) => {
      if (event.isComposing || event.keyCode === 229) return;
      const submitWithEnter = this.mode === 'keywords' && event.key === 'Enter' && !event.shiftKey;
      const submitWithModifier = event.key === 'Enter' && (event.ctrlKey || event.metaKey);
      if (submitWithEnter || submitWithModifier) {
        event.preventDefault();
        this.form.requestSubmit();
      }
    });
    this.libraryList.addEventListener('click', (event) => {
      const button = event.target.closest('button[data-library-id]');
      if (!button) return;
      const id = button.dataset.libraryId;
      if (this.selectedLibraryIds.has(id)) this.selectedLibraryIds.delete(id);
      else this.selectedLibraryIds.add(id);
      this.updateLibraryButtons();
      this.clearError();
      this.onSources?.([...this.selectedLibraryIds]);
    });
    this.fileInput.addEventListener('change', () => this.selectFile());
    this.uploadZone.addEventListener('click', () => this.chooseUpload());
    this.uploadRetry.addEventListener('click', () => this.retryUploadAction?.());
    this.clearFileButton.addEventListener('click', () => {
      this.clearFile({ focus: true });
    });
  }

  setLibraries(libraries, {
    agentAvailable = true,
    uploadCapability = {},
    ocrEnabled = null,
  } = {}) {
    this.setSourceChoices(libraries, libraries.map((library) => library.library_id));
    this.ocrEnabled = typeof ocrEnabled === 'boolean' ? ocrEnabled : null;
    this.applyUploadCapability(uploadCapability);
    this.libraryList.setAttribute('aria-busy', 'false');
    this.setAgentAvailable(agentAvailable);
  }

  setSourceChoices(libraries, ids) {
    const keys = libraries.map((library) => library.library_id).join('|');
    const previous = this.libraries.map((library) => library.library_id).join('|');
    this.libraries = libraries;
    this.selectedLibraryIds = new Set(ids);
    if (keys !== previous || !this.libraryList.querySelector('button[data-library-id]')) {
      clear(this.libraryList);
      this.renderSourceChoices(libraries);
    }
    this.updateLibraryButtons();
  }

  renderSourceChoices(libraries) {
    for (const library of libraries) {
      const label = libraryPillLabel(library);
      const notice = library.source_type === 'remote_api'
        ? `检索词会发给该在线数据源。${library.notice}` : library.notice;
      const button = el('button', {
        type: 'button',
        class: 'source-pill',
        dataset: { libraryId: library.library_id },
        text: label,
        title: `${label} · ${notice}`,
        'aria-pressed': 'true',
      });
      this.libraryList.append(button);
    }
  }

  setSourceBusy(busy, blocked = false) {
    this.sourcesBusy = busy;
    this.sourcesBlocked = blocked;
    this.libraryList.setAttribute('aria-busy', String(busy));
    this.updateLibraryButtons();
    this.updateButtons();
  }

  setAgentAvailable(agentAvailable) {
    this.ready = this.libraries.length > 0 && agentAvailable;
    if (this.ready) this.clearError();
    if (this.libraries.length) {
      const note = !agentAvailable ? '暂无可用模型，请检查模型设置或服务端配置。'
        : this.libraries.some((library) => library.simulation === true) ? '包含模拟文献库，仅供演示。' : '';
      this.setLibraryNote(note);
    }
    this.updateButtons();
  }

  setLibraryNote(note) {
    setText(this.libraryNote, note);
    setHidden(this.libraryNote, !note);
  }

  setLoadingError(message) {
    this.libraries = [];
    this.selectedLibraryIds.clear();
    this.ready = false;
    clear(this.libraryList);
    this.libraryList.append(el('span', { class: 'source-loading', text: '数据源读取失败' }));
    this.libraryList.setAttribute('aria-busy', 'false');
    this.setLibraryNote('请刷新页面重试。');
    this.uploadEnabled = false;
    this.updateFullTextCapability();
    this.showError(message);
    this.updateButtons();
  }

  updateLibraryButtons() {
    for (const button of this.libraryList.querySelectorAll('button[data-library-id]')) {
      const selected = this.selectedLibraryIds.has(button.dataset.libraryId);
      button.classList.toggle('is-selected', selected);
      button.setAttribute('aria-pressed', String(selected));
      button.disabled = Boolean(this.sourcesBusy || this.sourcesBlocked);
    }
  }

  applyUploadCapability(capability) {
    this.uploadEnabled = capability.manuscript_upload_enabled === true;
    this.scanFailure = capability.manuscript_scan_failure || null;
    if (Number.isInteger(capability.max_upload_bytes) && capability.max_upload_bytes > 0) {
      this.maxUploadBytes = capability.max_upload_bytes;
    }
    this.updateFullTextCapability();
  }

  updateFullTextCapability() {
    this.fileInput.disabled = !this.uploadEnabled;
    this.uploadZone.disabled = !this.uploadEnabled && !this.scanFailure;
    this.updateFullTextNote();
    this.updateUploadStatus();
  }

  updateFullTextNote() {
    let note = '当前服务未开放全文上传；请使用关键词或 Abstract。';
    if (this.scanFailure) {
      note = this.scanFailure === 'MANUSCRIPT_SCAN_STALE'
        ? '文件安全扫描的病毒库暂不可用，暂不能上传。'
        : '文件安全扫描暂不可用，暂不能上传。';
    } else if (this.uploadEnabled) {
      note = '选择文件即上传解析；点击发送后，所选模型可能读取必要段落。匿名实例请勿上传私稿。';
      if (this.ocrEnabled === false) note += ' 当前未启用 OCR，扫描版可能无法解析。';
    }
    setText(this.fullTextNote, note);
  }

  chooseUpload() {
    if (this.uploadEnabled) {
      this.fileInput.value = '';
      this.fileInput.click();
    } else if (this.scanFailure) void this.refreshUploadCapability();
  }

  async refreshUploadCapability() {
    if (this.uploadZone.disabled) return;
    this.clearError();
    this.uploadZone.disabled = true;
    this.uploadZone.setAttribute('aria-busy', 'true');
    setText(byId('upload-label'), '正在检查上传状态…');
    try {
      this.applyUploadCapability(await this.getUploadCapability());
    } catch (error) {
      this.showError(`无法检查上传状态：${failureMessage(error)}`);
    } finally {
      this.uploadZone.removeAttribute('aria-busy');
      this.updateFullTextCapability();
    }
  }

  toggleModeMenu(open) {
    this.modeMenu.hidden = !open;
    this.modePicker.classList.toggle('is-open', open);
    this.modeTrigger.setAttribute('aria-expanded', String(open));
  }

  setMode(mode) {
    if (!INPUT_MODES[mode]) return;
    const previousMode = this.mode;
    this.mode = mode;
    const config = INPUT_MODES[mode];
    this.modeLabel.textContent = config.label;
    this.modeTrigger.setAttribute('aria-label', `检索模式：${config.label}，点击切换`);
    for (const button of this.modeOptions) {
      button.setAttribute('aria-pressed', String(button.dataset.mode === mode));
    }
    const fullText = mode === 'full_text';
    setHidden(this.textInputWrap, fullText);
    setHidden(this.uploadControl, !fullText);
    setHidden(this.fullTextNote, !fullText);
    this.input.disabled = fullText;
    this.input.wrap = mode === 'keywords' ? 'off' : 'soft';
    this.input.placeholder = config.placeholder;
    byId('query-input-label').textContent = mode === 'abstract' ? '输入论文摘要' : '输入检索关键词';
    byId('input-help').textContent = config.help;
    byId('search-composer').dataset.mode = mode;
    if (previousMode === 'full_text' && !fullText) this.clearFile({ focus: false });
    this.clearError();
    this.updateFullTextCapability();
    this.resizeInput();
    if (fullText) (this.uploadZone.disabled ? this.modeTrigger : this.uploadZone).focus();
    else this.input.focus();
  }

  selectFile() {
    const file = this.fileInput.files?.[0];
    if (!file) return;
    const error = validateManuscriptFile(file, this.maxUploadBytes);
    if (error) {
      this.clearFile();
      this.showError(error);
      return;
    }
    if (!this.uploadEnabled) {
      this.showError('当前服务不能上传全文。');
      return;
    }
    this.selectedFile = file;
    void this.beginUpload(file, this.createUploadKey());
  }

  async beginUpload(file, idempotencyKey, manuscriptId = null) {
    this.abortUpload();
    const token = this.uploadToken;
    const controller = new AbortController();
    const { signal } = controller;
    this.uploadController = controller;
    this.clearError();
    this.setUploadRetry(null);
    this.manuscript = { manuscript_id: manuscriptId, file_name: file.name,
      parse_status: manuscriptId ? 'parsing' : 'uploading' };
    this.updateUploadStatus();
    try {
      if (!manuscriptId) {
        const created = await this.uploadManuscript(file, { signal, idempotencyKey });
        if (token !== this.uploadToken) return;
        manuscriptId = typeof created?.manuscript_id === 'string' ? created.manuscript_id : '';
        if (!manuscriptId) throw new Error('服务端未返回稿件编号，请重新上传。');
        if (this.applyManuscriptStatus(created, file.name)) return;
      }
      await this.pollManuscript(manuscriptId, file.name, signal, token);
    } catch (error) {
      if (token !== this.uploadToken || error?.name === 'AbortError') return;
      this.manuscript.parse_status = manuscriptId ? 'status_unknown' : 'upload_failed';
      this.updateUploadStatus();
      this.showError(`${manuscriptId ? '无法确认解析状态' : '全文上传失败'}：${failureMessage(error, '请稍后重试。')}`);
      if (error?.retryable !== false && error?.status !== 404 && error?.status !== 410) {
        this.setUploadRetry(() => void this.beginUpload(file, idempotencyKey, manuscriptId),
          manuscriptId ? '重查解析状态' : '重新上传');
      }
    } finally {
      if (this.uploadController === controller) this.uploadController = null;
    }
  }

  applyManuscriptStatus(data, fileName) {
    this.manuscript = { ...data, file_name: fileName };
    this.updateUploadStatus();
    if (data.parse_status === 'failed') {
      this.showError(`全文解析失败：${failureMessage(data.error, '请更换文件后重试。')}`);
    }
    return data.parse_status === 'ready' || data.parse_status === 'failed';
  }

  async pollManuscript(manuscriptId, fileName, signal, token) {
    let statusFailures = 0;
    for (let attempt = 0; attempt < 240; attempt += 1) {
      if (attempt) await sleep(900);
      if (token !== this.uploadToken) return;
      let data;
      try {
        data = await this.getManuscript(manuscriptId, { signal });
        statusFailures = 0;
      } catch (error) {
        if (signal.aborted || error?.status === 404 || error?.status === 410 || ++statusFailures >= 3) throw error;
        continue;
      }
      if (token !== this.uploadToken) return;
      if (data && this.applyManuscriptStatus(data, fileName)) return;
    }
    throw new Error('全文尚未完成解析，可以重查，不需要重新上传。');
  }

  setUploadRetry(action, label = '') {
    this.retryUploadAction = action;
    setText(this.uploadRetry, label);
    setHidden(this.uploadRetry, !action || this.mode !== 'full_text');
  }

  clearFile({ focus = false } = {}) {
    this.abortUpload();
    this.manuscript = null;
    this.selectedFile = null;
    this.fileInput.value = '';
    this.setUploadRetry(null);
    this.updateUploadStatus();
    this.clearError();
    if (focus) this.uploadZone.focus();
  }

  abortUpload() {
    this.uploadToken += 1;
    if (this.uploadController) {
      try {
        this.uploadController.abort();
      } catch {
        // 已结束的上传无需处理。
      }
      this.uploadController = null;
    }
  }

  updateUploadStatus() {
    const file = this.selectedFile;
    const manuscript = this.manuscript;
    const label = byId('upload-label');
    const meta = byId('file-meta');
    const status = this.manuscriptStatus;
    if (!file && !manuscript) {
      label.textContent = this.scanFailure ? '重新检查上传状态' : '上传全文 · 选择 PDF 或 DOCX';
      meta.textContent = this.uploadEnabled ? `单文件不超过 ${uploadLimitLabel(this.maxUploadBytes)}` : '';
      status.textContent = '';
      status.hidden = true;
      setHidden(this.clearFileButton, true);
      this.updateButtons();
      return;
    }

    label.textContent = file?.name || manuscript?.file_name || '已选择文件';
    const parseStatus = manuscript?.parse_status || 'unknown';
    if (parseStatus === 'uploading') {
      meta.textContent = '正在上传…';
    } else if (parseStatus === 'ready') {
      const pages = pageCount(manuscript);
      const passages = Number.isFinite(manuscript?.passage_count) ? manuscript.passage_count : null;
      const parts = [];
      if (pages != null) parts.push(`${pages} 页`);
      if (passages != null) parts.push(`${passages} 段`);
      parts.push(PARSE_LABELS.ready);
      meta.textContent = parts.join(' · ');
    } else if (parseStatus === 'upload_failed') {
      meta.textContent = '上传失败';
    } else if (parseStatus === 'status_unknown') {
      meta.textContent = '解析状态未确认';
    } else if (parseStatus === 'failed') {
      meta.textContent = PARSE_LABELS.failed;
    } else if (PARSE_LABELS[parseStatus]) {
      meta.textContent = PARSE_LABELS[parseStatus];
    } else {
      meta.textContent = `解析状态：${parseStatus}`;
    }

    let statusText = '';
    let level = 'info';
    if (parseStatus === 'ready') {
      const warnings = Array.isArray(manuscript?.warnings) ? manuscript.warnings : [];
      if (warnings.length) {
        statusText = `解析提示：${warnings.map((warning) => typeof warning === 'string' ? warning : warning?.message || warning?.code || '解析内容需核查').join('；')}`;
        level = 'warn';
      }
    } else if (parseStatus === 'parsing') {
      statusText = '正在解析；完成前不会开始检索。';
    }
    setText(status, statusText);
    status.dataset.level = level;
    status.hidden = !statusText;
    setHidden(this.clearFileButton, !file);
    this.updateButtons();
  }

  resizeInput() {
    if (this.mode === 'keywords') {
      this.input.style.height = '46px';
      return;
    }
    this.input.style.height = 'auto';
    this.input.style.height = `${Math.min(this.input.scrollHeight, 180)}px`;
  }

  submit() {
    if (this.busy) return;
    if (this.sourcesBusy || this.sourcesBlocked) return;
    if (!this.ready) {
      this.showError('未配置模型或数据源，无法开始检索。');
      return;
    }
    try {
      const payload = buildSearchPayload({
        mode: this.mode,
        value: this.input.value,
        libraries: this.libraries,
        selectedLibraryIds: this.selectedLibraryIds,
        demoScopeEnabled: this.libraries.every((library) => library.simulation === true),
        manuscript: this.manuscript,
      });
      const displayText = this.mode === 'full_text'
        ? `全文：${this.manuscript?.file_name || '已解析文件'}`
        : String(this.input.value || '').trim();
      this.clearError();
      void this.onSearch(payload, { displayText });
    } catch (error) {
      if (!(error instanceof InputError)) throw error;
      this.showError(error.message);
      if (error.code === 'EMPTY_QUERY') this.input.focus();
      if (error.code === 'MANUSCRIPT_NOT_READY') this.updateUploadStatus();
    }
  }

  showError(message) {
    this.formError.textContent = message;
    setHidden(this.formError, false);
  }

  clearError() {
    this.formError.textContent = '';
    setHidden(this.formError, true);
  }

  setBusy(busy) {
    this.busy = busy;
    this.form.setAttribute('aria-busy', String(busy));
    this.updateButtons();
  }

  updateButtons() {
    const fullText = this.mode === 'full_text';
    const manuscriptReady = !fullText || this.manuscript?.parse_status === 'ready';
    this.startButton.disabled = !this.ready || this.busy || !manuscriptReady
      || this.sourcesBusy || this.sourcesBlocked || (fullText && !this.uploadEnabled);
    this.startButton.title = !this.uploadEnabled && fullText
      ? '当前服务未开放全文上传'
      : fullText && !manuscriptReady ? '请先上传并等待全文解析完成' : '开始检索';
  }

  show() {
    this.setBusy(false);
    if (this.mode === 'full_text') byId('upload-zone').focus();
    else this.input.focus();
  }
}

function pageCount(manuscript) {
  if (Number.isFinite(manuscript?.page_count)) return manuscript.page_count > 0 ? manuscript.page_count : null;
  if (Array.isArray(manuscript?.pages)) return manuscript.pages.length;
  if (Number.isFinite(manuscript?.pages)) return manuscript.pages;
  return null;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
