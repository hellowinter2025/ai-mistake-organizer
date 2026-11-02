#!/usr/bin/env node
/**
 * 真实浏览器实测：导入图片后 100% 缩放是不是"整张图刚好放进预览区"。
 *
 * 造两张极端比例的图（竖拍 900×2400、横拍 2400×900），分别选中，
 * 量图片盒子与预览区可用尺寸，判断：
 *   1. 宽、高都不超出可用区域（不需要滚动条）；
 *   2. 至少有一边贴满（说明是按"宽高里更紧的那个"缩的，而不是缩得比需要的更小）。
 * 用法：node tools/fit_test.cjs http://127.0.0.1:8765/ data/_fit.png
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
const OUT = process.argv[3] || 'data/_fit.png';

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
  const userDir = fs.mkdtempSync(path.join(os.tmpdir(), 'mm-fit-'));
  const proc = spawn(browser, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--hide-scrollbars', '--remote-debugging-port=0', `--user-data-dir=${userDir}`,
    '--window-size=1440,960', 'about:blank',
  ], { stdio: 'ignore' });

  const errors = [];
  let ws;
  let nextId = 1;
  const pending = new Map();
  const send = (method, params = {}) => new Promise((resolve, reject) => {
    const id = nextId++;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async (expression) => {
    const res = await send('Runtime.evaluate', {
      expression, awaitPromise: true, returnByValue: true, allowUnsafeEvalBlockedByCSP: true,
    });
    if (res.exceptionDetails) {
      throw new Error(res.exceptionDetails.exception?.description || res.exceptionDetails.text || 'eval failed');
    }
    return res.result?.value;
  };

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
    ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && pending.has(msg.id)) {
        const { resolve, reject } = pending.get(msg.id);
        pending.delete(msg.id);
        if (msg.error) reject(new Error(msg.error.message)); else resolve(msg.result);
      }
      if (msg.method === 'Runtime.exceptionThrown') {
        errors.push(msg.params.exceptionDetails.exception?.description || 'exception');
      }
    });
    await send('Runtime.enable');
    await send('Page.enable');
    await send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 960, deviceScaleFactor: 1, mobile: false });
    await send('Page.navigate', { url: BASE });
    await sleep(2600);

    const result = await evaluate(`(async () => {
      const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
      // 切到识别页
      const nav = [...document.querySelectorAll('nav button, .tabs button, button[data-view]')]
        .find((b) => /识别/.test(b.textContent));
      if (nav) { nav.click(); await sleep(500); }

      const { state } = await import('/js/state.js');
      const { selectImage } = await import('/js/recognize.js');

      const makeImage = (w, h) => {
        const c = document.createElement('canvas');
        c.width = w; c.height = h;
        const g = c.getContext('2d');
        g.fillStyle = '#fff'; g.fillRect(0, 0, w, h);
        g.strokeStyle = '#222'; g.lineWidth = Math.max(2, w / 300);
        g.strokeRect(20, 20, w - 40, h - 40);
        g.font = Math.round(w / 12) + 'px serif';
        g.fillStyle = '#333';
        g.fillText(w + '×' + h, w / 6, h / 2);
        return c.toDataURL('image/png');
      };

      state.images = [
        { id: 'fit-tall', url: makeImage(900, 2600), thumb: '', width: 900, height: 2600, name: '竖拍', candidates: [], boxes: [] },
        { id: 'fit-wide', url: makeImage(2600, 900), thumb: '', width: 2600, height: 900, name: '横拍', candidates: [], boxes: [] },
      ];
      state.boxesByImage = { 'fit-tall': [], 'fit-wide': [] };

      const measure = async (id) => {
        selectImage(id);
        // 等图片解码 + applyZoom
        for (let i = 0; i < 40; i += 1) {
          await sleep(80);
          const img = document.querySelector('#canvasInner img');
          if (img && img.complete && img.naturalWidth) break;
        }
        await sleep(260);
        const scroll = document.querySelector('#canvasScroll');
        const img = document.querySelector('#canvasInner img');
        const inner = document.querySelector('#canvasInner');
        const box = img.getBoundingClientRect();
        const view = scroll.getBoundingClientRect();
        return {
          id,
          natural: img.naturalWidth + '×' + img.naturalHeight,
          css: Math.round(box.width) + '×' + Math.round(box.height),
          view: Math.round(view.width) + '×' + Math.round(view.height),
          overflowX: scroll.scrollWidth - scroll.clientWidth,
          overflowY: scroll.scrollHeight - scroll.clientHeight,
          innerW: Math.round(inner.getBoundingClientRect().width),
          label: document.querySelector('#zoomLabel')?.textContent || '',
        };
      };

      const tall = await measure('fit-tall');
      const wide = await measure('fit-wide');
      return { tall, wide, view: document.querySelector('#zoomRange')?.value };
    })()`);

    if (OUT) {
      const shot = await send('Page.captureScreenshot', { format: 'png', fromSurface: true });
      fs.writeFileSync(OUT, Buffer.from(shot.data, 'base64'));
    }
    process.stdout.write(JSON.stringify({ result, errors }, null, 1));
  } finally {
    try { ws && ws.close(); } catch (e) { /* ignore */ }
    try { proc.kill(); } catch (e) { /* ignore */ }
  }
})();
