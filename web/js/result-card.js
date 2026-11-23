import { el } from './dom.js';
import { icon } from './icons.js';
import { evidenceScope } from './evidence-view.js';
import { resultMetadata, resultTitle } from './citation.js';
import { RELATIONSHIP, REVOKE_REASON, label } from './labels.js';
import { libraryDisplayName } from './search-input.js';

export function recommendationReason(rec) {
  if (rec.revoked || !rec.citable) return '该候选尚未通过可引用资格，仅保留作诊断，不建议直接引用。';
  if (rec.reason?.status === 'generated' && rec.reason.text) return rec.reason.text;
  const code = rec.reason?.failure_code || 'MODEL_NOT_CONFIGURED';
  return `模型未能生成推荐理由（${code}）。请直接核对原文，不展示模板冒充模型理由。`;
}

export function cardSignature(rec) {
  const evidence = Array.isArray(rec.evidence) ? rec.evidence.map((item) => item.evidence_id || item.quote_sha256 || '').join(',') : '';
  const sources = Array.isArray(rec.sources) ? rec.sources.map((item) => item.paper_id || item.library_id || '').join(',') : '';
  return [
    rec.result_version,
    rec.updated_at,
    rec.citable,
    rec.rankable,
    rec.current_rank,
    rec.relationship,
    rec.verification_status,
    rec.reason?.status,
    rec.reason?.text,
    rec.reason?.model,
    rec.provisional,
    rec.revoked ? `${rec.revoked.reason}:${rec.revoked.notice_version || ''}` : '',
    evidence,
    sources,
  ].join('|');
}

export function buildCard(rec, { late = false } = {}) {
  const classes = ['result-card'];
  if (rec.revoked) classes.push('result-card--revoked');
  else if (!rec.rankable) classes.push('result-card--evidence');
  const card = el('article', {
    class: classes.join(' '),
    dataset: { resultId: rec.result_id },
    'aria-label': `${resultTitle(rec)}，打开证据详情`,
  });

  const scoreRail = el('div', { class: 'result-card__score-rail' });
  scoreRail.append(el('span', { class: 'result-card__rank', text: rankLabel(rec) }));
  card.append(scoreRail);

  const content = el('div', { class: 'result-card__content' });
  const titleLine = el('div');
  const titleLink = el('a', {
    class: 'result-card__title',
    href: `#result=${encodeURIComponent(rec.result_id)}`,
    text: resultTitle(rec),
    dataset: { action: 'open-result', resultId: rec.result_id },
  });
  titleLine.append(el('h3', { class: 'result-card__heading' }, titleLink));
  const badges = el('span', { class: 'result-card__badges' });
  if (rec.citable && !rec.revoked) {
    badges.append(el('span', { class: `badge ${rec.rankable ? 'badge--cite' : 'badge--evidence'}`, text: rec.rankable ? '可引用' : '仅证据' }));
  } else {
    badges.append(el('span', { class: 'badge badge--revoked', text: '已撤销' }));
  }
  if (rec.relationship) badges.append(el('span', { class: 'badge', text: label(RELATIONSHIP, rec.relationship) }));
  if (rec.provisional) badges.append(el('span', { class: 'badge badge--pending', text: '暂定' }));
  if (late) badges.append(el('span', { class: 'badge badge--diag', text: '定稿后迟到' }));
  titleLine.append(badges);
  content.append(titleLine);

  const metadata = resultMetadata(rec);
  const meta = el('div', { class: 'result-card__meta' });
  if (metadata.authors.length) {
    const authors = metadata.authors.length > 3 ? `${metadata.authors.slice(0, 3).join('、')} 等` : metadata.authors.join('、');
    meta.append(el('span', { text: authors }));
  }
  meta.append(el('span', { text: metadata.year ? String(metadata.year) : '年份未提供' }));
  if (metadata.venue) meta.append(el('span', { text: String(metadata.venue) }));
  if (metadata.doi) meta.append(el('span', { text: `DOI ${metadata.doi}` }));
  content.append(meta);

  content.append(el('p', { class: 'result-card__reason' },
    el('span', { text: '推荐理由' }),
    document.createTextNode(recommendationReason(rec)),
  ));
  const claim = rec.claim && typeof rec.claim === 'object' ? rec.claim : {};
  const claimText = claim.claim_text || rec.claim_text || '目标论述未返回';
  content.append(el('p', {
    class: 'result-card__claim',
    text: `目标 Claim：${claimText}`,
    title: claimText,
  }));

  const footer = el('div', { class: 'result-card__footer' });
  const sources = [...new Set((rec.sources || []).map((source) => libraryDisplayName(source)).filter(Boolean))];
  footer.append(el('span', { class: 'result-card__source', text: sources.length ? sources.join(' · ') : '来源未返回' }));
  if (rec.evidence?.length) footer.append(el('span', { text: `证据 ${rec.evidence.length} 条 · ${evidenceScope(rec.evidence)}` }));
  footer.append(el('span', { text: `结果 v${rec.result_version ?? '—'}` }));
  content.append(footer);
  card.append(content);

  const actions = el('div', { class: 'result-card__actions' });
  if (rec.citable && !rec.revoked && !late) {
    actions.append(el('button', {
      type: 'button',
      class: 'copy-button',
      dataset: { action: 'copy-citation', resultId: rec.result_id },
      title: '选择引用格式',
      'aria-label': `复制《${resultTitle(rec)}》的引用信息`,
    }, icon('clipboard'), el('span', { text: '复制引用信息' })));
  }
  actions.append(el('a', {
    class: 'detail-link',
    href: `#result=${encodeURIComponent(rec.result_id)}`,
    dataset: { action: 'open-result', resultId: rec.result_id },
    text: '证据详情',
  }, icon('arrow-right')));
  if (rec.citable && !rec.revoked && !late) {
    actions.append(el('button', {
      type: 'button',
      class: 'citation-button',
      dataset: { action: 'citation-text', resultId: rec.result_id },
      title: '查看有原文依据的引用建议',
      'aria-label': `查看引用建议：${resultTitle(rec)}`,
    }, icon('book-open'), el('span', { text: '引用建议' })));
  }
  card.append(actions);

  if (rec.revoked) {
    const reason = label(REVOKE_REASON, rec.revoked.reason);
    card.append(el('p', {
      class: 'result-card__revoked',
      text: `已撤销：${reason}。不再提供引用入口；已有快照内容不变，导出闸门仍会阻断。`,
    }));
  }
  return card;
}

function rankLabel(rec) {
  if (rec.revoked) return 'REVOKED';
  if (rec.rankable && Number.isFinite(rec.current_rank)) return `RANK ${String(rec.current_rank).padStart(2, '0')}`;
  if (!rec.rankable) return 'EVIDENCE';
  return 'PENDING';
}
