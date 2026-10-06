/* 密钥页纯逻辑：状态分档、掩码与配额显示、IP 白名单校验、表单校验与提交载荷、逐行提交的字段保护、客户端接入片段。
 * 运行：node tests/javascript/_test_keys_logic.js
 */
import assert from 'node:assert/strict';

import * as logic from '../../app/web/js/pages/keys/logic.js';

const now = new Date(2026, 9, 3, 12, 0, 0).getTime();
const sec = (ms) => Math.floor(ms / 1000);

const keyRow = (over = {}) => Object.assign({
  id: 'key-a', name: '甲', realm: '', enabled: true, masked: 'wbk-******demo',
  models: [], expires_at: 0, quota_tokens: 0, quota_credit: 0, ip_allowlist: [], usage_reset_at: 0,
  usage: { requests: 0, total_tokens: 0, credit: 0, last_used_at: 0 },
}, over);

const used = (over = {}) => keyRow(Object.assign({ usage: { requests: 3, total_tokens: 500, credit: 2.5, last_used_at: sec(now) - 600 } }, over));

// ---- 状态分档：停用优先于过期，过期优先于配额，配额用满即停
assert.equal(logic.deriveKeyStatus(used(), now), 'ok');
assert.equal(logic.deriveKeyStatus(used({ enabled: false, expires_at: sec(now) - 1, quota_tokens: 1 }), now), 'disabled',
  '停用的密钥不该被画成过期或超配额');
assert.equal(logic.deriveKeyStatus(used({ expires_at: sec(now) - 1, quota_tokens: 1 }), now), 'expired',
  '同时过期且超配额时按过期算');
assert.equal(logic.deriveKeyStatus(used({ quota_tokens: 500 }), now), 'quota_exceeded',
  '用量与配额相等即用满');
assert.equal(logic.deriveKeyStatus(used({ quota_tokens: 501 }), now), 'ok');
assert.equal(logic.deriveKeyStatus(used({ quota_credit: 2.5 }), now), 'quota_exceeded',
  '积分配额用满同样停止服务');
assert.equal(logic.deriveKeyStatus(used({ quota_credit: 0, quota_tokens: 0 }), now), 'ok',
  '配额为 0 表示不限，用到多少都不算超');
assert.equal(logic.deriveKeyStatus(used({ expires_at: sec(now) + 1 }), now), 'ok');
// 服务端给了 status 就以它为准，面板不自己另算
assert.equal(logic.deriveKeyStatus(keyRow({ status: 'quota_exceeded' }), now), 'quota_exceeded');
assert.equal(logic.deriveKeyStatus(keyRow({ status: 'ok', enabled: false }), now), 'ok');

const texts = ['ok', 'disabled', 'expired', 'quota_exceeded'].map((s) => logic.statusText(keyRow({ status: s })));
assert.equal(new Set(texts).size, 4, '四种状态要给出四种不同的徽章文字: ' + JSON.stringify(texts));

const summary = logic.summarizeKeys([
  used(), used({ id: 'b', enabled: false }), used({ id: 'c', expires_at: sec(now) - 5 }),
  used({ id: 'd', quota_credit: 1 }), used({ id: 'e' }),
], now);
assert.deepEqual(summary, { total: 5, ok: 2, disabled: 1, expired: 1, quota_exceeded: 1 },
  '表头汇总要按同一套分档统计: ' + JSON.stringify(summary));

// ---- 认证状态：有一把启用的密钥就要求携带 Key，过期与超配额的同样把门
const gate = logic.authSummary({ api_keys: [keyRow({ status: 'expired', expires_at: sec(now) - 10 })] });
assert.equal(gate.kind, 'panel', '只剩过期密钥时接口仍在把关: ' + JSON.stringify(gate));
assert.ok(gate.text.includes('1'), '要写出有几把密钥在把关: ' + gate.text);
assert.equal(logic.authSummary({ api_keys: [keyRow({ enabled: false })] }).kind, 'open');
assert.equal(logic.authSummary({ auth_required: false }).kind, 'none');
assert.equal(logic.authSummary({ api_key_set: true, api_key_set_by_panel: false, api_key_masked: 'wbk-****' }).kind, 'startup');
assert.equal(logic.authSummary({ api_key_set: true, api_key_set_by_panel: true, api_key_masked: 'wbk-****' }).kind, 'legacy',
  '设置文件里保存的单个 Key 同样在把关，不能说不校验');
