import assert from 'node:assert/strict';
import test from 'node:test';

import { Store, candidatesForClaim } from '../web/js/store.js';

test('agentic ranks are authoritative and stale pool events cannot overwrite them', () => {
  const store = new Store();
  const candidate = { result_id: 'r', work_id: 'w', claim_id: 'c', pool_version: 2,
    ranking_version: 1, ranking_status: 'ranked', base_rank: 2, llm_rank: 1,
    candidate_score: 40, rank_delta: 1 };
  store.applyEvent({ event_id: 'ranked', sequence: 2, event_type: 'candidate_reranked',
    payload: { ranking: { claim_id: 'c', pool_version: 2, ranking_version: 1 }, candidates: [candidate] } });
  store.applyEvent({ event_id: 'old', sequence: 1, event_type: 'candidate_pool_updated',
    payload: { ranking: { claim_id: 'c', pool_version: 1, ranking_version: 0 },
      candidates: [{ ...candidate, pool_version: 1, candidate_score: 99, llm_rank: null }] } });
  assert.equal(store.candidates.get('r').llm_rank, 1);
  assert.equal(store.candidates.get('r').candidate_score, 40);
  store.applyEvent({ event_id: 'next', sequence: 3, event_type: 'candidate_pool_updated',
    payload: { ranking: { claim_id: 'c', pool_version: 3, ranking_version: 1 },
      candidates: [{ ...candidate, pool_version: 3, ranking_status: 'pending', llm_rank: null, rank_delta: null }] } });
  assert.equal(store.candidates.get('r').llm_rank, null);
  assert.equal(store.candidateRankings.get('c').pool_version, 3);
});

test('LLM rank overrides numeric base score and survives state reconstruction', () => {
  const store = new Store();
  const items = [
    { result_id: 'a', work_id: 'a', claim_id: 'c', ranking_status: 'ranked', candidate_score: 99, base_rank: 1, llm_rank: 2 },
    { result_id: 'b', work_id: 'b', claim_id: 'c', ranking_status: 'ranked', candidate_score: 30, base_rank: 2, llm_rank: 1 },
  ];
  store.applyState({ candidate_diagnostics: items, candidate_rankings: [{ claim_id: 'c', pool_version: 2 }], workers: [] });
  assert.deepEqual(candidatesForClaim(store, 'c').map((item) => item.work_id), ['b', 'a']);
  assert.equal(store.candidateRankings.get('c').pool_version, 2);
});

function recommendation(id = 'result-1', claimId = 'claim-1') {
  return {
    result_id: id,
    result_version: 1,
    claim: { claim_id: claimId, claim_index: 0 },
    citable: true,
    rankable: true,
    rankable_group: 'test-scale-group',
    display_score: 80,
    evidence: [],
    sources: [],
  };
}

test('AT-EVT-001: replayed event IDs do not duplicate recommendations', () => {
  const store = new Store();
  const event = {
    event_id: 'event-1',
    sequence: 1,
    event_type: 'recommendation_created',
    payload: { recommendation: recommendation() },
  };
  assert.equal(store.applyEvent(event).changed, true);
  assert.equal(store.applyEvent(event).changed, false);
  assert.equal(store.results.size, 1);
  assert.equal(store.lastSequence, 1);
});

test('AT-SCR-007: state recovery keeps claim identities and groups separate', () => {
  const store = new Store();
  store.applyState({
    session: { search_state: 'running', final_snapshot_id: null },
    results: [recommendation('result-1', 'claim-1'), recommendation('result-2', 'claim-2')],
    groups: [
      { claim_id: 'claim-1', rankable_group: 'test-scale-group', entries: [{ result_id: 'result-1', rank: 1 }] },
      { claim_id: 'claim-2', rankable_group: 'test-scale-group', entries: [{ result_id: 'result-2', rank: 1 }] },
    ],
    workers: [],
    as_of_sequence: 2,
    latest_sequence: 2,
    is_live: true,
  });
  assert.equal(store.results.get('result-1').claim_id, 'claim-1');
  assert.equal(store.results.get('result-2').claim_id, 'claim-2');
  assert.equal(store.lastSequence, 2);
  assert.equal(store.groupOrder.size, 2);
});

test('AT-SNP-001: a late result cannot mutate a frozen snapshot', () => {
  const store = new Store();
  store.applyEvent({
    event_id: 'final',
    sequence: 1,
    event_type: 'search_finalized',
    payload: { snapshot_id: 'snapshot-1', snapshot_kind: 'final' },
  });
  const original = store.finalSnapshot;
  store.applyEvent({
    event_id: 'late',
    sequence: 2,
    event_type: 'recommendation_created',
    payload: { recommendation: recommendation('late-result') },
  });
  assert.equal(store.finalSnapshot, original);
  assert.equal(store.results.size, 0);
  assert.equal(store.lateResults.size, 1);
});

test('EVT-002: terminal state does not discard the subsequent final snapshot event', () => {
  const store = new Store();
  store.applyEvent({
    event_id: 'state-complete',
    sequence: 1,
    event_type: 'search_state_changed',
    payload: { previous_state: 'finalizing', current_state: 'completed' },
  });
  assert.equal(store.frozen, false);
  store.applyEvent({
    event_id: 'snapshot-final',
    sequence: 2,
    event_type: 'search_finalized',
    payload: {
      snapshot_id: 'snapshot-final',
      snapshot_kind: 'final',
      claim_coverage: { total: 1, completed: 1 },
    },
  });
  assert.equal(store.frozen, true);
  assert.equal(store.finalSnapshot.snapshot_id, 'snapshot-final');
});


