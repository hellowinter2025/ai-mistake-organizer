// 题图裁剪：在整块题目截图上框出一块，只保留图形本身。
// 选区一律存**归一化坐标**（0~1，相对图片），这样窗口缩放后重新绘制不用换算。

import { $, show, hide } from './ui.js';

let resolver = null;
let start = null;
let sel = null; // {x, y, w, h} 归一化
let bound = false;

/** 图片在 stage 里的偏移与尺寸（stage 是 position:relative 的容器）。 */
function bounds() {
  const img = $('#cropImage');
  const stage = $('#cropStage');
  const ir = img.getBoundingClientRect();
  const sr = stage.getBoundingClientRect();
  return {
    left: ir.left - sr.left,
    top: ir.top - sr.top,
    width: ir.width,
    height: ir.height,
  };
}

/**
 * 让图片缩到屏幕能装下的大小。
 *
 * 只用 CSS 的 max-height:100% 在部分窗口尺寸下仍会溢出（百分比高度需要父级
 * 有确定高度），所以这里按视口实际可用高度算一次像素上限，作为兜底。
 */
function fitStage() {
  const img = $('#cropImage');
  const mask = $('#cropMask');
  const dialog = mask?.querySelector('.crop-dialog');
  if (!img || !dialog) return;
  const head = dialog.querySelector('.crop-head');
  const foot = dialog.querySelector('.crop-foot');
  const chrome = (head?.offsetHeight || 0) + (foot?.offsetHeight || 0) + 24;
  const availH = Math.max(160, Math.round(window.innerHeight * 0.92 - chrome));
  const availW = Math.max(200, Math.round(dialog.clientWidth - 28));
  img.style.maxHeight = `${availH}px`;
  img.style.maxWidth = `${availW}px`;
}

function paint() {
  const rect = $('#cropRect');
  if (!sel || sel.w * sel.h <= 0) {
    hide(rect);
    return;
  }
  const b = bounds();
  if (b.width < 2 || b.height < 2) {
    hide(rect);
    return;
  }
  rect.style.left = `${b.left + sel.x * b.width}px`;
  rect.style.top = `${b.top + sel.y * b.height}px`;
  rect.style.width = `${sel.w * b.width}px`;
  rect.style.height = `${sel.h * b.height}px`;
  show(rect);
}

/** 把指针位置换算成图片内的归一化坐标。 */
function toNorm(e) {
  const b = bounds();
  if (b.width < 2 || b.height < 2) return null;
  const sr = $('#cropStage').getBoundingClientRect();
  const x = Math.min(Math.max((e.clientX - sr.left - b.left) / b.width, 0), 1);
  const y = Math.min(Math.max((e.clientY - sr.top - b.top) / b.height, 0), 1);
  return { x, y };
}

function onDown(e) {
  if (e.button !== 0) return;
  const p = toNorm(e);
  if (!p) return;
  start = p;
  sel = { x: p.x, y: p.y, w: 0, h: 0 };
  paint();
  e.preventDefault();
  window.addEventListener('pointermove', onMove);
  window.addEventListener('pointerup', onUp, { once: true });
}

function onMove(e) {
  if (!start) return;
  const p = toNorm(e);
  if (!p) return;
  sel = {
    x: Math.min(start.x, p.x),
    y: Math.min(start.y, p.y),
    w: Math.abs(p.x - start.x),
    h: Math.abs(p.y - start.y),
  };
  paint();
}

function onUp() {
  window.removeEventListener('pointermove', onMove);
  start = null;
  // 抖动一下（<1% 边长）当成没画，免得留下一个点
  if (sel && (sel.w < 0.01 || sel.h < 0.01)) {
    sel = null;
    paint();
  }
}

function close(result) {
  hide($('#cropMask'));
  const fn = resolver;
  resolver = null;
  if (fn) fn(result);
}

function round4(v) {
  return +Number(v).toFixed(4);
}

function bindOnce() {
  if (bound) return;
  bound = true;
  $('#cropStage').addEventListener('pointerdown', onDown);
  $('#cropReset').addEventListener('click', () => {
    sel = null;
    paint();
  });
  $('#cropCancel').addEventListener('click', () => close(null));
  $('#cropMask').addEventListener('click', (e) => {
    if (e.target.id === 'cropMask') close(null);
  });
  $('#cropConfirm').addEventListener('click', () => {
    if (!sel || sel.w < 0.01 || sel.h < 0.01) {
      close(null);
      return;
    }
    close({ x: round4(sel.x), y: round4(sel.y), w: round4(sel.w), h: round4(sel.h) });
  });
  document.addEventListener('keydown', (e) => {
    if ($('#cropMask').classList.contains('hidden')) return;
    if (e.key === 'Escape') close(null);
    if (e.key === 'Enter') $('#cropConfirm').click();
  });
  // 窗口一改，图片尺寸和 stage 高度都变了：重新适配并重画选区
  window.addEventListener('resize', () => {
    if ($('#cropMask').classList.contains('hidden')) return;
    fitStage();
    paint();
  });
}

/**
 * 打开裁剪弹层。
 * @param {{url: string, box?: {x:number,y:number,w:number,h:number}|null}} opts
 * @returns {Promise<{x,y,w,h}|null>} 归一化裁剪框；取消返回 null
 */
export function openCropper({ url, box }) {
  bindOnce();
  const img = $('#cropImage');
  img.style.maxHeight = '';
  img.style.maxWidth = '';
  img.src = url;
  sel = null;
  show($('#cropMask'));
  hide($('#cropRect'));

  return new Promise((resolve) => {
    resolver = resolve;
    const ready = () => {
      fitStage();
      if (box && box.w > 0 && box.h > 0) {
        sel = { x: box.x, y: box.y, w: box.w, h: box.h };
        paint();
      }
    };
    if (img.complete && img.naturalWidth) {
      // 已缓存：等一帧让弹层完成布局，否则 clientWidth 还是 0
      requestAnimationFrame(ready);
    } else {
      img.addEventListener('load', () => requestAnimationFrame(ready), { once: true });
    }
  });
}

/** 用 canvas 把裁剪结果显示成缩略图（纯前端预览，不经过服务器）。 */
export function paintCropPreview(container, url, box, maxHeight = 180) {
  container.textContent = '';
  if (!url) return;
  const img = new Image();
  img.alt = '题图';
  img.src = url;
  if (!box || !(box.w > 0) || !(box.h > 0)) {
    container.appendChild(img);
    return;
  }
  img.addEventListener('load', () => {
    try {
      const sx = Math.round(box.x * img.naturalWidth);
      const sy = Math.round(box.y * img.naturalHeight);
      const sw = Math.max(1, Math.round(box.w * img.naturalWidth));
      const sh = Math.max(1, Math.round(box.h * img.naturalHeight));
      const scale = Math.min(1, maxHeight / sh);
      const canvas = document.createElement('canvas');
      canvas.width = Math.max(1, Math.round(sw * scale));
      canvas.height = Math.max(1, Math.round(sh * scale));
      canvas.getContext('2d').drawImage(img, sx, sy, sw, sh, 0, 0, canvas.width, canvas.height);
      container.textContent = '';
      container.appendChild(canvas);
    } catch {
      container.textContent = '';
      container.appendChild(img);
    }
  }, { once: true });
}
