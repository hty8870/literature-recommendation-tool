import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { mkdir, mkdtemp, readFile, rm, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';

const TERMINAL = new Set(['completed', 'failed', 'cancelled']);
const conversationStorageKey = 'citation.conversation-id.v1';

function usage() {
  return `Usage:
  node tools/browser_conversation_acceptance.mjs <loopback-base> [output-root] --query "..." [options]

Options:
  --query "..."                 Required first-turn research query for --scenario all.
  --public                      Explicitly allow only https://biodataagent.com/literature/.
  --public-pdf-sha256 <hash>     Required for public uploads; verifies the approved public input.
  --scenario all|fulltext|citation-layout|restore  Default: all.
  --restore-conversation <id>    Restore completed real work read-only; no new model calls.
  --citation-record <path>       Required for citation-layout; no new model calls.
  --viewports both|desktop|mobile  Default: both. Reports explicitly name the tested devices.
  --explain-query "..."         Follow-up asking the Agent to explain the old result. Defaults to a generic request.
  --modify-query "..."          Follow-up that narrows/changes the requirement and asks for retrieval.
  --pdf <path>                  Existing public test PDF/DOCX; required when full-text upload is supported and this viewport performs the real upload.
  --fulltext-viewport desktop|both  Default: desktop. The mobile target always checks the capability/UI without uploading unless "both".
  --overall-timeout-ms 900000   Hard overall budget. No wait may exceed it.
  --run-timeout-ms 900000       Per Agent run terminal wait (capped by the overall budget).
  --ui-timeout-ms 30000         Per DOM transition wait.
  --parse-timeout-ms 240000     Manuscript parse-state wait.
  --citation-timeout-ms 90000   Citation dialog terminal wait.
  --self-test                   Check pure local logic only; no browser, API, or model call.
  --help                        Show this text.

The script uses an isolated headless Edge profile and CDP; it never operates the daily browser.
Artifacts are written under <output-root>/run-<timestamp>-<id>/ and are never overwritten.`;
}

function isLoopbackBase(value) {
  try {
    const url = new URL(value);
    return ['127.0.0.1', 'localhost', '[::1]', '::1'].includes(url.hostname)
      && ['http:', 'https:'].includes(url.protocol);
  } catch {
    return false;
  }
}

function isPublicTestBase(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && url.hostname === 'biodataagent.com'
      && !url.port && !url.username && !url.password && !url.search && !url.hash
      && /^\/literature\/?$/.test(url.pathname);
  } catch { return false; }
}

function makeRunId() {
  return `run-${new Date().toISOString().replace(/[:.]/g, '-')}-${randomUUID().slice(0, 8)}`;
}

function sanitizeRequestSummary(body) {
  if (!body || typeof body !== 'object') return {};
  return {
    input_type: typeof body.input_type === 'string' ? body.input_type : null,
    keyword_count: Array.isArray(body.keywords) ? body.keywords.length : 0,
    abstract_length: typeof body.abstract === 'string' ? body.abstract.length : 0,
    manuscript_id: typeof body.manuscript_id === 'string' ? body.manuscript_id : null,
    library_ids: Array.isArray(body.library_ids)
      ? body.library_ids.filter((value) => typeof value === 'string').slice(0, 16) : [],
    has_claim_scope: Boolean(body.claim_scope),
    message_length: typeof body.message === 'string' ? body.message.length : 0,
    active_query_id: typeof body.active_query_id === 'string' ? body.active_query_id : null,
    query_id: typeof body.query_id === 'string' ? body.query_id : null,
    result_id: typeof body.result_id === 'string' ? body.result_id : null,
    result_version: Number.isInteger(body.result_version) ? body.result_version : null,
    citation_style: typeof body.citation_style === 'string' ? body.citation_style : null,
    language: typeof body.language === 'string' ? body.language : null,
    has_model: Boolean(body.model && typeof body.model === 'object'),
    model_provider: typeof body.model?.provider === 'string' ? body.model.provider : null,
    model_name: typeof body.model?.model === 'string' ? body.model.model : null,
  };
}

function sanitizeRunSummary(run) {
  const queries = Array.isArray(run?.queries) ? run.queries : [];
  return {
    run_id: typeof run?.run_id === 'string' ? run.run_id : null,
    status: typeof run?.status === 'string' ? run.status : null,
    turn_index: Number.isFinite(Number(run?.turn_index)) ? Number(run.turn_index) : null,
    conversation_id: typeof run?.conversation_id === 'string' ? run.conversation_id : null,
    latest_query_id: typeof run?.latest_query_id === 'string' ? run.latest_query_id : null,
    query_ids: queries.map((query) => query?.created?.query_id).filter((id) => typeof id === 'string'),
    query_count: queries.length,
    summary_length: typeof run?.summary === 'string' ? run.summary.length : 0,
    error_code: typeof run?.error?.code === 'string' ? run.error.code : null,
    updated_at: typeof run?.updated_at === 'string' ? run.updated_at : null,
  };
}

function sanitizeConversationSummary(body) {
  const messages = Array.isArray(body?.messages) ? body.messages : [];
  return {
    conversation_id: typeof body?.conversation_id === 'string' ? body.conversation_id : null,
    revision: Number.isFinite(Number(body?.revision)) ? Number(body.revision) : null,
    message_count: messages.length,
    messages: messages.map((message) => ({
      role: message?.role === 'user' ? 'user' : message?.role === 'assistant' ? 'assistant' : null,
      run_id: typeof message?.run_id === 'string' ? message.run_id : null,
      length: typeof message?.content === 'string'
        ? message.content.length : typeof message?.text === 'string' ? message.text.length : 0,
      query_id_count: Array.isArray(message?.query_ids) ? message.query_ids.length : 0,
    })),
    run_count: Array.isArray(body?.runs) ? body.runs.length : 0,
    turn_count: Array.isArray(body?.turns) ? body.turns.length : 0,
  };
}

function positiveInt(value, fallback, name) {
  if (value == null) return fallback;
  const parsed = Number(value);
  assert(Number.isFinite(parsed) && parsed > 0, `${name} must be a positive number`);
  return parsed;
}

function parseOptions(argv) {
  const args = [...argv];
  const publicIndex = args.indexOf('--public');
  const allowPublic = publicIndex >= 0;
  if (allowPublic) args.splice(publicIndex, 1);
  const take = (name, fallback = null) => {
    const index = args.indexOf(name);
    if (index < 0) return fallback;
    const value = args[index + 1];
    assert(value != null && !value.startsWith('--'), `${name} requires a value`);
    args.splice(index, 2);
    return value;
  };
  const options = {
    query: take('--query'),
    explainQuery: take('--explain-query',
      '请解释上一轮结果与原始需求的对应关系，并指出还需要核对原文的限定。'),
    modifyQuery: take('--modify-query',
      '请在上一轮基础上收窄研究范围，并重新检索比较相关研究。'),
    pdfPath: take('--pdf'),
    publicPdfSha256: take('--public-pdf-sha256'),
    citationRecord: take('--citation-record'),
    restoreConversation: take('--restore-conversation'),
    allowPublic,
    fulltextViewport: take('--fulltext-viewport', 'desktop'),
    scenario: take('--scenario', 'all'),
    viewports: take('--viewports', 'both'),
    overallTimeoutMs: positiveInt(take('--overall-timeout-ms'), 900000, '--overall-timeout-ms'),
    runTimeoutMs: positiveInt(take('--run-timeout-ms'), 900000, '--run-timeout-ms'),
    uiTimeoutMs: positiveInt(take('--ui-timeout-ms'), 30000, '--ui-timeout-ms'),
    parseTimeoutMs: positiveInt(take('--parse-timeout-ms'), 240000, '--parse-timeout-ms'),
    citationTimeoutMs: positiveInt(take('--citation-timeout-ms'), 90000, '--citation-timeout-ms'),
  };
  const unknown = args.filter((value) => value.startsWith('--'));
  assert.deepEqual(unknown, [], `Unknown options: ${unknown.join(', ')}`);
  assert(args.length <= 2, 'Too many positional arguments; expected <loopback-base> [output-root]');
  return { base: args[0], outputRoot: args[1], ...options };
}

