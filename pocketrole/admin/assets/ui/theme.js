/* PocketRole Admin – Theme Manager (load synchronously in <head>) */
(function () {
  var STORAGE_KEY = 'pr-admin-theme';
  var stored = null;
  try { stored = localStorage.getItem(STORAGE_KEY); } catch (_) {}
  var prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  var theme = stored || (prefersDark ? 'dark' : 'light');
  document.documentElement.setAttribute('data-theme', theme);

  window.adminSetTheme = function (t) {
    document.documentElement.setAttribute('data-theme', t);
    try { localStorage.setItem(STORAGE_KEY, t); } catch (_) {}
    document.querySelectorAll('.admin-theme-toggle').forEach(function (btn) {
      btn.setAttribute('aria-label', t === 'dark' ? 'ライトモードに切替' : 'ダークモードに切替');
      btn.setAttribute('data-theme-current', t);
    });
  };

  window.adminToggleTheme = function () {
    var current = document.documentElement.getAttribute('data-theme');
    window.adminSetTheme(current === 'dark' ? 'light' : 'dark');
  };

  window.adminCurrentTheme = function () {
    return document.documentElement.getAttribute('data-theme') || 'light';
  };
})();
