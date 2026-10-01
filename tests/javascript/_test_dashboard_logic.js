/* 仪表盘纯逻辑：版本筛选、可用性分档与优先级、积分汇总与提示优先级、健康快照挑选、出口密钥计数、趋势拆分。
 * 只测可感知的行为：分档谁压过谁、合计覆盖了哪些账号、快照先展示哪一个、几把密钥真正生效。 */
import assert from 'node:assert/strict';
import {
  realmOf, realmAccounts, remainSeconds, isExpired, isReady, availability,
  availabilityCounts, accountSummary, readyHint, creditsSummary, accountExpiries, nextCreditExpiry,
  creditsHint, snapshotAccounts, creditBar, expiryVisual, creditTone, keyEffect, usageHints, trendSummary,
} from '../../app/web/js/pages/dashboard/logic.js';
import { fmtCredit } from '../../app/web/js/core/format.js';

const DAY = 86400;
const now = new Date(2026, 9, 3, 12, 0, 0).getTime();
const sec = (ms) => Math.floor(ms / 1000);

function account(patch) {
  return Object.assign({
    uid: 'uid-1', nickname: '一号', realm: 'intl', enabled: true,
    expiresAt: sec(now + 20 * DAY), credits: { remain: 1000, size: 2000 },
    reserveCredits: 0, reserveBlocked: false, dailyLimitBlocked: false, inCooldown: false, lastError: '',
  }, patch);
}

// ---- 版本筛选：没有 realm 的存量账号按国际版处理，国内版视图不包含它
assert.equal(realmOf({}), 'intl');
assert.equal(realmOf({ realm: 'cn' }), 'cn');
const mixed = [account({ uid: 'a' }), account({ uid: 'b', realm: '' }), account({ uid: 'c', realm: 'cn' })];
assert.deepEqual(realmAccounts(mixed, 'intl').map((a) => a.uid), ['a', 'b']);
assert.deepEqual(realmAccounts(mixed, 'cn').map((a) => a.uid), ['c']);

// ---- 剩余时间与过期：没有到期时间不算过期，也不能算出剩余时间
assert.equal(remainSeconds(account({ expiresAt: 0 }), now), null);
assert.equal(isExpired(account({ expiresAt: 0 }), now), false);
assert.equal(remainSeconds(account({ expiresAt: sec(now) + 90 }), now), 90);
assert.equal(isExpired(account({ expiresAt: sec(now) - 1 }), now), true);

// ---- 可用性分档的优先级：停用压过过期，过期压过冷却
const disabled = availability(account({ enabled: false, expiresAt: sec(now) - 1, inCooldown: true }), now);
const disabledOnly = availability(account({ enabled: false }), now);
assert.equal(disabled.label, disabledOnly.label, '停用优先于过期与冷却');
const expired = availability(account({ expiresAt: sec(now) - 1, inCooldown: true }), now);
assert.notEqual(expired.label, disabledOnly.label);
const cooling = availability(account({ inCooldown: true }), now);
assert.notEqual(cooling.label, expired.label);
assert.notEqual(availability(account({}), now).label, cooling.label, '冷却中与可用的档位不同');
// 低于保留积分的账号不能接流
assert.equal(isReady(account({ reserveBlocked: true }), now), false);
assert.equal(isReady(account({ dailyLimitBlocked: true }), now), false);
assert.equal(isReady(account({ inCooldown: true }), now), false);
assert.equal(isReady(account({ enabled: false }), now), false);
assert.equal(isReady(account({}), now), true);

// ---- 账号总数与需处理数：需处理 = 总数减可用，停用的账号也在内
const summarySource = [
  account({ uid: 'ok' }),
  account({ uid: 'cool', inCooldown: true }),
  account({ uid: 'off', enabled: false }),
];
const summary = accountSummary(summarySource, now);
assert.equal(summary.total, 3);
assert.equal(summary.enabled, 2);
assert.equal(summary.ready, 1);
assert.equal(summary.attention, 2);
assert.notEqual(readyHint(summary), readyHint(accountSummary([account({ uid: 'x' })], now)));
assert.ok(readyHint(summary).includes(String(summary.attention)), '提示里带上需处理的账号数');