function observerSource() {
  return `(() => {
    const sanitizeRequest = ${sanitizeRequestSummary.toString()};
    const sanitizeRun = ${sanitizeRunSummary.toString()};
    const sanitizeConversation = ${sanitizeConversationSummary.toString()};
    const original = window.fetch.bind(window);
    const acc = window.__acc = {
      errors: [],
      agentRequests: [],
      runs: [],
      conversation: null,
      conversationRequests: 0,
      citationRequests: [],
      citationResponses: [],
      manuscriptRequests: [],
      manuscriptResponses: [],
      tenant: null,
    };
    window.__accFetch = original;
    window.addEventListener('error', (event) => acc.errors.push(String(event.message || event.error || 'error')));
    window.addEventListener('unhandledrejection', (event) => acc.errors.push(String(event.reason?.message || event.reason || 'rejection')));
    const readJson = (response, handler) => {
      try {
        response.clone().json().then(handler).catch((error) => acc.errors.push('response observer: ' + error.name));
      } catch (error) {
        acc.errors.push('response clone: ' + error.name);
      }
    };
    window.fetch = async (...args) => {
      const request = typeof args[0] === 'string' ? null : args[0];
      const rawUrl = typeof args[0] === 'string' ? args[0] : request?.url || '';
      const url = String(rawUrl || '');
      const init = args[1] || {};
      const method = String(init.method || request?.method || 'GET').toUpperCase();
      const headers = new Headers(init.headers || request?.headers || {});
      const tenant = headers.get('X-Tenant-ID');
      if (tenant) acc.tenant = tenant;
      let body = null;
      if (typeof init.body === 'string') {
        try { body = JSON.parse(init.body); } catch { body = null; }
      }
      const requestPath = new URL(url, location.href).pathname;
      const turnCreation = requestPath.includes('/conversations/') && requestPath.endsWith('/turns');
      if ((url.includes('/agent/runs') || turnCreation) && method === 'POST') {
        acc.agentRequests.push({
          path: new URL(url, location.href).pathname,
          idempotency_key_present: Boolean(headers.get('Idempotency-Key')),
          ...sanitizeRequest(body),
        });
      }
      if (url.includes('/citation-texts') && method === 'POST') {
        acc.citationRequests.push({
          path: new URL(url, location.href).pathname,
          idempotency_key_present: Boolean(headers.get('Idempotency-Key')),
          ...sanitizeRequest(body),
        });
      }
      if (url.includes('/manuscripts') && method === 'POST') {
        acc.manuscriptRequests.push({
          path: new URL(url, location.href).pathname,
          filename: typeof init.body?.name === 'string' ? init.body.name : null,
          size: Number.isFinite(init.body?.size) ? init.body.size : null,
          content_type: headers.get('Content-Type'),
        });
      }
      if (url.includes('/conversations/') && method === 'GET') acc.conversationRequests += 1;
      const response = await original(...args);
      if (url.includes('/agent/runs') || turnCreation) {
        readJson(response, (data) => {
          const summary = sanitizeRun(data);
          if (!summary.run_id) return;
          const index = acc.runs.findIndex((item) => item.run_id === summary.run_id);
          if (index >= 0) acc.runs[index] = summary;
          else acc.runs.push(summary);
        });
      }
      if (url.includes('/citation-texts') || requestPath.endsWith('/citation-suggestion')) {
        readJson(response, (data) => {
          const lookup = requestPath.endsWith('/citation-suggestion');
          const record = lookup ? data?.record : data;
          acc.citationResponses.push({
            lookup,
            status: typeof record?.status === 'string' ? record.status : null,
            citation_text_id: typeof record?.citation_text_id === 'string' ? record.citation_text_id : null,
          });
        });
      }
      if (url.includes('/manuscripts') && (method === 'POST' || method === 'GET')) {
        readJson(response, (data) => {
          acc.manuscriptResponses.push({
            manuscript_id: typeof data?.manuscript_id === 'string' ? data.manuscript_id : null,
            parse_status: typeof data?.parse_status === 'string' ? data.parse_status : null,
          });
        });
      }
      if (url.includes('/conversations/') && method === 'GET') {
        readJson(response, (data) => { acc.conversation = sanitizeConversation(data); });
      }
      return response;
    };
  })();`;
}

function citationDisclaimer(text) {
  const needsAuthor = text.includes('需作者核对') || text.includes('需作者对照原文核查');
  const notCertified = text.includes('未升级为可引用全文认证')
    || text.includes('不表示全文已认证') || text.includes('不代表全文认证');
  return needsAuthor && notCertified;
}

function selfTest() {
  assert(citationDisclaimer('需作者核对的引用建议：未升级为可引用全文认证。'));
  assert(!citationDisclaimer('引用资格已通过，全文已认证。'));
  assert(!citationDisclaimer('需作者核对，但全文已认证。'));
  assert.doesNotThrow(() => new Function(observerSource()), 'Injected observer must compile as browser JavaScript');
  const parsed = parseOptions(['http://127.0.0.1:37302/', 'output', '--query', 'LFY2 soybean',
    '--explain-query', '解释', '--modify-query', '重新检索', '--pdf', 'fixture.pdf',
    '--overall-timeout-ms', '3600000', '--citation-timeout-ms', '300000']);
  assert.equal(parsed.base, 'http://127.0.0.1:37302/');
  const focused = parseOptions(['http://127.0.0.1:4291/', '--scenario', 'fulltext', '--viewports', 'mobile']);
  assert.equal(focused.scenario, 'fulltext');
  assert.equal(focused.viewports, 'mobile');
  assert.equal(sanitizeRequestSummary({ result_version: null }).result_version, null);
  assert.equal(sanitizeRequestSummary({ result_version: 0 }).result_version, 0);
  assert.equal(parsed.outputRoot, 'output');
  assert.equal(parsed.query, 'LFY2 soybean');
  assert.equal(parsed.pdfPath, 'fixture.pdf');
  assert.equal(parsed.overallTimeoutMs, 3600000);
  assert.throws(() => parseOptions(['http://127.0.0.1/', '--unexpected', 'value']), /Unknown options/);
  assert.equal(isLoopbackBase('http://127.0.0.1:5173/'), true);
  assert.equal(isLoopbackBase('http://localhost:8000/'), true);
  assert.equal(isLoopbackBase('https://example.com/'), false);
  assert.equal(isPublicTestBase('https://biodataagent.com/literature/'), true);
  assert.equal(isPublicTestBase('https://biodataagent.com.evil.example/literature/'), false);
  assert.equal(isPublicTestBase('http://biodataagent.com/literature/'), false);
  assert.equal(isPublicTestBase('https://biodataagent.com/other/'), false);
  assert.equal(parseOptions(['https://biodataagent.com/literature/', '--public']).allowPublic, true);
  const safe = sanitizeRequestSummary({
    input_type: 'keywords',
    keywords: ['alpha', 'beta'],
    library_ids: ['a', 'b'],
    model: { provider: 'moonshot', model: 'test-model', api_key: 'unit-test-secret' },
    message: 'message body',
  });
  const serialized = JSON.stringify(safe);
  assert.equal(serialized.includes('unit-test-secret'), false);
  assert.equal(safe.input_type, 'keywords');
  assert.equal(safe.keyword_count, 2);
  assert.equal(safe.has_model, true);
  assert.equal(safe.model_name, 'test-model');
  assert.equal(safe.message_length, 12);
  const citation = sanitizeRequestSummary({
    query_id: 'q-1', result_id: 'r-1', result_version: 2,
    citation_style: 'vancouver', language: 'zh',
    model: { provider: 'openai', model: 'test', api_key: 'secret-2' },
  });
  assert.equal(JSON.stringify(citation).includes('secret-2'), false);
  assert.equal(citation.citation_style, 'vancouver');
  assert.equal(citation.language, 'zh');
  assert.equal(citation.result_version, 2);
  const run = sanitizeRunSummary({
    run_id: 'run-1', status: 'running', turn_index: 1, conversation_id: 'conv-1',
    latest_query_id: 'q-1', queries: [{ created: { query_id: 'q-1' } }],
    summary: '普通说明', error: null, updated_at: '2026-01-01T00:00:00Z',
  });
  assert.deepEqual(run.query_ids, ['q-1']);
  assert.equal(run.summary_length, 4);
  const conversation = sanitizeConversationSummary({
    conversation_id: 'conv-1', revision: 3,
    messages: [{ role: 'user', content: 'x', run_id: 'run-1', query_ids: [] }],
    runs: [{ run_id: 'run-1' }],
  });
  assert.equal(conversation.message_count, 1);
  assert.equal(conversation.messages[0].length, 1);
  assert.equal(conversation.run_count, 1);
  assert.notEqual(makeRunId(), makeRunId());
  console.log(JSON.stringify({
    selfTest: 'passed',
    checks: ['loopback validation', 'request redaction', 'run/conversation projection', 'unique run id'],
  }, null, 2));
}

