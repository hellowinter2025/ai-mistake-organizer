// 关闭本地服务：只在用户明确确认后调用 /api/shutdown。
// 服务是 127.0.0.1 上的单进程，停掉后页面就再也连不上，所以按钮必须二次确认。

import { post } from './api.js';
import { $, toast, setBusy } from './ui.js';

let closing = false;

function byeScreen(scheduled) {
  document.title = '服务已关闭';
  const wrap = document.createElement('div');
  wrap.className = 'bye-screen';

  const card = document.createElement('div');
  card.className = 'bye-card';

  const icon = document.createElement('div');
  icon.className = 'bye-icon';
  icon.textContent = '⏻';

  const title = document.createElement('h1');
  title.textContent = scheduled ? '服务已关闭' : '未能自动关闭';

  const desc = document.createElement('p');
  desc.className = 'bye-desc';
  desc.textContent = scheduled
    ? '这个页面可以关掉了。下次使用时双击项目文件夹里的「启动.vbs」，不会弹出命令行窗口。'
    : '当前实例不是由启动器拉起的（例如直接用命令行运行），请在那个窗口按 Ctrl+C 结束。';

  card.append(icon, title, desc);

  if (scheduled) {
    const hint = document.createElement('p');
    hint.className = 'bye-hint';
    hint.textContent = '要继续整理错题：双击「启动.vbs」→ 浏览器会自动打开。';
    card.appendChild(hint);
  }

  wrap.appendChild(card);
  document.body.textContent = '';
  document.body.appendChild(wrap);
}

export async function requestShutdown() {
  if (closing) return;
  const ok = window.confirm(
    '关闭本地服务后，网页会立刻断开，需要重新双击「启动.vbs」才能继续使用。\n\n确定关闭？',
  );
  if (!ok) return;
  closing = true;
  try {
    const res = await post('/api/shutdown');
    const scheduled = Boolean(res && res.scheduled);
    if (scheduled) {
      byeScreen(true);
    } else {
      closing = false;
      toast('当前实例不是由启动器拉起的，请在命令行窗口按 Ctrl+C 结束', 'err', 6000);
    }
  } catch (err) {
    // 服务在响应发出前就退出了，fetch 会直接断连 —— 这正是预期结果
    byeScreen(true);
  }
}

export function initPower() {
  const btn = $('#btnPower');
  if (btn) btn.addEventListener('click', requestShutdown);
  const alt = $('#settingsShutdown');
  if (alt) {
    alt.addEventListener('click', async () => {
      setBusy(alt, true, '关闭中…');
      await requestShutdown();
      setBusy(alt, false);
    });
  }
}
