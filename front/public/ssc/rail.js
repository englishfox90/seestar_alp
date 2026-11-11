// iPhone Duo puts the camera and status bar in a column down one side, and Safari reports that
// column as a safe area on that side alone. No user agent names the Duo, so this reads the shape:
// a phone-width page with an inset on one side only gets data-rail="left" or "right" on <html>,
// and nav.css turns the tab bar into a rail there. A phone on its side has insets on both sides
// and keeps its bottom bar. Only the Home Screen app gets the rail: in the Safari browser that
// column holds Safari's own buttons and the page reports no inset, so the bar stays at the bottom.
// Duo opened is wide and its inner camera sits behind the screen, so there is no column to read:
// the installed app on a wide touch screen gets the rail on the right, where the closed phone's
// column is, instead of the sections in the top bar.
(function () {
  const root = document.documentElement;
  const MIN_INSET = 20;
  const phone = window.matchMedia("(max-width: 767.98px)");
  const app = window.matchMedia("(display-mode: standalone)");
  const wideTouch = window.matchMedia("(min-width: 768px) and (pointer: coarse)");
  const probe = document.createElement("div");
  probe.setAttribute("aria-hidden", "true");
  probe.style.cssText =
    "position:fixed;top:0;left:0;width:0;height:0;visibility:hidden;pointer-events:none;" +
    "padding-left:env(safe-area-inset-left);padding-right:env(safe-area-inset-right)";
  root.appendChild(probe);

  function check() {
    const style = getComputedStyle(probe);
    const left = parseFloat(style.paddingLeft) || 0;
    const right = parseFloat(style.paddingRight) || 0;
    let side = null;
    const installed = app.matches || window.navigator.standalone === true;
    if (installed && (phone.matches || wideTouch.matches)) {
      if (right >= MIN_INSET && left < 1) side = "right";
      else if (left >= MIN_INSET && right < 1) side = "left";
      else if (wideTouch.matches) side = "right";
    }
    if (side) {
      if (root.getAttribute("data-rail") !== side) root.setAttribute("data-rail", side);
    } else if (root.hasAttribute("data-rail")) {
      root.removeAttribute("data-rail");
    }
  }

  check();
  // On iPhone Duo the Home Screen app reports no insets at first, then the column a moment
  // later, and its width can change with no resize event. So look again after load and
  // whenever the page box changes size.
  document.addEventListener("DOMContentLoaded", check);
  window.addEventListener("load", check);
  window.addEventListener("pageshow", check);
  window.addEventListener("resize", check);
  window.addEventListener("orientationchange", check);
  if (phone.addEventListener) phone.addEventListener("change", check);
  if (app.addEventListener) app.addEventListener("change", check);
  if (wideTouch.addEventListener) wideTouch.addEventListener("change", check);
  if (window.ResizeObserver) new ResizeObserver(check).observe(root);
  setTimeout(check, 500);
})();
