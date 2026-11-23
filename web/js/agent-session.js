import { Store } from './store.js';
import { normalizeConversationMessages, runChatFallback } from './chat-model.js';

export const AGENT_TERMINAL = new Set(['completed', 'failed', 'cancelled']);

// 同一对话可包含多个 AgentRun；每个 query 的 Store 独立保留，新 run 不覆盖旧 artifact。
export class AgentSession {
  constructor() {
    this.reset();
  }

  reset() {
    this.conversationId = null;
    this.projectId = null;
    this.revision = null;
    this.messages = [];
    this.runs = new Map();
    this.runOrder = [];
    this.searches = new Map();
    this.queries = new Map();
    this.queryOrder = [];
    this.activeId = null;
    this.followLatest = true;
    this.emptyStore = new Store();
    this.outbox = [];
    this.outboxSeq = 0;
    this.libraryIds = [];
  }

  get store() {
    return this.searches.get(this.activeId) || this.emptyStore;
  }

  get run() {
    const query = this.activeId ? this.queries.get(this.activeId) : null;
    if (query) return this.runs.get(query.runId) || this.latestRun;
    return this.latestRun;
  }

  get latestRun() {
    for (let index = this.runOrder.length - 1; index >= 0; index -= 1) {
      const run = this.runs.get(this.runOrder[index]);
      if (run) return run;
    }
    return null;
  }

  get runList() {
    return this.runOrder.map((runId) => this.runs.get(runId)).filter(Boolean);
  }

  get queryList() {
    return this.queryOrder.map((queryId) => this.queries.get(queryId)).filter(Boolean);
  }

  get activeRuns() {
    return this.runList.filter((run) => !AGENT_TERMINAL.has(run.status));
  }

  get activeRun() {
    const runs = this.activeRuns;
    return runs.length ? runs[runs.length - 1] : null;
  }

  get active() {
    return this.activeRuns.length > 0;
  }

  get hasQueries() {
    return this.queryOrder.length > 0;
  }

  get hasConversation() {
    return Boolean(this.conversationId);
  }

  storeForQuery(queryId) {
    return this.searches.get(queryId) || null;
  }

  runForQuery(queryId) {
    const query = this.queries.get(queryId);
    return query ? this.runs.get(query.runId) || null : null;
  }

  queriesForRun(runId) {
    if (!runId) return [];
    return this.queryList.filter((query) => query.runId === runId);
  }

  applyRun(run, libraries = [], { focus = this.followLatest } = {}) {
    const empty = { addedIds: [], activeChanged: false, addedRun: false };
    if (!run || typeof run.run_id !== 'string') return empty;
    if (run.conversation_id && this.conversationId && run.conversation_id !== this.conversationId) return empty;
    if (run.conversation_id && !this.conversationId) this.conversationId = run.conversation_id;

    const existing = this.runs.get(run.run_id);
    const merged = mergeRun(existing, run);
    this.runs.set(run.run_id, merged);
    if (!this.runOrder.includes(run.run_id)) this.runOrder.push(run.run_id);

    const addedIds = [];
    for (const query of Array.isArray(run.queries) ? run.queries : []) {
      const queryId = query?.created?.query_id;
      if (!queryId) continue;
      const previous = this.queries.get(queryId);
      if (!previous) {
        this.queries.set(queryId, {
          queryId,
          runId: run.run_id,
          created: { ...query.created },
          requested_library_ids: Array.isArray(query.requested_library_ids)
            ? [...query.requested_library_ids] : [],
        });
        this.queryOrder.push(queryId);
      } else {
        previous.created = { ...previous.created, ...query.created };
        if (Array.isArray(query.requested_library_ids)) {
          previous.requested_library_ids = [...query.requested_library_ids];
        }
      }
      if (!this.searches.has(queryId)) {
        const search = new Store();
        search.attachCreateResponse(query.created);
        search.seedPendingWorkers(query.requested_library_ids, libraries);
        this.searches.set(queryId, search);
        addedIds.push(queryId);
      }
    }

    const previousActive = this.activeId;
    const latest = typeof merged.latest_query_id === 'string' && this.searches.has(merged.latest_query_id)
      ? merged.latest_query_id
      : this.#lastRunQuery(merged.run_id);
    if (focus && latest) this.activeId = latest;
    return {
      addedIds,
      activeChanged: previousActive !== this.activeId,
      addedRun: !existing,
    };
  }

  applyConversation(payload, libraries = [], { focus = false } = {}) {
    const empty = { addedIds: [], activeChanged: false, addedRuns: [] };
    if (!payload || typeof payload !== 'object') return empty;
    const conversationId = typeof payload.conversation_id === 'string' ? payload.conversation_id : null;
    if (conversationId && this.conversationId && conversationId !== this.conversationId) return empty;
    if (conversationId && !this.conversationId) this.conversationId = conversationId;

    const revision = Number(payload.revision);
    if (Number.isFinite(revision)) {
      if (this.revision != null && revision < this.revision) return empty;
      this.revision = revision;
    }

    this.projectId = typeof payload.project_id === 'string' ? payload.project_id : null;
    const previousActive = this.activeId;
    const addedIds = new Set();
    const addedRuns = [];
    for (const run of Array.isArray(payload.runs) ? payload.runs : []) {
      const result = this.applyRun(run, libraries, { focus: false });
      for (const queryId of result.addedIds) addedIds.add(queryId);
      if (result.addedRun && typeof run?.run_id === 'string') addedRuns.push(run.run_id);
    }
    this.messages = Array.isArray(payload.messages)
      ? normalizeConversationMessages(payload.messages)
      : this.messages;
    this.#pruneOutbox();

    if (focus) {
      const preferred = typeof payload.active_query_id === 'string'
        ? payload.active_query_id
        : typeof payload.last_active_query_id === 'string' ? payload.last_active_query_id : null;
      if (preferred && this.searches.has(preferred)) {
        this.activeId = preferred;
        this.followLatest = true;
      } else {
        this.focusLatest();
      }
    }

    return {
      addedIds: [...addedIds],
      activeChanged: previousActive !== this.activeId,
      addedRuns,
    };
  }

