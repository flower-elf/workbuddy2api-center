import { html, render, nothing } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { panelLogin } from '../core/api.js';

// 面板登录页。onSuccess(loginResponse) 由 main.js 提供。
export function showLogin(host, { message = '', defaultPassword = false, onSuccess, autoPassword = '' }) {
  let error = message;
  let busy = false;

  async function submit(event) {
    if (event) event.preventDefault();
    // 浏览器自动填充不一定触发 input 事件，提交时直接读输入框
    const input = host.querySelector('#loginPassword');
    const password = input.value;
    if (!password) {
      error = '请输入面板密码';
      draw();
      return;
    }
    busy = true;
    error = '';
    draw();
    try {
      const data = await panelLogin(password);
      input.value = '';
      onSuccess(data);
    } catch (err) {
      busy = false;
      error = err.status === 429 ? '尝试次数过多，请一分钟后再试' : err.status === 401 ? '密码错误' : err.message;
      draw();
    }
  }

  function draw() {
    render(html`
      <div class="login-screen">
        <div class="login-box">
          <div class="login-mark">${icon('Bot')}</div>
          <h1 class="page-title">WorkBuddy 网关</h1>
          <p class="page-desc">WorkBuddy / CodeBuddy 账号池 · OpenAI 兼容接口网关</p>
          <form class="card login-card" @submit=${submit}>
            <label class="field">
              <span class="field-label">面板密码</span>
              <input id="loginPassword" class="input" type="password" autocomplete="current-password" placeholder="请输入面板密码" ?disabled=${busy} autofocus>
            </label>
            ${error ? html`<p class="field-error" role="alert">${error}</p>` : nothing}
            <button class="btn btn-primary" type="submit" style="width:100%" ?disabled=${busy}>
              ${busy ? icon('LoaderCircle', 'spin') : icon('LogIn')}进入面板
            </button>
            ${defaultPassword ? html`<p class="field-hint tone-warning">当前仍在使用默认密码 admin，登录后请到「设置 → 面板密码」修改。</p>` : nothing}
          </form>
          <p class="login-foot">面板密码与 API Key 相互独立：密码用于打开本页面，API Key 用于客户端调用。</p>
        </div>
      </div>`, host);
  }

  draw();
  const input = host.querySelector('#loginPassword');
  input.focus();
  if (autoPassword) {
    input.value = autoPassword;
    submit();
  }
}
