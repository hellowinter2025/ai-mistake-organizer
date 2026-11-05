// 题库视图
//
// 三种查看样式（view.layout）：
//   card —— 卡片网格，题干折叠到 132px（原本的样子）
//   list —— 单列紧凑行，一眼扫得完
//   read —— 全展开，题干不截断，直接把答案与解析摊开
// 四种分组（view.group）：不分组 / 按日期 / 按学科 / 按题型。
// 两者都存 localStorage，下次打开还是上次那个样子。
import { get, del, post } from './api.js';
import { renderRich, renderFigure } from './math.js';
import { openEditor } from './editor.js';
import { state, on, emit, saveCart } from './state.js';
import { $, toast, debounce, fmtDate } from './ui.js';

const el = {};
let loading = false;
let currentGroups = [];       // 当前分组结果，组头「全选本组」要用
let suppressClick = 0;        // 框选松手后短时间内不把 click 当「打开编辑」
const DEFAULT_SUBJECTS = ['数学', '物理', '化学', '生物', '语文', '英语', '历史', '地理', '政治', '科学'];

// 学科色只用来点一个小圆点和卡片左缘，取的都是低饱和的，不至于把卷面弄花
const SUBJECT_COLOR = {
  数学: '#2f5bd7', 物理: '#7a4fd6', 化学: '#0f8b8d', 生物: '#2c7a52',
  语文: '#b3453f', 英语: '#c07a12', 历史: '#8a6a3b', 地理: '#1f7a9c',
  政治: '#a03a5a', 科学: '#3a7d44',
};
const FALLBACK_COLOR = '#8d877b';

const VIEW_KEY = 'mm_bank_view';
const view = {
  layout: 'card',
  group: '',
  collapsed: new Set(),   // 被点收起的组
};

function loadView() {
  try {
    const saved = JSON.parse(localStorage.getItem(VIEW_KEY) || '{}');
    if (['card', 'list', 'read'].includes(saved.layout)) view.layout = saved.layout;
    if (['', 'date', 'subject', 'type'].includes(saved.group)) view.group = saved.group;
  } catch {
    // 存坏了就当没存过，用默认值
  }
  // ?layout=list&group=subject 可以临时指定一种看法（分享/截图用），
  // 只覆盖这一次，不写回本地，用户点过按钮之后照旧按自己的偏好走
  const p = new URLSearchParams(location.search);
  const layout = p.get('layout');
  if (['card', 'list', 'read'].includes(layout)) view.layout = layout;
  if (p.has('group')) {
    const group = p.get('group');
    if (['', 'date', 'subject', 'type'].includes(group)) view.group = group;
  }
}

function saveView() {
  localStorage.setItem(VIEW_KEY, JSON.stringify({ layout: view.layout, group: view.group }));
}

// ---------------------------------------------------------------- 学科下拉
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
  return [...el.groups.querySelectorAll('.q-card')];
}