const rawArgs = process.argv.slice(2);
if (rawArgs.includes('--self-test')) {
  selfTest();
  process.exit(0);
}
if (rawArgs.includes('--help') || rawArgs.includes('-h')) {
  console.log(usage());
  process.exit(0);
}

const options = parseOptions(rawArgs);
const {
  base, outputRoot: outputRootArg, query, explainQuery, modifyQuery, pdfPath: pdfArg,
  fulltextViewport, scenario, viewports, citationRecord, allowPublic, restoreConversation, publicPdfSha256,
  overallTimeoutMs, runTimeoutMs, uiTimeoutMs, parseTimeoutMs, citationTimeoutMs,
} = options;
assert(base && (isLoopbackBase(base) || (allowPublic && isPublicTestBase(base))),
  'Use loopback, or --public with the explicitly allowed production literature URL');
if (allowPublic && pdfArg) assert(/^[a-f0-9]{64}$/.test(publicPdfSha256 || ''),
  'Public upload requires an explicit approved public-material SHA-256');
assert(['all', 'fulltext', 'citation-layout', 'restore'].includes(scenario), 'Invalid --scenario');
if (scenario === 'restore') assert(/^cnv_[a-f0-9]{32}$/.test(restoreConversation || ''),
  'Restore requires an actual --restore-conversation id');
let replayCitation = null;
if (scenario === 'citation-layout') {
  assert(citationRecord, '--citation-record is required');
  const saved = JSON.parse(await readFile(citationRecord, 'utf8'));
  assert(saved.public_test_data_only === true, 'Citation layout replay requires public data');
  replayCitation = saved.record;
  assert(replayCitation?.status === 'ready', 'Saved citation must be ready');
}
assert(['both', 'desktop', 'mobile'].includes(viewports), '--viewports must be both, desktop or mobile');
if (scenario === 'all') assert(typeof query === 'string' && query.trim(), '--query is required');
assert(['desktop', 'both'].includes(fulltextViewport), '--fulltext-viewport must be desktop or both');
assert(typeof explainQuery === 'string' && explainQuery.trim(), '--explain-query is empty');
assert(typeof modifyQuery === 'string' && modifyQuery.trim(), '--modify-query is empty');

const outputRoot = path.resolve(outputRootArg || 'data/runtime/conversation-browser-acceptance');
const runId = makeRunId();
const output = path.join(outputRoot, runId);
const overallDeadline = Date.now() + overallTimeoutMs;
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const pdfPath = pdfArg ? path.resolve(pdfArg) : null;
let pdfInfo = null;
if (pdfPath) {
  pdfInfo = await stat(pdfPath);
  assert(pdfInfo.isFile() && pdfInfo.size > 0, `--pdf is not a non-empty file: ${pdfPath}`);
  if (allowPublic) assert.equal(createHash('sha256').update(await readFile(pdfPath)).digest('hex'),
    publicPdfSha256, 'Public upload input differs from the approved public-material hash');
}
await mkdir(output, { recursive: true });
const config = {
  query,
  explain_query: explainQuery,
  modify_query: modifyQuery,
  fulltext_viewport: fulltextViewport,
  scenario,
  viewports,
  pdf: pdfPath ? { name: path.basename(pdfPath), bytes: pdfInfo.size } : null,
  overall_timeout_ms: overallTimeoutMs,
  run_timeout_ms: runTimeoutMs,
  ui_timeout_ms: uiTimeoutMs,
  parse_timeout_ms: parseTimeoutMs,
  citation_timeout_ms: citationTimeoutMs,
};
await writeFile(path.join(output, 'config.json'), JSON.stringify({
  base, output_root: outputRoot, output, run_id: runId, config,
}, null, 2));

