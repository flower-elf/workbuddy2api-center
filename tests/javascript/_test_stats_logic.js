/* 用量页纯逻辑：查询串组装、账号与密钥用量行、模型矩阵的行与合计、筛选条件与失效清理。
 * 关键行为：合计只在可见行上重算，延迟与速度按样本数加权。 */
import assert from 'node:assert/strict';
import {
  RANGES, rangeLabel, scopeRealm, scopeText, buildQuery, trendQuery, trendGranularity, TREND_MODES, trendSeries,
  accountRows, keyRows, idleKeyNames, matrixOptions, reconcileFilters, buildMatrix, allocateShares,
} from '../../app/web/js/pages/stats/logic.js';

const S = (avg, samples = 1) => ({ avg, p50: avg, p90: avg, p95: avg, p99: avg, samples });
const stat = (requests, total, prompt, completion, extra = {}) => Object.assign({
  requests, total_tokens: total, prompt_tokens: prompt, completion_tokens: completion,
  reasoning_tokens: 0, cached_tokens: 0, accounts: {},
}, extra);

function fixtures() {
  const deepseek = stat(3, 5800, 4000, 1800, { reasoning_tokens: 100, cached_tokens: 1000, accounts: { 'acct-A': 3 } });
  const glm = stat(1, 2500, 1000, 1500, { reasoning_tokens: 200, cached_tokens: 500, accounts: { 'acct-B': 1 } });
  return {
    usage: {
      requests: 4,
      total_tokens: 8300,
      prompt_tokens: 5000,
      completion_tokens: 3300,
      reasoning_tokens: 300,
      accounts_map: { 'acct-A': { nickname: 'Alice', realm: 'intl' }, 'acct-B': { nickname: 'Bob', realm: 'cn' } },
      by_model: { 'deepseek-v4.1-flash': deepseek, 'glm-5.3': glm },
      by_model_realm: { 'deepseek-v4.1-flash': { intl: deepseek }, 'glm-5.3': { cn: glm } },
      by_model_acct: { 'deepseek-v4.1-flash': { intl: { 'acct-A': deepseek } }, 'glm-5.3': { cn: { 'acct-B': glm } } },
    },
    perf: {
      errors: 1,
      ttft_ms: { avg: 620, p50: 600, p90: 750, p95: 900, p99: 1300, samples: 4 },
      by_model: {
        'deepseek-v4.1-flash': { requests: 3, errors: 1, ttft_ms: S(600), tokens_per_sec: S(40), wall_ms: S(3000), cache_hit_pct: S(25) },
        'glm-5.3': { requests: 1, errors: 0, ttft_ms: S(900), tokens_per_sec: S(20), wall_ms: S(5000), cache_hit_pct: S(50) },
      },
      by_model_realm: {
        'deepseek-v4.1-flash': { intl: { requests: 3, errors: 1, ttft_ms: S(600), tokens_per_sec: S(40), wall_ms: S(3000), cache_hit_pct: S(25) } },
        'glm-5.3': { cn: { requests: 1, errors: 0, ttft_ms: S(900), tokens_per_sec: S(20), wall_ms: S(5000), cache_hit_pct: S(50) } },
      },
      by_model_acct: {
        'deepseek-v4.1-flash': { intl: { 'acct-A': { requests: 3, errors: 1, ttft_ms: { avg: 600, p50: 500, p90: 700, p95: 800, p99: 1200, samples: 3 }, tokens_per_sec: S(40), wall_ms: S(3000), cache_hit_pct: S(25) } } },
        'glm-5.3': { cn: { 'acct-B': { requests: 1, errors: 0, ttft_ms: S(900), tokens_per_sec: S(20), wall_ms: S(5000), cache_hit_pct: S(50) } } },
      },
    },
  };
}