/** 选中状态同步到界面：卡片高亮 + 勾选框 + 底部操作条 */
function syncSel() {
  const alive = new Set((state.bank.items || []).map((q) => q.uid));
  for (const uid of [...selected]) if (!alive.has(uid)) selected.delete(uid);
  for (const card of cardNodes()) {
    const isOn = selected.has(card.dataset.uid);
    card.classList.toggle('selected', isOn);
    const cb = card.querySelector('.q-check');
    if (cb) cb.checked = isOn;
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
    // Shift 连选：把上一次点到这一次之间的全部选中
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

/** 选中某个分组里的全部题目（组头按钮，替代原来的「按日期全选」下拉） */
function selectGroup(key) {
  const group = currentGroups.find((g) => g.key === key);
  if (!group) return;
  for (const q of group.items) selected.add(q.uid);
  syncSel();
  toast(`已选中「${group.title}」${group.items.length} 题`, 'ok');
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

/**
 * 橡皮筋框选。
 *
 * 从**任何位置**（包括卡片上）按下左键都能起框：按下去先不算，移动超过 6px
 * 才进入框选，于是「点一下卡片」仍然是打开编辑，两者不打架。
 * 按住 Shift 起框是在原有选择上追加。
 */
function setupMarquee() {
  let start = null;
  let box = null;
  let dragging = false;
  let base = null;

  const clearHits = () => cardNodes().forEach((c) => c.classList.remove('marquee-hit'));

  const onDown = (ev) => {
    if (ev.button !== 0) return;
    if (ev.target.closest('button, input, a, .bank-selbar, .bank-group-head')) return;
    // 点在滚动条上不要起框
    if (ev.target === el.scroll && ev.offsetX >= el.scroll.clientWidth) return;
    start = { x: ev.clientX, y: ev.clientY };
    base = ev.shiftKey ? new Set(selected) : null;
    dragging = false;
  };

  const onMove = (ev) => {
    if (!start) return;
    if (!dragging) {
      if (Math.hypot(ev.clientX - start.x, ev.clientY - start.y) < 6) return;
      dragging = true;
      document.body.classList.add('is-marquee');
      window.getSelection()?.removeAllRanges();
      box = document.createElement('div');
      box.className = 'marquee';
      document.body.appendChild(box);
    }
    const left = Math.min(start.x, ev.clientX);
    const top = Math.min(start.y, ev.clientY);
    const right = Math.max(start.x, ev.clientX);
    const bottom = Math.max(start.y, ev.clientY);
    box.style.cssText = `left:${left}px;top:${top}px;width:${right - left}px;height:${bottom - top}px;`;
    for (const card of cardNodes()) {
      // 用屏幕坐标比对：卡片现在嵌在分组里，offsetLeft 不再相对滚动容器
      const r = card.getBoundingClientRect();
      const hit = !(r.right < left || r.left > right || r.bottom < top || r.top > bottom);
      card.classList.toggle('marquee-hit', hit);
    }
  };

  const onUp = () => {
    if (!start) return;
    if (dragging) {
      const hits = cardNodes().filter((c) => c.classList.contains('marquee-hit'));
      const next = base || new Set();
      for (const card of hits) next.add(card.dataset.uid);
      selected.clear();
      for (const uid of next) selected.add(uid);
      suppressClick = Date.now() + 160;
      box?.remove();
      box = null;
      clearHits();
      document.body.classList.remove('is-marquee');
      syncSel();
    }
    start = null;
    base = null;
    dragging = false;
  };

  el.scroll.addEventListener('mousedown', onDown);
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && selected.size) {
      selected.clear();
      syncSel();
    }
  });
}

// ---------------------------------------------------------------- 分组
const GROUPERS = {
  date: {
    key: (q) => String(q.created_at || '').slice(0, 10) || '日期未知',
    title: (key) => dayTitle(key),
    desc: true,
  },
  subject: {
    key: (q) => q.subject || '未分类',
    title: (key) => key,
    desc: false,
  },
  type: {
    key: (q) => q.question_type || '未分类',
    title: (key) => key,
    desc: false,
  },
};

function dayTitle(day) {
  if (!day || day === '日期未知') return '日期未知';
  const pad = (n) => String(n).padStart(2, '0');
  const fmt = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const now = new Date();
  if (day === fmt(now)) return '今天';
  if (day === fmt(new Date(now.getTime() - 86400000))) return '昨天';
  const d = new Date(`${day}T00:00:00`);
  if (Number.isNaN(d.getTime())) return day;
  const week = '日一二三四五六'[d.getDay()];
  return `${day}　周${week}`;
}

function groupItems(items) {
  if (!view.group) return [{ key: '', title: '', items }];
  const spec = GROUPERS[view.group];
  if (!spec) return [{ key: '', title: '', items }];
  const map = new Map();
  for (const q of items) {
    const key = spec.key(q);
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(q);
  }
  const list = [...map.entries()].map(([key, group]) => ({
    key,
    title: spec.title(key),
    items: group,
  }));
  if (spec.desc) {
    list.sort((a, b) => String(b.key).localeCompare(String(a.key)));
  } else {
    // 学科/题型按题数从多到少，同数量按名字
    list.sort((a, b) => b.items.length - a.items.length || a.title.localeCompare(b.title, 'zh'));
  }
  return list;
}

// ---------------------------------------------------------------- 渲染
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
  const items = state.bank.items || [];
  el.total.textContent = `共 ${state.bank.total} 题`;
  el.empty.classList.toggle('hidden', items.length > 0);
  el.groups.className = `bank-groups layout-${view.layout}`;
  el.groups.textContent = '';

  currentGroups = groupItems(items);
  for (const group of currentGroups) {
    el.groups.appendChild(renderGroup(group));
  }
  syncSel();
}

