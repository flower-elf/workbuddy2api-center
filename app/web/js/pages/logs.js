// 运行日志页：终端样式的实时日志面板；标签页在前台时按 LOG_POLL_MS 增量读取 /logs，暂停监听、滚动到底自动跟随。
import { html, render, nothing, repeat } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { getJSON, postJSON, downloadFile } from '../core/api.js';
import { fmtNumber } from '../core/format.js';
import { pageHeader, segmented, loadErrorPage, loadErrorInline, busyClick } from '../ui/widgets.js';
import { confirmDialog } from '../ui/dialog.js';
import { toast } from '../ui/toast.js';
import { copyWithToast } from '../ui/copy.js';
import {
  LOG_POLL_MS, LOG_RESET_LIMIT, LOG_POLL_LIMIT, LOG_MAX, LEVELS, TAGS,
  defaultFilters, buildLogsQuery, filterLogs, mergeLogs, nextSinceId, logStats,
  levelClass, tagClass, formatLogText,
} from './logs/logic.js';

export const refreshMs = LOG_POLL_MS;

const PAGE_DESC = '网关运行日志：反向代理请求、流式转发、调度巡检、任务打卡与异常告警。'
  + '网关内存中保留最新 2000 条，标签页在前台时每 2 秒读取一次新日志。';

