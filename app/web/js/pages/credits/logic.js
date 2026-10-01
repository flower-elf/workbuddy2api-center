// 积分页的纯逻辑：套餐明细的规整、按来源归并、汇总与到期文案。
// 只依赖 core/format.js 与账号页逻辑，可以在 Node 里直接测试。

import { compareText, fmtNumber, toMillis } from '../../core/format.js';
import { creditAgeText, expiryCountdown, urgencyTone } from '../accounts/logic.js';

/** 一笔套餐：字段名与凭证文件里 credits.packages 的条目一致。 */
function numberOrZero(value) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

/** 时间戳统一成毫秒；缺失或非法返回 0，表示上游没给这个时刻。 */
function millis(value) {
  return toMillis(value) || 0;
}

/**
 * 单笔套餐的规整。非对象与全零条目返回 null：凭证文件是手改得动的，脏数据
 * 不该让整页无法渲染，而没有额度的空条目也没有可看的内容。
 * 每个入口都过这一道，调用方给原始条目或规整后的条目结果都一样。
 */
function normalisePackage(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const size = numberOrZero(raw.size);
  const remain = numberOrZero(raw.remain);
  const used = numberOrZero(raw.used);
  if (!size && !remain && !used) return null;
  return {
    name: String(raw.name || '').trim() || '套餐',
    subProduct: String(raw.subProduct || '').trim(),
    size, remain, used,
    startAt: millis(raw.startAt),
    expireAt: millis(raw.expireAt),
  };
}

/** 账号的套餐明细，已规整。 */
export function packagesOf(account) {
  const credits = account && account.credits;
  const list = credits && Array.isArray(credits.packages) ? credits.packages : [];
  const rows = [];
  for (const raw of list) {
    const item = normalisePackage(raw);
    if (item) rows.push(item);
  }
  return rows;
}

/** 单笔套餐的状态：余额为 0 是用完，余额还在但过了期是过期，其余是可用。 */
export function packageState(raw, now = Date.now()) {
  const item = normalisePackage(raw);
  if (!item || item.remain <= 0) return 'used';
  if (item.expireAt > 0 && item.expireAt <= now) return 'expired';
  return 'active';
}

export const STATE_ORDER = { active: 0, expired: 1, used: 2 };

/** 状态徽章的文案与色调。 */
export function stateBadge(state) {
  if (state === 'expired') return { text: '已过期', tone: 'danger' };
  if (state === 'used') return { text: '已用完', tone: 'muted' };
  return { text: '可用', tone: 'success' };
}

/**
 * 按来源归并套餐：同一个来源可能分多笔到账（例如连着领了几次同一个裂变包），
 * 凑成一行读起来才看得清这个来源一共给了多少、剩下多少。
 */
export function groupPackages(packages, now = Date.now()) {
  const groups = new Map();
  for (const raw of packages || []) {
    const item = normalisePackage(raw);
    if (!item) continue;
    const key = item.name + '\u0000' + item.subProduct;
    let group = groups.get(key);
    if (!group) {
      group = {
        key, name: item.name, subProduct: item.subProduct, count: 0,
        size: 0, remain: 0, used: 0, nextExpireAt: 0, lastExpireAt: 0, state: 'active', packages: [],
      };
      groups.set(key, group);
    }
    group.count += 1;
    group.size += item.size;
    group.remain += item.remain;
    group.used += item.used;
    group.packages.push(item);
  }
  for (const group of groups.values()) {
    group.packages.sort((x, y) => (x.expireAt || Infinity) - (y.expireAt || Infinity));
    // 最近一笔到期只算还有额度、且还没到期的那些条目；已经过期的另记最后一个到期的时刻。
    let next = 0;
    let last = 0;
    for (const item of group.packages) {
      if (item.remain <= 0) continue;
      if (item.expireAt > now) {
        if (!next || item.expireAt < next) next = item.expireAt;
      } else if (item.expireAt > last) {
        last = item.expireAt;
      }
    }
    group.nextExpireAt = next;
    group.lastExpireAt = last;
    if (group.remain <= 0) group.state = 'used';
    else if (!next && group.packages.some((item) => item.remain > 0 && item.expireAt > 0)) group.state = 'expired';
    else group.state = 'active';
  }
  return [...groups.values()].sort((x, y) => STATE_ORDER[x.state] - STATE_ORDER[y.state]
    || (x.nextExpireAt || Infinity) - (y.nextExpireAt || Infinity)
    || y.remain - x.remain
    || compareText(x.name, y.name));
}

