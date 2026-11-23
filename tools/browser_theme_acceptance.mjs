import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';

const base = process.argv[2];
assert(base && new URL(base).hostname === '127.0.0.1', 'Use a loopback fixture server');
const outputRoot = path.resolve(process.argv[3] || 'data/runtime/theme-browser-acceptance');
const runId = `run-${new Date().toISOString().replace(/[:.]/g, '-')}-${randomUUID().slice(0, 8)}`;
const output = path.join(outputRoot, runId);
const executable = process.env.EDGE_EXECUTABLE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const allThemes = ['1', '2', '3', '4'];
const themeIds = { 1: 'paper', 2: 'caas', 3: 'editorial', 4: 'lab' };
const themes = (process.env.THEMES || allThemes.join(',')).split(',');
assert(themes.every((theme) => allThemes.includes(theme)), 'Unknown theme filter');
const devices = [
  { name: 'desktop', width: 1440, height: 960, mobile: false },
  { name: 'mobile', width: 390, height: 844, mobile: true },
].filter((device) => !process.env.VIEWPORTS || process.env.VIEWPORTS.split(',').includes(device.name));
const checks = [];
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let browser;
let profile;
let socket;
let serial = 0;
const pending = new Map();

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
  const result = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }, sessionId);
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
  return result.result.value;
}

async function waitFor(sessionId, expression, label, timeout = 30000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await evaluate(sessionId, expression)) return;
    await pause(100);
  }
  throw new Error(`Timed out: ${label}`);
}

async function click(sessionId, selector, mobile) {
  const point = await evaluate(sessionId, `(() => {
    const element = document.querySelector(${JSON.stringify(selector)});
    if (!element) return null;
    element.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' });
    const rect = element.getBoundingClientRect();
    return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2, width: rect.width, height: rect.height };
  })()`);
  assert(point && point.width > 0 && point.height > 0, `Visible control: ${selector}`);
  if (mobile) {
    await send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: point.x, y: point.y, radiusX: 1, radiusY: 1 }] }, sessionId);
    await send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] }, sessionId);
  } else {
    await send('Input.dispatchMouseEvent', { type: 'mousePressed', x: point.x, y: point.y, button: 'left', clickCount: 1 }, sessionId);
    await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: point.x, y: point.y, button: 'left', clickCount: 1 }, sessionId);
  }
}

async function screenshot(sessionId, name) {
  const image = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false }, sessionId);
  await writeFile(path.join(output, `${name}.png`), Buffer.from(image.data, 'base64'));
}

async function openTheme(device, theme) {
  const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
  const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true });
  await send('Page.enable', {}, sessionId);
  await send('Emulation.setDeviceMetricsOverride', {
    width: device.width, height: device.height, deviceScaleFactor: 1, mobile: device.mobile,
  }, sessionId);
  if (device.mobile) await send('Emulation.setTouchEmulationEnabled', { enabled: true }, sessionId);
  await send('Page.addScriptToEvaluateOnNewDocument', { source: `window.__themeErrors=[];
    window.addEventListener('error', event => window.__themeErrors.push(event.message));
    window.addEventListener('unhandledrejection', event => window.__themeErrors.push(String(event.reason)));` }, sessionId);
  const url = new URL(base);
  url.searchParams.set('theme', theme);
  await send('Page.navigate', { url: url.href }, sessionId);
  await waitFor(sessionId, `document.querySelector('#theme-picker')?.value === ${JSON.stringify(theme)}
    && document.querySelectorAll('#library-list .source-pill').length > 0`, `${theme} home`);
  return { targetId, sessionId };
}