export function mount(host) {
  const state = {
    entries: [],
    lastId: 0,
    filters: defaultFilters(),
    live: true,
    autoScroll: true,
    status: '正在读取网关日志…',
    loaded: false,
    error: '',
  };
  let fetching = false;
  let searchTimer = null;
  let drawFrame = 0;

  function draw() {
    render(view(), host);
    drawLines();
  }

  function scheduleDraw() {
    if (drawFrame) return;
    drawFrame = requestAnimationFrame(() => {
      drawFrame = 0;
      draw();
    });
  }

  function visibleLogs() {
    return filterLogs(state.entries, state.filters);
  }

  function drawLines() {
    const body = host.querySelector('.terminal-body');
    if (!body) return;
    const visible = visibleLogs();
    if (!visible.length) {
      const message = state.entries.length ? '暂无匹配的日志记录' : (state.loaded ? '网关暂时没有输出日志' : '正在读取网关日志…');
      render(html`<div class="log-empty">${message}</div>`, body);
    } else {
      render(html`${repeat(visible, (entry) => entry.id, (entry) => lineTemplate(entry))}`, body);
    }
    if (state.autoScroll) body.scrollTop = body.scrollHeight;
  }

  function lineTemplate(entry) {
    return html`
      <div class="log-line">
        <span class="log-ts">${entry.time || entry.ts || ''}</span>
        <span class="log-lvl ${levelClass(entry.level)}">${entry.level || 'INFO'}</span>
        <span class="log-tag ${tagClass(entry.tag)}">[${entry.tag || 'system'}]</span>
        <span class="log-msg">${entry.msg || ''}</span>
      </div>`;
  }

  async function fetchNow(forceReset) {
    if (fetching) return;
    fetching = true;
    if (forceReset) {
      state.status = '正在读取…';
      draw();
    }
    try {
      const data = await getJSON(buildLogsQuery({
        sinceId: forceReset ? 0 : state.lastId,
        limit: forceReset ? LOG_RESET_LIMIT : LOG_POLL_LIMIT,
      }));
      const logs = Array.isArray(data.logs) ? data.logs : [];
      if (forceReset) {
        state.entries = logs.slice(-LOG_MAX);
        state.lastId = 0;
      } else {
        state.entries = mergeLogs(state.entries, logs);
      }
      const maxId = Number(data.max_id);
      state.lastId = Math.max(state.lastId, Number.isFinite(maxId) && maxId > 0 ? maxId : nextSinceId(logs, state.lastId));
      state.loaded = true;
      state.error = '';
      state.status = '就绪 · ' + new Date().toLocaleTimeString('zh-CN', { hour12: false });
    } catch (err) {
      state.error = '日志没有读取成功。' + (err.message ? ' ' + err.message : '');
      state.status = '连接中断 · ' + (err.message || '读取失败');
    } finally {
      fetching = false;
    }
    draw();
  }

  function toggleLive() {
    state.live = !state.live;
    if (state.live) fetchNow(false);
    else draw();
  }

  function toggleAutoScroll() {
    state.autoScroll = !state.autoScroll;
    draw();
  }

  function onScroll(event) {
    const body = event.currentTarget;
    const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
    if (atBottom === state.autoScroll) return;
    state.autoScroll = atBottom;
    scheduleDraw();
  }

  function setFilter(name, value) {
    if (state.filters[name] === value) return;
    state.filters = { ...state.filters, [name]: value };
    draw();
  }

  function onSearchInput(event) {
    const value = event.target.value;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => setFilter('search', value), 200);
  }

  async function copyFiltered() {
    const visible = visibleLogs();
    if (!visible.length) {
      toast.warn('当前筛选没有日志可复制');
      return;
    }
    await copyWithToast(formatLogText(visible), `已复制 ${visible.length} 行日志`);
  }

  async function exportLogs() {
    const name = await downloadFile('/logs/export', 'wb-proxy-center.log');
    toast.success('日志文件已保存', name);
  }

  async function clearLogs() {
    const done = await confirmDialog({
      title: '清空网关日志？',
      desc: '将清空网关内存里保留的全部日志，最多 2000 条。用量统计与请求记录不受影响，清空后新产生的日志会继续出现在这里。',
      confirmText: '清空',
      danger: true,
      onConfirm: async () => {
        await postJSON('/logs/clear', {});
        state.entries = [];
        state.lastId = 0;
        state.status = '就绪 · ' + new Date().toLocaleTimeString('zh-CN', { hour12: false });
        draw();
      },
    });
    if (done) toast.success('网关日志已清空');
  }

  function toolbar() {
    const stats = logStats(state.entries);
    return html`
      <div class="card col">
        <div class="log-toolbar">
          ${segmented(LEVELS, state.filters.level, (id) => setFilter('level', id), 'seg-sm')}
          ${segmented(TAGS, state.filters.tag, (id) => setFilter('tag', id), 'seg-sm')}
          <span class="log-search">
            ${icon('Search')}
            <input class="input" type="search" placeholder="过滤关键字，例如 deepseek、11-128、401"
                   aria-label="过滤日志关键字" @input=${onSearchInput}>
          </span>
        </div>
        <div class="row-between">
          <span class="log-stats nums">
            共 ${fmtNumber(stats.total)} 条
            ${stats.errors ? html`<span class="tone-danger"> · 错误 ${fmtNumber(stats.errors)}</span>` : nothing}
            ${stats.warns ? html`<span class="tone-warning"> · 警告 ${fmtNumber(stats.warns)}</span>` : nothing}
          </span>
          <span class="row-wrap">
            <button class="btn ${state.autoScroll ? 'btn-primary' : 'btn-outline'} btn-sm"
                    @click=${toggleAutoScroll}>${icon('ArrowDownToLine')}自动滚屏：${state.autoScroll ? '开' : '关'}</button>
            <button class="btn btn-outline btn-sm" @click=${busyClick(copyFiltered)}>${icon('Copy')}复制筛选结果</button>
            <button class="btn btn-outline btn-sm" @click=${busyClick(exportLogs)}>${icon('Download')}导出日志文件</button>
            <button class="btn btn-danger btn-sm" @click=${busyClick(clearLogs)}>${icon('Trash2')}清空日志</button>
          </span>
        </div>
        <div class="terminal">
          <div class="terminal-head">
            <span class="terminal-dots">
              <span class="terminal-dot" style="background:#ef4444"></span>
              <span class="terminal-dot" style="background:#f59e0b"></span>
              <span class="terminal-dot" style="background:#10b981"></span>
              <span class="terminal-title">wb-proxy-center-console.log</span>
            </span>
            <span class="terminal-status">${state.status}</span>
          </div>
          <div class="terminal-body scroll-slim" @scroll=${onScroll}></div>
        </div>
      </div>`;
  }

  function view() {
    const actions = html`
      <button class="btn btn-outline btn-sm" @click=${busyClick(() => fetchNow(true))}>${icon('RefreshCw')}立即读取</button>
      <button class="btn btn-sm ${state.live ? 'btn-primary' : 'btn-outline'}" @click=${toggleLive}>
        ${icon(state.live ? 'Pause' : 'Play')}${state.live ? '实时监听中' : '已暂停监听'}
      </button>`;
    if (!state.loaded && state.error) {
      return html`
        <div class="page">
          ${pageHeader({ path: 'logs', title: '运行日志', desc: PAGE_DESC, actions })}
          ${loadErrorPage(state.error, () => fetchNow(true))}
        </div>`;
    }
    return html`
      <div class="page">
        ${pageHeader({ path: 'logs', title: '运行日志', desc: PAGE_DESC, actions })}
        ${state.error ? loadErrorInline(state.error, () => fetchNow(false)) : nothing}
        ${toolbar()}
      </div>`;
  }

  const instance = {
    refresh: async () => {
      if (!state.live) return;
      await fetchNow(false);
    },
    unmount() {
      clearTimeout(searchTimer);
      if (drawFrame) cancelAnimationFrame(drawFrame);
    },
  };

  draw();
  fetchNow(true);
  return instance;
}
