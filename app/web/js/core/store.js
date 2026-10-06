import { getJSON, postJSON } from './api.js';

// 跨页面共享的状态；页面用 subscribe 监听变化，变化原因写在 reason 里。
const state = {
  viewRealm: 'intl',     // 页面正在查看的版本（右上角切换）
  activeRealm: 'intl',   // 网关默认出口：没有绑定版本的 Key 走这一个
  panelStatus: {},       // GET /panel/status
  version: '',
};

const listeners = new Set();

export function getState() {
  return state;
}

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function emit(reason) {
  for (const fn of [...listeners]) fn(reason, state);
}

export function setViewRealm(realm) {
  if (realm !== 'intl' && realm !== 'cn') throw new Error('unknown realm: ' + realm);
  if (state.viewRealm === realm) return;
  state.viewRealm = realm;
  const url = new URL(location.href);
  url.searchParams.set('view', realm);
  history.replaceState(null, '', url.pathname + url.search + url.hash);
  emit('viewRealm');
}

/** 只刷新网关默认出口，不改动正在查看的版本。 */
export async function refreshActiveRealm() {
  const data = await getJSON('/realm');
  if (data.current && data.current !== state.activeRealm) {
    state.activeRealm = data.current;
    emit('activeRealm');
  }
  return state.activeRealm;
}

export async function switchActiveRealm(realm) {
  const data = await postJSON('/realm', { realm });
  state.activeRealm = data.current || realm;
  emit('activeRealm');
  return state.activeRealm;
}

/** 启动时调用一次：查看的版本优先取地址栏 ?view=，否则跟随网关默认出口。 */
export async function initRealms() {
  const data = await getJSON('/realm');
  state.activeRealm = data.current || 'intl';
  const pinned = new URLSearchParams(location.search).get('view');
  state.viewRealm = pinned === 'cn' || pinned === 'intl' ? pinned : state.activeRealm;
  emit('init');
}

export function setPanelStatus(status) {
  state.panelStatus = status || {};
  emit('panelStatus');
}

export function setVersion(version) {
  state.version = version || '';
  emit('version');
}
