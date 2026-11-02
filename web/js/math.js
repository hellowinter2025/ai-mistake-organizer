// 公式与文本渲染：直接构造 DOM，天然免疫 HTML 注入
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

export const mathStats = { rendered: 0, errors: [] };

export function resetMathStats() {
  mathStats.rendered = 0;
  mathStats.errors = [];
}

/** 把文本切成 text / math_inline / math_block 片段 */
export function tokenize(text) {
  const out = [];
  const src = String(text ?? '');
  let buf = '';
  let i = 0;
  const flush = () => { if (buf) { out.push({ type: 'text', value: buf }); buf = ''; } };

  while (i < src.length) {
    const ch = src[i];
    if (ch === '\\' && src[i + 1] === '$') { buf += '$'; i += 2; continue; }
    if (ch === '$' && src[i + 1] === '$') {
      const end = src.indexOf('$$', i + 2);
      if (end > i + 2) {
        flush();
        out.push({ type: 'display', value: src.slice(i + 2, end).trim() });
        i = end + 2;
        continue;
      }
      buf += '$$'; i += 2; continue;
    }
    if (ch === '$') {
      let j = i + 1;
      let end = -1;
      while (j < src.length) {
        if (src[j] === '\\') { j += 2; continue; }
        if (src[j] === '$') { end = j; break; }
        if (src[j] === '\n' && src[j + 1] === '\n') break;
        j += 1;
      }
      if (end > i + 1) {
        flush();
        out.push({ type: 'math', value: src.slice(i + 1, end).trim() });
        i = end + 1;
        continue;
      }
    }
    buf += ch;
    i += 1;
  }
  flush();
  return out;
}

function appendEmphasis(parent, line) {
  const re = /(\*\*[^*]+\*\*|`[^`]+`|(?<!\*)\*[^*\n]+\*(?!\*))/g;
  let last = 0;
  let m;
  while ((m = re.exec(line)) !== null) {
    if (m.index > last) parent.appendChild(document.createTextNode(line.slice(last, m.index)));
    const token = m[0];
    let el;
    if (token.startsWith('**')) { el = document.createElement('strong'); el.textContent = token.slice(2, -2); }
    else if (token.startsWith('`')) { el = document.createElement('code'); el.textContent = token.slice(1, -1); }
    else { el = document.createElement('em'); el.textContent = token.slice(1, -1); }
    parent.appendChild(el);
    last = m.index + token.length;
  }
  if (last < line.length) parent.appendChild(document.createTextNode(line.slice(last)));
}

function appendText(parent, value) {
  const lines = String(value).split('\n');
  lines.forEach((line, idx) => {
    if (line) appendEmphasis(parent, line);
    if (idx < lines.length - 1) parent.appendChild(document.createElement('br'));
  });
}

