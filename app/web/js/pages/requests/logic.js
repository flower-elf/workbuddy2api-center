// 请求日志页的纯逻辑：筛选状态、查询串组装、行内容与详情字段。
// 只允许 import core/format.js，任何 UI 模板都放在 requests.js 里，方便在 Node 里直接测试。
import { fmtCredit, fmtDateTime, fmtLatency, fmtNumber, fmtPercent, realmLabel } from '../../core/format.js';

export const PAGE_SIZES = [20, 50, 100];
export const LIMIT_STORE_KEY = 'wb-requests-limit';

export const DEFAULT_FILTERS = Object.freeze({
  range: '7d',
  realm: 'all',
  key: '',
  status: '',
  model: '',
  account: '',
  ip: '',
});

// 每个窗口都换算成「从现在往前推的一段」：网关的时间范围参数只有 custom 才读 since，
// 所以窗口一律发 range=custom 加 since，避免按自然日切分与「近 24 小时」对不上。
const RANGE_SECONDS = Object.freeze({ '24h': 86400, '7d': 7 * 86400, '30d': 30 * 86400 });

export const RANGE_OPTIONS = Object.freeze([
  { id: '24h', label: '近 24 小时' },
  { id: '7d', label: '近 7 天' },
  { id: '30d', label: '近 30 天' },
  { id: 'all', label: '全部' },
]);

export const REALM_OPTIONS = Object.freeze([
  { id: 'all', label: '全部' },
  { id: 'intl', label: '国际版' },
  { id: 'cn', label: '国内版' },
]);

export const STATUS_OPTIONS = Object.freeze([
  { id: '', label: '全部' },
  { id: 'ok', label: '成功' },
  { id: 'fail', label: '失败' },
]);

const API_LABELS = Object.freeze({
  chat: '对话',
  completions: '补全',
  responses: 'Responses 接口',
  messages: 'Messages 接口',
});

const OUTCOME_LABELS = Object.freeze({
  completed: '完成',
  client_aborted: '客户端取消',
  upstream_aborted: '上游中断',
  failed: '失败',
});

/** 初始筛选条件：版本默认跟随右上角「查看的版本」，与其它页面一致。 */
export function defaultFilters(realm = DEFAULT_FILTERS.realm) {
  return { ...DEFAULT_FILTERS, realm: REALM_OPTIONS.some((o) => o.id === realm) ? realm : DEFAULT_FILTERS.realm };
}

/** 每页条数只接受 20 / 50 / 100，其余值一律回到 20。 */
export function normalizeLimit(value) {
  const n = Number(value);
  return PAGE_SIZES.includes(n) ? n : PAGE_SIZES[0];
}

/** 与初始条件一模一样时为 false，「重置」按钮只在这种情况下隐藏。初始条件传 base。 */
export function isFiltered(filters, base = DEFAULT_FILTERS) {
  return Object.keys(DEFAULT_FILTERS).some((name) => filters[name] !== base[name]);
}

export function changeFilter(filters, name, value) {
  if (!(name in DEFAULT_FILTERS)) throw new Error('unknown filter: ' + name);
  return { ...filters, [name]: value };
}

/** 任一筛选条件变化都要回到第 1 页，否则新条件下的页码可能已经越界。 */
export function withFilter(state, name, value) {
  return { ...state, filters: changeFilter(state.filters, name, value), page: 1 };
}

/** 改变每页条数同样回到第 1 页。 */
export function withLimit(state, limit) {
  return { ...state, limit: normalizeLimit(limit), page: 1 };
}

export function withPage(state, page) {
  const pages = Math.max(1, Math.round(Number(state.pages) || 1));
  const wanted = Math.round(Number(page) || 1);
  return { ...state, page: Math.min(Math.max(1, wanted), pages) };
}

/** 选项列表刷新后清掉已经不在列表里的筛选值，避免筛出永远为空的结果。 */
export function pruneFilters(filters, { keyIds = [], accountIds = [] } = {}) {
  const next = { ...filters };
  if (filters.key && !keyIds.includes(filters.key)) next.key = '';
  if (filters.account && !accountIds.includes(filters.account)) next.account = '';
  return next;
}

