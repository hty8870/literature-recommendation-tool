// 按 result_id 增量协调结果行；排名、分组与资格均来自服务端状态。
import { el, clear, reconcileChildren, setHidden, setText } from './dom.js';
import { CLAIM_TYPE, RELATIONSHIP, REVOKE_REASON, VERIFICATION, groupLabel, groupScaleNote, label, shortHash } from './labels.js';
import { candidatesForClaim, claimList, diagnosticsForClaim, groupsForClaim } from './store.js';
import { CandidateCard, candidateCardSignature } from './candidate-card.js';
import { buildCard, cardSignature } from './result-card.js';
import { buildEvidence } from './evidence-view.js';
import { resultTitle } from './citation.js';

export function preserveResultScroll(root, pane) {
  const insidePane = pane?.scrollTop > 0;
  if (!insidePane && window.scrollY <= 0) return null;
  const viewport = insidePane ? pane.getBoundingClientRect() : { top: 0, bottom: window.innerHeight };
  for (const card of root.querySelectorAll('.result-card')) {
    const rect = card.getBoundingClientRect();
    if (rect.bottom <= viewport.top || rect.top >= viewport.bottom) continue;
    return () => {
      if (!card.isConnected) return;
      const delta = card.getBoundingClientRect().top - rect.top;
      if (Math.abs(delta) > 0.5) (insidePane ? pane : window).scrollBy({ top: delta, behavior: 'instant' });
    };
  }
  return null;
}

export class ResultsView {
  constructor({ root, note }) {
    this.root = root;
    this.pane = root.closest('.results-panel');
    this.note = note;
    this.claimNodes = new Map();
    this.groupNodes = new Map();
    this.cardNodes = new Map();
    this.lateNodes = new Map();
    this.lateSignature = '';
    this.empty = el('div', { class: 'empty-state' });
    this.emptyTitle = el('strong');
    this.emptyBody = el('span');
    this.empty.append(this.emptyTitle, this.emptyBody);
    this.claimsWrap = el('div', { class: 'claims' });
    this.lateSection = el('section', { class: 'claim-block late-block' });
    setHidden(this.lateSection, true);
    this.root.replaceChildren(this.empty, this.claimsWrap, this.lateSection);
  }

  reset() {
    this.claimNodes.clear();
    this.groupNodes.clear();
    this.cardNodes.clear();
    this.lateNodes.clear();
    this.lateSignature = '';
    this.claimsWrap.replaceChildren();
    this.lateSection.replaceChildren();
    setHidden(this.lateSection, true);
    this.emptyTitle.textContent = '';
    this.emptyBody.textContent = '';
    if (this.pane) this.pane.scrollTop = 0;
  }

  update(store, agentRun = null) {
    const restoreScroll = preserveResultScroll(this.root, this.pane);
    const liveClaims = [];
    const real = store.simulation === false;
    for (const claim of claimList(store)) {
      const groups = real ? [] : groupsForClaim(store, claim.claim_id);
      const diagnostics = real ? [] : diagnosticsForClaim(store, claim.claim_id);
      const candidates = real ? candidatesForClaim(store, claim.claim_id) : [];
      if (groups.length || diagnostics.length || candidates.length) {
        liveClaims.push({ claim, groups, diagnostics, candidates });
      }
    }
    const presentClaims = new Set(liveClaims.map((item) => item.claim.claim_id));
    for (const [claimId, node] of this.claimNodes) {
      if (!presentClaims.has(claimId)) {
        node.section.remove();
        this.claimNodes.delete(claimId);
      }
    }

    let publishedCount = 0;
    for (const { claim, groups, diagnostics, candidates } of liveClaims) {
      const node = this.#ensureClaim(claim.claim_id);
      if (!real) this.#updateClaimHead(node, claim);
      this.#updateGroups(node, claim.claim_id, groups);
      this.#updateCandidates(node, candidates, store.libraryNames, store.localOnly);
      this.#updateDiagnostics(node, diagnostics);
      publishedCount += real ? candidates.length : groups.reduce((sum, group) => sum + group.results.length, 0);
    }
    reconcileChildren(this.claimsWrap, liveClaims.map(({ claim }) => this.claimNodes.get(claim.claim_id).section));
    for (const [resultId, entry] of this.cardNodes) {
      const rec = store.results.get(resultId);
      if (!rec || !rec.citable || rec.revoked) {
        entry.card.remove();
        this.cardNodes.delete(resultId);
      }
    }
    this.#updateLate(store);
    this.#updateEmpty(store, publishedCount, agentRun);
    const note = real || !store.queryId
      ? ''
      : store.frozen
        ? '已定稿，迟到结果单列且不写入历史快照。模拟文献；模型理由不等于关系核验。'
        : '结果逐条流式发布；分数仅同 query、Claim、组内可比。模拟文献，模型理由需对照原文。';
    setText(this.note, note);
    setHidden(this.note, !note);
    restoreScroll?.();
  }

