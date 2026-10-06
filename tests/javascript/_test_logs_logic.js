/* 运行日志页纯逻辑：筛选、增量合并、统计与文本导出。
 * 运行：node tests/javascript/_test_logs_logic.js
 */
import assert from 'node:assert/strict';

const logic = await import('../../app/web/js/pages/logs/logic.js');

const entry = (id, level, tag, msg, time = '10:00:0' + id) => ({ id, level, tag, msg, time, ts: '2026-10-03 ' + time });
const filters = (patch = {}) => ({ ...logic.defaultFilters(), ...patch });

// ---- 级别筛选：只留完全同名的一档，空值表示全部
{
  const logs = [
    entry(1, 'INFO', 'chat', 'chat done 1'),
    entry(2, 'WARN', 'system', 'retry later'),
    entry(3, 'ERROR', 'accounts', 'account refresh failed'),
  ];
  assert.equal(logic.filterLogs(logs, filters()).length, 3);
  assert.deepEqual(logic.filterLogs(logs, filters({ level: 'ERROR' })).map((x) => x.id), [3]);
  assert.deepEqual(logic.filterLogs(logs, filters({ level: 'WARN' })).map((x) => x.id), [2]);
  assert.deepEqual(logic.filterLogs(logs, filters({ level: 'INFO' })).map((x) => x.id), [1]);
  assert.deepEqual(logic.filterLogs(logs, filters({ level: 'DEBUG' })).map((x) => x.id), []);
}

// ---- 模块筛选 + 关键字搜索：大小写无关，内容、模块、时刻都能搜到
{
  const logs = [
    entry(1, 'INFO', 'chat', 'deepseek-v4.1-flash replied'),
    entry(2, 'ERROR', 'tasks', '猫猫旅行 领取失败 11-128'),
    entry(3, 'WARN', 'scheduler', 'next checkin at 09:00:00'),
    entry(4, 'INFO', 'chat', 'upstream 502'),
  ];
  assert.deepEqual(logic.filterLogs(logs, filters({ tag: 'chat' })).map((x) => x.id), [1, 4]);
  assert.deepEqual(logic.filterLogs(logs, filters({ search: 'DEEPSEEK' })).map((x) => x.id), [1], '关键字不区分大小写');
  assert.deepEqual(logic.filterLogs(logs, filters({ search: '  11-128 ' })).map((x) => x.id), [2], '首尾空格被忽略');
  assert.deepEqual(logic.filterLogs(logs, filters({ search: 'scheduler' })).map((x) => x.id), [3], '模块名也能作为关键字');
  assert.deepEqual(logic.filterLogs(logs, filters({ search: '09:00:00' })).map((x) => x.id), [3], '时刻也能作为关键字');
  assert.deepEqual(logic.filterLogs(logs, filters({ level: 'ERROR', tag: 'chat' })), [], '多个条件同时生效');
  assert.deepEqual(logic.filterLogs(logs, filters({ level: 'INFO', tag: 'chat', search: '502' })).map((x) => x.id), [4]);
  assert.deepEqual(logic.filterLogs(logs, filters({ search: '不存在的关键字' })), []);
}

// ---- 增量合并：按 id 去重，保持旧→新顺序，超过上限丢最旧的
{
  const existing = [entry(1, 'INFO', 'system', 'a'), entry(2, 'INFO', 'system', 'b')];
  assert.deepEqual(logic.mergeLogs(existing, []).map((x) => x.id), [1, 2]);
  assert.deepEqual(logic.mergeLogs(existing, null).map((x) => x.id), [1, 2]);
  const withNew = logic.mergeLogs(existing, [entry(2, 'INFO', 'system', 'b'), entry(3, 'INFO', 'system', 'c')]);
  assert.deepEqual(withNew.map((x) => x.id), [1, 2, 3], '重复 id 只保留一条');
  const dupIncoming = logic.mergeLogs([], [entry(5, 'INFO', 'system', 'e'), entry(5, 'INFO', 'system', 'e')]);
  assert.deepEqual(dupIncoming.map((x) => x.id), [5], '同一批里的重复 id 也只留一条');
  assert.equal(existing.length, 2, '原数组不被就地修改');

  const many = Array.from({ length: logic.LOG_MAX + 5 }, (_, i) => entry(i + 1, 'INFO', 'system', 'm' + i));
  const capped = logic.mergeLogs([], many);
  assert.equal(capped.length, logic.LOG_MAX, '本地缓冲与网关一致，最多 2000 条');
  assert.equal(capped[0].id, 6, '丢掉的是最旧的记录');
  assert.equal(capped[capped.length - 1].id, logic.LOG_MAX + 5);
}