// ---- 查询串：三个端点共用窗口，自定义区间只带填了的一侧
assert.equal(buildQuery({ realm: 'intl', range: 'today' }), '?realm=intl&range=today');
assert.equal(buildQuery({ realm: 'all', range: 'custom', since: null, until: null }), '?realm=all&range=custom');
assert.equal(buildQuery({ realm: 'cn', range: 'custom', since: 1700000000, until: null }), '?realm=cn&range=custom&since=1700000000');
assert.equal(buildQuery({ realm: 'all', range: 'week', since: 1, until: 2 }), '?realm=all&range=week', '非自定义区间不带时间边界');
assert.equal(trendQuery({ realm: 'intl', days: 14 }), '?realm=intl&days=14');
assert.ok(trendQuery({ realm: 'cn', range: 'custom', since: 5, until: 9 }).includes('since=5'));
assert.equal(trendGranularity('today'), trendGranularity('today'));
assert.notEqual(trendGranularity('today'), trendGranularity('week'));
assert.equal(scopeRealm('cn', false), 'cn');
assert.equal(scopeRealm('cn', true), 'all');
assert.notEqual(scopeText(false, 'cn'), scopeText(false, 'intl'));
assert.equal(rangeLabel('week'), RANGES[1].label);
assert.equal(rangeLabel('不存在'), RANGES[0].label, '未知范围退回默认项');

// ---- 未筛选时合计取端点整体口径
const base = fixtures();
let built = buildMatrix(base.usage, base.perf, {});
assert.equal(built.summary.filtering, false);
assert.equal(built.summary.requests, 4);
assert.equal(built.summary.totalTokens, 8300);
assert.equal(built.summary.errors, 1);
assert.equal(built.summary.ttftAvg, 620, '合计的延迟取端点整体的桶');
assert.equal(built.summary.ttftP50, 600);
assert.equal(built.summary.ttftP90, 750);
assert.equal(built.summary.ttftP95, 900);
assert.equal(built.summary.ttftP99, 1300);
assert.equal(built.modelCount, 2);
assert.equal(built.rows.length, 2, '两个账号各自的模型各占一行');
assert.deepEqual(built.rows.map((r) => r.id).sort(), ['deepseek-v4.1-flash', 'glm-5.3']);
const deepseekRow = built.rows.find((r) => r.id === 'deepseek-v4.1-flash');
assert.equal(deepseekRow.acct, 'acct-A');
assert.equal(deepseekRow.realm, 'intl');
assert.equal(deepseekRow.ttftAvg, 600, '按账号的性能桶优先');
assert.equal(deepseekRow.ttftP50, 500, 'P50 直接取桶里的分位数');
assert.equal(deepseekRow.ttftP90, 700, 'P90 直接取桶里的分位数');
assert.equal(deepseekRow.ttftP95, 800, 'P95 直接取桶里的分位数');
assert.equal(deepseekRow.ttftP99, 1200, 'P99 直接取桶里的分位数');
assert.equal(deepseekRow.cachePct, 25, '提示词缓存比例取自这一账号的桶');
assert.equal(deepseekRow.share, Math.round((5800 / 8300) * 100), '占比按表内合计 8300 算');
assert.equal(built.rows.find((r) => r.id === 'glm-5.3').share, Math.round((2500 / 8300) * 100));
assert.equal(built.rows.reduce((sum, r) => sum + r.share, 0), 100, '各行占比加起来正好 100');

// ---- 占比分配：零头给余数最大的行，合计 100
assert.deepEqual(allocateShares([7, 11, 13, 17]), [15, 23, 27, 35]);
assert.deepEqual(allocateShares([101, 99]), [51, 49], '两个近乎对半的行相差 1');
assert.deepEqual(allocateShares([1, 1, 1]), [34, 33, 33], '余数相同时给靠前的行');
assert.deepEqual(allocateShares([1, 0, 0]), [100, 0, 0]);
assert.deepEqual(allocateShares([0, 0]), [0, 0], '没有用量时都是 0');
assert.deepEqual(allocateShares([]), []);

