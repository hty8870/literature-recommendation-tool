import { el, reconcileChildren, setHidden, setText } from './dom.js';
import { icon } from './icons.js';

function publicationBadge(candidate) {
  const types = candidate.publication_types || [];
  if (candidate.publication_status === 'retracted') return ['已撤稿', 'badge badge--revoked'];
  if (types.some((type) => /correction|erratum|retraction/i.test(type))) {
    return ['更正/撤稿声明', 'badge badge--pending'];
  }
  if (types.some((type) => /review/i.test(type))) return ['综述', 'badge'];
  if (candidate.publication_status === 'corrected') return ['已有更正', 'badge badge--pending'];
  return ['研究论文', 'badge'];
}

export function candidateRankChange(candidate) {
  if (candidate.ranking_status !== 'ranked' || !Number.isFinite(candidate.rank_delta)) return '';
  const delta = candidate.rank_delta;
  return delta === 0 ? '与基础排名一致' : `较基础排名 ${delta > 0 ? '↑' : '↓'}${Math.abs(delta)}`;
}

export function candidateCardSignature(candidate) {
  return JSON.stringify([
    candidate.result_id, candidate.title, candidate.authors, candidate.year, candidate.doi,
    candidate.source_uri, candidate.source_uris, candidate.library_ids, candidate.source_records,
    candidate.publication_status, candidate.publication_types, candidate.related_dois,
    candidate.evidence_excerpt, candidate.match_annotations, candidate.reason_evidence_quote,
    candidate.candidate_score, candidate.score_status, candidate.ranking_status,
    candidate.llm_rank, candidate.rank_delta,
    candidate.reason && [candidate.reason.status, candidate.reason.text, candidate.reason.failure_code],
  ]);
}

function reasonText(candidate) {
  if (candidate.reason?.status === 'generated' && candidate.reason.text) return candidate.reason.text;
  if (candidate.reason) return `模型未能生成推荐理由（${candidate.reason.failure_code || '未知错误'}）。`;
  if (candidate.ranking_status === 'pending') return '等待 LLM 排名与推荐理由。';
  if (candidate.ranking_status === 'ineligible') return '该记录仅作来源诊断，不参与推荐排名。';
  return '未生成推荐理由。';
}

function secureUrl(url) {
  return typeof url === 'string' && url.startsWith('https://') ? url : null;
}

export class CandidateCard {
  constructor(candidate, index, libraryNames = new Map(), localOnly = false) {
    this.card = el('article', { class: 'result-card candidate-card', dataset: { resultId: candidate.result_id } });
    this.rank = el('span', { class: 'result-card__rank' });
    this.movement = el('span', { class: 'result-card__scale' });
    this.title = el('a', { class: 'result-card__title', target: '_blank', rel: 'noopener noreferrer' });
    this.badge = el('span');
    this.authors = el('span');
    this.year = el('span');
    this.doi = el('span');
    this.reason = document.createTextNode('');
    this.reasonRow = el('p', { class: 'result-card__reason' }, el('span', { text: '推荐理由' }), this.reason);
    this.details = el('details', { class: 'candidate-card__abstract' });
    this.abstract = el('p');
    this.links = el('div', { class: 'candidate-card__sources' });
    this.quote = el('blockquote');
    this.matches = el('p', { class: 'field-help' });
    this.otherSources = el('div');
    this.publicationNote = el('p', { class: 'field-help', text: '撤稿状态未核查；只有题录与摘要，不是全文证据。' });
    this.corrections = el('div');
    this.sourceLinks = new Map();
    this.sourceDetails = new Map();
    this.correctionLinks = new Map();
    this.copy = el('button', {
      type: 'button', class: 'copy-button', 'aria-haspopup': 'menu',
      dataset: { action: 'copy-citation', resultId: candidate.result_id },
    }, icon('clipboard'), '复制引用信息');
    this.citation = el('button', {
      type: 'button',
      class: 'citation-button',
      dataset: { action: 'citation-text', resultId: candidate.result_id },
      title: '查看有原文依据的引用建议',
    }, icon('book-open'), '引用建议');
    this.details.append(el('summary', { text: '摘要与来源' }), this.abstract, this.links,
      this.quote, this.matches, this.otherSources, this.publicationNote, this.corrections);
    this.card.append(
      el('div', { class: 'result-card__score-rail' }, this.rank, this.movement),
      el('div', { class: 'result-card__content' },
        el('div', {}, el('h3', { class: 'result-card__heading' }, this.title),
          el('span', { class: 'result-card__badges' },
            el('span', { class: 'badge badge--evidence', text: '仅摘要' }), this.badge)),
        el('div', { class: 'result-card__meta' }, this.authors, this.year, this.doi),
        this.reasonRow, this.details),
      el('div', { class: 'result-card__actions' }, this.copy, this.citation),
    );
    setHidden(this.reasonRow, localOnly);
    setHidden(this.citation, localOnly);
    this.update(candidate, index, libraryNames);
  }

