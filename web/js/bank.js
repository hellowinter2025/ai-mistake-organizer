// 题库视图
import { get, del, post } from './api.js';
import { renderRich, plainSummary, renderFigure } from './math.js';
import { openEditor } from './editor.js';
import { state, on, emit, saveCart } from './state.js';
import { $, toast, debounce, fmtDate } from './ui.js';

const el = {};
let loading = false;
const DEFAULT_SUBJECTS = ['数学', '物理', '化学', '生物', '语文', '英语', '历史', '地理', '政治', '科学'];

/** 学科下拉：设置里的学科 + 题库里实际出现过的学科，按出现顺序排。 */
function fillSubjects() {
  if (!el.subject) return;
  const list = state.settings?.general?.subjects || DEFAULT_SUBJECTS;
  const names = new Set(list.filter(Boolean));
  for (const q of state.bank.items || []) if (q.subject) names.add(q.subject);
  const current = state.bank.subject || '';
  el.subject.textContent = '';
  const all = document.createElement('option');
  all.value = '';
  all.textContent = '全部学科';
  el.subject.appendChild(all);
  for (const name of names) {
    const opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name;
    el.subject.appendChild(opt);
  }
  el.subject.value = current;
}

// ---------------------------------------------------------------- 多选 / 框选
const selected = new Set();   // 已勾选的题目 uid
let lastClickIndex = -1;      // Shift 连选的锚点

function cardNodes() {
  return [...el.grid.querySelectorAll('.q-card')];
}

/** 选中状态同步到界面：卡片高亮 + 勾选框 + 底部操作条 */
function syncSel() {
  const alive = new Set((state.bank.items || []).map((q) => q.uid));
  for (const uid of [...selected]) if (!alive.has(uid)) selected.delete(uid);
  for (const card of cardNodes()) {
    const on = selected.has(card.dataset.uid);
    card.classList.toggle('selected', on);
    const cb = card.querySelector('.q-check');
    if (cb) cb.checked = on;
  }
  const bar = $('#bankSelBar');
  if (bar) bar.classList.toggle('hidden', selected.size === 0);
  const cnt = $('#bankSelCount');
  if (cnt) cnt.textContent = `已选 ${selected.size} 题`;
}

function handleCheck(uid, shift) {
  const nodes = cardNodes();
  const idx = nodes.findIndex((c) => c.dataset.uid === uid);
  if (shift && lastClickIndex >= 0 && idx >= 0) {
    // Shift 连选：把上一次点到这一次之间的全部选中（框选一片最常用的方式）
    const a = Math.min(idx, lastClickIndex);
    const b = Math.max(idx, lastClickIndex);
    for (let i = a; i <= b; i += 1) selected.add(nodes[i].dataset.uid);
  } else if (selected.has(uid)) {
    selected.delete(uid);
  } else {
    selected.add(uid);
  }
  lastClickIndex = idx;
  syncSel();
}

/** 按提交日期全选（"把今天这批都加上"） */
function selectByDate(day) {
  let n = 0;
  for (const q of state.bank.items || []) {
    if (String(q.created_at || '').startsWith(day) && !selected.has(q.uid)) {
      selected.add(q.uid);
      n += 1;
    }
  }
  syncSel();
  if (n) toast(`已选中 ${day} 的 ${n} 题`, 'ok');
}

function fillDates() {
  const sel = $('#bankDate');
  if (!sel) return;
  const items = state.bank.items || [];
  const days = [...new Set(items.map((q) => String(q.created_at || '').slice(0, 10)).filter(Boolean))]
    .sort()
    .reverse();
  const cur = sel.value;
  sel.textContent = '';
  const head = document.createElement('option');
  head.value = '';
  head.textContent = '按日期全选…';
  sel.appendChild(head);
  for (const day of days) {
    const opt = document.createElement('option');
    opt.value = day;
    const n = items.filter((q) => String(q.created_at || '').startsWith(day)).length;
    opt.textContent = `${day}（${n} 题）`;
    sel.appendChild(opt);
  }
  sel.value = days.includes(cur) ? cur : '';
  const btn = $('#btnSelectDate');
  if (btn) btn.disabled = !sel.value;
}

function addSelectedToCart() {
  let n = 0;
  for (const uid of selected) {
    if (!state.cart.includes(uid)) {
      state.cart.push(uid);
      n += 1;
    }
  }
  if (n) {
    saveCart();
    toast(`已加入 ${n} 题到组卷（共 ${state.cart.length} 题）`, 'ok');
  } else {
    toast('这些题已经在组卷里了', '');
  }
  selected.clear();
  syncSel();
}