const TALL_MATH = /\\begin\{(?:[pbBvV]?matrix|cases|aligned|alignedat|array|gathered|split)\}|\\[\w]*frac\{\\frac/;

/**
 * 把**字面量** ``\n`` / ``\r`` / ``\t`` 还原成真换行（服务端也会做，这里是兜底）。
 *
 * 为什么前端还要再做一遍：队列是落 localStorage 的，早先解析出来的条目带着
 * 两个字面量的 `\n`，服务端改好也管不到浏览器里那份缓存，用户会一直看到
 * 「…A 正确。\nB：…」。渲染前过一道即可，不用重新解析。
 *
 * 不能误伤 TeX：`\ne`（≠）、`\to`、`\nu`、`\theta`、`\notin`、`\node` 全是命令。
 * 判据：反斜杠后面跟的是「非字母」或「大写字母」才当换行 —— `\nA. ` 是选项、
 * `\n所以` 是中文，而 TeX 命令名不会是大写开头，也不会只有一个 n。
 */
export function fixLiteralEscapes(value) {
  const text = String(value ?? '');
  if (!text.includes('\\')) return text;
  return text.replace(/\\([nrt])/g, (whole, ch, offset) => {
    const next = text[offset + 2] || '';
    if (/[a-zA-Z]/.test(next) && next === next.toLowerCase()) return whole;  // \ne \to \nu
    return ch === 'n' ? '\n' : ch === 'r' ? '\r' : '\t';
  });
}


/** 矩阵、分段函数、多行推导即使在行内书写也要独占一行 */
export function isTallMath(latex) {
  return TALL_MATH.test(latex || '');
}

/**
 * 渲染含公式的文本到容器。
 * @returns {{errors: Array<{latex:string,message:string}>}}
 */
export function renderRich(container, text, options = {}) {
  const errors = [];
  container.textContent = '';
  const { display = 'block', inline = false } = options;
  // 渲染前统一还原字面量 \n（旧数据 / 旧队列缓存里还带着两个字符的写法）
  for (const seg of tokenize(fixLiteralEscapes(text))) {
    if (seg.type === 'text') {
      appendText(container, seg.value);
      continue;
    }
    const isDisplay = seg.type === 'display' || isTallMath(seg.value);
    const el = document.createElement(isDisplay ? 'div' : 'span');
    el.className = isDisplay ? 'math-block' : 'math-inline';
    try {
      katex.render(seg.value, el, {
        displayMode: isDisplay,
        throwOnError: true,
        strict: 'ignore',
        trust: false,
        output: 'html',
        macros: MACROS,
        maxExpand: 2000,
        maxSize: 40,
      });
      mathStats.rendered += 1;
    } catch (err) {
      el.classList.add('math-error');
      el.textContent = `${isDisplay ? '$$' : '$'}${seg.value}${isDisplay ? '$$' : '$'}`;
      const item = { latex: seg.value, message: String((err && err.message) || err) };
      errors.push(item);
      mathStats.errors.push(item);
    }
    if (inline && isDisplay) el.classList.add('inline-display');
    container.appendChild(el);
  }
  markVerdicts(container);
  return { errors };
}

/** 给「正确 / 错误」上色（绿 / 红），一眼能扫出错因结论。
 *  只处理不在 KaTeX 子树里的纯文本节点，避免改坏公式。 */
const VERDICT_RE = /(正确|错误)(?![\u4e00-\u9fff])/g;

export function markVerdicts(root) {
  if (!root || !root.ownerDocument) return;
  const walker = root.ownerDocument.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const text = node.nodeValue;
      if (!text || text.length > 400) return NodeFilter.FILTER_REJECT;
      VERDICT_RE.lastIndex = 0;
      if (!VERDICT_RE.test(text)) return NodeFilter.FILTER_REJECT;
      if (node.parentElement && node.parentElement.closest('.katex')) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const targets = [];
  while (walker.nextNode()) targets.push(walker.currentNode);
  for (const node of targets) {
    const text = node.nodeValue;
    const frag = node.ownerDocument.createDocumentFragment();
    let last = 0;
    VERDICT_RE.lastIndex = 0;
    for (let m = VERDICT_RE.exec(text); m; m = VERDICT_RE.exec(text)) {
      if (m.index > last) frag.appendChild(node.ownerDocument.createTextNode(text.slice(last, m.index)));
      const span = node.ownerDocument.createElement('span');
      span.className = m[1] === '正确' ? 'ok' : 'bad';
      span.textContent = m[1];
      frag.appendChild(span);
      last = m.index + m[1].length;
    }
    if (last === 0) continue;
    if (last < text.length) frag.appendChild(node.ownerDocument.createTextNode(text.slice(last)));
    node.parentNode.replaceChild(frag, node);
  }
}

/** 纯文本摘要（用于列表、缩略描述） */export function plainSummary(text, max = 120) {
  let s = String(text || '')
    .replace(/\$\$([\s\S]*?)\$\$/g, ' [公式] ')
    .replace(/\$([^$\n]{1,80})\$/g, (m, g) => g.replace(/\\[a-zA-Z]+/g, '').slice(0, 12))
    .replace(/[#*`>]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  if (s.length > max) s = s.slice(0, max) + '…';
  return s || '（无内容）';
}

/** 校验单条公式，返回错误信息或 null */
export function checkLatex(latex, display = false) {
  const box = document.createElement('span');
  try {
    katex.render(String(latex), box, {
      displayMode: display, throwOnError: true, strict: 'ignore', trust: false, macros: MACROS,
    });
    return null;
  } catch (err) {
    return String((err && err.message) || err);
  }
}

/** 统计文本里的公式数量，用于导出前后的校验 */
export function countFormulas(text) {
  return tokenize(text).filter((s) => s.type !== 'text').length;
}

/**
 * 渲染 AI 生成的题图。SVG 属于不可信内容：先经 DOMPurify 的 SVG 白名单过滤，
 * 再插入 DOM，服务端还会再过滤一次（双重保险）。
 */
/**
 * 渲染题图：只放服务端栅格化的 PNG，前端零 SVG 处理。
 *
 * 传入值优先级：
 *   1. 图片地址（/api/... 或 data:image/...）→ 直接 <img>
 *   2. SVG 源码 → 包成 data:image/svg+xml（<img> 里的 SVG 不执行脚本，
 *      本身就是安全的），仅在服务端没给出 PNG 地址时兜底
 * 预览与 PDF/DOCX 都出自服务端同一份 svg_to_png，所见即所得。
 */
export function renderFigure(container, src) {
  container.textContent = '';
  const raw = String(src || '').trim();
  if (!raw) return false;
  let url = '';
  if (raw.startsWith('data:image/') || raw.startsWith('/') || /^https?:\/\//.test(raw)) {
    url = raw;
  } else if (raw.toLowerCase().includes('<svg')) {
    url = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(raw);
  } else {
    return false;
  }
  const img = document.createElement('img');
  img.alt = '题图';
  img.src = url;
  img.style.maxWidth = '100%';
  img.style.height = 'auto';
  container.appendChild(img);
  return true;
}
