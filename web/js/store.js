// 搜索会话的前端投影。只保存服务端已提交状态：不做乐观合并、不猜测排名。
// 事件按 event_type 派发；结果以 result_id 为键；Worker 以 (library_id, attempt) 为键。

import { SEARCH_STATE } from './labels.js';

const TERMINAL_STATES = new Set(['completed', 'partial', 'failed', 'cancelled']);
const MAX_EVENTS = 120;
const MAX_NOTICES = 8;
const LOCAL_ONLY_ADAPTER_MODE = 'real_abstract_metadata_local_only';

export class Store {
  constructor() {
    this.reset();
  }

  reset() {
    this.queryId = null;
    this.session = null;
    this.claims = new Map();
    this.results = new Map();
    this.candidates = new Map();
    this.candidateRankings = new Map();
    this.libraryNames = new Map();
    this.simulation = null;
    this.localOnly = false;
    this.lateResults = new Map();
    this.workers = new Map();
    this.workerIndex = new Map();
    this.rejected = new Map();
    this.revokedAfterFinal = new Map();
    this.groupOrder = new Map();
    this.eventIds = new Set();
    this.events = [];
    this.notices = [];
    this.finalizations = [];
    this.snapshots = [];
    this.snapshotCache = new Map();
    this.lastSequence = null;
    this.asOfSequence = null;
    this.coverage = null;
    this.isLive = true;
    this.frozen = false;
    this.finalSnapshot = null;
    this.connection = 'idle';
    this.createdAt = null;
    this.deadlineMs = null;
  }

  get searchState() {
    return (this.session && this.session.search_state) || null;
  }

  get terminal() {
    return TERMINAL_STATES.has(this.searchState);
  }

  attachCreateResponse(data) {
    this.queryId = data.query_id || null;
    const session = data.session && typeof data.session === 'object' ? { ...data.session } : {};
    if (typeof data.search_state === 'string') session.search_state = data.search_state;
    else if (data.search_state && typeof data.search_state === 'object') Object.assign(session, data.search_state);
    session.query_id = session.query_id || this.queryId;
    this.session = session;
    this.simulation = data.simulation ?? null;
    this.localOnly = data.adapter_mode === LOCAL_ONLY_ADAPTER_MODE;
    for (const claim of data.claims || []) {
      if (claim && claim.claim_id) this.claims.set(claim.claim_id, { ...claim });
    }
    this.createdAt = Date.now();
  }

  seedPendingWorkers(libraryIds, libraries = []) {
    const names = new Map(libraries.map((library) => [library.library_id, library.display_name]));
    for (const libraryId of libraryIds || []) {
      const key = `${libraryId}#1`;
      if (this.workers.has(key)) continue;
      this.workers.set(key, {
        library_id: libraryId,
        display_name: names.get(libraryId) || libraryId,
        attempt: 1,
        worker_state: 'pending',
        placeholder: true,
        candidates_seen: 0,
        candidates_verified: 0,
        recommendations_emitted: 0,
      });
    }
  }

  addNotice(text, { level = 'info', key = null } = {}) {
    if (!text || (key === 'final-snapshot' && this.simulation === false)) return;
    if (key) {
      const existing = this.notices.find((notice) => notice.key === key);
      if (existing) {
        existing.text = text;
        existing.level = level;
        return;
      }
    }
    this.notices.push({ text, level, key });
    if (this.notices.length > MAX_NOTICES) this.notices.splice(0, this.notices.length - MAX_NOTICES);
  }

  detectGap(sequence) {
    if (!Number.isFinite(sequence)) return false;
    if (this.lastSequence == null) return false;
    return sequence > this.lastSequence + 1;
  }

