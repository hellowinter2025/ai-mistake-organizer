#!/usr/bin/env node
/**
 * TikZ 渲染服务（常驻进程）。
 *
 * 为什么常驻：node-tikzjax 首次要把 core.dump + tex.wasm 解压进内存（约 500ms），
 * 每次调用单起进程等于每题都多付一次这个成本。这里读 stdin 行、写 stdout 行。
 *
 * 协议（一行一 JSON，stdout 只放响应，日志一律走 stderr）：
 *   启动     → 输出 `READY`
 *   请求     → {"id":1,"body":"\\begin{tikzpicture}…\\end{tikzpicture}","withPlots":false}
 *   响应     → {"id":1,"ok":true,"svg":"<svg …>"} / {"id":1,"ok":false,"error":"…"}
 *
 * body 只放 tikzpicture，document 环境与宏包由这里拼——模型不碰 preamble。
 */
const readline = require('readline');

let tex2svg = null;

function loadEngine() {
  if (!tex2svg) {
    // 库是 CJS，导出挂在 default 上
    const pkg = require('node-tikzjax');
    tex2svg = pkg.default || pkg;
  }
  return tex2svg;
}

function send(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

// TeX 的报错只出现在 showConsole 的输出里，`TeX engine render failed` 本身
// 不带任何线索。这里临时劫持 console 把日志收下来，再摘出真正有用的那几行。
const TEX_ERROR_LINE = /^(!|l\.\d+)|Error:|Undefined control sequence|No shape named|Unknown|Runaway|Missing|Extra \}/;

function digestLogs(logs) {
  const lines = logs
    .join('\n')
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean);
  const hits = lines.filter((l) => TEX_ERROR_LINE.test(l));
  const picked = (hits.length ? hits : lines.slice(-3)).slice(0, 3);
  return picked.join(' | ').replace(/\s+/g, ' ').slice(0, 300);
}

async function captureConsole(fn) {
  const logs = [];
  const saved = { log: console.log, warn: console.warn, error: console.error, info: console.info };
  const tap = (...args) => {
    logs.push(args.map((a) => (typeof a === 'string' ? a : String(a))).join(' '));
  };
  console.log = console.warn = console.error = console.info = tap;
  try {
    return { value: await fn(), logs };
  } catch (err) {
    if (err && typeof err === 'object') err.__logs = logs;
    throw err;
  } finally {
    console.log = saved.log;
    console.warn = saved.warn;
    console.error = saved.error;
    console.info = saved.info;
  }
}

async function compile(req) {
  const { id, body, withPlots } = req;
  if (typeof body !== 'string' || !body.trim()) {
    send({ id, ok: false, error: '空代码' });
    return;
  }
  // pgfplots 只在用到 axis/addplot 时加载：多一个宏包会多 0.5s 左右
  const options = withPlots
    ? {
        showConsole: true,
        texPackages: { tikz: '', pgfplots: '' },
        addToPreamble: '\\pgfplotsset{compat=1.11}',
      }
    : { showConsole: true, texPackages: { tikz: '' } };

  const tex = `\\begin{document}\n${body}\n\\end{document}`;
  try {
    const { value: svg, logs } = await captureConsole(() => loadEngine()(tex, options));
    send({ id, ok: true, svg });
    // 编译成功但 TeX 有告警（比如缺字形），留一份给日志排查
    const warn = logs.length ? digestLogs(logs) : '';
    if (warn) process.stderr.write(`tikz warn: ${warn}\n`);
  } catch (err) {
    const detail = digestLogs((err && err.__logs) || []) || String((err && err.message) || err);
    send({ id, ok: false, error: detail.slice(0, 400) });
  }
}


const rl = readline.createInterface({ input: process.stdin, terminal: false });

// TeX 引擎是单例，必须串行：并发 tex2svg 会互相踩内存
let chain = Promise.resolve();

rl.on('line', (line) => {
  const text = line.trim();
  if (!text) return;
  let req = null;
  try {
    req = JSON.parse(text);
  } catch {
    send({ id: null, ok: false, error: '请求不是合法 JSON' });
    return;
  }
  chain = chain.then(() => compile(req)).catch((err) => {
    send({ id: req && req.id, ok: false, error: String((err && err.message) || err) });
  });
});

rl.on('close', () => process.exit(0));

process.on('uncaughtException', (err) => {
  process.stderr.write(`tikz_server uncaught: ${err && err.stack ? err.stack : err}\n`);
  process.exit(1);
});

process.stdout.write('READY\n');
