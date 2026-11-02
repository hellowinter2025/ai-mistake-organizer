// 识别整理：上传、切题、框选、AI 解析队列
import { uploadImages, get, post, patch, put } from './api.js';
import { BoxSelector } from './selector.js';
import { state, on, emit, currentBoxes, setBoxes, saveCart } from './state.js';
import { openCropper } from './figure.js';
import { openEditor } from './editor.js';
import { fixLiteralEscapes } from './math.js';
import { $, toast, show, hide, debounce } from './ui.js';

let selector = null;
let activeBoxId = null;
let solving = false;
let batchBusy = false;
let queueSeq = 0;

const el = {};

const FALLBACK_SUBJECTS = ['数学', '物理', '化学', '生物', '语文', '英语', '历史', '地理', '政治', '科学'];

/** 解析学科：默认跟随设置，切换后写回设置，下次打开还是这一科。 */
function initSubjectPicker() {
  const sel = $('#solveSubject');
  if (!sel) return;
  const current = state.settings?.general?.subject || '数学';
  const names = new Set(state.settings?.general?.subjects || FALLBACK_SUBJECTS);
  names.add(current);
  sel.textContent = '';
  for (const name of names) {
    const opt = document.createElement('option');
    opt.value = name;
    opt.textContent = name;
    sel.appendChild(opt);
  }
  sel.value = current;
  sel.addEventListener('change', async () => {
    const value = sel.value;
    state.settings = { ...(state.settings || {}), general: { ...(state.settings?.general || {}), subject: value } };
    emit('settings:changed');
    try {
      const res = await patch('/api/settings', { general: { subject: value } });
      state.settings = res.settings;
    } catch (err) {
      toast(err.message || '学科未能保存到设置', 'err');
    }
  });
}

export function initRecognize() {
  el.dropzone = $('#dropzone');
  el.dzIdle = $('#dzIdle');
  el.dzBusy = $('#dzBusy');
  el.dzBusyText = $('#dzBusyText');
  el.dzBusyPct = $('#dzBusyPct');
  el.fileInput = $('#fileInput');
  el.filmstrip = $('#filmstrip');
  el.stage = $('#stage');
  el.emptyStage = $('#emptyStage');
  el.canvasScroll = $('#canvasScroll');
  el.canvasInner = $('#canvasInner');
  el.img = $('#pageImage');
  el.canvas = $('#overlay');
  el.stageInfo = $('#stageInfo');
  el.queueList = $('#queueList');
  el.queueCount = $('#queueCount');
  el.solveHint = $('#solveHint');
  el.zoom = $('#zoomRange');
  el.zoomLabel = $('#zoomLabel');

  el.dropzone.addEventListener('click', () => el.fileInput.click());
  el.dropzone.addEventListener('dragover', (e) => { e.preventDefault(); el.dropzone.classList.add('drag'); });
  el.dropzone.addEventListener('dragleave', () => el.dropzone.classList.remove('drag'));
  el.dropzone.addEventListener('drop', (e) => {
    e.preventDefault();
    el.dropzone.classList.remove('drag');
    handleFiles(e.dataTransfer?.files);
  });
  el.fileInput.addEventListener('change', () => {
    handleFiles(el.fileInput.files);
    el.fileInput.value = '';
  });
  $('#btnPickFile').addEventListener('click', () => el.fileInput.click());
  document.addEventListener('paste', (e) => {
    const items = Array.from(e.clipboardData?.items || []);
    const files = items.filter((i) => i.type.startsWith('image/')).map((i) => i.getAsFile()).filter(Boolean);
    if (files.length) { handleFiles(files); toast('已从剪贴板粘贴图片'); }
  });

  $('#btnClearImages').addEventListener('click', () => {
    if (!state.images.length) return;
    if (!window.confirm('清空所有图片与已框选内容？（已入库题目不受影响）')) return;
    state.images = [];
    state.boxesByImage = {};
    state.currentImageId = null;
    state.queue = [];
    queueTouched = true;
    persistQueue(true);
    el.img.removeAttribute('src');
    hide(el.canvasScroll);
    hide(el.img);
    show(el.emptyStage);
    renderFilmstrip();
    renderQueue();
    syncToolbar();
  });

  $('#btnSelectAll').addEventListener('click', () => {
    currentBoxes().forEach((b) => { b.selected = true; });
    setBoxes(state.currentImageId, currentBoxes());
    syncToolbar();
  });
  $('#btnSelectNone').addEventListener('click', () => {
    currentBoxes().forEach((b) => { b.selected = false; });
    setBoxes(state.currentImageId, currentBoxes());
    syncToolbar();
  });
  $('#btnDeleteBox').addEventListener('click', deleteActiveBox);
  el.queueProgress = $('#queueProgress');
  el.queueProgressBar = $('#queueProgressBar');
  el.queueProgressText = $('#queueProgressText');
  el.btnContinue = $('#btnContinueQueue');
  el.btnRetry = $('#btnRetryFailed');
  el.btnClearSaved = $('#btnClearSaved');
  el.btnClearQueue = $('#btnClearQueue');
  el.queueBatch = $('#queueBatch');
  el.btnSaveAll = $('#btnSaveAll');
  el.btnCartAll = $('#btnCartAll');

  initSubjectPicker();
  $('#btnSolve').addEventListener('click', solveSelected);
  el.btnContinue.addEventListener('click', continueQueue);
  el.btnRetry.addEventListener('click', retryFailed);
  el.btnClearSaved.addEventListener('click', clearSaved);
  el.btnClearQueue.addEventListener('click', clearQueue);
  el.btnSaveAll.addEventListener('click', saveAll);
  el.btnCartAll.addEventListener('click', cartAll);
  window.addEventListener('beforeunload', () => persistQueue(true));

  el.zoom.addEventListener('input', () => {
    el.zoomLabel.textContent = `${el.zoom.value}%`;
    applyZoom();
  });

  document.addEventListener('keydown', (e) => {
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName)) return;
    if (e.key === 'Delete' || e.key === 'Backspace') {
      if (activeBoxId) { e.preventDefault(); deleteActiveBox(); }
    }
    if (e.key === 'a' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      currentBoxes().forEach((b) => { b.selected = true; });
      setBoxes(state.currentImageId, currentBoxes());
    }
  });

  el.img.addEventListener('load', () => {
    if (!selector) {
      selector = new BoxSelector({
        image: el.img,
        canvas: el.canvas,
        onChange: () => {
          setBoxes(state.currentImageId, currentBoxes());
          renderFilmstrip();
          syncToolbar();
        },
        onActiveChange: (id) => { activeBoxId = id; syncToolbar(); },
      });
    }
    show(el.canvasScroll);
    hide(el.emptyStage);
    requestAnimationFrame(() => { applyZoom(); syncToolbar(); });
  });

  window.addEventListener('resize', debounce(() => { if (state.currentImageId) applyZoom(); }, 160));

  on('boxes:changed', () => { if (selector) selector.draw(); syncToolbar(); });
  restoreQueue();
  renderQueue();
  syncToolbar();
}