  applyEvent(envelope) {
    if (!envelope || typeof envelope !== 'object') return { changed: false };
    const sequence = Number(envelope.sequence);
    if (Number.isFinite(sequence) && this.lastSequence !== null && sequence <= this.lastSequence) {
      return { changed: false, duplicate: true };
    }
    const eventId = envelope.event_id;
    if (eventId) {
      if (this.eventIds.has(eventId)) return { changed: false, duplicate: true };
      this.eventIds.add(eventId);
      if (this.eventIds.size > 4000) {
        const oldest = this.eventIds.values().next().value;
        this.eventIds.delete(oldest);
      }
    }

    if (Number.isFinite(sequence)) this.lastSequence = sequence;

    const type = envelope.event_type || 'unknown';
    const payload = envelope.payload && typeof envelope.payload === 'object' ? envelope.payload : {};
    this.events.push({
      sequence: Number.isFinite(sequence) ? sequence : null,
      type,
      occurred_at: envelope.occurred_at || null,
    });
    if (this.events.length > MAX_EVENTS) this.events.splice(0, this.events.length - MAX_EVENTS);

    switch (type) {
      case 'recommendation_created':
        this.#onCreated(payload);
        break;
      case 'candidate_diagnostic':
      case 'candidate_provenance_updated':
        this.#onCandidate(payload.candidate);
        break;
      case 'candidate_pool_updated':
      case 'candidate_reranked':
        this.#onCandidatePool(payload);
        break;
      case 'candidate_ranking_progress':
        this.#onRankingProgress(payload);
        break;
      case 'recommendation_merged':
        this.#onMerged(payload);
        break;
      case 'recommendation_reranked':
        this.#onReranked(payload);
        break;
      case 'recommendation_rejected':
        this.#onRejected(payload);
        break;
      case 'recommendation_revoked':
        this.#onRevoked(payload);
        break;
      case 'worker_progress':
      case 'worker_completed':
      case 'worker_failed':
      case 'worker_timed_out':
      case 'worker_cancelled':
        this.#onWorker(payload.worker);
        break;
      case 'search_state_changed':
        this.#onSearchState(payload);
        break;
      case 'search_finalized':
        this.#onFinalized(payload);
        break;
      default:
        this.addNotice(`收到未识别事件类型：${type}`, { level: 'warn', key: `unknown-${type}` });
    }

    return { changed: true, type };
  }