function renderGroup(group) {
  const section = document.createElement('section');
  section.className = 'bank-group';
  section.dataset.group = group.key;

  const collapsed = view.collapsed.has(group.key);
  if (collapsed) section.classList.add('collapsed');

  if (group.title) {
    const head = document.createElement('header');
    head.className = 'bank-group-head';

    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'gh-toggle';
    toggle.title = collapsed ? '展开这一组' : '收起这一组';
    toggle.setAttribute('aria-expanded', String(!collapsed));
    toggle.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 6.5 8 10.5 12 6.5"/></svg>';

    const dot = document.createElement('span');
    dot.className = 'gh-dot';
    if (view.group === 'subject') dot.style.background = SUBJECT_COLOR[group.key] || FALLBACK_COLOR;

    const title = document.createElement('h3');
    title.textContent = group.title;

    const count = document.createElement('span');
    count.className = 'gh-count';
    count.textContent = `${group.items.length} 题`;

    const spacer = document.createElement('span');
    spacer.className = 'spacer';

    const btnSel = document.createElement('button');
    btnSel.type = 'button';
    btnSel.className = 'gh-sel';
    btnSel.textContent = '全选本组';
    btnSel.addEventListener('click', (ev) => {
      ev.stopPropagation();
      selectGroup(group.key);
    });

    head.append(toggle, dot, title, count, spacer, btnSel);
    head.addEventListener('click', (ev) => {
      if (ev.target.closest('.gh-sel')) return;
      if (view.collapsed.has(group.key)) view.collapsed.delete(group.key);
      else view.collapsed.add(group.key);
      section.classList.toggle('collapsed');
      const nowOpen = !section.classList.contains('collapsed');
      toggle.setAttribute('aria-expanded', String(nowOpen));
      toggle.title = nowOpen ? '收起这一组' : '展开这一组';
    });
    section.appendChild(head);
  }

  const grid = document.createElement('div');
  grid.className = 'bank-grid';
  for (const q of group.items) grid.appendChild(renderCard(q));
  section.appendChild(grid);
  return section;
}

function renderCard(q) {
  const card = document.createElement('article');
  card.className = 'q-card';
  card.dataset.uid = q.uid;
  card.dataset.subject = q.subject || '';
  card.style.setProperty('--subj', SUBJECT_COLOR[q.subject] || FALLBACK_COLOR);
  if (selected.has(q.uid)) card.classList.add('selected');
  card.title = '点击打开编辑';

  // 勾选框：单选 / Shift 连选；也可以在任意位置拖拽框选一片
  const check = document.createElement('input');
  check.type = 'checkbox';
  check.className = 'q-check';
  check.title = '选中（Shift + 点击可连选，或直接拖框）';
  check.checked = selected.has(q.uid);
  check.addEventListener('click', (ev) => {
    ev.stopPropagation();
    handleCheck(q.uid, ev.shiftKey);
  });
  card.appendChild(check);

  const main = document.createElement('div');
  main.className = 'q-main';

  // ---- 头部：学科 / 题型 / 难度 / 编号 / 日期
  const head = document.createElement('div');
  head.className = 'q-card-head';
  if (q.subject) {
    const chip = document.createElement('span');
    chip.className = 'subj-chip';
    const dot = document.createElement('i');
    chip.append(dot, document.createTextNode(q.subject));
    head.appendChild(chip);
  }
  if (q.question_type) head.appendChild(tag(q.question_type));
  if (q.difficulty) head.appendChild(tag(`难度 ${q.difficulty}`));
  if (q.starred) {
    const star = document.createElement('span');
    star.className = 'star';
    star.textContent = '★';
    star.title = '已收藏';
    head.appendChild(star);
  }
  const meta = document.createElement('span');
  meta.className = 'q-meta';
  meta.append(fmtDate(q.created_at));
  // 题目编号：小字浅灰，报错时报这个码就能定位到具体哪道
  if (q.code) {
    const code = document.createElement('span');
    code.className = 'q-code-chip';
    code.textContent = `#${q.code}`;
    code.title = '题目编号（反馈问题时提供这个）';
    meta.appendChild(code);
  }
  head.appendChild(meta);
  main.appendChild(head);

  // ---- 图形
  if (q.figure_png_url) {
    const fig = document.createElement('div');
    fig.className = 'q-figure';
    if (renderFigure(fig, q.figure_png_url)) main.appendChild(fig);
  } else if (q.crop_url && view.layout === 'read') {
    // 原卷截图只放全展开模式：那里是逐题细看，留一条当佐证；
    // 卡片/列表里题干已经排好字了，再放一张带红笔的照片只是重复。
    const img = document.createElement('img');
    img.className = 'q-crop';
    img.src = q.crop_url;
    img.alt = '题目截图';
    img.loading = 'lazy';
    main.appendChild(img);
  }

  // ---- 题干
  const stem = document.createElement('div');
  stem.className = 'q-stem';
  renderRich(stem, q.stem || '');
  main.appendChild(stem);

  // ---- 知识点（列表视图太挤，不显示）
  if (q.knowledge_points?.length && view.layout !== 'list') {
    const kp = document.createElement('div');
    kp.className = 'q-kps';
    for (const point of q.knowledge_points.slice(0, 3)) kp.appendChild(tag(point));
    main.appendChild(kp);
  }

  card.appendChild(main);
  if (view.layout === 'read') card.appendChild(renderDetail(q));

  // ---- 底部操作
  const foot = document.createElement('div');
  foot.className = 'q-foot';
  foot.append(
    button('编辑', () => editQuestion(q)),
    button('加入试卷', () => addToCart(q), 'primary'),
    button(q.starred ? '取消收藏' : '收藏', () => toggleStar(q)),
    button('删除', () => removeQuestion(q), 'danger'),
  );
  card.appendChild(foot);

  // 点卡片任意空白处 = 打开编辑；按钮、勾选框、拖框后都不触发
  card.addEventListener('click', (ev) => {
    if (Date.now() < suppressClick) return;
    if (ev.target.closest('button, input, a')) return;
    if (window.getSelection()?.toString()) return;   // 正在选文字复制，别弹编辑
    editQuestion(q);
  });

  return card;
}