const MAX_BATCH_FILES = 10;                       // 与服务端 MAX_IMAGES_PER_UPLOAD 对齐
const MAX_BATCH_BYTES = 24 * 1024 * 1024;         // 单次请求体上限，留足 multipart 开销
const IMAGE_EXT = /\.(jpe?g|png|webp|bmp|gif|heic|heif|avif)$/i;

function isImageFile(f) {
  return Boolean(f) && ((f.type && f.type.startsWith('image/')) || IMAGE_EXT.test(f.name || ''));
}

/** 按「张数 + 总字节」双重切批，避免一次拖 30 张时被服务端整批拒绝 */
function makeBatches(files) {
  const batches = [];
  let cur = [];
  let bytes = 0;
  for (const f of files) {
    const size = f.size || 0;
    if (cur.length && (cur.length >= MAX_BATCH_FILES || bytes + size > MAX_BATCH_BYTES)) {
      batches.push(cur);
      cur = [];
      bytes = 0;
    }
    cur.push(f);
    bytes += size;
  }
  if (cur.length) batches.push(cur);
  return batches;
}

/** 只切换 dropzone 内部的两块内容，绝不动 innerHTML ——
 *  否则里面的 <input type=file> 会被销毁重建，监听器全丢，第二次上传就没反应。
 *  用引用计数，上传中再拖一批图不会把忙碌态提前清掉。 */
let uploadDepth = 0;

function setUploadBusy(on, text = '上传中…', pct = 0) {
  uploadDepth = Math.max(0, uploadDepth + (on ? 1 : -1));
  const active = uploadDepth > 0;
  if (active) {
    el.dzBusyText.textContent = text;
    el.dzBusyPct.textContent = `${pct}%`;
    show(el.dzBusy);
    hide(el.dzIdle);
  } else {
    hide(el.dzBusy);
    show(el.dzIdle);
  }
  el.dropzone.classList.toggle('uploading', active);
}

async function handleFiles(fileList) {
  const files = Array.from(fileList || []).filter(isImageFile);
  if (!files.length) {
    toast('没有可用的图片：支持 JPG / PNG / WEBP / HEIC，也可直接 Ctrl+V 粘贴截图', 'err', 5000);
    return;
  }

  const batches = makeBatches(files);
  const added = [];
  const errors = [];
  let reused = 0;

  setUploadBusy(true);
  try {
    for (let i = 0; i < batches.length; i += 1) {
      const stage = batches.length > 1 ? ` ${i + 1}/${batches.length}` : '';
      el.dzBusyText.textContent = `上传中${stage}…`;
      el.dzBusyPct.textContent = '0%';
      try {
        const data = await uploadImages(batches[i], (pct) => {
          el.dzBusyPct.textContent = `${pct}%`;
        });
        for (const image of data.images || []) {
          // 只按「当前片带里是否已有」判重；服务端返回 reused 只说明库里存过，
          // 清空片带后再拖同一张照片应该能重新载入。
          if (state.images.some((x) => x.id === image.id)) { reused += 1; continue; }
          state.images.push(image);
          // 不做自动切题：候选框总要人手改，不如让用户直接框
          state.boxesByImage[image.id] = [];
          added.push(image);
        }
        for (const e of data.errors || []) errors.push(`${e.name}：${e.error}`);
      } catch (err) {
        errors.push(err.message || '上传失败');
      }
    }
  } finally {
    setUploadBusy(false);
  }

  if (added.length) {
    renderFilmstrip();
    selectImage(added[0].id);
    toast(`已载入 ${added.length} 张图片${reused ? `，${reused} 张已在列表中` : ''}，在图上拖拽框出题目`, 'ok');
  } else if (reused) {
    toast(`这 ${reused} 张图片已经在列表里了`, '');
  }

  if (errors.length) {
    const head = errors.slice(0, 2).join('；');
    toast(`有 ${errors.length} 张未导入：${head}${errors.length > 2 ? ' …' : ''}`, 'err', 7000);
  }
}


