import { readStore, writeStore } from './storage.js';

// 面板会话令牌放 sessionStorage，关闭标签页即失效；API Key 可经 ?key= 传入后记在 localStorage。
const PANEL_STORE = 'wb-proxy-center-panel-token';
const KEY_STORE = 'wb-proxy-center-api-key';

let panelToken = readStore(PANEL_STORE, 'session') || '';
let apiKey = (readStore(KEY_STORE) || '').trim();
let unauthorizedHandler = null;

export class ApiError extends Error {
  constructor(status, message, body) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

export function isAuthError(err) {
  return err instanceof ApiError && (err.status === 401 || err.status === 403);
}

export function setPanelToken(token) {
  panelToken = token || '';
  writeStore(PANEL_STORE, panelToken || null, 'session');
}

export function setApiKey(key) {
  apiKey = (key || '').trim();
  writeStore(KEY_STORE, apiKey || null);
}

/** 会话失效时的统一处理，由 main.js 注册。 */
export function onUnauthorized(handler) {
  unauthorizedHandler = handler;
}

// 含非 ASCII 字符的值放不进请求头，fetch 会直接抛错；这样的值不可能是网关 Key，忽略它。
function usableKey(value) {
  return value && /^[\x20-\x7e]+$/.test(value) ? value : '';
}

export function authHeaders(base = {}) {
  const headers = { ...base };
  const key = usableKey(apiKey);
  if (key) headers.Authorization = 'Bearer ' + key;
  if (panelToken) headers['X-Panel-Token'] = panelToken;
  return headers;
}

function errorMessage(status, parsed) {
  if (parsed && typeof parsed === 'object') {
    if (parsed.error && parsed.error.message) return parsed.error.message;
    if (parsed.msg) return parsed.msg;
    if (parsed.raw) return String(parsed.raw).slice(0, 200);
  }
  return 'HTTP ' + status;
}

async function request(method, url, body) {
  const init = { method, cache: 'no-store', headers: authHeaders(body === undefined ? {} : { 'Content-Type': 'application/json' }) };
  if (body !== undefined) init.body = JSON.stringify(body);
  const response = await fetch(url, init);
  const text = await response.text();
  let parsed = {};
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = { raw: text };
    }
  }
  if (response.status === 401 || response.status === 403) {
    if (unauthorizedHandler) unauthorizedHandler();
    throw new ApiError(response.status, '面板会话已失效，请重新登录', parsed);
  }
  if (!response.ok) throw new ApiError(response.status, errorMessage(response.status, parsed), parsed);
  return parsed;
}

export function getJSON(url) {
  return request('GET', url);
}

export function postJSON(url, body = {}) {
  return request('POST', url, body);
}

/** 以面板会话下载文件：普通链接带不上 X-Panel-Token 请求头，所以先取回再交给浏览器保存。 */
export async function downloadFile(url, fallbackName) {
  const response = await fetch(url, { cache: 'no-store', headers: authHeaders() });
  if (response.status === 401 || response.status === 403) {
    if (unauthorizedHandler) unauthorizedHandler();
    throw new ApiError(response.status, '面板会话已失效，请重新登录', null);
  }
  if (!response.ok) throw new ApiError(response.status, 'HTTP ' + response.status, null);
  const disposition = response.headers.get('Content-Disposition') || '';
  const match = disposition.match(/filename="?([^";]+)"?/i);
  const name = match ? match[1] : fallbackName;
  const blob = await response.blob();
  const href = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = href;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
  return name;
}

/** 登录接口不带会话令牌，单独处理。 */
export async function panelLogin(password) {
  const response = await fetch('/panel/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  });
  const data = await response.json();
  if (!response.ok) throw new ApiError(response.status, errorMessage(response.status, data), data);
  setPanelToken(data.token || '');
  return data;
}