assert.ok(logic.authSummary({ api_key_set: true, api_key_set_by_panel: true, api_key_masked: 'wbk-****' }).text.includes('wbk-****'),
  '要给出掩码，好认出是哪把密钥');
// 服务端 identify_key 的口径：列表里已有密钥时只认列表里的，启动参数那把不再生效
const mixed = logic.authSummary({ api_key_set: true, api_key_set_by_panel: false, api_key_masked: 'wbk-****', api_keys: [keyRow({})] });
assert.equal(mixed.kind, 'panel', '列表里有启用的密钥时不能说成启动参数在把关: ' + JSON.stringify(mixed));
const locked = logic.authSummary({ api_key_set: true, api_key_set_by_panel: false, api_key_masked: 'wbk-****', api_keys: [keyRow({ enabled: false, status: 'disabled' })] });
assert.equal(locked.kind, 'locked', '列表里只剩停用的密钥而启动参数还带着 Key 时要单独说明: ' + JSON.stringify(locked));
assert.equal(logic.authSummary({ api_keys: [keyRow({ enabled: false, status: 'disabled' })] }).kind, 'open');
assert.equal(logic.startupKeyRow({ api_key_set: true, api_key_set_by_panel: false, api_key_masked: 'wbk-****', api_keys: [keyRow({})] }).name, '启动参数 Key');
assert.ok(logic.startupKeyRow({ api_key_set: true, api_key_set_by_panel: false, api_keys: [keyRow({})] }).note
  !== logic.startupKeyRow({ api_key_set: true, api_key_set_by_panel: false }).note,
  '启动参数那把被列表顶掉时，说明要跟着变');

const startup = logic.startupKeyRow({ api_key_set: true, api_key_set_by_panel: false, api_key_masked: 'wbk-****', key: 'wbk-plain' });
assert.ok(startup, '启动参数里带了 Key 时要给出只读说明');
assert.equal(startup.masked, 'wbk-****');
assert.ok(!(startup.masked || '').includes('wbk-plain'), '只读说明里不能出现明文');
assert.equal(logic.startupKeyRow({ api_key_set: true, api_key_set_by_panel: true }), null,
  '面板自己保存的 Key 不需要额外的只读行');
assert.equal(logic.startupKeyRow({ api_key_set: false }), null);

// ---- 显示：列表只画掩码，明文永远不出现在列表里
assert.equal(logic.keyDisplay({ masked: 'wbk-******demo', key: 'wbk-plain-0001' }), 'wbk-******demo');
assert.ok(!logic.keyDisplay({ key: 'wbk-plain-0001' }).includes('wbk-plain-0001'),
  '服务端没给掩码时也不能把明文画到列表里');
assert.ok(logic.keyDisplay({}) !== '', '掩码缺失时要有占位，不能空着');

assert.equal(logic.ipLimitText(keyRow()), '不限 IP');
assert.equal(logic.ipLimitText(keyRow({ ip_allowlist: ['10.0.0.1', '10.0.0.2'] })), '白名单 2 条');
assert.equal(logic.modelLimitText(keyRow()), '全部模型');
assert.equal(logic.modelLimitText(keyRow({ models: ['glm-*'] })), '限 1 个模型');
assert.ok(logic.limitTitle(keyRow({ ip_allowlist: ['10.0.0.1'], models: ['glm-*'] })).includes('10.0.0.1'));
assert.ok(logic.limitTitle(keyRow({ ip_allowlist: ['10.0.0.1'], models: ['glm-*'] })).includes('glm-*'));