test('real candidates merge sources without becoming recommendations or snapshot items', () => {
  const store = new Store();
  store.attachCreateResponse({ query_id: 'q-real', simulation: false, claims: [{ claim_id: 'claim-real', claim_index: 0 }] });
  const candidate = {
    result_id: 'res-real', work_id: 'work:10.1007/example', paper_id: 'paper:one',
    claim_id: 'claim-real', title: 'Real source', doi: '10.1007/example',
    library_ids: ['library-a'], publication_status: 'corrected', related_dois: {},
    relationship: 'insufficient', scope_match: 'unknown', rejection_reasons: ['source_trace_invalid'],
    evidence_excerpt: 'Abstract only', candidate_score: 80, publication_types: ['research-article'],
    source_uri: 'https://example.org/a', quality_note: 'Abstract only',
  };
  store.applyEvent({ event_id: 'candidate-a', sequence: 1, event_type: 'candidate_diagnostic', payload: { candidate } });
  store.applyEvent({
    event_id: 'candidate-b', sequence: 2, event_type: 'candidate_diagnostic',
    payload: { candidate: { ...candidate, library_ids: ['library-b'], candidate_score: 90, source_uris: { 'library-b': 'https://example.org/b' } } },
  });
  assert.equal(store.results.size, 0);
  assert.equal(store.candidates.size, 1);
  assert.deepEqual(store.candidates.get('res-real').library_ids, ['library-a', 'library-b']);
  assert.equal(candidatesForClaim(store, 'claim-real')[0].candidate_score, 90);
  store.applyEvent({
    event_id: 'candidate-c', sequence: 3, event_type: 'candidate_diagnostic',
    payload: { candidate: { ...candidate, library_ids: ['library-c'], candidate_score: 95,
      score_version: 'online-v1', score_scale_group: 'online', score_status: 'scoped' } },
  });
  assert.equal(store.candidates.get('res-real').score_status, 'incomparable');
  store.applyEvent({
    event_id: 'candidate-d', sequence: 4, event_type: 'candidate_diagnostic',
    payload: { candidate: { ...candidate, library_ids: ['library-d'], candidate_score: 100,
      score_version: 'local-v1', score_scale_group: 'local', score_status: 'scoped' } },
  });
  assert.equal(store.candidates.get('res-real').candidate_score, null);
  assert.equal(store.candidates.get('res-real').score_status, 'incomparable');
  assert.deepEqual(store.candidates.get('res-real').library_ids,
    ['library-a', 'library-b', 'library-c', 'library-d']);
  assert.equal(store.finalSnapshot, null);
  store.applyState({
    session: { query_id: 'q-real', search_state: 'completed', final_snapshot_id: 'snap-real' },
    adapter_mode: 'real_abstract_metadata_live_llm', results: [],
    candidate_diagnostics: [store.candidates.get('res-real')], workers: [], groups: [],
  });
  assert.equal(store.results.size, 0);
  assert.equal(store.candidates.size, 1);
  assert.equal(store.simulation, false);
  assert.equal(store.finalSnapshot.snapshot_id, 'snap-real');
});

test('unscored real candidates are not treated as zero or given a numeric rank', () => {
  const store = new Store();
  store.attachCreateResponse({ query_id: 'q-real', simulation: false, claims: [{ claim_id: 'claim-real', claim_index: 0 }] });
  const candidate = {
    result_id: 'res-scored', work_id: 'work:scored', claim_id: 'claim-real',
    title: 'Direct study', candidate_score: 100, score_status: 'scoped',
  };
  store.applyEvent({ event_id: 'candidate-scored', sequence: 1,
    event_type: 'candidate_diagnostic', payload: { candidate } });
  store.applyEvent({ event_id: 'candidate-failed', sequence: 2,
    event_type: 'candidate_diagnostic', payload: { candidate: {
      ...candidate, result_id: 'res-unscored', work_id: 'work:unscored',
      title: 'Unscored neighbour', candidate_score: null, score_status: 'failed',
    } } });
  const ordered = candidatesForClaim(store, 'claim-real');
  assert.deepEqual(ordered.map((item) => item.work_id), ['work:scored', 'work:unscored']);
  assert.equal(ordered[1].candidate_score, null);
  assert.equal(ordered[1].score_status, 'failed');
  const merged = new Store();
  merged.attachCreateResponse({ query_id: 'q-real', simulation: false, claims: [{ claim_id: 'claim-real', claim_index: 0 }] });
  for (const [sequence, current] of [candidate, {
    ...candidate, library_ids: ['library-b'], candidate_score: null, score_status: 'failed',
  }].entries()) {
    merged.applyEvent({ event_id: `candidate-${sequence}`, sequence: sequence + 1,
      event_type: 'candidate_diagnostic', payload: { candidate: current } });
  }
  assert.equal(merged.candidates.get('res-scored').candidate_score, 100);
  assert.equal(merged.candidates.get('res-scored').score_status, 'scoped');
});