  #onCreated(payload) {
    const raw = payload.recommendation;
    if (!raw || !raw.result_id) return;
    const target = this.frozen ? this.lateResults : this.results;
    if (this.frozen) {
      this.addNotice(
        `定稿后收到迟到结果 ${shortId(raw.result_id)}；未写入任何已保存快照。`,
        { level: 'warn', key: 'late-result' },
      );
    }
    this.#upsert(raw, target, payload.current_rank, payload.rankable_group);
  }

  #onCandidatePool(payload) {
    const ranking = payload.ranking;
    if (!ranking?.claim_id) return;
    const previous = this.candidateRankings.get(ranking.claim_id);
    if (previous && (ranking.pool_version < previous.pool_version
      || (ranking.pool_version === previous.pool_version
      && ranking.ranking_version < previous.ranking_version))) return;
    this.candidateRankings.set(ranking.claim_id, { ...ranking });
    for (const [id, candidate] of this.candidates) {
      if (candidate.claim_id === ranking.claim_id) this.candidates.delete(id);
    }
    for (const candidate of payload.candidates || []) this.#onCandidate(candidate);
  }

  #onRankingProgress({ claim_id: claimId, pool_version: version, phase }) {
    const ranking = this.candidateRankings.get(claimId);
    if (this.terminal || ranking?.status !== 'pending' || ranking.pool_version !== version) return;
    const expected = phase === 'blind' ? null : phase === 'score_review' ? 'blind' : undefined;
    if ((ranking.phase ?? null) !== expected) return;
    this.candidateRankings.set(claimId, { ...ranking, phase });
  }

  #onCandidate(raw) {
    if (!raw?.result_id || !raw.claim_id) return;
    if (raw.ranking_status && raw.ranking_status !== 'disabled') {
      const previous = this.candidates.get(raw.result_id);
      if (previous && (raw.pool_version < previous.pool_version
        || (raw.pool_version === previous.pool_version
        && raw.ranking_version < previous.ranking_version))) return;
      this.candidates.set(raw.result_id, { ...raw });
      return;
    }
    const previous = this.candidates.get(raw.result_id);
    const merged = { ...(previous || {}), ...raw };
    if (previous) {
      merged.library_ids = [...new Set([...previous.library_ids || [], ...raw.library_ids || []])].sort();
      merged.source_uris = { ...previous.source_uris, ...raw.source_uris };
      merged.candidate_score = Math.max(previous.candidate_score || 0, raw.candidate_score || 0);
      if (previous.score_status === 'incomparable' || raw.score_status === 'incomparable'
        || (previous.candidate_score != null && raw.candidate_score != null
        && (previous.score_version !== raw.score_version || previous.score_scale_group !== raw.score_scale_group))) {
        merged.candidate_score = null;
        merged.score_version = null;
        merged.score_scale_group = null;
        merged.score_status = 'incomparable';
      }
      if (merged.score_status === 'failed' && previous.score_status === 'scoped'
        && Number.isFinite(previous.candidate_score)) {
        merged.candidate_score = previous.candidate_score;
        merged.score_version = previous.score_version;
        merged.score_scale_group = previous.score_scale_group;
        merged.score_status = 'scoped';
      } else if (merged.score_status && merged.score_status !== 'scoped') {
        merged.candidate_score = null;
        merged.score_version = null;
        merged.score_scale_group = null;
      }
      const sourceScores = [...previous.retrieval_scores || [], ...raw.retrieval_scores || []];
      merged.retrieval_scores = [...new Map(sourceScores.map((score) => [
        `${score.library_id}:${score.score_version}:${score.score_scale_group}`, score,
      ])).values()].sort((a, b) => a.library_id.localeCompare(b.library_id)
        || a.score_version.localeCompare(b.score_version)
        || a.score_scale_group.localeCompare(b.score_scale_group));
      if (previous.reason?.status === 'generated' && raw.reason?.status !== 'generated') {
        merged.reason = previous.reason;
      }
      if (previous.publication_status === 'retracted' || raw.publication_status === 'retracted') {
        merged.publication_status = 'retracted';
      }
    }
    this.candidates.set(raw.result_id, merged);
  }

  #onMerged(payload) {
    const raw = payload.recommendation;
    if (!raw || !raw.result_id) return;
    const merged = Array.isArray(payload.merged_result_ids) ? payload.merged_result_ids : [];
    for (const id of merged) {
      if (id === raw.result_id) continue;
      this.results.delete(id);
      this.lateResults.delete(id);
    }
    const target = this.frozen ? this.lateResults : this.results;
    this.#upsert(raw, target, payload.current_rank, raw.rankable_group);
  }

  #onReranked(payload) {
    const changes = Array.isArray(payload.changes) ? payload.changes : [];
    if (!changes.length) return;
    for (const change of changes) {
      if (!change || !change.result_id) continue;
      const target = this.frozen ? this.lateResults : this.results;
      const rec = target.get(change.result_id);
      if (!rec) continue;
      if (
        Number.isFinite(change.result_version) &&
        Number.isFinite(rec.result_version) &&
        change.result_version < rec.result_version
      ) {
        continue;
      }
      if (Number.isFinite(change.current_rank)) rec.current_rank = change.current_rank;
      if (typeof change.previous_rank === 'number') rec.previous_rank = change.previous_rank;
      if (change.rankable_group) {
        rec.rankable_group = change.rankable_group;
        this.#noteGroup(rec.claim_id, displayGroup(rec));
      }
      rec.rankChanged = true;
    }
  }

  #onRejected(payload) {
    if (!payload || !payload.result_id) return;
    this.rejected.set(payload.result_id, {
      result_id: payload.result_id,
      work_id: payload.work_id || null,
      claim_id: payload.claim_id || null,
      verification_status: payload.verification_status || null,
      reason_code: payload.reason_code || null,
      received_at: new Date().toISOString(),
    });
    if (!this.frozen) this.results.delete(payload.result_id);
  }

  #onRevoked(payload) {
    if (!payload || !payload.result_id) return;
    if (this.frozen) {
      this.revokedAfterFinal.set(payload.result_id, { ...payload });
      this.addNotice(
        `定稿后收到撤销：${shortId(payload.result_id)}（${payload.reason || '原因未提供'}）。快照内容不变，导出闸门会阻断。`,
        { level: 'warn', key: `revoked-${payload.result_id}` },
      );
      return;
    }
    const rec = this.results.get(payload.result_id) || this.lateResults.get(payload.result_id);
    if (rec) {
      rec.revoked = {
        reason: payload.reason || 'unknown',
        notice_version: payload.notice_version || null,
        revoked_at: payload.revoked_at || null,
        current_rank: payload.current_rank ?? null,
      };
      rec.citable = false;
      rec.rankable = false;
    } else {
      this.revokedAfterFinal.set(payload.result_id, { ...payload, unloaded: true });
    }
  }

  #onWorker(worker) {
    if (!worker || typeof worker !== 'object') return;
    const libraryId = worker.library_id || worker.lane_id || 'unknown';
    const attempt = Number.isFinite(worker.attempt) ? worker.attempt : 1;
    const key = `${libraryId}#${attempt}`;
    const previous = this.workers.get(key);
    if (previous?.updated_at && worker.updated_at && worker.updated_at < previous.updated_at) return;
    if (previous && ['completed', 'failed', 'timed_out', 'cancelled'].includes(previous.worker_state)
      && worker.worker_state !== previous.worker_state) return;
    const merged = { ...(previous || {}), ...worker, attempt, placeholder: false };
    if (previous) {
      for (const field of ['candidates_seen', 'candidates_verified', 'recommendations_emitted', 'source_candidates_submitted']) {
        merged[field] = Math.max(previous[field] ?? 0, worker[field] ?? 0);
      }
    }
    this.workers.set(key, merged);
    if (worker.worker_id) this.workerIndex.set(worker.worker_id, key);
  }

  get completedLibraryIds() {
    return new Set(workersList(this).filter((worker) => worker.worker_state === 'completed').map((worker) => worker.library_id));
  }

  #onSearchState(payload) {
    const next = payload.current_state;
    if (!next) return;
    const session = this.session ? { ...this.session } : {};
    session.search_state = next;
    if (payload.previous_state) session.previous_state = payload.previous_state;
    this.session = session;
  }

  #onFinalized(payload) {
    if (!payload || !payload.snapshot_id) return;
    const manifest = Object.freeze({
      snapshot_id: payload.snapshot_id,
      snapshot_kind: payload.snapshot_kind || 'final',
      coverage: payload.coverage || null,
      termination_reason: payload.termination_reason || null,
      immutable_hash: payload.immutable_hash || null,
      applied_through_sequence: payload.applied_through_sequence ?? null,
      event_set_hash: payload.event_set_hash || null,
      received_at: new Date().toISOString(),
    });
    const wasFrozen = this.frozen;
    const registered = this.addSnapshot({ ...manifest, created_at: manifest.received_at });
    this.finalizations.push(registered || manifest);
    if (payload.coverage) this.coverage = payload.coverage;
    if (manifest.snapshot_kind === 'final') {
      if (!wasFrozen) {
        this.frozen = true;
        this.finalSnapshot = registered || manifest;
        this.addNotice(
          `已生成最终快照 ${shortId(manifest.snapshot_id)}；主列表冻结，定稿后结果不会写入该快照。`,
          { level: 'info', key: 'final-snapshot' },
        );
      }
    } else if (manifest.snapshot_kind === 'supplemental') {
      this.addNotice(
        `收到补充快照 ${shortId(manifest.snapshot_id)}；只追加，不改写前一快照。`,
        { level: 'info', key: 'supplemental' },
      );
    }
  }

  #upsert(raw, target, rank, group) {
    if (!raw || !raw.result_id) return null;
    const existing = target.get(raw.result_id);
    if (
      existing &&
      Number.isFinite(raw.result_version) &&
      Number.isFinite(existing.result_version) &&
      raw.result_version < existing.result_version
    ) {
      return null;
    }
    const rec = normalizeRecommendation(raw, existing);
    if (Number.isFinite(rank)) rec.current_rank = rank;
    if (group) rec.rankable_group = group;
    target.set(rec.result_id, rec);
    if (rec.claim && rec.claim.claim_id) {
      this.claims.set(rec.claim.claim_id, rec.claim);
      this.#noteGroup(rec.claim_id, displayGroup(rec));
    }
    return rec;
  }

  #noteGroup(claimId, group) {
    if (!claimId || !group) return;
    if (!this.groupOrder.has(claimId)) this.groupOrder.set(claimId, []);
    const order = this.groupOrder.get(claimId);
    if (!order.includes(group)) order.push(group);
  }

  applyState(payload) {
    const asOf = payload.as_of_sequence ?? payload.latest_sequence;
    if (Number.isSafeInteger(asOf) && this.lastSequence !== null && asOf < this.lastSequence) return false;

    const session = payload.session && typeof payload.session === 'object' ? { ...payload.session } : null;
    this.session = session;
    this.results.clear();
    this.candidates.clear();
    this.candidateRankings.clear();
    this.lateResults.clear();
    this.rejected.clear();
    this.revokedAfterFinal.clear();
    this.workers.clear();
    this.workerIndex.clear();
    this.groupOrder.clear();

    this.coverage = payload.coverage || (session && session.coverage) || null;
    this.isLive = payload.is_live !== false;

    for (const raw of flattenResults(payload.results)) {
      this.#upsert(raw, this.results, null, raw.rankable_group);
    }
    for (const candidate of payload.candidate_diagnostics || []) this.#onCandidate(candidate);
    for (const ranking of payload.candidate_rankings || []) {
      this.candidateRankings.set(ranking.claim_id, { ...ranking });
    }
    this.simulation = payload.adapter_mode?.startsWith('real_') ? false : this.simulation;
    if (payload.adapter_mode) this.localOnly = payload.adapter_mode === LOCAL_ONLY_ADAPTER_MODE;
    this.libraryNames = new Map((payload.libraries || []).map((library) => [library.library_id, library.display_name]));
    const libraryNames = this.libraryNames;
    for (const worker of payload.workers || []) {
      this.#onWorker({ ...worker, display_name: libraryNames.get(worker.library_id) || worker.library_id });
    }
    this.#applyGroups(payload.groups);

    this.asOfSequence = Number.isSafeInteger(asOf) ? asOf : this.lastSequence;
    if (this.asOfSequence !== null) this.lastSequence = this.asOfSequence;
    if (session && session.final_snapshot_id) {
      this.frozen = true;
      if (!this.finalSnapshot) {
        this.finalSnapshot = Object.freeze({
          snapshot_id: session.final_snapshot_id,
          snapshot_kind: 'final',
          applied_through_sequence: this.asOfSequence,
          recovered: true,
        });
      }
      if (!this.snapshots.some((item) => item.snapshot_id === session.final_snapshot_id)) {
        this.addSnapshot({
          snapshot_id: session.final_snapshot_id,
          snapshot_kind: 'final',
          applied_through_sequence: this.asOfSequence,
          recovered: true,
        });
      }
    }
    return true;
  }

  applyFinalManifest(manifest) {
    if (!manifest || !manifest.snapshot_id) return;
    this.cacheSnapshot(manifest);
    this.#applySnapshotRanks(manifest);
    if (manifest.coverage) this.coverage = manifest.coverage;
    if (manifest.claim_coverage && this.session) {
      this.session = { ...this.session, claim_coverage: manifest.claim_coverage };
    }
    const index = this.snapshots.findIndex((item) => item.snapshot_id === manifest.snapshot_id);
    if (index >= 0) {
      const previous = this.snapshots[index];
      this.snapshots[index] = Object.freeze({
        ...previous,
        ...manifest,
        stored_at: previous.stored_at || new Date().toISOString(),
      });
      if (this.finalSnapshot && this.finalSnapshot.snapshot_id === manifest.snapshot_id) {
        this.finalSnapshot = this.snapshots[index];
      }
    } else {
      this.addSnapshot(manifest);
    }
  }

  #applyGroups(groups) {
    if (!groups) return;
    if (Array.isArray(groups)) {
      for (const entry of groups) {
        if (!entry || typeof entry !== 'object') continue;
        const claimId = entry.claim_id || (entry.claim && entry.claim.claim_id);
        const group = entry.rankable_group || entry.group || entry.name;
        if (claimId && group) this.#noteGroup(claimId, group);
        const ranks = entry.results || entry.entries || entry.ranks;
        if (Array.isArray(ranks)) {
          for (const item of ranks) {
            if (!item || !item.result_id) continue;
            const rec = this.results.get(item.result_id);
            if (rec && Number.isFinite(item.rank)) rec.current_rank = item.rank;
            if (rec && item.rankable_group) rec.rankable_group = item.rankable_group;
          }
        }
      }
      return;
    }
    if (typeof groups === 'object') {
      for (const [claimId, value] of Object.entries(groups)) {
        if (!Array.isArray(value)) continue;
        for (const group of value) {
          if (typeof group === 'string') this.#noteGroup(claimId, group);
          else if (group && group.rankable_group) {
            this.#noteGroup(claimId, group.rankable_group);
            for (const item of group.results || []) {
              const rec = this.results.get(item.result_id);
              if (rec && Number.isFinite(item.rank)) rec.current_rank = item.rank;
            }
          }
        }
      }
    }
  }

  addSnapshot(manifest) {
    if (!manifest || !manifest.snapshot_id) return null;
    const existing = this.snapshots.find((snapshot) => snapshot.snapshot_id === manifest.snapshot_id);
    if (existing) return existing;
    const frozen = Object.freeze({ ...manifest, stored_at: new Date().toISOString() });
    this.snapshots.push(frozen);
    if (manifest.snapshot_kind === 'final' && !this.frozen) {
      this.frozen = true;
      this.finalSnapshot = frozen;
    }
    return frozen;
  }

  cacheSnapshot(manifest) {
    if (!manifest || !manifest.snapshot_id) return null;
    if (this.snapshotCache.has(manifest.snapshot_id)) return this.snapshotCache.get(manifest.snapshot_id);
    const frozen = deepFreeze(manifest);
    this.snapshotCache.set(manifest.snapshot_id, frozen);
    return frozen;
  }

  #applySnapshotRanks(manifest) {
    if (!Array.isArray(manifest.items)) return;
    for (const item of manifest.items) {
      const rec = this.results.get(item.result_id);
      if (!rec) continue;
      if (Number.isFinite(item.rank)) rec.current_rank = item.rank;
      if (item.rankable_group) {
        rec.rankable_group = item.rankable_group;
        this.#noteGroup(rec.claim_id, item.rankable_group);
      }
      if (typeof item.display_score === 'number') rec.display_score = item.display_score;
      if (item.provisional === false) rec.provisional = false;
      if (Number.isFinite(item.result_version)) rec.result_version = item.result_version;
    }
  }

  sawSequence(sequence) {
    if (!Number.isFinite(sequence)) return false;
    this.lastSequence = Math.max(this.lastSequence ?? 0, sequence);
    return true;
  }
}