// ---- 同一模型的多个账号各自成行
const splitUsage = {
  requests: 2, total_tokens: 300, prompt_tokens: 0, completion_tokens: 0, reasoning_tokens: 0,
  by_model: { 'm-one': stat(2, 300, 0, 0, { accounts: { 'acct-A': 1, 'acct-B': 1 } }) },
  by_model_realm: { 'm-one': { intl: stat(2, 300, 0, 0) } },
  by_model_acct: { 'm-one': { intl: { 'acct-A': stat(1, 200, 0, 0), 'acct-B': stat(1, 100, 0, 0) } } },
};
const splitBuilt = buildMatrix(splitUsage, {}, {});
assert.deepEqual(splitBuilt.rows.map((r) => r.acct), ['acct-A', 'acct-B'], '账号按用量降序');
assert.deepEqual(splitBuilt.rows.map((r) => r.split), [true, true]);
assert.deepEqual(splitBuilt.rows.map((r) => r.first), [true, false]);
assert.deepEqual(splitBuilt.rows.map((r) => r.splitAccounts), [2, 2], '两行都记着账号数，只有第一行显示');
assert.equal(built.rows[0].split, false, '单账号模型不算分行');

// ---- 按模型筛选
const onlyGlm = buildMatrix(base.usage, base.perf, { model: 'glm-5.3' });
assert.equal(onlyGlm.summary.filtering, true);
assert.notEqual(onlyGlm.summary.label, built.summary.label, '筛选后合计行的标题与全量不同');
assert.equal(onlyGlm.rows.length, 1);
assert.equal(onlyGlm.summary.requests, 1);
assert.equal(onlyGlm.summary.totalTokens, 2500);
assert.equal(onlyGlm.summary.ttftAvg, 900);
assert.equal(onlyGlm.modelCount, 1);
assert.equal(onlyGlm.rows[0].share, 100, '筛选后只剩这一行，占比 100');

// ---- 按账号筛选
const onlyA = buildMatrix(base.usage, base.perf, { account: 'acct-A' });
assert.equal(onlyA.rows.length, 1);
assert.equal(onlyA.rows[0].acct, 'acct-A');
assert.equal(onlyA.summary.requests, 3);
assert.equal(onlyA.summary.totalTokens, 5800);
assert.equal(onlyA.summary.errors, 1);

// ---- 筛不出行时返回空结果
const none = buildMatrix(base.usage, base.perf, { account: 'acct-B', model: 'deepseek-v4.1-flash' });
assert.deepEqual(none.rows, []);
assert.equal(none.modelCount, 0);
assert.equal(none.summary.requests, 0);
assert.equal(none.summary.totalTokens, 0);

// ---- 性能桶的取值顺序：账号桶、出口桶、模型桶
const layered = fixtures();
layered.perf.by_model_acct['deepseek-v4.1-flash'].intl['acct-A'].ttft_ms = S(111);
layered.perf.by_model_realm['deepseek-v4.1-flash'].intl.ttft_ms = S(222);
assert.equal(buildMatrix(layered.usage, layered.perf, {}).rows.find((r) => r.id === 'deepseek-v4.1-flash').ttftAvg, 111);
delete layered.perf.by_model_acct['deepseek-v4.1-flash'].intl['acct-A'];
delete layered.perf.by_model_acct['deepseek-v4.1-flash'].intl;
assert.equal(buildMatrix(layered.usage, layered.perf, {}).rows.find((r) => r.id === 'deepseek-v4.1-flash').ttftAvg, 222);
delete layered.perf.by_model_realm['deepseek-v4.1-flash'].intl;
layered.perf.by_model['deepseek-v4.1-flash'].ttft_ms = S(333);
assert.equal(buildMatrix(layered.usage, layered.perf, {}).rows.find((r) => r.id === 'deepseek-v4.1-flash').ttftAvg, 333);

