#!/usr/bin/env node
/**
 * 真实浏览器实测：打开题库 → 编辑帕比司他题 → 检查预览 → 打开设置看 token 用量。
 * 输出 JSON：{steps, errors, editor, settings}
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const { spawn } = require('child_process');

const CANDIDATES = [
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
];
const BASE = process.argv[2] || 'http://127.0.0.1:8765/';
const OUT = process.argv[3] || 'data/_ui_editor.png';

function getJson(port, p) {
  return new Promise((resolve, reject) => {
    const req = http.get({ host: '127.0.0.1', port, path: p, timeout: 5000 }, (res) => {
      let d = '';
      res.setEncoding('utf8');
      res.on('data', (c) => { d += c; });
      res.on('end', () => { try { resolve(JSON.parse(d)); } catch (e) { reject(e); } });
    });
    req.on('error', reject);
    req.on('timeout', () => req.destroy(new Error('timeout')));
  });
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = CANDIDATES.find((p) => fs.existsSync(p));
  if (!browser) { console.error('no browser'); process.exit(3); }
  const userDir = fs.mkdtempSync(path.join(os.tmpdir(), 'mm-ui-'));
  const proc = spawn(browser, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--hide-scrollbars', '--remote-debugging-port=0', `--user-data-dir=${userDir}`,
    '--window-size=1500,1000', 'about:blank',
  ], { stdio: 'ignore' });

  const errors = [];
  let ws;
  try {
    const portFile = path.join(userDir, 'DevToolsActivePort');
    const deadline = Date.now() + 20000;
    while (!fs.existsSync(portFile) && Date.now() < deadline) await sleep(120);
    const port = parseInt(fs.readFileSync(portFile, 'utf8').split('\n')[0], 10);
    let targets = [];
    for (let i = 0; i < 20; i += 1) {
      try { targets = await getJson(port, '/json/list'); } catch (e) { /* retry */ }
      if (targets.some((t) => t.type === 'page')) break;
      await sleep(150);
    }
    const page = targets.find((t) => t.type === 'page');
    ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', () => rej(new Error('ws'))); });

    let id = 0;
    const pending = new Map();
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data.toString());
      if (msg.id && pending.has(msg.id)) {
        const { resolve, reject } = pending.get(msg.id);
        pending.delete(msg.id);
        if (msg.error) reject(new Error(JSON.stringify(msg.error)));
        else resolve(msg.result);
        return;
      }
      if (msg.method === 'Runtime.exceptionThrown') {
        const d = msg.params.exceptionDetails;
        errors.push(`EXCEPTION: ${d.text} ${d.exception?.description || ''}`.slice(0, 400));
      }
      if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') {
        const text = (msg.params.args || []).map((a) => a.value ?? a.description ?? '').join(' ');
        errors.push(`CONSOLE: ${text}`.slice(0, 400));
      }
    });
    const send = (method, params) => new Promise((resolve, reject) => {
      const mid = ++id;
      pending.set(mid, { resolve, reject });
      ws.send(JSON.stringify({ id: mid, method, params: params || {} }));
    });
    const evaluate = async (expression) => {
      const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
      if (r.exceptionDetails) errors.push(`EVAL: ${r.exceptionDetails.text} ${r.exceptionDetails.exception?.description || ''}`.slice(0, 400));
      return r.result?.value ?? null;
    };

    await send('Runtime.enable');
    await send('Log.enable');
    await send('Page.enable');
    await send('Emulation.setDeviceMetricsOverride', { width: 1500, height: 1000, deviceScaleFactor: 1, mobile: false });
    await send('Page.navigate', { url: BASE });
    await sleep(2800);

    // 1) 题库 → 编辑帕比司他题 → 等预览图加载
    const editor = await evaluate(`(async () => {
      const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
      document.querySelector('button[data-view="bank"]').click();
      await sleep(1800);
      const cards = [...document.querySelectorAll('.q-card')];
      const card = cards.find((c) => (c.textContent || '').includes('帕比司他'));
      if (!card) return { ok: false, reason: 'card not found', cards: cards.length };
      const btn = [...card.querySelectorAll('button')].find((b) => b.textContent.trim() === '编辑');
      if (!btn) return { ok: false, reason: 'edit button not found' };
      btn.click();
      await sleep(2200);
      const fig = document.querySelector('.preview-figure');
      const img = fig ? fig.querySelector('img') : null;
      if (img && !img.complete) {
        await new Promise((res) => { img.onload = img.onerror = res; setTimeout(res, 4000); });
      }
      return {
        ok: true,
        editorOpen: !document.querySelector('#editor').classList.contains('hidden'),
        figureShown: Boolean(img),
        imgSrc: img ? img.src.slice(0, 80) : '',
        natural: img ? [img.naturalWidth, img.naturalHeight] : null,
        shown: img ? [Math.round(img.getBoundingClientRect().width), Math.round(img.getBoundingClientRect().height)] : null,
        bodyText: (document.querySelector('#editor')?.textContent || '').slice(0, 120),
      };
    })()`);

    await sleep(300);
    if (OUT) {
      const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true, fromSurface: true });
      fs.writeFileSync(OUT, Buffer.from(shot.data, 'base64'));
    }

    // 2) 关编辑器 → 开设置 → 读 token 用量文案
    const settings = await evaluate(`(async () => {
      const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
      const closeBtn = document.querySelector('#editorClose');
      if (closeBtn) closeBtn.click();
      await sleep(500);
      document.querySelector('#btnSettings').click();
      await sleep(1800);
      const summary = document.querySelector('#logSummary')?.textContent || '';
      const rows = [...document.querySelectorAll('#logList .log-row')].slice(0, 3).map((r) => r.textContent.trim());
      const mask = document.querySelector('#settingsMask');
      return {
        open: mask && !mask.classList.contains('hidden'),
        summary,
        rows,
      };
    })()`);

    process.stdout.write(JSON.stringify({ editor, settings, errors }, null, 1));
  } catch (err) {
    process.stderr.write('ui test failed: ' + (err && err.message));
    process.exitCode = 4;
  } finally {
    try { if (ws) ws.close(); } catch (e) { /* ignore */ }
    try { proc.kill(); } catch (e) { /* ignore */ }
    setTimeout(() => {
      try { fs.rmSync(userDir, { recursive: true, force: true }); } catch (e) { /* ignore */ }
      process.exit(process.exitCode || 0);
    }, 200);
  }
})();