function tag(text) {
  const span = document.createElement('span');
  span.className = 'tag';
  span.textContent = text;
  return span;
}

/** 全展开模式才追加：答案 + 解析 + 另解 */
function renderDetail(q) {
  const wrap = document.createElement('div');
  wrap.className = 'q-detail';

  wrap.appendChild(detailBlock('答案', q.answer, 'answer'));
  if (q.solution_fix) wrap.appendChild(detailBlock('解析', q.solution_fix, 'solution'));
  if (q.solution_alt) wrap.appendChild(detailBlock('另解', q.solution_alt, 'solution'));
  if (q.error_reason) wrap.appendChild(detailBlock('错因', q.error_reason, 'note'));
  if (q.notes) wrap.appendChild(detailBlock('备注', q.notes, 'note'));
  return wrap;
}

function detailBlock(label, text, kind) {
  const block = document.createElement('div');
  block.className = `q-detail-block ${kind}`;
  const head = document.createElement('span');
  head.className = 'q-label';
  head.textContent = label;
  const body = document.createElement('div');
  body.className = 'q-detail-body';
  renderRich(body, text || '—');
  block.append(head, body);
  return block;
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

// ---------------------------------------------------------------- 视图切换
function syncViewControls() {
  for (const b of el.layout.querySelectorAll('[data-layout]')) {
    b.classList.toggle('on', b.dataset.layout === view.layout);
    b.setAttribute('aria-pressed', String(b.dataset.layout === view.layout));
  }
  el.group.value = view.group;
}

export function initBank() {
  el.scroll = $('#bankScroll');
  el.groups = $('#bankGroups');
  el.empty = $('#bankEmpty');
  el.total = $('#bankTotal');
  el.search = $('#bankSearch');
  el.starred = $('#bankStarred');
  el.subject = $('#bankSubject');
  el.group = $('#bankGroup');
  el.layout = $('#bankLayout');

  loadView();
  el.groups.className = `bank-groups layout-${view.layout}`;
  syncViewControls();

  $('#btnBankRefresh').addEventListener('click', () => loadBank(true));
  el.search.addEventListener('input', debounce(() => { state.bank.keyword = el.search.value.trim(); loadBank(); }, 320));
  el.starred.addEventListener('change', () => { state.bank.starred = el.starred.checked; loadBank(); });
  el.subject?.addEventListener('change', () => { state.bank.subject = el.subject.value; loadBank(); });

  el.group.addEventListener('change', () => {
    view.group = el.group.value;
    view.collapsed.clear();
    saveView();
    syncViewControls();
    renderGrid();
  });

  el.layout.addEventListener('click', (ev) => {
    const btn = ev.target.closest('[data-layout]');
    if (!btn) return;
    view.layout = btn.dataset.layout;
    saveView();
    syncViewControls();
    renderGrid();
  });

  $('#btnSelCart')?.addEventListener('click', addSelectedToCart);
  $('#btnSelClear')?.addEventListener('click', () => { selected.clear(); syncSel(); });
  $('#btnSelAll')?.addEventListener('click', () => {
    for (const q of state.bank.items || []) selected.add(q.uid);
    syncSel();
    toast(`已选中当前 ${selected.size} 题`, 'ok');
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
