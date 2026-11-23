import {
  ApiError,
  cancelAgentRun,
  createAgentRun,
  createCitationText,
  createConversationTurn,
  getAgentRun,
  getCitationFormats,
  getCitationText,
  getPreparedCitation,
  getConversation,
  getLibraries,
  getManuscript,
  getSearch,
  getUploadCapability,
  getWorkspaceIdentity,
  handleWorkspaceError,
  login,
  logout,
  newIdempotencyKey,
  setWorkspaceAccount,
  uploadManuscript,
} from './api.js';
import { SearchEventStream } from './sse.js';
import { ResultsView } from './results-view.js';
import { HomeView } from './home-view.js';
import { ChatView } from './chat-view.js';
import { CitationDialog } from './citation-view.js';
import { BibliographyMenu } from './bibliography-menu.js';
import { citationRequestBody } from './citation-controller.js';
import { announce, renderError, renderStatus } from './render.js';
import { REVOKE_REASON, SEARCH_STATE, SNAPSHOT_KIND, label } from './labels.js';
import { hydrateIcons } from './icons.js';
import { renderResultDetail } from './detail-view.js';
import { formatCitationInfo, resultTitle } from './citation.js';
import { copyText } from './clipboard.js';
import { AgentSession, AGENT_TERMINAL } from './agent-session.js';
import { renderAttemptTabs } from './agent-run-view.js';
import { ModelSettingsDialog } from './model-settings.js';
import { failureMessage } from './chat-model.js';
import {
  clearConversationId,
  readConversationId,
  writeConversationId,
} from './conversation-storage.js';
import { WorkspaceAccount } from './workspace-account.js';
import { WorkspaceHistory } from './workspace-history.js';
import { WorkspaceProjects, OTHER_PROJECT } from './workspace-projects.js';
import { SourceRequests } from './source-requests.js';

const ids = (names) => Object.fromEntries(names.map((name) => [
  name.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()),
  document.getElementById(name),
]));
const refs = {
  ...ids([
    'search-home', 'search-workspace',
    'fatal-error', 'metric-results',
    'announcements', 'notice-list', 'worker-list', 'workers-empty',
    'worker-progress', 'worker-progress-count', 'source-progress',
    'results', 'results-note', 'new-search-dialog',
    'result-dialog', 'result-dialog-body', 'result-dialog-title', 'close-result-dialog',
    'citation-dialog', 'citation-dialog-body', 'citation-dialog-title', 'close-citation-dialog',
    'action-toast', 'attempt-tabs', 'main-content', 'login-view', 'login-form',
    'access-token', 'login-button', 'login-error', 'logout-button', 'metric-results-label',
    'model-settings-button', 'model-dialog', 'results-status', 'results-pane',
    'pane-switch', 'pane-chat', 'pane-results', 'chat-pane', 'chat-log', 'chat-form',
    'chat-input', 'chat-send', 'chat-note', 'chat-error', 'cancel-run',
  ]),
};

hydrateIcons();

const session = new AgentSession();
let store = session.store;
const view = new ResultsView({ root: refs.results, note: refs.resultsNote });
const chat = new ChatView({ root: refs.chatLog });

let libraries = [];
let defaultModelAvailable = false;
let agentAvailable = false;
let authenticated = false;
let authRequired = false;
let accountsEnabled = false;
let projectsEnabled = false;
let identityReady = false;
let account = null;
let identityRequest = 0;
let identityChannel = null;
let citationPreparationEnabled = false;
let createController = null;
let sendController = null;
let sendBusy = false;
let cancelBusy = false;
let sessionGeneration = 0;
let conversationPollBusy = false;
let conversationPollAt = 0;
let conversationPollUntil = 0;
let runPollBusy = false;
let chatComposing = false;
const streams = new Map();
const attempts = new Map();
let announcementTimer = null;
let toastTimer = null;
let detailTrigger = null;
let renderFrame = null;

