// 真实 HTTP 客户端。默认同源 /api/v1；可在页面 URL 用 ?api=http://127.0.0.1:8000/api/v1 覆盖调试。
// 本模块不做任何模拟：所有请求都发往配置的 API，失败按 problem+json 暴露给调用方。

import { manuscriptContentType } from './search-input.js';

const DEFAULT_BASE = new URL('./api/v1', window.location.href).pathname;
const SERVICE_BASE = new URL('./', window.location.href).pathname.replace(/\/$/, '');
export const TENANT_ID = 'local-demo';

function resolveBase() {
  const params = new URLSearchParams(window.location.search);
  const fromQuery = params.get('api');
  if (fromQuery && ['127.0.0.1', 'localhost', '::1'].includes(window.location.hostname)) {
    return fromQuery.replace(/\/+$/, '');
  }
  const meta = document.querySelector('meta[name="api-base"]');
  const configured = meta && meta.content ? meta.content : DEFAULT_BASE;
  return configured.replace(/\/+$/, '');
}

export const API_BASE = resolveBase();
let workspaceAccount = null;
let workspaceEpoch = 0;
let onWorkspaceChange = null;

export function setWorkspaceAccount(account, onChange = onWorkspaceChange) {
  if (workspaceAccount !== account) workspaceEpoch += 1;
  workspaceAccount = account;
  onWorkspaceChange = onChange;
}

function workspaceHeaders() {
  return workspaceAccount === null ? {} : { 'X-Workspace-Account': workspaceAccount };
}

function confirmWorkspace(epoch) {
  if (workspaceEpoch !== epoch) throw new DOMException('Workspace changed', 'AbortError');
}

export function handleWorkspaceError(error) {
  if (!['ACCOUNT_SESSION_EXPIRED', 'WORKSPACE_CHANGED'].includes(error?.code)) return false;
  onWorkspaceChange?.();
  return true;
}

export function getLibraries() {
  return request('/libraries');
}

export class ApiError extends Error {
  constructor(status, problem, requestId) {
    super((problem && problem.message) || `请求失败（HTTP ${status || '网络错误'}）`);
    this.name = 'ApiError';
    this.status = status;
    this.code = (problem && problem.code) || (status ? `HTTP_${status}` : 'NETWORK_ERROR');
    this.problem = problem || null;
    this.requestId = requestId || (problem && problem.request_id) || null;
    this.retryable = problem ? Boolean(problem.retryable) : status >= 500;
    this.details = (problem && problem.details) || null;
  }
}

function requestId() {
  const cryptoApi = globalThis.crypto;
  if (cryptoApi && typeof cryptoApi.randomUUID === 'function') return cryptoApi.randomUUID();
  return `local-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function newIdempotencyKey() {
  return requestId();
}

async function readProblem(response) {
  const text = await response.text();
  if (!text) return null;
  try {
    const problem = JSON.parse(text);
    return typeof problem?.code === 'string' && typeof problem.message === 'string' ? problem : null;
  } catch {
    return null;
  }
}

export async function readProblemFromResponse(response) {
  return readProblem(response);
}

export async function request(path, {
  method = 'GET', body, signal, idempotencyKey, headers = {}, discover = false, timeoutMs = 25_000,
} = {}) {
  const epoch = workspaceEpoch;
  const finalHeaders = {
    'X-Tenant-ID': TENANT_ID,
    ...(discover ? {} : workspaceHeaders()),
    'X-Request-ID': requestId(),
    Accept: 'application/json',
    ...headers,
  };
  if (body !== undefined) finalHeaders['Content-Type'] = 'application/json';
  if (idempotencyKey) finalHeaders['Idempotency-Key'] = idempotencyKey;

  const deadline = new AbortController();
  const requestSignal = signal ? AbortSignal.any([signal, deadline.signal]) : deadline.signal;
  const timer = setTimeout(() => deadline.abort(), timeoutMs);
  try {
    const response = await fetch(`${API_BASE}${path}`, {
      method,
      headers: finalHeaders,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: requestSignal,
      cache: 'no-store',
      credentials: 'same-origin',
    });
    confirmWorkspace(epoch);
    if (!response.ok) {
      const problem = await readProblem(response);
      confirmWorkspace(epoch);
      const error = new ApiError(response.status, problem, response.headers.get('X-Request-ID'));
      if (!discover) handleWorkspaceError(error);
      throw error;
    }

    if (response.status === 204) return null;
    const text = await response.text();
    confirmWorkspace(epoch);
    if (!text) return null;
    try {
      return JSON.parse(text);
    } catch {
      throw new ApiError(response.status, {
        code: 'INVALID_JSON',
        message: '服务端返回了无法解析的 JSON。',
        retryable: false,
      });
    }
  } catch (error) {
    if (error instanceof ApiError || signal?.aborted) throw error;
    confirmWorkspace(epoch);
    if (!deadline.signal.aborted && error?.name === 'AbortError') throw error;
    throw new ApiError(0, {
      code: deadline.signal.aborted ? 'NETWORK_TIMEOUT' : 'NETWORK_ERROR',
      message: deadline.signal.aborted
        ? '未及时收到服务响应，请检查网络后重试。'
        : '无法连接文献服务，请检查网络后重试。',
      retryable: true,
    });
  } finally {
    clearTimeout(timer);
  }
}

export async function login(token) {
  const response = await fetch(`${SERVICE_BASE}/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ token }),
    credentials: 'same-origin',
    cache: 'no-store',
  });
  if (!response.ok) {
    const problem = await readProblem(response);
    throw new ApiError(response.status, problem, response.headers.get('X-Request-ID'));
  }
  return response.json();
}

