#!/usr/bin/env node
/**
 * 页面巡检：打开本地页面，收集控制台错误/异常，截图并可选导出 DOM 或执行脚本。
 *
 * 用法：
 *   node tools/page_shot.cjs <url> <out.png> [--wait=2500] [--eval="expr"] [--dom]
 * 输出：JSON {url, errors:[], warnings:[], eval:any}
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const { spawn } = require('child_process');

const args = process.argv.slice(2);
const url = args[0];
const outPng = args[1];
const opt = { wait: 2500, evalExpr: null, dom: false, upload: null, uploadWait: 3500, width: 1600, height: 1000 };
for (const a of args.slice(2)) {
  if (a.startsWith('--wait=')) opt.wait = parseInt(a.slice(7), 10);
  else if (a.startsWith('--eval=')) opt.evalExpr = a.slice(7);
  else if (a === '--dom') opt.dom = true;
  else if (a.startsWith('--upload=')) opt.upload = a.slice(9);
  else if (a.startsWith('--upload-wait=')) opt.uploadWait = parseInt(a.slice(14), 10);
  else if (a.startsWith('--viewport=')) {
    const [w, h] = a.slice(11).split('x').map((v) => parseInt(v, 10));
    if (w && h) { opt.width = w; opt.height = h; }
  }
}

const CANDIDATES = [
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
];

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
  const userDir = fs.mkdtempSync(path.join(os.tmpdir(), 'mm-shot-'));
  const proc = spawn(browser, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--hide-scrollbars', '--remote-debugging-port=0', `--user-data-dir=${userDir}`,
    '--window-size=1600,1000', 'about:blank',
  ], { stdio: 'ignore' });

  let ws;
  const errors = [];
  const warnings = [];
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
        errors.push(`EXCEPTION: ${d.text} ${d.exception?.description || ''}`.slice(0, 500));
      }
      if (msg.method === 'Runtime.consoleAPICalled') {
        const text = (msg.params.args || []).map((a) => a.value ?? a.description ?? '').join(' ');
        if (msg.params.type === 'error') errors.push(`CONSOLE: ${text}`.slice(0, 500));
        if (msg.params.type === 'warning') warnings.push(text.slice(0, 300));
      }
      if (msg.method === 'Log.entryAdded') {
        const e = msg.params.entry;
        if (e.level === 'error') errors.push(`LOG: ${e.text} ${e.url || ''}`.slice(0, 500));
        if (e.level === 'warning') warnings.push(e.text.slice(0, 300));
      }
    });
    const send = (method, params) => new Promise((resolve, reject) => {
      const mid = ++id;
      pending.set(mid, { resolve, reject });
      ws.send(JSON.stringify({ id: mid, method, params: params || {} }));
    });

    await send('Runtime.enable');
    await send('Log.enable');
    await send('Page.enable');
    await send('DOM.enable');
    await send('Emulation.setDeviceMetricsOverride', { width: opt.width, height: opt.height, deviceScaleFactor: 1, mobile: false });
    await send('Page.navigate', { url });
    await sleep(opt.wait);
    await send('Runtime.evaluate', { expression: 'document.fonts.ready.then(()=>1)', awaitPromise: true });

    if (opt.upload) {
      const [selector, file] = opt.upload.split('::');
      const doc = await send('DOM.getDocument', { depth: -1 });
      const node = await send('DOM.querySelector', { nodeId: doc.root.nodeId, selector });
      if (node.nodeId) {
        await send('DOM.setFileInputFiles', { nodeId: node.nodeId, files: [path.resolve(file)] });
        await sleep(opt.uploadWait);
      } else {
        errors.push(`upload selector not found: ${selector}`);
      }
    }

    let evalResult = null;
    if (opt.evalExpr) {
      const r = await send('Runtime.evaluate', { expression: opt.evalExpr, awaitPromise: true, returnByValue: true });
      evalResult = r.result?.value ?? r.result?.description ?? null;
    }
    if (opt.dom) {
      const r = await send('Runtime.evaluate', { expression: 'document.documentElement.outerHTML', returnByValue: true });
      fs.writeFileSync(outPng.replace(/\.png$/, '') + '.html', r.result.value || '', 'utf8');
    }
    if (outPng) {
      const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true, fromSurface: true });
      fs.writeFileSync(outPng, Buffer.from(shot.data, 'base64'));
    }
    process.stdout.write(JSON.stringify({ url, errors, warnings, eval: evalResult }, null, 1));
  } catch (err) {
    process.stderr.write('shot failed: ' + (err && err.message));
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
