// SSE 客户端：用 fetch + ReadableStream，因为 EventSource 无法带 X-Tenant-ID / Last-Event-ID。
// 断线自动重连最多 3 次；after_sequence 与 Last-Event-ID 始终使用同一个值。

import { ApiError, eventsUrl, eventRequestHeaders, readProblemFromResponse } from './api.js';

const MAX_RECONNECTS = 3;
const BACKOFF_MS = [600, 1400, 2800];

export class SearchEventStream {
  constructor({ queryId, getAfterSequence, onEvent, onConnection, onGap, onExhausted, isTerminal }) {
    this.queryId = queryId;
    this.getAfterSequence = getAfterSequence;
    this.onEvent = onEvent;
    this.onConnection = onConnection;
    this.onGap = onGap;
    this.onExhausted = onExhausted;
    this.isTerminal = isTerminal || (() => false);
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
      const after = this.getAfterSequence();
      let outcome = null;
      try {
        outcome = await this.#attempt(after);
      } catch (error) {
        if (this.stopped || (error && error.name === 'AbortError')) return;
        if (error instanceof ApiError) {
          if (error.code === 'REPLAY_GAP' || error.code === 'REPLAY_REQUEST_CONFLICT') {
            this.onGap(error);
            return;
          }
          if (error.status >= 400 && error.status < 500) {
            this.onConnection('error', { error });
            return;
          }
          this.onConnection('reconnecting', {
            attempt: Math.min(this.attempts + 1, MAX_RECONNECTS),
            max: MAX_RECONNECTS,
            error,
          });
        } else {
          this.onConnection('reconnecting', {
            attempt: Math.min(this.attempts + 1, MAX_RECONNECTS),
            max: MAX_RECONNECTS,
            error,
          });
        }
      }

      if (this.stopped) return;
      if (outcome && outcome.terminal) {
        this.onConnection('closed');
        return;
      }
      if (this.isTerminal()) {
        this.onConnection('closed');
        return;
      }

      this.attempts += 1;
      if (this.attempts > MAX_RECONNECTS) {
        this.onExhausted({ attempts: MAX_RECONNECTS, lastError: outcome && outcome.error });
        return;
      }
      this.onConnection('reconnecting', { attempt: this.attempts, max: MAX_RECONNECTS });
      await sleep(BACKOFF_MS[this.attempts - 1] + Math.floor(Math.random() * 200));
    }
  }

  async #attempt(after) {
    const controller = new AbortController();
    this.abort = controller;
    const startedAt = performance.now();
    let receivedAny = false;
    let terminal = false;
    let lastError = null;

    let response;
    try {
      response = await fetch(eventsUrl(this.queryId, after), {
        method: 'GET',
        headers: eventRequestHeaders(after),
        signal: controller.signal,
        cache: 'no-store',
        credentials: 'same-origin',
      });
    } catch (error) {
      if (error && error.name === 'AbortError') throw error;
      throw new ApiError(0, {
        code: 'NETWORK_ERROR',
        message: '事件流无法连接。',
        retryable: true,
      });
    }

    if (!response.ok) {
      const problem = await readProblemFromResponse(response);
      throw new ApiError(response.status, problem, response.headers.get('X-Request-ID'));
    }
    if (!response.body) {
      throw new ApiError(0, {
        code: 'STREAM_UNAVAILABLE',
        message: '当前浏览器不支持流式响应。',
        retryable: false,
      });
    }

    this.onConnection('live', { resumeFrom: after });

    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    try {
      while (!this.stopped) {
        let chunk;
        try {
          chunk = await reader.read();
        } catch (error) {
          if (this.stopped || (error && error.name === 'AbortError')) break;
          lastError = error;
          break;
        }
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });

        let split = takeBlock(buffer);
        while (split) {
          buffer = split.rest;
          const message = parseBlock(split.block);
          if (message && message.data) {
            const envelope = decodeEnvelope(message);
            if (envelope) {
              const sequence = Number(envelope.sequence);
              if (Number.isFinite(sequence) && (after == null || sequence > after)) {
                receivedAny = true;
                this.attempts = 0;
              }
              if (!this.stopped) {
                this.onEvent(envelope);
              }
              if (this.isTerminal()) {
                terminal = true;
                break;
              }
            } else {
              this.protocolErrors += 1;
              this.onConnection('protocol-error', { count: this.protocolErrors });
            }
          }
          split = takeBlock(buffer);
        }
        if (terminal) break;
      }
    } finally {
      try {
        await reader.cancel();
      } catch {
        /* 连接已关闭 */
      }
      controller.abort();
      this.abort = null;
    }

    return { receivedAny, terminal, lastError, elapsedMs: performance.now() - startedAt };
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