function normalizeRecommendation(raw, existing) {
  const merged = { ...(existing || {}), ...raw };
  if (raw.claim && typeof raw.claim === 'object') merged.claim = raw.claim;
  merged.claim_id = (merged.claim && merged.claim.claim_id) || raw.claim_id || (existing && existing.claim_id) || null;
  if (!Array.isArray(merged.evidence)) merged.evidence = [];
  if (!Array.isArray(merged.sources)) merged.sources = [];
  return merged;
}

function flattenResults(results) {
  if (!results) return [];
  if (Array.isArray(results)) return results;
  if (typeof results === 'object') {
    const flat = [];
    for (const value of Object.values(results)) {
      if (Array.isArray(value)) flat.push(...value);
    }
    return flat;
  }
  return [];
}

function deepFreeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.freeze(value);
    for (const child of Object.values(value)) deepFreeze(child);
  }
  return value;
}

function displayGroup(rec) {
  if (!rec) return null;
  if (!rec.citable) return null;
  if (!rec.rankable) return 'evidence_only';
  return rec.rankable_group || 'evidence_only';
}

function shortId(id) {
  const text = String(id);
  return text.length > 18 ? `${text.slice(0, 18)}…` : text;
}

/* ---------- 选择器 ---------- */

export function claimList(store) {
  const claims = [...store.claims.values()];
  const allIndexed = claims.length > 0 && claims.every((claim) => Number.isFinite(claim.claim_index));
  if (allIndexed) {
    claims.sort((a, b) => a.claim_index - b.claim_index || String(a.claim_id).localeCompare(String(b.claim_id)));
  }
  return claims;
}

