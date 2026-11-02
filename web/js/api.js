// 统一请求封装：自动带 CSRF 令牌，统一错误信息
import { state } from './state.js';

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

let tokenPromise = null;

/**
 * 重新拉取会话令牌。
 * 服务重启后旧页面手里的是失效令牌，所有写请求都会 403；
 * 拿到新令牌重试一次即可自愈，用户不必手动刷新。
 */
export function refreshToken() {
  if (!tokenPromise) {
    tokenPromise = fetch('/api/bootstrap', { credentials: 'same-origin', cache: 'no-store' })
      .then((res) => {
        if (!res.ok) throw new ApiError('无法获取访问令牌', res.status);
        return res.json();
      })
      .then((data) => {
        state.token = data.token || '';
        return state.token;
      })
      .finally(() => { tokenPromise = null; });
  }
  return tokenPromise;
}

async function readError(res) {
  let message = `${res.status} ${res.statusText}`;
  let code = '';
  try {
    const data = await res.json();
    message = data.detail || data.error || message;
    code = data.code || '';
  } catch { /* 非 JSON 错误体 */ }
  return { message, code };
}

export async function api(path, options = {}) {
  const { method = 'GET', body, headers = {}, raw = false, signal } = options;

  const send = async (retried) => {
    const h = { ...headers };
    const opts = { method, headers: h, credentials: 'same-origin', signal };
    if (method !== 'GET' && method !== 'HEAD' && state.token) h['X-App-Token'] = state.token;
    if (body instanceof FormData) {
      opts.body = body;
    } else if (body !== undefined) {
      h['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }

    let res;
    try {
      res = await fetch(path, opts);
    } catch (err) {
      if (err.name === 'AbortError') throw err;
      throw new ApiError('无法连接本地服务，请确认程序仍在运行', 0);
    }
    if (res.ok) return res;

    const { message, code } = await readError(res);
    if (res.status === 403 && code === 'token' && !retried) {
      await refreshToken();
      return send(true);
    }
    throw new ApiError(message, res.status);
  };

  const res = await send(false);
  if (raw) return res;
  const ct = res.headers.get('content-type') || '';
  if (ct.includes('application/json')) return res.json();
  return res.text();
}

export const get = (path, signal) => api(path, { signal });
export const post = (path, body) => api(path, { method: 'POST', body });
export const patch = (path, body) => api(path, { method: 'PATCH', body });
export const put = (path, body) => api(path, { method: 'PUT', body });
export const del = (path) => api(path, { method: 'DELETE' });

function xhrPost(fd, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/images');
    if (state.token) xhr.setRequestHeader('X-App-Token', state.token);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(Math.round((e.loaded / e.total) * 100));
    };
    xhr.onload = () => {
      let data = {};
      try { data = JSON.parse(xhr.responseText || '{}'); } catch { data = {}; }
      resolve({ status: xhr.status, data });
    };
    xhr.onerror = () => reject(new ApiError('上传中断，请确认服务是否仍在运行', 0));
    xhr.ontimeout = () => reject(new ApiError('上传超时', 0));
    xhr.send(fd);
  });
}

export async function uploadImages(files, onProgress) {
  const build = () => {
    const fd = new FormData();
    for (const f of files) fd.append('files', f);
    return fd;
  };

  let res = await xhrPost(build(), onProgress);
  if (res.status === 403 && res.data?.code === 'token') {
    await refreshToken();
    res = await xhrPost(build(), onProgress);
  }

  if (res.status >= 200 && res.status < 300) return res.data;
  if (!res.status) throw new ApiError('上传失败，请检查服务是否运行', 0);
  throw new ApiError(res.data?.detail || res.data?.error || `上传失败（${res.status}）`, res.status);
}

export function downloadUrl(path) {
  return path;
}

/** 导出并直接写进本机「下载」文件夹：不经过浏览器下载，避免安全扫描拦文件。
 *  返回 { path, name, dir, opened }。 */
export async function saveToDownloads(path) {
  const res = await api(path, { method: 'POST', raw: true });
  try {
    return await res.json();
  } catch {
    throw new ApiError('导出结果异常，请重试', res.status);
  }
}

export async function downloadFile(path, fallbackName) {
  const res = await fetch(path, { credentials: 'same-origin' });
  if (!res.ok) {
    let detail = `导出失败（${res.status}）`;
    try { const j = await res.json(); detail = j.detail || detail; } catch { /* ignore */ }
    throw new ApiError(detail, res.status);
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = fallbackName || 'download';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
  return blob;
}
