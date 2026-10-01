// 在页面绘制前同步执行：按记忆的主题给 <html> 加上 dark，避免先亮后暗的闪烁。
(function () {
  var mode = null;
  try { mode = localStorage.getItem('wb-theme'); } catch (e) { mode = null; }
  var dark = mode === 'dark' || (mode !== 'light' && window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  if (dark) document.documentElement.classList.add('dark');
})();