export function groupsForClaim(store, claimId, map = store.results) {
  const buckets = new Map();
  for (const rec of map.values()) {
    if (rec.claim_id !== claimId) continue;
    if (!rec.citable || rec.revoked) continue;
    const group = rec.rankable ? rec.rankable_group || 'evidence_only' : 'evidence_only';
    if (!buckets.has(group)) buckets.set(group, []);
    buckets.get(group).push(rec);
  }
  const known = store.groupOrder.get(claimId) || [];
  const order = [];
  for (const group of known) if (buckets.has(group)) order.push(group);
  for (const group of buckets.keys()) {
    if (!order.includes(group) && group !== 'evidence_only') order.push(group);
  }
  if (buckets.has('evidence_only')) order.push('evidence_only');
  return order.map((group) => ({
    group,
    results: buckets.get(group).slice().sort(compareResults),
  }));
}

function compareResults(a, b) {
  const rankA = Number.isFinite(a.current_rank) ? a.current_rank : Infinity;
  const rankB = Number.isFinite(b.current_rank) ? b.current_rank : Infinity;
  if (rankA !== rankB) return rankA - rankB;
  const scoreA = typeof a.display_score === 'number' ? a.display_score : -Infinity;
  const scoreB = typeof b.display_score === 'number' ? b.display_score : -Infinity;
  if (scoreA !== scoreB) return scoreB - scoreA;
  return String(a.result_id).localeCompare(String(b.result_id));
}