/**
 * 组装 /usage/recent 的查询串。
 * realm 一律显式下发：网关在参数缺失时会退回默认出口，只显示一半的记录。
 */
export function buildRecentQuery(state, now = Math.floor(Date.now() / 1000)) {
  const filters = state.filters || defaultFilters();
  const params = new URLSearchParams();
  const span = RANGE_SECONDS[filters.range];
  if (span) {
    params.set('range', 'custom');
    params.set('since', String(Math.floor(now - span)));
  }
  params.set('realm', REALM_OPTIONS.some((o) => o.id === filters.realm) ? filters.realm : 'all');
  if (filters.key) params.set('key', filters.key);
  if (filters.status) params.set('status', filters.status);
  const model = String(filters.model || '').trim();
  if (model) params.set('model', model);
  if (filters.account) params.set('account', filters.account);
  const ip = String(filters.ip || '').trim();
  if (ip) params.set('ip', ip);
  params.set('page', String(state.page || 1));
  params.set('limit', String(normalizeLimit(state.limit)));
  return '/usage/recent?' + params.toString();
}

/** 旧记录只有 outcome 与 error，没有状态码，这里统一成同一个取值口径。 */
export function rowOutcome(row) {
  if (row.outcome) return row.outcome;
  return row.error ? 'failed' : 'completed';
}

export function outcomeLabel(outcome) {
  return OUTCOME_LABELS[outcome] || outcome || '—';
}

export function apiLabel(api) {
  return API_LABELS[api] || api || '—';
}

/**
 * 状态徽章：有状态码时按 2xx 成功 / 4xx 警告 / 其余危险分档并显示数字；
 * 旧记录没有状态码，改显示结果文字，分档仍按同一套口径推算。
 */
export function statusInfo(row) {
  const code = Number(row && row.status);
  if (Number.isFinite(code) && code > 0) {
    const tone = code >= 200 && code < 300 ? 'success' : code >= 400 && code < 500 ? 'warning' : 'danger';
    return { code: Math.trunc(code), tone, text: String(Math.trunc(code)), title: outcomeLabel(rowOutcome(row)) };
  }
  const outcome = rowOutcome(row);
  if (outcome === 'completed') {
    return { code: 200, tone: 'success', text: '完成', title: '旧记录没有状态码，按成功归档' };
  }
  if (outcome === 'client_aborted') {
    return { code: 499, tone: 'warning', text: '客户端取消', title: '客户端提前断开连接，网关没有返回状态码' };
  }
  if (outcome === 'upstream_aborted') {
    return { code: 502, tone: 'danger', text: '上游中断', title: '上游在应答中途断开，网关没有返回状态码' };
  }
  return { code: 502, tone: 'danger', text: '失败', title: '旧记录没有状态码，按失败归档' };
}

/** 密钥列：优先用网关解析好的 key_name，缺失时按当前密钥列表补上，再退回固定说明。 */
export function keyLabel(row, keysById = {}) {
  const name = typeof row.key_name === 'string' ? row.key_name.trim() : '';
  if (name) return name;
  const id = row.key_id || '';
  if (!id) return '未使用密钥';
  if (id === 'panel') return '面板测试台';
  const known = keysById[id];
  if (known && known.name) return known.name;
  return '已删除的密钥';
}

/** 账号列：昵称取自账号列表，账号已被删除时显示 uid 前 8 位。 */
export function accountLabel(uid, accountsById = {}) {
  const id = String(uid || '');
  if (!id) return '未采集';
  const account = accountsById[id];
  if (account && account.nickname) return account.nickname;
  return id.slice(0, 8);
}

export function rowTimestamp(row) {
  const at = Number(row.at);
  if (Number.isFinite(at) && at > 0) return at;
  const parsed = Date.parse(row.iso || '');
  return Number.isNaN(parsed) ? null : parsed / 1000;
}

