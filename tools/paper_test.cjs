#!/usr/bin/env node
/**
 * 组卷页真实浏览器巡检：按学科归类 / 批量清空 / 新建试卷 / 试卷库清空 / 横向溢出。
 *
 *   node tools/paper_test.cjs                          # 默认 1080×620
 *   node tools/paper_test.cjs http://127.0.0.1:8765/ 1440 900 data/_paper.png
 *
 * 题单从接口现取（借页面自己的 ESM 模块拿 token，不用另配鉴权），
 * 取不到就跳过归类相关断言。所有破坏性操作都会先拦 confirm，不会真删数据。
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
const W = Number(process.argv[3] || 1080);
const H = Number(process.argv[4] || 620);
const OUT = process.argv[5] || '';

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
  const userDir = fs.mkdtempSync(path.join(os.tmpdir(), 'mm-pp-'));
  const proc = spawn(browser, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--hide-scrollbars', '--remote-debugging-port=0', `--user-data-dir=${userDir}`,
    `--window-size=${W},${H}`, 'about:blank',
  ], { stdio: 'ignore' });

  let ws;
  const errors = [];
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
        errors.push(`EXCEPTION: ${msg.params.exceptionDetails.text}`.slice(0, 300));
      }
      if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') {
        errors.push(`CONSOLE: ${(msg.params.args || []).map((a) => a.value ?? a.description ?? '').join(' ')}`.slice(0, 300));
      }
    });
    const send = (method, params) => new Promise((resolve, reject) => {
      const mid = ++id;
      pending.set(mid, { resolve, reject });
      ws.send(JSON.stringify({ id: mid, method, params: params || {} }));
    });
    const evaluate = async (expression) => {
      const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
      if (r.exceptionDetails) errors.push(`EVAL: ${r.exceptionDetails.text}`.slice(0, 300));
      return r.result?.value ?? null;
    };

    await send('Runtime.enable');
    await send('Page.enable');
    await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });
    await send('Page.navigate', { url: BASE });
    await sleep(2600);
    // 借页面自己的 api 模块取题单（它带着 X-App-Token），塞进 localStorage 再刷新
    const seeded = await evaluate(`(async () => {
      try {
        const api = await import('/js/api.js');
        const data = await api.get('/api/questions?limit=300');
        const items = data.items || [];
        const bySubject = new Map();
        const picked = [];
        for (const q of items) {
          const s = q.subject || '';
          const n = bySubject.get(s) || 0;
          if (n < 2) { picked.push(q.uid); bySubject.set(s, n + 1); }
        }
        localStorage.setItem('mm_cart', JSON.stringify(picked));
        return { ok: true, count: picked.length, subjects: [...bySubject.keys()] };
      } catch (e) { return { ok: false, reason: String(e) }; }
    })()`);
    if (seeded?.ok) {
      await send('Page.navigate', { url: BASE });
      await sleep(2400);
    }
    await evaluate('window.confirm = () => true; true');

    const result = await evaluate(`(async () => {
      const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
      const btn = (t) => [...document.querySelectorAll('button')].find((b) => b.textContent.trim() === t);
      document.querySelector('button[data-view="paper"]').click();
      await sleep(1400);
      const out = {};
      const docW = () => document.documentElement.scrollWidth;
      const vw = document.documentElement.clientWidth;
      out.vw = vw;
      out.docWBefore = docW();
      out.rowsBefore = document.querySelectorAll('.paper-row').length;

      const orderBefore = [...document.querySelectorAll('.paper-row .txt')].map((n) => n.textContent.trim().slice(0, 8));
      out.runsBefore = document.querySelectorAll('.paper-group').length;
      btn('按学科归类')?.click();
      await sleep(900);
      out.groups = [...document.querySelectorAll('.paper-group')].map((n) => n.textContent.trim());
      out.runsAfter = document.querySelectorAll('.paper-group').length;
      out.rowsAfter = document.querySelectorAll('.paper-row').length;
      out.docWAfterGroup = docW();
      out.count = document.querySelector('#paperItemCount')?.textContent;
      const orderAfter = [...document.querySelectorAll('.paper-row .txt')].map((n) => n.textContent.trim().slice(0, 8));
      out.reordered = orderBefore.join('|') !== orderAfter.join('|');
      return out;
    })()`);

    if (OUT) {
      const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true, fromSurface: true });
      fs.writeFileSync(OUT, Buffer.from(shot.data, 'base64'));
    }

    const after = await evaluate(`(async () => {
      const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
      const btn = (t) => [...document.querySelectorAll('button')].find((b) => b.textContent.trim() === t);
      const out = {};

      // 试卷库「清空」：先取消，确认不会真删用户的卷子
      out.papersBefore = document.querySelectorAll('.saved-row').length;
      window.confirm = () => false;
      btn('清空')?.click();
      await sleep(800);
      out.papersAfterCancel = document.querySelectorAll('.saved-row').length;
      out.clearPapersBtn = Boolean(document.querySelector('#btnClearPapers'));

      // 批量清空
      window.confirm = () => true;
      btn('批量清空')?.click();
      await sleep(700);
      out.rowsAfterClear = document.querySelectorAll('.paper-row').length;
      out.emptyNote = document.querySelector('#paperItems .empty-note')?.textContent || '';

      // 新建试卷（题目已空，主要验证表单复位）
      document.querySelector('#paperTitle').value = '临时标题';
      document.querySelector('#paperSubtitle').value = '临时副标题';
      btn('新建试卷')?.click();
      await sleep(600);
      out.titleAfterNew = document.querySelector('#paperTitle').value;
      out.subtitleAfterNew = document.querySelector('#paperSubtitle').value;
      out.docWEnd = document.documentElement.scrollWidth;
      return out;
    })()`);

    process.stdout.write(JSON.stringify({ seeded, result: { ...result, ...after }, errors }, null, 1));
  } catch (err) {
    process.stderr.write('paper check failed: ' + (err && err.message));
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