const linesPlain = logic.usageLines(used());
assert.equal(linesPlain.length, 2);
assert.equal(linesPlain[1], '', '没设配额时不该画配额行');
const linesQuota = logic.usageLines(used({ quota_tokens: 1000, quota_credit: 3 }));
assert.ok(linesQuota[1].includes('1,000'), '配额行要写出 Token 上限: ' + JSON.stringify(linesQuota));
assert.ok(linesQuota[1].includes('3'), '配额行要写出积分上限: ' + JSON.stringify(linesQuota));
assert.equal(logic.lastUsedText(keyRow()), '从未使用');
assert.notEqual(logic.lastUsedText(used(), now), '从未使用');
assert.ok(logic.expiryText(keyRow({ expires_at: sec(now) - 86400 }), now).includes('已过期'),
  '过期日期要标出来，免得被当成还有效');
assert.ok(!logic.expiryText(keyRow({ expires_at: sec(now) + 86400 }), now).includes('已过期'),
  '没过期的不能标已过期');

// ---- IP 白名单：逐条校验，写错的条目必须报出来
const allow = logic.parseIpAllowlist('10.0.0.1\n192.168.0.0/16, 203.0.113.7\n\n10.0.0.1');
assert.deepEqual(allow, { ok: true, list: ['10.0.0.1', '192.168.0.0/16', '203.0.113.7'], invalid: [] },
  '换行、逗号、空格混合输入都要认，重复的去掉: ' + JSON.stringify(allow));
const bad = logic.parseIpAllowlist('10.0.0.1\n10.0.0.999\nfoo');
assert.equal(bad.ok, false);
assert.deepEqual(bad.invalid, ['10.0.0.999', 'foo'], '不合法的条目要逐条列出: ' + JSON.stringify(bad));
assert.deepEqual(bad.list, ['10.0.0.1'], '合法的条目仍要留在名单里');

for (const good of ['127.0.0.1', '255.255.255.255', '0.0.0.0/0', '10.0.0.0/32', '::1', 'fe80::1', '2001:db8::/128']) {
  assert.equal(logic.isIpOrCidr(good), true, good + ' 应当被接受');
}
for (const badIp of ['', '10.0.0', '10.0.0.256', '10.0.0.1/33', '10.0.0.1/-1', '10.0.0.1:8080', ':::', 'fe80::1/129', 'abc', '10.0.0.1/abc']) {
  assert.equal(logic.isIpOrCidr(badIp), false, JSON.stringify(badIp) + ' 应当被拒绝');
}

// ---- 表单校验：与服务端同一口径，新密钥至少 4 个字符、天数是正整数、配额非负
const form = (over = {}) => Object.assign(logic.formFromRow({}), over);

assert.notEqual(logic.validateKeyForm(form()), '', '新建时没填密钥内容要拦下来');
assert.notEqual(logic.validateKeyForm(form({ keyValue: 'abc' })), '', '短于 4 个字符的密钥服务端会拒，本地先拦');
assert.equal(logic.validateKeyForm(form({ keyValue: 'abcd' })), '');
assert.equal(logic.validateKeyForm(form({ editing: true, keyValue: '' })), '',
  '编辑时留空表示不修改，必须能保存');
assert.notEqual(logic.validateKeyForm(form({ realm: 'us' })), '', '未知出口要拦下来');
for (const realm of ['', 'intl', 'cn']) assert.equal(logic.validateKeyForm(form({ keyValue: 'abcd', realm })), '');

for (const days of ['0', '-1', '1.5', '3651', '']) {
  assert.notEqual(logic.validateKeyForm(form({ keyValue: 'abcd', expiryMode: 'days', expiryDays: days })), '',
    '天数 ' + JSON.stringify(days) + ' 不合法要拦下来');
}
for (const days of [1, 30, 3650]) assert.equal(logic.validateKeyForm(form({ keyValue: 'abcd', expiryMode: 'days', expiryDays: days })), '');

