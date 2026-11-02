// 画布框选：画框、拖动、缩放、点选
const HANDLE = 8;         // 手柄尺寸（屏幕像素）
const MIN_SIZE = 18;      // 最小框（原图像素）

export class BoxSelector {
  constructor({ image, canvas, onChange, onActiveChange }) {
    this.img = image;
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.onChange = onChange || (() => {});
    this.onActiveChange = onActiveChange || (() => {});
    this.boxes = [];
    this.activeId = null;
    this.scale = 1;
    this.mode = 'idle';
    this.drag = null;
    this.hoverId = null;
    this._bind();
  }

  setBoxes(boxes) {
    this.boxes = boxes;
    if (this.activeId && !boxes.some((b) => b.id === this.activeId)) this.activeId = null;
    const p = this.canvas.parentElement;
    if (p) {
      const r = this.img.getBoundingClientRect();
      if (r.width) this.canvas.style.width = `${r.width}px`;
      if (r.height) this.canvas.style.height = `${r.height}px`;
    }
    this.resize();
  }

  resize() {
    const rect = this.img.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const dpr = window.devicePixelRatio || 1;
    this.canvas.style.width = `${rect.width}px`;
    this.canvas.style.height = `${rect.height}px`;
    this.canvas.width = Math.round(rect.width * dpr);
    this.canvas.height = Math.round(rect.height * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.ctx.imageSmoothingEnabled = true;
    this.scale = rect.width / (this.img.naturalWidth || 1);
    this.draw();
  }

  // ---------- 坐标换算 ----------
  toImage(px, py) {
    return { x: px / this.scale, y: py / this.scale };
  }

  toDisplay(box) {
    return { x: box.x * this.scale, y: box.y * this.scale, w: box.w * this.scale, h: box.h * this.scale };
  }

  sorted() {
    return [...this.boxes].sort((a, b) => (a.y - b.y) || (a.x - b.x));
  }

  // ---------- 绘制 ----------
  draw() {
    const ctx = this.ctx;
    const { width, height } = this.canvas.getBoundingClientRect();
    ctx.clearRect(0, 0, width, height);
    const order = this.sorted();
    order.forEach((box, index) => {
      const d = this.toDisplay(box);
      const active = box.id === this.activeId;
      const hover = box.id === this.hoverId;
      ctx.save();
      if (box.selected) {
        ctx.fillStyle = active ? 'rgba(47,91,215,.16)' : 'rgba(47,91,215,.09)';
      } else {
        ctx.fillStyle = active ? 'rgba(255,255,255,.16)' : 'rgba(255,255,255,.04)';
      }
      ctx.fillRect(d.x, d.y, d.w, d.h);

      ctx.lineWidth = active ? 2.2 : 1.5;
      if (box.selected) {
        ctx.strokeStyle = active ? '#2f5bd7' : 'rgba(47,91,215,.75)';
        ctx.setLineDash([]);
      } else {
        ctx.strokeStyle = box.source === 'user' ? 'rgba(178,118,27,.85)' : 'rgba(120,114,104,.65)';
        ctx.setLineDash([5, 4]);
      }
      if (hover && !active) ctx.strokeStyle = '#2f5bd7';
      ctx.strokeRect(d.x + 0.5, d.y + 0.5, d.w - 1, d.h - 1);
      ctx.setLineDash([]);

      // 序号
      const label = String(index + 1);
      ctx.font = '600 11px "Segoe UI", system-ui, sans-serif';
      const tw = Math.max(16, ctx.measureText(label).width + 8);
      ctx.fillStyle = box.selected ? '#2f5bd7' : 'rgba(90,85,76,.82)';
      roundRect(ctx, d.x, d.y, tw, 15, 4);
      ctx.fill();
      ctx.fillStyle = '#fff';
      ctx.fillText(label, d.x + 4.5, d.y + 11);

      // 红笔痕迹标记
      if (box.mark_count > 0) {
        const tagW = 30;
        const hx = d.x + d.w - tagW - 2;
        ctx.fillStyle = 'rgba(201,64,59,.92)';
        roundRect(ctx, hx, d.y + 2, tagW, 14, 4);
        ctx.fill();
        ctx.fillStyle = '#fff';
        ctx.font = '600 9.5px "Segoe UI", system-ui, sans-serif';
        ctx.fillText(`红笔${box.mark_count > 1 ? box.mark_count : ''}`, hx + 3.5, d.y + 12);
      }

      if (active) {
        ctx.fillStyle = '#fff';
        ctx.strokeStyle = '#2f5bd7';
        ctx.lineWidth = 1.6;
        for (const [hx2, hy2] of handlePoints(d)) {
          ctx.beginPath();
          ctx.rect(hx2 - HANDLE / 2, hy2 - HANDLE / 2, HANDLE, HANDLE);
          ctx.fill();
          ctx.stroke();
        }
      }
      ctx.restore();
    });
  }

  // ---------- 命中测试 ----------
  hitHandle(px, py) {
    const box = this.boxes.find((b) => b.id === this.activeId);
    if (!box) return -1;
    const d = this.toDisplay(box);
    const pts = handlePoints(d);
    for (let i = 0; i < pts.length; i += 1) {
      if (Math.abs(px - pts[i][0]) <= HANDLE && Math.abs(py - pts[i][1]) <= HANDLE) return i;
    }
    return -1;
  }

  hitBox(px, py) {
    const order = this.sorted();
    for (let i = order.length - 1; i >= 0; i -= 1) {
      const d = this.toDisplay(order[i]);
      if (px >= d.x && px <= d.x + d.w && py >= d.y && py <= d.y + d.h) return order[i];
    }
    return null;
  }

  // ---------- 事件 ----------
  _pos(e) {
    const r = this.canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }

  _bind() {
    const c = this.canvas;
    c.addEventListener('pointerdown', (e) => this._down(e));
    c.addEventListener('pointermove', (e) => this._move(e));
    c.addEventListener('pointerup', (e) => this._up(e));
    c.addEventListener('pointercancel', (e) => this._up(e));
    c.addEventListener('pointerleave', () => { this.hoverId = null; this.draw(); });
    c.addEventListener('contextmenu', (e) => {
      e.preventDefault();
      const p = this._pos(e);
      const box = this.hitBox(p.x, p.y);
      if (box) {
        this.boxes = this.boxes.filter((b) => b.id !== box.id);
        if (this.activeId === box.id) { this.activeId = null; this.onActiveChange(null); }
        this.onChange();
        this.draw();
      }
    });
  }

  _down(e) {
    if (e.button !== 0) return;
    this.canvas.setPointerCapture(e.pointerId);
    const p = this._pos(e);
    const hi = this.hitHandle(p.x, p.y);
    if (hi >= 0) {
      const box = this.boxes.find((b) => b.id === this.activeId);
      this.mode = 'resize';
      this.drag = { handle: hi, start: p, origin: { ...box }, moved: false };
      return;
    }
    const hit = this.hitBox(p.x, p.y);
    if (hit) {
      this.activeId = hit.id;
      this.onActiveChange(hit.id);
      this.mode = 'maybe-move';
      this.drag = { start: p, origin: { ...hit }, moved: false, clicked: hit };
      this.draw();
      return;
    }
    this.mode = 'draw';
    const img = this.toImage(p.x, p.y);
    this.drag = { start: p, startImg: img, current: img, moved: false };
    this.activeId = null;
    this.onActiveChange(null);
    this.draw();
  }

  _move(e) {
    const p = this._pos(e);
    if (this.mode === 'idle') {
      const hit = this.hitBox(p.x, p.y);
      const id = hit ? hit.id : null;
      if (id !== this.hoverId) { this.hoverId = id; this.draw(); }
      return;
    }
    if (!this.drag) return;
    const dx = p.x - this.drag.start.x;
    const dy = p.y - this.drag.start.y;
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) this.drag.moved = true;

    if (this.mode === 'draw') {
      const cur = this.toImage(p.x, p.y);
      this.drag.current = cur;
      this.draw();
      const a = this.drag.startImg;
      const b = cur;
      const ctx = this.ctx;
      const x = Math.min(a.x, b.x) * this.scale;
      const y = Math.min(a.y, b.y) * this.scale;
      const w = Math.abs(a.x - b.x) * this.scale;
      const h = Math.abs(a.y - b.y) * this.scale;
      ctx.save();
      ctx.fillStyle = 'rgba(47,91,215,.12)';
      ctx.strokeStyle = '#2f5bd7';
      ctx.lineWidth = 1.8;
      ctx.setLineDash([6, 4]);
      ctx.fillRect(x, y, w, h);
      ctx.strokeRect(x, y, w, h);
      ctx.restore();
      return;
    }

    const box = this.boxes.find((b) => b.id === this.activeId);
    if (!box) return;

    if (this.mode === 'maybe-move' && this.drag.moved) this.mode = 'move';

    if (this.mode === 'move') {
      const nx = this.drag.origin.x + dx / this.scale;
      const ny = this.drag.origin.y + dy / this.scale;
      box.x = clamp(nx, 0, this.img.naturalWidth - box.w);
      box.y = clamp(ny, 0, this.img.naturalHeight - box.h);
      this.draw();
      return;
    }

    if (this.mode === 'resize') {
      const o = this.drag.origin;
      const ix = dx / this.scale;
      const iy = dy / this.scale;
      let { x, y, w, h } = o;
      switch (this.drag.handle) {
        case 0: x = o.x + ix; y = o.y + iy; w = o.w - ix; h = o.h - iy; break; // 左上
        case 1: y = o.y + iy; h = o.h - iy; break;                            // 上中
        case 2: w = o.w + ix; y = o.y + iy; h = o.h - iy; break;              // 右上
        case 3: x = o.x + ix; w = o.w - ix; break;                            // 左中
        case 4: w = o.w + ix; break;                                          // 右中
        case 5: x = o.x + ix; w = o.w - ix; h = o.h + iy; break;              // 左下
        case 6: h = o.h + iy; break;                                          // 下中
        case 7: w = o.w + ix; h = o.h + iy; break;                            // 右下
        default: break;
      }
      if (w < MIN_SIZE) { w = MIN_SIZE; if (this.drag.handle === 0 || this.drag.handle === 3 || this.drag.handle === 5) x = o.x + o.w - MIN_SIZE; }
      if (h < MIN_SIZE) { h = MIN_SIZE; if (this.drag.handle <= 2) y = o.y + o.h - MIN_SIZE; }
      box.x = Math.max(0, x);
      box.y = Math.max(0, y);
      box.w = Math.min(w, this.img.naturalWidth - box.x);
      box.h = Math.min(h, this.img.naturalHeight - box.y);
      this.draw();
    }
  }

