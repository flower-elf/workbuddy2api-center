// 设置页的纯逻辑：选项生成、本地校验、请求体组装与运行信息整理。
// 只依赖 core/format.js、其它页面逻辑模块与自身，可在 Node 里直接测试。
import { authSummary } from '../keys/logic.js';

export const MESSAGES_FORMATS = [
  { value: 'openai-completions', label: '旧版 openai-completions' },
  { value: 'openai-responses', label: '新版 openai-responses' },
];

/** 服务端存的值 → 下拉选项；存的值不在枚举里时退回服务端给出的默认值。 */
export function messagesFormatValue(stored, fallback) {
  const known = MESSAGES_FORMATS.map((f) => f.value);
  if (known.includes(stored)) return stored;
  if (known.includes(fallback)) return fallback;
  return MESSAGES_FORMATS[0].value;
}

export function messagesFormatOptions(stored, fallback) {
  const picked = messagesFormatValue(stored, fallback);
  return MESSAGES_FORMATS.map((f) => ({ value: f.value, label: f.label, selected: f.value === picked }));
}

/**
 * 测试使用的模型下拉：首项是「默认」，随后是该出口目录里的模型。
 * 服务端存着的模型如果已经不在目录里，补一条并标注「当前设置」，免得保存时被悄悄换掉。
 */
export function testModelOptions(ids, stored) {
  const keep = String(stored == null ? '' : stored).trim();
  const options = [{ value: '', label: '默认', selected: keep === '' }];
  const seen = new Set(['']);
  for (const raw of ids || []) {
    const id = String(raw == null ? '' : raw).trim();
    if (!id || seen.has(id)) continue;
    seen.add(id);
    options.push({ value: id, label: id, selected: id === keep });
  }
  if (keep && !seen.has(keep)) {
    options.push({ value: keep, label: keep + '（当前设置）', current: true, selected: true });
  }
  return options;
}

export function testModelSelected(stored) {
  return String(stored == null ? '' : stored).trim();
}

/** 下拉旁的状态说明：使用默认模型还是自定义模型（存着的值与默认相同时也算默认）。 */
export function testModelStateText(stored, fallback) {
  const keep = String(stored == null ? '' : stored).trim();
  const effective = String(fallback == null ? '' : fallback).trim();
  if (keep && keep !== effective) return { text: '当前 ' + keep, tone: 'info' };
  const shown = effective || keep;
  return { text: shown ? '默认 ' + shown : '默认', tone: 'muted' };
}

/** 与服务端 parse_test_model 同一口径：字母或数字开头，只含字母数字点连字符下划线。 */
export function validateTestModel(value) {
  const text = String(value == null ? '' : value).trim();
  if (!text) return '';
  return /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(text)
    ? ''
    : '模型名称只能包含字母、数字、点、连字符与下划线，并以字母或数字开头。';
}

export function parseNonNegativeInteger(text, { label = '数值', max = Number.MAX_SAFE_INTEGER } = {}) {
  const raw = String(text == null ? '' : text).trim();
  if (raw === '') return { ok: true, value: 0 };
  if (!/^\d+$/.test(raw)) return { ok: false, error: label + '需要填写 0 或正整数。' };
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value > max) {
    return { ok: false, error: label + '需要填写 0 到 ' + max + ' 之间的整数。' };
  }
  return { ok: true, value };
}

export function parseNonNegativeNumber(text, { label = '数值', max = 1e15 } = {}) {
  const raw = String(text == null ? '' : text).trim();
  if (raw === '') return { ok: true, value: 0 };
  if (!/^\d+(\.\d+)?$/.test(raw)) return { ok: false, error: label + '需要填写 0 或正数。' };
  const value = Number(raw);
  if (!Number.isFinite(value) || value > max) {
    return { ok: false, error: label + '需要填写 0 到 ' + max + ' 之间的数。' };
  }
  return { ok: true, value };
}

/** 面板密码修改前的本地校验；错误文案为空串表示通过。 */
export function validatePasswordChange(current, next) {
  if (!String(current == null ? '' : current)) return '请填写当前密码。';
  if (!String(next == null ? '' : next)) return '请填写新密码。';
  if (String(next).length < 4) return '新密码至少 4 位。';
  return '';
}

// ---------------------------------------------------------------------------
// 代理槽
// ---------------------------------------------------------------------------