export function selectImage(id) {
  const image = state.images.find((i) => i.id === id);
  if (!image) return;
  state.currentImageId = id;
  activeBoxId = null;
  el.zoom.value = '100';
  el.zoomLabel.textContent = '100%';
  hide(el.emptyStage);
  el.img.src = image.url;
  if (el.img.complete) {
    show(el.canvasScroll);
    requestAnimationFrame(() => { applyZoom(); });
  }
  renderFilmstrip();
  syncToolbar();
}

function applyZoom() {
  const image = state.images.find((i) => i.id === state.currentImageId);
  if (!image || !image.width) return;
  const zoom = Number(el.zoom.value) / 100;
  // 预览区可用尺寸（.canvas-scroll 四周各有 18px 内边距）
  const availW = Math.max(160, (el.canvasScroll.clientWidth || 0) - 36);
  const availH = Math.max(160, (el.canvasScroll.clientHeight || 0) - 36);
  // 100% = 整张图刚好放进来：按宽、按高各算一个比例，取更小的那个
  // （竖拍的试卷照片按宽度适配会高得离谱，得改成按高度适配）
  const height = image.height || image.width;
  const fit = Math.min(availW / image.width, availH / height);
  const width = Math.max(48, Math.round(image.width * fit * zoom));
  el.img.style.width = `${width}px`;
  el.img.style.height = 'auto';
  gridBackground(width);
  requestAnimationFrame(() => {
    if (selector) {
      selector.setBoxes(currentBoxes());
      selector.resize();
    }
  });
}

function gridBackground(width) {
  el.canvasInner.style.width = `${width}px`;
}

function deleteActiveBox() {
  if (!activeBoxId) return;
  const boxes = currentBoxes().filter((b) => b.id !== activeBoxId);
  activeBoxId = null;
  setBoxes(state.currentImageId, boxes);
  if (selector) selector.setBoxes(boxes);
  renderFilmstrip();
  syncToolbar();
}

function renderFilmstrip() {
  el.filmstrip.textContent = '';
  for (const image of state.images) {
    const box = document.createElement('div');
    box.className = `film-item${image.id === state.currentImageId ? ' active' : ''}`;
    const img = document.createElement('img');
    img.src = image.thumb || image.url;
    img.alt = image.name || '图片';
    const name = document.createElement('div');
    name.className = 'film-name';
    name.textContent = image.name || '试卷照片';
    const selected = (state.boxesByImage[image.id] || []).filter((b) => b.selected).length;
    const badge = document.createElement('div');
    badge.className = 'film-badge';
    badge.textContent = selected ? `${selected} 题` : '未选';
    box.append(img, name, badge);
    box.addEventListener('click', () => selectImage(image.id));
    el.filmstrip.appendChild(box);
  }
}

function syncToolbar() {
  const has = Boolean(state.currentImageId);
  const boxes = currentBoxes();
  const selected = boxes.filter((b) => b.selected);
  for (const id of ['btnSelectAll', 'btnSelectNone']) {
    const b = document.getElementById(id);
    if (b) b.disabled = !has || !boxes.length;
  }
  const delBtn = document.getElementById('btnDeleteBox');
  if (delBtn) delBtn.disabled = !activeBoxId;
  const solveBtn = document.getElementById('btnSolve');
  if (solveBtn) {
    // 解析中也能继续加：worker 会自己捡起新任务，不用一直等着
    solveBtn.disabled = !selected.length;
    const waiting = selected.filter((b) => !queueItemFor(state.currentImageId, b.id)).length;
    const queued = selected.length - waiting;
    if (solving) solveBtn.textContent = waiting ? `解析中…仍需加入 ${waiting} 题` : '解析中…可继续添加';
    else solveBtn.textContent = waiting ? `AI 解析所选 ${waiting} 题` : 'AI 解析所选题目';
    const parts = [];
    if (waiting) parts.push(`已选 ${selected.length} 题`);
    else if (selected.length) parts.push(`所选 ${selected.length} 题都已在队列中`);
    else parts.push('先在左侧勾选题目');
    if (waiting && queued) parts.push(`另有 ${queued} 题已在队列，不会重复解析`);
    el.solveHint.textContent = parts.join('，');
  } else {
    el.solveHint.textContent = selected.length
      ? `已选 ${selected.length} 题，将逐题调用 AI`
      : '先在左侧勾选题目';
  }
  const image = state.images.find((i) => i.id === state.currentImageId);
  if (image) {
    el.stageInfo.textContent = `${image.width}×${image.height} · ${boxes.length} 块 · 选 ${selected.length}`;
  } else {
    el.stageInfo.textContent = '未载入图片';
  }
  updateQueueHead();
}

