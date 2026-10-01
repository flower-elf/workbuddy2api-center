// 任务页：调度器状态、国内版成长任务与日常福利、国际版每日活跃打卡、积分变动记录。
// 心跳 30 秒；任何操作都不会改动右上角「查看的版本」，页面始终只读 getState().viewRealm。

import { html, nothing, render } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { getJSON, postJSON } from '../core/api.js';
import { getState } from '../core/store.js';
import { fmtNumber, realmLabel } from '../core/format.js';
import { confirmDialog } from '../ui/dialog.js';
import { toast, toastError } from '../ui/toast.js';
import {
  badge, busyClick, emptyState, loadErrorInline, loadErrorPage, pageHeader, segmented, skeletonBlock, skeletonRows, statCard,
} from '../ui/widgets.js';
import * as logic from './tasks/logic.js';
import * as acct from './accounts/actions.js';
import { anyOk, summarizeResults } from './accounts/logic.js';

export const autoRefresh = true;

export function mount(host) {
  const S = {
    scheduler: null,
    growth: null,
    growthUid: 'all',
    mobileTab: 'scheduler',
    running: false,
    runLogs: [],
    creditEvents: null,
    loading: true,
    error: '',
    partial: '',
  };

  function draw() {
    render(view(), host);
  }

  // ---------------------------------------------------------------- 数据

  async function refresh() {
    const realm = getState().viewRealm;
    const problems = [];
    let schedulerOk = false;
    let eventsOk = false;
    try {
      S.scheduler = await getJSON('/scheduler');
      schedulerOk = true;
    } catch (err) {
      problems.push('调度器状态没有读取成功：' + err.message);
    }
    try {
      const data = await getJSON('/accounts/credit-events?realm=' + encodeURIComponent(realm) + '&limit=200');
      S.creditEvents = data.events || [];
      eventsOk = true;
    } catch (err) {
      problems.push('积分变动记录没有读取成功：' + err.message);
    }
    if (realm === 'cn') {
      try {
        await loadGrowth();
      } catch (err) {
        problems.push('成长任务没有读取成功：' + err.message);
      }
    }
    if (!schedulerOk && !eventsOk && S.loading) {
      S.error = problems.join(' ');
    } else {
      S.error = '';
    }
    S.partial = problems.join(' ');
    S.loading = false;
    draw();
  }

  async function loadGrowth() {
    const query = S.growthUid && S.growthUid !== 'all' ? '?uid=' + encodeURIComponent(S.growthUid) : '';
    S.growth = await getJSON('/tasks' + query);
  }

  // ---------------------------------------------------------------- 动作

  async function triggerScheduler() {
    try {
      const result = await postJSON('/scheduler/trigger', {});
      toast.success(result.msg || '已经触发一次巡检');
      setTimeout(refresh, 2000);
    } catch (err) {
      toastError('触发失败', err);
    }
  }

  async function toggleScheduler() {
    try {
      const result = await postJSON('/scheduler/toggle', {});
      toast.success(result.enabled ? '后台调度器已启用' : '后台调度器已暂停');
      await refresh();
    } catch (err) {
      toastError('切换失败', err);
    }
  }

  async function runGrowthTasks() {
    S.running = true;
    S.runLogs = ['[task_runner] 正在接取任务、上报事件并领取奖励，请稍候'];
    draw();
    try {
      const payload = {};
      if (S.growthUid && S.growthUid !== 'all') payload.uid = S.growthUid;
      const result = await postJSON('/tasks/run', payload);
      S.runLogs = result.logs || [];
      if (result.credit_added) toast.success('成长任务执行完成', '本次共增加 ' + fmtNumber(result.credit_added) + ' 积分');
      else toast.success('成长任务执行完成', result.msg || '没有新的积分变化');
      await refresh();
    } catch (err) {
      S.runLogs = S.runLogs.concat(['执行出错：' + err.message]);
      toastError('任务执行失败', err);
    } finally {
      S.running = false;
      draw();
    }
  }

  async function triggerCatTravel() {
    try {
      const payload = {};
      if (S.growthUid && S.growthUid !== 'all') payload.uid = S.growthUid;
      const result = await postJSON('/tasks/travel', payload);
      if (result.ok) toast.success('猫猫旅行结算完成', result.msg || '');
      else toast.warn('猫猫旅行没有完成', result.msg || '上游没有返回结果');
      await refresh();
    } catch (err) {
      toastError('猫猫旅行失败', err);
    }
  }

  async function doCheckin() {
    try {
      const result = await acct.checkin();
      const results = result.results || [];
      if (!results.length) toast.warn('没有可签到的国内版账号');
      else if (anyOk(results)) toast.success('每日签到', summarizeResults(results, { okText: '签到成功' }));
      else toast.warn('每日签到', summarizeResults(results));
      await refresh();
    } catch (err) {
      toastError('签到失败', err);
    }
  }

  async function doDailyChat() {
    try {
      const result = await acct.dailyChat();
      const results = result.results || [];
      if (!results.length) toast.warn('没有可打卡的国际版账号');
      else if (anyOk(results)) toast.success('每日活跃打卡', summarizeResults(results, { okText: '活跃打卡成功' }));
      else toast.warn('每日活跃打卡', summarizeResults(results));
      await refresh();
    } catch (err) {
      toastError('打卡失败', err);
    }
  }

  async function doDailyChatWeb() {
    const go = await confirmDialog({
      title: '确认网页通道打卡？',
      desc: '会为每个已启用的国际版账号各建一个网页端会话。这会实际发起一次任务并消耗该账号少量积分，用于计入官方「每日活跃」。',
      confirmText: '我已了解，继续',
    });
    if (!go) return;
    try {
      const result = await acct.dailyChatWeb();
      const results = result.results || [];
      if (!results.length) toast.warn('没有可打卡的国际版账号');
      else if (anyOk(results)) {
        toast.success('网页通道打卡', summarizeResults(results, { okText: '已建会话', withDetail: 'conversation' }));
      } else toast.warn('网页通道打卡', summarizeResults(results));
      await refresh();
    } catch (err) {
      toastError('网页通道打卡失败', err);
    }
  }

  // ---------------------------------------------------------------- 片段

  function schedulerCard(realm) {
    if (S.loading && !S.scheduler) {
      return html`<div class="card">${skeletonBlock(96)}</div>`;
    }
    const view = logic.schedulerView(S.scheduler || {}, realm);
    const logs = logic.schedulerLogLines(S.scheduler);
    return html`
      <div class="card">
        <div class="card-head">
          <h2 class="card-title">${icon('AlarmClock')}后台定时调度器</h2>
          <div class="row-wrap">
            <button class="btn btn-outline btn-sm" title="不等下一个排程时刻，立刻执行一次巡检"
                    @click=${busyClick(triggerScheduler)}>${icon('Play')}${view.triggerText}</button>
            <button class="btn btn-outline btn-sm" title="暂停期间不会自动巡检，手动触发仍然可用"
                    @click=${busyClick(toggleScheduler)}>${view.enabled ? icon('Pause') : icon('Play')}${view.toggleText}</button>
          </div>
        </div>
        <div class="row-wrap">
          <span class="status-dot ${view.enabled ? 'ok' : 'bad'}"></span>
          <span class="text-sm">${view.stateText}</span>
        </div>
        <div class="text-2xs muted">${view.timesText}</div>
        <div class="log-box scroll-slim">
          ${logs.length
            ? logs.map((line) => html`<div class="log-line">${line}</div>`)
            : html`<div class="log-line muted">还没有巡检记录，第一次巡检完成后会显示在这里。</div>`}
        </div>
      </div>`;
  }

  function growthCard(realm) {
    if (realm !== 'cn') {
      return html`
        <div class="card">
          <div class="card-head"><h2 class="card-title">${icon('Sparkles')}成长任务与日常福利</h2></div>
          <div class="text-xs muted">${logic.growthEmptyText(realm)}</div>
        </div>`;
    }
    if (S.loading && !S.growth) {
      return html`<div class="card">${skeletonBlock(180)}</div>`;
    }
    const data = S.growth || {};
    const accounts = data.accounts || [];
    const options = logic.growthAccountOptions(accounts, S.growthUid);
    const summary = data.summary || {};
    const tasks = data.tasks || [];
    const travel = logic.travelView(summary.travel);
    return html`
      <div class="card">
        <div class="card-head">
          <div>
            <h2 class="card-title">${icon('Sparkles')}成长任务与日常福利</h2>
            <p class="card-sub">批量接取任务、上报事件并领取官方成长积分，最多 1450 分；猫猫日常每天可以出发一次。</p>
          </div>
          <div class="row-wrap">
            <select class="select input-sm" title="选择要执行任务的账号，选「全部国内账号」会依次为每个已启用的国内版账号执行"
                    .value=${options.selected}
                    @change=${(event) => { S.growthUid = event.target.value; refresh(); }}>
              ${options.options.map((option) => html`<option value=${option.value}>${option.label}</option>`)}
            </select>
            <button class="btn btn-primary btn-sm" title="接取任务、上报事件并领取已经达成的奖励"
                    ?disabled=${S.running} @click=${busyClick(runGrowthTasks)}>
              ${S.running ? icon('LoaderCircle', 'spin') : icon('Sparkles')}${S.running ? '任务执行中' : '执行任务领积分'}
            </button>
            <button class="btn btn-outline btn-sm" title="让猫猫出门旅行，归来后可以领取积分奖励"
                    @click=${busyClick(triggerCatTravel)}>${icon('Gift')}猫猫旅行</button>
          </div>
        </div>
        ${data.msg ? loadErrorInline(data.msg, refresh) : nothing}
        <div class="grid-3">
          ${statCard({ label: '连续打卡天数', value: (summary.streak_days || 0) + ' 天', icon: 'CalendarDays' })}
          ${statCard({ label: '成长能量余额', value: fmtNumber(summary.energy || 0), icon: 'Zap', tone: 'success' })}
          ${statCard({ label: '猫猫日常状态', value: travel.text, icon: 'Cat', tone: travel.tone, hint: travel.desc })}
        </div>
        <div class="table-wrap">
          <table class="table table-compact">
            <thead>
              <tr>
                <th>任务名称 / 代码</th>
                <th>任务说明</th>
                <th>进度</th>
                <th>奖励</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              ${tasks.length
                ? tasks.map((task) => {
                  const row = logic.growthTaskView(task);
                  return html`
                    <tr>
                      <td><div class="medium">${row.name}</div><div class="cell-mono">${row.code}</div></td>
                      <td class="cell-sub">${row.description}</td>
                      <td class="nums">${row.progress}</td>
                      <td class="nums">${row.rewards}</td>
                      <td>${badge(row.badge.text, row.badge.tone)}</td>
                    </tr>`;
                })
                : html`<tr><td colspan="5">${emptyState({ icon: 'Inbox', title: '没有读到成长任务', desc: logic.growthEmptyText(realm) })}</td></tr>`}
            </tbody>
          </table>
        </div>
        ${S.runLogs.length ? html`
          <div class="log-box scroll-slim">
            ${S.runLogs.map((line) => html`<div class="log-line">${line}</div>`)}
          </div>` : nothing}
      </div>`;
  }

  function dailyCard(realm) {
    if (realm === 'cn') {
      return html`
        <div class="card">
          <div class="card-head">
            <div>
              <h2 class="card-title">${icon('CalendarCheck')}每日签到</h2>
              <p class="card-sub">为每个国内版账号签到一次，领取当天的签到积分。签到结果与上游自动任务记录之后会一并出现在积分变动记录里。</p>
            </div>
            <button class="btn btn-primary btn-sm" @click=${busyClick(doCheckin)}>${icon('CalendarCheck')}开始签到</button>
          </div>
        </div>`;
    }
    return html`
      <div class="card">
        <div class="card-head">
          <div>
            <h2 class="card-title">${icon('CalendarCheck')}每日活跃打卡</h2>
            <p class="card-sub">每日活跃只认网页端对话：桌面身份打卡发送一条轻量对话，网页通道打卡只建立一个网页端会话。两者都会消耗少量积分，用于计入官方「每日活跃」。</p>
          </div>
          <div class="row-wrap">
            <button class="btn btn-primary btn-sm" title="为每个已启用的国际版账号发送一条轻量对话"
                    @click=${busyClick(doDailyChat)}>${icon('CalendarCheck')}每日活跃打卡</button>
            <button class="btn btn-outline btn-sm" title="只建网页端会话，不发送桌面端对话；会消耗少量积分"
                    @click=${doDailyChatWeb}>${icon('Globe')}网页通道打卡</button>
          </div>
        </div>
      </div>`;
  }

  function creditEventsCard(realm) {
    if (S.creditEvents === null) {
      return html`<div class="card card-flush">${skeletonRows(4)}</div>`;
    }
    const rows = logic.creditEventRows(S.creditEvents);
    return html`
      <div class="card card-flush">
        <div class="card-head">
          <div>
            <h2 class="card-title">${icon('Coins')}积分变动记录</h2>
            <p class="card-sub">${realmLabel(realm)}账号余额增加的记录，最新的在前面。</p>
          </div>
          <span class="text-2xs muted">共 ${rows.length} 条</span>
        </div>
        ${rows.length ? html`
          <div class="table-wrap">
            <table class="table table-compact">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>账号</th>
                  <th class="right">变更前</th>
                  <th class="right">变更后</th>
                  <th class="right">变动</th>
                </tr>
              </thead>
              <tbody>
                ${rows.map((row) => html`
                  <tr>
                    <td class="nums">${row.time}</td>
                    <td>${row.account}</td>
                    <td class="right nums">${row.before}</td>
                    <td class="right nums">${row.after}</td>
                    <td class="right nums tone-${row.tone}">${row.deltaText}</td>
                  </tr>`)}
              </tbody>
            </table>
          </div>`
          : emptyState({ icon: 'Coins', title: '还没有积分变动记录', desc: logic.creditEventsEmptyText(realm) })}
      </div>`;
  }

  function view() {
    const realm = getState().viewRealm;
    return html`
      <div class="page">
        ${pageHeader({
          path: 'tasks',
          title: '任务',
          desc: realm === 'cn'
            ? '国内版的每日签到、成长任务与猫猫日常，以及后台调度器状态与积分变动记录'
            : '国际版的每日活跃打卡，以及后台调度器状态与积分变动记录',
          actions: html`<button class="btn btn-outline btn-sm" @click=${busyClick(refresh)}>${icon('RefreshCw')}刷新</button>`,
        })}
        ${S.error ? loadErrorPage(S.error, refresh) : nothing}
        ${!S.error && S.partial ? loadErrorInline(S.partial, refresh) : nothing}
        ${S.error ? nothing : html`
          <div class="tasks-switch">${segmented(sections(realm), S.mobileTab, switchSection, 'seg-sm')}</div>
          ${taskBlock('scheduler', schedulerCard(realm))}
          ${taskBlock('daily', dailyCard(realm))}
          ${taskBlock('growth', growthCard(realm))}
          ${taskBlock('credits', creditEventsCard(realm))}`}
      </div>`;
  }

  // 移动端一次只显示一块，桌面端四块同时可见（与参考项目一致）。
  function sections(realm) {
    return [
      { id: 'scheduler', label: '调度器' },
      { id: 'daily', label: realm === 'cn' ? '每日签到' : '每日打卡' },
      { id: 'growth', label: '成长任务' },
      { id: 'credits', label: '积分记录' },
    ];
  }

  function switchSection(id) {
    S.mobileTab = id;
    draw();
  }

  function taskBlock(id, content) {
    return html`<div class=${'task-block' + (S.mobileTab === id ? ' active' : '')}>${content}</div>`;
  }

  draw();
  refresh();

  return {
    refresh,
    unmount() {},
  };
}