// ---- 筛选后的合计：冷门行不能压过主力行
const weighted = fixtures();
const many = stat(9, 900, 500, 400, { accounts: { 'acct-B': 9 } });
weighted.usage.by_model_acct['glm-5.3'].cn['acct-C'] = many;
weighted.usage.by_model_acct['glm-5.3'].cn['acct-B'] = stat(1, 100, 50, 50, { accounts: { 'acct-B': 1 } });
weighted.perf.by_model_acct['glm-5.3'].cn['acct-C'] = { requests: 9, errors: 0, tokens_per_sec: S(10, 9) };
weighted.perf.by_model_acct['glm-5.3'].cn['acct-B'] = { requests: 1, errors: 0, tokens_per_sec: S(100, 1) };
const filtered = buildMatrix(weighted.usage, weighted.perf, { model: 'glm-5.3' });
assert.equal(filtered.rows.length, 2);
assert.equal(Math.round(filtered.summary.tps * 10) / 10, 19, '加权平均为 19，直接平均会得到 55');

// ---- 被筛选掉的账号不出现在行里
assert.equal(filtered.summary.accountsLabel, '已筛选');
assert.equal(buildMatrix(base.usage, base.perf, {}).summary.accountsLabel, '全部账号');

// ---- 筛选下拉的选项来自数据本身
const options = matrixOptions(base.usage);
assert.deepEqual(options.accounts.map((a) => a.uid), ['acct-A', 'acct-B']);
assert.deepEqual(options.models, ['deepseek-v4.1-flash', 'glm-5.3']);
const junk = fixtures();
junk.usage.by_model['glm-5.3-model'] = stat(2, 10, 5, 5);
junk.usage.by_model['无调用的模型'] = stat(0, 0, 0, 0);
assert.deepEqual(matrixOptions(junk.usage).models, ['deepseek-v4.1-flash', 'glm-5.3'], '被占位名与零调用过滤掉');
assert.deepEqual(matrixOptions(null), { accounts: [], models: [] });

// ---- 换范围后失效的筛选被清空
assert.deepEqual(reconcileFilters({ account: 'acct-A', model: 'glm-5.3' }, options), { account: 'acct-A', model: 'glm-5.3' });
const narrowed = matrixOptions({ by_model: { 'deepseek-v4.1-flash': stat(1, 1, 1, 0) }, by_model_acct: { 'deepseek-v4.1-flash': { intl: { 'acct-A': stat(1, 1, 1, 0) } } } });
assert.deepEqual(reconcileFilters({ account: 'acct-A', model: 'glm-5.3' }, narrowed), { account: 'acct-A', model: '' });
assert.deepEqual(reconcileFilters({ account: '已删除的账号', model: 'glm-5.3' }, options), { account: '', model: 'glm-5.3' });

// ---- 账号用量行：零调用未归属桶丢掉，按 Token 降序
const rows = accountRows({
  accounts: [
    { uid: '(unattributed)', nickname: '(unattributed)', realm: 'intl', window: stat(0, 0, 0, 0), all_time: stat(0, 0, 0, 0), window_models: {} },
    { uid: 'small', nickname: '小号', realm: 'intl', window: stat(1, 10, 5, 5), all_time: stat(1, 10, 5, 5), window_models: { b: { requests: 1, tokens: 10 } } },
    { uid: 'big', nickname: '大号', realm: 'cn', credits: { remain: 500, size: 900 }, window: stat(2, 900, 400, 500), all_time: stat(2, 900, 400, 500),
      window_models: { b: { requests: 1, tokens: 100 }, a: { requests: 1, tokens: 800 } } },
  ],
});
assert.deepEqual(rows.map((r) => r.uid), ['big', 'small']);
assert.deepEqual(rows[0].models.map((m) => m.id), ['a', 'b']);
assert.equal(rows[0].credits.remain, 500);
assert.deepEqual(accountRows(null), []);
// 未归属桶窗口内无调用但历史有记录，按真实记录保留
const kept = accountRows({ accounts: [{ uid: '(unattributed)', window: stat(0, 0, 0, 0), all_time: stat(4, 40, 20, 20), window_models: {} }] });
assert.equal(kept.length, 1);

