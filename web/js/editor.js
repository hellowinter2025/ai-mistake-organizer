// 题目编辑抽屉（AI 结果审核 / 题库编辑共用）
import { post, patch, del } from './api.js';
import { renderRich, countFormulas, renderFigure } from './math.js';
import { $, toast, show, hide, debounce } from './ui.js';
import { emit, state } from './state.js';
import { openCropper, paintCropPreview } from './figure.js';

const el = {};
let ctx = null; // { uid, cropToken, imageId, bbox, meta, raw, onSaved, onDeleted }

export function initEditor() {
  el.mask = $('#editorMask');
  el.drawer = $('#editor');
  el.title = $('#editorTitle');
  el.meta = $('#editorMeta');
  el.previewCrop = $('#previewCrop');
  el.previewMath = $('#previewMath');
  el.warnings = $('#formulaWarnings');
  el.fStem = $('#fStem');
  el.fAnswer = $('#fAnswer');
  el.fError = $('#fError');
  el.fFix = $('#fFix');
  el.fAlt = $('#fAlt');
  el.fKp = $('#fKp');
  el.fType = $('#fType');
  el.fSubject = $('#fSubject');
  el.fDifficulty = $('#fDifficulty');
  el.fNotes = $('#fNotes');
  el.fStarred = $('#fStarred');
  el.fTikz = $('#fTikz');
  el.fTikzHint = $('#tikzHint');
  el.fSmiles = $('#fSmiles');
  el.fFlow = $('#fFlow');
  el.flowHint = $('#flowHint');
  el.chemHint = $('#chemHint');
  el.fNeedsFigure = $('#fNeedsFigure');
  el.figureCropBody = $('#figureCropPreview');
  el.previewFigure = document.createElement('div');
  el.previewFigure.className = 'preview-figure hidden';
  el.previewMath.parentElement.insertBefore(el.previewFigure, el.previewMath);

  $('#editorClose').addEventListener('click', closeEditor);
  el.mask.addEventListener('click', closeEditor);
  $('#btnSaveQuestion').addEventListener('click', save);
  $('#btnDeleteQuestion').addEventListener('click', remove);
  $('#btnReSolve').addEventListener('click', reSolve);
  $('#btnRenderTikz').addEventListener('click', renderTikz);
  $('#btnRenderChem').addEventListener('click', renderChem);
  $('#btnRenderFlow').addEventListener('click', renderFlow);
  $('#btnCropFigure').addEventListener('click', cropFigure);
  $('#btnClearFigureCrop').addEventListener('click', clearFigureCrop);
  $('#btnReEnhanceFigure')?.addEventListener('click', reEnhanceFigure);

  const refresh = debounce(refreshPreview, 220);
  for (const node of [el.fStem, el.fAnswer, el.fError, el.fFix, el.fAlt, el.fKp, el.fType, el.fSubject, el.fDifficulty, el.fNotes]) {
    node.addEventListener('input', refresh);
    node.addEventListener('change', refresh);
  }
  el.fTikz.addEventListener('input', () => {
    if (!el.fTikz.value.trim()) setTikzHint('未设置题图', '');
    else if (el.fTikz.value.trim() !== (ctx?.figureTikz || '')) setTikzHint('改动未渲染，点「渲染预览」或直接保存', 'warn');
  });
  // 勾/取消「改用原题截图」直接影响预览里显示哪张图
  el.fNeedsFigure?.addEventListener('change', () => {
    refreshFigureCrop();
    refreshPreview();
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !el.drawer.classList.contains('hidden')) closeEditor();
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter' && !el.drawer.classList.contains('hidden')) save();
  });
}