// ---------------------------------------------------------------------------
// 题目队列
//
// 状态机：wait → run → done → saving → saved，失败进 error 可重试。
// 之前的问题：保存回调把状态写成 saved，但渲染用的映射表里没有 saved，
// 于是 `map[status] || map.wait` 退化成「待解析」，saved 项点击也毫无反应
// —— 用户看到的就是「明明保存了却还显示待解析、点了没反应」。
//
// 现在每个状态都有明确文案、颜色和可执行动作；队列落服务端 SQLite
// （PUT /api/queue），刷新页面、换机器都不会丢掉花钱买回来的解析结果。
// ---------------------------------------------------------------------------
const STATUS = {
  wait: { cls: 'wait', text: '待解析' },
  run: { cls: 'run', text: '解析中' },
  done: { cls: 'done', text: '待入库' },
  saving: { cls: 'run', text: '保存中' },
  saved: { cls: 'saved', text: '已入库' },
  error: { cls: 'error', text: '失败' },
};

// 未完成的排前面、已入库沉底：一眼看到还剩什么要做
const QUEUE_ORDER = { run: 0, saving: 0, error: 1, wait: 2, done: 3, saved: 4 };

// 同一题反复失败时在列表里点名提示，别让用户一直点重试
const MANY_ATTEMPTS = 3;

let queueStopped = false;
// 只有本页真的动过队列才允许写回。否则开着两个标签页时，空队列的那个
// 一刷新/一关闭就会把另一个标签页刚攒的队列覆盖成空。
let queueTouched = false;

function queueItemFor(imageId, boxId) {
  return state.queue.find((q) => q.imageId === imageId && q.boxId === boxId) || null;
}

function queueSnapshot() {
  return state.queue.map((q) => ({
    id: q.id,
    boxId: q.boxId,
    imageId: q.imageId,
    bbox: q.bbox,
    // 刷新时还在跑的项续不上（请求已随页面销毁），回落成 wait 让用户决定是否重跑
    status: q.status === 'run' || q.status === 'saving' ? 'wait' : q.status,
    fields: q.fields,
    meta: q.meta,
    crop: q.crop,
    // 批量入库时框好的图形区域，刷新后不该让用户再框一遍
    figureBox: q.figureBox || null,
    error: q.error,
    errorKind: q.errorKind,
    attempts: q.attempts || 0,
    savedUid: q.savedUid || null,
    savedAt: q.savedAt || null,
    question: q.question || null,
  }));
}

/** 队列落服务端 SQLite：浏览器 localStorage 有 5MB 配额（AI 原始响应会被截断）、
 *  多标签页互相覆盖、换台机器就丢。改存服务端后这些问题都没有了。 */
function persistQueue(immediate = false) {
  if (!queueTouched) return;
  const write = () => {
    put('/api/queue', { items: queueSnapshot() }).catch(() => {
      /* 队列是临时工作区，写失败不打断操作，下次变更还会重试 */
    });
  };
  clearTimeout(persistQueue.timer);
  if (immediate) write();
  else persistQueue.timer = setTimeout(write, 400);
}

async function restoreQueue() {
  let items = [];
  try {
    const res = await get('/api/queue');
    items = Array.isArray(res?.items) ? res.items : [];
  } catch {
    items = [];
  }
  if (!items.length) return;
  const revived = [];
  for (const raw of items) {
    if (!raw?.bbox || !raw.imageId) continue;
    queueSeq = Math.max(queueSeq, Number(String(raw.id || '').slice(1)) || 0);
    // run / saving 是上次页面被杀时残留的状态，回退成待解析
    const status = raw.status === 'run' || raw.status === 'saving' ? 'wait' : raw.status || 'wait';
    state.queue.push({ ...raw, status });
    revived.push(state.queue[state.queue.length - 1]);
  }
  if (!revived.length) return;
  queueTouched = true;
  const pending = revived.filter((q) => q.status === 'wait').length;
  const saved = revived.filter((q) => q.status === 'saved').length;
  const failed = revived.filter((q) => q.status === 'error').length;
  const bits = [`已恢复上次的 ${revived.length} 道题`];
  if (saved) bits.push(`${saved} 道已入库`);
  if (pending) bits.push(`${pending} 道待解析`);
  if (failed) bits.push(`${failed} 道待重试`);
  toast(bits.join('，'), 'ok', 4200);
}

