import { html, render, nothing } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { toastError } from './toast.js';

// 弹窗与底部抽屉。内容由函数生成，状态变化后调用 ctl.update() 重新绘制。
const openStack = [];

function overlayRoot() {
  let el = document.getElementById('overlays');
  if (!el) {
    el = document.createElement('div');
    el.id = 'overlays';
    document.body.appendChild(el);
  }
  return el;
}

const value = (v, ctl) => (typeof v === 'function' ? v(ctl) : v);

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape' || !openStack.length) return;
  const top = openStack[openStack.length - 1];
  if (top.dismissible) {
    event.preventDefault();
    top.close();
  }
});

/**
 * openDialog({ title, desc, icon, width, top, dismissible, body, foot, onClose })
 * title / desc / body / foot 可以是值或 (ctl) => 模板；返回 ctl = { close, update, element }。
 */
export function openDialog(options) {
  const { width = 512, top = false, dismissible = true, onClose } = options;
  const mask = document.createElement('div');
  mask.className = 'dialog-mask' + (top ? ' top' : '');
  let closed = false;
  const ctl = {
    element: mask,
    dismissible,
    close() {
      if (closed) return;
      closed = true;
      const i = openStack.indexOf(ctl);
      if (i >= 0) openStack.splice(i, 1);
      mask.remove();
      if (onClose) onClose();
    },
    update() {
      if (closed) return;
      const titleIcon = value(options.icon, ctl);
      const desc = value(options.desc, ctl);
      const foot = value(options.foot, ctl);
      render(html`
        <div class="dialog ${options.className || ''}" role="dialog" aria-modal="true" style="max-width:${width}px"
             @mousedown=${(e) => e.stopPropagation()}>
          ${options.title !== undefined ? html`
            <div class="dialog-head">
              <h2 class="dialog-title">${titleIcon ? icon(titleIcon) : nothing}${value(options.title, ctl)}</h2>
              ${desc ? html`<p class="dialog-desc">${desc}</p>` : nothing}
            </div>
            <button class="btn btn-ghost btn-icon btn-sm dialog-close" aria-label="关闭" @click=${() => ctl.close()}>${icon('X')}</button>
          ` : nothing}
          ${options.raw ? value(options.raw, ctl) : options.body !== undefined ? html`<div class="dialog-body">${value(options.body, ctl)}</div>` : nothing}
          ${foot ? html`<div class="dialog-foot">${foot}</div>` : nothing}
        </div>
      `, mask);
    },
  };
  mask.addEventListener('mousedown', () => { if (dismissible) ctl.close(); });
  overlayRoot().appendChild(mask);
  openStack.push(ctl);
  ctl.update();
  const first = mask.querySelector('[autofocus], input:not([type=hidden]):not([disabled]), textarea, select');
  if (first) first.focus();
  return ctl;
}

/**
 * 二次确认。onConfirm 执行期间两个按钮都不可点；它抛错时弹窗保持打开并提示原因。
 * 返回 Promise：确认并执行成功为 true，取消为 false。
 */
export function confirmDialog({ title, desc, confirmText = '确认', cancelText = '取消', danger = false, onConfirm }) {
  return new Promise((resolve) => {
    let busy = false;
    let done = false;
    const ctl = openDialog({
      title,
      desc,
      width: 440,
      dismissible: true,
      onClose: () => { if (!done) resolve(false); },
      foot: () => html`
        <button class="btn btn-ghost" ?disabled=${busy} @click=${() => ctl.close()}>${cancelText}</button>
        <button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" ?disabled=${busy} @click=${confirm}>
          ${busy ? icon('LoaderCircle', 'spin') : nothing}${confirmText}
        </button>`,
    });
    async function confirm() {
      busy = true;
      ctl.dismissible = false;
      ctl.update();
      try {
        if (onConfirm) await onConfirm();
      } catch (err) {
        busy = false;
        ctl.dismissible = true;
        ctl.update();
        toastError('操作失败', err);
        return;
      }
      done = true;
      ctl.close();
      resolve(true);
    }
  });
}

/** 底部抽屉：只读明细。返回 ctl = { close, update }。 */
export function openDrawer({ title, sub, body }) {
  const wrap = document.createElement('div');
  let closed = false;
  const ctl = {
    dismissible: true,
    close() {
      if (closed) return;
      closed = true;
      const i = openStack.indexOf(ctl);
      if (i >= 0) openStack.splice(i, 1);
      wrap.remove();
    },
    update() {
      if (closed) return;
      render(html`
        <div class="drawer-mask" @click=${() => ctl.close()}></div>
        <div class="drawer" role="dialog" aria-modal="true">
          <div class="drawer-handle"></div>
          <div class="drawer-inner">
            <div class="drawer-head">
              <div>
                <div class="strong">${value(title, ctl)}</div>
                ${sub ? html`<div class="text-xs muted">${value(sub, ctl)}</div>` : nothing}
              </div>
              <button class="btn btn-ghost btn-icon btn-sm" aria-label="关闭" @click=${() => ctl.close()}>${icon('X')}</button>
            </div>
            <div class="drawer-body scroll-slim">${value(body, ctl)}</div>
          </div>
        </div>
      `, wrap);
    },
  };
  overlayRoot().appendChild(wrap);
  openStack.push(ctl);
  ctl.update();
  return ctl;
}