  #ensureClaim(claimId) {
    let node = this.claimNodes.get(claimId);
    if (node) return node;
    const title = el('p', { class: 'claim-block__title' });
    const meta = el('div', { class: 'claim-block__meta' });
    const head = el('header', { class: 'claim-block__head' }, title, meta);
    const groupsWrap = el('div', { class: 'claim-groups' });
    const candidateWrap = el('div', { class: 'candidate-list' });
    const diagWrap = el('div', { class: 'diagnostics' });
    const section = el('section', { class: 'claim-block' }, groupsWrap, candidateWrap, diagWrap);
    section.dataset.claimId = claimId;
    node = { section, title, meta, groupsWrap, candidateWrap, diagWrap, diagSignature: null,
      candidateCards: new Map(), candidateNote: null };
    this.claimNodes.set(claimId, node);
    return node;
  }

  #updateClaimHead(node, claim) {
    const claimText = claim.claim_text || '（Claim 文本未返回）';
    if (node.title.textContent !== claimText) node.title.textContent = claimText;
    const metaParts = [`Claim ${claim.claim_id || '—'}`];
    if (claim.claim_type) metaParts.push(label(CLAIM_TYPE, claim.claim_type));
    if (Array.isArray(claim.topic_context) && claim.topic_context.length) metaParts.push(`主题：${claim.topic_context.join('、')}`);
    const signature = metaParts.join('|');
    if (node.metaSignature !== signature) {
      node.metaSignature = signature;
      clear(node.meta);
      for (const part of metaParts) node.meta.append(el('span', { text: part }));
    }
  }

  #updateGroups(node, claimId, groups) {
    const seenGroups = new Set();
    for (const { group, results } of groups) {
      seenGroups.add(group);
      const groupNode = this.#ensureGroup(claimId, group);
      setText(groupNode.name, groupLabel(group));
      setText(groupNode.scale, groupScaleNote(group, results));
      setText(groupNode.count, `${results.length} 条`);
      const seenCards = new Set();
      const cards = [];
      for (const rec of results) {
        seenCards.add(rec.result_id);
        const entry = this.#ensureCard(rec);
        cards.push(entry.card);
      }
      reconcileChildren(groupNode.list, cards);
      for (const [resultId, entry] of this.cardNodes) {
        if (entry.groupKey === `${claimId}::${group}` && !seenCards.has(resultId)) {
          entry.card.remove();
          this.cardNodes.delete(resultId);
        }
      }
    }
    reconcileChildren(node.groupsWrap, groups.map(({ group }) => this.groupNodes.get(`${claimId}::${group}`).container));
    for (const [key, groupNode] of this.groupNodes) {
      if (!key.startsWith(`${claimId}::`)) continue;
      const group = key.slice(claimId.length + 2);
      if (!seenGroups.has(group)) {
        groupNode.container.remove();
        this.groupNodes.delete(key);
      }
    }
  }

  #ensureGroup(claimId, group) {
    const key = `${claimId}::${group}`;
    let node = this.groupNodes.get(key);
    if (node) return node;
    const name = el('span', { class: 'group__name' });
    const scale = el('span', { class: 'group__scale' });
    const count = el('span', { class: 'group__scale' });
    const head = el('div', { class: 'group__head' }, name, scale, count);
    const list = el('div', { class: 'group__list' });
    const container = el('div', { class: 'group' }, head, list);
    node = { container, name, scale, count, list };
    this.groupNodes.set(key, node);
    return node;
  }

  #ensureCard(rec) {
    const signature = cardSignature(rec);
    const groupKey = `${rec.claim_id}::${displayGroup(rec)}`;
    const existing = this.cardNodes.get(rec.result_id);
    if (existing && existing.signature === signature) {
      existing.groupKey = groupKey;
      return existing;
    }
    const card = buildCard(rec);
    if (existing) existing.card.replaceWith(card);
    const entry = { card, signature, groupKey };
    this.cardNodes.set(rec.result_id, entry);
    return entry;
  }

  #updateCandidates(node, candidates, libraryNames, localOnly) {
    const seen = new Set();
    const children = [];
    const appendCandidate = (candidate, index) => {
      const signature = JSON.stringify([candidateCardSignature(candidate), index,
        candidate.library_ids?.map((id) => [id, libraryNames.get(id)])]);
      let entry = node.candidateCards.get(candidate.result_id);
      if (!entry) {
        entry = { view: new CandidateCard(candidate, index, libraryNames, localOnly), signature };
        node.candidateCards.set(candidate.result_id, entry);
      } else if (entry.signature !== signature) {
        entry.view.update(candidate, index, libraryNames);
        entry.signature = signature;
      }
      seen.add(candidate.result_id);
      children.push(entry.view.card);
    };
    const agentic = candidates.some((candidate) => candidate.ranking_status && candidate.ranking_status !== 'disabled');
    const ranked = agentic ? candidates : candidates.filter((candidate) => candidate.score_status !== 'incomparable'
      && typeof candidate.candidate_score === 'number' && Number.isFinite(candidate.candidate_score));
    const unranked = agentic ? [] : candidates.filter((candidate) => !ranked.includes(candidate));
    ranked.forEach((candidate, index) => appendCandidate(candidate, agentic ? 0 : index));
    if (unranked.length) {
      node.candidateNote ||= el('p', { class: 'candidate-list__note', text: '未统一评分，不参与候选排序' });
      children.push(node.candidateNote);
      unranked.forEach((candidate) => appendCandidate(candidate, 0));
    }
    reconcileChildren(node.candidateWrap, children);
    for (const id of node.candidateCards.keys()) {
      if (!seen.has(id)) node.candidateCards.delete(id);
    }
  }

  #updateDiagnostics(node, items) {
    const signature = items.map((item) => {
      if (item.kind === 'rejected') return `${item.kind}:${item.rejected.result_id}:${item.rejected.reason_code || ''}`;
      if (item.kind === 'post-final-revoked') return `${item.kind}:${item.revoked.result_id}:${item.revoked.reason || ''}`;
      const rec = item.rec;
      return `${item.kind}:${rec.result_id}:${rec.result_version}:${rec.revoked ? rec.revoked.notice_version || rec.revoked.reason : ''}`;
    }).join('|');
    if (node.diagSignature === signature) return;
    node.diagSignature = signature;
    clear(node.diagWrap);
    if (!items.length) {
      setHidden(node.diagWrap, true);
      return;
    }
    setHidden(node.diagWrap, false);
    node.diagWrap.append(el('div', { class: 'diagnostics__head' },
      el('h3', { text: '诊断与不可引用项' }),
      el('span', { class: 'diagnostics__hint', text: '未通过引用资格；不参与排序，也不提供引用入口。' }),
    ));
    for (const item of items) node.diagWrap.append(buildDiagnostic(item));
  }

  #updateLate(store) {
    const records = [...store.lateResults.values()].sort((a, b) => {
      const claimA = String(a.claim_id || '');
      const claimB = String(b.claim_id || '');
      return claimA === claimB ? String(a.result_id).localeCompare(String(b.result_id)) : claimA.localeCompare(claimB);
    });
    const signature = records.map((rec) => `${rec.result_id}:${rec.result_version}`).join('|');
    if (signature === this.lateSignature) return;
    this.lateSignature = signature;
    if (!records.length) {
      setHidden(this.lateSection, true);
      clear(this.lateSection);
      this.lateNodes.clear();
      return;
    }
    const head = el('header', { class: 'claim-block__head' },
      el('p', { class: 'claim-block__title', text: '定稿后迟到结果（未进入最终快照）' }),
      el('div', { class: 'claim-block__meta', text: `共 ${records.length} 条；不写入任何快照，也不产生引用入口。` }),
    );
    const list = el('div', { class: 'group__list' });
    for (const rec of records) {
      let entry = this.lateNodes.get(rec.result_id);
      if (!entry || entry.signature !== cardSignature(rec)) {
        entry = { card: buildCard(rec, { late: true }), signature: cardSignature(rec) };
        this.lateNodes.set(rec.result_id, entry);
      }
      list.append(entry.card);
    }
    for (const [resultId, entry] of this.lateNodes) {
      if (!records.some((rec) => rec.result_id === resultId)) {
        entry.card.remove();
        this.lateNodes.delete(resultId);
      }
    }
    this.lateSection.replaceChildren(head, list);
    setHidden(this.lateSection, false);
  }

  #updateEmpty(store, publishedCount, agentRun) {
    let title = '';
    let body = '';
    if (!store.queryId) {
      if (!agentRun) {
        title = '正在启动本轮';
        body = '回复与进度会出现在左侧对话；这里不会提前生成空榜单。';
      } else if (!['completed', 'failed', 'cancelled'].includes(agentRun.status)) {
        title = 'Agent 正在判断是否需要检索';
        body = '若本轮不需要检索，回复只出现在左侧对话；这里不会生成空榜单。';
      } else {
        title = '本轮未发起检索';
        body = '回复见左侧对话；没有生成可排序的榜单。';
      }
    } else if (store.connection === 'error') {
      title = '事件连接中断，已提交结果仍保留';
      body = '请按页面提示重连；恢复前不要将当前列表视为最终快照。';
    } else if (publishedCount === 0) {
      if (store.simulation === false) {
        title = store.terminal ? '本次没有找到符合条件的候选' : '正在检索真实题录与摘要';
        body = store.terminal
          ? '可尝试更换研究主题或关键词；不声称库中绝对没有相关研究。'
          : '候选将逐条出现；摘要仅供核查，不是全文可引用证据。';
      } else if (store.frozen) {
        title = '最终快照中没有可引用结果';
        body = '这不等于检索失败。诊断项与终止原因已在页面中单独保留。';
      } else if (store.terminal) {
        title = '本次没有通过核验的可引用结果';
        body = '证据不足或未通过资格的候选只进入诊断区，不会为凑数生成推荐。';
      } else {
        title = '正在等待第一条合格推荐';
        body = 'Worker 正在检索与核验。先完成的数据源会先发布结果，慢库不会形成首结果屏障。';
      }
    }
    setText(this.emptyTitle, title);
    setText(this.emptyBody, body);
    setHidden(this.empty, !title);
  }
}

