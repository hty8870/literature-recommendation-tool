import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';

import { InputError, buildSearchPayload, libraryDisplayName, libraryPillLabel, manuscriptContentType, terms, validateManuscriptFile } from '../web/js/search-input.js';
import { formatCitationInfo } from '../web/js/citation.js';
import { Store, artifactCount, workersList } from '../web/js/store.js';
import { AgentSession } from '../web/js/agent-session.js';
import { preserveResultScroll } from '../web/js/results-view.js';
import { recommendationReason } from '../web/js/result-card.js';
import { candidateScoreText, candidateRankChange, candidateCardSignature } from '../web/js/candidate-card.js';
import { agentOutcome } from '../web/js/agent-run-view.js';
import { resultStatus, workerCounts } from '../web/js/render.js';
import { modelRequestPayload, readModelSelection, writeModelSelection } from '../web/js/model-settings.js';
import { CitationController, citationRequestBody } from '../web/js/citation-controller.js';
import { citationBibliography, citationFactEvidence, citationLocatorLabel } from '../web/js/citation-view.js';
import { citationPreparationNote, failureMessage, normalizeConversationMessages, runProgressText } from '../web/js/chat-model.js';
import { CONVERSATION_STORAGE_KEY, clearConversationId, readConversationId, writeConversationId } from '../web/js/conversation-storage.js';

const libraries = [
  { library_id: 'fixture-fast', display_name: '本地演示快库（模拟夹具）', default_delay_ms: 250 },
  { library_id: 'fixture-slow', display_name: '本地演示慢库（模拟夹具）', default_delay_ms: 1600 },
];

test('UI-ROUTING-001: API defaults stay under the deployment subpath', async (t) => {
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  t.after(() => {
    if (previousWindow === undefined) delete globalThis.window;
    else globalThis.window = previousWindow;
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  });
  globalThis.document = { querySelector: () => null };
  for (const [path, expected] of [
    ['https://biodataagent.com/literature/', '/literature/api/v1'],
    ['http://127.0.0.1:8765/', '/api/v1'],
    ['https://biodataagent.com/literature/?api=https%3A%2F%2Fexample.com%2Fapi', '/literature/api/v1'],
    ['http://127.0.0.1:8765/?api=http%3A%2F%2F127.0.0.1%3A9999%2Fapi', 'http://127.0.0.1:9999/api'],
  ]) {
    globalThis.window = {
      location: { href: path, hostname: new URL(path).hostname, search: new URL(path).search },
    };
    const module = await import(`../web/js/api.js?routing=${encodeURIComponent(path)}`);
    assert.equal(module.API_BASE, expected);
  }
});

test('UI-INPUT-001: keywords use the selected libraries and explicit demo scope', () => {
  const payload = buildSearchPayload({
    mode: 'keywords',
    value: 'LFY2, soybean；floral development',
    libraries,
    selectedLibraryIds: new Set(['fixture-fast']),
    demoScopeEnabled: true,
  });
  assert.equal(payload.input_type, 'keywords');
  assert.deepEqual(payload.keywords, ['LFY2', 'soybean', 'floral development']);
  assert.deepEqual(payload.library_ids, ['fixture-fast']);
  assert.deepEqual(payload.options.library_delays_ms, { 'fixture-fast': 250 });
  assert.equal(payload.claim_scope.species[0], 'Glycine max');
  assert.equal(Object.hasOwn(payload, 'abstract'), false);
});

test('real sources never receive the hidden fixture scope even with a stale demo flag', () => {
  const payload = buildSearchPayload({
    mode: 'keywords', value: 'LFY2',
    libraries: [{ library_id: 'real', simulation: false, default_delay_ms: 0 }],
    selectedLibraryIds: new Set(['real']), demoScopeEnabled: true,
  });
  assert.equal(Object.hasOwn(payload, 'claim_scope'), false);
});

test('UI-INPUT-002: abstract stays a distinct API input without inventing keywords', () => {
  const payload = buildSearchPayload({
    mode: 'abstract',
    value: '  A structured abstract about soybean flowering.  ',
    libraries,
    selectedLibraryIds: new Set(libraries.map((library) => library.library_id)),
    demoScopeEnabled: false,
  });
  assert.equal(payload.input_type, 'abstract');
  assert.equal(payload.abstract, 'A structured abstract about soybean flowering.');
  assert.equal(Object.hasOwn(payload, 'keywords'), false);
  assert.equal(Object.hasOwn(payload, 'claim_scope'), false);
});

test('UI-INPUT-003: empty queries, unselected libraries and unavailable full text fail explicitly', () => {
  assert.throws(() => buildSearchPayload({
    mode: 'keywords', value: '', libraries, selectedLibraryIds: new Set(['fixture-fast']), demoScopeEnabled: true,
  }), (error) => error instanceof InputError && error.code === 'EMPTY_QUERY');
  assert.throws(() => buildSearchPayload({
    mode: 'keywords', value: 'LFY2', libraries, selectedLibraryIds: new Set(), demoScopeEnabled: true,
  }), (error) => error instanceof InputError && error.code === 'NO_LIBRARY_SELECTED');
  assert.throws(() => buildSearchPayload({
    mode: 'full_text', value: '', libraries, selectedLibraryIds: new Set(['fixture-fast']), demoScopeEnabled: true,
  }), (error) => error instanceof InputError && error.code === 'MANUSCRIPT_REQUIRED');
  assert.throws(() => buildSearchPayload({
    mode: 'full_text', value: '', libraries, selectedLibraryIds: new Set(['fixture-fast']),
    demoScopeEnabled: true, manuscript: { manuscript_id: 'm-parsing', parse_status: 'parsing' },
  }), (error) => error instanceof InputError && error.code === 'MANUSCRIPT_NOT_READY');
});

