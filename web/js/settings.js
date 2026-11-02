// 设置面板
import { get, post, patch, del } from './api.js';
import { state, emit } from './state.js';
import { $, toast, show, hide, setBusy, fmtDate } from './ui.js';

let modelsCache = {};

export function initSettings() {
  $('#btnSettings').addEventListener('click', openSettings);
  $('#settingsClose').addEventListener('click', closeSettings);
  $('#settingsMask').addEventListener('click', (e) => { if (e.target.id === 'settingsMask') closeSettings(); });
  $('#sConfig').addEventListener('change', onConfigChange);
  $('#btnFetchModels').addEventListener('click', fetchModels);
  $('#btnTestConfig').addEventListener('click', testConfig);
  $('#settingsSave').addEventListener('click', saveSettings);
  $('#btnNewConfig').addEventListener('click', () => fillConfigEditor(null));
  $('#btnSaveConfig').addEventListener('click', saveConfig);
  $('#btnDeleteConfig').addEventListener('click', deleteConfig);
  $('#btnImportHint').addEventListener('click', () => {
    window.alert(
      '三种填法，任选其一：\n\n' +
      '1. 直接在「添加 / 编辑服务」里填名称、接口地址、API Key，保存即可；\n' +
      '2. 拉取模型：填好地址与 Key 后点「从接口拉取模型」；\n' +
      '3. 批量导入（在项目目录执行）：\n' +
      '   .venv\\Scripts\\python.exe tools\\import_ai_config.py --from-json 导出.json\n' +
      '   也支持 --mysql 用户:密码@主机/库名 或 --config-php 某个 PHP 配置文件。\n\n' +
      'API Key 只会写入本机 data/config.json，浏览器端只拿到掩码。'
    );
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('#settingsMask').classList.contains('hidden')) closeSettings();
  });
}

function openSettings() {
  const cfg = state.settings?.ai || {};
  const preferred = state.active?.config_id ?? cfg.active_config_id ?? '';
  const select = refreshConfigOptions(preferred);
  $('#sConcurrency').value = String(cfg.concurrency ?? 2);
  $('#sSubject').value = state.settings?.general?.subject || '数学';
  $('#sPreferJson').checked = cfg.prefer_json_mode !== false;
  // 切题算法的设置项已下线（自动识别取消，改为手动框选），这里不再回填
  $('#testResult').textContent = '';
  fillModels(select.value, state.active?.model);
  fillConfigEditor(currentConfig());
  loadLogs();
  show($('#settingsMask'));
}

/** 重建服务配置下拉框，返回 select 元素（方便链式取值）。 */
function refreshConfigOptions(preferred) {
  const select = $('#sConfig');
  select.textContent = '';
  if (!(state.settings?.configs || []).length) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = '还没有配置，点下面「添加 / 编辑服务」新建一条';
    select.appendChild(opt);
    select.value = '';
    return select;
  }
  for (const item of state.settings.configs) {
    const opt = document.createElement('option');
    opt.value = String(item.id);
    opt.textContent = `${item.name}（${String(item.base_url || '').replace(/^https?:\/\//, '')}）`;
    select.appendChild(opt);
  }
  select.value = String(preferred ?? '');
  if (!select.value) select.value = String(state.settings.configs[0].id);
  return select;
}

/**
 * 把一条配置回填到编辑表单；传 null 表示"新建"，表单清空。
 * key 永远不回填（后端只给掩码），留空即"不修改"。
 */
function fillConfigEditor(cfg) {
  const editing = !!cfg;
  $('#sCfgName').value = cfg?.name || '';
  $('#sCfgBase').value = cfg?.base_url || '';
  $('#sCfgKey').value = '';
  $('#sCfgKey').placeholder = cfg?.has_key
    ? `已保存 ${cfg.key_hint || ''}，留空＝不修改`
    : '粘贴 API Key';
  $('#sCfgModels').value = (cfg?.models || []).join(', ');
  $('#cfgEditor').dataset.editing = editing ? String(cfg.id) : '';
  $('#cfgHint').textContent = editing
    ? `正在修改「${cfg.name || '#' + cfg.id}」${cfg.has_key ? '' : '（还没有 Key）'}；改完点「保存这条配置」立即生效。想新增请先点「新建一条」。`
    : '新建：填名称、接口地址、Key，保存后会自动成为当前服务。';
  $('#btnDeleteConfig').disabled = !editing;
}