// ---- 档位汇总按固定顺序，只列出现的档位
const counts = availabilityCounts(summarySource, now);
assert.equal(counts.length, 3);
assert.equal(counts[0].label, availability(account({}), now).label, '可用排在最前');
assert.equal(counts.reduce((sum, c) => sum + c.count, 0), 3);

// ---- 积分汇总：未知余额不计入覆盖账号数，低于保留积分单独计数
const credits = creditsSummary([
  account({ uid: 'a', credits: { remain: 500 }, reserveCredits: 200 }),
  account({ uid: 'b', credits: { remain: 100 }, reserveCredits: 200 }),
  account({ uid: 'c', credits: {} }),
  account({ uid: 'd', credits: null }),
]);
assert.equal(credits.known, 2);
assert.equal(credits.total, 600);
assert.equal(credits.low, 1);

// ---- 到期明细：优先读后端算好的 creditExpiries，读不到时退回首笔套餐；按时刻升序并丢掉已过去的
const withDirect = account({ creditExpiries: [{ at: sec(now) + 9 * DAY, amount: 300, name: '月度' }, { at: sec(now) + 2 * DAY, amount: 50, name: '试用' }] });
assert.deepEqual(accountExpiries(withDirect).map((e) => e.amount), [50, 300]);
const withPackages = account({ creditExpiries: [], credits: { remain: 10, packages: [{ remain: 20, expireAt: sec(now) + 3 * DAY }, { remain: 0, expireAt: sec(now) + 1 * DAY }] } });
assert.deepEqual(accountExpiries(withPackages).map((e) => e.amount), [20]);
assert.equal(nextCreditExpiry([withDirect, withPackages], now).amount, 50, '取全体账号里最早的一笔');

// ---- 积分提示的优先级：低于保留积分压过到期信息，没有低余额时才报到期的绝对值
const lowList = [account({ uid: 'a', credits: { remain: 10 }, reserveCredits: 200 }), withDirect];
const lowHint = creditsHint(lowList, now);
assert.equal(lowHint, creditsHint([lowList[0]], now), '有低余额账号时提示由低余额决定');
const expiryHint = creditsHint([withDirect], now);
assert.ok(expiryHint.includes(fmtCredit(nextCreditExpiry([withDirect], now).amount)), '提示里带上到期的积分额度');
assert.notEqual(expiryHint, creditsHint([account({ uid: 'x' })], now), '没有到期信息时提示改成覆盖账号数');
assert.equal(creditsHint([account({ uid: 'x', credits: {} })], now), '', '一份余额都没有时不编造提示');

// ---- 健康快照：需要处理的排前面，其余按剩余时间从少到多，最多取 limit 个
const snap = snapshotAccounts([
  account({ uid: 'healthy', expiresAt: sec(now + 30 * DAY) }),
  account({ uid: 'expiring', expiresAt: sec(now + 1 * DAY) }),
  account({ uid: 'cooling', inCooldown: true, expiresAt: sec(now + 29 * DAY) }),
  account({ uid: 'dead', expiresAt: sec(now - 1) }),
], 9, now);
assert.equal(snap[0].uid, 'dead', '过期的账号最先展示');
assert.equal(snap[1].uid, 'cooling');
assert.deepEqual(snap.slice(2).map((a) => a.uid), ['expiring', 'healthy']);
assert.equal(snapshotAccounts([...Array(12)].map((_, i) => account({ uid: 'u' + i, expiresAt: sec(now + (i + 1) * DAY) })), 9, now).length, 9);
// 停用是用户自己的决定，不需要被优先处理，排在可用账号之后
const offLast = snapshotAccounts([account({ uid: 'off', enabled: false }), account({ uid: 'live' })], 9, now);
assert.deepEqual(offLast.map((a) => a.uid), ['live', 'off']);
// 同一份数据两次挑选顺序一致
const twiceA = snapshotAccounts(mixed, 9, now).map((a) => a.uid);
const twiceB = snapshotAccounts(mixed.slice().reverse(), 9, now).map((a) => a.uid);
assert.deepEqual(twiceB, twiceA, '顺序不随输入顺序变化');

