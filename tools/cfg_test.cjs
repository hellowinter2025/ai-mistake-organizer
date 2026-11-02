#!/usr/bin/env node
/**
 * 真实浏览器实测：设置面板里的「添加 / 编辑服务」。
 *
 * 覆盖：展开折叠区 → 填名称/地址/Key/模型 → 保存 → 下拉框出现新条目 →
 *       再次点开能回填（Key 留空、显示掩码提示）→ 删除。
 * 全程走真实 HTTP 接口，测完把自己造的配置删掉，不留垃圾。
 *
 * 用法：node tools/cfg_test.cjs [base] [outPng]
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
const OUT = process.argv[3] || 'data/_cfg_ui.png';
const NAME = `__UI测试_${Date.now()}`;

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
  const userDir = fs.mkdtempSync(path.join(os.tmpdir(), 'mm-cfg-'));
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
    const evaluate = async (expression) => {
      const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
      if (r.exceptionDetails) {
        errors.push(`EVAL: ${r.exceptionDetails.text} ${r.exceptionDetails.exception?.description || ''}`.slice(0, 400));
      }
      return r.result?.value ?? null;
    };

    await send('Runtime.enable');
    await send('Log.enable');
    await send('Page.enable');
    await send('Emulation.setDeviceMetricsOverride', { width: 1500, height: 1000, deviceScaleFactor: 1, mobile: false });
    await send('Page.navigate', { url: BASE });
    await sleep(2600);

    const result = await evaluate(`(async () => {
      const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
      const $ = (s) => document.querySelector(s);
      const out = { steps: [] };
      $('#btnSettings').click();
      await sleep(1200);
      out.steps.push('open-settings');

      const editor = $('#cfgEditor');
      if (!editor) return { ok: false, reason: '#cfgEditor 不存在' };
      editor.open = true;
      await sleep(200);
      out.steps.push('expand-details');
      // 记下测试前已有的配置，收尾时核对一条都没少
      out.before = [...document.querySelectorAll('#sConfig option')].map((o) => o.textContent);

      $('#sCfgName').value = ${JSON.stringify(NAME)};
      // 必须先点「新建一条」：否则表单里还是当前服务，保存就变成改别人的配置
      $('#btnNewConfig').click();
      await sleep(300);
      out.steps.push('click-new');
      out.namedBeforeSave = $('#sCfgName').value;
      $('#sCfgName').value = ${JSON.stringify(NAME)};
      $('#sCfgBase').value = 'https://ui.example.com/v1/';
      $('#sCfgKey').value = 'sk-ui-test-1234567890';
      $('#sCfgModels').value = 'ui-model-a, ui-model-b';
      $('#btnSaveConfig').click();
      await sleep(1600);
      out.steps.push('save');

      const opts = [...document.querySelectorAll('#sConfig option')].map((o) => o.textContent);
      out.options = opts;
      out.picked = $('#sConfig').value;
      out.models = [...document.querySelectorAll('#sModel option')].map((o) => o.textContent);
      out.hint = $('#cfgHint').textContent;
      out.keyPlaceholder = $('#sCfgKey').placeholder;
      out.keyFilled = $('#sCfgKey').value;
      out.deleteEnabled = !$('#btnDeleteConfig').disabled;
      out.chip = $('#aiStatus').textContent;
      out.hasName = opts.some((t) => t.includes('__UI测试_'));
      out.editorOpenAfterSave = !$('#settingsMask').classList.contains('hidden');

      // 再存一次（不带 key，验证「沿用旧 key」不会把 has_key 弄丢）
      $('#sCfgName').value = ${JSON.stringify(NAME + '_改')};
      $('#btnSaveConfig').click();
      await sleep(1400);
      out.steps.push('save-again');
      out.renamed = [...document.querySelectorAll('#sConfig option')].some((o) => o.textContent.includes('_改'));
      out.keyPlaceholder2 = $('#sCfgKey').placeholder;
      return out;
    })()`);

    await sleep(300);
    if (OUT) {
      const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true, fromSurface: true });
      fs.writeFileSync(OUT, Buffer.from(shot.data, 'base64'));
    }

    // 收尾：删掉测试配置，别留垃圾
    const cleanup = await evaluate(`(async () => {
      const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
      const $ = (s) => document.querySelector(s);
      const sel = $('#sConfig');
      const opt = [...sel.options].find((o) => o.textContent.includes('__UI测试_'));
      if (!opt) return { deleted: false, reason: 'not found' };
      sel.value = opt.value;
      sel.dispatchEvent(new Event('change'));
      await sleep(400);
      const realConfirm = window.confirm;
      window.confirm = () => true;
      $('#btnDeleteConfig').click();
      await sleep(1500);
      window.confirm = realConfirm;
      const left = [...sel.options].filter((o) => o.textContent.includes('__UI测试_')).length;
      return {
        deleted: left === 0,
        left,
        after: [...sel.options].map((o) => o.textContent),
      };
    })()`);

    // 安全网：收尾后原有配置必须条目齐全，少一条说明删错了对象
    const before = (result?.before || []).filter((t) => !t.includes('__UI测试_'));
    const after = (cleanup?.after || []).filter((t) => !t.includes('__UI测试_'));
    const lost = before.filter((t) => !after.includes(t));
    if (lost.length) errors.push(`WRONG-DELETE: 测试后丢失了原有配置 ${JSON.stringify(lost)}`);
    cleanup.lost = lost;

    console.log(JSON.stringify({ result, cleanup, errors }, null, 2));
  } finally {
    try { ws?.close(); } catch (e) { /* ignore */ }
    try { proc.kill(); } catch (e) { /* ignore */ }
    try { fs.rmSync(userDir, { recursive: true, force: true }); } catch (e) { /* ignore */ }
  }
})();
