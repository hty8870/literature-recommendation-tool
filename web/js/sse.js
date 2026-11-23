// SSE 客户端：用 fetch + ReadableStream，因为 EventSource 无法带 X-Tenant-ID / Last-Event-ID。
// 断线自动重连最多 3 次；after_sequence 与 Last-Event-ID 始终使用同一个值。

import { ApiError, eventsUrl, eventRequestHeaders, readProblemFromResponse } from './api.js';

const MAX_RECONNECTS = 3;
const BACKOFF_MS = [600, 1400, 2800];
const IDLE_MS = 45_000;

export class SearchEventStream {
  constructor({ queryId, getAfterSequence, onEvent, onConnection, onGap, onExhausted, isTerminal, idleMs = IDLE_MS }) {
    this.queryId = queryId;
    this.getAfterSequence = getAfterSequence;
    this.onEvent = onEvent;
    this.onConnection = onConnection;
    this.onGap = onGap;
    this.onExhausted = onExhausted;
    this.isTerminal = isTerminal || (() => false);
    this.idleMs = idleMs;
    this.stopped = true;
    this.attempts = 0;
    this.abort = null;
    this.protocolErrors = 0;
  }

  start() {
    if (!this.stopped) return;
    this.stopped = false;
    this.attempts = 0;
    this.protocolErrors = 0;
    void this.#loop();
  }

  stop() {
    this.stopped = true;
    if (this.abort) {
      try {
        this.abort.abort();
      } catch {
        /* 已结束 */
      }
      this.abort = null;
    }
  }

  async #loop() {
    while (!this.stopped) {
      let failure = null;
      try {
        await this.#attempt(this.getAfterSequence());
      } catch (error) {
        if (this.stopped) return;
        if (error instanceof ApiError) {
          if (error.code === 'REPLAY_GAP' || error.code === 'REPLAY_REQUEST_CONFLICT') {
            this.onGap(error);
            return;
          }
          if (error.status >= 400 && error.status < 500 && error.status !== 429 && !error.retryable) {
            this.onConnection('error', { error });
            return;
          }
        }
        failure = error;
      }

      if (this.stopped) return;
      if (this.isTerminal()) {
        this.onConnection('closed');
        return;
      }
      this.attempts += 1;
      if (this.attempts > MAX_RECONNECTS) {
        this.onExhausted({ attempts: MAX_RECONNECTS, lastError: failure });
        return;
      }
      this.onConnection('reconnecting', { attempt: this.attempts, max: MAX_RECONNECTS, error: failure });
      await sleep(BACKOFF_MS[this.attempts - 1] + Math.floor(Math.random() * 200));
    }
  }

  async #attempt(after) {
    const controller = new AbortController();
    this.abort = controller;
    let timer;
    let timedOut = false;
    let reader;
    const received = () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, this.idleMs);
    };

    try {
      received();
      const response = await fetch(eventsUrl(this.queryId, after), {
        method: 'GET',
        headers: eventRequestHeaders(after),
        signal: controller.signal,
        cache: 'no-store',
        credentials: 'same-origin',
      });
      if (!response.ok) {
        const problem = await readProblemFromResponse(response);
        throw new ApiError(response.status, problem, response.headers.get('X-Request-ID'));
      }
      if (!response.body || !response.headers.get('Content-Type')?.startsWith('text/event-stream')) {
        throw new ApiError(0, {
          code: 'STREAM_UNAVAILABLE', message: '服务未返回可读取的事件流。', retryable: true,
        });
      }
      if (this.stopped) return;
      this.onConnection('live', { resumeFrom: after });
      reader = response.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let buffer = '';

      while (!this.stopped) {
        const chunk = await reader.read();
        if (chunk.done || this.stopped) return;
        if (chunk.value.byteLength) received();
        buffer += decoder.decode(chunk.value, { stream: true });

        let split;
        while (!this.stopped && (split = takeBlock(buffer))) {
          buffer = split.rest;
          const message = parseBlock(split.block);
          if (!message.data) continue;
          const envelope = decodeEnvelope(message);
          if (!envelope) {
            this.protocolErrors += 1;
            this.onConnection('protocol-error', { count: this.protocolErrors });
            continue;
          }
          const previousSequence = this.getAfterSequence();
          this.onEvent(envelope);
          if (this.getAfterSequence() > (previousSequence ?? 0)) this.attempts = 0;
          if (this.isTerminal()) return;
        }
      }
    } catch (error) {
      if (!timedOut) throw error;
      throw new ApiError(0, {
        code: 'STREAM_IDLE_TIMEOUT', message: '事件流长时间未收到结果或心跳。', retryable: true,
      });
    } finally {
      clearTimeout(timer);
      try {
        if (reader) await reader.cancel();
      } catch {
        // Fetch may already have closed the body after aborting.
      } finally {
        controller.abort();
        if (this.abort === controller) this.abort = null;
      }
    }
  }
}

function decodeEnvelope(message) {
  try {
    const envelope = JSON.parse(message.data);
    if (!envelope || typeof envelope !== 'object') return null;
    if (!envelope.event_type && message.event) envelope.event_type = message.event;
    if (envelope.sequence == null && message.id && /^\d+$/.test(message.id)) {
      envelope.sequence = Number(message.id);
    }
    return envelope;
  } catch {
    return null;
  }
}

function takeBlock(buffer) {
  const match = /\r?\n\r?\n/.exec(buffer);
  if (!match) return null;
  return {
    block: buffer.slice(0, match.index),
    rest: buffer.slice(match.index + match[0].length),
  };
}

function parseBlock(block) {
  const message = { data: '', event: null, id: null };
  const dataLines = [];
  for (const rawLine of block.split(/\r?\n/)) {
    if (!rawLine || rawLine.startsWith(':')) continue;
    const colon = rawLine.indexOf(':');
    const field = colon === -1 ? rawLine : rawLine.slice(0, colon);
    let value = colon === -1 ? '' : rawLine.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'data') dataLines.push(value);
    else if (field === 'event') message.event = value;
    else if (field === 'id') message.id = value;
  }
  message.data = dataLines.join('\n');
  return message;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
