// 组卷与导出
import { get, post, put, del, saveToDownloads } from './api.js';
import { renderRich } from './math.js';
import { state, on, saveCart } from './state.js';
import { $, toast, fmtDate, todayISO, show, hide, setBusy } from './ui.js';
import { updateCartBadge } from './bank.js';

const el = {};
let cartData = [];

export function initPaper() {
  el.title = $('#paperTitle');
  el.subtitle = $('#paperSubtitle');
  el.source = $('#paperSource');
  el.subject = $('#paperSubject');
  el.layout = $('#paperLayout');
  el.layoutHint = $('#layoutHint');
  el.form = document.querySelector('.paper-form');
  el.name = $('#paperName');
  el.date = $('#paperDate');
  el.items = $('#paperItems');
  el.count = $('#paperItemCount');
  el.saved = $('#savedPapers');

  el.date.value = todayISO();
  const exp = state.settings?.export || {};
  el.title.value = defaultTitle();
  el.title.placeholder = defaultTitle();
  el.subject.value = state.settings?.general?.subject || '数学';
  if (exp.student_name) el.name.value = exp.student_name;
  applyLayout(el.layout?.querySelector('button.active')?.dataset.value || 'standard');

  el.layout?.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-value]');
    if (!btn) return;
    el.layout.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b === btn));
    applyLayout(btn.dataset.value);
  });
  $('#answerPosition').addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-value]');
    if (!btn) return;
    $('#answerPosition').querySelectorAll('button').forEach((b) => b.classList.toggle('active', b === btn));
  });
  $('#btnPaperClear').addEventListener('click', clearAll);
  $('#btnNewPaper')?.addEventListener('click', newPaper);
  $('#btnAutoLayout')?.addEventListener('click', groupBySubject);
  $('#btnExportPdf').addEventListener('click', () => doExport('pdf'));
  $('#btnExportDocx').addEventListener('click', () => doExport('docx'));
  $('#btnPreviewPaper').addEventListener('click', previewPaper);
  $('#btnReloadPapers')?.addEventListener('click', () => loadSaved());
  $('#btnClearPapers')?.addEventListener('click', clearSavedPapers);
  $('#previewClose').addEventListener('click', () => hide($('#previewMask')));
  $('#previewMask').addEventListener('click', (e) => { if (e.target.id === 'previewMask') hide($('#previewMask')); });

  on('cart:changed', refreshCart);
  updateCartBadge();
  refreshCart();
  loadSaved();
}

const LAYOUT_HINTS = {
  standard: 'A4 整卷：题干 + 错因 + 解析 + 答案',
  cutout: '裁切粘贴：仍是 A4 纸，只有题目和错因；正文收在中间 158mm，四周留 24~26mm，剪成 16 开也切不到字。每题右上角会标出「预留 N 行」（0.9cm 一行），第一题上方也有剪切线。导出文件名自动带「-裁切」',
};

// 学科固定顺序：同一学科的题排在一起，导出也就是这个顺序
const SUBJECT_ORDER = ['数学', '物理', '化学', '生物', '语文', '英语', '历史', '地理', '政治', '科学'];

function subjectRank(subject) {
  const i = SUBJECT_ORDER.indexOf(String(subject || '').trim());
  return i < 0 ? SUBJECT_ORDER.length : i;
}

function subjectOf(q) {
  return String(q?.subject || '').trim() || '未标注';
}

/**
 * 按学科归类：把**同一学科**的题目挪到一起（学科之间按固定顺序），
 * 学科内部保持原来的先后。不动排版参数、不预留空白行——
 * 一次组卷里混着数学和化学时，卷面上才是一段数学、一段化学。
 */