test('UI-INPUT-005: a ready manuscript becomes a real full_text request, never an abstract', () => {
  const payload = buildSearchPayload({
    mode: 'full_text',
    value: '',
    libraries,
    selectedLibraryIds: new Set(libraries.map((library) => library.library_id)),
    demoScopeEnabled: true,
    manuscript: { manuscript_id: 'm-ready', parse_status: 'ready', page_count: 12 },
  });
  assert.deepEqual(payload, {
    input_type: 'full_text',
    manuscript_id: 'm-ready',
    library_ids: ['fixture-fast', 'fixture-slow'],
  });
  assert.equal(Object.hasOwn(payload, 'abstract'), false);
  assert.equal(Object.hasOwn(payload, 'keywords'), false);
  assert.equal(Object.hasOwn(payload, 'claim_scope'), false);
  assert.equal(manuscriptContentType('paper.PDF'), 'application/pdf');
  assert.equal(manuscriptContentType('paper.docx'),
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document');
  assert.equal(manuscriptContentType('paper.txt'), null);
});

test('UI-INPUT-004: local file selection checks extension and bounded size without reading content', () => {
  assert.equal(validateManuscriptFile({ name: 'paper.PDF', size: 1024 }), null);
  assert.equal(validateManuscriptFile({ name: 'paper.docx', size: 2048 }), null);
  assert.match(validateManuscriptFile({ name: 'paper.txt', size: 1024 }), /仅支持/);
  assert.match(validateManuscriptFile({ name: 'paper.pdf', size: 0 }), /为空/);
  assert.match(validateManuscriptFile({ name: 'paper.pdf', size: 51 * 1024 * 1024 }), /50 MB/);
  assert.deepEqual(terms('a, b\nc'), ['a', 'b', 'c']);
});

test('UI-CITATION-001: copied information contains only available metadata and a simulation warning', () => {
  const text = formatCitationInfo({
    simulation: true,
    sources: [{ display_name: '本地演示快库', title: 'Demo title', year: 2024, doi: '10.5555/demo' }],
    evidence: [],
  });
  assert.match(text, /本地模拟数据，请勿用于真实引用/);
  assert.match(text, /Demo title\. \(2024\)\. DOI: 10\.5555\/demo/);
  assert.match(text, /来源库：本地演示快库/);
  assert.doesNotMatch(text, /作者未提供|未知作者/);
});

test('UI-CITATION-003: real candidates copy actual metadata without a simulation warning', () => {
  const text = formatCitationInfo({
    title: 'LFY2 study', authors: 'Wang L, Liu H', year: 2025,
    doi: '10.1007/example', candidate_score: null, score_status: 'incomparable',
    library_ids: ['europepmc-real'],
  }, new Map([['europepmc-real', 'Europe PMC']]));
  assert.match(text, /^Wang L, Liu H\. LFY2 study\. \(2025\)\. DOI: 10\.1007\/example/);
  assert.match(text, /来源库：Europe PMC/);
  assert.doesNotMatch(text, /模拟数据/);
});

test('UI-CITATION-002: library labels are shortened without maintaining another library registry', () => {
  assert.equal(libraryDisplayName(libraries[0]), '本地演示快库');
  assert.equal(libraryDisplayName({ library_id: 'external', display_name: '' }), 'external');
  assert.equal(libraryPillLabel({ library_id: 'online', display_name: 'Europe PMC 在线' }), 'Europe PMC 在线');
  assert.equal(libraryPillLabel({ display_name: '多学科文献快照', record_count: 11002 }), '多学科文献快照 · 11,002');
  assert.equal(libraryPillLabel({ display_name: 'aBiotech 老师样例库', record_count: 50 }), 'aBiotech 老师样例库 · 50');
});

test('UI-PROGRESS-001: one card per library uses the latest attempt and requested order', () => {
  const store = new Store();
  store.attachCreateResponse({ query_id: 'q-progress', search_state: 'running', claims: [] });
  store.session.requested_library_ids = ['fixture-slow', 'fixture-fast'];
  const events = [
    ['event-1', 1, 'worker_completed', 'fixture-fast', 1],
    ['event-2', 2, 'worker_progress', 'fixture-fast', 2],
    ['event-3', 3, 'worker_completed', 'fixture-slow', 1],
  ];
  for (const [event_id, sequence, event_type, library_id, attempt] of events) {
    store.applyEvent({
      event_id,
      sequence,
      event_type,
      payload: { worker: { library_id, attempt, worker_state: event_type === 'worker_completed' ? 'completed' : 'running' } },
    });
  }
  const workers = workersList(store);
  assert.equal(workers.length, 2);
  assert.deepEqual(workers.map((worker) => worker.library_id), ['fixture-slow', 'fixture-fast']);
  assert.equal(workers[0].attempt, 1);
  assert.equal(workers[1].attempt, 2);
  assert.deepEqual([...store.completedLibraryIds], ['fixture-slow'], 'a new running attempt is not completed');
});

test('UI-PROGRESS-002: stale worker updates cannot regress counts or completion', () => {
  const store = new Store();
  store.attachCreateResponse({ query_id: 'q-monotonic', search_state: 'running', claims: [] });
  store.seedPendingWorkers(['fixture-fast']);
  store.applyEvent({
    event_id: 'complete', sequence: 1, event_type: 'worker_completed',
    payload: { worker: { library_id: 'fixture-fast', attempt: 1, worker_state: 'completed', candidates_seen: 5 } },
  });
  store.applyEvent({
    event_id: 'stale', sequence: 2, event_type: 'worker_progress',
    payload: { worker: { library_id: 'fixture-fast', attempt: 1, worker_state: 'running', candidates_seen: 1 } },
  });
  assert.equal(store.completedLibraryIds.size, 1);
  assert.equal(workersList(store)[0].worker_state, 'completed');
  assert.equal(workersList(store)[0].candidates_seen, 5);
});

test('UI-PROGRESS-003: source submissions are not described as verification or recommendations', () => {
  assert.equal(workerCounts({ placeholder: true }), '等待任务开始');
  assert.equal(workerCounts({ source_agent_status: 'running', candidates_seen: 32,
    candidates_verified: 19, source_candidates_submitted: 7 }), '已发现 32 · 已提交 7');
  assert.equal(workerCounts({ source_agent_status: 'completed', candidates_seen: 1,
    candidates_verified: 1, source_candidates_submitted: 0 }), '已发现 1 · 已提交 0');
  assert.equal(workerCounts({ source_agent_status: 'disabled', candidates_seen: 5,
    candidates_verified: 3 }), '候选 5 · 已处理 3');
});

test('UI-PROGRESS-004: source completion does not imply central ranking or Agent completion', () => {
  const store = new Store();
  store.attachCreateResponse({ query_id: 'q', search_state: 'running' });
  store.applyEvent({ event_id: 'source', sequence: 1, event_type: 'worker_completed',
    payload: { worker: { library_id: 'a', attempt: 1, worker_state: 'completed' } } });
  store.candidateRankings.set('c', { status: 'pending' });
  assert.equal(store.completedLibraryIds.size, 1);
  assert.equal(resultStatus(store).text, '等待 LLM 重排');
  store.candidateRankings.set('c', { status: 'completed' });
  assert.equal(resultStatus(store).text, '结果更新中');
  store.session.search_state = 'completed';
  assert.equal(resultStatus(store, { status: 'running' }).text, 'Agent 处理中');
  assert.equal(resultStatus(store, { status: 'completed' }).text, '');
  store.candidateRankings.set('c', { status: 'failed', failure_code: 'MODEL_UNGROUNDED_OUTPUT' });
  assert.deepEqual(resultStatus(store), { text: 'LLM 重排未完成', level: 'error', detail: 'MODEL_UNGROUNDED_OUTPUT' });
  store.session.search_state = 'cancelled';
  assert.equal(resultStatus(store).text, '已取消');
  store.connection = 'exhausted';
  assert.equal(resultStatus(store).text, '连接中断');
});

test('real LLM phase events affect only the current pending pool and preserve cards', () => {
  const store = new Store();
  store.attachCreateResponse({ query_id: 'q', search_state: 'running' });
  let sequence = 0;
  const event = (type, payload) => store.applyEvent({ event_id: String(++sequence), sequence,
    event_type: type, payload });
  const ranking = { claim_id: 'c', pool_version: 2, ranking_version: 0, status: 'pending' };
  const candidate = { result_id: 'r', claim_id: 'c', ranking_status: 'pending', pool_version: 2 };
  const phase = (name, version = 2) => event('candidate_ranking_progress', {
    claim_id: 'c', pool_version: version, phase: name,
  });
  event('candidate_pool_updated', { ranking, candidates: [candidate] });
  const original = store.candidates.get('r');
  phase('blind', 1);
  assert.equal(resultStatus(store).text, '等待 LLM 重排');
  phase('blind');
  assert.equal(resultStatus(store).text, 'LLM 重排 · 盲排中');
  phase('score_review');
  phase('blind');
  assert.equal(resultStatus(store).text, 'LLM 重排 · 揭分复核中');
  assert.equal(store.candidates.get('r'), original);
  event('candidate_reranked', {
    ranking: { ...ranking, ranking_version: 1, status: 'completed', score_review: { status: 'kept' } },
    candidates: [candidate],
  });
  phase('blind');
  assert.equal(resultStatus(store).text, 'LLM 重排完成');
});

test('citation progress is distinct from search and an incomplete batch is not success', () => {
  const waiting = { status: 'running', citation_preparation: { status: 'waiting' } };
  assert.equal(runProgressText(waiting, true), '正在准备引用建议…');
  assert.equal(citationPreparationNote(waiting), '');
  assert.equal(citationPreparationNote({ status: 'completed', citation_preparation: {
    status: 'completed', ready: 2,
  } }), '');
  assert.match(citationPreparationNote({ status: 'completed', citation_preparation: {
    status: 'partial', unfinished_batches: 1,
  } }), /尚未完成/);
  assert.match(citationPreparationNote({ status: 'completed', citation_preparation: {
    status: 'partial', unavailable: 2,
  } }), /2 条/);
});

test('UI-PROGRESS-005: old attempts cannot complete a running retry or override its error', () => {
  const store = new Store();
  const event = (sequence, worker) => store.applyEvent({ event_id: String(sequence), sequence,
    event_type: 'worker_progress', payload: { worker: { library_id: 'a', ...worker } } });
  event(1, { attempt: 1, worker_state: 'completed', source_candidates_submitted: 20 });
  event(2, { attempt: 2, worker_state: 'running', source_candidates_submitted: 2 });
  event(3, { attempt: 1, worker_state: 'completed', source_candidates_submitted: 21 });
  assert.equal(store.completedLibraryIds.size, 0);
  assert.equal(workersList(store)[0].source_candidates_submitted, 2);
  event(4, { attempt: 2, worker_state: 'failed', source_candidates_submitted: 3,
    updated_at: '2026-11-17T01:00:02Z', last_error: { code: 'SOURCE_AGENT_FAILED' } });
  event(5, { attempt: 2, worker_state: 'running', source_candidates_submitted: 1,
    updated_at: '2026-11-17T01:00:01Z' });
  assert.equal(workersList(store)[0].worker_state, 'failed');
  assert.equal(workersList(store)[0].source_candidates_submitted, 3);
  assert.equal(workersList(store)[0].last_error.code, 'SOURCE_AGENT_FAILED');
});

test('UI-MINIMAL-001: the result page omits diagnostic and duplicate query surfaces', async () => {
  const html = await readFile(new URL('../web/index.html', import.meta.url), 'utf8');
  for (const id of ['search-query', 'session-badges', 'session-button', 'session-dialog',
    'checkpoint-button', 'final-button', 'coverage', 'event-log', 'source-kind-label']) {
    assert.equal(html.includes(`id="${id}"`), false, id);
  }
  assert.match(html, /id="worker-progress"/);
  assert.match(html, /id="worker-list"/);
  assert.match(html, /id="metric-results"/);
  assert.equal(html.includes('id="agent-summary"'), false);
  assert.equal(html.includes('class="worker-sidebar"'), false);
  assert.match(html, /id="chat-log"/);
  assert.match(html, /id="source-progress"/);
  assert.match(html, /id="pane-switch"/);
  assert.match(html, /<span class="site-title">文献推荐工具<\/span>/);
  assert.match(html, /id="chat-note" hidden><\/p>/);
  assert.match(html, /id="results-pane"[^>]*tabindex="0"/);
});

test('UI-RESULTS-001: streaming updates re-anchor the first visible card in the correct viewport', (t) => {
  const original = globalThis.window;
  t.after(() => {
    if (original === undefined) delete globalThis.window;
    else globalThis.window = original;
  });
  globalThis.window = { scrollY: 0, innerHeight: 700, scrollBy: t.mock.fn() };
  const pane = { scrollTop: 160, getBoundingClientRect: () => ({ top: 80, bottom: 680 }), scrollBy: t.mock.fn() };
  let offset = 0;
  const card = { isConnected: true, getBoundingClientRect: () => ({ top: 85 + offset, bottom: 180 + offset }) };
  const clipped = { isConnected: true, getBoundingClientRect: () => ({ top: 10, bottom: 75 }) };
  const root = { querySelectorAll: () => [clipped, card] };
  const restore = preserveResultScroll(root, pane);
  offset += 120;
  restore();
  assert.deepEqual(pane.scrollBy.mock.calls[0].arguments, [{ top: 120, behavior: 'instant' }]);
  assert.equal(window.scrollBy.mock.callCount(), 0);
  card.isConnected = false;
  restore();
  assert.equal(pane.scrollBy.mock.callCount(), 1);
  assert.equal(preserveResultScroll(root, { scrollTop: 0 }), null);

  card.isConnected = true;
  window.scrollY = 130;
  const restorePage = preserveResultScroll({ querySelectorAll: () => [card] }, { scrollTop: 0 });
  offset -= 24;
  restorePage();
  assert.deepEqual(window.scrollBy.mock.calls[0].arguments, [{ top: -24, behavior: 'instant' }]);
});

test('home keeps input guidance and contextual disclosures without persistent introductory copy', async () => {
  const html = await readFile(new URL('../web/index.html', import.meta.url), 'utf8');
  const home = html.match(/<section [^>]*id="search-home"[\s\S]*?<\/section>/)[0];
  assert.doesNotMatch(home, /home-hero__subtitle|demo-note|model-disclosure/);
  assert.match(home, /id="input-help"/);
  assert.match(home, /id="library-note" hidden><\/p>/);
  assert.match(home, /aria-describedby="full-text-note"/);
  assert.match(html, /id="model-dialog"[\s\S]*?在线库时，检索词会发给该数据源。/);
});

test('home has a single file chooser, inside the full-text input', async () => {
  const html = await readFile(new URL('../web/index.html', import.meta.url), 'utf8');
  const home = html.match(/<section [^>]*id="search-home"[\s\S]*?<\/section>/)[0];
  assert.match(home, /data-mode="full_text"/);
  const upload = home.match(/<div [^>]*id="upload-control" hidden>[\s\S]*?<\/div>/)[0];
  assert.match(upload, /type="file" id="manuscript-file"/);
  assert.match(upload, /id="upload-zone"/);
  assert.equal(home.match(/type="file"/g).length, 1);
  assert.doesNotMatch(home, /attach-manuscript|composer-upload/);
});

test('home shows a library note only for simulation or an unavailable search', async (t) => {
  const { HomeView } = await import('../web/js/home-view.js');
  const realLibrary = { simulation: false };
  const view = Object.assign(Object.create(HomeView.prototype), {
    libraries: [realLibrary], libraryNote: { textContent: '', hidden: true },
    clearError: t.mock.fn(), updateButtons: t.mock.fn(),
  });
  view.setAgentAvailable(false);
  assert.equal(view.ready, false);
  assert.equal(view.libraryNote.hidden, false);
  assert.match(view.libraryNote.textContent, /暂无可用模型/);
  view.setAgentAvailable(true);
  assert.equal(view.ready, true);
  assert.equal(view.libraryNote.hidden, true);
  assert.equal(view.libraryNote.textContent, '');
  view.libraries = [realLibrary, { simulation: true }];
  view.setAgentAvailable(true);
  assert.equal(view.libraryNote.hidden, false);
  assert.match(view.libraryNote.textContent, /模拟/);
  view.libraries = [];
  view.setLibraryNote('请刷新页面重试。');
  view.setAgentAvailable(true);
  assert.equal(view.ready, false);
  assert.equal(view.libraryNote.hidden, false);
  assert.equal(view.libraryNote.textContent, '请刷新页面重试。');
});

test('FND-061 project controls are auth-gated and source requests start hidden', async () => {
  const html = await readFile(new URL('../web/index.html', import.meta.url), 'utf8');
  assert.match(html, /id="projects-control" hidden/);
  assert.match(html, /<select id="project-picker"><option value="other">其它/);
  assert.match(html, /id="source-request-dock"[^>]* hidden/);
  assert.match(html, /aria-controls="source-requests-dialog"/);
  const session = new AgentSession();
  session.applyConversation({ conversation_id: 'cnv_example', project_id: 'prj_example', revision: 2 });
  assert.equal(session.projectId, 'prj_example');
  session.reset();
  assert.equal(session.projectId, null);
});

test('UI-HISTORY-001: one top-bar session entry and one shared history panel', async () => {
  const html = await readFile(new URL('../web/index.html', import.meta.url), 'utf8');
  const header = html.match(/<header class="site-header">[\s\S]*?<\/header>/)[0];
  assert.doesNotMatch(header, /brand|history-(home|results)-button|data-icon="book-open"/);
  assert.match(header, /class="site-header__actions"[\s\S]*id="new-search-button"[\s\S]*id="profile-button"/);
  assert.equal(html.match(/id="history-panel"/g).length, 1);
  assert.match(html, /id="search-home"[\s\S]*<aside [^>]*id="history-panel"[\s\S]*id="history-new"/);
});

function installWorkspaceDocument(t, document) {
  const previous = { window: globalThis.window, document: globalThis.document };
  t.after(() => {
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete globalThis[name];
      else globalThis[name] = value;
    }
  });
  globalThis.window = { location: new URL('http://127.0.0.1:8765/') };
  globalThis.document = document;
}