// ---- 健康快照的进度条：按可用积分占总额的比例取满格，颜色按比例分档
const longBar = creditBar(account({ credits: { remain: 3819, size: 4698 } }));
assert.equal(Math.round(longBar.pct), 81);
assert.equal(longBar.color, 'var(--success)', '过半绿色');
assert.equal(creditBar(account({ credits: { remain: 1900, size: 4000 } })).color, 'var(--warning)', '不到一半黄色');
assert.equal(creditBar(account({ credits: { remain: 900, size: 4000 } })).color, 'var(--danger)', '不到四分之一红色');
assert.equal(creditBar(account({ credits: { remain: 5000, size: 4000 } })).pct, 100, '可用超过总额时封顶');
assert.equal(creditBar(account({ credits: { remain: 100, size: 0 } })), null, '没有总额不画进度条');
assert.equal(creditBar(account({ credits: null })), null);
const mixedCredits = account({ credits: { remain: 1500, size: 6000, packages: [
  { remain: 500, size: 1000, expireAt: sec(now) - 3600 },
  { remain: 1000, size: 2000, expireAt: sec(now) + 86400 },
  { remain: 0, size: 3000, expireAt: sec(now) + 86400 },
] } });
assert.equal(Math.round(creditBar(mixedCredits, now).pct), 20, '过期套餐不计入分母');
assert.equal(creditTone(mixedCredits, now).text, '1,000', '只显示有效期内的余额');
assert.equal(creditTone(mixedCredits, now).total, '5,000', '总额只合计有效期内的套餐');
assert.equal(creditTone(account({ credits: { remain: 100 } })).total, null, '没有总额时只显示余额');
assert.equal(creditsSummary([mixedCredits], now).total, 1000);

// ---- 到期文字：剩余秒数与颜色分档，没有到期时间时整块不显示
assert.equal(expiryVisual(account({ expiresAt: sec(now) + 30 * DAY }), now).remain, 30 * DAY);
assert.equal(expiryVisual(account({ expiresAt: sec(now) + 30 * DAY }), now).tone, 'success');
assert.equal(expiryVisual(account({ expiresAt: sec(now) + 1800 }), now).tone, 'warning');
assert.equal(expiryVisual(account({ expiresAt: sec(now) + 2 * 3600 }), now).tone, 'info');
assert.equal(expiryVisual(account({ expiresAt: sec(now - 1) }), now).tone, 'danger');
assert.equal(expiryVisual(account({ expiresAt: 0 }), now), null, '没有到期时间时显示「到期时间未知」');

// ---- 积分颜色：未知、非正、低于保留积分分别有不同的分档
assert.equal(creditTone(account({ credits: null })).tone, 'muted');
assert.equal(creditTone(account({ credits: { remain: 0 } })).tone, 'danger');
assert.equal(creditTone(account({ credits: { remain: 100 }, reserveCredits: 200 })).tone, 'warning');
assert.equal(creditTone(account({ credits: { remain: 5000 }, reserveCredits: 200 })).tone, '');

// ---- 出口密钥数：绑定该出口的启用密钥 + 未绑定版本的启用密钥，停用的不算
const keys = [
  { realm: 'intl', enabled: true },
  { realm: 'cn', enabled: true },
  { realm: '', enabled: true },
  { realm: 'intl', enabled: false },
];
assert.deepEqual(keyEffect(keys, 'intl'), { bound: 1, follow: 1, total: 2 });
assert.deepEqual(keyEffect(keys, 'cn'), { bound: 1, follow: 1, total: 2 });
assert.deepEqual(keyEffect(null, 'intl'), { bound: 0, follow: 0, total: 0 });

// ---- 趋势合计：请求次数、失败次数与 Token 消耗一起给
const trendTotals = trendSummary([{ requests: 3, errors: 1, total_tokens: 1200 }, { requests: 5, errors: 0, total_tokens: 3400 }]);
assert.deepEqual(trendTotals, { requests: 8, failures: 1, tokens: 4600 });
assert.deepEqual(trendSummary(null), { requests: 0, failures: 0, tokens: 0 });

// ---- 今日用量提示
assert.equal(usageHints({ errors: 0, credit: 1.5 }).requestHint, usageHints({ errors: 0, credit: 0 }).requestHint);
assert.ok(usageHints({ errors: 3, credit: 0 }).requestHint.includes('3'));
assert.ok(usageHints({ errors: 0, credit: 2.5 }).tokenHint.includes(fmtCredit(2.5)));

console.log('dashboard logic assertions passed');