export function openEditor(payload) {
  ctx = { ...payload };
  const q = payload.question || payload.draft || {};
  ctx.question = q;
  el.title.textContent = payload.uid ? '编辑题目' : '审核 AI 解析结果';
  const metaParts = [];
  // 编号放最前面：截图/口头描述问题时直接报这个 6 位码就能定位
  if (q.code) metaParts.push(`编号 #${q.code}`);
  if (payload.meta?.model) metaParts.push(`模型 ${payload.meta.model}`);
  if (payload.meta?.latency_ms) metaParts.push(`${(payload.meta.latency_ms / 1000).toFixed(1)}s`);
  if (payload.meta?.strategy) metaParts.push(`解析方式 ${payload.meta.strategy}`);
  if (payload.meta?.mode) metaParts.push(payload.meta.mode);
  el.meta.textContent = metaParts.join(' · ') || '手动整理';

  el.fStem.value = q.stem || q.question_text || '';
  el.fAnswer.value = q.answer || '';
  el.fError.value = q.error_reason || q.error_analysis || '';
  el.fFix.value = q.solution_fix || '';
  el.fAlt.value = q.solution_alt || '';
  el.fKp.value = Array.isArray(q.knowledge_points) ? q.knowledge_points.join('、') : (q.knowledge_points || '');
  el.fType.value = q.question_type || '';
  el.fSubject.value = q.subject || state.settings?.general?.subject || '数学';
  el.fDifficulty.value = q.difficulty ? String(Math.round(Number(q.difficulty))) : '';
  el.fNotes.value = q.notes || '';
  el.fStarred.checked = Boolean(q.starred);
  el.fTikz.value = q.figure_tikz || '';
  el.fSmiles.value = smilesToText(q.figure_smiles);
  el.fFlow.value = flowToText(q.figure_flow);
  // 已编译的 SVG 单独放 ctx：它是渲染产物，用户改的是上面的源码
  ctx.figureSvg = q.figure_svg || '';
  // 预览只放服务端栅格化的 PNG（与 PDF/DOCX 同一张图），前端不渲染 SVG
  ctx.figureUrl = q.figure_png_url || '';
  ctx.figureTikz = q.figure_tikz || '';
  el.fNeedsFigure.checked = Boolean(q.needs_figure);
  ctx.figureBox = normalizeBox(q.figure_box);
  // 已增强的题图（服务端算好的那张，导出用的就是它）。之前预览走的是
  // canvas 现裁原始截图，用户看到的永远是没增强的原图 —— 以为增强没生效。
  ctx.figureCropUrl = q.figure_crop_url || '';
  ctx.figureBoxDirty = false;
  refreshFigureCrop();
  if (q.figure_tikz) setTikzHint('已编译', 'ok');
  else if (q.figure_svg) setTikzHint('早期版本生成的 SVG（没有源码）', '');
  else setTikzHint('可留空；AI 会自动重绘几何图形', '');

  el.previewCrop.textContent = '';
  const cropUrl = payload.cropUrl || q.crop_url;
  if (cropUrl) {
    const img = document.createElement('img');
    img.src = cropUrl;
    img.alt = '原题截图';
    el.previewCrop.appendChild(img);
  } else {
    el.previewCrop.textContent = '';
  }
  $('#btnReSolve').classList.toggle('hidden', !(payload.imageId && payload.bbox));
  $('#btnDeleteQuestion').classList.toggle('hidden', !payload.uid);
  refreshPreview();
  // 记一份基线，关闭时用来判断「改了但没保存」
  ctx.baseline = snapshot();
  ctx.dirty = false;
  show(el.mask);
  show(el.drawer);
  setTimeout(() => el.fStem.focus(), 60);
}

function snapshot() {
  try {
    return JSON.stringify(collect());
  } catch {
    return '';
  }
}

/**
 * 关闭抽屉。改过但没保存时先问一句——之前点遮罩或按 Esc 会直接把
 * 十几分钟的核对工作丢掉，且没有任何提示。
 */
export function closeEditor(force = false) {
  if (ctx && !force) {
    const changed = ctx.baseline !== undefined && snapshot() !== ctx.baseline;
    if (changed && !window.confirm('这道题有未保存的修改，确定关闭？')) return;
  }
  hide(el.mask);
  hide(el.drawer);
  ctx = null;
}

function collect() {
  return {
    stem: el.fStem.value.trim(),
    answer: el.fAnswer.value.trim(),
    error_reason: el.fError.value.trim(),
    solution_fix: el.fFix.value.trim(),
    solution_alt: el.fAlt.value.trim(),
    knowledge_points: el.fKp.value.split(/[、,，;；\/|]/).map((s) => s.trim()).filter(Boolean),
    subject: el.fSubject.value.trim() || '数学',
    question_type: el.fType.value || null,
    difficulty: el.fDifficulty.value ? Number(el.fDifficulty.value) : null,
    notes: el.fNotes.value.trim(),
    starred: el.fStarred.checked,
    figure_tikz: el.fTikz.value.trim(),
    figure_smiles: textToSmiles(el.fSmiles.value),
    figure_flow: textToFlow(el.fFlow.value),
    figure_svg: ctx?.figureSvg || '',
    needs_figure: el.fNeedsFigure.checked,
    figure_box: ctx?.figureBox || null,
  };
}