test('UI-ACCOUNT-001: avatar activates one visual, including SVG and Unicode initials', async (t) => {
  const elements = new Map();
  const getElementById = (id) => {
    if (!elements.has(id)) elements.set(id, {});
    return elements.get(id);
  };
  const svgAttrs = new Map();
  elements.set('profile-icon', { toggleAttribute: (name, force) => force ? svgAttrs.set(name, '') : svgAttrs.delete(name) });
  installWorkspaceDocument(t, { querySelector: () => null, getElementById });
  const { WorkspaceAccount } = await import('../web/js/workspace-account.js');
  const accountView = { trigger: { setAttribute() {} } };
  const initial = getElementById('profile-initial');
  for (const [account, glyph] of [
    [{ display_name: ' Hannah ', username: 'han' }, 'H'],
    [{ display_name: '徐老师', username: 'teacher' }, '徐'],
    [{ display_name: '👩‍🔬User', username: 'test' }, '👩‍🔬'],
    [null, ''],
  ]) {
    WorkspaceAccount.prototype.setIdentity.call(accountView, account, { enabled: true, models: true });
    assert.equal(initial.textContent, glyph);
    assert.equal(initial.hidden, !glyph);
    assert.equal(svgAttrs.has('hidden'), Boolean(glyph));
  }
});

