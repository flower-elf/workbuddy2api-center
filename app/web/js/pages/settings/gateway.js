// 设置 → 网关行为：保留积分、每日限额、自动切换、打卡通道、本地网络工具、Messages 翻译路径与测试使用的模型；控件先改本地草稿，由卡片上的「保存」统一提交。
import { html, live, nothing, repeat } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { getJSON } from '../../core/api.js';
import { toast, toastError } from '../../ui/toast.js';
import {
  messagesFormatOptions,
  messagesFormatValue,
  parseNonNegativeInteger,
  testModelOptions,
  testModelSelected,
  testModelStateText,
  validateTestModel,
} from './logic.js';

export function createGatewayTab(page) {
  const form = {
    reserve: '0',
    dailyLimit: '0',
    smartRouting: true,
    sessionAffinity: true,
    autoSwitch: false,
    dailyWeb: true,
    localWeb: false,
    messagesFormat: 'openai-completions',
    testModel: { intl: '', cn: '' },
  };
  const errors = { reserve: '', dailyLimit: '', testModel: { intl: '', cn: '' } };
  // 服务端最后一次给出的设置：撤销回到这里，也是「有未保存的改动」的对照。
  let baseline = null;
  // 表头的未保存标记只在翻转时重绘，输入过程中不打断光标位置。
  let dirtyShown = false;
  let saving = false;
  let catalogs = { intl: [], cn: [] };
  let catalogError = '';

  function stateOf(view) {
    return {
      reserve: String(Number(view.reserve_credits) || 0),
      dailyLimit: String(Number(view.daily_token_limit) || 0),
      smartRouting: view.smart_routing !== false,
      sessionAffinity: view.session_affinity !== false,
      autoSwitch: view.auto_switch_product === true,
      dailyWeb: view.daily_chat_web !== false,
      localWeb: view.local_web_tools === true,
      messagesFormat: messagesFormatValue(view.messages_format, view.messages_format_default),
      testModel: {
        intl: testModelSelected(view.test_model_intl),
        cn: testModelSelected(view.test_model_cn),
      },
    };
  }

  function applyState(state) {
    form.reserve = state.reserve;
    form.dailyLimit = state.dailyLimit;
    form.smartRouting = state.smartRouting;
    form.sessionAffinity = state.sessionAffinity;
    form.autoSwitch = state.autoSwitch;
    form.dailyWeb = state.dailyWeb;
    form.localWeb = state.localWeb;
    form.messagesFormat = state.messagesFormat;
    form.testModel.intl = state.testModel.intl;
    form.testModel.cn = state.testModel.cn;
    errors.reserve = '';
    errors.dailyLimit = '';
    errors.testModel.intl = '';
    errors.testModel.cn = '';
    dirtyShown = false;
  }

  /** 用服务端的值填满控件，换标签或重新进入时调用。 */
  function seed(view) {
    baseline = stateOf(view);
    applyState(baseline);
  }

  function isDirty() {
    if (!baseline) return false;
    return String(form.reserve).trim() !== baseline.reserve
      || String(form.dailyLimit).trim() !== baseline.dailyLimit
      || form.smartRouting !== baseline.smartRouting
      || form.sessionAffinity !== baseline.sessionAffinity
      || form.autoSwitch !== baseline.autoSwitch
      || form.dailyWeb !== baseline.dailyWeb
      || form.localWeb !== baseline.localWeb
      || form.messagesFormat !== baseline.messagesFormat
      || form.testModel.intl !== baseline.testModel.intl
      || form.testModel.cn !== baseline.testModel.cn;
  }

  function structChanged() {
    if (isDirty() === dirtyShown) return;
    dirtyShown = isDirty();
    page.draw();
  }

  /** 撤销：控件全部退回服务端最后一次给出的设置。 */
  function revert() {
    if (!baseline) return;
    applyState(baseline);
    page.draw();
    toast.info('已撤销未保存的改动');
  }

  async function loadCatalogs() {
    try {
      const [intl, cn] = await Promise.all([
        getJSON('/v1/models?realm=intl'),
        getJSON('/v1/models?realm=cn'),
      ]);
      const ids = (payload) => ((payload && payload.data) || []).map((m) => m.id).filter(Boolean);
      catalogs = { intl: ids(intl), cn: ids(cn) };
      catalogError = '';
    } catch (err) {
      // 目录读取失败不影响其它设置项；下拉退回只有「默认」一项并说明原因。
      catalogError = err.message;
    }
  }

  /** 校验并提交整张卡片；返回是否保存成功。 */
  async function saveAll() {
    const reserve = parseNonNegativeInteger(form.reserve, { label: '保留积分' });
    const dailyLimit = parseNonNegativeInteger(form.dailyLimit, { label: '每日 Token 限额' });
    errors.reserve = reserve.ok ? '' : reserve.error;
    errors.dailyLimit = dailyLimit.ok ? '' : dailyLimit.error;
    for (const realm of ['intl', 'cn']) {
      form.testModel[realm] = String(form.testModel[realm] || '').trim();
      errors.testModel[realm] = validateTestModel(form.testModel[realm]);
    }
    if (errors.reserve || errors.dailyLimit || errors.testModel.intl || errors.testModel.cn) {
      page.draw();
      return false;
    }
    saving = true;
    page.draw();
    try {
      const view = await page.save({
        reserve_credits: reserve.value,
        daily_token_limit: dailyLimit.value,
        smart_routing: form.smartRouting,
        session_affinity: form.sessionAffinity,
        auto_switch_product: form.autoSwitch,
        daily_chat_web: form.dailyWeb,
        local_web_tools: form.localWeb,
        messages_format: form.messagesFormat,
        test_model_intl: form.testModel.intl,
        test_model_cn: form.testModel.cn,
      });
      baseline = stateOf(view);
      applyState(baseline);
      toast.success('网关行为设置已保存');
      return true;
    } catch (err) {
      toastError('保存网关行为设置失败', err);
      return false;
    } finally {
      saving = false;
      page.draw();
    }
  }

  function switchRow({ name, desc, checked, onChange }) {
    return html`
      <div class="setting">
        <div class="setting-main">
          <div class="setting-name">${name}</div>
          <div class="setting-desc">${desc}</div>
        </div>
        <div class="setting-control">
          <label class="switch">
            <input type="checkbox" .checked=${live(checked)} ?disabled=${saving} aria-label=${name}
                   @change=${(e) => onChange(e.target.checked)}>
            <span></span>
          </label>
        </div>
      </div>`;
  }

  function numberRow({ name, desc, field, suffix, step = '1' }) {
    return html`
      <div class="setting">
        <div class="setting-main">
          <div class="setting-name">${name}</div>
          <div class="setting-desc">${desc}</div>
          ${errors[field] ? html`<div class="field-error">${errors[field]}</div>` : nothing}
        </div>
        <div class="setting-control">
          <input class="input input-num" type="number" min="0" step=${step} .value=${live(form[field])}
                 ?disabled=${saving}
                 @input=${(e) => { form[field] = e.target.value; errors[field] = ''; structChanged(); }}>
          <span class="setting-unit">${suffix}</span>
        </div>
      </div>`;
  }

  function testModelRow() {
    const view = page.state.view || {};
    const cell = (realm) => {
      const label = realm === 'cn' ? '国内版' : '国际版';
      const fallback = realm === 'cn' ? view.test_model_cn_default : view.test_model_intl_default;
      const stateText = testModelStateText(form.testModel[realm], fallback);
      const options = testModelOptions(catalogs[realm], form.testModel[realm]);
      return html`
        <div class="test-model-cell">
          <div class="test-model-head">
            <span class="test-model-realm">${label}</span>
            <span class="text-2xs ${stateText.tone === 'info' ? 'tone-info' : 'muted'}">${stateText.text}</span>
          </div>
          <select class="select input-sm select-wide" .value=${live(form.testModel[realm])} ?disabled=${saving}
                  @change=${(e) => { form.testModel[realm] = e.target.value; errors.testModel[realm] = ''; structChanged(); }}>
            ${repeat(options, (opt) => opt.value, (opt) => html`<option value=${opt.value} ?selected=${opt.selected}>${opt.label}</option>`)}
          </select>
          ${errors.testModel[realm] ? html`<div class="field-error">${errors.testModel[realm]}</div>` : nothing}
        </div>`;
    };
    return html`
      <div class="setting span-2">
        <div class="setting-main">
          <div class="setting-name">测试使用的模型</div>
          <div class="setting-desc">
            账号行的「测试」按钮会发送一条内容为 hi 的真实对话，这里分别选择两个出口使用哪个模型；「默认」表示不指定，由网关按该出口的默认模型请求。
          </div>
        </div>
        <div class="setting-control test-model-row">
          ${cell('intl')}
          ${cell('cn')}
        </div>
      </div>`;
  }

  return {
    seed,
    render() {
      const view = page.state.view || {};
      const dirty = isDirty();
      const messagesOptions = messagesFormatOptions(form.messagesFormat, view.messages_format_default);
      return html`
        <div class="card">
          <div class="card-head">
            <div>
              <h2 class="card-title">${icon('Sliders')}网关行为</h2>
              <div class="card-sub">
                改动后点「保存」提交，点「撤销」回到上一次保存的设置。
                <span class="text-2xs ${dirty ? 'tone-warning' : 'muted'}">${dirty ? '(有未保存的改动)' : ''}</span>
              </div>
            </div>
            <div class="row-wrap">
              <button class="btn btn-outline btn-sm" ?disabled=${saving || !dirty} @click=${revert}>
                ${icon('Undo2')}撤销
              </button>
              <button class="btn btn-primary btn-sm" ?disabled=${saving || !dirty} @click=${saveAll}>
                ${saving ? icon('LoaderCircle', 'spin') : icon('Save')}保存
              </button>
            </div>
          </div>
          <div class="setting-list cols-2">
            ${numberRow({
              name: '保留积分', field: 'reserve', suffix: '积分',
              desc: '账号余额低于或等于该值时暂停接单，填写 0 表示关闭。与每日 Token 限额各自独立，任一命中都会暂停接单。',
            })}
            ${numberRow({
              name: '每日 Token 限额', field: 'dailyLimit', suffix: 'token', step: '1000',
              desc: '账号当日消耗的 token 达到该值后暂停接单，请求自动切到其他账号，本地时间 0 点后恢复。填写 0 表示不限额。',
            })}
            ${switchRow({
              name: '会话粘性',
              desc: '同一段对话固定使用同一个账号，复用上游的前缀缓存；账号不能接单时才换号。关闭后每一轮请求都重新选择账号。',
              checked: form.sessionAffinity,
              onChange: (value) => { form.sessionAffinity = value; structChanged(); },
            })}
            ${switchRow({
              name: '智能分配',
              desc: '关闭时按轮询顺序分配新对话；打开时优先把快要过期的积分用掉。已在服务的对话不受影响。',
              checked: form.smartRouting,
              onChange: (value) => { form.smartRouting = value; structChanged(); },
            })}
            ${switchRow({
              name: '429 自动切换出站身份',
              desc: '某账号在某模型上收到上游 429 限流时，自动在 WB / VSC / CLI 之间换身份重试，同一账号与模型 60 秒内最多切换 4 次。切换后的身份会保存，重启后继续沿用。',
              checked: form.autoSwitch,
              onChange: (value) => { form.autoSwitch = value; structChanged(); },
            })}
            ${switchRow({
              name: '国际版每日活跃打卡网页通道',
              desc: '国际版账号每日打卡时，除桌面端身份的轻量对话外，再走一次网页通道的会话。只有网页端对话计入官方的每日活跃奖励，开启后会消耗账号少量积分。',
              checked: form.dailyWeb,
              onChange: (value) => { form.dailyWeb = value; structChanged(); },
            })}
            ${switchRow({
              name: '本地网络工具（web_search / web_fetch）',
              desc: '客户端声明的 web_search / web_fetch 服务端工具在本网关代为执行：搜索走 DuckDuckGo，抓取页面时读取模型给出的 URL。开启后网关会主动出网，每轮多请求一次上游。',
              checked: form.localWeb,
              onChange: (value) => { form.localWeb = value; structChanged(); },
            })}
            <div class="setting">
              <div class="setting-main">
                <div class="setting-name">Messages 接口翻译</div>
                <div class="setting-desc">
                  /v1/messages 把 Anthropic 请求翻译成哪种上游格式。默认使用旧版；客户端携带 Responses 风格的自定义工具时改用新版。
                </div>
              </div>
              <div class="setting-control">
                <select class="select input-sm select-wide" .value=${live(form.messagesFormat)} ?disabled=${saving}
                        @change=${(e) => { form.messagesFormat = e.target.value; structChanged(); }}>
                  ${messagesOptions.map((opt) => html`<option value=${opt.value} ?selected=${opt.selected}>${opt.label}</option>`)}
                </select>
              </div>
            </div>
            ${testModelRow()}
          </div>
          ${catalogError ? html`
            <p class="field-hint tone-warning">模型目录这次没有读取成功，下拉里只显示「默认」与已保存的值：${catalogError}</p>` : nothing}
        </div>`;
    },
    /** 有未保存的改动时先问一句：保存、放弃或取消这次跳转。 */
    async canLeave() {
      if (!isDirty()) return true;
      const choice = await page.confirmUnsaved({
        desc: '「网关行为」里的改动还没有保存。',
        onSave: saveAll,
      });
      return choice !== 'cancel';
    },
    onMount() {
      // 首次进入就要填满测试模型下拉：目录来自 /v1/models，读取失败只影响这一项。
      loadCatalogs().then(() => page.draw());
    },
  };
}
