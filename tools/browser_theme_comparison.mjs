import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';

const base = process.argv[2];
const output = path.resolve(process.argv[3] || 'data/runtime/theme-comparison');
assert(base && new URL(base).hostname === '127.0.0.1', 'Use a loopback server');
const username = process.env.THEME_COMPARISON_USERNAME || 'theme-comparison-demo';
const password = process.env.THEME_COMPARISON_DEMO_PASSWORD;
assert(password, 'Provide the disposable account password through THEME_COMPARISON_DEMO_PASSWORD');
const executable = process.env.EDGE_EXECUTABLE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const themes = [
  { choice: '1', id: 'paper', label: '简洁经典' },
  { choice: '2', id: 'caas', label: '农科院门户' },
  { choice: '3', id: 'editorial', label: '学术札记' },
  { choice: '4', id: 'lab', label: '研究工作台' },
];
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let browser;
let profile;
let socket;
let serial = 0;
let cookieName;
let cookieValue;
const pending = new Map();

function send(method, params = {}, sessionId) {
  return new Promise((resolve, reject) => {
    const id = ++serial;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`CDP timeout: ${method}`));
    }, 45000);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  });
}

async function evaluate(sessionId, expression) {
  const result = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }, sessionId);
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
  return result.result.value;
}

async function waitFor(sessionId, expression, label, timeout = 45000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await evaluate(sessionId, expression)) return;
    await pause(120);
  }
  throw new Error(`Timed out: ${label}`);
}

async function screenshot(sessionId, name) {
  await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }] }, sessionId);
  await pause(250);
  const image = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false }, sessionId);
  const target = path.join(output, `${name}.png`);
  await writeFile(target, Buffer.from(image.data, 'base64'));
  return target;
}

async function openPage(theme) {
  const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
  await send('Page.enable', {}, sessionId);
  await send('Network.enable', {}, sessionId);
  await send('Network.setCookie', {
    name: cookieName, value: cookieValue, url: new URL(base).origin, httpOnly: true, sameSite: 'Lax',
  }, sessionId);
  await send('Emulation.setDeviceMetricsOverride', {
    width: 1440, height: 960, deviceScaleFactor: 1.5, mobile: false,
  }, sessionId);
  return { targetId, sessionId, url: new URL(`?theme=${theme.choice}`, base).href };
}

async function navigate(sessionId, url) {
  await send('Page.navigate', { url }, sessionId);
  await waitFor(sessionId, `document.querySelector('#theme-picker') !== null
    && document.querySelector('#profile-button')?.getAttribute('aria-label')?.includes('研究演示')
    && document.querySelectorAll('#library-list .source-pill').length > 0`, 'logged-in application');
}

