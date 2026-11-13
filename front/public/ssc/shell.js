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

  // Loading placeholders (ui.skeleton). A scope that is slow to answer says so, and a fetch
  // that fails offers a retry, so a placeholder never just sits there.
  function skeletonFor(target) {
    if (!target) return null;
    if (target.matches("[data-skeleton]")) return target;
    return target.querySelector(":scope > [data-skeleton]");
  }
  function setSkeletonText(skeleton, text) {
    const label = skeleton.querySelector("[data-skeleton-text]");
    if (label) label.textContent = text;
  }
  function watchSkeletons(root) {
    root.querySelectorAll("[data-skeleton]").forEach(function (skeleton) {
      if (skeleton.dataset.watched) return;
      skeleton.dataset.watched = "true";
      setTimeout(function () {
        if (skeleton.isConnected && !skeleton.dataset.failed) {
          setSkeletonText(skeleton, "Still waiting for the scope…");
        }
      }, 6000);
    });
  }
  watchSkeletons(document);
  function onLoadFailed(event) {
    const target = event.detail.target;
    const skeleton = skeletonFor(target);
    if (!skeleton || !event.detail.requestConfig) return;
    const path = event.detail.requestConfig.path;
    skeleton.dataset.failed = "true";
    setSkeletonText(skeleton, "Couldn't load this from the scope.");
    const label = skeleton.querySelector(".ssc-skeleton__label");
    if (!label || label.querySelector("button")) return;
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "ssc-button";
    retry.dataset.kind = "plain";
    retry.textContent = "Retry";
    retry.addEventListener("click", function () {
      retry.remove();
      delete skeleton.dataset.failed;
      setSkeletonText(skeleton, "Loading from the scope…");
      htmx.ajax("GET", path, { target: target, swap: "innerHTML" });
    });
    label.appendChild(retry);
  }
  document.addEventListener("htmx:responseError", onLoadFailed);
  document.addEventListener("htmx:sendError", onLoadFailed);

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

  // Which edges of the page list have more past them, for the fade.
  document.querySelectorAll(".ssc-subnav__inner").forEach(function (list) {
    function mark() {
      const left = list.scrollLeft > 1;
      const right = list.scrollLeft + list.clientWidth < list.scrollWidth - 1;
      list.dataset.more = left && right ? "both" : left ? "left" : right ? "right" : "";
    }
    list.addEventListener("scroll", mark, { passive: true });
    window.addEventListener("resize", mark);
    mark();
  });

  // Phone sheets follow a finger on the handle or title down, and close past 40% of their
  // height or on a quick flick. Short of that they spring back.
  const SHEET_DISMISS_SHARE = 0.4;
  const SHEET_FLICK = 0.11; // px per ms
  document.addEventListener("pointerdown", function (event) {
    const sheet = event.target.closest("dialog.ssc-sheet[open]");
    if (!sheet || event.pointerType === "mouse" || !window.matchMedia("(max-width: 767.98px)").matches) return;
    if (!event.target.closest(".ssc-sheet__grab, .ssc-sheet__title")) return;
    const startY = event.clientY;
    let lastY = startY;
    let lastT = event.timeStamp;
    let speed = 0;
    let moved = false;
    sheet.setPointerCapture(event.pointerId);
    function move(e) {
      const dy = Math.max(0, e.clientY - startY);
      if (!moved && dy < 4) return;
      moved = true;
      speed = (e.clientY - lastY) / Math.max(1, e.timeStamp - lastT);
      lastY = e.clientY;
      lastT = e.timeStamp;
      sheet.style.transition = "none";
      sheet.style.transform = "translateY(" + dy + "px)";
    }
    function end() {
      sheet.removeEventListener("pointermove", move);
      sheet.removeEventListener("pointerup", end);
      sheet.removeEventListener("pointercancel", end);
      if (!moved) return;
      const dy = Math.max(0, lastY - startY);
      sheet.style.transition = "";
      if (dy > sheet.offsetHeight * SHEET_DISMISS_SHARE || speed > SHEET_FLICK) {
        sheet.style.transform = "translateY(100%)";
        sheet.style.opacity = "0";
        const done = parseFloat(getComputedStyle(sheet).transitionDuration) * 1000 || 0;
        setTimeout(function () {
          sheet.close();
          sheet.style.transform = "";
          sheet.style.opacity = "";
        }, done);
      } else {
        sheet.style.transform = "";
      }
    }
    sheet.addEventListener("pointermove", move);
    sheet.addEventListener("pointerup", end);
    sheet.addEventListener("pointercancel", end);
  });
})();