// ---- since_id：网关没给 max_id 时用最后一条推算
{
  assert.equal(logic.nextSinceId([], 7), 7);
  assert.equal(logic.nextSinceId([entry(3, 'INFO', 'system', 'a'), entry(9, 'INFO', 'system', 'b')], 4), 9);
  assert.equal(logic.nextSinceId([{ id: 'x' }], 3), 3, '读不出数字的 id 不影响起点');
  assert.equal(logic.nextSinceId(null, 12), 12);
}

// ---- 查询串
{
  assert.equal(logic.buildLogsQuery({ sinceId: 0, limit: 500 }), '/logs?limit=500');
  assert.equal(logic.buildLogsQuery({ sinceId: 128, limit: 200 }), '/logs?since_id=128&limit=200');
  assert.equal(logic.buildLogsQuery(), '/logs?limit=' + logic.LOG_POLL_LIMIT);
}

// ---- 统计：总条数、错误、警告
{
  const logs = [
    entry(1, 'INFO', 'chat', 'a'), entry(2, 'WARN', 'chat', 'b'),
    entry(3, 'ERROR', 'chat', 'c'), entry(4, 'ERROR', 'system', 'd'),
  ];
  assert.deepEqual(logic.logStats(logs), { total: 4, errors: 2, warns: 1 });
  assert.deepEqual(logic.logStats([]), { total: 0, errors: 0, warns: 0 });
}

// ---- 样式类名按白名单生成，未知级别与模块不会拼出任意类名
{
  assert.equal(logic.levelClass('ERROR'), 'lvl-ERROR');
  assert.equal(logic.levelClass('DEBUG'), 'lvl-DEBUG');
  assert.equal(logic.levelClass(''), 'lvl-INFO');
  assert.equal(logic.levelClass('INFO" onload="x'), 'lvl-INFO');
  assert.equal(logic.tagClass('tasks'), 'tag-tasks');
  assert.equal(logic.tagClass('catalog'), 'tag-catalog', '网关自定义的模块也要有自己的颜色');
  assert.equal(logic.tagClass('unknown-module'), 'tag-system');
  assert.equal(logic.tagClass(undefined), 'tag-system');
}

// ---- 文本导出：每行带时刻、级别与模块，缺失字段有兜底
{
  const logs = [
    { id: 1, ts: '2026-10-03 10:00:00', time: '10:00:00', level: 'ERROR', tag: 'chat', msg: 'upstream 502' },
    { id: 2, level: 'WARN', msg: 'retry' },
  ];
  assert.equal(logic.formatLogLine(logs[0]), '[2026-10-03 10:00:00] [ERROR] [chat] upstream 502');
  assert.equal(logic.formatLogLine(logs[1]), '[] [WARN] [system] retry', '缺少时刻时留空方括号，不编造时间');
  assert.equal(logic.formatLogLine({ time: '10:00:02', level: 'INFO', tag: 'chat', msg: 'hi' }), '[10:00:02] [INFO] [chat] hi');
  assert.equal(logic.formatLogText(logs), [
    '[2026-10-03 10:00:00] [ERROR] [chat] upstream 502',
    '[] [WARN] [system] retry',
  ].join('\n'));
  assert.equal(logic.formatLogText([]), '');

  // 复制与导出只带当前筛选命中的行
  const filtered = logic.filterLogs([
    { id: 1, level: 'INFO', tag: 'chat', msg: 'deepseek ok' },
    { id: 2, level: 'ERROR', tag: 'chat', msg: 'upstream 502' },
  ], filters({ level: 'ERROR' }));
  const text = logic.formatLogText(filtered);
  assert.equal(text.includes('upstream 502'), true);
  assert.equal(text.includes('deepseek ok'), false, '未命中的行不能出现在复制内容里');
}

console.log('logs logic assertions passed');
