// Menus, sheets and the theme controls, on native <details> and <dialog>. No Bootstrap.
// Loaded by the shell and by base.html, so the nav behaves the same on both.
(function () {
  function closeMenus(except) {
    document.querySelectorAll("details.ssc-menu[open]").forEach(function (menu) {
      if (!menu.contains(except)) menu.open = false;
    });
  }

  // Marks Red in the scope menu and the current mode in any appearance control.
  function syncThemeControls() {
    if (!window.sscTheme) return;
    const mode = window.sscTheme.mode();
    document.querySelectorAll("[data-red-toggle]").forEach(function (button) {
      button.setAttribute("aria-pressed", String(mode === "red"));
    });
    document.querySelectorAll("[data-theme-mode]").forEach(function (button) {
      button.setAttribute("aria-pressed", String(button.dataset.themeMode === mode));
    });
  }

  document.addEventListener("click", function (event) {
    const opener = event.target.closest("[data-open-sheet]");
    if (opener) {
      const sheet = document.getElementById(opener.dataset.openSheet);
      if (sheet && !sheet.open) sheet.showModal();
      return;
    }
    if (event.target.closest("[data-close-sheet]")) {
      event.target.closest("dialog").close();
      return;
    }
    if (event.target.closest("[data-red-toggle]")) {
      window.sscTheme.toggleRed();
      syncThemeControls();
      closeMenus(null);
      return;
    }
    const modeButton = event.target.closest("[data-theme-mode]");
    if (modeButton) {
      window.sscTheme.set(modeButton.dataset.themeMode);
      syncThemeControls();
      return;
    }
    // A click on the sheet's own backdrop targets the dialog itself.
    if (event.target.matches("dialog.ssc-sheet")) event.target.close();
    // Only one menu open, and a click elsewhere closes it.
    closeMenus(event.target);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closeMenus(null);
  });

  // A poll that comes back empty must not blank the page.
  document.addEventListener("htmx:beforeSwap", function (event) {
    const target = event.detail.target;
    if (!target || !target.hasAttribute("data-keep-on-empty")) return;
    if (!(event.detail.xhr.responseText || "").trim()) event.detail.shouldSwap = false;
  });

  // A control can arrive in a swapped fragment.
  document.addEventListener("htmx:afterSettle", syncThemeControls);

  // A sub-navigation scrolled inside itself shows the current page without moving the window.
  document.querySelectorAll("[data-subnav]").forEach(function (strip) {
    const current = strip.querySelector('[aria-current="page"]');
    if (!current) return;
    const stripBox = strip.getBoundingClientRect();
    const box = current.getBoundingClientRect();
    if (box.left < stripBox.left || box.right > stripBox.right) {
      strip.scrollLeft += box.left - stripBox.left - (stripBox.width - box.width) / 2;
    }
  });

  syncThemeControls();
})();
