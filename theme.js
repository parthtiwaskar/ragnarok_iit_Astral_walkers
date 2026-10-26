// Light or dark theme. Loaded in <head> so the right colours are set before first paint (no flash).
// Order of choice: the visitor's saved choice, else the operating system's setting.
(function () {
  var KEY = "ragnarisk-theme", root = document.documentElement, saved = null;
  try { saved = localStorage.getItem(KEY); } catch (e) { /* storage blocked: follow the system */ }
  var theme = saved === "light" || saved === "dark" ? saved : (window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
  function apply(value) {
    root.dataset.theme = value;
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", value === "light" ? "#f4f6fa" : "#090e1a");
  }
  apply(theme);
  window.RagnariskTheme = {
    get: function () { return root.dataset.theme; },
    set: function (value) {
      apply(value);
      try { localStorage.setItem(KEY, value); } catch (e) { /* not saved, still applied */ }
      document.dispatchEvent(new CustomEvent("themechange", { detail: value }));
    },
  };
  document.addEventListener("DOMContentLoaded", function () { apply(root.dataset.theme); });
})();
