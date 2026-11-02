#!/usr/bin/env node
/**
 * 批量把 LaTeX 渲染成 KaTeX HTML。
 * 输入（stdin）: {"items":[{"latex":"x^2","display":false}, ...]}
 * 输出（stdout）: {"html":["<span class=...>", ...], "errors":[{"index":0,"message":"..."}]}
 *
 * 与前端使用同一份 KaTeX，保证 "界面预览 == 导出结果"。
 */
const path = require('path');
const fs = require('fs');

const katexPath = path.join(__dirname, '..', 'web', 'vendor', 'katex', 'katex.min.js');
if (!fs.existsSync(katexPath)) {
  process.stderr.write('katex not found: ' + katexPath);
  process.exit(2);
}
const katex = require(katexPath);

// mhchem 提供 \ce{}（化学式、方程式），是 KaTeX 官方 contrib。它是 UMD，
// 内部 require('katex')，这里把那次解析劫持到 vendor 这份，省掉再装一个 katex 包。
const mhchemPath = path.join(__dirname, '..', 'web', 'vendor', 'katex', 'contrib', 'mhchem.min.js');
if (fs.existsSync(mhchemPath)) {
  const Module = require('module');
  const originalResolve = Module._resolveFilename;
  Module._resolveFilename = function (request, ...rest) {
    if (request === 'katex') return katexPath;
    return originalResolve.call(this, request, ...rest);
  };
  try {
    require(mhchemPath);
  } catch (err) {
    process.stderr.write('mhchem load failed: ' + err.message + '\n');
  } finally {
    Module._resolveFilename = originalResolve;
  }
}

const MACROS = {
  '\\R': '\\mathbb{R}',
  '\\N': '\\mathbb{N}',
  '\\Z': '\\mathbb{Z}',
  '\\Q': '\\mathbb{Q}',
  '\\C': '\\mathbb{C}',
  '\\dx': '\\mathrm{d}x',
  '\\d': '\\mathrm{d}',
  '\\degree': '^{\\circ}',
};

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => { input += chunk; });
process.stdin.on('end', () => {
  let payload = {};
  try {
    payload = JSON.parse(input || '{}');
  } catch (err) {
    process.stderr.write('bad json: ' + err.message);
    process.exit(3);
  }
  const items = Array.isArray(payload.items) ? payload.items : [];
  const html = [];
  const errors = [];
  items.forEach((item, index) => {
    const latex = String(item.latex == null ? '' : item.latex);
    if (!latex.trim()) {
      html.push('');
      return;
    }
    try {
      html.push(katex.renderToString(latex, {
        displayMode: Boolean(item.display),
        throwOnError: true,
        strict: 'ignore',
        trust: false,
        output: 'html',
        macros: Object.assign({}, MACROS),
        maxSize: 40,
        maxExpand: 2000,
      }));
    } catch (err) {
      errors.push({ index, latex, message: String((err && err.message) || err) });
      html.push('');
    }
  });
  process.stdout.write(JSON.stringify({ html, errors }));
});
