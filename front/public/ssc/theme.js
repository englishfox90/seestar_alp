// Sets data-theme on <html> before first paint, so it blocks on purpose.
// Day and night still follow the server's uitheme switch. Red belongs to the
// device, because the pier and a remote screen need different answers.
(function () {
  const root = document.documentElement;
  const KEY = "ssc-red";

  function redStored() {
    try {
      return localStorage.getItem(KEY) === "1";
    } catch (err) {
      return false;
    }
  }

  let base = root.getAttribute("data-bs-theme") === "light" ? "light" : "dark";

  function apply() {
    if (redStored()) {
      root.setAttribute("data-theme", "red");
      root.setAttribute("data-bs-theme", "dark");
    } else {
      root.setAttribute("data-theme", base === "light" ? "day" : "night");
      root.setAttribute("data-bs-theme", base);
    }
  }

  window.sscTheme = {
    // Called after something else changed data-bs-theme, such as the Dark Mode switch.
    sync() {
      base = root.getAttribute("data-bs-theme") === "light" ? "light" : "dark";
      apply();
    },
    toggleRed() {
      try {
        if (redStored()) {
          localStorage.removeItem(KEY);
        } else {
          localStorage.setItem(KEY, "1");
        }
      } catch (err) {
        console.error("Could not save red mode", err);
      }
      apply();
    },
  };

  apply();
})();