async function saveConfig() {
  const btn = $('#btnSaveConfig');
  const editing = $('#cfgEditor').dataset.editing || '';
  const payload = {
    name: $('#sCfgName').value.trim(),
    base_url: $('#sCfgBase').value.trim(),
    models: $('#sCfgModels').value.split(/[\s,;，、]+/).filter(Boolean),
  };
  const key = $('#sCfgKey').value.trim();
  if (key) payload.api_key = key;          // 留空＝不带该字段＝沿用旧 key
  if (!payload.name || !payload.base_url) {
    toast('名称和接口地址都要填', 'err');
    return;
  }
  setBusy(btn, true, '保存中…');
  try {
    const res = editing
      ? await patch(`/api/settings/configs/${editing}`, payload)
      : await post('/api/settings/configs', payload);
    state.settings = res.settings;
    const savedId = res.config?.id ?? editing;
    modelsCache[savedId] = res.config?.models || [];
    // 当前服务/模型以服务端为准（新建第一条时后端会自动接上，前端别再自己猜）
    try {
      const fresh = await get('/api/state');
      state.active = fresh.active;
    } catch (e) { /* 取不到就沿用本地，至少界面不崩 */ }
    // 下拉框停在「刚保存的这一条」上：停到 active 会让人以为在编辑当前服务，
    // 接着改字段就会把当前服务覆盖掉（真踩过）
    refreshConfigOptions(savedId);
    // 只在"编辑的正是当前服务"时沿用当前模型，否则会把别的服务的模型名带进来
    const sameService = Number(state.active?.config_id) === Number(savedId);
    fillModels(savedId, sameService ? state.active?.model : null);
    if (sameService) {
      state.active.model = $('#sModel').value || null;
    }
    fillConfigEditor(currentConfig());
    updateAiChip();
    emit('settings:changed');
    toast(editing ? '配置已更新' : '配置已添加', 'ok');
  } catch (err) {
    toast(err.message || '保存失败', 'err');
  } finally {
    setBusy(btn, false);
  }
}

async function deleteConfig() {
  // 删的必须是"下拉框里当前选中的这条"：表单上的 dataset.editing 可能还停在
  // 上一次编辑的对象上（曾经的 bug 就是这样把别人的配置删了）
  const cfg = currentConfig();
  const id = cfg?.id;
  if (!id) return;
  if (!window.confirm(`删除服务「${cfg?.name || id}」？题库里的题目不受影响，只是这条接口配置没了。`)) return;
  const btn = $('#btnDeleteConfig');
  setBusy(btn, true, '删除中…');
  try {
    const res = await del(`/api/settings/configs/${id}`);
    state.settings = res.settings;
    try {
      const fresh = await get('/api/state');
      state.active = fresh.active;
    } catch (e) {
      const first = state.settings?.configs?.[0];
      state.active = first
        ? { config_id: Number(first.id), model: first.models?.[0] || null }
        : { config_id: null, model: null };
    }
    refreshConfigOptions(state.active?.config_id ?? '');
    fillModels($('#sConfig').value, state.active?.model);
    fillConfigEditor(currentConfig());
    updateAiChip();
    emit('settings:changed');
    toast('配置已删除', 'ok');
  } catch (err) {
    toast(err.message || '删除失败', 'err');
  } finally {
    setBusy(btn, false);
  }
}

function closeSettings() { hide($('#settingsMask')); }

function currentConfig() {
  const id = Number($('#sConfig').value);
  return (state.settings?.configs || []).find((c) => Number(c.id) === id) || null;
}

function fillModels(configId, selected) {
  const modelSelect = $('#sModel');
  modelSelect.textContent = '';
  const cfg = (state.settings?.configs || []).find((c) => String(c.id) === String(configId));
  const models = modelsCache[configId] || cfg?.models || [];
  if (!models.length) {
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = '请先「从接口拉取模型」';
    modelSelect.appendChild(opt);
    return;
  }
  for (const m of models) {
    const opt = document.createElement('option');
    opt.value = m;
    opt.textContent = m;
    modelSelect.appendChild(opt);
  }
  if (selected && !models.includes(selected)) {
    const opt = document.createElement('option');
    opt.value = selected;
    opt.textContent = selected;
    modelSelect.appendChild(opt);
  }
  modelSelect.value = selected || models[0];
}

function onConfigChange() {
  const id = $('#sConfig').value;
  fillModels(id, null);
  fillConfigEditor(currentConfig());
  $('#testResult').textContent = '';
}

