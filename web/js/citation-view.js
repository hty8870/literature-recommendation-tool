import { clear, el, setText } from './dom.js';
import { icon } from './icons.js';
import { CitationController } from './citation-controller.js';

export class CitationDialog {
  constructor({ refs, request, poll, lookup, copy, openFormats, onToast, wait, interval, maxAttempts }) {
    this.refs = refs;
    this.copy = copy;
    this.openFormats = openFormats;
    this.onToast = onToast || (() => {});
    this.currentTarget = null;
    this.lastReady = null;
    this.draftInput = null;
    this.lastRenderedId = null;
    this.controller = new CitationController({
      request,
      poll,
      lookup,
      wait,
      interval,
      maxAttempts,
      onChange: (state) => this.render(state),
    });
    refs.close.addEventListener('click', () => this.close());
    refs.dialog.addEventListener('close', () => {
      this.controller.cancel();
      this.currentTarget = null;
      this.lastReady = null;
    });
    refs.body.addEventListener('click', (event) => {
      const copyButton = event.target.closest('[data-action="citation-copy"]');
      if (copyButton) {
        event.preventDefault();
        if (copyButton.dataset.copyKind === 'bibliography' && this.currentTarget && this.openFormats) {
          this.openFormats(copyButton, this.currentTarget, citationBibliography(this.lastReady));
        } else void this.copyCurrent('text');
        return;
      }
      const retry = event.target.closest('[data-action="citation-retry"]');
      if (retry && this.currentTarget) {
        event.preventDefault();
        void this.controller.generate(this.currentTarget);
      }
    });
  }

  open(target) {
    this.currentTarget = target;
    this.lastReady = null;
    this.lastRenderedId = null;
    this.draftInput = null;
    setText(this.refs.title, target.title || '引用建议');
    if (!this.refs.dialog.open) this.refs.dialog.showModal();
    void this.controller.open(target);
  }

  close() {
    if (this.refs.dialog.open) this.refs.dialog.close();
  }

  showLoading() {
    clear(this.refs.body);
    this.refs.body.setAttribute('aria-busy', 'true');
    this.refs.body.append(
      el('p', { class: 'citation-loading', text: '正在准备引用建议，您可以先查看其他结果。' }),
    );
  }

  render(state) {
    this.refs.body.dataset.status = state.status;
    this.refs.body.removeAttribute('aria-busy');
    if (state.status === 'loading' || state.status === 'idle') {
      this.showLoading();
      return;
    }
    if (state.status === 'ready') {
      this.lastReady = state.data;
      this.renderReady(state.data);
      return;
    }
    clear(this.refs.body);
    const unprepared = state.status === 'unprepared';
    if (unprepared || state.status === 'timeout') {
      this.refs.body.append(el('p', {
        class: 'citation-loading', text: state.message || '这篇文献还没有准备好的引用建议。',
      }));
    } else {
      this.refs.body.append(el('p', {
        class: 'form-error citation-error', text: state.message || '未能生成引用建议。',
      }));
    }
    if (Array.isArray(state.limitations) && state.limitations.length) {
      this.refs.body.append(buildLimitations(state.limitations));
    }
    if (unprepared || state.retryable || state.status === 'timeout') {
      this.refs.body.append(el('div', { class: 'form-actions' }, el('button', {
        type: 'button', class: 'button button--quiet',
        'data-action': 'citation-retry', text: unprepared ? '生成这一篇' : state.sourceRecheck ? '补查原文' : '重试这一篇',
      })));
    }
  }