  update(candidate, index, libraryNames = new Map()) {
    const agentic = candidate.ranking_status && candidate.ranking_status !== 'disabled';
    const ranked = candidate.ranking_status === 'ranked' && Number.isFinite(candidate.llm_rank);
    const scored = candidate.score_status !== 'incomparable' && Number.isFinite(candidate.candidate_score);
    setText(this.rank, agentic
      ? ranked ? `LLM ${String(candidate.llm_rank).padStart(2, '0')}` : '待核查候选'
      : scored ? `候选 ${String(index + 1).padStart(2, '0')}` : '待核查候选');
    const movement = candidateRankChange(candidate);
    setText(this.movement, movement);
    setHidden(this.movement, !movement);
    const title = candidate.title || '题名未提供';
    setText(this.title, title);
    const uri = secureUrl(candidate.source_uri);
    if (uri) {
      if (this.title.getAttribute('href') !== uri) this.title.setAttribute('href', uri);
    } else this.title.removeAttribute('href');
    const [type, badgeClass] = publicationBadge(candidate);
    setText(this.badge, type);
    this.badge.className = badgeClass;
    for (const [node, text] of [[this.authors, candidate.authors], [this.year, candidate.year],
      [this.doi, candidate.doi ? `DOI ${candidate.doi}` : '']]) {
      setText(node, text);
      setHidden(node, !text);
    }
    const reason = reasonText(candidate);
    if (this.reason.nodeValue !== reason) this.reason.nodeValue = reason;
    setText(this.abstract, candidate.evidence_excerpt || '未提供摘要。');
    this.#updateSources(candidate, libraryNames);
    setText(this.quote, candidate.reason_evidence_quote);
    setHidden(this.quote, !candidate.reason_evidence_quote);
    const matches = [...new Set((candidate.match_annotations || []).map((match) => match.quote))];
    setText(this.matches, matches.length ? `文字命中：${matches.join('、')}` : '');
    setHidden(this.matches, !matches.length);
    setHidden(this.publicationNote, candidate.publication_status !== 'unknown');
    const corrections = (candidate.related_dois?.corrected_by || []).map((doi) => {
      let link = this.correctionLinks.get(doi);
      if (!link) {
        link = el('a', { class: 'candidate-card__relation', href: `https://doi.org/${encodeURIComponent(doi)}`,
          target: '_blank', rel: 'noopener noreferrer', text: `更正声明：${doi}` });
        this.correctionLinks.set(doi, link);
      }
      return link;
    });
    reconcileChildren(this.corrections, corrections);
    for (const [doi, node] of this.correctionLinks) {
      if (!corrections.includes(node)) this.correctionLinks.delete(doi);
    }
    const copyLabel = `复制引用信息：${title}`;
    if (this.copy.getAttribute('aria-label') !== copyLabel) this.copy.setAttribute('aria-label', copyLabel);
    const citationLabel = `查看引用建议：${title}`;
    if (this.citation.getAttribute('aria-label') !== citationLabel) this.citation.setAttribute('aria-label', citationLabel);
  }

  #updateSources(candidate, libraryNames) {
    const sources = Object.keys(candidate.source_uris || {}).length
      ? candidate.source_uris : { [candidate.library_ids?.[0] || '来源']: candidate.source_uri };
    const links = [];
    for (const [libraryId, value] of Object.entries(sources)) {
      const url = secureUrl(value);
      if (!url) continue;
      let link = this.sourceLinks.get(libraryId);
      if (!link) {
        link = el('a', { target: '_blank', rel: 'noopener noreferrer' });
        this.sourceLinks.set(libraryId, link);
      }
      if (link.getAttribute('href') !== url) link.setAttribute('href', url);
      setText(link, libraryNames.get(libraryId) || libraryId);
      links.push(link);
    }
    reconcileChildren(this.links, links);
    for (const [id, node] of this.sourceLinks) {
      if (!links.includes(node)) this.sourceLinks.delete(id);
    }
    const details = [];
    for (const source of candidate.source_records || []) {
      if (source.abstract === candidate.evidence_excerpt) continue;
      const key = `${source.library_id}:${source.paper_id}`;
      let entry = this.sourceDetails.get(key);
      if (!entry) {
        const summary = el('summary');
        const body = el('p');
        entry = { node: el('details', {}, summary, body), summary, body };
        this.sourceDetails.set(key, entry);
      }
      setText(entry.summary, `${libraryNames.get(source.library_id) || source.library_id} 的原始摘要`);
      setText(entry.body, source.abstract);
      details.push(entry.node);
    }
    reconcileChildren(this.otherSources, details);
    for (const [key, entry] of this.sourceDetails) {
      if (!details.includes(entry.node)) this.sourceDetails.delete(key);
    }
  }
}