let lastErrors = [];

function refreshPreview() {
  const data = collect();
  el.previewMath.textContent = '';
  const errors = [];

  // 勾了「改用原题截图」→ 预览也贴截图（框选过的纯图形优先），
  // 否则用户以为导出的是重绘图；导出的确是截图。
  const wantsShot = Boolean(el.fNeedsFigure?.checked);
  const shotUrl = wantsShot ? (ctx?.figureCropUrl || cropSourceUrl() || '') : '';
  const figSrc = (shotUrl || ctx?.figureUrl || ctx?.figureSvg || '').trim();
  const figOk = renderFigure(el.previewFigure, figSrc);
  el.previewFigure.classList.toggle('hidden', !figOk);

  const sections = [
    ['题目', data.stem, 'sec-stem'],
    ['答案', data.answer, 'sec-answer'],
    ['错误原因', data.error_reason, 'sec-error'],
    ['纠正解析', data.solution_fix, 'sec-fix'],
    ['另解', data.solution_alt, 'sec-alt'],
    ['知识点', data.knowledge_points.join('、'), 'sec-kp'],
    ['备注', data.notes, 'sec-note'],
  ];
  for (const [label, text, cls] of sections) {
    if (!text) continue;
    const wrap = document.createElement('div');
    wrap.className = `psec ${cls}`;
    const h = document.createElement('h4');
    h.textContent = label;
    const box = document.createElement('div');
    box.className = 'block';
    const res = renderRich(box, text);
    errors.push(...res.errors);
    wrap.append(h, box);
    el.previewMath.appendChild(wrap);
  }
  lastErrors = errors;

  const messages = [];
  if (el.fTikz.value.trim() && !figSrc) {
    messages.push('题图还没编译：点「渲染预览」看效果，或直接保存（保存时后端会编译）。');
  }
  if (figSrc && !figOk) messages.push('题图无法渲染（可能不完整或被安全策略过滤），保存时将自动改为保留原题截图。');
  if (data.needs_figure && !figOk) messages.push('已标记「保留原题截图」：导出时会附上原题图片。');
  if (ctx?.meta?.degraded) {
    const how = String(ctx.meta.strategy || '');
    if (how.includes('salvage')) {
      messages.push('AI 输出的 JSON 结构损坏，已按字段逐个抢救恢复，部分字段可能缺失，请重点核对。');
    } else {
      messages.push('AI 输出未按标准 JSON 返回，已按文本解析，建议核对字段是否错位。');
    }
  }
  for (const w of ctx?.meta?.warnings || []) messages.push(w);
  for (const e of errors) messages.push(`公式渲染失败：${e.latex.slice(0, 60)} → ${e.message.slice(0, 80)}`);
  if (messages.length) {
    el.warnings.textContent = messages.slice(0, 6).join('；');
    show(el.warnings);
  } else {
    hide(el.warnings);
  }
}

