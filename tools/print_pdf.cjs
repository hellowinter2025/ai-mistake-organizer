#!/usr/bin/env node
/**
 * 用 Edge/Chrome 无头模式把 HTML 打印成 A4 PDF。
 * 走 DevTools 协议，好处是可以用 headerTemplate/footerTemplate
 * 实现"每页左上角姓名、右上角日期 + 页脚页码"。
 *
 * 用法: node print_pdf.cjs <input.html> <output.pdf> <headerHtmlFile> <footerHtmlFile>
 * 失败时退出码非 0，由 Python 侧回退到命令行 --print-to-pdf。
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const { spawn } = require('child_process');
const { pathToFileURL } = require('url');

const [inputHtml, outputPdf, headerFile, footerFile, ...rest] = process.argv.slice(2);

// 可选：[宽 高 上 下 左 右]（英寸）。不传就按 A4。
// 裁切粘贴版会传 178×257mm，比 16 开窄一点，剪下来正好贴笔记本。
const nums = rest.map((v) => Number(v)).filter((v) => Number.isFinite(v) && v > 0);
const GEO = nums.length >= 2
  ? {
      paperWidth: nums[0],
      paperHeight: nums[1],
      marginTop: nums[2] || 0.4,
      marginBottom: nums[3] || 0.4,
      marginLeft: nums[4] || 0.3,
      marginRight: nums[5] || nums[4] || 0.3,
    }
  : null;

const CANDIDATES = [
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
];

function pickBrowser() {
  for (const p of CANDIDATES) {
    if (fs.existsSync(p)) return p;
  }
  return null;
}

function getJson(port, urlPath) {
  return new Promise((resolve, reject) => {
    const req = http.get({ host: '127.0.0.1', port, path: urlPath, timeout: 5000 }, (res) => {
      let data = '';
      res.setEncoding('utf8');
      res.on('data', (c) => { data += c; });
      res.on('end', () => {
        try { resolve(JSON.parse(data)); } catch (e) { reject(e); }
      });
    });
    req.on('error', reject);
    req.on('timeout', () => req.destroy(new Error('timeout')));
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitForPort(userDir, timeoutMs = 20000) {
  const portFile = path.join(userDir, 'DevToolsActivePort');
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (fs.existsSync(portFile)) {
      const txt = fs.readFileSync(portFile, 'utf8').split('\n').filter(Boolean);
      if (txt.length >= 1) return parseInt(txt[0], 10);
    }
    await sleep(120);
  }
  throw new Error('DevTools port not ready');
}

function cdp(ws) {
  let id = 0;
  const pending = new Map();
  ws.addEventListener('message', (ev) => {
    let msg;
    try { msg = JSON.parse(typeof ev.data === 'string' ? ev.data : ev.data.toString()); } catch (e) { return; }
    if (msg.id && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(new Error(JSON.stringify(msg.error)));
      else resolve(msg.result);
    }
  });
  return (method, params) => new Promise((resolve, reject) => {
    const mid = ++id;
    pending.set(mid, { resolve, reject });
    ws.send(JSON.stringify({ id: mid, method, params: params || {} }));
  });
}

(async () => {
  if (!inputHtml || !outputPdf) {
    process.stderr.write('usage: print_pdf.cjs <in.html> <out.pdf> [header.html] [footer.html]\n');
    process.exit(2);
  }
  const browser = pickBrowser();
  if (!browser) {
    process.stderr.write('no Edge/Chrome found');
    process.exit(3);
  }
  const userDir = fs.mkdtempSync(path.join(os.tmpdir(), 'mm-pdf-'));
  const url = pathToFileURL(path.resolve(inputHtml)).href;
  const proc = spawn(browser, [
    '--headless=new',
    '--disable-gpu',
    '--disable-extensions',
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-background-networking',
    '--allow-file-access-from-files',
    '--remote-debugging-port=0',
    `--user-data-dir=${userDir}`,
    '--window-size=1240,1754',
    url,
  ], { stdio: 'ignore' });

  let ws;
  try {
    const port = await waitForPort(userDir);
    let targets = [];
    for (let i = 0; i < 25; i += 1) {
      try {
        targets = await getJson(port, '/json/list');
        if (targets.some((t) => t.type === 'page')) break;
      } catch (e) { /* 继续等 */ }
      await sleep(200);
    }
    const page = targets.find((t) => t.type === 'page') || targets[0];
    if (!page || !page.webSocketDebuggerUrl) throw new Error('no page target');

    ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener('open', resolve);
      ws.addEventListener('error', () => reject(new Error('ws error')));
    });
    const send = cdp(ws);
    await send('Page.enable');
    await send('Runtime.enable');
    // 等待字体与 KaTeX 布局完成
    await send('Runtime.evaluate', {
      expression: 'document.fonts.ready.then(()=>true)',
      awaitPromise: true,
    });
    // 裁切粘贴版里每题右上角要标「预留 N 行」，数字是页面脚本按真实高度算的。
    // 不等到算完就打印，PDF 上会是一片空白。
    const hasCutout = await send('Runtime.evaluate', {
      expression: "!!document.querySelector('.cut-lines')",
      returnByValue: true,
    });
    if (hasCutout.result && hasCutout.result.value) {
      for (let i = 0; i < 40; i += 1) {
        const done = await send('Runtime.evaluate', {
          expression: "document.documentElement.dataset.measured === '1'",
          returnByValue: true,
        });
        if (done.result && done.result.value) break;
        await sleep(80);
      }
    }
    await sleep(320);
    const header = headerFile && fs.existsSync(headerFile) ? fs.readFileSync(headerFile, 'utf8') : '';
    const footer = footerFile && fs.existsSync(footerFile) ? fs.readFileSync(footerFile, 'utf8') : '';
    const result = await send('Page.printToPDF', {
      landscape: false,
      displayHeaderFooter: Boolean(header || footer),
      headerTemplate: header || '<span></span>',
      footerTemplate: footer || '<span></span>',
      printBackground: true,
      preferCSSPageSize: true,
      paperWidth: GEO ? GEO.paperWidth : 8.27,
      paperHeight: GEO ? GEO.paperHeight : 11.69,
      marginTop: GEO ? GEO.marginTop : (header ? 0.72 : 0.55),
      marginBottom: GEO ? GEO.marginBottom : (footer ? 0.6 : 0.55),
      marginLeft: GEO ? GEO.marginLeft : 0.62,
      marginRight: GEO ? GEO.marginRight : 0.62,
      scale: 1,
    });
    fs.writeFileSync(outputPdf, Buffer.from(result.data, 'base64'));
    process.stdout.write(JSON.stringify({ ok: true, bytes: fs.statSync(outputPdf).size }));
  } catch (err) {
    process.stderr.write('print failed: ' + (err && err.message));
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
