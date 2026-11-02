#!/usr/bin/env node
/**
 * 队列状态探针。
 *
 * 预置三条不同状态的队列项（待入库 / 失败 / 已入库），刷新页面验证：
 *   1. 刷新后队列能恢复（localStorage）
 *   2. 三种状态的徽标、副标题、按钮是否正确
 *   3. 点「入库」后是否真的变成「已入库」，进度条是否跟着走
 *   4. 已入库的项点开是不是「编辑题目」（而不是空草稿）
 *
 * 用法：node tools/queue_probe.cjs <url> <out.png> <imageId>
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const { spawn } = require('child_process');

const [url, outPng, imageId] = process.argv.slice(2);

const CANDIDATES = [
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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

(async () => {
  const browser = CANDIDATES.find((p) => fs.existsSync(p));
  if (!browser) { console.error('no browser'); process.exit(3); }
  const userDir = fs.mkdtempSync(path.join(os.tmpdir(), 'mm-probe-'));
  const proc = spawn(browser, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--hide-scrollbars', '--remote-debugging-port=0', `--user-data-dir=${userDir}`,
    '--window-size=1600,1000', 'about:blank',
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
    await new Promise((res, rej) => {
      ws.addEventListener('open', res);
      ws.addEventListener('error', () => rej(new Error('ws')));
    });

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
    const evalJs = async (expression) => {
      const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
      if (r.exceptionDetails) throw new Error(r.exceptionDetails.text + ' ' + (r.exceptionDetails.exception?.description || ''));
      return r.result?.value ?? null;
    };

    await send('Runtime.enable');
    await send('Page.enable');
    await send('DOM.enable');
    await send('Emulation.setDeviceMetricsOverride', { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false });
    await send('Page.navigate', { url });
    await sleep(2600);

    // ---- 1. 预置三条队列项，然后刷新，验证恢复 ----
    const seed = await evalJs(`(() => {
      const img = ${JSON.stringify(String(imageId || ''))};
      const items = [
        { id: 'q1', boxId: 'a0', imageId: img,
          bbox: { x: 0.08, y: 0.10, w: 0.80, h: 0.16 }, status: 'done', attempts: 1,
          fields: { question_text: '已知 $x^2-5x+6=0$，求两根之和。', answer: '5',
                    error_analysis: '把判别式算错了，符号写反。', solution_fix: '由韦达定理 $x_1+x_2=5$。',
                    question_type: '填空题' },
          meta: { latency_ms: 2100, strategy: 'json:strict', model: 'probe' },
          crop: { url: '/api/crops/' + img } },
        { id: 'q2', boxId: 'a1', imageId: img,
          bbox: { x: 0.08, y: 0.30, w: 0.80, h: 0.16 }, status: 'error', attempts: 2,
          error: 'AI 接口返回 503：busy', errorKind: 'upstream', fields: null, crop: null },
        { id: 'q3', boxId: 'a2', imageId: img,
          bbox: { x: 0.08, y: 0.50, w: 0.80, h: 0.16 }, status: 'saved', attempts: 1,
          savedUid: 'probe-uid', savedAt: Date.now() - 120000,
          fields: { question_text: '已经入库的题', question_type: '解答题' },
          question: { uid: 'probe-uid', stem: '已经入库的题', question_type: '解答题' },
          crop: null },
        { id: 'q4', boxId: 'a3', imageId: img,
          bbox: { x: 0.08, y: 0.70, w: 0.80, h: 0.16 }, status: 'wait', attempts: 0,
          fields: null, crop: null },
      ];
      localStorage.setItem('mm_queue_v2', JSON.stringify({ v: 2, at: Date.now(), items }));
      return items.length;
    })()`);

    await send('Page.reload');
    await sleep(3200);

    // ---- 关键：拦截真实接口 ----
    // 探针会点「重试」和「入库」，那会**真的花掉一次 AI 调用 / 真的写库**。
    // 这里把 /api/ai/solve 和 POST /api/questions 换成桩，只验证前端状态流转。
    const stubbed = await evalJs(`(() => {
      const orig = window.fetch;
      const hits = { solve: 0, save: 0 };
      const reply = (obj, status) => Promise.resolve(new Response(JSON.stringify(obj), {
        status: status || 200, headers: { 'Content-Type': 'application/json' },
      }));
      window.fetch = (input, init) => {
        const url = typeof input === 'string' ? input : (input && input.url) || '';
        const method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
        if (url.indexOf('/api/ai/solve') >= 0) {
          hits.solve += 1;
          return reply({ detail: '探针拦截：没有调用真实 AI 接口' }, 503);
        }
        if (url.indexOf('/api/questions') >= 0 && method === 'POST') {
          hits.save += 1;
          return reply({ question: { uid: 'probe-saved-uid', stem: '探针模拟入库', subject: '数学' } });
        }
        return orig(input, init);
      };
      window.__probeHits = () => hits;
      return hits;
    })()`);

    // 诊断：刷新后 localStorage 还在不在、加载的 JS 里有没有 restoreQueue
    const diag = await evalJs(`(async () => {
      const raw = localStorage.getItem('mm_queue_v2');
      let parsed = null;
      try { parsed = JSON.parse(raw || 'null'); } catch (e) { parsed = { parseError: String(e) }; }
      let hasRestore = null;
      try {
        const txt = await fetch('/js/recognize.js', { cache: 'no-store' }).then((r) => r.text());
        hasRestore = txt.includes('function restoreQueue');
      } catch (e) { hasRestore = 'fetch-error'; }
      return {
        origin: location.origin,
        rawLen: raw ? raw.length : 0,
        itemCount: parsed && parsed.items ? parsed.items.length : -1,
        firstStatus: parsed && parsed.items && parsed.items[0] ? parsed.items[0].status : null,
        hasRestore,
        queueInPage: window.__mmQueueLen ?? null,
      };
    })()`);

    const snap = async () => evalJs(`(() => {
      const rows = [...document.querySelectorAll('.queue-item')];
      return {
        rows: rows.length,
        badges: rows.map((r) => r.querySelector('.badge')?.textContent),
        subs: rows.map((r) => r.querySelector('.qsub')?.textContent),
        ops: rows.map((r) => [...r.querySelectorAll('.qops button')].map((b) => b.textContent)),
        titles: rows.map((r) => r.querySelector('.qtitle')?.textContent),
        count: document.querySelector('#queueCount')?.textContent,
        progress: document.querySelector('#queueProgress')?.classList.contains('hidden')
          ? null : document.querySelector('#queueProgressText')?.textContent,
        barWidth: document.querySelector('#queueProgressBar')?.style.width || null,
        head: {
          continueBtn: !document.querySelector('#btnContinueQueue')?.classList.contains('hidden'),
          retryBtn: document.querySelector('#btnRetryFailed')?.classList.contains('hidden')
            ? null : document.querySelector('#btnRetryFailed')?.textContent,
          clearSaved: !document.querySelector('#btnClearSaved')?.classList.contains('hidden'),
        },
        toasts: [...document.querySelectorAll('.toast')].map((t) => t.textContent),
      };
    })()`);

    const before = await snap();

    // ---- 2. 点第一条（待入库）的「入库」 ----
    const saved = await evalJs(`(async () => {
      const s = (ms) => new Promise((r) => setTimeout(r, ms));
      const row = [...document.querySelectorAll('.queue-item')]
        .find((r) => r.querySelector('.badge')?.textContent === '待入库');
      if (!row) return { error: '没有待入库的行' };
      const btn = [...row.querySelectorAll('.qops button')].find((b) => b.textContent === '入库');
      if (!btn) return { error: '没有入库按钮' };
      btn.click();
      await s(1600);
      const target = document.querySelector('.queue-item .badge.saved') ? true : false;
      const rows = [...document.querySelectorAll('.queue-item')];
      return {
        afterBadges: rows.map((r) => r.querySelector('.badge')?.textContent),
        afterSubs: rows.map((r) => r.querySelector('.qsub')?.textContent),
        progress: document.querySelector('#queueProgress')?.classList.contains('hidden')
          ? null : document.querySelector('#queueProgressText')?.textContent,
        hasSaved: target,
        toasts: [...document.querySelectorAll('.toast')].map((t) => t.textContent),
      };
    })()`);

    // ---- 3. 点「重试」：失败的题回到解析中，再失败时 attempts 累加 ----
    const retried = await evalJs(`(async () => {
      const s = (ms) => new Promise((r) => setTimeout(r, ms));
      window.confirm = () => true;
      const btn = document.querySelector('#btnRetryFailed');
      if (!btn) return { error: '没有重试按钮' };
      const badges = () => [...document.querySelectorAll('.queue-item')].map((r) => r.querySelector('.badge')?.textContent);
      const before = badges();
      btn.click();
      await s(400);
      const during = badges();
      await s(4000);
      const rows = [...document.querySelectorAll('.queue-item')];
      return {
        before,
        during,
        after: badges(),
        subs: rows.map((r) => r.querySelector('.qsub')?.textContent),
        retryBtn: document.querySelector('#btnRetryFailed')?.classList.contains('hidden')
          ? null : document.querySelector('#btnRetryFailed')?.textContent,
        continueBtn: document.querySelector('#btnContinueQueue')?.classList.contains('hidden')
          ? null : document.querySelector('#btnContinueQueue')?.textContent,
        toasts: [...document.querySelectorAll('.toast')].map((t) => t.textContent).slice(-3),
      };
    })()`);

    // ---- 3. 点开已入库的项，确认是「编辑题目」而非空草稿 ----
    const editor = await evalJs(`(async () => {
      const s = (ms) => new Promise((r) => setTimeout(r, ms));
      window.confirm = () => true;
      const row = [...document.querySelectorAll('.queue-item.saved')][0];
      if (!row) return { error: '没有已入库的行' };
      row.click();
      await s(900);
      return {
        title: document.querySelector('#editorTitle')?.textContent,
        visible: !document.querySelector('#editor')?.classList.contains('hidden'),
        stem: (document.querySelector('#fStem')?.value || '').slice(0, 40),
        deleteShown: !document.querySelector('#btnDeleteQuestion')?.classList.contains('hidden'),
      };
    })()`);

    await evalJs(`(() => { window.confirm = () => true; document.querySelector('#editorClose')?.click(); return 1; })()`);
    await sleep(500);

    const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true, fromSurface: true });
    fs.writeFileSync(outPng, Buffer.from(shot.data, 'base64'));

    const hits = await evalJs('window.__probeHits ? window.__probeHits() : null');
    process.stdout.write(JSON.stringify({ seeded: seed, stubbed, hits, diag, before, saved, retried, editor, errors }, null, 1));
  } catch (err) {
    process.stderr.write('probe failed: ' + (err && err.message));
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