test('UI-HISTORY-002: leaving the popover cannot close the permanent home history', async (t) => {
  const element = () => new EventTarget();
  installWorkspaceDocument(t, Object.assign(element(), { getElementById: element, querySelector: () => null }));
  const { WorkspaceHistory } = await import('../web/js/workspace-history.js');
  const history = Object.assign(Object.create(WorkspaceHistory.prototype), {
    trigger: element(), newButton: element(), cluster: element(), more: element(),
    editor: element(), editorForm: element(), panel: { hidden: false, contains: () => false },
    view: 'home', close: t.mock.fn(),
  });
  const schedule = t.mock.method(globalThis, 'setTimeout', () => {});
  history.bind();
  history.cluster.dispatchEvent(new Event('pointerleave'));
  assert.equal(schedule.mock.callCount(), 0);
  history.view = 'results';
  history.cluster.dispatchEvent(new Event('pointerleave'));
  assert.equal(schedule.mock.callCount(), 1);
  const dismiss = schedule.mock.calls[0].arguments[0];
  history.view = 'home';
  dismiss();
  assert.equal(history.close.mock.callCount(), 0);
  history.view = 'results';
  dismiss();
  assert.equal(history.close.mock.callCount(), 1);
});

test('candidate cards ignore nonvisual pool updates but react to displayed rank and reason changes', () => {
  const candidate = { result_id: 'r', llm_rank: 1, pool_version: 1,
    reason: { status: 'generated', text: '真实理由', generated_at: 'old' } };
  const signature = candidateCardSignature(candidate);
  assert.equal(candidateCardSignature({ ...candidate, pool_version: 2, ranking_version: 3,
    reason: { ...candidate.reason, generated_at: 'new' } }), signature);
  assert.notEqual(candidateCardSignature({ ...candidate, llm_rank: 2 }), signature);
  assert.notEqual(candidateCardSignature({ ...candidate, reason: { ...candidate.reason, text: '新的真实理由' } }), signature);
});