try {
  await mkdir(output, { recursive: true });
  const origin = new URL(base).origin;
  const login = await fetch(`${origin}/api/v1/account/login`, {
    method: 'POST',
    headers: { Origin: origin, 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ username, password }),
  });
  assert.equal(login.status, 200, 'Log in to the isolated demo account');
  const identity = (await login.json()).account;
  const cookie = login.headers.getSetCookie().map((row) => row.split(';', 1)[0]).find((row) => row.startsWith('literature_account_session='));
  assert(cookie, 'Receive an actual account cookie');
  const separator = cookie.indexOf('=');
  assert(separator > 0, 'Parse the account cookie');
  cookieName = cookie.slice(0, separator);
  cookieValue = cookie.slice(separator + 1);
  const histories = await (await fetch(`${origin}/api/v1/conversations`, {
    headers: { Cookie: cookie, 'X-Workspace-Account': identity.id, Accept: 'application/json' },
  })).json();
  assert(Array.isArray(histories.items) && histories.items.length >= 2, 'At least two persisted conversations');
  const selected = process.env.THEME_COMPARISON_CONVERSATION || histories.items[0].conversation_id;
  const restored = await (await fetch(`${origin}/api/v1/conversations/${selected}`, {
    headers: { Cookie: cookie, 'X-Workspace-Account': identity.id, Accept: 'application/json' },
  })).json();
  assert(restored.messages?.filter((message) => message.role === 'user').length >= 2, 'A real multi-turn conversation');
  assert(restored.messages?.some((message) => message.role === 'assistant'), 'A persisted assistant answer');
  assert(restored.runs?.every((run) => ['completed', 'cancelled'].includes(run.status)), 'Every captured run is settled');
  assert.equal(restored.turns?.at(-1)?.status, 'completed', 'The latest captured turn is complete');

  profile = await mkdtemp(path.join(output, '.edge-profile-'));
  browser = spawn(executable, ['--headless=new', '--disable-gpu', '--disable-extensions',
    '--disable-features=Translate', '--no-first-run', '--no-default-browser-check',
    '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
    `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore' });
  let port;
  for (let attempt = 0; attempt < 200; attempt += 1) {
    try {
      port = (await readFile(path.join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0];
      if (port) break;
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
    await pause(100);
  }
  assert(port, 'Isolated Edge startup');
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
  const captures = {};
  for (const theme of themes) {
    const { targetId, sessionId, url } = await openPage(theme);
    try {
      const { identifier } = await send('Page.addScriptToEvaluateOnNewDocument', {
        source: `sessionStorage.removeItem(${JSON.stringify(`citation.conversation-id.v1:${identity.id}`)});`,
      }, sessionId);
      await navigate(sessionId, url);
      await waitFor(sessionId, `document.querySelectorAll('#history-items .history-row').length >= 2
        && document.querySelector('#profile-initial')?.hidden === false`, `${theme.label} home history`);
      await evaluate(sessionId, `document.querySelector('#profile-button').click()`);
      await waitFor(sessionId, `!document.querySelector('#profile-menu').hidden
        && document.querySelector('#profile-label')?.textContent === '研究演示'`, `${theme.label} logged-in identity`);
      captures[`${theme.choice}-home`] = await screenshot(sessionId, `${theme.choice}-${theme.id}-home`);
      await send('Page.removeScriptToEvaluateOnNewDocument', { identifier }, sessionId);
      await evaluate(sessionId, `sessionStorage.setItem(${JSON.stringify(`citation.conversation-id.v1:${identity.id}`)}, ${JSON.stringify(selected)})`);
      await send('Page.reload', { ignoreCache: true }, sessionId);
      await waitFor(sessionId, `document.body?.dataset.view === 'results'
        && document.querySelectorAll('#chat-log .chat-turn').length >= 4
        && document.querySelectorAll('#results .result-card').length >= 1
        && document.querySelector('#cancel-run')?.hidden === true`, `${theme.label} restored conversation`);
      await evaluate(sessionId, `document.querySelector('#new-search-button').click()`);
      await waitFor(sessionId, `!document.querySelector('#history-panel').hidden
        && document.querySelectorAll('#history-items .history-row').length >= 2`, `${theme.label} history`);
      captures[`${theme.choice}-conversation`] = await screenshot(sessionId, `${theme.choice}-${theme.id}-conversation`);
      assert.equal(await evaluate(sessionId, `document.documentElement.scrollWidth > innerWidth + 1`), false);
      console.log(`CAPTURED ${theme.choice} ${theme.label}`);
    } finally {
      await send('Target.closeTarget', { targetId });
    }
  }
  await writeFile(path.join(output, 'capture.json'), JSON.stringify({
    account: { username: identity.username, display_name: identity.display_name },
    histories: histories.items.map((row) => ({ title: row.title, updated_at: row.updated_at })),
    selected_conversation: selected,
    messages: restored.messages.map((message) => ({ role: message.role, content: message.text })),
    themes,
    captures,
  }, null, 2));
  console.log(output);
} catch (error) {
  console.error(error);
  process.exitCode = 1;
} finally {
  if (socket?.readyState === WebSocket.OPEN) socket.close();
  if (browser && !browser.killed) browser.kill();
  await pause(150);
  if (profile) await rm(profile, { recursive: true, force: true }).catch(() => {});
}