  focusLatest() {
    for (let index = this.queryOrder.length - 1; index >= 0; index -= 1) {
      const queryId = this.queryOrder[index];
      if (this.searches.has(queryId)) {
        this.activeId = queryId;
        this.followLatest = true;
        return true;
      }
    }
    return false;
  }

  select(queryId) {
    if (!this.searches.has(queryId)) return false;
    this.activeId = queryId;
    this.followLatest = false;
    return true;
  }

  forQuery(queryId) {
    return this.searches.get(queryId) || null;
  }

  addPendingMessage(content, { kind = 'turn' } = {}) {
    const text = String(content || '');
    const random = globalThis.crypto?.randomUUID?.()
      || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const pending = {
      id: `local-${++this.outboxSeq}`,
      key: `msg-${random}`,
      content: text,
      kind,
      status: 'sending',
      runId: null,
      failure: '',
    };
    this.outbox.push(pending);
    return pending;
  }

  findPending(pendingId) {
    return this.outbox.find((item) => item.id === pendingId) || null;
  }

  findFailedPending(content) {
    const text = String(content || '');
    return this.outbox.find((item) => item.status === 'failed' && item.content === text) || null;
  }

  markPendingSent(pendingId, runId) {
    const pending = this.findPending(pendingId);
    if (!pending) return null;
    pending.status = 'sent';
    pending.runId = typeof runId === 'string' ? runId : null;
    pending.failure = '';
    return pending;
  }

  markPendingFailed(pendingId, failure) {
    const pending = this.findPending(pendingId);
    if (!pending) return null;
    pending.status = 'failed';
    pending.failure = typeof failure === 'string' ? failure : '消息发送失败。';
    return pending;
  }

  chatMessages() {
    const rows = this.messages.map((message) => {
      const queryIds = message.query_ids?.length
        ? message.query_ids
        : message.role === 'assistant'
          ? this.queriesForRun(message.run_id).map((query) => query.queryId)
          : [];
      return {
        ...message,
        query_ids: queryIds,
        kind: message.role === 'user' ? 'user' : 'assistant',
      };
    });
    const assistantRuns = new Set(
      this.messages.filter((message) => message.role === 'assistant' && message.run_id
        && typeof message.content === 'string' && message.content.trim())
        .map((message) => message.run_id),
    );
    const serverUserRuns = new Set(
      this.messages.filter((message) => message.role === 'user' && message.run_id)
        .map((message) => message.run_id),
    );
    for (const pending of this.outbox) {
      if (pending.runId && serverUserRuns.has(pending.runId)) continue;
      rows.push({
        message_id: pending.id,
        role: 'user',
        content: pending.content,
        run_id: pending.runId,
        query_ids: [],
        kind: 'user',
        pending: pending.status !== 'failed',
        failed: pending.status === 'failed',
        failure: pending.failure,
        local: true,
      });
    }
    for (const run of this.runList) {
      if (assistantRuns.has(run.run_id)) continue;
      const fallback = runChatFallback(run);
      if (!fallback) continue;
      fallback.query_ids = this.queriesForRun(run.run_id).map((query) => query.queryId);
      rows.push(fallback);
    }
    return rows;
  }

  #lastRunQuery(runId) {
    if (!runId) return null;
    for (let index = this.queryOrder.length - 1; index >= 0; index -= 1) {
      const queryId = this.queryOrder[index];
      if (this.queries.get(queryId)?.runId === runId && this.searches.has(queryId)) return queryId;
    }
    return null;
  }

  #pruneOutbox() {
    this.outbox = this.outbox.filter((pending) => {
      if (!pending.runId) return true;
      return !this.messages.some((message) => message.role === 'user' && message.run_id === pending.runId);
    });
  }
}

function mergeRun(existing, incoming) {
  if (!existing) {
    return {
      ...incoming,
      queries: Array.isArray(incoming.queries) ? incoming.queries.slice() : [],
    };
  }
  const merged = { ...existing, ...incoming };
  if (AGENT_TERMINAL.has(existing.status) && !AGENT_TERMINAL.has(incoming.status)) {
    merged.status = existing.status;
  }
  if (!Array.isArray(incoming.queries)) {
    merged.queries = existing.queries;
  } else {
    const byId = new Map();
    for (const query of existing.queries || []) {
      const queryId = query?.created?.query_id;
      if (queryId) byId.set(queryId, query);
    }
    for (const query of incoming.queries) {
      const queryId = query?.created?.query_id;
      if (queryId) byId.set(queryId, query);
    }
    merged.queries = [...byId.values()];
  }
  if (incoming.latest_query_id == null) merged.latest_query_id = existing.latest_query_id;
  if (incoming.summary == null) merged.summary = existing.summary;
  if (incoming.error == null) merged.error = existing.error;
  return merged;
}