test('UI-RANK-001: movements use the server-provided same-pool rank difference', () => {
  assert.equal(candidateRankChange({ ranking_status: 'ranked', rank_delta: 6 }), '较基础排名 ↑6');
  assert.equal(candidateRankChange({ ranking_status: 'ranked', rank_delta: -2 }), '较基础排名 ↓2');
  assert.equal(candidateRankChange({ ranking_status: 'ranked', rank_delta: 0 }), '与基础排名一致');
  assert.equal(candidateRankChange({ ranking_status: 'pending', rank_delta: 6 }), '');
  assert.equal(candidateRankChange({ ranking_status: 'failed', rank_delta: null }), '');
});

test('UI-SCORE-001: incomparable or missing candidate scores never render as zero', () => {
  assert.equal(candidateScoreText(null), '—');
  assert.equal(candidateScoreText(undefined), '—');
  assert.equal(candidateScoreText(Number.NaN), '—');
  assert.equal(candidateScoreText(0), '0.0');
  assert.equal(candidateScoreText(91.25), '91.3');
});

test('UI-REASON-001: reasons are server-generated text or an honest failure, never template text', () => {
  const generated = '该文涉及目标基因与开花组织，可核对相关论述的适用范围。';
  assert.equal(recommendationReason({
    citable: true,
    reason: { status: 'generated', text: generated },
  }), generated);
  const failure = recommendationReason({
    citable: true,
    reason: { status: 'failed', failure_code: 'MODEL_TIMEOUT' },
  });
  assert.match(failure, /MODEL_TIMEOUT/);
  assert.doesNotMatch(failure, /支持当前目标论述/);
});

function agentRun(queryIds) {
  return {
    run_id: 'agt-test', status: 'running', latest_query_id: queryIds.at(-1),
    queries: queryIds.map((id) => ({
      created: {
        query_id: id, search_state: 'created',
        claims: [{ claim_id: `claim-${id}`, claim_text: id, claim_type: 'topic' }],
      },
      requested_library_ids: ['fixture-fast'],
    })),
  };
}

test('UI-AGENT-001: independent searches survive autonomous retries without mixing ranks', () => {
  const session = new AgentSession();
  session.applyRun(agentRun(['q-one']), libraries);
  const first = session.store;
  first.applyState({
    session: { query_id: 'q-one', search_state: 'running' },
    results: [{
      result_id: 'res-one', query_id: 'q-one', claim_id: 'claim-q-one', citable: true,
      rankable: true, rankable_group: 'group', display_score: 80,
      claim: { claim_id: 'claim-q-one', claim_text: 'first', claim_type: 'topic' },
      sources: [], evidence: [],
    }],
    groups: [{ claim_id: 'claim-q-one', rankable_group: 'group', entries: [{ result_id: 'res-one', rank: 1 }] }],
    workers: [], coverage: null, as_of_sequence: 1, latest_sequence: 1,
  });
  session.applyRun(agentRun(['q-one', 'q-two']), libraries);
  assert.equal(session.store.queryId, 'q-two');
  assert.equal(first.results.get('res-one').current_rank, 1);
  assert.equal(session.store.results.has('res-one'), false);
  assert.equal(session.select('q-one'), true);
  assert.equal(session.store, first);
  session.applyRun(agentRun(['q-one', 'q-two', 'q-three']), libraries);
  assert.equal(session.store, first, 'manual inspection must not jump away from the selected query');
});

test('UI-AGENT-002: choosing no search leaves an honest empty session', () => {
  const session = new AgentSession();
  session.applyRun({ run_id: 'agt-none', status: 'completed', queries: [], latest_query_id: null });
  assert.equal(session.store.queryId, null);
  assert.equal(session.active, false);
  assert.equal(session.searches.size, 0);
});