  renderReady(data) {
    if (data.citation_text_id && this.lastRenderedId === data.citation_text_id) return;
    this.lastRenderedId = data.citation_text_id || null;
    clear(this.refs.body);
    const output = el('section', { class: 'citation-output' });
    if (typeof data.citation_text === 'string' && data.citation_text) {
      const draft = el('textarea', {
        class: 'citation-output__draft', rows: 4, 'aria-label': '可编辑的引用表述', spellcheck: false,
      });
      draft.value = data.citation_text;
      this.draftInput = draft;
      const note = el('p', { class: 'field-help', text: '可自行修改；修改后的文本未经模型复核。' });
      draft.addEventListener('input', () => {
        setText(note, draft.value === data.citation_text
          ? '可自行修改；修改后的文本未经模型复核。' : '已修改；复制前请重新对照下方原文。');
      });
      output.append(el('h3', { text: '引用表述' }), draft, note,
        el('div', { class: 'citation-output__actions' }, el('button', {
          type: 'button', class: 'button button--primary',
          'data-action': 'citation-copy', 'data-copy-kind': 'text',
        }, icon('clipboard'), '复制引用表述')));
    }
    const bibliography = citationBibliography(data);
    if (bibliography) {
      output.append(el('h3', { class: 'citation-output__subhead', text: '规范书目' }),
        el('p', { class: 'citation-output__text', text: bibliography }),
        el('div', { class: 'citation-output__actions' }, el('button', {
          type: 'button', class: 'button button--quiet', 'data-action': 'citation-copy',
          'data-copy-kind': 'bibliography',
          'aria-haspopup': 'menu',
        }, icon('clipboard'), '复制规范书目')));
    }
    this.refs.body.append(output);

    this.refs.body.append(el('p', {
      class: 'dialog-note',
      text: data.verification_notice || '引用表述需作者对照原文核查；书目由服务端按所选 CSL 样式生成，不代表全文认证或自动采用。',
    }));

    const evidenceSection = el('section', { class: 'citation-evidence' },
      el('h3', { text: '原文依据' }),
    );
    const facts = citationFactEvidence(data);
    if (facts.length) {
      for (const fact of facts) {
        const group = el('section', { class: 'citation-evidence__fact' },
          el('p', { class: 'citation-evidence__statement', text: fact.statement }));
        group.append(el('ul', { class: 'citation-evidence__list' },
          fact.evidence.map(buildCitationEvidence)));
        if (fact.missingEvidence) group.append(el('p', {
          class: 'detail-unavailable', text: '部分事实依据未返回，不能视为原文核查完成。',
        }));
        evidenceSection.append(group);
      }
    } else {
      evidenceSection.append(el('p', {
        class: 'detail-unavailable',
        text: '服务端未返回事实与原文的映射；不能把这份建议当作已核对全文。',
      }));
    }
    this.refs.body.append(evidenceSection);

    if (data.limitations) {
      const limitations = Array.isArray(data.limitations)
        ? data.limitations
        : typeof data.limitations === 'string' ? [data.limitations] : [];
      if (limitations.length) this.refs.body.append(buildLimitations(limitations));
    }
    const sourceLabels = {
      fulltext_xml: '开放全文',
      fulltext_html: '开放预印本正文（非期刊正式版）',
      verified_evidence: '已核验片段',
      mixed: '多类原文证据',
    };
    if (Object.hasOwn(sourceLabels, data.source_level)) {
      this.refs.body.append(el('p', {
        class: 'citation-level', text: `引用依据：${sourceLabels[data.source_level]}`,
      }));
    }
  }

  async copyCurrent(kind = 'bibliography') {
    const data = this.lastReady;
    const text = kind === 'text' ? this.draftInput?.value || '' : citationBibliography(data);
    if (!text) {
      this.onToast('当前没有可复制的引用建议。', { level: 'error' });
      return;
    }
    try {
      await this.copy(text);
      this.onToast('已复制引用建议；仍需核对原文与使用限定。');
    } catch (error) {
      this.onToast(error.message || '复制失败。', { level: 'error' });
    }
  }
}

export function citationBibliography(data) {
  if (typeof data?.bibliography?.bibliography === 'string') return data.bibliography.bibliography;
  return typeof data?.formatted_citation === 'string' ? data.formatted_citation : '';
}

export function citationFactEvidence(data) {
  const evidence = Array.isArray(data?.evidence) ? data.evidence : [];
  const byId = new Map(evidence.filter((item) => item?.evidence_id)
    .map((item) => [item.evidence_id, item]));
  const facts = Array.isArray(data?.facts) ? data.facts : [];
  return facts.map((fact) => {
    const ids = [...new Set(Array.isArray(fact?.evidence_ids) ? fact.evidence_ids : [])];
    const matched = ids.map((id) => byId.get(id)).filter(Boolean);
    return {
      statement: typeof fact?.statement === 'string' ? fact.statement : '未返回完整事实表述。',
      evidence: matched,
      missingEvidence: !ids.length || matched.length !== ids.length,
    };
  });
}

export function citationLocatorLabel(evidence) {
  if (typeof evidence?.paragraph_id === 'string' && evidence.paragraph_id.trim()) {
    return `段落 ${evidence.paragraph_id}`;
  }
  return Number.isInteger(evidence?.paragraph_index) && evidence.paragraph_index >= 0
    ? `段落 ${evidence.paragraph_index + 1}` : '';
}

function buildCitationEvidence(evidence) {
  if (!evidence || typeof evidence !== 'object') {
    return el('li', { class: 'citation-evidence__item', text: '（未返回原文片段）' });
  }
  const item = el('li', { class: 'citation-evidence__item' });
  item.append(el('blockquote', {
    class: 'citation-evidence__quote',
    text: typeof evidence.quote === 'string' && evidence.quote ? evidence.quote : '（未返回原文片段）',
  }));
  const meta = el('p', { class: 'citation-evidence__meta' });
  const section = Array.isArray(evidence.section_path)
    ? evidence.section_path.filter(Boolean).join(' › ')
    : typeof evidence.section_path === 'string' ? evidence.section_path : '';
  if (section) meta.append(el('span', { text: section }));
  const locator = citationLocatorLabel(evidence);
  if (locator) meta.append(el('span', { text: locator }));
  const url = secureUrl(evidence.source_url || evidence.source_uri);
  if (url) {
    meta.append(el('a', {
      href: url,
      target: '_blank',
      rel: 'noopener noreferrer',
      text: '打开来源',
    }));
  }
  if (meta.childNodes.length) item.append(meta);
  return item;
}

function buildLimitations(limitations) {
  return el('div', { class: 'citation-limitation' },
    el('strong', { text: '使用限定 ' }),
    el('span', { text: limitations.map((item) => String(item)).join('；') }),
  );
}

function secureUrl(value) {
  if (typeof value !== 'string' || !value) return null;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : null;
  } catch {
    return null;
  }
}
