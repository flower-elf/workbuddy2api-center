import { html, render } from '../core/dom.js';
import { icon } from '../core/icons.js';

// 顶部居中的提示消息，错误与警告停留更久。
const MAX_VISIBLE = 4;
const KINDS = {
  success: { icon: 'Check', duration: 3600 },
  info: { icon: 'Info', duration: 3600 },
  warning: { icon: 'TriangleAlert', duration: 5200 },
  error: { icon: 'X', duration: 5200 },
};

function stack() {
  let el = document.getElementById('toasts');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toasts';
    document.body.appendChild(el);
  }
  el.className = 'toast-stack';
  return el;
}

function dismiss(node) {
  if (!node.isConnected || node.classList.contains('leaving')) return;
  node.classList.add('leaving');
  setTimeout(() => node.remove(), 200);
}

function show(kind, title, desc = '') {
  const spec = KINDS[kind];
  const box = stack();
  const node = document.createElement('div');
  node.className = 'toast toast-' + kind;
  node.setAttribute('role', kind === 'error' ? 'alert' : 'status');
  render(html`
    <div class="toast-icon">${icon(spec.icon)}</div>
    <div class="toast-text">
      <div class="toast-title">${title}</div>
      ${desc ? html`<div class="toast-desc">${desc}</div>` : ''}
    </div>
    <button class="toast-close" aria-label="关闭" @click=${() => dismiss(node)}>${icon('X')}</button>
    <div class="toast-progress" style="animation-duration:${spec.duration}ms"></div>
  `, node);
  box.appendChild(node);
  while (box.children.length > MAX_VISIBLE) box.firstElementChild.remove();
  setTimeout(() => dismiss(node), spec.duration);
}

export const toast = {
  success: (title, desc) => show('success', title, desc),
  info: (title, desc) => show('info', title, desc),
  warn: (title, desc) => show('warning', title, desc),
  error: (title, desc) => show('error', title, desc),
};

/** 操作失败的统一提示：会话失效由登录页负责说明，这里不再重复。 */
export function toastError(title, err) {
  if (err && (err.status === 401 || err.status === 403)) return;
  show('error', title, err && err.message ? err.message : String(err || ''));
}