/** Token 单元的第二行：缓存命中率，命中 0 显示「未命中」，没有采集到显示「—」。 */
export function cacheHint(row) {
  const prompt = Number(row.prompt_tokens) || 0;
  const hit = Number(row.cached_tokens) || 0;
  const miss = Math.max(0, prompt - hit);
  const title = `提示词缓存：命中 ${fmtNumber(hit)} Token，未命中 ${fmtNumber(miss)} Token`;
  const pct = row.cache_hit_pct;
  if (pct === null || pct === undefined || pct === '') return { text: '—', tone: 'muted', title };
  const value = Number(pct);
  if (!Number.isFinite(value)) return { text: '—', tone: 'muted', title };
  if (value <= 0) return { text: '未命中', tone: 'muted', title };
  return { text: '缓存 ' + fmtPercent(value), tone: 'success', title };
}

export function creditText(row) {
  const credit = row.credit;
  if (credit === null || credit === undefined || credit === '') {
    return { text: '—', title: '上游未返回该项', missing: true };
  }
  const value = Number(credit);
  if (!Number.isFinite(value)) return { text: '—', title: '上游未返回该项', missing: true };
  return { text: fmtCredit(value), title: '本次请求在上游的实际扣费', missing: false };
}

function numberText(value) {
  if (value === null || value === undefined || value === '') return '—';
  const n = Number(value);
  return Number.isFinite(n) ? fmtNumber(n) : '—';
}

/** 页码列表：首尾固定在两端，中间最多显示当前页前后两页。 */
export function pageList(page, pages) {
  const total = Math.max(1, Math.round(Number(pages) || 1));
  const current = Math.min(Math.max(1, Math.round(Number(page) || 1)), total);
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const out = [1];
  let start = Math.max(2, current - 2);
  let end = Math.min(total - 1, current + 2);
  if (current <= 4) end = 5;
  if (current >= total - 3) start = total - 4;
  if (start > 2) out.push('...');
  for (let i = start; i <= end; i++) out.push(i);
  if (end < total - 1) out.push('...');
  out.push(total);
  return out;
}

/**
 * 抽屉里的逐项字段。copy 非空时渲染复制按钮；失败记录多一行错误原因。
 * ctx = { keysById, accountsById }
 */
export function requestDetail(row, ctx = {}) {
  const keys = ctx.keysById || {};
  const accounts = ctx.accountsById || {};
  const status = statusInfo(row);
  const cache = cacheHint(row);
  const credit = creditText(row);
  const speed = Number(row.tokens_per_sec);
  const rows = [
    { label: '时间', value: fmtDateTime(rowTimestamp(row)) },
    { label: 'API 接口', value: apiLabel(row.api) },
    { label: '版本', value: realmLabel(row.realm) },
    { label: '密钥', value: keyLabel(row, keys), copy: row.key_id || '' },
    { label: '来源 IP', value: row.ip || '未采集', copy: row.ip || '' },
    { label: '账号', value: accountLabel(row.account, accounts), copy: row.account || '' },
    { label: '模型', value: row.model || '—', copy: row.model || '' },
    { label: '状态码', value: Number(row.status) > 0 ? String(Math.trunc(Number(row.status))) : '没有采集到状态码' },
    { label: '结果', value: outcomeLabel(rowOutcome(row)) },
    { label: '流式', value: row.stream ? '流式' : '非流式' },
    { label: '首字延迟 (TTFT)', value: fmtLatency(row.ttft_ms) },
    { label: '总耗时', value: fmtLatency(row.elapsed_ms) },
    { label: '生成耗时', value: fmtLatency(row.gen_ms) },
    { label: '速度', value: Number.isFinite(speed) && speed > 0 ? speed.toFixed(1) + ' Token/秒' : '—' },
    { label: '输入 Token', value: numberText(row.prompt_tokens) },
    { label: '输出 Token', value: numberText(row.completion_tokens) },
    { label: '思考 Token', value: numberText(row.reasoning_tokens) },
    { label: '缓存命中 Token', value: numberText(row.cached_tokens) },
    { label: '总 Token', value: numberText(row.total_tokens) },
    { label: '缓存命中率', value: cache.text },
    { label: '积分', value: credit.text, title: credit.title },
  ];
  if (row.error) rows.push({ label: '错误', value: String(row.error) });
  return rows;
}