// ---- 密钥用量行：零调用的密钥不进表，在下沿点名
const keys = keyRows({
  keys: [
    { key_id: 'k1', key_name: '客服组', window: stat(3, 300, 200, 100, { cache_hit_pct: 25, ttft_ms_avg: 700 }), all_time: stat(9, 900, 600, 300) },
    { key_id: 'k2', key_name: '内部测试', window: stat(1, 100, 50, 50), all_time: stat(1, 100, 50, 50) },
    { key_id: 'k3', key_name: '闲置密钥', window: stat(0, 0, 0, 0), all_time: stat(5, 500, 300, 200) },
    { key_id: 'panel', window: stat(0, 0, 0, 0), all_time: stat(0, 0, 0, 0) },
  ],
});
assert.deepEqual(keys.map((k) => k.keyId), ['k1', 'k2']);
assert.equal(keys[0].cacheHitPct, 25);
assert.equal(keys[0].ttftAvg, 700);
assert.equal(keys[0].share, 75, '占表内合计 400 里的 300');
assert.equal(keys[1].share, 25, '100 / 400');
assert.deepEqual(idleKeyNames({ keys: [
  { key_id: 'k3', key_name: '闲置密钥', window: stat(0, 0, 0, 0) },
  { key_id: 'panel', window: stat(0, 0, 0, 0) },
] }), ['闲置密钥', 'panel'], '没有名字的密钥用 id 显示');
assert.deepEqual(keyRows(null), []);

// ---- 趋势序列：请求数左轴、Token 右轴、失败数跟随请求数
const buckets = [
  { label: '10-01', requests: 3, errors: 1, total_tokens: 1200 },
  { label: '10-02', requests: 5, errors: 0, total_tokens: 3400 },
];
const both = trendSeries('both', buckets, String, (v) => v + 't');
assert.deepEqual(both.labels, ['10-01', '10-02']);
assert.deepEqual(both.series.map((s) => s.label), ['请求数', 'Token']);
assert.deepEqual(both.series[0].data, [3, 5]);
assert.deepEqual(both.series[1].data, [1200, 3400]);
assert.equal(both.series[0].axis, undefined, '请求数用默认左轴');
assert.equal(both.series[1].axis, 'y2', 'Token 用右轴');
assert.equal(both.series[0].color, '--success', '请求数绿色');
assert.equal(both.series[1].color, '--info', 'Token 蓝色');
assert.equal(both.series[1].dashed, undefined, 'Token 用实线');
assert.equal(both.series[0].fill, undefined, '请求数不带填充');
assert.equal(both.series[1].fill, undefined, 'Token 也不带填充');
assert.ok(both.series[0].order > both.series[1].order, '请求数先画、Token 压在上面');
const withErrors = trendSeries('both', buckets, String, String, { errors: true });
assert.deepEqual(withErrors.series.map((s) => s.label), ['请求数', '失败数', 'Token']);
assert.deepEqual(withErrors.series[1].data, [1, 0]);
assert.equal(withErrors.series[1].dashed, true);
assert.equal(withErrors.series[1].color, '--destructive', '失败数红色');
assert.equal(withErrors.series[1].axis, undefined, '失败数与请求数共用左轴');
assert.ok(withErrors.series[0].order > withErrors.series[1].order && withErrors.series[1].order > withErrors.series[2].order, '叠放次序请求数、失败数、Token');
assert.deepEqual(trendSeries('requests', buckets, String, String, { errors: true }).series.map((s) => s.label), ['请求数', '失败数']);
const onlyRequests = trendSeries('requests', buckets, String, String);
assert.deepEqual(onlyRequests.series.map((s) => s.label), ['请求数']);
const onlyTokens = trendSeries('tokens', buckets, String, String);
assert.deepEqual(onlyTokens.series.map((s) => s.label), ['Token']);
assert.equal(onlyTokens.series[0].axis, undefined, '单独画 Token 时占左轴');
assert.deepEqual(trendSeries('both', null, String, String).labels, []);

console.log('stats logic assertions passed');