export async function logout() {
  const response = await fetch(`${SERVICE_BASE}/logout`, {
    method: 'POST', credentials: 'same-origin', cache: 'no-store',
  });
  if (!response.ok) throw new ApiError(response.status, await readProblem(response));
}

export function createAgentRun(payload, { signal, idempotencyKey } = {}) {
  return request('/agent/runs', {
    method: 'POST',
    body: payload,
    signal,
    idempotencyKey: idempotencyKey || newIdempotencyKey(),
  });
}

export function getAgentRun(runId, { signal } = {}) {
  return request(`/agent/runs/${encodeURIComponent(runId)}`, { signal });
}

export function cancelAgentRun(runId, { signal } = {}) {
  return request(`/agent/runs/${encodeURIComponent(runId)}/cancel`, {
    method: 'POST', body: {}, signal,
  });
}

export function getWorkspaceIdentity() {
  return request('/account/session', { discover: true });
}

export function accountAction(action, body) {
  return request(`/account/${action}`, { method: action === 'profile' ? 'PATCH' : 'POST', body });
}

export function getAccountModel(signal) {
  return request('/account/model-settings', { signal });
}

export function saveAccountModel(body, signal) {
  return request('/account/model-settings', { method: 'PUT', body, signal });
}

export function listConversations(cursor, signal, projectId) {
  const params = new URLSearchParams();
  if (cursor) params.set('before', cursor);
  if (projectId) params.set('project_id', projectId);
  return request(`/conversations${params.size ? `?${params}` : ''}`, { signal });
}

export function listProjects(signal) {
  return request('/projects', { signal });
}

export function getProject(id, signal) {
  return request(`/projects/${encodeURIComponent(id)}`, { signal });
}

export function createProject(name, signal) {
  return request('/projects', { method: 'POST', body: { name }, signal });
}

export function updateProject(id, body, signal) {
  return request(`/projects/${encodeURIComponent(id)}`, { method: 'PATCH', body, signal });
}

export function listSourceRequests(signal) {
  return request('/source-requests', { signal });
}

export function decideSource(item, decision, signal) {
  return request(`/source-requests/${encodeURIComponent(item.application_id)}/decision`, {
    method: 'POST', body: { decision, connector_version: item.connector.version }, signal,
  });
}

export function renameConversation(id, title) {
  return request(`/conversations/${encodeURIComponent(id)}`, { method: 'PATCH', body: { title } });
}