export function candidatesForClaim(store, claimId) {
  return [...store.candidates.values()]
    .filter((candidate) => candidate.claim_id === claimId)
    .sort((a, b) => {
      if ((a.ranking_status && a.ranking_status !== 'disabled')
        || (b.ranking_status && b.ranking_status !== 'disabled')) {
        return Number(a.ranking_status !== 'ranked') - Number(b.ranking_status !== 'ranked')
          || (a.llm_rank ?? Infinity) - (b.llm_rank ?? Infinity)
          || Number(a.ranking_status === 'ineligible') - Number(b.ranking_status === 'ineligible')
          || (a.base_rank ?? Infinity) - (b.base_rank ?? Infinity)
          || String(a.work_id).localeCompare(String(b.work_id));
      }
      const scoreA = a.score_status === 'scoped' && Number.isFinite(a.candidate_score)
        ? a.candidate_score : null;
      const scoreB = b.score_status === 'scoped' && Number.isFinite(b.candidate_score)
        ? b.candidate_score : null;
      return Number(scoreA === null) - Number(scoreB === null)
        || (scoreB ?? 0) - (scoreA ?? 0)
        || String(a.title || a.work_id).localeCompare(String(b.title || b.work_id))
        || String(a.work_id).localeCompare(String(b.work_id));
    });
}