async function solveSelected() {
  if (!state.images.length) return;
  // 一次提交**所有图片**上的选中框：以前只认当前图片，切一张就要点一次「解析」
  let added = 0;
  let revived = 0;
  for (const image of state.images) {
    const boxes = state.boxesByImage[image.id] || [];
    for (const box of boxes) {
      if (!box.selected) continue;
      const existing = queueItemFor(image.id, box.id);
      if (!existing) {
        queueSeq += 1;
        state.queue.push({
          id: `q${queueSeq}`,
          boxId: box.id,
          imageId: image.id,
          bbox: { x: box.x, y: box.y, w: box.w, h: box.h },
          status: 'wait',
          fields: null,
          meta: null,
          crop: null,
          error: null,
          errorKind: null,
          attempts: 0,
        });
        added += 1;
      } else if (existing.status === 'error') {
        // 失败过的块：只重试，不再插一条重复记录
        resetToWait(existing);
        revived += 1;
      }
    }
  }
  if (!added && !revived) {
    toast('所选题目都已在队列里，可直接在队列中操作', '');
    return;
  }
  queueTouched = true;
  persistQueue();
  renderQueue();
  syncToolbar();
  if (added) {
    const imgs = new Set(state.queue.filter((q) => q.status === 'wait').map((q) => q.imageId)).size;
    toast(`已加入 ${added} 道题${imgs > 1 ? `（${imgs} 张图，按并发限制一起跑）` : ''}`, 'ok');
  }
  await runQueue();
}

async function runQueue() {
  if (solving) return;
  solving = true;
  queueStopped = false;
  syncToolbar();
  const concurrency = Math.max(1, Math.min(6, Number(state.settings?.ai?.concurrency || 2)));
  try {
    for (;;) {
      await Promise.all(Array.from({ length: concurrency }, () => worker()));
      // 所有 worker 都退了之后可能又有新题进来（解析中点解析），再来一轮
      if (queueStopped) break;
      if (!state.queue.some((q) => q.status === 'wait')) {
        // 边跑边加题时，新任务可能刚好卡在两次检查之间：
        // 让出一个事件循环再确认一次，别把刚点「解析」的题晾在队列里
        await new Promise((resolve) => setTimeout(resolve, 60));
        if (!state.queue.some((q) => q.status === 'wait')) break;
      }
    }
  } finally {
    solving = false;
    syncToolbar();
    renderQueue();
    persistQueue(true);
  }
}

async function worker() {
  for (;;) {
    if (queueStopped) return;
    const item = state.queue.find((q) => q.status === 'wait');
    if (!item) return;
    queueTouched = true;
    item.status = 'run';
    item.attempts = (item.attempts || 0) + 1;
    item.error = null;
    item.errorKind = null;
    renderQueue();
    try {
      const res = await post('/api/ai/solve', {
        image_id: item.imageId,
        bbox: item.bbox,
        subject: state.settings?.general?.subject || '数学',
      });
      item.fields = res.fields;
      item.meta = res.meta;
      item.crop = res.crop;
      item.status = 'done';
      item.solvedAt = Date.now();
    } catch (err) {
      item.status = 'error';
      item.error = err.message || '解析失败';
      item.errorKind = classifyError(err);
      if (item.errorKind === 'format') {
        // 模型答了但内容不可用（JSON 损坏 / 缺题干），必须让用户看到
        toast(`AI 输出无法解析：${item.error}`, 'err', 7000);
      } else if (item.errorKind === 'config') {
        // 没配 Key / 没选模型：后面的题也不用试了
        queueStopped = true;
        toast(item.error, 'err', 5200);
      }
    }
    persistQueue();
    renderQueue();
    syncToolbar();
  }
}

function classifyError(err) {
  const msg = String(err?.message || '');
  if (/尚未配置|缺少 API Key|未指定模型/.test(msg)) return 'config';
  if (err?.status === 422) return 'format';
  if (Number(err?.status) >= 500) return 'upstream';
  return 'network';
}

function resetToWait(item) {
  queueTouched = true;
  item.status = 'wait';
  item.error = null;
  item.errorKind = null;
  item.attempts = 0;
}

function retryFailed() {
  let count = 0;
  for (const item of state.queue) {
    if (item.status === 'error') {
      resetToWait(item);
      count += 1;
    }
  }
  if (!count) {
    toast('没有失败的题目', '');
    return;
  }
  persistQueue();
  renderQueue();
  syncToolbar();
  runQueue();
}

function continueQueue() {
  const pending = state.queue.filter((q) => q.status === 'wait').length;
  if (!pending) {
    toast('没有待解析的题目', '');
    return;
  }
  toast(`继续解析 ${pending} 道题`, 'ok');
  runQueue();
}

function clearSaved() {
  const before = state.queue.length;
  state.queue = state.queue.filter((q) => q.status !== 'saved');
  const removed = before - state.queue.length;
  if (!removed) return;
  queueTouched = true;
  persistQueue(true);
  renderQueue();
  syncToolbar();
  toast(`已从队列移除 ${removed} 道，题库里的题目不受影响`, 'ok', 3600);
}

function clearQueue() {
  if (!state.queue.length) return;
  const saved = state.queue.filter((q) => q.status === 'saved').length;
  const unsaved = state.queue.length - saved;
  const tip = saved
    ? `清空队列？\n\n已入库的 ${saved} 道仍在题库里；${unsaved} 道未保存的解析结果会被丢弃。`
    : '清空队列？未保存的解析结果会被丢弃。';
  if (!window.confirm(tip)) return;
  state.queue = [];
  queueTouched = true;
  persistQueue(true);
  renderQueue();
  syncToolbar();
}

