import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';

const [base, recordingPath, outputPath] = process.argv.slice(2);
assert.equal(new URL(base).hostname, '127.0.0.1', 'Replay only against a loopback service');
const recording = JSON.parse(await readFile(recordingPath, 'utf8'));
assert(recording.events?.length && recording.state?.candidate_diagnostics?.length);
const baseline = process.argv.includes('--baseline');
const overallTimeoutMs = Number(process.env.ACCEPTANCE_OVERALL_TIMEOUT_MS || 900000);
assert(Number.isFinite(overallTimeoutMs) && overallTimeoutMs > 0, 'ACCEPTANCE_OVERALL_TIMEOUT_MS must be positive');
const overallDeadline = Date.now() + overallTimeoutMs;
const outputRoot = path.resolve(outputPath);
const runId = `run-${new Date().toISOString().replace(/[:.]/g, '-')}-${randomUUID().slice(0, 8)}`;
const output = path.join(outputRoot, runId);
await mkdir(output, { recursive: true });
const profile = await mkdtemp(path.join(output, '.edge-profile-'));
const executable = process.env.EDGE_EXECUTABLE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const browser = spawn(executable, ['--headless=new', '--disable-gpu', '--disable-extensions',
  '--disable-features=Translate', '--no-first-run', '--no-default-browser-check',
  '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
  `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore' });
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let socket;
let serial = 0;
const pending = new Map();
const checks = [];

async function waitFor(read, label, timeout = 15000) {
  const deadline = Math.min(Date.now() + timeout, overallDeadline);
  while (Date.now() < deadline) {
    const value = await read();
    if (value) return value;
    await pause(100);
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
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
  return result.result.value;
}
async function screenshot(sessionId, name) {
  const image = await send('Page.captureScreenshot', { format: 'png' }, sessionId);
  await writeFile(path.join(output, `${name}.png`), Buffer.from(image.data, 'base64'));
}

try {
  const port = await waitFor(async () => {
    try { return (await readFile(path.join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]; }
    catch (error) { if (error.code === 'ENOENT') return null; throw error; }
  }, 'isolated browser');
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
    const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
    await send('Page.enable', {}, sessionId);
    await send('Emulation.setDeviceMetricsOverride', { width, height, deviceScaleFactor: 1, mobile }, sessionId);
    if (mobile) await send('Emulation.setTouchEmulationEnabled', { enabled: true }, sessionId);
    await send('Page.addScriptToEvaluateOnNewDocument', { source: `window.__uiErrors=[];
      window.addEventListener('error',event=>window.__uiErrors.push(event.message));
      window.addEventListener('unhandledrejection',event=>window.__uiErrors.push(String(event.reason)));` }, sessionId);
    await send('Page.navigate', { url: base }, sessionId);
    await waitFor(() => evaluate(sessionId, `!document.querySelector('#main-content').hidden && document.querySelectorAll('#library-list .source-pill').length>0`), 'page ready');
    await screenshot(sessionId, `${label}-home`);
    await evaluate(sessionId, `(async () => {
      const {Store}=await import(new URL('js/store.js',location.href));
      const {ResultsView}=await import(new URL('js/results-view.js',location.href));
      const {renderStatus}=await import(new URL('js/render.js',location.href));
      const data=${JSON.stringify(recording)};
      const store=new Store();
      const state=data.state;
      const claimIds=[...new Set(state.candidate_diagnostics.map(item=>item.claim_id))];
      store.attachCreateResponse({query_id:data.query_id,simulation:false,
        search_state:'running',claims:claimIds.map(claim_id=>({claim_id,claim_text:'',claim_type:'topic'}))});
      store.session.requested_library_ids=state.session.requested_library_ids;
      store.libraryNames=new Map(state.libraries.map(item=>[item.library_id,item.display_name]));
      store.seedPendingWorkers(store.session.requested_library_ids,state.libraries);
      const refs=Object.fromEntries(['metric-results','notice-list','worker-list','workers-empty',
        'worker-progress','worker-progress-count','results-status'].map(id=>[
          id.replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase()),document.getElementById(id)]));
      const view=new ResultsView({root:document.querySelector('#results'),note:document.querySelector('#results-note')});
      const render=()=>{renderStatus(store,refs,{status:'running'});view.update(store);};
      document.body.dataset.view='results';
      document.querySelector('#search-home').hidden=true;
      document.querySelector('#search-workspace').hidden=false;
      document.querySelector('#new-search-button').hidden=false;
      document.querySelector('#metric-results-label').textContent='待核查候选';
      document.querySelector('#search-workspace').dataset.pane='results';
      document.querySelector('#pane-chat').setAttribute('aria-selected','false');
      document.querySelector('#pane-results').setAttribute('aria-selected','true');
      window.__ui={store,view,render,data,replacements:0,reinsertions:0,animatedFrames:0,stageCount:0};
      render();
    })()`);
    await evaluate(sessionId, `(async () => {
      const state=window.__ui;
      for(const event of state.data.events){
        const before=new Map([...document.querySelectorAll('.candidate-card')].map(node=>[node.dataset.resultId,node]));
        const root=document.querySelector('#results');
        const observer=new MutationObserver(()=>{});
        observer.observe(root,{childList:true,subtree:true});
        state.store.applyEvent(event);
        state.render();
        const mutations=observer.takeRecords();
        observer.disconnect();
        for(const node of document.querySelectorAll('.candidate-card')){
          const previous=before.get(node.dataset.resultId);
          if(previous && previous!==node) state.replacements++;
        }
        for(const mutation of mutations) for(const node of mutation.removedNodes){
          if(node.nodeType===1 && (node.matches('.candidate-card') || node.querySelector('.candidate-card'))) state.reinsertions++;
        }
        state.stageCount=Math.max(state.stageCount,document.querySelectorAll('.worker__steps').length);
        await new Promise(requestAnimationFrame);
        if(document.querySelector('#results').getAnimations({subtree:true}).length) state.animatedFrames++;
      }
    })()`);
    const replay = await evaluate(sessionId, `(() => {
      const state=window.__ui;
      const cards=[...document.querySelectorAll('.candidate-card')];
      const firstCardTop=cards[0].getBoundingClientRect().top;
      const cardGap=cards[1].getBoundingClientRect().top-cards[0].getBoundingClientRect().bottom;
      const observer=new MutationObserver(()=>{});
      observer.observe(document.querySelector('#results'),{childList:true,subtree:true});
      for(let index=0;index<5;index++)state.render();
      const identicalMutations=observer.takeRecords().length;
      observer.disconnect();
      const target=cards[0];
      const details=target.querySelector('.candidate-card__abstract');
      details.open=true;
      details.querySelector('summary').focus({preventScroll:true});
      const focus=document.activeElement;
      const selection=getSelection();
      const text=details.querySelector('p').firstChild;
      selection.setBaseAndExtent(text,0,text,20);
      const selected=selection.toString();
      const item=state.store.candidates.get(target.dataset.resultId);
      const second=state.store.candidates.get(cards[1].dataset.resultId);
      state.store.candidates.set(second.result_id,{...second,llm_rank:1});
      state.store.candidates.set(item.result_id,{...item,llm_rank:2,rank_delta:2,
        reason:{...item.reason,text:item.reason.text+'（界面局部更新回归用文本，不是新的科学结论。）'}});
      state.render();
      const updated=[...document.querySelectorAll('.candidate-card')].find(node=>node.dataset.resultId===item.result_id);
      const selectionPreserved=selection.toString()===selected;
      const list=target.parentNode;
      Object.defineProperty(list,'moveBefore',{value:undefined,configurable:true});
      state.store.candidates.set(item.result_id,{...state.store.candidates.get(item.result_id),llm_rank:1});
      state.store.candidates.set(second.result_id,{...second,llm_rank:2});
      state.render();
      const fallbackSelectionPreserved=selection.toString()===selected;
      const fallbackFocusPreserved=document.activeElement===focus;
      delete list.moveBefore;
      selection.removeAllRanges();
      return {events:state.data.events.length,count:cards.length,replacements:state.replacements,
        reinsertions:state.reinsertions,animatedFrames:state.animatedFrames,stageCount:state.stageCount,
        identicalMutations,sameCardOnVisibleUpdate:updated===target,
        sameDetailsOnVisibleUpdate:updated.querySelector('.candidate-card__abstract')===details,
        focusPreserved:document.activeElement===focus,detailsExpanded:updated.querySelector('.candidate-card__abstract').open,
        selectionPreserved,fallbackSelectionPreserved,fallbackFocusPreserved,
        firstCardTop,cardGap,
        overflow:document.documentElement.scrollWidth>innerWidth,
        progress:document.querySelector('#worker-progress-count').textContent,
        workerText:document.querySelector('#worker-list').textContent,
        errors:window.__uiErrors};
    })()`);
    const viewport = await evaluate(sessionId, `(() => {
      const state=window.__ui;
      state.store.applyState(state.data.state);
      state.render();
      const cards=[...document.querySelectorAll('.candidate-card')];
      const anchor=cards[Math.floor(cards.length/2)];
      anchor.scrollIntoView({block:'start',behavior:'instant'});
      const before=anchor.getBoundingClientRect().top;
      const first=state.store.candidates.values().next().value;
      for(const [id,item] of state.store.candidates)state.store.candidates.set(id,{...item,llm_rank:item.llm_rank+1});
      state.store.candidates.set('ui-regression-only',{...first,result_id:'ui-regression-only',
        work_id:'ui-regression-only',title:'UI regression fixture only',llm_rank:1});
      state.render();
      const shift=anchor.getBoundingClientRect().top-before;
      state.store.applyState(state.data.state);
      state.render();
      for(const details of document.querySelectorAll('.candidate-card details'))details.open=false;
      document.activeElement?.blur();
      window.scrollTo({top:0,behavior:'instant'});
      return {readingAnchorShift:shift};
    })()`);
    Object.assign(replay,viewport);
    await screenshot(sessionId, `${label}-results`);
    if (!baseline) {
      assert.equal(replay.replacements, 0, JSON.stringify(replay));
      assert.equal(replay.animatedFrames, 0, JSON.stringify(replay));
      assert.equal(replay.stageCount, 0);
      assert.equal(replay.identicalMutations, 0);
      assert.equal(replay.sameCardOnVisibleUpdate, true);
      assert.equal(replay.sameDetailsOnVisibleUpdate, true);
      assert.equal(replay.focusPreserved, true);
      assert.equal(replay.detailsExpanded, true);
      assert.equal(replay.selectionPreserved, true);
      assert.equal(replay.fallbackSelectionPreserved, true);
      assert.equal(replay.fallbackFocusPreserved, true);
      assert(Math.abs(replay.readingAnchorShift)<1, JSON.stringify(replay));
      assert(replay.cardGap >= 15, JSON.stringify(replay));
      if (!mobile) assert(replay.firstCardTop < 220, JSON.stringify(replay));
      assert.equal(replay.overflow, false);
      assert.doesNotMatch(replay.workerText, /核验|推荐 0/);
      assert.deepEqual(replay.errors, []);
    }
    checks.push({label,...replay});
    await send('Target.closeTarget', {targetId});
  }
  if (process.argv.includes('--fixture')) {
    const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
    const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
    await send('Page.enable', {}, sessionId);
    await send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true }, sessionId);
    await send('Emulation.setTouchEmulationEnabled', { enabled: true }, sessionId);
    await send('Page.navigate', { url: base }, sessionId);
    await waitFor(() => evaluate(sessionId, `!document.querySelector('#main-content').hidden && document.querySelectorAll('#library-list .source-pill').length===2`), 'fixture page ready');
    const touch = async (selector) => {
      const rect = await evaluate(sessionId, `(() => { const node=document.querySelector(${JSON.stringify(selector)}); node.scrollIntoView({block:'center',behavior:'instant'}); const r=node.getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2}; })()`);
      await send('Input.dispatchTouchEvent', {type:'touchStart',touchPoints:[{...rect,radiusX:1,radiusY:1}]}, sessionId);
      await send('Input.dispatchTouchEvent', {type:'touchEnd',touchPoints:[]}, sessionId);
    };
    await evaluate(sessionId, `(() => { const input=document.querySelector('#keywords'); input.value='LFY2, soybean'; input.dispatchEvent(new Event('input',{bubbles:true})); })()`);
    await touch('#start-button');
    await waitFor(() => evaluate(sessionId, `(() => {
      const send=document.querySelector('#chat-send');
      return document.querySelectorAll('#attempt-tabs button').length>=2
        && document.querySelectorAll('.result-card').length>0
        && send && !send.disabled;
    })()`), 'two completed real HTTP searches', overallTimeoutMs);
    assert.equal(await evaluate(sessionId, `document.querySelectorAll('#chat-log .chat-bubble--error').length`), 0,
      'Agent run or transport error bubble appeared');
    if (mobile) {
      await touch('#pane-results');
      await waitFor(() => evaluate(sessionId, `getComputedStyle(document.querySelector('#results-pane')).display!=='none'`), 'mobile results pane');
    }
    const latestIds = await evaluate(sessionId, `[...document.querySelectorAll('.result-card')].map(node=>node.dataset.resultId)`);
    await touch('#attempt-tabs button:first-child');
    await waitFor(() => evaluate(sessionId, `document.querySelector('#attempt-tabs button:first-child').getAttribute('aria-pressed')==='true' && document.querySelectorAll('.result-card').length>0`), 'query switch');
    const olderIds = await evaluate(sessionId, `[...document.querySelectorAll('.result-card')].map(node=>node.dataset.resultId)`);
    assert(latestIds.length && olderIds.length && !olderIds.some(id=>latestIds.includes(id)), 'query identities must remain separate');
    const top = await evaluate(sessionId, `(() => { window.scrollTo({top:0,behavior:'instant'}); return document.querySelector('.result-card').getBoundingClientRect().top; })()`);
    await screenshot(sessionId, 'mobile-http-results');
    await touch('.detail-link');
    await waitFor(() => evaluate(sessionId, `document.querySelector('#result-dialog').open && document.querySelector('#result-dialog-body').textContent.includes('LFY2')`), 'real evidence route');
    await touch('#close-result-dialog');
    await touch('#new-search-button');
    if (await evaluate(sessionId, `document.querySelector('#new-search-button').getAttribute('aria-expanded') === 'true'`)) {
      await touch('#history-new');
    }
    await waitFor(() => evaluate(sessionId, `!document.querySelector('#search-home').hidden`), 'home after results');
    await touch('#mode-summary');
    const fullTextCapability = await evaluate(sessionId, `({
      modeDisabled: document.querySelector('button[data-mode="full_text"]').disabled,
      inputDisabled: document.querySelector('#manuscript-file').disabled,
    })`);
    if (fullTextCapability.modeDisabled) {
      assert.equal(fullTextCapability.inputDisabled, true);
    } else {
      await touch('[data-mode="full_text"]');
      const fullTextState = await evaluate(sessionId, `({
        noteVisible: !document.querySelector('#full-text-note').hidden,
        uploadVisible: !document.querySelector('#upload-control').hidden,
        textInputHidden: document.querySelector('#text-input-wrap').hidden,
        startDisabled: document.querySelector('#start-button').disabled,
        note: document.querySelector('#full-text-note').textContent,
      })`);
      assert.equal(fullTextState.noteVisible, true);
      assert.equal(fullTextState.uploadVisible, true);
      assert.equal(fullTextState.textInputHidden, true);
      assert.equal(fullTextState.startDisabled, true);
      assert.match(fullTextState.note, /匿名实例|模型/);
      await touch('#mode-summary');
    }
    await touch('[data-mode="abstract"]');
    await evaluate(sessionId, `(() => { const input=document.querySelector('#keywords'); input.value='An abstract for the explicitly scripted no-search UI case.'; input.dispatchEvent(new Event('input',{bubbles:true})); })()`);
    await touch('#start-button');
    await waitFor(() => evaluate(sessionId, `(() => {
      const assistant=document.querySelectorAll('#chat-log .chat-turn--assistant').length;
      const send=document.querySelector('#chat-send');
      return assistant>=1 && send && !send.disabled;
    })()`), 'Agent no-search turn terminal', overallTimeoutMs);
    const noSearch = await evaluate(sessionId, `({
      assistantTurns: document.querySelectorAll('#chat-log .chat-turn--assistant').length,
      cards: document.querySelectorAll('.result-card').length,
      sourceHidden: document.querySelector('#source-progress').hidden,
      noteHidden: document.querySelector('#results-note').hidden,
      errorBubbles: document.querySelectorAll('#chat-log .chat-bubble--error').length,
      chatText: document.querySelector('#chat-log').innerText,
    })`);
    assert.equal(noSearch.cards, 0);
    assert.equal(noSearch.sourceHidden, true);
    assert.equal(noSearch.noteHidden, true);
    assert.equal(noSearch.errorBubbles, 0);
    assert(noSearch.assistantTurns >= 1);
    await screenshot(sessionId, 'mobile-http-no-search');
    checks.push({label:'real_http_fixture',submission:'real_touch',independentQueries:2,
      olderCount:olderIds.length,latestCount:latestIds.length,firstCardTop:top,
      source:'scripted test model; not a science-quality evaluation',evidenceRoute:true,
      fullTextCapability: fullTextCapability.modeDisabled ? 'unsupported' : 'supported',
      noSearchAnswer: noSearch.assistantTurns >= 1, noSearchCards: noSearch.cards,
      noSearchErrorBubbles: noSearch.errorBubbles, clipboard:'not_tested'});
    await send('Target.closeTarget', {targetId});
  }
  await writeFile(path.join(output, 'acceptance.json'), JSON.stringify({
    mode:baseline?'baseline':'verified',browser:'isolated_headless_edge_cdp',
    output_root:outputRoot,output,overall_timeout_ms:overallTimeoutMs,
    source:'previous real search SSE replay; no model calls; UI regression only',checks,
  },null,2));
  console.log(JSON.stringify({output,output_root:outputRoot,checks},null,2));
} finally {
  if(socket?.readyState===WebSocket.OPEN){
    try{await send('Browser.close');}catch(error){console.error('Browser cleanup: '+error.message);}
    socket.close();
  }
  for(const entry of pending.values())clearTimeout(entry.timer);
  if(browser.exitCode===null)browser.kill();
  await pause(1000);
  await rm(profile,{recursive:true,force:true,maxRetries:5,retryDelay:300});
}