assert.notEqual(logic.validateKeyForm(form({ keyValue: 'abcd', quotaTokens: '-1' })), '');
assert.notEqual(logic.validateKeyForm(form({ keyValue: 'abcd', quotaTokens: '1.5' })), '');
assert.notEqual(logic.validateKeyForm(form({ keyValue: 'abcd', quotaCredit: '-0.1' })), '');
assert.equal(logic.validateKeyForm(form({ keyValue: 'abcd', quotaTokens: '0', quotaCredit: '0.5' })), '');
const ipError = logic.validateKeyForm(form({ keyValue: 'abcd', ipText: '10.0.0.1\n10.0.0.999' }));
assert.ok(ipError.includes('10.0.0.999'), '不合法 IP 要在错误里指名道姓: ' + ipError);
assert.equal(logic.validateKeyForm(form({ keyValue: 'abcd', ipText: '10.0.0.1\n192.168.0.0/16' })), '');

// ---- 表单 → 载荷：省略字段表示保持原值，有效期是绝对时间戳
const created = logic.buildKeyEntry(form({ keyValue: 'abcd', name: '  甲组  ', realm: 'cn', expiryMode: 'days', expiryDays: 7 }), now);
assert.equal(created.expires_at, sec(now) + 7 * 86400, '有效期要换算成绝对时间戳');
assert.equal(created.name, '甲组');
assert.equal(created.realm, 'cn');
assert.equal(created.enabled, true);
assert.notEqual(created.name, '', '名字为空时要有兜底，不能提交空名字');

assert.equal(logic.buildKeyEntry(form({ keyValue: 'abcd', expiryMode: 'never' }), now).expires_at, 0);
assert.ok(!('expires_at' in logic.buildKeyEntry(form({ keyValue: 'abcd', editing: true, expiryMode: 'keep' }), now)),
  '「保持不变」不能带 expires_at，否则服务端会覆盖已存的有效期');

const untouched = logic.buildKeyEntry(form({ keyValue: 'abcd', editing: true }), now);
assert.ok(!('models' in untouched), '服务端没返回模型限制且操作员没动过时，不能提交 models，免得清空已存的限制');
assert.deepEqual(logic.buildKeyEntry(form({ keyValue: 'abcd', editing: true, patternsTouched: true, patterns: [] }), now).models, [],
  '在编辑器里删掉全部标签表示改成不限制');
assert.deepEqual(logic.buildKeyEntry(form({ keyValue: 'abcd', patternsTouched: true, patterns: ['glm-*'] }), now).models, ['glm-*']);
assert.deepEqual(logic.buildKeyEntry(form({ keyValue: 'abcd', patternsKnown: true, patterns: [] }), now).models, [],
  '服务端返回过 models 字段时，清空标签表示改成不限制');

const quotas = logic.buildKeyEntry(form({ keyValue: 'abcd', quotaTokens: '1000.9', quotaCredit: '5.5', ipText: '10.0.0.1\n10.0.0.1\n192.168.0.0/16' }), now);
assert.equal(quotas.quota_tokens, 1000, 'Token 配额要取整');
assert.equal(quotas.quota_credit, 5.5);
assert.deepEqual(quotas.ip_allowlist, ['10.0.0.1', '192.168.0.0/16'], '白名单去重后按输入顺序提交');
assert.deepEqual(logic.buildKeyEntry(form({ keyValue: 'abcd', quotaTokens: '', quotaCredit: '' }), now).quota_tokens, 0);

// ---- 标签编辑器
assert.deepEqual(logic.addPatterns([], 'glm-*, deepseek*  GLM-*'), ['glm-*', 'deepseek*'],
  '标签按小写去重，重复的不再加进去');
assert.deepEqual(logic.removePatternAt(['a', 'b', 'c'], 1), ['a', 'c']);
assert.deepEqual(logic.removePatternAt(['a', 'b'], 9), ['a', 'b']);
assert.deepEqual(logic.parsePatternInput('gpt-6-astra'), ['gpt-6-astra']);