test('UI-AGENT-003: model failure diagnostics include the stable error code', () => {
  assert.match(agentOutcome({
    summary: null,
    error: { code: 'MODEL_AUTHENTICATION_FAILED', message: 'Agent 未能完成。' },
  }), /MODEL_AUTHENTICATION_FAILED/);
});


test('UI-MODEL-001: custom model is session-scoped and the default request has no secret', () => {
  const values = new Map();
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
  const providers = ['deepseek', 'moonshot', 'openai'];
  assert.equal(readModelSelection(storage, providers), null);
  assert.equal(modelRequestPayload(null), null);
  const selection = writeModelSelection(storage, {
    provider: 'moonshot', model: ' test-model ', api_key: ' unit-test-placeholder ',
  }, providers);
  assert.deepEqual(readModelSelection(storage, providers), selection);
  assert.deepEqual(modelRequestPayload(selection), {
    provider: 'moonshot', model: 'test-model', api_key: 'unit-test-placeholder',
  });
  assert.equal(writeModelSelection(storage, null, providers), null);
  assert.equal(values.size, 0);
  assert.throws(() => writeModelSelection(storage, {
    provider: 'custom', model: 'test', api_key: 'dummy',
  }, providers), /不支持/);
  values.set('citation.model-selection.v1', '{invalid');
  assert.equal(readModelSelection(storage, providers), null);
  assert.equal(values.size, 0);
});

test('custom endpoint credentials and conversation pointers stay scoped to the browser identity', () => {
  const values = new Map();
  const storage = { getItem: (k) => values.get(k), setItem: (k, v) => values.set(k, v), removeItem: (k) => values.delete(k) };
  const protocols = ['chat_completions', 'responses'];
  const chosen = writeModelSelection(storage, { api_type: 'responses', base_url: 'https://EXAMPLE.net/v1/responses/', model: 'm1', api_key: 'fixture-key' }, [], protocols, 'alice');
  assert.deepEqual(modelRequestPayload(chosen), { api_type: 'responses', base_url: 'https://example.net/v1', model: 'm1', api_key: 'fixture-key' });
  assert.equal(readModelSelection(storage, [], protocols, 'bob'), null);
  writeModelSelection(storage, null, [], protocols, 'alice');
  assert.equal(readModelSelection(storage, [], protocols, 'alice'), null);
  writeConversationId(storage, 'conv-public');
  writeConversationId(storage, 'conv-private', 'alice');
  assert.equal(readConversationId(storage, 'alice'), 'conv-private');
  assert.equal(readConversationId(storage, 'bob'), null);
  clearConversationId(storage, 'alice');
  assert.equal(readConversationId(storage), 'conv-public');
});

test('UI-AGENT-004: every run keeps its own artifact and old ranks are never mixed', () => {
  const session = new AgentSession();
  const query = (id) => ({
    created: {
      query_id: id, search_state: 'created',
      claims: [{ claim_id: `claim-${id}`, claim_text: id, claim_type: 'topic' }],
    },
    requested_library_ids: ['fixture-fast'],
  });
  const run = (id, queryIds) => ({
    run_id: id, conversation_id: 'conv-multi', status: 'running',
    latest_query_id: queryIds.at(-1), queries: queryIds.map(query),
  });
  session.applyRun(run('run-1', ['q-one']), libraries);
  const first = session.storeForQuery('q-one');
  first.applyState({
    session: { query_id: 'q-one', search_state: 'running' },
    results: [{
      result_id: 'res-one', query_id: 'q-one', claim_id: 'claim-q-one', citable: true,
      rankable: true, rankable_group: 'group', display_score: 80, current_rank: 1,
      claim: { claim_id: 'claim-q-one', claim_text: 'first', claim_type: 'topic' },
      sources: [], evidence: [],
    }],
    groups: [{ claim_id: 'claim-q-one', rankable_group: 'group', entries: [{ result_id: 'res-one', rank: 1 }] }],
    workers: [], coverage: null, as_of_sequence: 1, latest_sequence: 1,
  });
  session.applyRun(run('run-2', ['q-one', 'q-two']), libraries);
  const second = session.storeForQuery('q-two');
  second.applyState({
    session: { query_id: 'q-two', search_state: 'running' },
    results: [{
      result_id: 'res-two', query_id: 'q-two', claim_id: 'claim-q-two', citable: true,
      rankable: true, rankable_group: 'group', display_score: 90, current_rank: 1,
      claim: { claim_id: 'claim-q-two', claim_text: 'second', claim_type: 'topic' },
      sources: [], evidence: [],
    }],
    groups: [], workers: [], as_of_sequence: 1, latest_sequence: 1,
  });
  assert.equal(session.store.queryId, 'q-two');
  assert.equal(first.results.get('res-one').current_rank, 1);
  assert.equal(second.results.has('res-one'), false);
  assert.equal(artifactCount(first), 1);
  assert.equal(session.select('q-one'), true);
  session.applyRun(run('run-3', ['q-one', 'q-three']), libraries);
  assert.equal(session.store, first, 'manual inspection of an old artifact must not jump away');
  assert.equal(session.store.results.has('res-two'), false);
  assert.equal(session.storeForQuery('q-three').queryId, 'q-three');
});

test('UI-AGENT-005: server conversation messages restore the chat and remain authoritative', () => {
  const session = new AgentSession();
  const created = {
    query_id: 'q-conv', search_state: 'completed',
    claims: [{ claim_id: 'claim-conv', claim_text: 'restored', claim_type: 'topic' }],
  };
  const result = session.applyConversation({
    conversation_id: 'conv-restored',
    revision: 4,
    messages: [
      { message_id: 'm-1', role: 'user', content: '第一问', run_id: 'run-conv', query_ids: [] },
      { message_id: 'm-2', role: 'assistant', content: '服务端结论', run_id: 'run-conv', query_ids: ['q-conv'] },
    ],
    runs: [{
      run_id: 'run-conv', conversation_id: 'conv-restored', status: 'completed',
      latest_query_id: 'q-conv', queries: [{ created, requested_library_ids: ['fixture-fast'] }],
      summary: '服务端结论',
    }],
  }, libraries, { focus: true });
  assert.equal(session.conversationId, 'conv-restored');
  assert.equal(session.revision, 4);
  assert.equal(session.store.queryId, 'q-conv');
  assert.deepEqual(result.addedIds, ['q-conv']);
  const chat = session.chatMessages();
  assert.deepEqual(chat.map((message) => message.content), ['第一问', '服务端结论']);
  assert.equal(chat[1].query_ids[0], 'q-conv');
  assert.equal(session.messages.length, 2);
});

