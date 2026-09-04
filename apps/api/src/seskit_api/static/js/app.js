/* SESKit dashboard behaviour.
 *
 * Deliberately small. HTMX handles server interaction; this file covers only
 * what needs to happen before or without a request.
 */
(function () {
  "use strict";

  var STORAGE_KEY = "seskit-theme";

  /* Theme -----------------------------------------------------------------
   * Three states, matching the CSS: "light" and "dark" stamp data-theme on
   * <html>; "system" removes it and lets prefers-color-scheme decide.
   */

  function storedTheme() {
    try {
      return localStorage.getItem(STORAGE_KEY);
    } catch (e) {
      // Private windows and blocked site-data throw on access.
      return null;
    }
  }

  function applyTheme(theme) {
    if (theme === "light" || theme === "dark") {
      document.documentElement.setAttribute("data-theme", theme);
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
  }

  function systemTheme() {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  }

  function effectiveTheme() {
    return storedTheme() || systemTheme();
  }

  function toggleTheme() {
    var next = effectiveTheme() === "dark" ? "light" : "dark";
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch (e) {
      // Not persisting is survivable; applying it still is not.
    }
    applyTheme(next);
    syncToggleLabel();
  }

  function syncToggleLabel() {
    var button = document.querySelector("[data-theme-toggle]");
    if (!button) return;
    var isDark = effectiveTheme() === "dark";
    button.setAttribute("aria-label", isDark ? "Switch to light theme" : "Switch to dark theme");
    button.setAttribute("aria-pressed", String(isDark));
    var sun = button.querySelector("[data-icon='sun']");
    var moon = button.querySelector("[data-icon='moon']");
    if (sun) sun.hidden = !isDark;
    if (moon) moon.hidden = isDark;
  }

  /* Copy to clipboard ------------------------------------------------------
   * Used for DNS records (Phase 5) and API keys (Phase 3). Wired here so the
   * behaviour exists as soon as those pages do.
   */

  function initCopy() {
    document.addEventListener("click", function (event) {
      var trigger = event.target.closest("[data-copy]");
      if (!trigger) return;

      var value = trigger.getAttribute("data-copy");
      if (!value || !navigator.clipboard) return;

      navigator.clipboard.writeText(value).then(function () {
        var previous = trigger.getAttribute("data-copy-label") || trigger.textContent;
        trigger.setAttribute("data-copy-label", previous);
        trigger.textContent = "Copied";
        setTimeout(function () {
          trigger.textContent = previous;
        }, 1500);
      });
    });
  }

  /* Toast ------------------------------------------------------------------
   * The server renders the message into the document; this floats it and takes
   * it away again. Doing it in this order means the text is in the HTML for a
   * screen reader and for anyone with scripts blocked, rather than existing
   * only if JavaScript arrives.
   */

  var TOAST_VISIBLE_MS = 5000;

  function dismissToast(toast) {
    if (toast && toast.parentNode) toast.parentNode.removeChild(toast);
  }

  function initToast() {
    var toast = document.querySelector("[data-toast]");
    if (!toast) return;

    // Only now does it become a floating element. Until this attribute lands
    // it is an ordinary block at the top of the page, which is what a
    // scripts-blocked reader sees.
    toast.setAttribute("data-toast-floating", "");

    var timer = setTimeout(function () {
      dismissToast(toast);
    }, TOAST_VISIBLE_MS);

    toast.addEventListener("click", function (event) {
      if (!event.target.closest("[data-toast-dismiss]")) return;
      clearTimeout(timer);
      dismissToast(toast);
    });

    // A message that disappears while being read is worse than one that
    // overstays, and a pointer resting on it is the clearest signal that it is
    // being read.
    toast.addEventListener("mouseenter", function () {
      clearTimeout(timer);
    });
  }

  /* Init ------------------------------------------------------------------ */

  applyTheme(storedTheme());

  document.addEventListener("DOMContentLoaded", function () {
    syncToggleLabel();
    initCopy();
    initToast();

    var toggle = document.querySelector("[data-theme-toggle]");
    if (toggle) toggle.addEventListener("click", toggleTheme);

    // Follow the OS while the user has not made an explicit choice.
    if (window.matchMedia) {
      window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
        if (!storedTheme()) syncToggleLabel();
      });
    }
  });
})();