/** 在题库空白处按住拖拽 → 橡皮筋框选 */
function setupMarquee() {
  let start = null;
  let box = null;
  let rect = null;
  const clearHits = () => cardNodes().forEach((c) => c.classList.remove('marquee-hit'));

  const pos = (ev) => ({
    x: ev.clientX - rect.left + el.grid.scrollLeft,
    y: ev.clientY - rect.top + el.grid.scrollTop,
  });

  el.grid.addEventListener('mousedown', (ev) => {
    if (ev.button !== 0) return;
    if (ev.target.closest('.q-card, .bank-selbar')) return;   // 卡片上照常点击
    ev.preventDefault();
    rect = el.grid.getBoundingClientRect();
    start = pos(ev);
    box = document.createElement('div');
    box.className = 'marquee';
    el.grid.appendChild(box);
  });

  document.addEventListener('mousemove', (ev) => {
    if (!box) return;
    const p = pos(ev);
    const left = Math.min(start.x, p.x);
    const top = Math.min(start.y, p.y);
    const w = Math.abs(p.x - start.x);
    const h = Math.abs(p.y - start.y);
    box.style.cssText = `left:${left}px;top:${top}px;width:${w}px;height:${h}px;`;
    for (const card of cardNodes()) {
      const hit = !(
        card.offsetLeft + card.offsetWidth < left ||
        card.offsetLeft > left + w ||
        card.offsetTop + card.offsetHeight < top ||
        card.offsetTop > top + h
      );
      card.classList.toggle('marquee-hit', hit);
    }
  });

  document.addEventListener('mouseup', () => {
    if (!box) return;
    const hits = cardNodes().filter((c) => c.classList.contains('marquee-hit'));
    for (const card of hits) selected.add(card.dataset.uid);
    box.remove();
    box = null;
    start = null;
    clearHits();
    syncSel();
  });
}

export function initBank() {
  el.grid = $('#bankGrid');
  el.empty = $('#bankEmpty');
  el.total = $('#bankTotal');
  el.search = $('#bankSearch');
  el.starred = $('#bankStarred');
  el.subject = $('#bankSubject');
  $('#btnBankRefresh').addEventListener('click', () => loadBank(true));
  el.search.addEventListener('input', debounce(() => { state.bank.keyword = el.search.value.trim(); loadBank(); }, 320));
  el.starred.addEventListener('change', () => { state.bank.starred = el.starred.checked; loadBank(); });
  el.subject?.addEventListener('change', () => { state.bank.subject = el.subject.value; loadBank(); });

  $('#btnSelCart')?.addEventListener('click', addSelectedToCart);
  $('#btnSelClear')?.addEventListener('click', () => { selected.clear(); syncSel(); });
  $('#btnSelAll')?.addEventListener('click', () => {
    for (const q of state.bank.items || []) selected.add(q.uid);
    syncSel();
    toast(`已选中当前 ${selected.size} 题`, 'ok');
  });
  $('#bankDate')?.addEventListener('change', (e) => {
    const btn = $('#btnSelectDate');
    if (btn) btn.disabled = !e.target.value;
  });
  $('#btnSelectDate')?.addEventListener('click', () => {
    const day = $('#bankDate')?.value;
    if (day) selectByDate(day);
  });
  setupMarquee();

  fillSubjects();
  on('settings:changed', fillSubjects);
  on('bank:changed', () => loadBank());
  on('question:created', () => loadBank());
  on('question:updated', () => loadBank());
  on('question:deleted', () => loadBank());
  loadBank();
}

export async function loadBank(force = false) {
  if (loading) return;
  loading = true;
  try {
    const params = new URLSearchParams();
    if (state.bank.keyword) params.set('keyword', state.bank.keyword);
    if (state.bank.starred) params.set('starred', 'true');
    if (state.bank.subject) params.set('subject', state.bank.subject);
    params.set('limit', '120');
    const data = await get(`/api/questions?${params.toString()}`);
    state.bank.items = data.items;
    state.bank.total = data.total;
    fillSubjects();
    renderGrid();
    $('#bankCount').textContent = String(data.total);
    emit('stats:changed');
  } catch (err) {
    toast(err.message || '题库加载失败', 'err');
  } finally {
    loading = false;
  }
}

