// 引用建议的只读查看、手动重试及有限轮询。

const UNAVAILABLE = '没有找到可核验的开放全文，不能生成原文引用建议。仍可复制书目信息。';
const SOURCE_TEMPORARY = '全文来源暂时不可用。可重试这一篇，无需重新检索。';
const FAILURE_TEXT = Object.freeze({
  CITATION_FULLTEXT_UNAVAILABLE: UNAVAILABLE,
  CITATION_SOURCE_NOT_OPEN: UNAVAILABLE,
  CITATION_LICENSE_UNCLEAR: '无法确认这个版本的开放许可，不能使用其正文。',
  CITATION_LICENSE_CONFLICT: '来源的开放许可信息冲突，不能使用其正文。',
  CITATION_INSUFFICIENT_EVIDENCE: '未找到足以支持当前论述的原文证据，未生成引用建议。',
  CITATION_SOURCE_TIMEOUT: SOURCE_TEMPORARY,
  CITATION_SOURCE_RATE_LIMITED: SOURCE_TEMPORARY,
  CITATION_RETRACTED: '来源已撤回或撤稿，不生成引用建议。',
  CITATION_CORRECTED: '来源存在更正，未确认可用版本前不生成引用建议。',
});

function suggestionFailure(error, fallback) {
  if (error?.code === 'CITATION_SOURCE_UNAVAILABLE') return error.retryable ? SOURCE_TEMPORARY : UNAVAILABLE;
  if (Object.hasOwn(FAILURE_TEXT, error?.code)) return FAILURE_TEXT[error.code];
  if (typeof error?.message === 'string' && error.message.trim()) return error.message;
  return fallback;
}

const READY = 'ready';
const FAILED = 'failed';

export function citationRequestBody({ queryId, resultId, resultVersion = 1, resultKind = 'candidate', model = null }) {
  const version = Number(resultVersion);
  const body = {
    query_id: String(queryId || ''),
    result_id: String(resultId || ''),
    result_version: Number.isInteger(version) && version >= 0 ? version : 1,
    citation_style: 'vancouver',
    language: 'zh',
  };
  if (resultKind === 'recommendation') body.result_kind = resultKind;
  if (model && typeof model === 'object') body.model = model;
  return body;
}

function timer(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export class CitationController {
  constructor({
    request,
    poll,
    lookup = null,
    wait = timer,
    interval = 900,
    maxAttempts = 360,
    onChange = () => {},
  }) {
    if (typeof request !== 'function' || typeof poll !== 'function') {
      throw new Error('引用建议缺少请求实现。');
    }
    this.request = request;
    this.poll = poll;
    this.lookup = lookup;
    this.wait = wait;
    this.interval = interval;
    this.maxAttempts = maxAttempts;
    this.onChange = onChange;
    this.state = { status: 'idle' };
    this.token = 0;
    this.target = null;
  }

  cancel() {
    this.token += 1;
    this.target = null;
  }

  open(target) {
    return this.#start(target, this.lookup ? this.#readPrepared : this.#run);
  }

  generate(target) {
    return this.#start(target, this.#run);
  }

  #start(target, action) {
    const token = ++this.token;
    this.target = target;
    this.#emit({ status: 'loading' });
    return action.call(this, token, target);
  }

  async #readPrepared(token, target) {
    try {
      for (let attempt = 0; attempt < this.maxAttempts; attempt += 1) {
        const { record, preparing, source_recheck: sourceRecheck } = await this.lookup(target);
        if (token !== this.token) return this.state;
        if (record?.status === READY || (!preparing && record)) {
          if (this.#done(record, sourceRecheck === true)) return this.state;
        }
        if (!preparing && !record) {
          this.#emit({ status: 'unprepared' });
          return this.state;
        }
        if (attempt < this.maxAttempts - 1) await this.wait(this.interval);
        if (token !== this.token) return this.state;
      }
      this.#emit({ status: 'timeout', message: '引用建议仍在准备，可稍后重新查看。' });
    } catch (error) {
      if (token !== this.token) return this.state;
      this.#emit({
        status: FAILED, code: error?.code,
        message: suggestionFailure(error, '无法读取引用建议状态。'), retryable: Boolean(error?.retryable),
      });
    }
    return this.state;
  }

  #emit(state) {
    this.state = state;
    this.onChange(state);
  }

  async #run(token, target) {
    try {
      const created = await this.request(target);
      if (token !== this.token) return this.state;
      const citationTextId = typeof created?.citation_text_id === 'string' ? created.citation_text_id : '';
      if (!citationTextId) {
        this.#emit({
          status: FAILED,
          code: 'CITATION_ID_MISSING',
          message: '服务端未返回引用建议编号。',
        });
        return this.state;
      }
      return await this.#awaitResult(token, citationTextId);
    } catch (error) {
      if (token !== this.token) return this.state;
      this.#emit({
        status: FAILED,
        code: typeof error?.code === 'string' ? error.code : 'CITATION_REQUEST_FAILED',
        message: suggestionFailure(error, '引用建议请求失败。'),
        retryable: Boolean(error?.retryable),
      });
      return this.state;
    }
  }

  async #awaitResult(token, citationTextId) {
    for (let attempt = 0; attempt < this.maxAttempts; attempt += 1) {
      let data;
      try {
        data = await this.poll(citationTextId);
      } catch (error) {
        if (token !== this.token) return this.state;
        this.#emit({
          status: FAILED,
          code: typeof error?.code === 'string' ? error.code : 'CITATION_POLL_FAILED',
          message: suggestionFailure(error, '无法读取引用建议状态。'),
          retryable: Boolean(error?.retryable),
        });
        return this.state;
      }
      if (token !== this.token) return this.state;
      if (this.#done(data)) return this.state;
      if (attempt < this.maxAttempts - 1) await this.wait(this.interval);
      if (token !== this.token) return this.state;
    }
    this.#emit({
      status: 'timeout',
      message: '引用建议尚未生成完成；可稍后重试。',
    });
    return this.state;
  }

  #done(data, sourceRecheck = false) {
    if (data?.status === READY) {
      if (typeof data.citation_text !== 'string' && typeof data.formatted_citation !== 'string') {
        this.#emit({ status: FAILED, code: 'CITATION_TEXT_MISSING', message: '服务端未返回可复制的引用文本。' });
      } else this.#emit({ status: READY, data });
      return true;
    }
    if (data?.status !== FAILED && data?.status !== 'cancelled') return false;
    this.#emit({
      status: FAILED,
      code: data.failure?.code || data.failure_code || (data.status === 'cancelled' ? 'CITATION_CANCELLED' : 'CITATION_FAILED'),
      message: sourceRecheck
        ? '旧记录没有取到全文；现在可补查这个版本的开放原文，无需重新检索。'
        : suggestionFailure(data.failure || data.error || { message: data.message }, '未能生成引用建议。'),
      sourceRecheck,
      retryable: sourceRecheck || data.status === 'cancelled' || data.failure?.retryable === true,
      limitations: Array.isArray(data.limitations) ? data.limitations : null,
    });
    return true;
  }
}
