/* 请求日志页纯逻辑：筛选状态、查询串、状态分档、详情字段。
 * node tests/javascript/_test_requests_logic.js */
import assert from 'node:assert/strict';

const logic = await import('../../app/web/js/pages/requests/logic.js');

const NOW = 1_700_000_000;
const params = (url) => new URLSearchParams(url.slice(url.indexOf('?') + 1));
const state = (filters = {}, extra = {}) => ({
  filters: { ...logic.defaultFilters(), ...filters },
  page: 1,
  limit: 20,
  pages: 1,
  ...extra,
});

// ---- 默认筛选：显式下发 realm=all，否则网关只回默认出口的记录
{
  const query = logic.buildRecentQuery(state(), NOW);
  const p = params(query);
  assert.equal(query.startsWith('/usage/recent?'), true);
  assert.equal(p.get('range'), 'custom');
  assert.equal(p.get('since'), String(NOW - 7 * 86400), '近 7 天按滚动窗口换算，不按自然日');
  assert.equal(p.get('realm'), 'all');
  assert.equal(p.get('page'), '1');
  assert.equal(p.get('limit'), '20');
  assert.equal(p.get('key'), null);
  assert.equal(p.get('status'), null);
  assert.equal(p.get('model'), null);
  assert.equal(p.get('account'), null);
  assert.equal(p.get('ip'), null);
  assert.deepEqual([...p.keys()].sort(), ['limit', 'page', 'range', 'realm', 'since']);
}

// ---- 每个筛选项各自映射到一个查询参数
{
  assert.equal(params(logic.buildRecentQuery(state({ range: '24h' }), NOW)).get('since'), String(NOW - 86400));
  assert.equal(params(logic.buildRecentQuery(state({ range: '30d' }), NOW)).get('since'), String(NOW - 30 * 86400));
  const all = params(logic.buildRecentQuery(state({ range: 'all' }), NOW));
  assert.equal(all.get('range'), null, '全部时间不带窗口参数');
  assert.equal(all.get('since'), null);
  assert.equal(params(logic.buildRecentQuery(state({ realm: 'cn' }), NOW)).get('realm'), 'cn');
  assert.equal(params(logic.buildRecentQuery(state({ key: 'panel' }), NOW)).get('key'), 'panel');
  assert.equal(params(logic.buildRecentQuery(state({ status: 'fail' }), NOW)).get('status'), 'fail');
  assert.equal(params(logic.buildRecentQuery(state({ model: '  DeepSeek  ' }), NOW)).get('model'), 'DeepSeek', '模型名去掉首尾空格');
  assert.equal(params(logic.buildRecentQuery(state({ account: 'uid-intl-a' }), NOW)).get('account'), 'uid-intl-a');
  assert.equal(params(logic.buildRecentQuery(state({ ip: '192.168.' }), NOW)).get('ip'), '192.168.');

  const combo = params(logic.buildRecentQuery(
    state({ range: '24h', realm: 'intl', key: 'k1', status: 'ok', model: 'glm', account: 'uid-cn-a', ip: '10.0.' }, { page: 3, limit: 50 }),
    NOW,
  ));
  assert.equal(combo.get('since'), String(NOW - 86400));
  assert.equal(combo.get('realm'), 'intl');
  assert.equal(combo.get('key'), 'k1');
  assert.equal(combo.get('status'), 'ok');
  assert.equal(combo.get('model'), 'glm');
  assert.equal(combo.get('account'), 'uid-cn-a');
  assert.equal(combo.get('ip'), '10.0.');
  assert.equal(combo.get('page'), '3');
  assert.equal(combo.get('limit'), '50');

  const bogus = params(logic.buildRecentQuery(state({ realm: 'eu' }), NOW));
  assert.equal(bogus.get('realm'), 'all', '取值不在登记表里时退回全部');
}

// ---- 筛选变化与页码
{
  const started = state({ realm: 'cn' }, { page: 7, pages: 9 });
  const changed = logic.withFilter(started, 'status', 'fail');
  assert.equal(changed.page, 1, '筛选变化必须回到第 1 页');
  assert.equal(changed.filters.status, 'fail');
  assert.equal(started.page, 7, '原状态不被就地修改');
  assert.equal(logic.withFilter(started, 'realm', 'cn').page, 1, '同值也不能把页码停在旧页');

  assert.equal(logic.withLimit(started, 100).limit, 100);
  assert.equal(logic.withLimit(started, 100).page, 1);
  assert.equal(logic.withLimit(started, '7').limit, 20, '非法每页条数回到 20');
  assert.equal(logic.normalizeLimit('50'), 50);
  assert.equal(logic.normalizeLimit(undefined), 20);

  assert.equal(logic.withPage(state({}, { pages: 5 }), 3).page, 3);
  assert.equal(logic.withPage(state({}, { pages: 5 }), 99).page, 5);
  assert.equal(logic.withPage(state({}, { pages: 5 }), 0).page, 1);
  assert.throws(() => logic.changeFilter(logic.defaultFilters(), 'days', '1'), /unknown filter/);
}