  _up(e) {
    if (this.mode === 'idle') return;
    const p = this._pos(e);
    if (this.mode === 'draw' && this.drag) {
      const a = this.drag.startImg;
      const b = this.toImage(p.x, p.y);
      const x = Math.min(a.x, b.x);
      const y = Math.min(a.y, b.y);
      const w = Math.abs(a.x - b.x);
      const h = Math.abs(a.y - b.y);
      if (w >= MIN_SIZE && h >= MIN_SIZE) {
        const box = {
          id: `u${Math.random().toString(36).slice(2, 8)}`,
          x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(h),
          source: 'user', selected: true, wrong_score: 0, mark_count: 0, mark_kinds: '',
        };
        this.boxes.push(box);
        this.activeId = box.id;
        this.onActiveChange(box.id);
        this.onChange();
      }
    } else if (this.mode === 'maybe-move' && this.drag && !this.drag.moved) {
      const box = this.drag.clicked;
      box.selected = !box.selected;
      this.onChange();
    } else if (this.mode === 'move' || this.mode === 'resize') {
      this.onChange();
    }
    this.mode = 'idle';
    this.drag = null;
    this.draw();
  }
}

function handlePoints(d) {
  const { x, y, w, h } = d;
  return [
    [x, y], [x + w / 2, y], [x + w, y],
    [x, y + h / 2], [x + w, y + h / 2],
    [x, y + h], [x + w / 2, y + h], [x + w, y + h],
  ];
}

function roundRect(ctx, x, y, w, h, r) {
  const rr = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + rr, y);
  ctx.arcTo(x + w, y, x + w, y + h, rr);
  ctx.arcTo(x + w, y + h, x, y + h, rr);
  ctx.arcTo(x, y + h, x, y, rr);
  ctx.arcTo(x, y, x + w, y, rr);
  ctx.closePath();
}

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
