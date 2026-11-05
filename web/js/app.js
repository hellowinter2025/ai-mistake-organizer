// 应用入口：引导、路由、模块装配
import { get, post } from './api.js';
import { state, on, emit } from './state.js';
import { $, $$, toast } from './ui.js';
import { initEditor } from './editor.js';
import { initRecognize } from './recognize.js';
import { initBank, loadBank, updateCartBadge } from './bank.js';
import { initPaper, refreshPapers } from './paper.js';
import { initSettings, updateAiChip } from './settings.js';
import { initPower } from './power.js';
import { resetMathStats, mathStats } from './math.js';

async function boot() {
  try {
    const data = await get('/api/bootstrap');
    state.token = data.token;
    state.version = data.version;
    state.settings = data.settings;
    state.active = data.active;
    state.stats = data.stats;
  } catch (err) {
    document.body.innerHTML = `<div style="padding:40px;font-family:system-ui">
      <h2>无法连接本地服务</h2><p style="color:#666">${err.message || err}</p>
      <p style="color:#999;font-size:13px">请确认 start.py 仍在运行，然后刷新页面。</p></div>`;
    return;
  }

  initEditor();
  initRecognize();
  initBank();
  initPaper();
  initSettings();
  initPower();
  updateAiChip();
  updateCartBadge();

  bindTabs();
  bindGlobal();
  refreshStats();
  resetMathStats();

  // 刷新后停在原来那一页（#bank / #paper），免得每次都要重新点
  const initial = location.hash.replace('#', '');
  if (['recognize', 'bank', 'paper'].includes(initial)) switchView(initial, { remember: false });
}

function bindTabs() {
  for (const tab of $$('.tab')) {
    tab.addEventListener('click', () => switchView(tab.dataset.view));
  }
}

export function switchView(name, { remember = true } = {}) {
  for (const tab of $$('.tab')) tab.classList.toggle('active', tab.dataset.view === name);
  for (const view of $$('.view')) view.classList.toggle('active', view.id === `view-${name}`);
  if (remember) history.replaceState(null, '', `#${name}`);
  if (name === 'bank') loadBank();
  if (name === 'paper') refreshPapers();
  if (name === 'recognize') window.dispatchEvent(new Event('resize'));
}

function bindGlobal() {
  on('stats:changed', refreshStats);
  window.addEventListener('error', (e) => {
    console.error(e.error || e.message);
  });
  // 公式渲染健康检查：控制台可查 window.mathHealth()
  window.mathHealth = () => ({
    rendered: mathStats.rendered,
    errors: mathStats.errors.slice(-10),
  });
}

async function refreshStats() {
  try {
    const data = await get('/api/state');
    state.stats = data.stats;
    if (data.active) state.active = data.active;
    $('#bankCount').textContent = String(data.stats.questions || 0);
    updateAiChip();
  } catch { /* 静默失败 */ }
}

boot();