const home = new HomeView({
  onSearch: startSearch,
  onSources: (ids) => void projects.useSources(ids),
  uploadManuscript,
  getManuscript,
  getUploadCapability,
  createUploadKey: newIdempotencyKey,
});
const bibliography = new BibliographyMenu({
  load: getCitationFormats,
  copy: copyText,
  onToast: showToast,
});
const modelSettings = new ModelSettingsDialog({ onChange: () => updateAgentAvailability() });
const accountView = new WorkspaceAccount({
  onChange: (next, changedSession) => {
    identityRequest += 1;
    adoptIdentity(next);
    if (changedSession) identityChannel?.postMessage('identity-changed');
  },
  onModel: () => modelSettings.open(),
  onNotice: showToast,
});
const historyView = new WorkspaceHistory({
  onNew: () => {
    if (session.active || sendBusy) openConfirmation(refs.newSearchDialog);
    else returnHome();
  },
  onOpen: openHistory,
  onLogin: () => accountView.open('login'),
  onDeleted: (id) => { if (id === session.conversationId) returnHome(); },
  onNotice: showToast,
  activeId: () => session.conversationId,
});
const projects = new WorkspaceProjects({
  onDraft: (project, busy, blocked, preserve) => {
    home.setSourceBusy(busy, blocked);
    if (busy) return;
    if (historyView.view === 'home') historyView.setProject(project?.project_id || (projects.enabled ? projects.target : null));
    const choices = project?.libraries || libraries;
    const defaults = project?.library_ids || libraries.map((library) => library.library_id);
    const available = new Set(choices.map((library) => library.library_id));
    const selected = preserve && !project?.share_sources
      ? [...home.selectedLibraryIds].filter((id) => available.has(id)) : defaults;
    home.setSourceChoices(choices, selected.length ? selected : defaults);
    if (project?.archived) home.showError('此项目已归档。可查看历史，恢复后才能新建会话。');
    else if (blocked) home.showError('暂时无法读取项目。请重新选择，或在设置中重试。');
    else home.clearError();
  },
  onScope: (learning) => {
    sourceRequests.setIdentity(account?.id, learning);
    syncChatControls();
  },
  onNotice: showToast,
});
const sourceRequests = new SourceRequests({
  onOpenHistory: openHistory,
  onReady: (item) => {
    if (item.project_id === projects.target && !projects.pending) void projects.select(projects.target, { preserve: true });
  },
});
const citation = new CitationDialog({
  refs: {
    dialog: refs.citationDialog,
    body: refs.citationDialogBody,
    title: refs.citationDialogTitle,
    close: refs.closeCitationDialog,
  },
  request: requestCitation,
  poll: getCitationText,
  lookup: (target) => citationPreparationEnabled ? getPreparedCitation(target) : { preparing: false },
  copy: copyText,
  openFormats: (trigger, target, fallback) => bibliography.open(trigger, target, { fallback }),
  onToast: showToast,
});

bindActions();
renderAll();
void initialize();
window.setInterval(tick, 1000);