test('UI-AGENT-006: a zero-tool reply creates no ranking artifact and the summary stays in chat', () => {
  const session = new AgentSession();
  session.applyRun({
    run_id: 'run-none', conversation_id: 'conv-none', status: 'completed',
    queries: [], latest_query_id: null, summary: '这轮不需要检索，直接回答。',
  }, libraries);
  assert.equal(session.store.queryId, null);
  assert.equal(session.searches.size, 0);
  assert.equal(session.hasQueries, false);
  const rows = session.chatMessages();
  assert.equal(rows.length, 1);
  assert.equal(rows[0].role, 'assistant');
  assert.equal(rows[0].content, '这轮不需要检索，直接回答。');

  const failed = new AgentSession();
  failed.applyRun({
    run_id: 'run-failed', conversation_id: 'conv-failed', status: 'failed',
    queries: [], error: { code: 'MODEL_TIMEOUT', message: '模型超时。' },
  }, libraries);
  const failedRows = failed.chatMessages();
  assert.equal(failedRows[0].kind, 'status');
  assert.equal(failedRows[0].level, 'error');
  assert.match(failedRows[0].content, /MODEL_TIMEOUT/);
  assert.equal(failed.searches.size, 0);
});

test('UI-STORAGE-001: refresh recovery stores only an opaque conversation id', () => {
  const values = new Map();
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
  assert.equal(readConversationId(storage), null);
  writeConversationId(storage, 'conv-opaque-1');
  assert.deepEqual([...values.entries()], [[CONVERSATION_STORAGE_KEY, 'conv-opaque-1']]);
  assert.equal(readConversationId(storage), 'conv-opaque-1');
  writeConversationId(storage, '');
  assert.equal(values.size, 0);
  writeConversationId(storage, 'conv-opaque-2');
  clearConversationId(storage);
  assert.equal(readConversationId(storage), null);
  assert.equal(values.size, 0);
});

test('UI-CITATION-004: viewing a prepared suggestion never starts generation', async () => {
  let called = 0;
  const target = { queryId: 'q-current', resultId: 'r-current', resultVersion: 3 };
  const replies = [
    { preparing: true, record: null },
    { preparing: false, record: { status: 'generating' } },
    { preparing: false, record: { status: 'ready', citation_text: '有来源依据的引用表述。' } },
  ];
  const controller = new CitationController({
    request: async () => { called += 1; return { citation_text_id: 'ct-manual' }; },
    poll: async () => ({ status: 'ready', citation_text: '手动补生成。' }),
    lookup: async (value) => { assert.equal(value, target); return replies.shift(); },
    wait: async () => {}, maxAttempts: 3,
  });
  assert.equal((await controller.open(target)).status, 'ready');
  assert.equal(called, 0);
  controller.lookup = async () => ({ preparing: false, record: null });
  assert.equal((await controller.open(target)).status, 'unprepared');
  assert.equal(called, 0);
  assert.equal((await controller.generate(target)).status, 'ready');
  assert.equal(called, 1);
});

test('legacy missing-fulltext suggestion can explicitly recheck only the selected work', async () => {
  const target = { queryId: 'q-current', resultId: 'res-preprint', resultVersion: 3 };
  let requests = 0;
  const controller = new CitationController({
    lookup: async () => ({ preparing: false, source_recheck: true, record: {
      status: 'failed', failure: { code: 'CITATION_FULLTEXT_UNAVAILABLE', retryable: false },
    } }),
    request: async (value) => { assert.equal(value, target); requests++; return { citation_text_id: 'ct-2' }; },
    poll: async () => ({ status: 'ready', citation_text: '公开预印本引用建议。' }),
  });
  const state = await controller.open(target);
  assert.equal(requests, 0);
  assert.equal(state.sourceRecheck, true);
  assert.equal(state.retryable, true);
  assert.match(state.message, /无需重新检索/);
  assert.equal((await controller.generate(target)).status, 'ready');
  assert.equal(requests, 1);
});

test('citation dialog distinguishes access, temporary transport and evidence limits without internal codes', async () => {
  for (const [code, retryable, expected] of [
    ['CITATION_FULLTEXT_UNAVAILABLE', false, /没有找到可核验/],
    ['CITATION_SOURCE_UNAVAILABLE', true, /重试这一篇/],
    ['CITATION_INSUFFICIENT_EVIDENCE', false, /未找到足以支持/],
  ]) {
    const controller = new CitationController({
      lookup: async () => ({ preparing: false, record: { status: 'failed', failure: { code, retryable } } }),
      request: async () => { assert.fail('opening an error must not resubmit'); }, poll: async () => {},
    });
    const state = await controller.open({});
    assert.match(state.message, expected);
    assert.doesNotMatch(state.message, /CITATION_/);
    assert.equal(state.retryable, retryable);
  }
});

test('explicit generation remains available for unprepared older results', async () => {
  let requests = 0;
  let polls = 0;
  const controller = new CitationController({
    request: async () => {
      requests += 1;
      return { citation_text_id: 'ct-1', status: 'queued' };
    },
    poll: async () => {
      polls += 1;
      return {
        status: 'ready',
        citation_text: '（Smith, 2024）',
        formatted_citation: 'Smith J. Example study. 2024.',
        evidence: [{ quote: '原文片段', source_uri: 'https://example.org/paper', section_path: ['Results'], paragraph_index: 2, source_level: 'full_text' }],
        limitations: ['仅适用于所核对的研究范围'],
        source_level: 'full_text',
      };
    },
    wait: async () => {},
    maxAttempts: 2,
  });
  assert.equal(requests, 0, 'constructing the controller must not request citation text');
  assert.equal(polls, 0);
  const state = await controller.open({ queryId: 'q-1', resultId: 'r-1', resultVersion: 3, title: 'Example' });
  assert.equal(requests, 1);
  assert.equal(polls, 1);
  assert.equal(state.status, 'ready');
  assert.equal(state.data.formatted_citation, 'Smith J. Example study. 2024.');

  const body = citationRequestBody({ queryId: 'q-1', resultId: 'r-1', resultVersion: 3 });
  assert.deepEqual(body, {
    query_id: 'q-1', result_id: 'r-1', result_version: 3,
    citation_style: 'vancouver', language: 'zh',
  });
  assert.equal(Object.hasOwn(body, 'model'), false);
  const withModel = citationRequestBody({
    queryId: 'q-1', resultId: 'r-1', resultVersion: 0,
    model: { provider: 'moonshot', model: 'test', api_key: 'unit-test-placeholder' },
  });
  assert.equal(withModel.result_version, 0);
  assert.deepEqual(withModel.model, { provider: 'moonshot', model: 'test', api_key: 'unit-test-placeholder' });
});

