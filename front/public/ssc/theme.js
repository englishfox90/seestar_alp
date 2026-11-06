// Sets data-theme and data-bs-theme on <html> before first paint, so it blocks
// on purpose. The mode belongs to the device: auto, day, night or red.
// Auto is Day when the sun is above -6 degrees, otherwise Night, and never Red.
// Until the sun is known it uses the last cached altitude, then the server's
// uitheme (the initial data-bs-theme), and when in doubt it picks Night.
(function () {
  const root = document.documentElement;
  const MODE_KEY = "ssc-theme";
  const OLD_RED_KEY = "ssc-red";
  const SUN_KEY = "ssc-sun";
  const LAST_KEY = "ssc-theme-last";
  const MODES = ["auto", "day", "night", "red"];
  const SUN_LIMIT_DEG = -6;
  const REFRESH_MS = 5 * 60 * 1000;
  const CACHE_MAX_MS = 60 * 60 * 1000;

  function read(key) {
    try {
      return localStorage.getItem(key);
    } catch (err) {
      return null;
    }
  }

  function write(key, value) {
    try {
      if (value === null) {
        localStorage.removeItem(key);
      } else {
        localStorage.setItem(key, value);
      }
    } catch (err) {
      console.error("Could not save theme", err);
    }
  }

  // The server's uitheme arrives as the starting data-bs-theme. Anything but
  // an explicit light means night.
  let serverTheme = root.getAttribute("data-bs-theme") === "light" ? "day" : "night";

  function storedMode() {
    const value = read(MODE_KEY);
    if (MODES.includes(value)) {
      return value;
    }
    if (read(OLD_RED_KEY) === "1") {
      write(MODE_KEY, "red");
      write(OLD_RED_KEY, null);
      return "red";
    }
    return "auto";
  }

  let mode = storedMode();
  let sunAlt = null; // fetched during this page's life

  function cachedTheme() {
    try {
      const cached = JSON.parse(read(SUN_KEY));
      if (cached && Number.isFinite(cached.alt) && Number.isFinite(cached.at)) {
        // An old reading may only ever give Night.
        if (Date.now() - cached.at > CACHE_MAX_MS) {
          return "night";
        }
        return cached.alt > SUN_LIMIT_DEG ? "day" : "night";
      }
    } catch (err) {
      // fall through to the server value
    }
    return null;
  }

  function autoTheme() {
    if (sunAlt !== null) {
      return sunAlt > SUN_LIMIT_DEG ? "day" : "night";
    }
    return cachedTheme() || serverTheme || "night";
  }

  function effective() {
    return mode === "auto" ? autoTheme() : mode;
  }

  function themeColor(theme) {
    // Tokens may not be loaded yet during the first call; then keep what is there.
    const value = getComputedStyle(root).getPropertyValue("--ssc-canvas").trim();
    if (!value || !theme) {
      return "";
    }
    return value;
  }

  function updateMeta(theme) {
    const color = themeColor(theme);
    let meta = document.querySelector('meta[name="theme-color"]');
    if (!color) {
      return;
    }
    if (!meta) {
      if (!document.head) {
        return;
      }
      meta = document.createElement("meta");
      meta.setAttribute("name", "theme-color");
      document.head.appendChild(meta);
    }
    meta.setAttribute("content", color);
  }

  function apply() {
    const theme = effective();
    root.setAttribute("data-theme", theme);
    root.setAttribute("data-bs-theme", theme === "day" ? "light" : "dark");
    updateMeta(theme);
  }

  function rootPath() {
    return root.getAttribute("data-root-path") || "";
  }

  async function fetchSun() {
    if (mode !== "auto" || document.visibilityState === "hidden") {
      return;
    }
    try {
      const response = await fetch(rootPath() + "/roof/sun", { cache: "no-store" });
      if (!response.ok) {
        return;
      }
      const data = await response.json();
      const alt = Number(data && data.altitude_deg);
      if (!Number.isFinite(alt)) {
        return;
      }
      sunAlt = alt;
      write(SUN_KEY, JSON.stringify({ alt: alt, at: Date.now() }));
      if (mode === "auto") {
        apply();
      }
    } catch (err) {
      // Silent: keep the current theme.
    }
  }

  function setMode(next) {
    if (!MODES.includes(next)) {
      return;
    }
    if (mode !== "red" && next === "red") {
      write(LAST_KEY, mode);
    }
    mode = next;
    write(MODE_KEY, next);
    apply();
    fetchSun();
  }

  window.sscTheme = {
    mode() {
      return mode;
    },
    set(next) {
      setMode(next);
    },
    // Red on, or back to the mode the device had before Red.
    toggleRed() {
      if (mode === "red") {
        const back = read(LAST_KEY);
        setMode(MODES.includes(back) && back !== "red" ? back : "auto");
      } else {
        setMode("red");
      }
    },
    // Re-read the stored mode, apply it and refresh the sun altitude.
    sync() {
      mode = storedMode();
      apply();
      fetchSun();
    },
  };

  apply();

  document.addEventListener("DOMContentLoaded", function () {
    updateMeta(effective());
    fetchSun();
  });
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") {
      fetchSun();
    }
  });
  setInterval(fetchSun, REFRESH_MS);
})();
