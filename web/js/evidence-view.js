import { el, mono } from './dom.js';
import { EVIDENCE_ROLE, SCOPE_FIELDS, SCOPE_MATCH, label, shortHash } from './labels.js';

const CHECK_FIELDS = [
  ['文本偏移', 'text_offset_valid'],
  ['PDF 定位', 'pdf_location_valid'],
  ['阅读顺序', 'reading_order_valid'],
  ['来源溯源', 'source_trace_valid'],
];

export function evidenceRoles(evidence) {
  const roles = [...new Set(evidence.map((item) => EVIDENCE_ROLE[item.evidence_role]).filter(Boolean))];
  return roles.length ? roles.join(' / ') : '角色未返回';
}

export function evidenceScope(evidence) {
  const scopes = [...new Set(evidence.map((item) => SCOPE_MATCH[item.scope_match]).filter(Boolean))];
  return scopes.length ? scopes.join(' / ') : '范围未知';
}

export function buildEvidence(ev) {
  const item = el('li', { class: 'evidence', dataset: { evidenceId: ev.evidence_id || '' } });
  item.append(el('blockquote', { class: 'evidence__quote', text: ev.quote || ev.display_quote || '（未返回原文片段）' }));
  if (ev.context) item.append(el('p', { class: 'evidence__context', text: ev.context }));

  const locator = ev.locator && typeof ev.locator === 'object' ? ev.locator : {};
  const start = ev.start_offset ?? ev.start;
  const end = ev.end_offset ?? ev.end;
  const page = locator.pdf_page ?? locator.page_start ?? ev.page_start;
  const printed = locator.printed_page ?? ev.printed_page_start;
  const section = Array.isArray(locator.section_path) && locator.section_path.length
    ? locator.section_path.join(' › ')
    : Array.isArray(ev.section_path) && ev.section_path.length
      ? ev.section_path.join(' › ')
      : null;
  const paragraph = locator.paragraph_index ?? ev.paragraph_index;
  const readingOrder = locator.reading_order_index ?? ev.reading_order_index;

  const grid = el('dl', { class: 'evidence__grid' });
  addGrid(grid, 'evidence_id', ev.evidence_id);
  addGrid(grid, 'claim_id', ev.claim_id);
  addGrid(grid, '证据角色', label(EVIDENCE_ROLE, ev.evidence_role));
  addGrid(grid, 'scope', label(SCOPE_MATCH, ev.scope_match));
  addGrid(grid, 'UTF-8 偏移', start != null && end != null ? `${start}–${end}` : '—');
  addGrid(grid, 'PDF 页', page != null ? String(page) : '—');
  if (printed) addGrid(grid, '印刷页', String(printed));
  addGrid(grid, '章节', section || '—');
  if (paragraph != null) addGrid(grid, '段落索引', String(paragraph));
  if (readingOrder != null) addGrid(grid, '阅读顺序', String(readingOrder));
  addGrid(grid, 'parser', ev.parser_version);
  addGrid(grid, 'normalization', ev.normalization_version);
  addGrid(grid, 'resolver', ev.source_resolver_id);
  addGrid(grid, 'raw_text_sha256', shortHash(ev.raw_text_sha256, 20), ev.raw_text_sha256);
  addGrid(grid, 'quote_sha256', shortHash(ev.quote_sha256, 20), ev.quote_sha256);
  if (ev.ocr_confidence != null) addGrid(grid, 'OCR 置信度', String(ev.ocr_confidence));
  item.append(grid);

  const scope = ev.scope && typeof ev.scope === 'object' ? ev.scope : null;
  if (scope) {
    const chips = el('div', { class: 'scope' });
    for (const [key, fieldLabel] of Object.entries(SCOPE_FIELDS)) {
      const value = scope[key];
      if (value == null || value === '' || (Array.isArray(value) && !value.length)) continue;
      const text = Array.isArray(value) ? value.join(' / ') : String(value);
      chips.append(el('span', { text: `${fieldLabel}：${text}` }));
    }
    if (chips.childNodes.length) item.append(chips);
  }

  const checks = el('div', { class: 'evidence__checks' });
  for (const [checkLabel, field] of CHECK_FIELDS) {
    const value = ev[field];
    const state = value === true ? 'true' : value === false ? 'false' : 'unknown';
    const text = value === true ? `${checkLabel}：通过` : value === false ? `${checkLabel}：失败` : `${checkLabel}：未知`;
    checks.append(el('span', { class: 'check', dataset: { ok: state }, text }));
  }
  item.append(checks);
  item.append(el('p', {
    class: 'evidence__note',
    text: '以上是服务端程序核验字段；当前演示未进行 PDF 人工视觉核对，字节偏移正确不等于版面阅读顺序已验证。',
  }));
  return item;
}

function addGrid(grid, term, value, title = null) {
  const dd = mono(value == null || value === '' ? '—' : value, title || (value ? String(value) : null));
  grid.append(el('div', {}, el('dt', { text: term }), dd));
}
