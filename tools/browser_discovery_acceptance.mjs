import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';

const base = process.argv[2];
const authorizedPublic = process.argv.includes('--public') && base === 'https://biodataagent.com/literature/';
assert(base && (new URL(base).hostname === '127.0.0.1' || authorizedPublic),
  'Use a loopback acceptance server, or the authorized literature HTTPS URL with --public');
const outputRoot = path.resolve(process.argv[3] || 'data/runtime/discovery-browser-acceptance');
const runId = `run-${new Date().toISOString().replace(/[:.]/g, '-')}-${randomUUID().slice(0, 8)}`;
const output = path.join(outputRoot, runId);
const overallTimeoutMs = Number(process.env.ACCEPTANCE_OVERALL_TIMEOUT_MS || 900000);
const overallDeadline = Date.now() + overallTimeoutMs;
await mkdir(output, { recursive: true });
const profile = await mkdtemp(path.join(output, '.edge-profile-'));
const executable = process.env.EDGE_EXECUTABLE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const browser = spawn(executable, ['--headless=new', '--disable-gpu', '--disable-extensions', '--disable-features=Translate', '--no-first-run',
  '--no-default-browser-check', '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
  `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore' });
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let socket;
let activeSession;
let activeLabel;
let serial = 0;
const pending = new Map();
const checks = [];

async function waitFor(read, label, timeout = 180000, interval = 200) {
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
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 30000);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  });
}
async function evaluate(sessionId, expression) {
  const result = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }, sessionId);
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
  return result.result.value;
}
async function click(sessionId, selector, mobile) {
  const rect = await evaluate(sessionId, `(() => { const r = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2}; })()`);
  if (mobile) {
    await send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ ...rect, radiusX: 1, radiusY: 1 }] }, sessionId);
    await send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] }, sessionId);
  } else {
    await send('Input.dispatchMouseEvent', { type: 'mousePressed', ...rect, button: 'left', clickCount: 1 }, sessionId);
    await send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...rect, button: 'left', clickCount: 1 }, sessionId);
  }
}

try {
  const port = await waitFor(async () => {
    try { return (await readFile(path.join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]; }
    catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  }, 'isolated Edge startup', 20000);
  const version = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
  socket = new WebSocket(version.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }); });
  socket.addEventListener('message', ({ data }) => {
    const message = JSON.parse(data);
    const entry = pending.get(message.id);
    if (!entry) return;
    clearTimeout(entry.timer);
    pending.delete(message.id);
    if (message.error) entry.reject(new Error(message.error.message));
    else entry.resolve(message.result);
  });
  for (const [label, width, height, mobile] of [['desktop', 1440, 960, false], ['mobile', 390, 844, true]]) {
    const target = await send('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await send('Target.attachToTarget', { targetId: target.targetId, flatten: true });
    activeSession = sessionId;
    activeLabel = label;
    await send('Page.enable', {}, sessionId);
    await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile }, sessionId);
    if (mobile) await send('Emulation.setTouchEmulationEnabled', { enabled: true }, sessionId);
    await send('Page.addScriptToEvaluateOnNewDocument', { source: `(() => {
      const original = window.fetch;
      window.__acceptanceFetch = original;
      window.__acceptanceErrors = [];
      window.__acceptanceRequests = [];
      window.addEventListener('error', e => window.__acceptanceErrors.push(e.message));
      window.fetch = async (...args) => {
        const response = await original(...args);
        const url = String(args[0]);
        const tenant = new Headers(args[1]?.headers || {}).get('X-Tenant-ID');
        if (tenant) window.__acceptanceTenant = tenant;
        if (url.includes('/agent/runs') && args[1]?.method === 'POST') {
          const request = JSON.parse(args[1].body);
          window.__acceptanceRequests.push({ has_scope: Object.hasOwn(request, 'claim_scope') });
        }
        if (url.includes('/agent/runs')) {
          response.clone().json().then(body => {
            const previous = window.__acceptanceAgent;
            if (body.run_id && (!previous || body.updated_at >= previous.updated_at)) {
              window.__acceptanceAgent = body;
            }
          })
            .catch(error => window.__acceptanceErrors.push('Agent response observer: ' + error.name));
        }
        if (url.includes('/conversations/') && (args[1]?.method || 'GET') === 'GET') {
          response.clone().json().then(body => {
            window.__acceptanceConversation = body;
          }).catch(error => window.__acceptanceErrors.push('Conversation response observer: ' + error.name));
        }
        return response;
      };
    })();` }, sessionId);
    await send('Page.navigate', { url: base }, sessionId);
    await waitFor(() => evaluate(sessionId, `document.querySelectorAll('.source-pill').length >= 3 && !document.querySelector('#main-content').hidden`), `${label} sources`);
    await evaluate(sessionId, `(async () => {
      const {Store}=await import(new URL('js/store.js',window.location.href));
      const {AgentSession}=await import(new URL('js/agent-session.js',window.location.href));
      window.__acceptanceNotices=[];
      window.__acceptanceVisibleRuns=[];
      const notice=Store.prototype.addNotice;
      Store.prototype.addNotice=function(...args) {
        window.__acceptanceNotices.push(args[0]);
        if (window.__acceptanceNotices.length>100) window.__acceptanceNotices.shift();
        return notice.apply(this,args);
      };
      const apply=AgentSession.prototype.applyRun;
      AgentSession.prototype.applyRun=function(run,...args) {
        const result=apply.call(this,run,...args);
        window.__acceptanceVisibleRuns.push({status:run.status,updated_at:run.updated_at,summary:run.summary});
        if (window.__acceptanceVisibleRuns.length>10) window.__acceptanceVisibleRuns.shift();
        return result;
      };
    })()`);
    const home = await send('Page.captureScreenshot', { format: 'png' }, sessionId);
    await writeFile(path.join(output, `${label}-home.png`), Buffer.from(home.data, 'base64'));
    await evaluate(sessionId, `(() => { const input=document.querySelector('#keywords'); input.value='LFY2, soybean floral development'; input.dispatchEvent(new Event('input',{bubbles:true})); input.focus(); })()`);
    await click(sessionId, '#start-button', mobile);
    await waitFor(() => evaluate(sessionId, `(async () => {
      const current=window.__acceptanceAgent;
      if (!current?.run_id) return false;
      const url=new URL('api/v1/agent/runs/'+current.run_id,window.location.href);
      const response=await window.__acceptanceFetch(url,{headers:{'X-Tenant-ID':window.__acceptanceTenant || 'local'},cache:'no-store'});
      if (!response.ok) throw new Error('Agent status HTTP '+response.status);
      const run=await response.json();
      window.__acceptanceAgent=run;
      return ['completed','failed','cancelled'].includes(run.status);
    })()`), `${label} real Agent`, overallTimeoutMs, 1000);
    await waitFor(() => evaluate(sessionId, `(() => {
      const run=window.__acceptanceAgent;
      return !!run && ['completed','failed','cancelled'].includes(run.status)
        && document.querySelectorAll('#chat-log .chat-turn--assistant').length>0;
    })()`), `${label} visible chat completion`, 30000);
    if (mobile) {
      await click(sessionId, '#pane-results', true);
      await waitFor(() => evaluate(sessionId, `getComputedStyle(document.querySelector('#results-pane')).display!=='none'`),
        `${label} results pane`, 15000);
    }
    const details = await evaluate(sessionId, `(async () => {
      const run = window.__acceptanceAgent;
      const url = new URL('api/v1/searches/' + run.latest_query_id, window.location.href);
      const state = await (await fetch(url, {headers:{'X-Tenant-ID':window.__acceptanceTenant || 'local'}})).json();
      return {
        agent: run.status, error: run.error?.code,
        agent_run_id: run.run_id, query_id: run.latest_query_id,
        search_state: state.session?.search_state,
        ranking_states: state.candidate_rankings?.map(item=>item.status),
        ranking_failures: state.candidate_rankings?.filter(item=>item.status === 'failed')
          .map(item=>({code:item.failure_code,details:item.failure_details})), 
        workers: state.workers?.map(item=>({library:item.library_id,state:item.worker_state,error:item.last_error?.code})),
        requests: window.__acceptanceRequests,
        ranks: [...document.querySelectorAll('.candidate-card .result-card__rank')].map(e=>e.textContent),
        scales: [...document.querySelectorAll('.candidate-card .result-card__scale')].map(e=>e.textContent),
        count: document.querySelectorAll('.candidate-card').length,
        progress: document.querySelector('#worker-progress-count').textContent,
        overflow: document.documentElement.scrollWidth > window.innerWidth,
        errors: window.__acceptanceErrors,
        notices: window.__acceptanceNotices,
        visible_run_updates: window.__acceptanceVisibleRuns?.map(item=>({status:item.status,updated_at:item.updated_at,summary_length:item.summary?.length || 0})),
        chat_assistant_turns: document.querySelectorAll('#chat-log .chat-turn--assistant').length,
        chat_text_length: document.querySelector('#chat-log')?.innerText.length || 0,
        conversation_id: run.conversation_id || window.__acceptanceConversation?.conversation_id || null,
        conversation_message_count: window.__acceptanceConversation?.messages?.length ?? null,
        visibleError: document.querySelector('#fatal-error').hidden ? null : document.querySelector('#fatal-error').textContent
      };
    })()`);
    await writeFile(path.join(output, `${label}-state.json`), JSON.stringify(details, null, 2));
    await evaluate(sessionId, `(() => { const abstract=document.querySelector('.candidate-card__abstract'); if (abstract) abstract.open=true; })()`);
    const screenshot = await send('Page.captureScreenshot', { format: 'png' }, sessionId);
    await writeFile(path.join(output, `${label}-results.png`), Buffer.from(screenshot.data, 'base64'));
    assert.equal(details.agent, 'completed', JSON.stringify(details));
    assert.equal(details.search_state, 'completed', JSON.stringify(details));
    assert(details.requests.length > 0 && details.requests.every(request => !request.has_scope));
    assert(details.ranking_states?.every(status => status === 'completed'));
    assert(details.workers?.every(worker => worker.state === 'completed'));
    assert(details.count > 0 && details.ranks.some(rank => rank.startsWith('LLM')), JSON.stringify(details));
    assert(details.scales.includes('共同基础分'));
    assert.equal(details.overflow, false);
    assert.deepEqual(details.errors, []);
    assert.equal(details.visibleError, null);
    assert(details.chat_assistant_turns > 0, JSON.stringify(details));
    checks.push({ label, ...details, submission: mobile ? 'real_touch' : 'real_mouse', clipboard: 'not_tested' });
    await send('Target.closeTarget', { targetId: target.targetId });
  }
  await writeFile(path.join(output, 'acceptance.json'), JSON.stringify({
    browser: 'isolated_headless_edge_cdp',
    extensions: 'disabled',
    automatic_translation: 'disabled',
    output_root: outputRoot,
    output,
    overall_timeout_ms: overallTimeoutMs,
    checks,
  }, null, 2));
  console.log(JSON.stringify({ status: 'passed', output, output_root: outputRoot, checks }, null, 2));
} catch (error) {
  if (activeSession && socket?.readyState === WebSocket.OPEN) {
    try {
      const state=await evaluate(activeSession, `({
        observed_agent:window.__acceptanceAgent,
        visible_runs:window.__acceptanceVisibleRuns,
        notices:window.__acceptanceNotices,
        errors:window.__acceptanceErrors,
        conversation:window.__acceptanceConversation,
        chat:document.querySelector('#chat-log')?.innerText,
        fatal:document.querySelector('#fatal-error')?.textContent,
        candidate_count:document.querySelectorAll('.candidate-card').length
      })`);
      await writeFile(path.join(output, `${activeLabel}-failure.json`), JSON.stringify({error:error.message,state},null,2));
      const image=await send('Page.captureScreenshot',{format:'png'},activeSession);
      await writeFile(path.join(output, `${activeLabel}-failure.png`),Buffer.from(image.data,'base64'));
    } catch (captureError) { console.error(`Failure capture: ${captureError.message}`); }
  }
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