function dropItem(item) {
  if (item.status === 'run' || item.status === 'saving') {
    toast('正在处理中，稍等再移除', '');
    return;
  }
  state.queue = state.queue.filter((q) => q.id !== item.id);
  queueTouched = true;
  persistQueue(true);
  renderQueue();
  syncToolbar();
}

function draftFromFields(fields) {
  if (!fields) return {};
  // 队列是落 localStorage 的：早先解析的条目里 \n 可能是两个字符，
  // 这里统一还原一次，textarea 和预览才不会一直显示「\n」。
  const T = (v) => (typeof v === 'string' ? fixLiteralEscapes(v) : v);
  return {
    stem: T(fields.question_text),
    answer: T(fields.answer),
    error_reason: T(fields.error_analysis),
    solution_fix: T(fields.solution_fix),
    solution_alt: T(fields.solution_alt),
    knowledge_points: fields.knowledge_points,
    question_type: fields.question_type,
    difficulty: fields.difficulty,
    notes: T(fields.notes),
    figure_svg: fields.figure_svg,
    figure_tikz: fields.figure_tikz,
    figure_smiles: fields.figure_smiles,
    figure_flow: fields.figure_flow,
    needs_figure: fields.needs_figure,
  };
}

function questionPayload(item) {
  return {
    ...draftFromFields(item.fields),
    subject: state.settings?.general?.subject || '数学',
    crop_token: item.crop?.token,
    image_id: item.imageId,
    bbox: item.bbox ? [item.bbox.x, item.bbox.y, item.bbox.w, item.bbox.h] : undefined,
    // 批量入库时用户在模态框里框好的图形区域
    figure_box: item.figureBox || undefined,
    ai_meta: item.meta,
    raw_ai: item.meta?.raw_preview,
  };
}

function markSaved(item, question) {
  queueTouched = true;
  item.status = 'saved';
  item.savedAt = Date.now();
  if (question?.uid) item.savedUid = question.uid;
  if (question) item.question = question;
}

/** 队列里的「入库」：不开抽屉，直接存。 */
async function saveItem(item, { silent = false } = {}) {
  if (item.status !== 'done') return null;
  if (!item.fields?.question_text) {
    toast('这道题缺少题干，请点开核对后再保存', 'err');
    return null;
  }
  item.status = 'saving';
  renderQueue();
  syncToolbar();
  try {
    const res = await post('/api/questions', questionPayload(item));
    markSaved(item, res.question);
    emit('question:created', res.question);
    emit('bank:changed');
    if (!silent) toast('已存入题库', 'ok');
    return res.question;
  } catch (err) {
    item.status = 'done';
    toast(err.message || '保存失败', 'err');
    return null;
  } finally {
    persistQueue(true);
    renderQueue();
    syncToolbar();
  }
}

// ---------------------------------------------------------------------------
// 批量收尾
//
// 一道一道点「入库」太磨人，尤其一次识别十几题的时候。这里给两个总按钮：
// 全部入库、全部组卷。需要框选图形的题会先弹裁剪框——不裁就存，
// 卷子上会贴进整块题干截图，图比字还大。
// ---------------------------------------------------------------------------

/** 这题必须让用户先框出图形吗（已经重绘过或生成了结构式就不用） */
function needsManualCrop(item) {
  const f = item.fields || {};
  if (f.figure_svg || f.figure_smiles || f.figure_tikz) return false;
  if (item.figureBox) return false;
  return Boolean(f.needs_figure);
}

function setBatchBusy(on) {
  batchBusy = on;
  updateQueueHead();
}

/** 逐题询问裁剪；返回 false 表示用户放弃这一题 */
async function cropIfNeeded(item) {
  if (!needsManualCrop(item)) return true;
  const url = item.crop?.url;
  if (!url) return true;
  const box = await openCropper({ url, box: item.figureBox || null });
  if (!box) return false;
  item.figureBox = box;
  persistQueue();
  return true;
}

/** 一键入库：逐条保存，要裁图的先弹框框选 */
async function saveAll() {
  if (batchBusy) return;
  const targets = state.queue.filter((q) => q.status === 'done');
  if (!targets.length) {
    toast('没有待入库的题', '');
    return;
  }
  setBatchBusy(true);
  let ok = 0;
  let skipped = 0;
  let failed = 0;
  try {
    for (const item of targets) {
      if (!(await cropIfNeeded(item))) {
        skipped += 1;
        continue;
      }
      const saved = await saveItem(item, { silent: true });
      if (saved) ok += 1;
      else failed += 1;
    }
  } finally {
    setBatchBusy(false);
  }
  const bits = [`已入库 ${ok} 道`];
  if (skipped) bits.push(`跳过 ${skipped} 道（未框选图形）`);
  if (failed) bits.push(`${failed} 道失败`);
  toast(bits.join('，'), ok ? 'ok' : 'err', 5200);
}