// ---- 是否处于筛选状态
{
  assert.equal(logic.isFiltered(logic.defaultFilters()), false);
  assert.equal(logic.isFiltered({ ...logic.defaultFilters(), range: 'all' }), true);
  assert.equal(logic.isFiltered({ ...logic.defaultFilters(), ip: '10.0.' }), true);
  assert.equal(logic.isFiltered(logic.changeFilter(logic.defaultFilters(), 'status', 'ok')), true);
}

// ---- 初始筛选的版本跟随右上角「查看的版本」
{
  assert.equal(logic.defaultFilters().realm, 'all');
  assert.equal(logic.defaultFilters('cn').realm, 'cn');
  assert.equal(logic.defaultFilters('intl').realm, 'intl');
  assert.equal(logic.defaultFilters('eu').realm, 'all', '未知版本回到全部');
  const base = logic.defaultFilters('cn');
  assert.equal(logic.isFiltered(base, base), false, '跟随版本的默认条件不算已筛选');
  assert.equal(logic.isFiltered({ ...base, model: 'glm' }, base), true);
  assert.equal(params(logic.buildRecentQuery({ filters: base, page: 1 }, NOW)).get('realm'), 'cn');
}

// ---- 刷新后清掉失效的筛选值
{
  const current = logic.defaultFilters();
  const dropped = logic.pruneFilters({ ...current, key: 'gone', account: 'uid-gone' }, { keyIds: ['k1'], accountIds: ['uid-intl-a'] });
  assert.equal(dropped.key, '', '密钥已被删除时清空该筛选');
  assert.equal(dropped.account, '');
  const kept = logic.pruneFilters({ ...current, key: 'k1', account: 'uid-intl-a' }, { keyIds: ['k1'], accountIds: ['uid-intl-a'] });
  assert.equal(kept.key, 'k1');
  assert.equal(kept.account, 'uid-intl-a');
  assert.equal(logic.pruneFilters({ ...current, range: '24h' }).range, '24h', '其它筛选不受影响');
}

// ---- 状态分档：2xx 成功、4xx 警告、其余危险
{
  assert.deepEqual(logic.statusInfo({ status: 200, outcome: 'completed' }).tone, 'success');
  assert.equal(logic.statusInfo({ status: 201 }).text, '201');
  assert.equal(logic.statusInfo({ status: 429 }).tone, 'warning');
  assert.equal(logic.statusInfo({ status: 404 }).tone, 'warning');
  assert.equal(logic.statusInfo({ status: 502 }).tone, 'danger');
  assert.equal(logic.statusInfo({ status: 302 }).tone, 'danger', '3xx 不属于成功也不属于用户错误');

  // 旧记录缺状态码时按 outcome 推算并显示结果文字
  assert.deepEqual(logic.statusInfo({ outcome: 'completed' }), { code: 200, tone: 'success', text: '完成', title: '旧记录没有状态码，按成功归档' });
  assert.equal(logic.statusInfo({ outcome: 'client_aborted' }).tone, 'warning');
  assert.equal(logic.statusInfo({ outcome: 'upstream_aborted' }).tone, 'danger');
  const legacyFailure = logic.statusInfo({ error: 'upstream 502 bad gateway' });
  assert.equal(legacyFailure.tone, 'danger');
  assert.equal(legacyFailure.text, '失败');
  assert.equal(logic.statusInfo({ status: 0, outcome: 'completed' }).text, '完成', '状态码 0 不是有效状态码');
  assert.equal(logic.statusInfo({ status: 'abc' }).tone, 'success', '状态码读不出数字时按 outcome 推算');
  assert.equal(logic.rowOutcome({ error: 'x' }), 'failed');
  assert.equal(logic.rowOutcome({}), 'completed');
  assert.equal(logic.rowOutcome({ outcome: 'client_aborted', error: 'x' }), 'client_aborted');
}