function groupBySubject() {
  if (!cartData.length) {
    toast('还没有题目，先去题库挑题', 'err');
    return;
  }
  const before = state.cart.join('|');
  const seq = new Map(cartData.map((q, i) => [q.uid, i]));
  const sorted = [...cartData].sort((a, b) => {
    const d = subjectRank(a.subject) - subjectRank(b.subject);
    return d !== 0 ? d : seq.get(a.uid) - seq.get(b.uid);
  });
  state.cart = sorted.map((q) => q.uid);
  saveCart();
  refreshCart();
  const counts = new Map();
  for (const q of sorted) counts.set(subjectOf(q), (counts.get(subjectOf(q)) || 0) + 1);
  const summary = [...counts].map(([k, v]) => `${k} ${v} 题`).join('、');
  if (state.cart.join('|') === before) {
    toast(`已经是按学科排好的：${summary}`, 'ok', 4200);
  } else {
    toast(`已按学科归类：${summary}`, 'ok', 4200);
  }
}

/** 新建试卷：清空题目 + 表单回到默认，导出的是一份新记录 */
function newPaper() {
  const hasWork =
    state.cart.length > 0 ||
    el.subtitle.value.trim() ||
    el.source.value.trim() ||
    (el.title.value.trim() && el.title.value.trim() !== defaultTitle());
  if (hasWork && !window.confirm('新建试卷会清空当前已选的题目和表单，确定吗？（已存进试卷库的卷子不受影响）')) {
    return;
  }
  state.cart = [];
  saveCart();
  state.paper.uid = null;
  state.paper.signature = '';
  el.title.value = defaultTitle();
  el.subtitle.value = '';
  el.source.value = '';
  el.name.value = state.settings?.export?.student_name || '';
  el.date.value = todayISO();
  el.subject.value = state.settings?.general?.subject || '数学';
  if ($('#optAnswerSpace')) $('#optAnswerSpace').value = '0';
  refreshCart();
  toast('已新建空白试卷', 'ok');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

/** 批量清空已选题目（题目本身不删，只移出这张卷） */
function clearAll() {
  if (!state.cart.length) {
    toast('已选题目本来就是空的', '');
    return;
  }
  const n = state.cart.length;
  if (!window.confirm(`把已选的 ${n} 道题一次性移出试卷？题目本身不会被删除。`)) return;
  state.cart = [];
  saveCart();
  refreshCart();
  toast(`已清空 ${n} 道题`, 'ok');
}

function currentLayout() {
  return el.layout?.querySelector('button.active')?.dataset.value || 'standard';
}

function applyLayout(layout) {
  const cut = layout === 'cutout';
  el.form?.classList.toggle('cutout', cut);
  if (el.layoutHint) {
    el.layoutHint.textContent = LAYOUT_HINTS[cut ? 'cutout' : 'standard'];
    el.layoutHint.classList.toggle('warn', cut);
  }
}

function options() {
  const position = $('#answerPosition').querySelector('button.active')?.dataset.value || 'inline';
  return {
    subject: (el.subject?.value || '').trim(),
    layout: currentLayout(),
    show_answer: $('#optShowAnswer').checked,
    show_error_reason: $('#optShowError').checked,
    show_analysis: $('#optShowAnalysis').checked,
    answer_position: position,
    include_source_image: $('#optShowImage').checked,
    show_knowledge_points: $('#optShowKp').checked,
    answer_space: Math.max(0, Math.min(6, Number($('#optAnswerSpace').value || 0))),
  };
}

async function refreshCart() {
  if (!el.items) return;
  el.count.textContent = String(state.cart.length);
  if (!state.cart.length) {
    el.items.textContent = '';
    const note = document.createElement('div');
    note.className = 'empty-note';
    note.textContent = '去题库点「加入试卷」挑题';
    el.items.appendChild(note);
    cartData = [];
    return;
  }
  try {
    const data = await get('/api/questions?limit=300');
    const map = new Map(data.items.map((q) => [q.uid, q]));
    cartData = state.cart.map((uid) => map.get(uid)).filter(Boolean);
    if (cartData.length !== state.cart.length) {
      state.cart = cartData.map((q) => q.uid);
      saveCart();
    }
  } catch (err) {
    toast(err.message || '读取题目失败', 'err');
    return;
  }
  renderCart();
}

function renderCart() {
  el.items.textContent = '';
  if (!cartData.length) {
    const note = document.createElement('div');
    note.className = 'empty-note';
    note.textContent = '去题库点「加入试卷」挑题';
    el.items.appendChild(note);
    return;
  }
  const counts = new Map();
  for (const q of cartData) counts.set(subjectOf(q), (counts.get(subjectOf(q)) || 0) + 1);
  const mixed = counts.size > 1; // 混科才画分组标题，单科不啰嗦
  let lastSubject = null;
  cartData.forEach((q, index) => {
    const subject = subjectOf(q);
    if (mixed && subject !== lastSubject) {
      const head = document.createElement('div');
      head.className = 'paper-group';
      head.textContent = `${subject} · ${counts.get(subject)} 题`;
      el.items.appendChild(head);
      lastSubject = subject;
    }
    const row = document.createElement('div');
    row.className = 'paper-row';
    const idx = document.createElement('div');
    idx.className = 'idx';
    idx.textContent = String(index + 1);
    const txt = document.createElement('div');
    txt.className = 'txt';
    const inner = document.createElement('span');
    renderRich(inner, q.stem || '');
    txt.appendChild(inner);
    const ops = document.createElement('div');
    ops.className = 'ops';
    ops.append(
      iconBtn('↑', () => move(index, -1), index === 0),
      iconBtn('↓', () => move(index, 1), index === cartData.length - 1),
      iconBtn('✕', () => removeAt(index)),
    );
    row.append(idx, txt, ops);
    el.items.appendChild(row);
  });
}

function iconBtn(label, fn, disabled = false) {
  const b = document.createElement('button');
  b.textContent = label;
  b.disabled = disabled;
  if (disabled) b.style.opacity = '.35';
  b.addEventListener('click', fn);
  return b;
}

function move(index, delta) {
  const next = index + delta;
  if (next < 0 || next >= state.cart.length) return;
  const arr = [...state.cart];
  [arr[index], arr[next]] = [arr[next], arr[index]];
  state.cart = arr;
  saveCart();
  refreshCart();
}

function removeAt(index) {
  state.cart.splice(index, 1);
  saveCart();
  refreshCart();
}

export function defaultTitle() {
  const d = new Date();
  return `${d.getMonth() + 1}月${d.getDate()}日错题`;
}

function paperPayload() {
  return {
    title: el.title.value.trim() || defaultTitle(),
    subtitle: el.subtitle.value.trim(),
    source: el.source.value.trim(),
    subject: el.subject.value.trim(),
    student_name: el.name.value.trim(),
    exam_date: el.date.value || todayISO(),
    options: options(),
    question_uids: state.cart,
  };
}

async function ensurePaper() {
  if (!state.cart.length) throw new Error('试卷里还没有题目');
  const signature = JSON.stringify(paperPayload());
  if (state.paper.uid && state.paper.signature === signature) return state.paper.uid;
  // 已经有试卷记录就原地更新，避免每次点导出都在试卷库里多一份
  const res = state.paper.uid
    ? await put(`/api/papers/${state.paper.uid}`, paperPayload())
    : await post('/api/papers', paperPayload());
  state.paper.uid = res.paper.uid;
  state.paper.signature = signature;
  await loadSaved(res.paper.uid);
  return res.paper.uid;
}

async function doExport(fmt) {
  const btn = fmt === 'pdf' ? $('#btnExportPdf') : $('#btnExportDocx');
  setBusy(btn, true, fmt === 'pdf' ? '生成 PDF…' : '生成 DOCX…');
  try {
    const uid = await ensurePaper();
    const saved = await saveToDownloads(`/api/papers/${uid}/export?fmt=${fmt}`);
    await loadSaved(uid);
    toast(`${fmt.toUpperCase()} 已保存：${saved.path}，试卷已存入试卷库`, 'ok', 6000);
  } catch (err) {
    toast(err.message || '导出失败', 'err');
  } finally {
    setBusy(btn, false);
  }
}

async function previewPaper() {
  const btn = $('#btnPreviewPaper');
  setBusy(btn, true, '生成预览…');
  try {
    const uid = await ensurePaper();
    const res = await fetch(`/api/papers/${uid}/preview`, { credentials: 'same-origin' });
    if (!res.ok) throw new Error('预览生成失败');
    const html = await res.text();
    const blob = new Blob([html], { type: 'text/html' });
    const frame = $('#previewFrame');
    const old = frame.src;
    frame.src = URL.createObjectURL(blob);
    if (old.startsWith('blob:')) setTimeout(() => URL.revokeObjectURL(old), 5000);
    show($('#previewMask'));
  } catch (err) {
    toast(err.message || '预览失败', 'err');
  } finally {
    setBusy(btn, false);
  }
}

async function loadSaved(highlightUid = '') {
  try {
    const data = await get('/api/papers');
    el.saved.textContent = '';
    const count = $('#savedCount');
    if (count) count.textContent = String(data.items.length);
    if (!data.items.length) {
      const note = document.createElement('div');
      note.className = 'empty-note';
      note.textContent = '还没有保存过试卷 · 点「导出 PDF / DOCX」会自动存入';
      el.saved.appendChild(note);
      return;
    }
    for (const paper of data.items) {
      el.saved.appendChild(savedRow(paper, paper.uid === highlightUid));
    }
  } catch (err) {
    toast(err.message || '读取试卷库失败', 'err');
  }
}

function savedRow(paper, highlight = false) {
  const row = document.createElement('div');
  row.className = `saved-row${highlight ? ' fresh' : ''}`;

  const main = document.createElement('div');
  main.className = 'saved-main';
  const name = document.createElement('span');
  name.className = 'saved-title';
  name.textContent = paper.title || '未命名试卷';
  const meta = document.createElement('span');
  meta.className = 'meta';
  const bits = [fmtDate(paper.created_at), `${paper.question_count} 题`];
  if (paper.student_name) bits.push(paper.student_name);
  if (paper.subject && paper.subject !== '数学') bits.push(paper.subject);
  if (paper.source) bits.push(`来源 ${paper.source}`);
  if (paper.options?.layout === 'cutout') bits.push('裁切粘贴');
  if (paper.exam_date && paper.exam_date !== paper.created_at?.slice(0, 10)) bits.push(paper.exam_date);
  meta.textContent = bits.join(' · ');
  main.append(name, meta);

  const ops = document.createElement('span');
  ops.className = 'ops';
  ops.append(
    miniBtn('预览', () => previewSaved(paper)),
    miniBtn('PDF', () => saveToDownloads(`/api/papers/${paper.uid}/export?fmt=pdf`)
      .then((r) => toast(`PDF 已保存：${r.path}`, 'ok', 6000))
      .catch((e) => toast(e.message, 'err'))),
    miniBtn('DOCX', () => saveToDownloads(`/api/papers/${paper.uid}/export?fmt=docx`)
      .then((r) => toast(`DOCX 已保存：${r.path}`, 'ok', 6000))
      .catch((e) => toast(e.message, 'err'))),
    miniBtn('载入', () => loadSavedPaper(paper)),
    miniBtn('删除', async () => {
      if (!window.confirm(`删除试卷「${paper.title}」？题目本身不会被删除。`)) return;
      try {
        await del(`/api/papers/${paper.uid}`);
        toast('已删除', 'ok');
        loadSaved();
      } catch (err) {
        toast(err.message || '删除失败', 'err');
      }
    }),
  );

  row.append(main, ops);
  return row;
}

function miniBtn(label, fn) {
  const b = document.createElement('button');
  b.className = 'mini-btn';
  b.textContent = label;
  b.addEventListener('click', fn);
  return b;
}

async function previewSaved(paper) {
  try {
    const res = await fetch(`/api/papers/${paper.uid}/preview`, { credentials: 'same-origin' });
    if (!res.ok) throw new Error('预览生成失败');
    const html = await res.text();
    const frame = $('#previewFrame');
    const old = frame.src;
    frame.src = URL.createObjectURL(new Blob([html], { type: 'text/html' }));
    if (old.startsWith('blob:')) setTimeout(() => URL.revokeObjectURL(old), 5000);
    show($('#previewMask'));
  } catch (err) {
    toast(err.message || '预览失败', 'err');
  }
}

/** 把一份旧试卷还原到当前编辑区（题单 + 标题 + 姓名 + 选项）。 */
async function loadSavedPaper(paper) {
  try {
    const data = await get(`/api/papers/${paper.uid}`);
    const uids = (data.questions || []).map((q) => q.uid);
    if (!uids.length) {
      toast('这份试卷里的题目已被删除', 'err');
      return;
    }
    state.cart = uids;
    saveCart();
    el.title.value = paper.title || '';
    el.subtitle.value = paper.subtitle || '';
    el.source.value = paper.source || '';
    el.subject.value = paper.subject || state.settings?.general?.subject || '数学';
    el.name.value = paper.student_name || '';
    el.date.value = paper.exam_date || todayISO();
    const opts = paper.options || {};
    for (const [id, key] of [
      ['#optShowAnswer', 'show_answer'],
      ['#optShowError', 'show_error_reason'],
      ['#optShowAnalysis', 'show_analysis'],
      ['#optShowImage', 'include_source_image'],
      ['#optShowKp', 'show_knowledge_points'],
    ]) {
      const node = $(id);
      if (node && key in opts) node.checked = Boolean(opts[key]);
    }
    if (opts.answer_position) {
      $('#answerPosition').querySelectorAll('button').forEach((b) => {
        b.classList.toggle('active', b.dataset.value === opts.answer_position);
      });
    }
    if (typeof opts.answer_space === 'number') $('#optAnswerSpace').value = String(opts.answer_space);
    if (opts.layout) {
      el.layout?.querySelectorAll('button').forEach((b) => {
        b.classList.toggle('active', b.dataset.value === opts.layout);
      });
      applyLayout(opts.layout);
    }
    // 载入后沿用同一份记录，重复导出更新它而不是再建一份
    state.paper.uid = paper.uid;
    state.paper.signature = JSON.stringify(paperPayload());
    refreshCart();
    toast(`已载入「${paper.title}」${uids.length} 道题`, 'ok');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } catch (err) {
    toast(err.message || '载入失败', 'err');
  }
}

export function refreshPapers() {
  return loadSaved();
}

/** 清空试卷库：批量删掉保存过的试卷记录（题目本身不动） */
async function clearSavedPapers() {
  let items = [];
  try {
    items = (await get('/api/papers')).items || [];
  } catch (err) {
    toast(err.message || '读取试卷库失败', 'err');
    return;
  }
  if (!items.length) {
    toast('试卷库本来就是空的', '');
    return;
  }
  if (!window.confirm(`删除试卷库里的全部 ${items.length} 份试卷？题目本身不会被删除。`)) return;
  let done = 0;
  for (const paper of items) {
    try {
      await del(`/api/papers/${paper.uid}`);
      done += 1;
    } catch (err) {
      // 单份失败不影响其余，最后统一报数
    }
  }
  if (state.paper.uid && items.some((p) => p.uid === state.paper.uid)) {
    state.paper.uid = null;
    state.paper.signature = '';
  }
  toast(`已删除 ${done}/${items.length} 份试卷`, done === items.length ? 'ok' : 'err', 5000);
  loadSaved();
}