test('UI-XSS-001: conversation and citation text remain inert text, never HTML', async () => {
  const payload = '<img src=x onerror="globalThis.__pwned=1">';
  const normalized = normalizeConversationMessages([{
    message_id: 'evil', role: 'assistant', content: payload, run_id: 'run-evil',
  }]);
  assert.equal(normalized[0].content, payload);
  const session = new AgentSession();
  session.applyConversation({
    conversation_id: 'conv-evil', revision: 1,
    messages: [{ message_id: 'evil', role: 'assistant', content: payload, run_id: 'run-evil', query_ids: [] }],
    runs: [],
  });
  assert.equal(session.chatMessages()[0].content, payload);
  assert.equal(failureMessage({ code: 'ERR_XSS', message: payload }), `ERR_XSS：${payload}`);
  for (const file of ['chat-view.js', 'citation-view.js', 'main.js']) {
    const source = await readFile(new URL(`../web/js/${file}`, import.meta.url), 'utf8');
    assert.doesNotMatch(source, /\.innerHTML|insertAdjacentHTML|outerHTML/, file);
  }
  const chatSource = await readFile(new URL('../web/js/chat-view.js', import.meta.url), 'utf8');
  assert.match(chatSource, /setText\(entry\.text, message\.content/);
});

test('UI-CITATION-005: citation failure stays explicit and does not fabricate text', async () => {
  const controller = new CitationController({
    request: async () => ({ citation_text_id: 'ct-failed', status: 'queued' }),
    poll: async () => ({
      status: 'failed',
      failure_code: 'CITATION_NO_FULL_TEXT',
      message: '缺少合法全文，不能生成引用建议。',
      limitations: ['仅核对到摘要'],
    }),
    wait: async () => {},
    maxAttempts: 2,
  });
  const state = await controller.open({ queryId: 'q-1', resultId: 'r-1', resultVersion: 1 });
  assert.equal(state.status, 'failed');
  assert.equal(state.code, 'CITATION_NO_FULL_TEXT');
  assert.match(state.message, /合法全文/);
  assert.deepEqual(state.limitations, ['仅核对到摘要']);
  assert.equal(Object.hasOwn(state, 'data'), false);
});

test('UI-AGENT-007: a cancelled run is an honest status and leaves no artifact', () => {
  const session = new AgentSession();
  session.applyRun({
    run_id: 'run-cancelled', conversation_id: 'conv-cancelled', status: 'cancelled',
    queries: [], error: null,
  }, libraries);
  const rows = session.chatMessages();
  assert.equal(rows.length, 1);
  assert.equal(rows[0].kind, 'status');
  assert.equal(rows[0].level, 'warn');
  assert.match(rows[0].content, /取消/);
  assert.equal(session.searches.size, 0);
});


test('citation facts keep only their actual source links and expose missing mappings', () => {
  const first = { evidence_id: 'ev-a', quote: 'Source A' };
  const second = { evidence_id: 'ev-b', quote: 'Source B' };
  const groups = citationFactEvidence({
    evidence: [first, second],
    facts: [
      { statement: 'First statement', evidence_ids: ['ev-a', 'ev-a'] },
      { statement: 'Second statement', evidence_ids: ['ev-b', 'ev-missing'] },
    ],
  });
  assert.deepEqual(groups[0], { statement: 'First statement', evidence: [first], missingEvidence: false });
  assert.deepEqual(groups[1], { statement: 'Second statement', evidence: [second], missingEvidence: true });
  assert.deepEqual(citationFactEvidence({ evidence: [first] }), []);
});

test('citation locators use one XML id or a human-readable paragraph index, never both', () => {
  assert.equal(citationLocatorLabel({ paragraph_id: 'Par15', paragraph_index: 14 }), '段落 Par15');
  assert.equal(citationLocatorLabel({ paragraph_index: 14 }), '段落 15');
  assert.equal(citationLocatorLabel({ paragraph_index: null }), '');
  assert.equal(citationLocatorLabel({}), '');
});

test('citation bibliography uses canonical CSL output and never relabels prose as a bibliography', () => {
  assert.equal(citationBibliography({ bibliography: { bibliography: 'Source Author. Real title.' }, citation_text: '来源支持的表述。' }), 'Source Author. Real title.');
  assert.equal(citationBibliography({ citation_text: '没有规范书目。' }), '');
  assert.equal(citationRequestBody({ queryId: 'q', resultId: 'r', resultVersion: 0 }).result_version, 0);
  assert.equal(citationRequestBody({ queryId: 'q', resultId: 'r', resultKind: 'recommendation' }).result_kind, 'recommendation');
});

test('citation polling exposes the canonical backend failure and cancellation instead of waiting forever', async () => {
  for (const data of [
    { status: 'failed', failure: { code: 'CITATION_FULLTEXT_UNAVAILABLE', message: '没有合法全文。' } },
    { status: 'cancelled' },
  ]) {
    let polls = 0;
    const controller = new CitationController({
      request: async () => ({ citation_text_id: 'ct-1' }),
      poll: async () => { polls += 1; return data; },
      wait: async () => {},
    });
    const state = await controller.open({ queryId: 'q', resultId: 'r' });
    assert.equal(state.status, 'failed');
    assert.equal(state.code, data.failure?.code || 'CITATION_CANCELLED');
    assert.equal(polls, 1);
  }
});