// ---- 密钥显示名：key_name 优先，其次密钥列表与固定说明
{
  const keys = { k1: { id: 'k1', name: '客服组' } };
  assert.equal(logic.keyLabel({ key_id: 'k1', key_name: '客服组' }, keys), '客服组');
  assert.equal(logic.keyLabel({ key_id: 'k1' }, keys), '客服组', '网关没给名字时用当前密钥列表解析');
  assert.equal(logic.keyLabel({ key_id: 'panel' }, keys), '面板测试台');
  assert.equal(logic.keyLabel({ key_id: 'k9' }, keys), '已删除的密钥');
  assert.equal(logic.keyLabel({ key_id: '' }, keys), '未使用密钥');
  assert.equal(logic.keyLabel({}, keys), '未使用密钥');
  assert.equal(logic.keyLabel({ key_id: 'k1', key_name: '  已删除的 Key  ' }, keys), '已删除的 Key', '网关给的名字原样采信');
}

// ---- 账号显示名：昵称优先，账号被删除后退回 uid 前 8 位
{
  const accounts = { 'uid-intl-a': { uid: 'uid-intl-a', nickname: 'Alice 主号' } };
  assert.equal(logic.accountLabel('uid-intl-a', accounts), 'Alice 主号');
  assert.equal(logic.accountLabel('0123456789abcdef', accounts), '01234567');
  assert.equal(logic.accountLabel('', accounts), '未采集');
  assert.equal(logic.accountLabel(undefined, accounts), '未采集');
}

// ---- Token 单元：缓存命中率与提示词缓存提示
{
  const row = { prompt_tokens: 1000, cached_tokens: 425, cache_hit_pct: 42.5 };
  const hit = logic.cacheHint(row);
  assert.equal(hit.text, '缓存 42.5%');
  assert.equal(hit.tone, 'success');
  assert.equal(hit.title, '提示词缓存：命中 425 Token，未命中 575 Token');
  assert.equal(logic.cacheHint({ prompt_tokens: 1000, cached_tokens: 0, cache_hit_pct: 0 }).text, '未命中');
  assert.equal(logic.cacheHint({ cache_hit_pct: null }).text, '—');
  assert.equal(logic.cacheHint({}).tone, 'muted');
  assert.match(logic.cacheHint({ prompt_tokens: 10, cached_tokens: 20 }).title, /命中 20 Token，未命中 0 Token/, '缓存数大于提示词数也不出现负数');
}

// ---- 积分：缺项显示占位并说明原因
{
  assert.deepEqual(logic.creditText({ credit: 0 }), { text: '0', title: '本次请求在上游的实际扣费', missing: false });
  assert.equal(logic.creditText({ credit: 1.23456 }).text, '1.23');
  assert.equal(logic.creditText({ credit: 0.42 }).text, '0.4200', '不足 1 积分保留四位小数');
  assert.equal(logic.creditText({}).text, '—');
  assert.equal(logic.creditText({}).title, '上游未返回该项');
  assert.equal(logic.creditText({ credit: 'x' }).missing, true);
}

// ---- 思考档位：只有带该字段的行才有值
{
  assert.equal(logic.effortLabel({ reasoning_effort: 'high' }), 'high');
  assert.equal(logic.effortLabel({ reasoning_effort: ' none ' }), 'none', '去掉首尾空格');
  assert.equal(logic.effortLabel({ reasoning_effort: '' }), '');
  assert.equal(logic.effortLabel({ reasoning_effort: null }), '');
  assert.equal(logic.effortLabel({}), '');
  assert.equal(logic.effortLabel(undefined), '');
}

// ---- 时间：秒级时间戳优先；缺失时按 iso 解析
{
  assert.equal(logic.rowTimestamp({ at: 1700000000 }), 1700000000);
  assert.equal(logic.rowTimestamp({ at: 1700000000000 }), 1700000000000);
  assert.equal(logic.rowTimestamp({ iso: '2026-10-03T01:02:03' }), new Date(2026, 9, 3, 1, 2, 3).getTime() / 1000);
  assert.equal(logic.rowTimestamp({}), null);
}

// ---- 页码列表：超过 7 页时省略中间
{
  assert.deepEqual(logic.pageList(1, 1), [1]);
  assert.deepEqual(logic.pageList(1, 7), [1, 2, 3, 4, 5, 6, 7]);
  assert.deepEqual(logic.pageList(1, 20), [1, 2, 3, 4, 5, '...', 20]);
  assert.deepEqual(logic.pageList(10, 20), [1, '...', 8, 9, 10, 11, 12, '...', 20]);
  assert.deepEqual(logic.pageList(20, 20), [1, '...', 16, 17, 18, 19, 20]);
  assert.deepEqual(logic.pageList(99, 3), [1, 2, 3], '超出范围的页码退回最后一页');
}