function renderGrid() {
  el.grid.textContent = '';
  const items = state.bank.items;
  el.total.textContent = `共 ${state.bank.total} 题`;
  el.empty.classList.toggle('hidden', items.length > 0);
  for (const q of items) {
    const card = document.createElement('article');
    card.className = 'q-card';
    card.dataset.uid = q.uid;
    if (selected.has(q.uid)) card.classList.add('selected');

    // 勾选框：单选 / Shift 连选；也可以在空白处拖拽框选一片
    const check = document.createElement('input');
    check.type = 'checkbox';
    check.className = 'q-check';
    check.title = '选中（Shift + 点击可连选一片）';
    check.checked = selected.has(q.uid);
    check.addEventListener('click', (ev) => {
      ev.stopPropagation();
      handleCheck(q.uid, ev.shiftKey);
    });
    card.appendChild(check);

    const head = document.createElement('div');
    head.className = 'q-card-head';
    if (q.subject && q.subject !== '数学') {
      const sub = document.createElement('span');
      sub.className = 'tag subj';
      sub.textContent = q.subject;
      head.appendChild(sub);
    }
    if (q.question_type) {
      const t = document.createElement('span');
      t.className = 'tag';
      t.textContent = q.question_type;
      head.appendChild(t);
    }
    if (q.difficulty) {
      const d = document.createElement('span');
      d.className = 'tag';
      d.textContent = `难度 ${q.difficulty}`;
      head.appendChild(d);
    }
    if (q.starred) {
      const s = document.createElement('span');
      s.className = 'star';
      s.textContent = '★';
      head.appendChild(s);
    }
    const date = document.createElement('span');
    date.className = 'date';
    date.style.marginLeft = 'auto';
    date.textContent = fmtDate(q.created_at);
    head.appendChild(date);
    // 题目编号：小字浅灰，报错/提问时报这个码就能定位到具体哪道
    if (q.code) {
      const code = document.createElement('span');
      code.className = 'q-code-chip';
      code.textContent = `#${q.code}`;
      code.title = '题目编号（反馈问题时提供这个）';
      head.appendChild(code);
    }

    const stem = document.createElement('div');
    stem.className = 'q-stem';
    card.append(head);
    if (q.figure_png_url) {
      const fig = document.createElement('div');
      fig.className = 'q-figure';
      if (renderFigure(fig, q.figure_png_url)) card.appendChild(fig);
    } else if (q.crop_url) {
      const img = document.createElement('img');
      img.className = 'q-crop';
      img.src = q.crop_url;
      img.alt = '题目截图';
      img.loading = 'lazy';
      card.appendChild(img);
    }
    renderRich(stem, q.stem || '');
    card.appendChild(stem);

    if (q.knowledge_points?.length) {
      const kp = document.createElement('div');
      kp.className = 'q-card-head';
      for (const point of q.knowledge_points.slice(0, 4)) {
        const tag = document.createElement('span');
        tag.className = 'tag';
        tag.textContent = point;
        kp.appendChild(tag);
      }
      card.appendChild(kp);
    }

    const foot = document.createElement('div');
    foot.className = 'q-foot';
    const btnEdit = button('编辑', () => editQuestion(q));
    const btnCart = button('加入试卷', () => addToCart(q), 'primary');
    const btnStar = button(q.starred ? '取消收藏' : '收藏', () => toggleStar(q));
    const btnDel = button('删除', () => removeQuestion(q), 'danger');
    foot.append(btnEdit, btnCart, btnStar, btnDel);
    card.appendChild(foot);

    el.grid.appendChild(card);
  }
  fillDates();
  syncSel();
}

function button(label, onClick, kind = 'ghost') {
  const b = document.createElement('button');
  b.className = `btn ${kind}`;
  b.textContent = label;
  b.addEventListener('click', (e) => { e.stopPropagation(); onClick(); });
  return b;
}

function editQuestion(q) {
  openEditor({
    uid: q.uid,
    question: q,
    cropUrl: q.crop_url,
    imageId: q.image_id,
    bbox: Array.isArray(q.bbox) ? q.bbox : (q.bbox ? [q.bbox.x, q.bbox.y, q.bbox.w, q.bbox.h] : null),
    meta: { model: q.ai_model || '' },
    onSaved: () => loadBank(true),
    onDeleted: () => loadBank(true),
  });
}

async function toggleStar(q) {
  try {
    const res = await post(`/api/questions/${q.uid}/star`, {});
    q.starred = res.starred;
    renderGrid();
  } catch (err) {
    toast(err.message || '操作失败', 'err');
  }
}

async function removeQuestion(q) {
  if (!window.confirm('删除这道题？删除后无法恢复。')) return;
  try {
    await del(`/api/questions/${q.uid}`);
    toast('已删除', 'ok');
    loadBank(true);
  } catch (err) {
    toast(err.message || '删除失败', 'err');
  }
}

export function addToCart(q) {
  if (state.cart.includes(q.uid)) {
    toast('已在试卷中');
    return;
  }
  state.cart.push(q.uid);
  saveCart();
  updateCartBadge();
  toast(`已加入试卷（${state.cart.length} 题）`, 'ok');
}

export function updateCartBadge() {
  const badge = $('#cartCount');
  badge.textContent = String(state.cart.length);
  badge.classList.toggle('hidden', state.cart.length === 0);
}
