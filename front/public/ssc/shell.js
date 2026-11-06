// The shell's menus and sheets, on native <details> and <dialog>. No Bootstrap.
(function () {
  const rootPath = document.documentElement.dataset.rootPath || "";

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
      return;
    }
    if (event.target.closest("[data-dark-toggle]")) {
      const root = document.documentElement;
      root.setAttribute("data-bs-theme", root.getAttribute("data-bs-theme") === "dark" ? "light" : "dark");
      window.sscTheme.sync();
      fetch(rootPath + "/toggleuitheme").catch(function (err) {
        console.error("Failed to toggle ui theme", err);
      });
      return;
    }
    // A click on the sheet's own backdrop targets the dialog itself.
    if (event.target.matches("dialog.ssc-sheet")) event.target.close();
    // Only one menu open, and a click elsewhere closes it.
    document.querySelectorAll("details.ssc-menu[open]").forEach(function (menu) {
      if (!menu.contains(event.target)) menu.open = false;
    });
  });
})();