async function checkTheme(device, theme) {
  const label = `${theme}-${themeIds[theme]}-${device.name}`;
  const { targetId, sessionId } = await openTheme(device, theme);
  try {
    assert.equal(await evaluate(sessionId, `document.querySelectorAll('script[type="module"][src="./js/main.js"]').length`), 1);
    assert.equal(await evaluate(sessionId, `document.querySelectorAll('input[type="file"]').length`), 1);
    await screenshot(sessionId, `${label}-home`);

    await click(sessionId, '#mode-summary', device.mobile);
    await click(sessionId, '[data-mode="full_text"]', device.mobile);
    assert.equal(await evaluate(sessionId, `document.querySelector('#keywords').disabled`), true);
    assert.equal(await evaluate(sessionId, `!document.querySelector('#upload-control').hidden`), true);
    await click(sessionId, '#mode-summary', device.mobile);
    await click(sessionId, '[data-mode="keywords"]', device.mobile);
    assert.equal(await evaluate(sessionId, `document.querySelector('#keywords').disabled`), false);
    assert.equal(await evaluate(sessionId, `document.querySelector('#upload-control').hidden`), true);
    checks.push({ case: label, check: 'mode-and-single-upload-entry', passed: true });

    await evaluate(sessionId, `(() => {
      const input = document.querySelector('#keywords');
      input.value = 'LFY2, soybean';
      input.dispatchEvent(new Event('input', { bubbles: true }));
    })()`);
    await click(sessionId, '#start-button', device.mobile);
    await waitFor(sessionId, `document.querySelector('#chat-log')?.textContent.includes('两次结果分别保留')`, `${label} conversation`, 60000);
    await waitFor(sessionId, `document.querySelectorAll('#attempt-tabs [data-query-id]').length === 2
      && document.querySelectorAll('#results .result-card').length === 2`, `${label} recovered artifacts`);
    assert.equal(await evaluate(sessionId, `document.querySelectorAll('#results .result-card__score').length`), 0);
    assert.equal(await evaluate(sessionId, `document.querySelector('#worker-progress-count').textContent`), '1/1');
    checks.push({ case: label, check: 'two-independent-searches-and-real-worker-count', passed: true });

    if (device.mobile) {
      await click(sessionId, '#pane-results', true);
      await waitFor(sessionId, `document.querySelector('#search-workspace').dataset.pane === 'results'`, `${label} touch results`);
      assert.equal(await evaluate(sessionId, `getComputedStyle(document.querySelector('#chat-pane')).display`), 'none');
      checks.push({ case: label, check: 'native-touch-pane-switch', passed: true });
    }
    await screenshot(sessionId, `${label}-results`);

    await click(sessionId, '#results [data-action="copy-citation"]', device.mobile);
    await waitFor(sessionId, `!document.querySelector('#bibliography-menu')?.hidden`, `${label} citation formats`);
    assert.equal(await evaluate(sessionId, `document.querySelectorAll('#bibliography-menu [role="menuitem"]').length > 0`), true);
    await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape' }, sessionId);
    await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Escape', code: 'Escape' }, sessionId);
    checks.push({ case: label, check: 'bibliography-menu', passed: true });

    await click(sessionId, '#results [data-action="citation-text"]', device.mobile);
    await waitFor(sessionId, `document.querySelector('#citation-dialog').open`, `${label} citation dialog`);
    await pause(240);
    await screenshot(sessionId, `${label}-citation`);
    await click(sessionId, '#close-citation-dialog', device.mobile);
    await waitFor(sessionId, `!document.querySelector('#citation-dialog').open`, `${label} close citation`);

    await click(sessionId, '#results [data-action="open-result"]', device.mobile);
    await waitFor(sessionId, `document.querySelector('#result-dialog').open`, `${label} evidence dialog`);
    assert.equal(await evaluate(sessionId, `document.querySelectorAll('#result-dialog-body .evidence').length > 0`), true);
    assert.equal(await evaluate(sessionId, `document.querySelector('#result-dialog-body').textContent.includes('相关性分')`), false);
    await pause(240);
    await screenshot(sessionId, `${label}-evidence`);
    await click(sessionId, '#close-result-dialog', device.mobile);
    await waitFor(sessionId, `!document.querySelector('#result-dialog').open`, `${label} close evidence`);
    checks.push({ case: label, check: 'citation-and-evidence-dialogs', passed: true });

    await evaluate(sessionId, `document.querySelector('#profile-button').click()`);
    await waitFor(sessionId, `!document.querySelector('#profile-menu').hidden`, `${label} profile menu`);
    await evaluate(sessionId, `document.querySelector('#model-settings-button').click()`);
    await waitFor(sessionId, `document.querySelector('#model-dialog').open`, `${label} model settings`);
    await evaluate(sessionId, `(() => {
      const provider = document.querySelector('#model-provider');
      provider.value = 'custom';
      provider.dispatchEvent(new Event('change', { bubbles: true }));
    })()`);
    assert.equal(await evaluate(sessionId, `getComputedStyle(document.querySelector('#model-custom-fields')).display !== 'none'`), true);
    assert.equal(await evaluate(sessionId, `document.querySelector('#model-protocol').value.length > 0`), true);
    assert.equal(await evaluate(sessionId, `document.querySelector('#model-api-key').type`), 'password');
    await pause(240);
    await screenshot(sessionId, `${label}-model`);
    await evaluate(sessionId, `document.querySelector('#model-dialog').close()`);
    await waitFor(sessionId, `!document.querySelector('#model-dialog').open`, `${label} close model settings`);
    checks.push({ case: label, check: 'custom-model-fields-without-credentials', passed: true });

    const overflow = await evaluate(sessionId, `document.documentElement.scrollWidth > window.innerWidth + 1`);
    assert.equal(overflow, false, `${label} horizontal overflow`);
    assert.deepEqual(await evaluate(sessionId, `window.__themeErrors`), []);
    checks.push({ case: label, check: 'no-overflow-or-browser-errors', passed: true });
    console.log(`PASS ${label}`);
  } catch (error) {
    await screenshot(sessionId, `${label}-failure`).catch(() => {});
    const state = await evaluate(sessionId, `({
      view: document.body.dataset.view,
      mode: document.querySelector('#search-composer')?.dataset.mode,
      query: document.querySelector('#keywords')?.value,
      startDisabled: document.querySelector('#start-button')?.disabled,
      formError: document.querySelector('#form-error')?.textContent,
      chat: document.querySelector('#chat-log')?.textContent,
      fatal: document.querySelector('#fatal-error')?.textContent,
      profileHidden: document.querySelector('#profile-control')?.hidden,
      menuHidden: document.querySelector('#profile-menu')?.hidden,
      modelOpen: document.querySelector('#model-dialog')?.open,
      errors: window.__themeErrors,
    })`).catch(() => null);
    console.error(JSON.stringify({ case: label, state }));
    throw error;
  } finally {
    await send('Target.closeTarget', { targetId });
  }
}

try {
  await mkdir(output, { recursive: true });
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
  for (const device of devices) {
    for (const theme of themes) await checkTheme(device, theme);
  }
  await writeFile(path.join(output, 'report.json'), JSON.stringify({ runId, base, checks }, null, 2));
  console.log(`Verified ${checks.length} checks across ${themes.length} themes and ${devices.length} viewports`);
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