// ---- 列表行 → 提交载荷：服务端没给的字段不提交，给过的原样带上
const servedRow = keyRow({ models: ['glm-*'], expires_at: sec(now) + 86400, quota_tokens: 10, quota_credit: 1, ip_allowlist: ['10.0.0.1'] });
const servedPayload = logic.keyRowPayload(servedRow);
assert.deepEqual(servedPayload.models, ['glm-*']);
assert.equal(servedPayload.expires_at, sec(now) + 86400);
assert.equal(servedPayload.quota_tokens, 10);
assert.equal(servedPayload.quota_credit, 1);
assert.deepEqual(servedPayload.ip_allowlist, ['10.0.0.1']);

const oldRow = { id: 'key-old', name: '旧服务端', realm: 'intl', enabled: true };
const oldPayload = logic.keyRowPayload(oldRow);
assert.deepEqual(Object.keys(oldPayload).sort(), ['enabled', 'id', 'key', 'name', 'realm'],
  '旧字段照旧提交，新字段不在行里就不要带上: ' + JSON.stringify(oldPayload));
assert.equal(oldPayload.key, '', '列表里没有明文，空值表示保持原值（服务端口径）');

const rows = [keyRow({ id: 'k1', name: '甲' }), keyRow({ id: 'k2', name: '乙' }), keyRow({ id: 'k3', name: '丙' })];
assert.deepEqual(logic.rowsAfterReplace(rows, 2, null).map((r) => r.id), ['k1', 'k2'], '删除只去掉那一行');
assert.deepEqual(logic.rowsAfterReplace(rows, 1, keyRow({ id: 'k2', name: '乙改' })).map((r) => r.name), ['甲', '乙改', '丙']);
assert.deepEqual(logic.rowsAfterReplace(rows, -1, keyRow({ id: 'k4', name: '丁' })).map((r) => r.id), ['k1', 'k2', 'k3', 'k4']);
assert.deepEqual(logic.buildSavePayload(rows).api_keys.length, 3);

// ---- 创建成功后的接入片段
assert.equal(logic.apiBaseUrl('http://127.0.0.1:8788/'), 'http://127.0.0.1:8788/v1');
assert.equal(logic.anthropicBaseUrl('http://127.0.0.1:8788/'), 'http://127.0.0.1:8788',
  'Claude Code 会自己追加 /v1，基址不能带 /v1');
const snippets = logic.clientSnippets('http://127.0.0.1:8788', 'wbk-secret-0001');
assert.equal(snippets.length, 3);
const byId = Object.fromEntries(snippets.map((s) => [s.id, s.text]));
assert.ok(byId.curl.includes('http://127.0.0.1:8788/v1/chat/completions'));
assert.ok(byId.curl.includes('Bearer wbk-secret-0001'));
assert.ok(byId.openai.includes('OPENAI_BASE_URL=http://127.0.0.1:8788/v1'));
assert.ok(byId.openai.includes('OPENAI_API_KEY=wbk-secret-0001'));
assert.ok(byId.anthropic.includes('ANTHROPIC_BASE_URL=http://127.0.0.1:8788'));
assert.ok(!byId.anthropic.includes('ANTHROPIC_BASE_URL=http://127.0.0.1:8788/v1'),
  'ANTHROPIC_BASE_URL 带上 /v1 会打到 /v1/v1/messages');
assert.ok(byId.anthropic.includes('ANTHROPIC_AUTH_TOKEN=wbk-secret-0001'));
for (const snippet of snippets) assert.ok(snippet.text.includes('wbk-secret-0001'), snippet.id + ' 片段里没有密钥');
assert.ok(!byId.curl.includes('//v1'), '基址末尾多斜杠不能拼出双斜杠');

// ---- 随机密钥：36 位十六进制，每次生成都不同
const random = logic.randomKeyValue();
assert.match(random, /^[0-9a-f]{36}$/, '随机密钥要是 18 字节的十六进制: ' + random);
assert.notEqual(logic.randomKeyValue(), random, '两次生成不能是同一个值');

console.log('keys logic assertions passed');