export function deleteConversation(id) {
  return request(`/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

export function getConversation(conversationId, { signal } = {}) {
  return request(`/conversations/${encodeURIComponent(conversationId)}`, { signal });
}

export function createConversationTurn(conversationId, body, { signal, idempotencyKey } = {}) {
  return request(`/conversations/${encodeURIComponent(conversationId)}/turns`, {
    method: 'POST',
    body,
    signal,
    idempotencyKey: idempotencyKey || newIdempotencyKey(),
  });
}

export async function uploadManuscript(file, { signal, idempotencyKey = newIdempotencyKey() } = {}) {
  if (!file) {
    throw new ApiError(0, {
      title: '未选择文件',
      code: 'MANUSCRIPT_FILE_MISSING',
      message: '请先选择一个 PDF 或 DOCX 文件。',
      retryable: false,
    });
  }
  const contentType = manuscriptContentType(file.name);
  if (!contentType) {
    throw new ApiError(0, {
      title: '不支持的文件类型',
      code: 'MANUSCRIPT_TYPE_UNSUPPORTED',
      message: '仅支持 .pdf 或 .docx 文件。',
      retryable: false,
    });
  }

  const epoch = workspaceEpoch;
  let response;
  try {
    response = await fetch(`${API_BASE}/manuscripts`, {
      method: 'POST',
      headers: {
        'X-Tenant-ID': TENANT_ID,
        ...workspaceHeaders(),
        'X-Request-ID': requestId(),
        Accept: 'application/json',
        'Content-Type': contentType,
        'X-Filename': encodeURIComponent(file.name || ''),
        'Idempotency-Key': idempotencyKey,
      },
      body: file,
      signal,
      cache: 'no-store',
      credentials: 'same-origin',
    });
  } catch (error) {
    if (error && error.name === 'AbortError') throw error;
    throw new ApiError(0, {
      title: '无法连接服务',
      code: 'NETWORK_ERROR',
      message: '全文上传失败：无法连接当前服务。',
      retryable: true,
    });
  }

  confirmWorkspace(epoch);
  if (!response.ok) {
    const problem = await readProblem(response);
    confirmWorkspace(epoch);
    const error = new ApiError(response.status, problem, response.headers.get('X-Request-ID'));
    handleWorkspaceError(error);
    throw error;
  }
  const text = await response.text();
  confirmWorkspace(epoch);
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    throw new ApiError(response.status, {
      code: 'INVALID_JSON',
      message: '服务端返回了无法解析的全文解析状态。',
      retryable: false,
    });
  }
}

export function getManuscript(manuscriptId, { signal } = {}) {
  return request(`/manuscripts/${encodeURIComponent(manuscriptId)}?catalog_limit=0`, { signal });
}

export function getUploadCapability() {
  return request('/manuscripts/capability');
}

function resultResource(target, resource) {
  const params = new URLSearchParams({
    result_id: target.resultId,
    result_version: String(target.resultVersion),
    result_kind: target.resultKind,
  });
  return `/searches/${encodeURIComponent(target.queryId)}/${resource}?${params}`;
}

export function getPreparedCitation(target) {
  return request(resultResource(target, 'citation-suggestion'));
}

export function getCitationFormats(target, { signal } = {}) {
  return request(resultResource(target, 'citation-formats'), { signal });
}

export function createCitationText(body, { signal, idempotencyKey } = {}) {
  return request('/citation-texts', {
    method: 'POST',
    body,
    signal,
    idempotencyKey: idempotencyKey || newIdempotencyKey(),
  });
}

export function getCitationText(citationTextId, { signal } = {}) {
  return request(`/citation-texts/${encodeURIComponent(citationTextId)}`, { signal });
}

export function createSearch(payload, { signal, idempotencyKey } = {}) {
  return request('/searches', {
    method: 'POST',
    body: payload,
    signal,
    idempotencyKey: idempotencyKey || newIdempotencyKey(),
  });
}

export function getSearch(queryId, { signal } = {}) {
  return request(`/searches/${encodeURIComponent(queryId)}`, { signal });
}

export function cancelSearch(queryId, { signal, idempotencyKey } = {}) {
  return request(`/searches/${encodeURIComponent(queryId)}/cancel`, {
    method: 'POST',
    body: {},
    signal,
    idempotencyKey,
  });
}

export function createSnapshot(queryId, kind, { signal, idempotencyKey } = {}) {
  return request(`/searches/${encodeURIComponent(queryId)}/snapshots`, {
    method: 'POST',
    body: { kind },
    signal,
    idempotencyKey: idempotencyKey || newIdempotencyKey(),
  });
}

export function getSnapshot(snapshotId, { signal } = {}) {
  return request(`/snapshots/${encodeURIComponent(snapshotId)}`, { signal });
}

export function eventsUrl(queryId, afterSequence) {
  const base = `${API_BASE}/searches/${encodeURIComponent(queryId)}/events`;
  if (afterSequence == null) return base;
  const params = new URLSearchParams();
  params.set('after_sequence', String(afterSequence));
  return `${base}?${params.toString()}`;
}

export function eventRequestHeaders(afterSequence) {
  const headers = {
    Accept: 'text/event-stream',
    'X-Tenant-ID': TENANT_ID,
    ...workspaceHeaders(),
    'X-Request-ID': requestId(),
    'Cache-Control': 'no-cache',
  };
  // Last-Event-ID 与 after_sequence 必须一致，否则服务端应返回 REPLAY_REQUEST_CONFLICT。
  if (afterSequence != null) headers['Last-Event-ID'] = String(afterSequence);
  return headers;
}