async function fetchModels() {
  const id = Number($('#sConfig').value);
  if (!id) return;
  const btn = $('#btnFetchModels');
  setBusy(btn, true, '拉取中…');
  try {
    const data = await get(`/api/settings/models?config_id=${id}`);
    modelsCache[id] = data.models;
    await post('/api/settings/models', { config_id: id, models: data.models });
    const cfg = (state.settings.configs || []).find((c) => Number(c.id) === id);
    if (cfg) cfg.models = data.models;
    fillModels(id, $('#sModel').value);
    toast(`获取到 ${data.models.length} 个模型`, 'ok');
  } catch (err) {
    toast(err.message || '拉取模型失败', 'err');
  } finally {
    setBusy(btn, false);
  }
}

async function testConfig() {
  const id = Number($('#sConfig').value);
  const model = $('#sModel').value;
  if (!id || !model) { toast('请先选择配置与模型', 'err'); return; }
  const btn = $('#btnTestConfig');
  setBusy(btn, true, '测试中…');
  try {
    const res = await post('/api/settings/test', { config_id: id, model });
    $('#testResult').textContent = `可用 · ${res.latency_ms}ms · ${res.reply || 'ok'}`;
    toast('连接成功', 'ok');
  } catch (err) {
    $('#testResult').textContent = err.message;
    toast(err.message || '测试失败', 'err');
  } finally {
    setBusy(btn, false);
  }
}

async function saveSettings() {
  const btn = $('#settingsSave');
  setBusy(btn, true, '保存中…');
  try {
    const payload = {
      general: {
        subject: $('#sSubject').value.trim() || '数学',
      },
      ai: {
        active_config_id: Number($('#sConfig').value) || null,
        active_model: $('#sModel').value || null,
        concurrency: Number($('#sConcurrency').value) || 2,
        prefer_json_mode: $('#sPreferJson').checked,
      },
      // 自动切题已下线：把已有的切题配置原样保留，不覆盖、不丢
      segment: state.settings?.segment ?? {},
    };
    const res = await patch('/api/settings', payload);
    state.settings = res.settings;
    state.active = { config_id: payload.ai.active_config_id, model: payload.ai.active_model };
    updateAiChip();
    toast('设置已保存', 'ok');
    closeSettings();
    emit('settings:changed');
  } catch (err) {
    toast(err.message || '保存失败', 'err');
  } finally {
    setBusy(btn, false);
  }
}

async function loadLogs() {
  const list = $('#logList');
  const summary = $('#logSummary');
  list.textContent = '';
  try {
    const data = await get('/api/ai/logs?limit=20');
    const week = data.week || {};
    const all = data.all || {};
    const fmt = (n) => (n >= 10000 ? `${(n / 10000).toFixed(1)}万` : String(n || 0));
    summary.textContent = week.total
      ? `近 7 天调用 ${week.total} 次，成功 ${week.ok} 次（${Math.round((week.success_rate || 0) * 100)}%），` +
        `平均 ${week.avg_ms}ms；消耗 token：输入 ${fmt(week.tokens_in)} / 输出 ${fmt(week.tokens_out)}` +
        `（累计 ${all.total} 次，输入 ${fmt(all.tokens_in)} / 输出 ${fmt(all.tokens_out)}）`
      : '近 7 天还没有调用记录';
    for (const log of data.items) {
      const row = document.createElement('div');
      row.className = `log-row${log.ok ? '' : ' bad'}`;
      const t = document.createElement('span');
      t.textContent = (log.created_at || '').slice(5, 16);
      const m = document.createElement('span');
      m.textContent = `${log.model || '-'} · ${log.parse_strategy || log.mode || '-'}${log.ok ? '' : ' · ' + (log.error || '失败').slice(0, 40)}`;
      const ms = document.createElement('span');
      ms.className = 'm';
      const parts = [];
      if (log.latency_ms) parts.push(`${log.latency_ms}ms`);
      if (log.tokens_in || log.tokens_out) parts.push(`↑${log.tokens_in || 0} ↓${log.tokens_out || 0}`);
      ms.textContent = parts.join(' · ');
      row.append(t, m, ms);
      list.appendChild(row);
    }
  } catch (err) {
    summary.textContent = err.message;
  }
}

export function updateAiChip() {
  const chip = $('#aiStatus');
  const active = state.active;
  if (!active || !active.model) {
    chip.textContent = '未选择模型';
    chip.classList.add('bad');
    chip.title = '点击右上角齿轮选择 AI 服务与模型';
    return;
  }
  const cfg = (state.settings?.configs || []).find((c) => Number(c.id) === Number(active.config_id));
  chip.textContent = `${cfg?.name || 'AI'} · ${active.model}`;
  chip.classList.remove('bad');
  chip.title = `当前使用 ${cfg?.base_url || ''} / ${active.model}`;
}