/** 一键组卷：未入库的先存，然后把队列里所有题加进组卷 */
async function cartAll() {
  if (batchBusy) return;
  const pending = state.queue.filter((q) => q.status === 'done');
  if (pending.length) await saveAll();
  const uids = state.queue
    .filter((q) => q.status === 'saved' && q.savedUid)
    .map((q) => q.savedUid);
  if (!uids.length) {
    toast('队列里还没有已入库的题', 'err');
    return;
  }
  let added = 0;
  for (const uid of uids) {
    if (state.cart.includes(uid)) continue;
    state.cart.push(uid);
    added += 1;
  }
  saveCart();
  if (added) toast(`已加入 ${added} 道到组卷（共 ${state.cart.length} 题）`, 'ok', 4200);
  else toast(`这些题已经在组卷里了（共 ${state.cart.length} 题）`, '');
}

function openItemEditor(item) {
  if (item.status !== 'done' && item.status !== 'saved') return;
  const saved = item.status === 'saved';
  openEditor({
    uid: saved ? item.savedUid : undefined,
    draft: item.question || draftFromFields(item.fields),
    meta: item.meta,
    crop: item.crop,
    cropUrl: item.crop?.url || item.question?.crop_url,
    imageId: item.imageId,
    bbox: item.bbox ? [item.bbox.x, item.bbox.y, item.bbox.w, item.bbox.h] : null,
    raw: item.meta?.raw_preview,
    onSaved: (question) => {
      // 关键：保存后切到 saved 并把服务器返回的内容回写，
      // 否则再点开看到的还是编辑前的草稿。
      markSaved(item, question);
      persistQueue(true);
      renderQueue();
      syncToolbar();
      emit('bank:changed');
    },
    onDeleted: () => {
      // 在抽屉里删掉了已入库的题：退回「待入库」，还能重新存一份
      item.status = 'done';
      item.savedUid = null;
      item.question = null;
      persistQueue(true);
      renderQueue();
      syncToolbar();
      emit('bank:changed');
    },
  });
}

function ago(ts) {
  if (!ts) return '';
  const diff = Date.now() - Number(ts);
  if (!Number.isFinite(diff) || diff < 0) return '';
  if (diff < 60000) return '刚刚';
  if (diff < 3600000) return `${Math.floor(diff / 60000)} 分钟前`;
  if (diff < 86400000) return `${Math.floor(diff / 3600000)} 小时前`;
  return new Date(Number(ts)).toLocaleDateString('zh-CN');
}

function usageText(meta) {
  const u = meta?.usage || {};
  const pin = Number(u.prompt_tokens || 0);
  const pout = Number(u.completion_tokens || 0);
  if (!pin && !pout) return '';
  return `token ↑${pin} ↓${pout}`;
}


function queueSub(item) {
  switch (item.status) {
    case 'saved': {
      const bits = ['已存入题库'];
      const when = ago(item.savedAt);
      if (when) bits.push(when);
      const tk = usageText(item.meta);
      if (tk) bits.push(tk);
      return bits.join(' · ');
    }
    case 'done': {
      const bits = [];
      if (item.fields?.question_type) bits.push(item.fields.question_type);
      if (item.meta?.latency_ms) bits.push(`${(item.meta.latency_ms / 1000).toFixed(1)}s`);
      const tk = usageText(item.meta);
      if (tk) bits.push(tk);
      bits.push('点「入库」保存，或点开核对');
      return bits.join(' · ');
    }
    case 'saving':
      return '正在写入题库…';
    case 'run':
      return item.attempts > 1 ? `第 ${item.attempts} 次尝试…` : '正在请求模型…';
    case 'error': {
      const bits = [item.error || '解析失败'];
      if (item.attempts > 1) bits.push(`已试 ${item.attempts} 次`);
      if (item.attempts >= MANY_ATTEMPTS) bits.push('建议先检查设置或网络');
      if (item.errorKind === 'config') bits.push('检查设置里的 API Key');
      else if (item.errorKind === 'format') bits.push('原始响应已存档');
      return bits.join(' · ');
    }
    default:
      return '排队中';
  }
}

function updateQueueHead() {
  const total = state.queue.length;
  const saved = state.queue.filter((q) => q.status === 'saved').length;
  const failed = state.queue.filter((q) => q.status === 'error').length;
  const waiting = state.queue.filter((q) => q.status === 'wait').length;
  const pending = total - saved;

  if (el.queueCount) el.queueCount.textContent = String(total);
  if (el.btnContinue) {
    el.btnContinue.classList.toggle('hidden', !waiting || solving);
    el.btnContinue.textContent = `继续 ${waiting}`;
    el.btnContinue.title = `继续解析队列里剩余 ${waiting} 道题`;
  }
  if (el.btnRetry) {
    el.btnRetry.classList.toggle('hidden', !failed);
    el.btnRetry.textContent = `重试 ${failed}`;
    el.btnRetry.title = `重新解析失败的 ${failed} 道题`;
  }
  if (el.btnClearSaved) {
    el.btnClearSaved.classList.toggle('hidden', !saved);
    el.btnClearSaved.textContent = '清理';
    el.btnClearSaved.title = `从队列移除 ${saved} 道已入库的题（题库里仍保留）`;
  }
  if (el.btnClearQueue) el.btnClearQueue.disabled = !total;

  // 批量按钮：有待入库或已入库的题才出现，避免空队列时占着位置
  const doneCount = state.queue.filter((q) => q.status === 'done').length;
  if (el.queueBatch) {
    // 批量执行中也保持可见：按钮变成「入库中…」，直接隐藏会让人以为点丢了
    el.queueBatch.classList.toggle('hidden', !batchBusy && !doneCount && !saved);
  }
  if (el.btnSaveAll) {
    el.btnSaveAll.disabled = batchBusy || !doneCount;
    el.btnSaveAll.textContent = batchBusy ? '入库中…' : doneCount ? `全部入库 ${doneCount}` : '全部入库';
  }
  if (el.btnCartAll) {
    el.btnCartAll.disabled = batchBusy || (!doneCount && !saved);
    el.btnCartAll.textContent = batchBusy
      ? '处理中…'
      : `全部加入组卷${saved ? ` ${saved}` : ''}`;
  }

  const showBar = total > 0 && pending > 0;
  if (el.queueProgress) {
    el.queueProgress.classList.toggle('hidden', !showBar);
    if (showBar) {
      el.queueProgressBar.style.width = `${Math.round((saved / total) * 100)}%`;
      const bits = [`${saved} / ${total} 已入库`];
      if (failed) bits.push(`${failed} 道失败`);
      el.queueProgressText.textContent = bits.join(' · ');
    }
  }
}