export function diagnosticsForClaim(store, claimId, map = store.results) {
  const items = [];
  for (const rec of map.values()) {
    if (rec.claim_id !== claimId) continue;
    if (rec.citable && !rec.revoked) continue;
    items.push({ kind: rec.revoked ? 'revoked' : 'uncitable', rec });
  }
  for (const rejected of store.rejected.values()) {
    if (rejected.claim_id === claimId) items.push({ kind: 'rejected', rejected });
  }
  for (const revoked of store.revokedAfterFinal.values()) {
    if (revoked.claim_id === claimId) items.push({ kind: 'post-final-revoked', revoked });
  }
  return items;
}

export function workersList(store) {
  const latestByLibrary = new Map();
  for (const worker of store.workers.values()) {
    const libraryId = worker.library_id || 'unknown';
    const previous = latestByLibrary.get(libraryId);
    if (!previous || (worker.attempt || 1) >= (previous.attempt || 1)) {
      latestByLibrary.set(libraryId, worker);
    }
  }
  const requested = store.session?.requested_library_ids || [];
  const order = new Map(requested.map((libraryId, index) => [libraryId, index]));
  return [...latestByLibrary.values()].sort((a, b) => {
    const orderA = order.get(a.library_id) ?? Number.MAX_SAFE_INTEGER;
    const orderB = order.get(b.library_id) ?? Number.MAX_SAFE_INTEGER;
    if (orderA !== orderB) return orderA - orderB;
    return String(a.library_id).localeCompare(String(b.library_id));
  });
}

export function artifactCount(store) {
  if (!store || !store.queryId) return 0;
  if (store.simulation === false) return store.candidates.size;
  let count = 0;
  for (const rec of store.results.values()) {
    if (rec.citable && !rec.revoked) count += 1;
  }
  return count;
}

export function searchStateLabel(store) {
  const state = store.searchState;
  return SEARCH_STATE[state] || (state ? state : '—');
}

export function isTerminalState(store) {
  return store.terminal;
}