export function slotPayload(slots) {
  return (slots || []).map((slot) => ({
    id: slot.id || '',
    name: String(slot.name || '').trim(),
    url: String(slot.url || '').trim(),
    enabled: slot.enabled !== false,
  }));
}

/** 每行都需要代理地址，服务端会丢弃没有地址的行，这里先拦住。 */
export function validateSlots(slots) {
  const rows = slots || [];
  for (let i = 0; i < rows.length; i += 1) {
    const url = String(rows[i].url || '').trim();
    if (!url) return `第 ${i + 1} 个槽位还没有填写代理地址。`;
    if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(url)) return `第 ${i + 1} 个槽位的地址需要带协议，例如 http://127.0.0.1:7890。`;
  }
  return '';
}

export function addSlot(slots) {
  const out = (slots || []).slice();
  out.push({ id: '', name: '槽 ' + (out.length + 1), url: '', enabled: true, bound: 0 });
  return out;
}

export function removeSlot(slots, index) {
  const out = (slots || []).slice();
  if (index >= 0 && index < out.length) out.splice(index, 1);
  return out;
}

/**
 * 编辑器里有没有还没保存的改动：跟着服务端最后给的列表逐项比。
 * 轮询靠它决定要不要覆盖编辑器内容，否则正在填的行会被刷掉（问题 #79）。
 */
export function slotsDirty(slots, baseline) {
  const shape = (list) => (list || []).map((slot) => ({
    id: String((slot && slot.id) || ''),
    name: String((slot && slot.name) || ''),
    url: String((slot && slot.url) || ''),
    enabled: !(slot && slot.enabled === false),
  }));
  return JSON.stringify(shape(slots)) !== JSON.stringify(shape(baseline));
}

/** 把探测到并勾选的出口加入列表；已有相同地址的不再重复添加。 */
export function importCandidates(slots, candidates, pickedUrls) {
  const out = (slots || []).slice();
  const known = new Set(out.map((s) => String(s.url || '').trim()));
  const wanted = new Set((pickedUrls || []).map((u) => String(u || '').trim()));
  for (const candidate of candidates || []) {
    const url = String(candidate.url || '').trim();
    if (!url || !wanted.has(url) || known.has(url)) continue;
    known.add(url);
    out.push({ id: '', name: '槽 ' + (out.length + 1), url, enabled: true, bound: 0 });
  }
  return out;
}

export function discoverText(candidates) {
  const list = candidates || [];
  const reachable = list.filter((c) => c.reachable).length;
  return `探测到 ${list.length} 个地址，其中 ${reachable} 个可达`;
}

/** 启用中且还没有绑定出口的账号。 */
export function unboundEnabledAccounts(accounts) {
  return (accounts || []).filter((a) => a && a.enabled !== false && !a.proxySlot);
}

/** 自动分配：未绑定账号依次分到启用中的槽位，返回 [{uid, slotId}]。 */
export function autoAssignPlan(accounts, slots) {
  const spare = (slots || []).filter((s) => s.enabled !== false && s.id);
  if (!spare.length) return [];
  const plan = [];
  unboundEnabledAccounts(accounts).forEach((account, index) => {
    plan.push({ uid: account.uid, slotId: spare[index % spare.length].id });
  });
  return plan;
}

export function bindingText(count) {
  const n = Number(count) || 0;
  return n ? `绑定 ${n} 个账号` : '未绑定账号';
}

// ---------------------------------------------------------------------------
// 运行信息
// ---------------------------------------------------------------------------

/**
 * 运行信息页的行。usageLog 由用量目录推导，服务端没有单独的字段。
 */
export function aboutRows(view, origin) {
  const data = view || {};
  const usageDir = String(data.usage_dir || '');
  const usageLog = usageDir ? usageDir.replace(/[\\/]+$/, '') + '/usage.jsonl' : '—';
  return [
    { label: '当前版本', value: data.version || '—', mono: true },
    { label: '账号存储目录', value: data.accounts_dir || '—', mono: true, copy: true },
    { label: '用量目录', value: usageDir || '—', mono: true, copy: true },
    { label: '用量日志', value: usageLog, mono: true, copy: true },
    { label: '设置文件', value: data.settings_file || '—', mono: true, copy: true },
    // 与密钥页同一口径：接口到底要不要密钥，三种状态各说各的。
    { label: '接口认证', value: authSummary(data).text },
    { label: 'API 地址', value: String(origin || '').replace(/\/+$/, '') + '/v1', mono: true, copy: true },
  ];
}
