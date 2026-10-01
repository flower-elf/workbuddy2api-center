// 命令面板的条目与匹配规则。纯函数，便于在 Node 里测试。
import { ROUTES, SETTINGS_TABS } from '../core/routes.js';

/** 全部可跳转的目的地：页面在前，设置页各标签紧随其后。 */
export function paletteEntries() {
  const entries = [];
  for (const route of ROUTES) {
    entries.push({ id: route.path, path: route.path, sub: '', label: route.title, icon: route.icon,
      group: route.section && route.dock !== true ? sectionOwner(route.section) : '页面', keywords: route.keywords || '' });
    if (route.path === 'settings') {
      for (const tab of SETTINGS_TABS) {
        entries.push({ id: 'settings/' + tab.id, path: 'settings', sub: tab.id, label: tab.title, icon: tab.icon,
          group: '设置', keywords: tab.keywords || '' });
      }
    }
  }
  return entries;
}

function sectionOwner(section) {
  const owner = ROUTES.find((r) => r.section === section && r.dock);
  return owner ? owner.dockTitle || owner.title : '页面';
}

const normalize = (s) => String(s || '').toLowerCase().replace(/[-_\s]+/g, '');

/** 单个词的得分：名称全等 4，名称前缀 3，名称包含 2，关键词或归属包含 1，不匹配 0。 */
function scoreWord(entry, word) {
  const label = normalize(entry.label);
  if (label === word) return 4;
  if (label.startsWith(word)) return 3;
  if (label.includes(word)) return 2;
  if (normalize(entry.keywords).includes(word) || normalize(entry.group).includes(word) || normalize(entry.id).includes(word)) return 1;
  return 0;
}

/** 多个词之间是「并且」关系；按总分降序，同分保持原顺序。空查询返回全部。 */
export function searchPalette(entries, query) {
  const words = String(query || '').trim().split(/\s+/).map(normalize).filter(Boolean);
  if (!words.length) return entries.slice();
  const scored = [];
  entries.forEach((entry, index) => {
    let total = 0;
    for (const word of words) {
      const s = scoreWord(entry, word);
      if (!s) return;
      total += s;
    }
    scored.push({ entry, total, index });
  });
  scored.sort((a, b) => b.total - a.total || a.index - b.index);
  return scored.map((s) => s.entry);
}