function bindActions() {
  refs.loginForm.addEventListener('submit', (event) => {
    event.preventDefault();
    void authenticate();
  });
  refs.logoutButton.addEventListener('click', () => void endSession());
  bindDialogButtons(refs.newSearchDialog);
  refs.newSearchDialog.addEventListener('close', () => {
    if (refs.newSearchDialog.returnValue === 'confirm') void cancelAndReturnHome();
  });
  refs.closeResultDialog.addEventListener('click', () => refs.resultDialog.close());
  refs.resultDialog.addEventListener('close', () => {
    if (window.location.hash.startsWith('#result=')) {
      window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}`);
    }
    refs.resultDialogBody.replaceChildren();
    refs.resultDialogTitle.textContent = '证据详情';
    if (detailTrigger?.isConnected) detailTrigger.focus({ preventScroll: true });
    detailTrigger = null;
  });
  refs.attemptTabs.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-query-id]');
    if (button) selectQuery(button.dataset.queryId);
  });
  refs.results.addEventListener('click', handleResultClick);
  refs.resultDialogBody.addEventListener('click', handleResultClick);
  refs.paneSwitch.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-pane]');
    if (button) setPane(button.dataset.pane);
  });
  refs.cancelRun.addEventListener('click', () => void cancelCurrentRun());
  refs.chatForm.addEventListener('submit', (event) => {
    event.preventDefault();
    submitChat();
  });
  refs.chatInput.addEventListener('input', () => {
    resizeChatInput();
  });
  refs.chatInput.addEventListener('compositionstart', () => { chatComposing = true; });
  refs.chatInput.addEventListener('compositionend', () => { chatComposing = false; });
  refs.chatInput.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' || event.shiftKey) return;
    if (event.isComposing || chatComposing || event.keyCode === 229) return;
    event.preventDefault();
    refs.chatForm.requestSubmit();
  });
  refs.chatLog.addEventListener('click', (event) => {
    const pill = event.target.closest('button[data-query-id]');
    if (pill) {
      selectQuery(pill.dataset.queryId);
      return;
    }
    const retry = event.target.closest('button[data-action="retry-message"]');
    if (retry) void retryPending(retry.dataset.pendingId);
  });
  window.addEventListener('hashchange', syncResultRoute);
  window.addEventListener('focus', () => {
    void (async () => {
      if (accountsEnabled) await refreshIdentity();
      if (authenticated && session.conversationId && !conversationPollBusy) await refreshConversation();
    })();
  });
  window.addEventListener('beforeunload', () => {
    for (const stream of streams.values()) stream.stop();
    sendController?.abort();
  });
}

function bindDialogButtons(dialog) {
  dialog.addEventListener('click', (event) => {
    const button = event.target.closest('button[value]');
    if (!button) return;
    event.preventDefault();
    dialog.close(button.value);
  });
}

function openConfirmation(dialog) {
  dialog.returnValue = '';
  dialog.showModal();
}

async function initialize() {
  const loaded = await loadLibraries();
  if (!loaded) {
    if (!refs.loginView.hidden) return;
    home.show();
    return;
  }
  refs.loginView.hidden = true;
  refs.mainContent.hidden = false;
  refs.logoutButton.hidden = !authRequired;
  if (accountsEnabled) {
    setWorkspaceAccount(null, () => void refreshIdentity({ stale: true }));
    if (typeof BroadcastChannel === 'function') {
      identityChannel = new BroadcastChannel(`citation-workspace:${location.pathname}`);
      identityChannel.addEventListener('message', () => void refreshIdentity());
    }
    await refreshIdentity();
  } else {
    identityReady = true;
    adoptIdentity(null);
  }
  const restored = identityReady && await restoreConversation();
  if (!restored) home.show();
}

function adoptIdentity(next) {
  const previousId = account?.id || 'guest';
  const nextId = next?.id || 'guest';
  const changed = previousId !== nextId && identityReady;
  if (changed) {
    if (account) clearConversationId(storage(), account.id);
    accountView.toggleMenu(false);
    home.clearFile();
    refs.searchHome.querySelector('#keywords').value = '';
    home.setBusy(false);
    resetSession({ forgetConversation: false });
  }
  account = next;
  setWorkspaceAccount(accountsEnabled ? nextId : null);
  if (changed || modelSettings.scope !== nextId) modelSettings.setScope(nextId, { forget: changed });
  accountView.setIdentity(next, { enabled: accountsEnabled, models: modelSettings.available });
  if (changed) sourceRequests.setIdentity(null, false);
  historyView.setIdentity(accountsEnabled, next);
  projects.setIdentity(projectsEnabled ? next?.id ?? null : null);
  identityReady = true;
  updateAgentAvailability();
  if (changed) revealHome();
}

async function refreshIdentity({ stale = false } = {}) {
  const ticket = ++identityRequest;
  if (stale) {
    accountView.reset();
    sourceRequests.setIdentity(null, false);
    projects.setIdentity(null);
    historyView.setIdentity(false, null);
    resetSession({ forgetConversation: false });
    home.clearFile();
    refs.searchHome.querySelector('#keywords').value = '';
    revealHome();
  }
  try {
    const result = await getWorkspaceIdentity();
    if (ticket !== identityRequest) return;
    adoptIdentity(result.account);
    if (stale) showToast('工作区身份已更新。');
  } catch (error) {
    if (ticket !== identityRequest || error.name === 'AbortError') return;
    if (!identityReady) {
      // Existing anonymous workspace is usable when account discovery is unavailable.
      identityReady = true;
      adoptIdentity(null);
    } else if (stale) {
      modelSettings.clear();
      historyView.setIdentity(false, null);
      accountView.setIdentity(null, { enabled: false, models: false });
      identityReady = false;
      updateAgentAvailability();
    }
    showToast(`暂时无法核对账户：${errorMessage(error)}`, { level: 'error' });
  }
}

async function authenticate() {
  refs.loginButton.disabled = true;
  refs.loginError.hidden = true;
  try {
    await login(refs.accessToken.value);
    authRequired = true;
    resetSession({ forgetConversation: true });
    const loaded = await loadLibraries();
    if (loaded) {
      refs.loginView.hidden = true;
      refs.mainContent.hidden = false;
      refs.logoutButton.hidden = false;
      revealHome();
    }
  } catch (error) {
    refs.loginError.textContent = error.status === 401
      ? '访问令牌无效或会话已过期。' : `登录失败：${errorMessage(error)}`;
    refs.loginError.hidden = false;
  } finally {
    refs.accessToken.value = '';
    refs.loginButton.disabled = false;
  }
}

async function endSession() {
  modelSettings.clear();
  try {
    await logout();
    showLogin();
  } catch (error) {
    showToast(`退出失败：${errorMessage(error)}`, { level: 'error' });
  }
}

function showLogin() {
  authenticated = false;
  authRequired = true;
  modelSettings.clear();
  resetSession({ forgetConversation: true });
  refs.mainContent.hidden = true;
  refs.loginView.hidden = false;
  refs.logoutButton.hidden = true;
  refs.accessToken.value = '';
  refs.accessToken.focus();
}

async function loadLibraries() {
  try {
    const response = await getLibraries();
    libraries = Array.isArray(response) ? response : response.libraries || [];
    if (!libraries.length) throw new Error('服务端没有配置可用文献库。');
    defaultModelAvailable = response.agent_available === true;
    citationPreparationEnabled = response.citation_preparation_enabled === true;
    accountsEnabled = response.accounts_enabled === true;
    projectsEnabled = response.projects_enabled === true;
    authRequired = authRequired || response.authentication_required === true;
    modelSettings.setProviders(response.user_model_providers || [], response.user_model_protocols || [], response.model);
    home.setLibraries(libraries, {
      agentAvailable: defaultModelAvailable || modelSettings.isCustom,
      uploadCapability: response,
      ocrEnabled: response.ocr_enabled,
    });
    authenticated = true;
    updateAgentAvailability();
    return true;
  } catch (error) {
    authenticated = false;
    if (error.status === 401) {
      showLogin();
      return false;
    }
    refs.mainContent.hidden = false;
    home.setLoadingError(`无法读取数据源：${errorMessage(error)}`);
    agentAvailable = false;
    syncChatControls();
    return false;
  }
}

function updateAgentAvailability() {
  agentAvailable = Boolean(authenticated && (identityReady || !accountsEnabled) && libraries.length
    && (defaultModelAvailable || modelSettings.isCustom));
  home.setAgentAvailable(agentAvailable);
  syncChatControls();
}

function storage() {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

function rememberConversation(conversationId) {
  if (typeof conversationId !== 'string' || !conversationId) return;
  const cache = storage();
  if (readConversationId(cache, account?.id) === conversationId) return;
  writeConversationId(cache, conversationId, account?.id);
}

function forgetConversation() {
  clearConversationId(storage(), account?.id);
}

function restoreConversation() {
  return openHistory(readConversationId(storage(), account?.id), { restore: true });
}

let historyLoad = 0;
async function openHistory(conversationId, { restore = false } = {}) {
  if (!conversationId) return false;
  const generation = sessionGeneration;
  const ticket = ++historyLoad;
  try {
    const data = await getConversation(conversationId);
    if (generation !== sessionGeneration || ticket !== historyLoad) return false;
    if (data?.conversation_id !== conversationId || (!data.messages?.length && !data.runs?.length)) {
      if (restore) forgetConversation();
      return false;
    }
    if (!restore) resetSession({ forgetConversation: false });
    const current = sessionGeneration;
    const result = session.applyConversation(data, [...libraries, ...home.libraries], { focus: true });
    store = session.store;
    syncConversationProject();
    rememberConversation(conversationId);
    showWorkspace({ pane: 'chat' });
    renderAll();
    if (!restore) refs.chatInput.focus({ preventScroll: true });
    await Promise.all(result.addedIds.map((queryId) => recoverState(queryId)));
    if (current !== sessionGeneration) return false;
    conversationPollAt = 0;
    conversationPollUntil = Date.now() + (session.active ? 0 : 2600);
    if (!session.active) void refreshConversation();
    return true;
  } catch (error) {
    if (generation !== sessionGeneration || ticket !== historyLoad || error.name === 'AbortError') return false;
    if (restore && error.status === 404) forgetConversation();
    if (handleIdentityError(error)) return false;
    showToast(`无法打开对话：${errorMessage(error)}`, { level: 'error' });
    return false;
  }
}

function resetSession({ forgetConversation: shouldForget = true } = {}) {
  sessionGeneration += 1;
  for (const stream of streams.values()) stream.stop();
  streams.clear();
  createController?.abort();
  createController = null;
  sendController?.abort();
  sendController = null;
  sendBusy = false;
  cancelBusy = false;
  if (announcementTimer) clearTimeout(announcementTimer);
  announcementTimer = null;
  if (refs.resultDialog.open) refs.resultDialog.close();
  citation.close();
  bibliography.close();
  window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}`);
  session.reset();
  store = session.store;
  view.reset();
  chat.reset();
  attempts.clear();
  conversationPollBusy = false;
  conversationPollAt = 0;
  conversationPollUntil = 0;
  setChatError('');
  refs.chatInput.value = '';
  resizeChatInput();
  renderError(refs.fatalError, {});
  refs.attemptTabs.replaceChildren();
  refs.attemptTabs.hidden = true;
  setPane('chat');
  if (shouldForget) forgetConversation();
  renderAll();
}

function syncConversationProject() {
  const id = projects.enabled ? session.projectId || OTHER_PROJECT : null;
  projects.setChat(id);
  if (historyView.view === 'results') historyView.setProject(id);
}

function showWorkspace({ pane = 'chat' } = {}) {
  refs.searchHome.hidden = true;
  refs.searchWorkspace.hidden = false;
  document.body.dataset.view = 'results';
  historyView.setView('results');
  syncConversationProject();
  sourceRequests.setView('results');
  setPane(pane);
  window.scrollTo(0, 0);
}

function revealHome() {
  refs.searchWorkspace.hidden = true;
  refs.searchHome.hidden = false;
  document.body.dataset.view = 'home';
  historyView.setView('home');
  historyView.setProject(projects.enabled ? projects.target : null);
  sourceRequests.setView('home');
  home.clearError();
  home.show();
  window.scrollTo(0, 0);
}

function returnHome() {
  resetSession({ forgetConversation: true });
  revealHome();
  if (projects.enabled) void projects.select(OTHER_PROJECT);
}

function handleIdentityError(error) {
  if (['ACCOUNT_SESSION_EXPIRED', 'WORKSPACE_CHANGED'].includes(error?.code)) return true;
  if (error?.status === 401 && !accountsEnabled) {
    showLogin();
    return true;
  }
  return false;
}

async function startSearch(payload, context = {}) {
  if (projects.enabled && (projects.pending || !projects.draft || projects.draft.archived)) return;
  const project = projects.draft;
  resetSession({ forgetConversation: true });
  session.projectId = project?.project_id === OTHER_PROJECT ? null : project?.project_id || null;
  session.libraryIds = Array.isArray(payload.library_ids) ? [...payload.library_ids] : [];
  if (session.projectId) payload = { ...payload, project_id: session.projectId };
  if (project?.share_sources) delete payload.library_ids;
  const displayText = typeof context.displayText === 'string' && context.displayText
    ? context.displayText : fallbackDisplayText(payload);
  showWorkspace({ pane: 'chat' });
  home.setBusy(true);
  const pending = session.addPendingMessage(displayText, { kind: 'first' });
  const attempt = {
    kind: 'first',
    payload,
    idempotencyKey: pending.key,
    pendingId: pending.id,
    message: displayText,
  };
  attempts.set(pending.id, attempt);
  session.followLatest = true;
  renderAll();
  chat.scrollToBottom();
  void dispatchAttempt(attempt);
}

function fallbackDisplayText(payload) {
  if (Array.isArray(payload?.keywords) && payload.keywords.length) return payload.keywords.join('、');
  if (typeof payload?.abstract === 'string' && payload.abstract) return payload.abstract.slice(0, 240);
  if (payload?.input_type === 'full_text') return '全文检索';
  return '开始检索';
}

function webRunPayload(body) {
  const model = modelSettings.payload();
  return {
    ...body,
    ...(model ? { model } : {}),
    ...(citationPreparationEnabled ? { prepare_citations: true } : {}),
  };
}

async function dispatchAttempt(attempt) {
  const generation = sessionGeneration;
  const pending = session.findPending(attempt.pendingId);
  if (pending) {
    pending.status = 'sending';
    pending.failure = '';
  }
  sendBusy = true;
  syncChatControls();
  renderAll();
  const controller = new AbortController();
  sendController = controller;
  attempt.controller = controller;
  try {
    let run;
    if (attempt.kind === 'first') {
      const payload = webRunPayload(attempt.payload);
      createController = controller;
      run = await createAgentRun(payload, {
        signal: controller.signal,
        idempotencyKey: attempt.idempotencyKey,
      });
      if (createController === controller) createController = null;
    } else {
      const body = webRunPayload(attempt.body);
      run = await createConversationTurn(attempt.conversationId, body, {
        signal: controller.signal,
        idempotencyKey: attempt.idempotencyKey,
      });
    }
    if (generation !== sessionGeneration || controller.signal.aborted) return;
    if (!run || typeof run.run_id !== 'string') throw new Error('服务端未返回 AgentRun。');
    if (run.conversation_id) rememberConversation(run.conversation_id);
    session.markPendingSent(attempt.pendingId, run.run_id);
    attempts.delete(attempt.pendingId);
    sendBusy = false;
    const recoverIds = syncRun(run, { focus: true });
    setChatError('');
    renderAll();
    chat.scrollToBottom();
    conversationPollAt = 0;
    if (!session.conversationId) {
      setChatError('服务端未返回 conversation_id，后续追问暂不可用；当前结果仍可查看。');
    }
    await Promise.all(recoverIds.map((queryId) => recoverState(queryId)));
    if (generation === sessionGeneration) await refreshConversation();
  } catch (error) {
    if (error?.name === 'AbortError') return;
    if (generation !== sessionGeneration) return;
    if (handleIdentityError(error)) return;
    session.markPendingFailed(attempt.pendingId, failureMessage(error, '消息发送失败。'));
    if (!refs.chatInput.value.trim()) {
      refs.chatInput.value = attempt.message;
      resizeChatInput();
    }
    renderAll();
    chat.scrollToBottom();
  } finally {
    if (sendController === controller) sendController = null;
    if (generation === sessionGeneration) {
      sendBusy = false;
      home.setBusy(false);
      syncChatControls();
    }
  }
}

function submitChat() {
  if (projects.chatArchived) {
    setChatError('此项目已归档。可从对话标题旁的项目设置恢复。');
    return;
  }
  if (sendBusy || session.active) {
    showToast('本轮仍在处理中；请稍候或先停止本轮。', { level: 'error' });
    return;
  }
  if (!agentAvailable) {
    setChatError('未配置模型，无法继续发送；可在模型设置中接入自己的模型。');
    return;
  }
  if (!session.conversationId) {
    setChatError('当前对话尚未建立；请返回首页开始新检索。');
    return;
  }
  const message = refs.chatInput.value.trim();
  if (!message) return;
  const failed = session.findFailedPending(message);
  if (failed) {
    void retryPending(failed.id);
    return;
  }
  const pending = session.addPendingMessage(message, { kind: 'turn' });
  const body = { message };
  if (store.queryId) body.active_query_id = store.queryId;
  if (!projects.enabled && Array.isArray(session.libraryIds) && session.libraryIds.length) {
    body.library_ids = [...session.libraryIds];
  }
  const attempt = {
    kind: 'turn',
    conversationId: session.conversationId,
    body,
    idempotencyKey: pending.key,
    pendingId: pending.id,
    message,
  };
  attempts.set(pending.id, attempt);
  session.followLatest = true;
  setChatError('');
  refs.chatInput.value = '';
  resizeChatInput();
  setPane('chat');
  renderAll();
  chat.scrollToBottom();
  void dispatchAttempt(attempt);
}

function retryPending(pendingId) {
  if (sendBusy || session.active) {
    showToast('本轮仍在处理中；请稍候或先停止本轮。', { level: 'error' });
    return;
  }
  const attempt = attempts.get(pendingId);
  const pending = session.findPending(pendingId);
  if (!attempt || !pending) {
    showToast('这条消息无法重试；请重新输入。', { level: 'error' });
    return;
  }
  if (refs.chatInput.value.trim() === attempt.message) {
    refs.chatInput.value = '';
    resizeChatInput();
  }
  setPane('chat');
  void dispatchAttempt(attempt);
}

async function cancelCurrentRun() {
  if (cancelBusy || !session.active) return;
  const generation = sessionGeneration;
  cancelBusy = true;
  syncChatControls();
  try {
    const runs = session.activeRuns.slice();
    for (const run of runs) {
      const updated = await cancelAgentRun(run.run_id);
      if (generation !== sessionGeneration) return;
      if (updated?.run_id) syncRun(updated, { focus: false });
    }
    conversationPollAt = 0;
    conversationPollUntil = Date.now() + 8000;
    renderAll();
    showToast('已请求取消本轮。');
    await refreshConversation();
  } catch (error) {
    if (generation !== sessionGeneration || handleIdentityError(error)) return;
    setChatError(`取消失败：${failureMessage(error)}`);
  } finally {
    if (generation === sessionGeneration) {
      cancelBusy = false;
      syncChatControls();
      renderAll();
    }
  }
}

async function cancelAndReturnHome() {
  const generation = sessionGeneration;
  try {
    for (const run of session.activeRuns.slice()) {
      await cancelAgentRun(run.run_id);
      if (generation !== sessionGeneration) return;
    }
    returnHome();
  } catch (error) {
    if (generation !== sessionGeneration || handleIdentityError(error)) return;
    showError(refs.fatalError, '取消失败，仍停留在当前结果页', error);
  }
}

function syncRun(run, { focus = session.followLatest } = {}) {
  if (!run || typeof run.run_id !== 'string') return [];
  if (run.conversation_id && session.conversationId
    && run.conversation_id !== session.conversationId) return [];
  const previousStatus = session.runs.get(run.run_id)?.status;
  const result = session.applyRun(run, libraries, { focus });
  store = session.store;
  if (run.conversation_id) rememberConversation(run.conversation_id);
  if (result.activeChanged) {
    if (refs.resultDialog.open) refs.resultDialog.close();
    citation.close();
    view.reset();
  }
  const recover = new Set(result.addedIds);
  if (result.activeChanged && store.queryId) recover.add(store.queryId);
  if (previousStatus !== run.status && store.queryId
    && session.runForQuery(store.queryId)?.run_id === run.run_id) {
    recover.add(store.queryId);
  }
  if (previousStatus !== run.status) {
    conversationPollAt = 0;
    if (AGENT_TERMINAL.has(run.status)) conversationPollUntil = Date.now() + 8000;
  }
  return [...recover].filter(Boolean);
}

function selectQuery(queryId) {
  if (!session.select(queryId)) return;
  store = session.store;
  if (refs.resultDialog.open) refs.resultDialog.close();
  citation.close();
  bibliography.close();
  view.reset();
  setPane('results');
  renderAll();
  void recoverState(queryId);
}

function setPane(pane) {
  if (pane === 'results' && !session.hasQueries) return;
  const next = pane === 'results' ? 'results' : 'chat';
  refs.searchWorkspace.dataset.pane = next;
  refs.paneChat.setAttribute('aria-selected', String(next === 'chat'));
  refs.paneResults.setAttribute('aria-selected', String(next === 'results'));
}

function resizeChatInput() {
  refs.chatInput.style.height = 'auto';
  refs.chatInput.style.height = `${Math.min(refs.chatInput.scrollHeight || 36, 120)}px`;
}

function setChatError(message) {
  const text = message || '';
  refs.chatError.textContent = text;
  refs.chatError.hidden = !text;
}

function syncChatControls() {
  const canSend = authenticated && session.hasConversation && !session.active
    && !sendBusy && agentAvailable && !projects.chatArchived;
  refs.chatSend.disabled = !canSend;
  refs.cancelRun.hidden = !session.active;
  refs.cancelRun.disabled = cancelBusy;
  refs.paneResults.disabled = !session.hasQueries;
  refs.paneResults.title = session.hasQueries ? '' : '本轮没有可查看的榜单';
  if (!session.hasQueries && refs.searchWorkspace.dataset.pane === 'results') setPane('chat');
  let note = '';
  if (!agentAvailable) note = '未配置模型，无法继续发送；可在模型设置中接入。';
  else if (!session.hasConversation && !sendBusy && !session.active) note = '当前对话尚未建立；请返回首页开始新检索。';
  refs.chatNote.hidden = !note;
  refs.chatNote.textContent = note;
}

function connectStream(queryId) {
  const target = session.forQuery(queryId);
  if (!target || target.terminal) return;
  streams.get(queryId)?.stop();
  target.connection = 'connecting';
  scheduleRender();
  const stream = new SearchEventStream({
    queryId,
    getAfterSequence: () => target.lastSequence,
    onEvent: (envelope) => handleEvent(queryId, envelope),
    onConnection: (state, info) => handleConnection(queryId, state, info),
    onGap: () => recoverState(queryId),
    onExhausted: () => handleStreamFailure(queryId),
    isTerminal: () => target.frozen,
  });
  streams.set(queryId, stream);
  stream.start();
}

function handleEvent(queryId, envelope) {
  const target = session.forQuery(queryId);
  if (!target) return;
  if (target.detectGap(envelope.sequence)) {
    streams.get(queryId)?.stop();
    void recoverState(queryId);
    return;
  }
  if (!target.applyEvent(envelope).changed) return;
  if (target !== store) return;
  const text = announcementFor(envelope);
  if (text) announcementTimer = announce(refs.announcements, text);
  scheduleRender();
}

function handleConnection(queryId, state, info = {}) {
  const target = session.forQuery(queryId);
  if (!target) return;
  if (state === 'protocol-error') {
    target.addNotice('无法解析的事件已跳过；请重新读取状态。', { level: 'warn', key: 'protocol' });
  } else if (state === 'error' && (handleWorkspaceError(info.error) || handleIdentityError(info.error))) {
    return;
  } else {
    target.connection = state;
    target.connectionDetail = state === 'reconnecting' ? `${info.attempt || 1}/${info.max || 3}` : '';
  }
  if (target === store) scheduleRender();
}

function tick() {
  if (!authenticated) return;
  if (document.visibilityState !== 'hidden') void sourceRequests.poll();
  if (store.queryId && !store.terminal) renderStatus(store, refs, session.run);
  const now = Date.now();
  if (session.active) {
    if (!runPollBusy) void refreshActiveRuns();
    if (!conversationPollBusy && now >= conversationPollAt) void refreshConversation();
  } else if (session.conversationId && now < conversationPollUntil
    && !conversationPollBusy && now >= conversationPollAt) {
    void refreshConversation();
  }
}

async function refreshActiveRuns() {
  if (!authenticated || runPollBusy || !session.active) return;
  runPollBusy = true;
  const generation = sessionGeneration;
  const activeRuns = session.activeRuns.slice();
  const recoverIds = new Set();
  let changed = false;
  try {
    for (const active of activeRuns) {
      try {
        const fresh = await getAgentRun(active.run_id);
        if (generation !== sessionGeneration) return;
        if (!fresh || typeof fresh.run_id !== 'string') continue;
        const previousStatus = session.runs.get(fresh.run_id)?.status;
        const previousUpdated = session.runs.get(fresh.run_id)?.updated_at;
        for (const queryId of syncRun(fresh, { focus: session.followLatest })) recoverIds.add(queryId);
        if (previousStatus !== fresh.status || previousUpdated !== fresh.updated_at) changed = true;
      } catch (error) {
        if (generation !== sessionGeneration || handleIdentityError(error)) return;
        if (generation === sessionGeneration) {
          setChatError(`本轮状态暂未读取成功：${failureMessage(error)}`);
        }
      }
    }
    if (generation !== sessionGeneration) return;
    if (changed || recoverIds.size) {
      renderAll();
      conversationPollAt = 0;
    }
    await Promise.all([...recoverIds].map((queryId) => recoverState(queryId)));
  } finally {
    runPollBusy = false;
  }
}

async function refreshConversation() {
  if (!authenticated || !session.conversationId || conversationPollBusy) return;
  conversationPollBusy = true;
  conversationPollAt = Date.now() + 2500;
  const generation = sessionGeneration;
  const conversationId = session.conversationId;
  try {
    const data = await getConversation(conversationId);
    if (generation !== sessionGeneration) return;
    if (data?.conversation_id && data.conversation_id !== conversationId) return;
    const result = session.applyConversation(data, [...libraries, ...home.libraries], { focus: session.followLatest });
    store = session.store;
    syncConversationProject();
    if (result.activeChanged) {
      if (refs.resultDialog.open) refs.resultDialog.close();
      citation.close();
      view.reset();
    }
    setChatError('');
    renderAll();
    await Promise.all(result.addedIds.map((queryId) => recoverState(queryId)));
  } catch (error) {
    if (generation !== sessionGeneration) return;
    if (handleIdentityError(error)) return;
    if (error.status === 404) {
      session.conversationId = null;
      forgetConversation();
    }
    setChatError(`对话同步暂不可用：${failureMessage(error)}`);
    syncChatControls();
  } finally {
    conversationPollBusy = false;
  }
}

async function recoverState(queryId = store.queryId) {
  const target = session.forQuery(queryId);
  if (!target) return;
  const generation = sessionGeneration;
  try {
    const state = await getSearch(queryId);
    if (generation !== sessionGeneration) return;
    target.applyState(state);
    if (target === store) scheduleRender();
    if (target.terminal) {
      target.connection = 'closed';
    } else {
      connectStream(queryId);
    }
  } catch (error) {
    if (generation !== sessionGeneration || handleIdentityError(error)) return;
    if (target === store) showError(refs.fatalError, '读取状态失败', error, () => void recoverState(queryId));
  }
}

function handleStreamFailure(queryId) {
  const target = session.forQuery(queryId);
  if (!target) return;
  target.connection = 'exhausted';
  target.addNotice('事件流重连失败，已改为读取当前状态。', { level: 'warn', key: 'stream-failure' });
  void recoverState(queryId);
}

function handleResultClick(event) {
  const citationButton = event.target.closest('[data-action="citation-text"]');
  if (citationButton) {
    event.preventDefault();
    event.stopPropagation();
    openCitation(citationButton.dataset.resultId);
    return;
  }
  const copyButton = event.target.closest('[data-action="copy-citation"]');
  if (copyButton) {
    event.preventDefault();
    event.stopPropagation();
    openCopyMenu(copyButton);
    return;
  }
  const openLink = event.target.closest('[data-action="open-result"]');
  if (openLink) {
    detailTrigger = openLink;
    return;
  }
  if (event.target.closest('a,button,summary,details,input,textarea,select')) return;
  if (window.getSelection()?.toString()) return;
  const card = event.target.closest('.result-card[data-result-id]');
  if (!card) return;
  detailTrigger = card;
  window.location.hash = `result=${encodeURIComponent(card.dataset.resultId)}`;
}

function citationTarget(resultId) {
  if (!store.queryId) return null;
  const candidate = store.candidates.get(resultId);
  const rec = candidate || store.results.get(resultId);
  if (!rec || (!candidate && (!rec.citable || rec.revoked))) return null;
  return {
    rec,
    queryId: store.queryId,
    resultId,
    resultVersion: candidate ? rec.pool_version ?? 0 : rec.result_version,
    resultKind: candidate ? 'candidate' : 'recommendation',
    title: resultTitle(rec),
  };
}

function openCitation(resultId) {
  const target = citationTarget(resultId);
  if (!target) {
    showToast('未找到可查看建议的当前条目。', { level: 'error' });
    return;
  }
  bibliography.close();
  citation.open(target);
}

function requestCitation(target) {
  return createCitationText(citationRequestBody({ ...target, model: modelSettings.payload() }));
}

function syncResultRoute() {
  const match = window.location.hash.match(/^#result=(.+)$/);
  if (!match) {
    if (refs.resultDialog.open) refs.resultDialog.close();
    return;
  }
  let resultId;
  try {
    resultId = decodeURIComponent(match[1]);
  } catch {
    showToast('结果链接无法解析。', { level: 'error' });
    return;
  }
  const rec = store.results.get(resultId) || store.lateResults.get(resultId);
  if (!rec) return;
  renderResultDetail(refs, rec);
  if (!refs.resultDialog.open) refs.resultDialog.showModal();
  refs.closeResultDialog.focus({ preventScroll: true });
}

function openCopyMenu(trigger) {
  const target = citationTarget(trigger.dataset.resultId);
  if (!target) {
    showToast('当前条目不能复制。', { level: 'error' });
    return;
  }
  const { rec } = target;
  bibliography.open(trigger, store.simulation === false ? target : null, {
    raw: formatCitationInfo(rec, store.libraryNames),
  });
}

function showToast(message, { level = 'info' } = {}) {
  if (toastTimer) clearTimeout(toastTimer);
  refs.actionToast.textContent = message;
  refs.actionToast.dataset.level = level;
  refs.actionToast.hidden = false;
  toastTimer = window.setTimeout(() => {
    refs.actionToast.hidden = true;
    toastTimer = null;
  }, 4200);
}

function showError(target, title, error, retry = null) {
  const detail = error instanceof ApiError
    ? `${error.code}：${error.message}${error.requestId ? `（request_id ${error.requestId}）` : ''}`
    : error.message || '未知错误，请检查本地服务。';
  renderError(target, {
    title,
    message: detail,
    actions: retry ? [{ label: '重试', primary: true, onClick: retry }] : [],
  });
}

function errorMessage(error) {
  return error instanceof ApiError ? `${error.code}：${error.message}` : error.message || '未知错误';
}

function announcementFor({ event_type, payload = {} }) {
  const worker = payload.worker || {};
  const messages = {
    candidate_pool_updated: '候选列表已更新，等待 LLM 排名',
    candidate_reranked: payload.ranking?.status === 'completed' ? 'LLM 排名已更新' : 'LLM 排名未完成',
    recommendation_created: '收到一条新的推荐结果',
    recommendation_merged: '同一 Claim 的来源已合并',
    recommendation_reranked: '组内排名已更新',
    recommendation_revoked: `推荐已撤销：${label(REVOKE_REASON, payload.reason)}`,
    worker_completed: `${worker.display_name || worker.library_id || 'Worker'} 已完成`,
    worker_failed: `${worker.library_id || 'Worker'} 失败`,
    worker_timed_out: `${worker.library_id || 'Worker'} 超时`,
    worker_cancelled: `${worker.library_id || 'Worker'} 已取消`,
    search_state_changed: `搜索状态：${label(SEARCH_STATE, payload.current_state)}`,
    search_finalized: `${label(SNAPSHOT_KIND, payload.snapshot_kind)}已生成`,
  };
  return messages[event_type] || null;
}

function scheduleRender() {
  if (renderFrame !== null) return;
  renderFrame = window.requestAnimationFrame(renderAll);
}

function renderAll() {
  if (renderFrame !== null) window.cancelAnimationFrame(renderFrame);
  renderFrame = null;
  const activeRun = session.run;
  renderStatus(store, refs, activeRun);
  renderAttemptTabs(session, refs.attemptTabs);
  view.update(store, activeRun);
  chat.update(session);
  refs.metricResultsLabel.textContent = store.simulation === false ? '待核查候选' : '已发布结果';
  syncChatControls();
}