const executable = process.env.EDGE_EXECUTABLE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const profile = await mkdtemp(path.join(output, '.edge-profile-'));
const browser = spawn(executable, ['--headless=new', '--disable-gpu', '--disable-extensions',
  '--disable-features=Translate', '--no-first-run', '--no-default-browser-check',
  '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
  `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore' });

let socket;
let serial = 0;
let activeSession = null;
let activeLabel = null;
let currentPhase = 'startup';
const pending = new Map();
const checks = [];
const skips = [];

async function waitFor(read, label, timeout = uiTimeoutMs, interval = 150) {
  const deadline = Math.min(Date.now() + timeout, overallDeadline);
  while (Date.now() < deadline) {
    const value = await read();
    if (value) return value;
    await pause(interval);
  }
  throw new Error(`Timed out: ${label}${Date.now() >= overallDeadline ? ' (overall budget exhausted)' : ''}`);
}

function send(method, params = {}, sessionId) {
  return new Promise((resolve, reject) => {
    const id = ++serial;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`CDP timeout: ${method}`));
    }, 30000);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  });
}

async function evaluate(sessionId, expression) {
  const result = await send('Runtime.evaluate', {
    expression,
    awaitPromise: true,
    returnByValue: true,
    userGesture: true,
  }, sessionId);
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.exception?.description
      || result.exceptionDetails.text || 'Runtime exception');
  }
  return result.result?.value;
}

async function screenshot(sessionId, name) {
  const image = await send('Page.captureScreenshot', { format: 'png' }, sessionId);
  await writeFile(path.join(output, `${name}.png`), Buffer.from(image.data, 'base64'));
}

async function waitForExpression(sessionId, expression, label, timeout = uiTimeoutMs) {
  return waitFor(() => evaluate(sessionId, expression), label, timeout, 150);
}

async function dispatchPointer(sessionId, rect, mobile) {
  if (mobile) {
    await send('Input.dispatchTouchEvent', {
      type: 'touchStart', touchPoints: [{ ...rect, radiusX: 1, radiusY: 1 }],
    }, sessionId);
    await send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] }, sessionId);
  } else {
    await send('Input.dispatchMouseEvent', { type: 'mousePressed', ...rect, button: 'left', clickCount: 1 }, sessionId);
    await send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...rect, button: 'left', clickCount: 1 }, sessionId);
  }
  await pause(80);
}

async function click(sessionId, selector, mobile) {
  const rect = await evaluate(sessionId, `(() => {
    const node = document.querySelector(${JSON.stringify(selector)});
    if (!node) return { error: 'not found' };
    node.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
    const box = node.getBoundingClientRect();
    if (!box.width || !box.height) return { error: 'not visible' };
    if (node.disabled) return { error: 'disabled' };
    return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  })()`);
  if (rect.error) throw new Error(`Cannot click ${selector}: ${rect.error}`);
  await dispatchPointer(sessionId, rect, mobile);
}

async function typeInto(sessionId, selector, text) {
  await evaluate(sessionId, `(() => {
    const node = document.querySelector(${JSON.stringify(selector)});
    if (!node) return false;
    node.focus();
    node.value = ${JSON.stringify(text)};
    node.dispatchEvent(new Event('input', { bubbles: true }));
    return true;
  })()`);
}

async function ensurePane(sessionId, pane, mobile) {
  if (!mobile) return;
  const current = await evaluate(sessionId, `document.querySelector('#search-workspace').dataset.pane`);
  if (current !== pane) await click(sessionId, `#pane-${pane}`, true);
  await waitFor(() => evaluate(sessionId, pane === 'chat'
    ? `getComputedStyle(document.querySelector('#chat-pane')).display!=='none'`
    : `getComputedStyle(document.querySelector('#results-pane')).display!=='none'`),
  `${pane} pane`, uiTimeoutMs);
}

async function activeQuery(sessionId) {
  return evaluate(sessionId, `(() => {
    const tab = document.querySelector('#attempt-tabs button[aria-pressed="true"]');
    if (tab) return tab.dataset.queryId;
    const pill = document.querySelector('#chat-log .chat-pill[aria-pressed="true"]');
    return pill?.dataset.queryId || null;
  })()`);
}

async function clickQuery(sessionId, queryId, mobile, retried = false) {
  const target = await evaluate(sessionId, `(() => {
    const wanted = ${JSON.stringify(queryId)};
    const nodes = [...document.querySelectorAll('#chat-log .chat-pill[data-query-id], #attempt-tabs button[data-query-id]')]
      .filter((node) => node.dataset.queryId === wanted);
    const visible = nodes.find((node) => node.getBoundingClientRect().width > 0);
    const node = visible || nodes[0];
    if (!node) return { error: 'query button missing', hasPill: false, hasTab: false };
    const hasPill = !!document.querySelector('#chat-log .chat-pill[data-query-id]');
    const hasTab = !!document.querySelector('#attempt-tabs button[data-query-id]');
    if (!visible) return { error: 'hidden', hasPill, hasTab };
    node.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
    const box = node.getBoundingClientRect();
    return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  })()`);
  if (target.error) {
    if (retried) throw new Error(`Query button not found or not clickable: ${queryId} (${target.error})`);
    if (target.hasPill) await ensurePane(sessionId, 'chat', mobile);
    else if (target.hasTab) await ensurePane(sessionId, 'results', mobile);
    else throw new Error(`Query button not found: ${queryId}`);
    return clickQuery(sessionId, queryId, mobile, true);
  }
  await dispatchPointer(sessionId, target, mobile);
}

async function waitForActiveQuery(sessionId, queryId, label) {
  await waitForExpression(sessionId, `(() => {
    const wanted = ${JSON.stringify(queryId)};
    return [...document.querySelectorAll('#attempt-tabs button[data-query-id], #chat-log .chat-pill[data-query-id]')]
      .some((node) => node.dataset.queryId === wanted && node.getAttribute('aria-pressed') === 'true');
  })()`, label, uiTimeoutMs);
}

async function conversationRefresh(sessionId, label) {
  const before = await evaluate(sessionId, `window.__acc.conversationRequests`);
  await waitFor(async () => {
    await evaluate(sessionId, `window.dispatchEvent(new Event('focus')); true;`);
    return evaluate(sessionId, `window.__acc.conversationRequests > ${before}`);
  }, label, uiTimeoutMs, 500);
  await pause(350);
}

async function assertNoRuntimeErrors(sessionId, label) {
  const errors = await evaluate(sessionId, `window.__acc.errors`);
  assert.deepEqual(errors, [], `${label}: ${JSON.stringify(errors)}`);
}

async function runIds(sessionId) {
  return evaluate(sessionId, `window.__acc.runs.map((run) => run.run_id)`);
}

async function latestNewRun(sessionId, knownIds, label, timeout = runTimeoutMs) {
  return waitFor(async () => {
    const state = await evaluate(sessionId, `(() => {
      const known = new Set(${JSON.stringify(knownIds)});
      const run = window.__acc.runs.find((item) => !known.has(item.run_id));
      if (run) return { kind: 'run', run };
      if (!document.querySelector('#login-view').hidden) return { kind: 'auth', text: 'login required' };
      const home = document.querySelector('#search-home');
      const homeError = document.querySelector('#form-error');
      if (home && !home.hidden && homeError && !homeError.hidden) {
        return { kind: 'home-error', text: homeError.textContent };
      }
      const failure = document.querySelector('#chat-log .chat-failure__text');
      if (failure?.textContent.trim() && failure.getClientRects().length) {
        return { kind: 'send-failed', text: failure.textContent };
      }
      const fatal = document.querySelector('#fatal-error');
      if (fatal && !fatal.hidden) return { kind: 'fatal', text: fatal.textContent };
      return null;
    })()`);
    if (!state) return null;
    if (state.kind === 'run') return state.run;
    throw new Error(`Send did not create a run (${state.kind}): ${state.text}`);
  }, label, timeout, 250);
}

async function fetchRun(sessionId, runId) {
  const result = await evaluate(sessionId, `(async () => {
    const headers = window.__acc.tenant ? { 'X-Tenant-ID': window.__acc.tenant } : {};
    const response = await window.__accFetch(new URL('api/v1/agent/runs/' + ${JSON.stringify(runId)}, location.href), {
      headers, cache: 'no-store', credentials: 'same-origin',
    });
    const text = await response.text();
    let body = null;
    try { body = text ? JSON.parse(text) : null; } catch { body = null; }
    return { ok: response.ok, status: response.status, body };
  })()`);
  if (!result?.ok) throw new Error(`GET Agent run ${runId} failed: HTTP ${result?.status}`);
  return result.body;
}

async function waitForRunTerminal(sessionId, run, label, timeout = runTimeoutMs) {
  return waitFor(async () => {
    const fresh = await fetchRun(sessionId, run.run_id);
    if (fresh && TERMINAL.has(fresh.status)) return fresh;
    return null;
  }, label, timeout, 900);
}

async function waitForAssistant(sessionId, minimum, label) {
  await waitForExpression(sessionId, `document.querySelectorAll('#chat-log .chat-turn--assistant:not(.chat-turn--progress):not(.is-pending)').length >= ${minimum}`, 
    label, uiTimeoutMs);
}

function queryIds(run) {
  return (run.queries || []).map((query) => query.created?.query_id).filter(Boolean);
}

async function waitForNewQuery(sessionId, run, knownIds, label, timeout = runTimeoutMs) {
  return waitFor(async () => {
    const fresh = await fetchRun(sessionId, run.run_id);
    const ids = queryIds(fresh);
    const added = ids.filter((id) => !knownIds.includes(id));
    if (added.length) return { run: fresh, added };
    if (TERMINAL.has(fresh.status)) return { run: fresh, added: [] };
    return null;
  }, label, timeout, 900);
}

async function waitForResults(sessionId, label) {
  await waitForExpression(sessionId, `(() => {
    const cards = document.querySelectorAll('#results .result-card').length;
    const empty = document.querySelector('#results .empty-state');
    const title = empty?.querySelector('strong')?.textContent || '';
    return cards > 0 || (empty && !empty.hidden && title && !title.includes('正在启动本轮'));
  })()`, label, uiTimeoutMs);
}

async function artifactState(sessionId) {
  return evaluate(sessionId, `(() => {
    const cards = [...document.querySelectorAll('#results .result-card[data-result-id]')];
    const empty = document.querySelector('#results .empty-state');
    return {
      active_query: (() => {
        const tab = document.querySelector('#attempt-tabs button[aria-pressed="true"]');
        if (tab) return tab.dataset.queryId;
        const pill = document.querySelector('#chat-log .chat-pill[aria-pressed="true"]');
        return pill?.dataset.queryId || null;
      })(),
      result_count: cards.length,
      result_ids: cards.map((node) => node.dataset.resultId),
      titles: cards.slice(0, 4).map((node) => node.querySelector('.result-card__title')?.textContent || ''),
      empty_title: empty && !empty.hidden ? empty.querySelector('strong')?.textContent || '' : '',
      attempt_tabs: document.querySelectorAll('#attempt-tabs button[data-query-id]').length,
      chat_assistant_turns: document.querySelectorAll('#chat-log .chat-turn--assistant').length,
    };
  })()`);
}

async function setManuscriptFile(sessionId) {
  await send('DOM.enable', {}, sessionId);
  const { root } = await send('DOM.getDocument', { depth: -1 }, sessionId);
  const { nodeId } = await send('DOM.querySelector', {
    nodeId: root.nodeId, selector: '#manuscript-file',
  }, sessionId);
  assert(nodeId, 'manuscript file input not found');
  await send('DOM.setFileInputFiles', { files: [pdfPath], nodeId }, sessionId);
  await pause(900);
  const state = await evaluate(sessionId, `({
    meta: document.querySelector('#file-meta')?.textContent || '',
    label: document.querySelector('#upload-label')?.textContent || '',
  })`);
  if (!state.meta.includes('上传') && !state.meta.includes('解析') && !state.meta.includes('排队')) {
    await evaluate(sessionId, `document.querySelector('#manuscript-file').dispatchEvent(new Event('change', { bubbles: true })); true;`);
  }
}

async function attemptCitation(sessionId, label, mobile, knownQueryIds) {
  currentPhase = 'citation';
  await ensurePane(sessionId, 'results', mobile);
  await waitForResults(sessionId, `${label} citation artifact`);
  let button = await evaluate(sessionId, `document.querySelectorAll('[data-action="citation-text"]').length`);
  let selectedQuery = await activeQuery(sessionId);
  if (!button) {
    for (const queryId of [...new Set(knownQueryIds)].reverse()) {
      await clickQuery(sessionId, queryId, mobile);
      await waitForActiveQuery(sessionId, queryId, `${label} citation query`);
      await waitForResults(sessionId, `${label} citation old artifact`);
      button = await evaluate(sessionId, `document.querySelectorAll('[data-action="citation-text"]').length`);
      selectedQuery = queryId;
      if (button) break;
    }
  }
  if (!button) {
    return { opened: false, reason: 'no eligible result card with a citation button' };
  }
  const before = await evaluate(sessionId, `window.__acc.citationRequests.length`);
  await click(sessionId, '[data-action="citation-text"]', mobile);
  await waitForExpression(sessionId, `document.querySelector('#citation-dialog').open`, `${label} citation dialog`, uiTimeoutMs);
  const readTerminal = () => evaluate(sessionId, `(() => {
    const body = document.querySelector('#citation-dialog-body');
    if (!body || body.getAttribute('aria-busy') === 'true') return null;
    const state = body.dataset.status;
    const ready = window.__acc.citationResponses.at(-1)?.status === 'ready'
      && !!body.querySelector('.citation-output');
    const timedOut = state === 'timeout' || !!body.querySelector('.citation-error[data-level="warn"]');
    const failed = state === 'failed' || !!body.querySelector('.citation-error[data-level="error"]');
    const unprepared = state === 'unprepared';
    return ready || failed || timedOut || unprepared
      ? { text: body.innerText || '', ready, failed, timedOut, unprepared } : null;
  })()`);
  let terminal = await waitFor(readTerminal, `${label} citation terminal`, citationTimeoutMs, 500);
  const usedLookup = await evaluate(sessionId, `window.__acc.citationResponses.some((item) => item.lookup)`);
  if (usedLookup) {
    const posts = await evaluate(sessionId, `window.__acc.citationRequests.length`);
    assert.equal(posts, before, `${label}: opening a suggestion must not create one`);
  }
  if (terminal.unprepared) {
    await click(sessionId, '[data-action="citation-retry"]', mobile);
    terminal = await waitFor(readTerminal, `${label} manual citation terminal`, citationTimeoutMs, 500);
  }
  const request = await evaluate(sessionId,
    `window.__acc.citationRequests.length > ${before} ? window.__acc.citationRequests.at(-1) : null`);
  const response = await evaluate(sessionId, `window.__acc.citationResponses.at(-1)`);
  const details = await evaluate(sessionId, `(() => {
    const body = document.querySelector('#citation-dialog-body');
    const text = body?.innerText || '';
    return {
      text_length: text.length,
      evidence_links: body?.querySelectorAll('a[href^="https://"], a[href^="http://"]').length || 0,
      has_disclaimer: (${citationDisclaimer.toString()})(body?.querySelector('.dialog-note')?.textContent || ''),
      draft_length: body?.querySelector('.citation-output__draft')?.value.trim().length || 0,
      body_images: body?.querySelectorAll('img').length || 0,
      request_count: window.__acc.citationRequests.length,
    };
  })()`);
  const status = terminal.ready ? 'ready' : terminal.timedOut ? 'timeout' : terminal.failed ? 'failed' : 'timeout';
  if (status === 'ready') {
    assert.equal(details.has_disclaimer, true, `${label}: citation disclaimer missing`);
    assert(details.draft_length > 0, `${label}: generated citation text is empty`);
    assert(details.evidence_links > 0, `${label}: source evidence link missing`);
  }
  assert(details.body_images === 0, `${label}: citation dialog parsed untrusted HTML into images`);
  assert(request || usedLookup, `${label}: neither a citation lookup nor generation was observed`);
  if (request) {
    assert.equal(request.idempotency_key_present, true, `${label}: citation Idempotency-Key missing`);
    assert.equal(request.citation_style, 'vancouver', `${label}: citation style not sent`);
    assert.equal(request.language, 'zh', `${label}: citation language not sent`);
    assert(Number.isInteger(request.result_version) && request.result_version >= 0,
      `${label}: citation result version missing`);
    assert(request.result_id, `${label}: citation result id missing`);
  }
  const layout = await citationLayout(sessionId, label);
  await screenshot(sessionId, `${label}-citation`);
  await click(sessionId, '#close-citation-dialog', mobile);
  if (status === 'timeout') skips.push(`${label}: citation dialog reached its finite UI timeout`);
  return {
    opened: true,
    selected_query: selectedQuery,
    status,
    request,
    response,
    layout,
    ...details,
  };
}

async function citationLayout(sessionId, label) {
  await pause(250);
  const geometry = await evaluate(sessionId, `(() => {
    const dialog = document.querySelector('#citation-dialog');
    const body = document.querySelector('#citation-dialog-body');
    const close = document.querySelector('#close-citation-dialog');
    const box = dialog.getBoundingClientRect();
    const within = (node) => {
      const rect = node.getBoundingClientRect();
      return rect.left >= box.left - 1 && rect.right <= box.right + 1;
    };
    return {
      dialog_width: dialog.clientWidth,
      dialog_scroll_width: dialog.scrollWidth,
      dialog_scroll_left: dialog.scrollLeft,
      body_scroll_width: body.scrollWidth,
      body_width: body.clientWidth,
      close_visible: within(close),
      content_within: [...body.children].every(within),
    };
  })()`);
  assert.equal(geometry.dialog_scroll_left, 0, `${label}: citation dialog scrolled sideways`);
  assert(geometry.dialog_scroll_width <= geometry.dialog_width + 1, `${label}: dialog overflow`);
  assert(geometry.body_scroll_width <= geometry.body_width + 1, `${label}: citation body overflow`);
  assert(geometry.close_visible && geometry.content_within, `${label}: citation controls clipped`);
  return geometry;
}

async function fullTextCheck(sessionId, label, mobile, shouldUpload) {
  currentPhase = 'fulltext';
  const onHome = await evaluate(sessionId, `!document.querySelector('#search-home').hidden`);
  if (!onHome) {
    await click(sessionId, '#new-search-button', mobile);
    if (await evaluate(sessionId, `document.querySelector('#new-search-button').getAttribute('aria-expanded') === 'true'`)) {
      await click(sessionId, '#history-new', mobile);
    }
  }
  await waitForExpression(sessionId, `!document.querySelector('#search-home').hidden`, `${label} home for fulltext`, uiTimeoutMs);
  await click(sessionId, '#mode-summary', mobile);
  const capability = await evaluate(sessionId, `({
    mode_disabled: document.querySelector('button[data-mode="full_text"]').disabled,
    input_disabled: document.querySelector('#manuscript-file').disabled,
  })`);
  if (capability.input_disabled) {
    return { supported: false, actual_upload: false };
  }
  assert.equal(capability.mode_disabled, false);
  await click(sessionId, '[data-mode="full_text"]', mobile);
  const ui = await evaluate(sessionId, `({
    note_visible: !document.querySelector('#full-text-note').hidden,
    upload_visible: !document.querySelector('#upload-control').hidden,
    text_input_hidden: document.querySelector('#text-input-wrap').hidden,
    start_disabled: document.querySelector('#start-button').disabled,
    note: document.querySelector('#full-text-note').textContent,
  })`);
  assert.equal(ui.note_visible, true);
  assert.equal(ui.upload_visible, true);
  assert.equal(ui.text_input_hidden, true);
  assert.equal(ui.start_disabled, true);
  assert.match(ui.note, /匿名实例|模型/);
  if (!shouldUpload) {
    await screenshot(sessionId, `${label}-fulltext-capability`);
    return { supported: true, actual_upload: false };
  }
  assert(pdfPath, `FULLTEXT_PDF_REQUIRED: ${label} service supports full text; pass --pdf`);
  await setManuscriptFile(sessionId);
  const parsed = await waitFor(() => evaluate(sessionId, `(() => {
    const meta = document.querySelector('#file-meta')?.textContent || '';
    const error = document.querySelector('#form-error');
    if (error && !error.hidden) return { state: 'error', text: error.textContent, meta };
    if (meta.includes('解析完成')) return { state: 'ready', meta, status: document.querySelector('#manuscript-status')?.textContent || '' };
    if (meta.includes('解析失败')) return { state: 'failed', meta };
    return null;
  })()`), `${label} manuscript parse`, parseTimeoutMs, 500);
  if (parsed.state !== 'ready') {
    throw new Error(`Manuscript parse ${parsed.state}: ${parsed.text || parsed.meta}`);
  }
  const startEnabled = await evaluate(sessionId, `!document.querySelector('#start-button').disabled`);
  assert.equal(startEnabled, true, `${label}: manuscript parsed but start is disabled`);
  await waitForExpression(sessionId, `window.__acc.manuscriptResponses.some((item) => item.parse_status === 'ready')`,
    `${label} observed manuscript ready`, uiTimeoutMs);
  await screenshot(sessionId, `${label}-fulltext-ready`);
  const known = await runIds(sessionId);
  await click(sessionId, '#start-button', mobile);
  const run = await latestNewRun(sessionId, known, `${label} fulltext run`);
  const terminalRun = await waitForRunTerminal(sessionId, run, `${label} fulltext terminal`);
  assert.equal(terminalRun.status, 'completed', `${label}: fulltext run ${terminalRun.status}`);
  assert(queryIds(terminalRun).length > 0, `${label}: FULLTEXT_NO_QUERY`);
  const bodyIndices = [...new Set((terminalRun.manuscript_reads || [])
    .flatMap((read) => read.passage_indices || []))];
  const anchoredClaims = (terminalRun.queries || []).flatMap((item) => item.created?.claims || [])
    .filter((claim) => claim.manuscript_anchor?.manuscript_id === terminalRun.request.manuscript_id);
  assert(bodyIndices.length || anchoredClaims.length, `${label}: FULLTEXT_BODY_NOT_READ`);
  await waitForAssistant(sessionId, 1, `${label} fulltext assistant`);
  await waitForExpression(sessionId, `document.querySelector('#cancel-run').hidden`,
    `${label} fulltext terminal UI`, uiTimeoutMs);
  const observed = await evaluate(sessionId, `({
    request: window.__acc.agentRequests.at(-1),
    manuscript_request: window.__acc.manuscriptRequests.at(-1),
    manuscript_response: window.__acc.manuscriptResponses.find((item) => item.parse_status === 'ready')
      || window.__acc.manuscriptResponses.at(-1),
    conversation: window.__acc.conversation,
    chat_assistant_turns: document.querySelectorAll('#chat-log .chat-turn--assistant').length,
  })`);
  assert.equal(observed.request?.input_type, 'full_text', `${label}: fulltext payload missing input_type`);
  assert(observed.request?.manuscript_id, `${label}: fulltext payload missing manuscript_id`);
  assert.equal(observed.request?.abstract_length, 0, `${label}: fulltext payload faked an abstract`);
  assert.equal(observed.request?.keyword_count, 0, `${label}: fulltext payload faked keywords`);
  assert.equal(observed.request?.has_claim_scope, false, `${label}: fulltext payload received a hidden demo scope`);
  assert(observed.request?.library_ids?.length > 0, `${label}: fulltext payload missing libraries`);
  assert.equal(observed.manuscript_request?.filename, path.basename(pdfPath));
  assert.equal(observed.manuscript_request?.size, pdfInfo.size);
  assert(['application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document']
    .includes(observed.manuscript_request?.content_type), `${label}: unexpected manuscript content type`);
  assert.equal(observed.manuscript_response?.parse_status, 'ready');
  assert.equal(observed.manuscript_response?.manuscript_id, observed.request?.manuscript_id);
  await screenshot(sessionId, `${label}-fulltext`);
  return {
    supported: true,
    actual_upload: true,
    parse_meta: parsed.meta,
    parse_status: parsed.status,
    manuscript_request: observed.manuscript_request,
    manuscript_response: observed.manuscript_response,
    payload: observed.request,
    run_status: terminalRun.status,
    conversation_id: terminalRun.conversation_id,
    query_count: queryIds(terminalRun).length,
    body_passage_indices: bodyIndices,
    anchored_claim_count: anchoredClaims.length,
    document_passage_count: terminalRun.context_manifest?.manuscript?.passage_count || null,
    catalog_read_count: (terminalRun.manuscript_reads || []).filter((read) => read.view === 'catalog').length,
  };
}

try {
  const port = await waitFor(async () => {
    try { return (await readFile(path.join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]; }
    catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  }, 'isolated Edge startup', 20000, 200);
  const version = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
  socket = new WebSocket(version.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', reject, { once: true });
  });
  socket.addEventListener('message', ({ data }) => {
    const message = JSON.parse(data);
    const entry = pending.get(message.id);
    if (!entry) return;
    clearTimeout(entry.timer);
    pending.delete(message.id);
    if (message.error) entry.reject(new Error(message.error.message));
    else entry.resolve(message.result);
  });

  for (const [label, width, height, mobile] of [['desktop', 1705, 984, false], ['mobile', 390, 844, true]]) {
    if (viewports !== 'both' && viewports !== label) continue;
    currentPhase = `${label}:open`;
    activeLabel = label;
    const target = await send('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await send('Target.attachToTarget', { targetId: target.targetId, flatten: true });
    activeSession = sessionId;
    await send('Page.enable', {}, sessionId);
    await send('Runtime.enable', {}, sessionId);
    await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile }, sessionId);
    if (mobile) await send('Emulation.setTouchEmulationEnabled', { enabled: true }, sessionId);
    await send('Page.addScriptToEvaluateOnNewDocument', { source: observerSource() }, sessionId);
    if (scenario === 'restore') {
      await send('Page.addScriptToEvaluateOnNewDocument', {
        source: `sessionStorage.setItem(${JSON.stringify(conversationStorageKey)}, ${JSON.stringify(restoreConversation)});`,
      }, sessionId);
    }
    await send('Page.navigate', { url: base }, sessionId);
    await waitForExpression(sessionId,
      scenario === 'restore'
        ? `!document.querySelector('#main-content').hidden && !document.querySelector('#search-workspace').hidden`
        : `!document.querySelector('#main-content').hidden && !document.querySelector('#search-home').hidden && document.querySelectorAll('#library-list .source-pill').length>0`,
      `${label} initial view`, uiTimeoutMs);
    if (scenario === 'restore') {
      currentPhase = `${label}:restore-terminal`;
      await waitForAssistant(sessionId, 1, `${label} restored actual assistant`);
      await waitForExpression(sessionId, `document.querySelector('#cancel-run').hidden`,
        `${label} restored terminal UI`, uiTimeoutMs);
      await screenshot(sessionId, `${label}-restored-chat`);
      await ensurePane(sessionId, 'results', mobile);
      await waitForResults(sessionId, `${label} restored results`);
      const artifact = await artifactState(sessionId);
      assert(artifact.result_count > 0, `${label}: restored artifact is empty`);
      assert.equal(await evaluate(sessionId, `window.__acc.agentRequests.length`), 0);
      await assertNoRuntimeErrors(sessionId, label);
      await screenshot(sessionId, `${label}-restored-results`);
      checks.push({ label, viewport: { width, height, mobile }, conversation_id: restoreConversation,
        restored_artifact: artifact, new_model_requests: 0,
        boundary: 'Read-only restoration of already completed real work, not a fresh model run' });
      await send('Target.closeTarget', { targetId: target.targetId });
      continue;
    }
    await screenshot(sessionId, `${label}-home`);
    if (scenario === 'citation-layout') {
      currentPhase = `${label}:citation-layout-replay`;
      await evaluate(sessionId, `(async () => {
        const { CitationDialog } = await import('./js/citation-view.js');
        const record = ${JSON.stringify(replayCitation)};
        const dialog = new CitationDialog({
          refs: {
            dialog: document.querySelector('#citation-dialog'),
            title: document.querySelector('#citation-dialog-title'),
            body: document.querySelector('#citation-dialog-body'),
            close: document.querySelector('#close-citation-dialog'),
          },
          request: async () => record,
          poll: async () => record,
          copy: async () => { throw new Error('Layout replay does not write the clipboard'); },
        });
        dialog.open({ title: record.bibliography.csl_item.title });
        window.__citationLayoutReplay = dialog;
      })()`);
      await waitForExpression(sessionId, `!!document.querySelector('.citation-output__draft')`,
        `${label} replay rendered`, uiTimeoutMs);
      const layout = await citationLayout(sessionId, label);
      await screenshot(sessionId, `${label}-citation-layout`);
      await assertNoRuntimeErrors(sessionId, label);
      checks.push({ label, viewport: { width, height, mobile }, layout,
        boundary: 'Saved public citation replay; no new generation or clipboard verification' });
      await send('Target.closeTarget', { targetId: target.targetId });
      continue;
    }
    if (scenario === 'fulltext') {
      const fulltext = await fullTextCheck(sessionId, label, mobile, true);
      await assertNoRuntimeErrors(sessionId, label);
      const check = { label, viewport: { width, height, mobile }, fulltext, runtime_errors: [] };
      checks.push(check);
      await writeFile(path.join(output, `${label}.json`), JSON.stringify(check, null, 2));
      await send('Target.closeTarget', { targetId: target.targetId });
      continue;
    }

    currentPhase = `${label}:first-turn`;
    await typeInto(sessionId, '#keywords', query);
    let knownRuns = await runIds(sessionId);
    await click(sessionId, '#start-button', mobile);
    let run = await latestNewRun(sessionId, knownRuns, `${label} first run`);
    let terminal = await waitForRunTerminal(sessionId, run, `${label} first terminal`);
    assert.equal(terminal.status, 'completed', `${label}: first run ${terminal.status} ${terminal.error?.code || ''}`);
    const firstQueries = queryIds(terminal);
    assert(firstQueries.length > 0, `${label}: FIRST_TURN_NO_QUERY; cannot test old artifacts`);
    const conversationId = terminal.conversation_id
      || await waitForExpression(sessionId, `window.__acc.conversation?.conversation_id || null`, `${label} conversation id`, uiTimeoutMs);
    assert(conversationId, `${label}: missing conversation_id`);
    assert.equal(await evaluate(sessionId, `sessionStorage.getItem(${JSON.stringify(conversationStorageKey)})`), conversationId);
    await waitForAssistant(sessionId, 1, `${label} first assistant`);
    await ensurePane(sessionId, 'results', mobile);
    await waitForResults(sessionId, `${label} first results`);
    await waitForExpression(sessionId, `document.querySelectorAll('#results .result-card').length>0`,
      `${label} first result cards`, uiTimeoutMs);
    const firstArtifact = await artifactState(sessionId);
    assert(firstArtifact.result_count > 0, `${label}: FIRST_TURN_NO_RESULTS`);
    const firstQuery = terminal.latest_query_id && firstQueries.includes(terminal.latest_query_id)
      ? terminal.latest_query_id : firstQueries[firstQueries.length - 1];
    await screenshot(sessionId, `${label}-first-turn`);

    currentPhase = `${label}:explain-turn`;
    await ensurePane(sessionId, 'chat', mobile);
    await waitForExpression(sessionId, `document.querySelector('#chat-send') && !document.querySelector('#chat-send').disabled`,
      `${label} first turn send enabled`, uiTimeoutMs);
    knownRuns = await runIds(sessionId);
    await typeInto(sessionId, '#chat-input', explainQuery);
    await click(sessionId, '#chat-send', mobile);
    run = await latestNewRun(sessionId, knownRuns, `${label} explain run`);
    terminal = await waitForRunTerminal(sessionId, run, `${label} explain terminal`);
    assert.equal(terminal.status, 'completed', `${label}: explain run ${terminal.status}`);
    assert.equal(terminal.conversation_id || conversationId, conversationId);
    const explainQueries = queryIds(terminal);
    await waitForAssistant(sessionId, 2, `${label} explain assistant`);
    await ensurePane(sessionId, 'results', mobile);
    await waitForResults(sessionId, `${label} explain results`);
    const explainArtifact = await artifactState(sessionId);
    await screenshot(sessionId, `${label}-explain-turn`);

    currentPhase = `${label}:modify-turn`;
    await ensurePane(sessionId, 'chat', mobile);
    await waitForExpression(sessionId, `document.querySelector('#chat-send') && !document.querySelector('#chat-send').disabled`,
      `${label} explain send enabled`, uiTimeoutMs);
    const knownQueries = [...new Set([...firstQueries, ...explainQueries])];
    knownRuns = await runIds(sessionId);
    await typeInto(sessionId, '#chat-input', modifyQuery);
    await click(sessionId, '#chat-send', mobile);
    const modifyRun = await latestNewRun(sessionId, knownRuns, `${label} modify run`);
    const newQueryResult = await waitForNewQuery(sessionId, modifyRun, knownQueries, `${label} modified query`);
    assert(newQueryResult.added.length > 0,
      `${label}: MODIFY_TURN_NO_NEW_QUERY; the user asked for a new requirement but no new query was created`);
    const newQueryId = newQueryResult.run.latest_query_id && newQueryResult.added.includes(newQueryResult.run.latest_query_id)
      ? newQueryResult.run.latest_query_id : newQueryResult.added[newQueryResult.added.length - 1];
    await waitForActiveQuery(sessionId, newQueryId, `${label} modified query active`);

    currentPhase = `${label}:old-artifact`;
    await clickQuery(sessionId, firstQuery, mobile);
    await waitForActiveQuery(sessionId, firstQuery, `${label} old artifact selected`);
    const oldBeforeRefresh = await artifactState(sessionId);
    await conversationRefresh(sessionId, `${label} old artifact refresh`);
    const oldAfterRefresh = await artifactState(sessionId);
    assert.equal(oldAfterRefresh.active_query, firstQuery, `${label}: old artifact jumped away after a conversation refresh`);
    assert.deepEqual(oldAfterRefresh.result_ids, oldBeforeRefresh.result_ids,
      `${label}: old artifact result set changed while viewing it`);
    await screenshot(sessionId, `${label}-old-artifact`);

    currentPhase = `${label}:modify-terminal`;
    terminal = await waitForRunTerminal(sessionId, newQueryResult.run, `${label} modify terminal`);
    assert.equal(terminal.status, 'completed', `${label}: modify run ${terminal.status}`);
    await waitForAssistant(sessionId, 3, `${label} modify assistant`);
    await conversationRefresh(sessionId, `${label} post-modify refresh`);
    const oldAfterTerminal = await artifactState(sessionId);
    assert.equal(oldAfterTerminal.active_query, firstQuery,
      `${label}: old artifact jumped away when the background run completed`);
    assert.deepEqual(oldAfterTerminal.result_ids, oldBeforeRefresh.result_ids,
      `${label}: old artifact changed when the background run completed`);

    currentPhase = `${label}:new-artifact`;
    await clickQuery(sessionId, newQueryId, mobile);
    await waitForActiveQuery(sessionId, newQueryId, `${label} new artifact selected`);
    await waitForResults(sessionId, `${label} new leaderboard`);
    await waitForExpression(sessionId, `document.querySelectorAll('#results .result-card').length>0`,
      `${label} new leaderboard cards`, uiTimeoutMs);
    const newArtifact = await artifactState(sessionId);
    assert(newArtifact.result_count > 0, `${label}: MODIFY_TURN_NO_RESULTS`);
    await screenshot(sessionId, `${label}-new-leaderboard`);

    currentPhase = `${label}:refresh-recovery`;
    await send('Page.reload', { ignoreCache: true }, sessionId);
    await waitForExpression(sessionId, `document.readyState==='complete' && !document.querySelector('#main-content').hidden`,
      `${label} reload ready`, uiTimeoutMs);
    await waitForAssistant(sessionId, 3, `${label} restored messages`);
    await waitForExpression(sessionId, `window.__acc.conversation?.conversation_id === ${JSON.stringify(conversationId)}`,
      `${label} restored conversation`, uiTimeoutMs);
    await ensurePane(sessionId, 'results', mobile);
    await waitForResults(sessionId, `${label} restored leaderboard`);
    await waitForExpression(sessionId, `document.querySelectorAll('#results .result-card').length>0`,
      `${label} restored leaderboard cards`, uiTimeoutMs);
    const restored = await evaluate(sessionId, `(() => ({
      stored_conversation_id: sessionStorage.getItem(${JSON.stringify(conversationStorageKey)}),
      conversation: window.__acc.conversation,
      attempt_tabs: document.querySelectorAll('#attempt-tabs button[data-query-id]').length,
      assistant_turns: document.querySelectorAll('#chat-log .chat-turn--assistant').length,
      active_query: (() => {
        const tab = document.querySelector('#attempt-tabs button[aria-pressed="true"]');
        if (tab) return tab.dataset.queryId;
        const pill = document.querySelector('#chat-log .chat-pill[aria-pressed="true"]');
        return pill?.dataset.queryId || null;
      })(),
    }))()`);
    assert.equal(restored.stored_conversation_id, conversationId);
    assert(restored.conversation?.message_count >= 6, `${label}: restored conversation lost messages`);
    assert(restored.attempt_tabs >= 2, `${label}: restored conversation lost query artifacts`);
    assert(restored.assistant_turns >= 3, `${label}: restored chat lost assistant turns`);
    await screenshot(sessionId, `${label}-refreshed`);

    const citation = await attemptCitation(sessionId, label, mobile,
      [...new Set([...knownQueries, ...newQueryResult.added, newQueryId])]);
    await assertNoRuntimeErrors(sessionId, label);

    currentPhase = `${label}:fulltext`;
    const fulltext = await fullTextCheck(sessionId, label, mobile,
      fulltextViewport === 'both' || label === 'desktop');
    await assertNoRuntimeErrors(sessionId, label);

    const check = {
      label,
      viewport: { width, height, mobile },
      submission: mobile ? 'real_touch' : 'real_mouse',
      conversation_id: conversationId,
      first_query_count: firstQueries.length,
      first_artifact: firstArtifact,
      explain_query_count: explainQueries.length,
      explain_artifact: explainArtifact,
      modified_query_count: newQueryResult.added.length,
      new_query_id: newQueryId,
      new_artifact: newArtifact,
      old_artifact_active_after_refresh: oldAfterRefresh.active_query,
      old_artifact_result_ids_stable: JSON.stringify(oldAfterRefresh.result_ids) === JSON.stringify(oldBeforeRefresh.result_ids),
      old_artifact_active_after_background_completion: oldAfterTerminal.active_query,
      refresh_recovery: restored,
      citation,
      fulltext,
      runtime_errors: [],
    };
    checks.push(check);
    await writeFile(path.join(output, `${label}.json`), JSON.stringify(check, null, 2));
    await send('Target.closeTarget', { targetId: target.targetId });
  }

  const openedCitation = checks.find((check) => check.citation?.opened);
  if (scenario === 'all') assert(openedCitation, 'CITATION_NOT_COVERED: no viewport found an eligible result card with a citation button');
  const fulltextSupported = checks.some((check) => check.fulltext?.supported);
  const fulltextUploaded = checks.some((check) => check.fulltext?.actual_upload);
  if (fulltextSupported) {
    assert(fulltextUploaded, 'FULLTEXT_UPLOAD_NOT_COVERED: service supports full text but no viewport performed the upload');
  } else if (['all', 'fulltext'].includes(scenario)) {
    skips.push('fulltext upload unsupported by service capability');
  }
  const status = skips.length ? 'passed_with_skips' : 'passed';
  const summary = {
    status,
    browser: 'isolated_headless_edge_cdp',
    base,
    output_root: outputRoot,
    output,
    run_id: runId,
    config,
    skips,
    checks,
  };
  await writeFile(path.join(output, 'acceptance.json'), JSON.stringify(summary, null, 2));
  console.log(JSON.stringify({ status, output, output_root: outputRoot, skips, checks }, null, 2));
} catch (error) {
  const failure = {
    error: error.message,
    label: activeLabel,
    phase: currentPhase,
    checks,
    skips,
    config,
    state: null,
  };
  if (activeSession && socket?.readyState === WebSocket.OPEN) {
    try {
      failure.state = await evaluate(activeSession, `(() => ({
        phase: ${JSON.stringify(currentPhase)},
        location: location.href,
        body_view: document.body.dataset.view,
        pane: document.querySelector('#search-workspace')?.dataset.pane || null,
        home_visible: !document.querySelector('#search-home')?.hidden,
        workspace_visible: !document.querySelector('#search-workspace')?.hidden,
        login_visible: !document.querySelector('#login-view')?.hidden,
        chat: document.querySelector('#chat-log')?.innerText || '',
        chat_assistant_turns: document.querySelectorAll('#chat-log .chat-turn--assistant').length,
        chat_failure: document.querySelector('#chat-log .chat-failure__text')?.textContent || null,
        attempt_tabs: [...document.querySelectorAll('#attempt-tabs button[data-query-id]')]
          .map((node) => ({ query_id: node.dataset.queryId, pressed: node.getAttribute('aria-pressed') })),
        result_count: document.querySelectorAll('#results .result-card').length,
        result_ids: [...document.querySelectorAll('#results .result-card[data-result-id]')]
          .map((node) => node.dataset.resultId),
        source_progress_hidden: document.querySelector('#source-progress')?.hidden ?? null,
        citation_dialog_open: document.querySelector('#citation-dialog')?.open || false,
        citation_body: document.querySelector('#citation-dialog-body')?.innerText || '',
        file_meta: document.querySelector('#file-meta')?.textContent || null,
        manuscript_status: document.querySelector('#manuscript-status')?.textContent || null,
        form_error: document.querySelector('#form-error')?.hidden ? null : document.querySelector('#form-error')?.textContent,
        fatal_error: document.querySelector('#fatal-error')?.hidden ? null : document.querySelector('#fatal-error')?.textContent,
        acc: window.__acc,
      }))()`);
      await screenshot(activeSession, `failure-${activeLabel || 'unknown'}`);
    } catch (captureError) {
      console.error(`Failure capture: ${captureError.message}`);
    }
  }
  await writeFile(path.join(output, 'failure.json'), JSON.stringify(failure, null, 2));
  throw error;
} finally {
  if (socket?.readyState === WebSocket.OPEN) {
    try { await send('Browser.close'); } catch (error) { console.error(`Browser cleanup: ${error.message}`); }
    socket.close();
  }
  for (const entry of pending.values()) clearTimeout(entry.timer);
  if (browser.exitCode === null) browser.kill();
  await pause(1000);
  await rm(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 300 });
}
