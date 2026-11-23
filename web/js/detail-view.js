import { clear, el } from './dom.js';
import { icon } from './icons.js';
import { buildEvidence } from './evidence-view.js';
import { formatCitationInfo, resultMetadata, resultTitle } from './citation.js';
import { recommendationReason } from './result-card.js';
import { RELATIONSHIP, label, shortHash } from './labels.js';

export function renderResultDetail(refs, rec) {
  refs.resultDialogTitle.textContent = resultTitle(rec);
  const body = refs.resultDialogBody;
  clear(body);

  const metadata = resultMetadata(rec);
  const grid = el('div', { class: 'detail-grid' });
  const claim = rec.claim && typeof rec.claim === 'object' ? rec.claim : {};
  const claimPanel = el('section', { class: 'detail-panel detail-panel--claim' });
  claimPanel.append(
    el('h3', { text: '目标 Claim / 引用位置' }),
    el('p', { class: 'detail-panel__text', text: claim.claim_text || rec.claim_text || '目标论述未返回' }),
  );
  const claimMeta = [];
  if (claim.claim_id || rec.claim_id) claimMeta.push(`Claim ${claim.claim_id || rec.claim_id}`);
  if (rec.relationship) claimMeta.push(`关系：${label(RELATIONSHIP, rec.relationship)}`);
  if (rec.rankable && Number.isFinite(rec.current_rank)) claimMeta.push(`同 Claim、同组第 ${rec.current_rank} 名`);
  claimMeta.push(`结果版本 v${rec.result_version ?? '—'}`);
  claimPanel.append(el('p', { class: 'detail-panel__reason', text: claimMeta.join(' · ') }));
  if (rec.insertion_anchor) {
    claimPanel.append(el('p', { class: 'detail-technical', text: `插入锚点：${JSON.stringify(rec.insertion_anchor)}` }));
  }
  grid.append(claimPanel);

  const sourcePanel = el('section', { class: 'detail-panel' });
  sourcePanel.append(el('h3', { text: '推荐理由与书目信息' }));
  sourcePanel.append(el('p', { class: 'detail-panel__text', text: recommendationReason(rec) }));
  if (rec.reason?.status === 'generated') {
    sourcePanel.append(el('p', {
      class: 'detail-technical',
      text: `推荐理由由 ${rec.reason.provider}/${rec.reason.served_model || rec.reason.model} 生成；不是独立关系核验。`,
    }));
  }
  const facts = [];
  if (metadata.authors.length) facts.push(metadata.authors.join('、'));
  if (metadata.year) facts.push(String(metadata.year));
  if (metadata.venue) facts.push(String(metadata.venue));
  if (metadata.doi) facts.push(`DOI ${metadata.doi}`);
  sourcePanel.append(el('p', { class: 'detail-panel__reason', text: facts.join(' · ') || '书目信息未完整返回' }));
  sourcePanel.append(el('p', { class: 'detail-panel__citation', text: formatCitationInfo(rec) }));
  const actions = el('div', { class: 'detail-panel__actions' });
  if (rec.citable && !rec.revoked) {
    actions.append(el('button', {
      type: 'button',
      class: 'copy-button',
      dataset: { action: 'copy-citation', resultId: rec.result_id },
    }, icon('clipboard'), el('span', { text: '复制引用信息' })));
  }
  const url = publicSourceUrl(rec);
  if (url) {
    actions.append(el('a', {
      class: 'detail-link',
      href: url,
      target: '_blank',
      rel: 'noopener noreferrer',
    }, icon('arrow-top-right-on-square'), el('span', { text: '打开来源原文' })));
  }
  sourcePanel.append(actions);
  if (!url) {
    sourcePanel.append(el('p', {
      class: 'detail-unavailable',
      text: rec.simulation !== false
        ? '这是模拟条目，没有可跳转的真实论文页面。下方展示已保存的模拟片段与定位，未伪造原文跳转。'
        : '当前条目没有可公开访问的原文链接；下方仍保留已保存的原文片段与定位。',
    }));
  }
  grid.append(sourcePanel);
  body.append(grid);

  const evidence = Array.isArray(rec.evidence) ? rec.evidence : [];
  const evidenceSection = el('section', { class: 'detail-evidence' });
  evidenceSection.append(el('div', { class: 'detail-evidence__heading' },
    el('h3', { text: '来源原文与定位' }),
    el('span', { text: `${evidence.length} 条证据 · 逐条核对角色、范围与坐标` }),
  ));
  if (evidence.length) evidenceSection.append(el('ul', { class: 'evidence-list' }, evidence.map(buildEvidence)));
  else evidenceSection.append(el('p', { class: 'detail-unavailable', text: '服务端未随此结果返回证据，无法完成原文核查。' }));
  body.append(evidenceSection);
  body.append(el('p', {
    class: 'detail-technical',
    text: `work ${rec.work_id || '—'} · result ${rec.result_id || '—'} · evidence ${shortHash(rec.evidence_set_hash, 18)}`,
  }));
}

function publicSourceUrl(rec) {
  const candidates = (rec.evidence || []).map((item) => item.locator?.source_uri).filter(Boolean);
  for (const candidate of candidates) {
    try {
      const url = new URL(candidate);
      if (url.protocol === 'https:' || url.protocol === 'http:') return url.href;
    } catch {
      // Unsupported or malformed source URIs remain visible as metadata, never as links.
    }
  }
  if (rec.simulation === false) {
    const doi = resultMetadata(rec).doi;
    if (doi) return `https://doi.org/${encodeURIComponent(doi)}`;
  }
  return null;
}
