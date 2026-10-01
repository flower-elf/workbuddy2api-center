// 浏览器禁用存储时 localStorage / sessionStorage 的读写会抛出 SecurityError。
// 面板在这种环境下照常工作，只是记不住主题等设置，所以只有这里吞掉这类异常。

function area(kind) {
  try {
    return kind === 'session' ? globalThis.sessionStorage : globalThis.localStorage;
  } catch {
    return null;
  }
}

export function readStore(key, kind = 'local') {
  const s = area(kind);
  if (!s) return null;
  try {
    return s.getItem(key);
  } catch {
    return null;
  }
}

export function writeStore(key, value, kind = 'local') {
  const s = area(kind);
  if (!s) return;
  try {
    if (value === null || value === undefined) s.removeItem(key);
    else s.setItem(key, String(value));
  } catch {
    // 存储被禁用：放弃记忆
  }
}