// ---- 详情弹窗字段：缺项有说明，失败记录另有错误行
{
  const row = {
    at: 1700000000, iso: '2026-10-03T01:02:03', api: 'chat', realm: 'intl', model: 'deepseek-v4.1-flash',
    key_id: 'k1', key_name: '客服组', ip: '192.168.1.20', account: 'uid-intl-a', status: 200, outcome: 'completed',
    stream: true, ttft_ms: 420, elapsed_ms: 2400, gen_ms: 1980, tokens_per_sec: 42.5,
    prompt_tokens: 1000, completion_tokens: 300, reasoning_tokens: 12, cached_tokens: 425, total_tokens: 1300,
    cache_hit_pct: 42.5, credit: 0.42, reasoning_effort: 'high',
  };
  const ctx = { keysById: { k1: { id: 'k1', name: '客服组' } }, accountsById: { 'uid-intl-a': { uid: 'uid-intl-a', nickname: 'Alice 主号' } } };
  const detail = logic.requestDetail(row, ctx);
  const byLabel = Object.fromEntries(detail.map((item) => [item.label, item]));
  assert.equal(byLabel['API 密钥'].value, '客服组');
  assert.equal(byLabel['API 密钥'].copy, undefined, '密钥名称一行不复制，避免把编号当成密钥');
  assert.equal(byLabel['密钥 ID'].copy, 'k1', '密钥 ID 复制的是筛选用的 key id');
  assert.equal(byLabel['来源 IP'].copy, '192.168.1.20');
  assert.equal(byLabel['账号'].value, 'Alice 主号');
  assert.equal(byLabel['账号 UID'].copy, 'uid-intl-a');
  assert.equal(byLabel['模型'].copy, 'deepseek-v4.1-flash');
  assert.equal(byLabel['思考档位'].value, '高（high）');
  assert.equal(byLabel['请求结果'].value, '完成 · HTTP 200');
  assert.equal(byLabel['返回方式'].value, '流式');
  assert.equal(byLabel['首字延迟'].value, '420ms');
  assert.equal(byLabel['总耗时'].value, '2.40s');
  assert.equal(byLabel['输入 Token'].value, '1,000');
  assert.equal(byLabel['合计'].value, '1,300');
  assert.equal(byLabel['缓存命中率'].value, '42.5%');
  assert.equal(byLabel['积分消耗'].value, '0.4200');
  assert.equal(byLabel['积分消耗'].copy, undefined, '积分不提供复制按钮');
  assert.equal(byLabel['错误原因'], undefined, '成功的记录没有错误行');

  const failure = logic.requestDetail({ ...row, status: 502, outcome: 'failed', error: 'upstream 502 bad gateway' }, ctx);
  const failedByLabel = Object.fromEntries(failure.map((item) => [item.label, item]));
  assert.equal(failedByLabel['请求结果'].value, '失败 · HTTP 502');
  assert.equal(failedByLabel['错误原因'].value, 'upstream 502 bad gateway');

  const panel = Object.fromEntries(logic.requestDetail({ ...row, key_id: 'panel', key_name: '' }, ctx).map((item) => [item.label, item.value]));
  assert.equal(panel['API 密钥'], '面板测试台');
  assert.equal(panel['密钥 ID'], undefined, '面板测试台没有密钥编号');
  assert.equal(logic.effortText({ reasoning_effort: 'none' }), '关闭思考（none）');
  assert.equal(logic.effortText({ reasoning_effort: 'turbo' }), 'turbo', '不认识的档位照原样显示');

  const legacy = Object.fromEntries(logic.requestDetail({ at: 1700000000 }, {}).map((item) => [item.label, item.value]));
  assert.equal(legacy['请求结果'], '完成 · HTTP 200（旧记录没有状态码，按结果推断）');
  assert.equal(legacy['API 密钥'], '未使用密钥');
  assert.equal(legacy['密钥 ID'], undefined);
  assert.equal(legacy['来源 IP'], '未采集');
  assert.equal(legacy['账号'], '未采集');
  assert.equal(legacy['账号 UID'], undefined);
  assert.equal(legacy['模型'], '—');
  assert.equal(legacy['思考档位'], '没有记录');
  assert.equal(legacy['缓存命中率'], '—');
  assert.equal(legacy['返回方式'], '非流式');
  assert.equal(legacy['积分消耗'], '—');
}

// ---- 接口名称：认识的翻成中文，其余原样显示
{
  assert.equal(logic.apiLabel('chat'), 'Chat Completions 接口');
  assert.equal(logic.apiLabel('messages'), 'Messages 接口');
  assert.equal(logic.apiLabel('brand-new-api'), 'brand-new-api');
  assert.equal(logic.apiLabel(''), '—');
  assert.equal(logic.outcomeLabel('upstream_aborted'), '上游中断');
  assert.equal(logic.outcomeLabel(''), '—');
}

console.log('requests logic assertions passed');