/**
 * 归并行的展开键：同一个来源（例如裂变包）在每个账号下都会出现一行，键里要带上
 * 账号的 uid，否则展开一个账号的同名行会让其他账号一起展开。
 */
export function creditRowKey(account, group) {
  return String((account && account.uid) || '') + '\u0000' + String((group && group.key) || '');
}

/** 账号的积分构成：汇总数字、按来源分组与上次读取时间。 */
export function accountCredits(account, now = Date.now()) {
  const credits = account && account.credits;
  const packages = packagesOf(account);
  const groups = groupPackages(packages, now);
  const remain = credits ? numberOrZero(credits.remain) : 0;
  const used = credits ? numberOrZero(credits.used) : 0;
  const size = credits ? numberOrZero(credits.size) : 0;
  let nextExpireAt = 0;
  let nextExpireAmount = 0;
  for (const item of packages) {
    if (item.remain <= 0 || item.expireAt <= now) continue;
    if (!nextExpireAt || item.expireAt < nextExpireAt) {
      nextExpireAt = item.expireAt;
      nextExpireAmount = item.remain;
    }
  }
  return {
    queried: Boolean(credits),
    remain, used, size,
    sizeText: fmtNumber(size),
    count: packages.length,
    groups,
    nextExpireAt,
    nextExpireAmount,
    age: creditAgeText(credits, now),
  };
}

/** 页头汇总：只统计已经查到余额的账号。 */
export function pageTotals(accounts) {
  const totals = { remain: 0, used: 0, packages: 0, queried: 0, missing: 0 };
  for (const account of accounts || []) {
    if (!account || !account.credits) {
      if (account) totals.missing += 1;
      continue;
    }
    const credits = account.credits;
    totals.queried += 1;
    totals.remain += numberOrZero(credits.remain);
    totals.used += numberOrZero(credits.used);
    totals.packages += packagesOf(account).length;
  }
  return totals;
}

/** 全部账号里最早到期、且还有额度的一笔；没有时返回 null。 */
export function nextExpiry(accounts, now = Date.now()) {
  let hit = null;
  for (const account of accounts || []) {
    for (const item of packagesOf(account)) {
      if (item.remain <= 0 || item.expireAt <= now) continue;
      if (hit && item.expireAt >= hit.at) continue;
      hit = {
        at: item.expireAt,
        amount: item.remain,
        name: item.name,
        account: (account && (account.nickname || account.uid)) || '账号',
      };
    }
  }
  return hit;
}

/** 到期角标：时刻加剩余额度与倒计时；没有到期安排时返回 null。 */
export function expiryChip(at, amount, now = Date.now()) {
  if (!at) return null;
  const msLeft = at - now;
  return {
    text: expiryCountdown(msLeft),
    tone: urgencyTone(msLeft),
    title: fmtNumber(amount) + ' 积分 · ' + expiryCountdown(msLeft),
  };
}

/** 套餐的周期文案：上游没给周期时留空。 */
export function cycleText(raw) {
  const item = normalisePackage(raw);
  if (!item) return '';
  const day = (ms) => {
    const d = new Date(ms);
    return (d.getMonth() + 1) + '-' + d.getDate();
  };
  if (!item.startAt && !item.expireAt) return '';
  if (item.startAt && item.expireAt) return day(item.startAt) + ' 至 ' + day(item.expireAt);
  return item.expireAt ? day(item.expireAt) + ' 到期' : day(item.startAt) + ' 开始';
}

/** 归并行的说明：来源说明与笔数。 */
export function groupSubText(group) {
  const parts = [];
  if (group.count > 1) parts.push('共 ' + group.count + ' 笔');
  if (group.subProduct) parts.push(group.subProduct);
  return parts.join(' · ');
}

/** 还没有查到余额的账号，供页面按需补查。 */
export function unqueriedAccounts(accounts, tried) {
  return (accounts || []).filter((account) => account && account.uid
    && !account.credits && !tried.has(account.uid));
}

/** 打开积分页时要不要重新读取：本会话还没读过就读取，间隔为 0 表示不再自动读取。 */
export function dueForRefresh(lastAt, now, seconds) {
  if (!lastAt) return true;
  if (!(seconds > 0)) return false;
  return now - lastAt >= seconds * 1000;
}