async function save() {
  const data = collect();
  if (!data.stem) { toast('题目内容不能为空', 'err'); el.fStem.focus(); return; }
  const btn = $('#btnSaveQuestion');
  btn.disabled = true;
  btn.textContent = '保存中…';
  try {
    if (ctx?.uid) {
      const res = await patch(`/api/questions/${ctx.uid}`, data);
      toast('已保存修改', 'ok');
      emit('question:updated', res.question);
      ctx.saved = true;
      ctx.onSaved?.(res.question);
      closeEditor(true);
    } else {
      const payload = {
        ...data,
        crop_token: ctx?.crop?.token,
        image_id: ctx?.imageId,
        bbox: ctx?.bbox,
        ai_meta: ctx?.meta,
        raw_ai: ctx?.raw,
      };
      const res = await post('/api/questions', payload);
      toast('已存入题库', 'ok');
      emit('question:created', res.question);
      ctx.saved = true;
      ctx.onSaved?.(res.question);
      closeEditor(true);
    }
  } catch (err) {
    toast(err.message || '保存失败', 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '保存';
  }
}

async function remove() {
  if (!ctx?.uid) return;
  if (!window.confirm('确定删除这道题？删除后无法恢复。')) return;
  try {
    await del(`/api/questions/${ctx.uid}`);
    toast('已删除', 'ok');
    emit('question:deleted', { uid: ctx.uid });
    ctx.onDeleted?.(ctx.uid);
    closeEditor(true);
  } catch (err) {
    toast(err.message || '删除失败', 'err');
  }
}

async function reSolve() {
  if (!ctx?.imageId || !ctx?.bbox) return;
  const btn = $('#btnReSolve');
  btn.disabled = true;
  btn.textContent = '解析中…';
  try {
    const res = await post('/api/ai/solve', {
      subject: el.fSubject.value.trim() || undefined,
      image_id: ctx.imageId,
      bbox: ctx.bbox,
      extra: el.fNotes.value.trim(),
    });
    const f = res.fields;
    el.fStem.value = f.question_text || '';
    el.fAnswer.value = f.answer || '';
    el.fError.value = f.error_analysis || '';
    el.fFix.value = f.solution_fix || '';
    el.fAlt.value = f.solution_alt || '';
    el.fKp.value = (f.knowledge_points || []).join('、');
    el.fType.value = f.question_type || '';
    el.fDifficulty.value = f.difficulty ? String(Math.round(Number(f.difficulty))) : '';
    ctx.meta = res.meta;
    ctx.crop = res.crop;
    ctx.raw = res.meta?.raw_preview;
    if (res.crop?.url) {
      el.previewCrop.textContent = '';
      const img = document.createElement('img');
      img.src = res.crop.url;
      el.previewCrop.appendChild(img);
    }
    refreshPreview();
    toast('已重新解析，记得保存', 'ok');
  } catch (err) {
    toast(err.message || '解析失败', 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = '重新解析';
  }
}

function normalizeBox(box) {
  if (!box) return null;
  const src = Array.isArray(box) ? { x: box[0], y: box[1], w: box[2], h: box[3] } : box;
  const nums = ['x', 'y', 'w', 'h'].map((k) => Number(src?.[k]));
  if (nums.some((v) => !Number.isFinite(v)) || nums[2] <= 0 || nums[3] <= 0) return null;
  return { x: nums[0], y: nums[1], w: nums[2], h: nums[3] };
}

function cropSourceUrl() {
  return ctx?.cropUrl || ctx?.question?.crop_url || ctx?.crop?.url || null;
}

function refreshFigureCrop() {
  if (!el.figureCropBody) return;
  const url = cropSourceUrl();
  const box = ctx?.figureBox;
  const clearBtn = $('#btnClearFigureCrop');
  const reBtn = $('#btnReEnhanceFigure');
  const canCrop = Boolean(url);
  $('#btnCropFigure').disabled = !canCrop;
  $('#btnCropFigure').style.opacity = canCrop ? '1' : '.4';
  if (clearBtn) clearBtn.hidden = !box;
  if (reBtn) {
    const canRe = Boolean(ctx?.uid && box && url);
    reBtn.hidden = !canRe;
    reBtn.disabled = !canRe;
  }
  if (!url) {
    el.figureCropBody.innerHTML = '<span class="empty-note">这道题没有原始截图，无法裁剪</span>';
    return;
  }
  if (!box) {
    el.figureCropBody.innerHTML = '<span class="empty-note">未裁剪 · 导出时贴整块题目截图</span>';
    return;
  }
  // 有裁剪框就说明想贴图，勾上「保留原题截图」，否则导出时这张图不会被用到
  if (!el.fNeedsFigure.checked) el.fNeedsFigure.checked = true;

  // 框没改过 + 已有增强图 → 直接显示服务端那张（和导出完全一致）
  if (ctx.figureCropUrl && !ctx.figureBoxDirty) {
    el.figureCropBody.textContent = '';
    const img = new Image();
    img.alt = '增强后的题图';
    img.src = ctx.figureCropUrl;
    el.figureCropBody.appendChild(img);
    const note = document.createElement('div');
    note.className = 'crop-note';
    note.textContent = el.fNeedsFigure.checked
      ? '已勾选「改用原题截图」：导出就用这张（优先于 AI 重绘）'
      : '已增强：去批注 / 去手写 / 纸底拉白（勾上「改用原题截图」后导出用这张）';
    el.figureCropBody.appendChild(note);
    return;
  }
  paintCropPreview(el.figureCropBody, url, box);
  const note = document.createElement('div');
  note.className = 'crop-note';
  note.textContent = '新框选区域 · 保存后自动增强（去批注、纸底拉白）';
  el.figureCropBody.appendChild(note);
}

/** 重新增强：从原始截图重算这张题图（不做二次增强，不会越跑越糊）。 */
async function reEnhanceFigure() {
  if (!ctx?.uid) return;
  const btn = $('#btnReEnhanceFigure');
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = '增强中…';
  try {
    const res = await post(`/api/questions/${ctx.uid}/re-enhance`, {});
    const q = res.question || {};
    ctx.figureCropUrl = q.figure_crop_url ? `${q.figure_crop_url}?t=${Date.now()}` : '';
    ctx.figureBoxDirty = false;
    ctx.question = { ...(ctx.question || {}), ...q };
    refreshFigureCrop();
    emit('question:updated', q);
    toast('题图已重新增强', 'ok');
  } catch (err) {
    toast(err.message || '增强失败', 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

async function cropFigure() {
  const url = cropSourceUrl();
  if (!url) {
    toast('这道题没有原始截图，无法裁剪', 'err');
    return;
  }
  const box = await openCropper({ url, box: ctx?.figureBox || null });
  if (!box) return;
  ctx.figureBox = box;
  ctx.figureBoxDirty = true;
  if (!el.fNeedsFigure.checked) el.fNeedsFigure.checked = true;
  refreshFigureCrop();
  toast('已设置为只保留图形，保存后生效', 'ok');
}

function clearFigureCrop() {
  if (!ctx) return;
  ctx.figureBox = null;
  ctx.figureBoxDirty = true;
  ctx.figureCropUrl = '';
  refreshFigureCrop();
  toast('已恢复整块截图', '');
}

function setTikzHint(text, kind) {
  if (!el.fTikzHint) return;
  el.fTikzHint.textContent = text;
  el.fTikzHint.classList.toggle('warn', kind === 'warn');
  el.fTikzHint.classList.toggle('ok', kind === 'ok');
}

/**
 * 编译 TikZ 预览。
 *
 * 只在用户点按钮时编译：单张图要 0.5~1.6s，边打字边编译会把输入卡住。
 * 不点也没关系——保存时后端会再编译一次，图不会丢。
 */
async function renderTikz() {
  const code = el.fTikz.value.trim();
  const btn = $('#btnRenderTikz');
  if (!code) {
    ctx.figureSvg = '';
    ctx.figureUrl = '';
    ctx.figureTikz = '';
    setTikzHint('未设置题图', '');
    refreshPreview();
    return;
  }
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = '编译中…';
  try {
    const res = await post('/api/tikz/render', { code });
    ctx.figureSvg = res.svg;
    ctx.figureUrl = res.image_url || '';
    // 后端补全过代码（截断的 \end{tikzpicture}、漏的分号）→ 同步回输入框，
    // 否则用户看着还是坏的，下次保存又把坏代码写回库里
    const fixed = typeof res.code === 'string' && res.code.trim() ? res.code : code;
    if (fixed !== el.fTikz.value) el.fTikz.value = fixed;
    ctx.figureTikz = fixed;
    const notes = Array.isArray(res.fixes) && res.fixes.length ? ` · 已自动修复：${res.fixes.join('、')}` : '';
    setTikzHint(`编译通过 · ${res.ms}ms${res.cached ? '（命中缓存）' : ''}${notes}`, 'ok');
    refreshPreview();
  } catch (err) {
    ctx.figureSvg = '';
    ctx.figureUrl = '';
    setTikzHint(err.message || '编译失败', 'err');
    refreshPreview();
    toast(err.message || 'TikZ 编译失败', 'err', 5200);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

export function currentFormulaCount() {
  return countFormulas(el.fStem?.value || '');
}

/** {名称: SMILES} → 供输入框编辑的多行文本 */
function smilesToText(value) {
  if (!value) return '';
  if (typeof value === 'string') {
    // 识别结果里 figure_smiles 是 JSON 文本（库里存 TEXT），直接丢进输入框
    // 用户会看到一坨 {"X": "...", "Y": "..."}，还得自己改成 "名称: SMILES"
    const text = value.trim();
    if (text.startsWith('{') || text.startsWith('[')) {
      try {
        return smilesToText(JSON.parse(text));
      } catch {
        return value;
      }
    }
    return value;
  }
  if (typeof value !== 'object') return '';
  return Object.entries(value).map(([name, smi]) => `${name}: ${smi}`).join('\n');
}


/** 流程图描述 → 输入框文本；后端有时给 dict、有时给 JSON 文本 */
function flowToText(value) {
  if (!value) return '';
  if (typeof value === 'string') {
    const text = value.trim();
    if (!text) return '';
    try {
      return JSON.stringify(JSON.parse(text), null, 1);
    } catch {
      return text;
    }
  }
  if (typeof value !== 'object') return '';
  return JSON.stringify(value, null, 1);
}

/** 输入框文本 → 流程描述；空返回 null，JSON 坏了原样回传让后端报错 */
function textToFlow(text) {
  const raw = String(text || '').trim();
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

function setFlowHint(text, kind) {
  if (!el.flowHint) return;
  el.flowHint.textContent = text;
  el.flowHint.classList.toggle('warn', kind === 'warn');
  el.flowHint.classList.toggle('ok', kind === 'ok');
}

/** 渲染工艺流程图（和 TikZ、结构式一样按需触发） */
async function renderFlow() {
  const spec = textToFlow(el.fFlow.value);
  if (!spec) {
    setFlowHint('未设置流程图', '');
    refreshPreview();
    return;
  }
  const btn = $('#btnRenderFlow');
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = '渲染中…';
  try {
    const res = await post('/api/flow/render', { flow: spec });
    ctx.figureSvg = res.svg;
    ctx.figureUrl = res.image_url || '';
    const steps = res.spec?.steps?.length || 0;
    const figs = (res.spec?.steps || []).filter((s) => s.smiles).length;
    const parts = [`已渲染 ${steps} 个步骤${figs ? `（${figs} 个结构式）` : ''}`];
    if (res.warnings?.length) parts.push(res.warnings.join('；'));
    setFlowHint(parts.join(' · '), res.warnings?.length ? 'warn' : 'ok');
    refreshPreview();
  } catch (err) {
    ctx.figureSvg = '';
    ctx.figureUrl = '';
    setFlowHint(err.message || '流程图渲染失败', 'err');
    refreshPreview();
    toast(err.message || '流程图渲染失败', 'err', 5200);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

/** 多行文本 → {名称: SMILES}；没写名称的自动编号，空输入返回 null */
function textToSmiles(text) {
  const out = {};
  let auto = 1;
  for (const raw of String(text || '').split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    const idx = line.search(/[:：]/);
    if (idx >= 0) {
      const name = line.slice(0, idx).trim() || `结构式${auto++}`;
      out[name] = line.slice(idx + 1).trim();
    } else {
      out[`结构式${auto++}`] = line;
    }
  }
  return Object.keys(out).length ? out : null;
}

/**
 * 编译化学结构式。
 *
 * 和 TikZ 一样按需触发：RDKit 出图要几十毫秒到几百毫秒，边打字边渲染会卡。
 * 不点也没关系，保存时后端会再编译一次。
 */
async function renderChem() {
  const mapping = textToSmiles(el.fSmiles.value);
  if (!mapping) {
    ctx.figureSvg = '';
    ctx.figureUrl = '';
    setChemHint('未设置结构式', '');
    refreshPreview();
    return;
  }
  const btn = $('#btnRenderChem');
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = '渲染中…';
  try {
    const res = await post('/api/chem/render', { smiles: mapping });
    ctx.figureSvg = res.svg;
    ctx.figureUrl = res.image_url || '';
    const count = Object.keys(res.molecules || {}).length;
    if (res.warnings?.length) {
      setChemHint(`已渲染 ${count} 个，${res.warnings.length} 个无法解析`, 'warn');
      toast(res.warnings.join('；'), 'err', 6500);
    } else {
      setChemHint(`已渲染 ${count} 个结构式`, 'ok');
    }
    refreshPreview();
  } catch (err) {
    ctx.figureSvg = '';
    ctx.figureUrl = '';
    setChemHint(err.message || '渲染失败', 'err');
    refreshPreview();
    toast(err.message || '结构式渲染失败', 'err', 5200);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

function setChemHint(text, kind) {
  if (!el.chemHint) return;
  el.chemHint.textContent = text;
  el.chemHint.classList.toggle('warn', kind === 'warn');
  el.chemHint.classList.toggle('ok', kind === 'ok');
}