function displayGroup(rec) {
  if (!rec.citable) return 'uncitable';
  if (!rec.rankable) return 'evidence_only';
  return rec.rankable_group || 'evidence_only';
}

function buildDiagnostic(item) {
  if (item.kind === 'rejected') {
    const rejected = item.rejected;
    return el('div', { class: 'diag-item' },
      el('span', { class: 'diag-item__title', text: `核验未通过 · ${label(VERIFICATION, rejected.verification_status)}` }),
      el('span', { class: 'diag-item__meta', text: rejected.reason_code || 'reason_code 未返回' }),
      el('span', { class: 'diag-item__meta', text: `result ${shortHash(rejected.result_id, 24)}` }),
    );
  }
  if (item.kind === 'post-final-revoked') {
    const revoked = item.revoked;
    return el('div', { class: 'diag-item' },
      el('span', { class: 'diag-item__title', text: '定稿后撤销（快照不变）' }),
      el('span', { class: 'diag-item__meta', text: `${label(REVOKE_REASON, revoked.reason)} · notice ${revoked.notice_version || '—'}` }),
      el('span', { class: 'diag-item__meta', text: `result ${shortHash(revoked.result_id, 24)}` }),
    );
  }
  const rec = item.rec;
  const container = el('div', { class: 'diag-item' },
    el('span', { class: 'diag-item__title', text: resultTitle(rec) }),
    el('span', {
      class: 'diag-item__meta',
      text: rec.revoked
        ? `已撤销：${label(REVOKE_REASON, rec.revoked.reason)}`
        : `citable=false · 核验 ${label(VERIFICATION, rec.verification_status)} · 关系 ${label(RELATIONSHIP, rec.relationship)}`,
    }),
  );
  const evidence = Array.isArray(rec.evidence) ? rec.evidence : [];
  if (evidence.length) {
    const details = el('details');
    details.append(el('summary', { text: `查看诊断证据（${evidence.length} 条）` }), el('ul', { class: 'evidence-list' }, evidence.map(buildEvidence)));
    container.append(details);
  }
  return container;
}