function queueRow(item, index) {
  const st = STATUS[item.status] || STATUS.wait;
  const row = document.createElement('div');
  row.className = `queue-item ${item.status}`;
  row.dataset.id = item.id;

  const thumb = document.createElement('div');
  thumb.className = 'thumb';
  const src = item.crop?.url || item.question?.crop_url;
  if (src) {
    thumb.style.backgroundImage = `url("${src}")`;
  } else {
    const ph = document.createElement('span');
    ph.className = 'thumb-ph';
    ph.textContent = String(index);
    thumb.appendChild(ph);
  }

  const meta = document.createElement('div');
  meta.className = 'qmeta';
  const title = document.createElement('div');
  title.className = 'qtitle';
  title.textContent = `第 ${index} 题`;
  if (item.fields?.question_type) {
    const t = document.createElement('span');
    t.className = 'qtype';
    t.textContent = item.fields.question_type;
    title.appendChild(t);
  }
  const sub = document.createElement('div');
  sub.className = 'qsub';
  sub.textContent = queueSub(item);
  sub.title = sub.textContent;
  meta.append(title, sub);

  const badge = document.createElement('span');
  badge.className = `badge ${st.cls}`;
  badge.textContent = st.text;

  const ops = document.createElement('div');
  ops.className = 'qops';
  const addBtn = (label, act, cls = '', title = '') => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = `mini-btn ${cls}`.trim();
    b.textContent = label;
    b.dataset.act = act;
    if (title) b.title = title;
    ops.appendChild(b);
    return b;
  };

  if (item.status === 'done') {
    // 点整行就是审核，这里只放「不入库」的快捷操作，省出标题宽度
    addBtn('入库', 'save', 'primary-mini', '直接存入题库');
  } else if (item.status === 'saved') {
    addBtn('编辑', 'open', '', '修改这道题');
  } else if (item.status === 'error') {
    addBtn('重试', 'retry', 'primary-mini', '重新解析这一题');
  } else if (item.status === 'wait') {
    addBtn('取消', 'drop', '', '从队列移除');
  }
  if (item.status !== 'run' && item.status !== 'saving') {
    addBtn('×', 'drop', 'icon-mini', '从队列移除');
  }

  // 侧栏只有 320px：徽标占标题行右侧、按钮占副标题行右侧，
  // 中间留给文字。没有缩略图时干脆不占那一列，省出的宽度全给标题。
  if (src) row.append(thumb);
  row.append(meta, badge, ops);

  row.addEventListener('click', (e) => {
    const act = e.target.closest('button[data-act]')?.dataset.act;
    if (act === 'save') {
      saveItem(item);
      return;
    }
    if (act === 'retry') {
      resetToWait(item);
      persistQueue();
      renderQueue();
      syncToolbar();
      runQueue();
      return;
    }
    if (act === 'drop') {
      dropItem(item);
      return;
    }
    if (act === 'open' || item.status === 'done' || item.status === 'saved') {
      openItemEditor(item);
      return;
    }
    if (item.status === 'error') {
      resetToWait(item);
      persistQueue();
      renderQueue();
      syncToolbar();
      runQueue();
    }
  });

  return row;
}

function renderQueue() {
  if (!el.queueList) return;
  el.queueList.textContent = '';
  updateQueueHead();
  if (!state.queue.length) {
    const note = document.createElement('div');
    note.className = 'empty-note';
    note.textContent = '勾选题目后出现在这里';
    el.queueList.appendChild(note);
    return;
  }
  const items = [...state.queue].sort(
    (a, b) => (QUEUE_ORDER[a.status] ?? 9) - (QUEUE_ORDER[b.status] ?? 9),
  );
  // 编号按当前显示顺序 1..N：排序后不会出现「第 2、第 1、第 3」这种跳号
  items.forEach((item, idx) => {
    el.queueList.appendChild(queueRow(item, idx + 1));
  });
}

export function openImageById(id) {
  selectImage(id);
  document.querySelector('.tab[data-view="recognize"]')?.click();
}
