// The Target List's sky view: every target's frame on one sky, adding new targets
// from a search, and adjusting the framing of a listed one. Nothing here commands
// the scope. Saving posts to the page's normal forms.
(function () {
  // Red's floor keeps only the red channel, which would all but erase the blue.
  const COLORS = {
    night: { selected: "#0A84FF", other: "#AEAEB2", label: "#FFFFFF" },
    red: { selected: "#FF0000", other: "#FF0000", label: "#FF0000" },
  };
  const MODELS_FALLBACK = "S30 Pro";
  const TOUCH_RADIUS = 22; // half of the 44px hit area
  const TAP_MAX_MOVE = 10; // a finger shifts a little on a tap, and Aladin calls that a drag

  const card = document.getElementById("target-sky");
  if (!card) return;

  const targets = JSON.parse(document.getElementById("target-sky-data").textContent);
  const model = window.SEESTAR_MODELS[card.dataset.model] || window.SEESTAR_MODELS[MODELS_FALLBACK];
  const fovW = model.fov_x / 60.0;
  const fovH = model.fov_y / 60.0;
  const field = (id) => document.getElementById(id);
  const view = field("target-sky-view");

  let aladin = null;
  let frames = null;
  let labelLayer = null;
  let skyObjects = null;
  let press = null;
  // The frame being adjusted. target is null while it's a new one from a search.
  const current = { target: null, ra: null, dec: null };

  // Designation and common name, in the characters a target name may use.
  function catalogName(entry) {
    const common = (entry.c || "").replace(/[^A-Za-z0-9 _.-]/g, "");
    return common ? `${entry.n} ${common}` : entry.n;
  }

  // ---- what's out of reach on the chosen night ----------------------------------------
  const REACH_KEY = "ssc-sky-reach";
  const SHADE_SIZE_DEG = 5;
  // A pale wash, because the sky is already black: darkening it wouldn't show. The same in
  // Red, where the floor turns it red. The dashed edge says where the limit is.
  const SHADE_FILL = "rgba(150, 150, 160, 0.28)";
  const SHADE_EDGE = "rgba(255, 255, 255, 0.75)";
  let shadeLayer = null;
  let shadePending = false;
  let shadeBands = [];
  let shadeCells = new Set();
  let win = null;
  let reachCache = new Map();

  function reachOf(entry) {
    let found = reachCache.get(entry);
    if (!found) {
      found = window.SkyReach.reach(win, entry.r, entry.d);
      reachCache.set(entry, found);
    }
    return found;
  }

  function reachOn() {
    return field("target-sky-reach-on").checked;
  }

  function isOut(entry) {
    return Boolean(win && win.steps.length && reachOn() && reachOf(entry).first === null);
  }

  function reachText(entry) {
    if (!win || !win.steps.length) return "";
    const found = reachOf(entry);
    const floor = `${win.floor}°`;
    if (found.never) return `Never gets above ${floor} from here`;
    if (found.first === null) {
      return `Not above ${floor} in the dark that night. It peaks at ${Math.round(found.peak)}°`;
    }
    const from = win.steps[found.first].at;
    const to = win.steps[found.last].at;
    return `Above ${floor} from ${from} to ${to}, peak ${Math.round(found.peak)}°`;
  }

  // Its own canvas, between the sky and the markers. Aladin's filled polygons dim the wrong
  // parts of a whole-sky view, where a band wraps around, and leave seams between bands.
  function shadeCanvas() {
    if (!shadeLayer) {
      const sky = document.querySelector("#target-sky-view canvas.aladin-imageCanvas");
      if (!sky) return null;
      shadeLayer = document.createElement("canvas");
      shadeLayer.className = "plan-sky__shade";
      shadeLayer.setAttribute("aria-hidden", "true");
      sky.after(shadeLayer);
    }
    return shadeLayer;
  }

  function paintShade() {
    shadePending = false;
    const canvas = shadeCanvas();
    if (!canvas) return;
    const box = canvas.parentElement.getBoundingClientRect();
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(box.width * ratio);
    canvas.height = Math.round(box.height * ratio);
    if (!win || !win.steps.length || !reachOn()) return;
    const ctx = canvas.getContext("2d");
    ctx.scale(ratio, ratio);
    ctx.fillStyle = SHADE_FILL;
    const isCellOut = (ra, dec) => shadeCells.has(`${ra},${dec}`);
    const edges = new Path2D();
    // In a whole-sky view, a cell this wide has wrapped around the edge of the projection.
    // Zoomed in, a cell can simply be wider than the view.
    const wrapping = aladin.getFov()[0] > 100;
    const tooWide = Math.max(box.width, box.height) / 3;
    ctx.beginPath();
    shadeBands.forEach(function (band) {
      for (let ra = band.raStart; ra < band.raEnd; ra += SHADE_SIZE_DEG) {
        const corners = [
          aladin.world2pix(ra, band.dec),
          aladin.world2pix(ra + SHADE_SIZE_DEG, band.dec),
          aladin.world2pix(ra + SHADE_SIZE_DEG, band.dec + SHADE_SIZE_DEG),
          aladin.world2pix(ra, band.dec + SHADE_SIZE_DEG),
        ];
        if (corners.some((c) => !c)) continue;
        const xs = corners.map((c) => c[0]);
        const ys = corners.map((c) => c[1]);
        if (wrapping && (Math.max(...xs) - Math.min(...xs) > tooWide || Math.max(...ys) - Math.min(...ys) > tooWide)) continue;
        ctx.moveTo(corners[0][0], corners[0][1]);
        for (let i = 1; i < 4; i++) ctx.lineTo(corners[i][0], corners[i][1]);
        ctx.closePath();
        // An edge is drawn only where the next cell over is in reach.
        const next = (360 + ra + SHADE_SIZE_DEG) % 360;
        const last = (360 + ra - SHADE_SIZE_DEG) % 360;
        const neighbours = [
          [corners[0], corners[1], band.dec - SHADE_SIZE_DEG >= -90 && !isCellOut(ra, band.dec - SHADE_SIZE_DEG)],
          [corners[1], corners[2], !isCellOut(next, band.dec)],
          [corners[2], corners[3], band.dec + SHADE_SIZE_DEG < 90 && !isCellOut(ra, band.dec + SHADE_SIZE_DEG)],
          [corners[3], corners[0], !isCellOut(last, band.dec)],
        ];
        neighbours.forEach(function ([a, b, inReach]) {
          if (!inReach) return;
          edges.moveTo(a[0], a[1]);
          edges.lineTo(b[0], b[1]);
        });
      }
    });
    ctx.fill("nonzero");
    ctx.strokeStyle = SHADE_EDGE;
    ctx.lineWidth = 1.25;
    ctx.setLineDash([5, 4]);
    ctx.stroke(edges);
  }

  function drawShade() {
    if (shadePending) return;
    shadePending = true;
    window.requestAnimationFrame(paintShade);
  }

  function showReachNote() {
    const note = field("target-sky-reach-note");
    if (!win.steps.length) {
      note.textContent = `No full darkness on the night of ${win.night}.`;
      return;
    }
    const first = win.steps[0].at;
    const last = win.steps[win.steps.length - 1].at;
    // Where the sky is out of reach whatever the date.
    const limit = -(90 - win.floor - win.lat);
    note.textContent =
      `Dark ${first} to ${last} on the night of ${win.night}. Shaded sky stays below ${win.floor}° ` +
      `all that time. South of Dec ${limit.toFixed(1)}° never rises that high from here.`;
  }

  function loadReach(days) {
    return fetch(`${card.dataset.skyWindow}?days=${days}`)
      .then(function (reply) {
        if (!reply.ok) throw new Error(String(reply.status));
        return reply.json();
      })
      .then(function (data) {
        if (!data.known) return;
        win = data;
        reachCache = new Map();
        shadeBands = win.steps.length ? window.SkyReach.outOfReach(win, SHADE_SIZE_DEG) : [];
        shadeCells = new Set();
        shadeBands.forEach(function (band) {
          for (let ra = band.raStart; ra < band.raEnd; ra += SHADE_SIZE_DEG) shadeCells.add(`${ra},${band.dec}`);
        });
        field("target-sky-reach").hidden = false;
        showReachNote();
        drawShade();
        if (skyObjects) skyObjects.reachChanged();
      })
      .catch(function (e) {
        console.log("The sky's dark hours could not load", e);
      });
  }

  function setUpReach() {
    if (!window.SkyReach || !card.dataset.skyWindow) return;
    ["positionChanged", "zoomChanged", "projectionChanged", "resizeChanged"].forEach((name) =>
      aladin.on(name, drawShade)
    );
    const toggle = field("target-sky-reach-on");
    let saved = null;
    try {
      saved = localStorage.getItem(REACH_KEY);
    } catch (e) {
      // Storage can be refused. It stays on.
    }
    toggle.checked = saved !== "0";
    toggle.addEventListener("change", function () {
      try {
        localStorage.setItem(REACH_KEY, toggle.checked ? "1" : "0");
      } catch (e) {
        // Only the choice is lost.
      }
      drawShade();
      if (skyObjects) skyObjects.reachChanged();
    });
    document.querySelectorAll('input[name="target-sky-reach-days"]').forEach(function (input) {
      input.addEventListener("change", () => loadReach(input.value));
    });
    loadReach(0);
  }

  function setStatus(text) {
    field("target-sky-status").textContent = text;
  }

  function scale() {
    return parseFloat(field("target-sky-scale").value);
  }

  function angle() {
    return parseFloat(field("target-sky-angle").value);
  }

  function colors() {
    return document.documentElement.dataset.theme === "red" ? COLORS.red : COLORS.night;
  }

  function polygon(ra, dec, frameScale, frameAngle, color, lineWidth) {
    const corners = window.FrameGeometry.corners(ra, dec, fovW, fovH, frameScale, frameAngle);
    // Aladin's default selection is green, which Red turns black.
    return window.A.polygon(corners, { color: color, lineWidth: lineWidth, selectionColor: color, hoverColor: color });
  }

  function draw() {
    if (!aladin) return;
    aladin.selectObjects(); // a selected frame would outlive removeAll
    frames.removeAll();
    const palette = colors();
    targets.forEach(function (t) {
      if (current.target && t.id === current.target.id) return;
      frames.add(polygon(t.ra, t.dec, t.scale, t.angle, palette.other, 1));
    });
    if (current.ra !== null) {
      frames.add(polygon(current.ra, current.dec, scale(), angle(), palette.selected, 3));
    }
  }

  function showControls() {
    const editing = Boolean(current.target);
    const hasFrame = current.ra !== null;
    field("target-sky-scale-value").textContent = `${scale().toFixed(1)}x`;
    field("target-sky-angle-value").textContent = `${angle()}°`;
    field("target-sky-frame-controls").hidden = !hasFrame;
    field("target-sky-add").hidden = editing || !hasFrame;
    field("target-sky-save").hidden = !editing;
    field("target-sky-heading").textContent = editing
      ? current.target.name
      : hasFrame
        ? "New target"
        : "All targets";
    if (hasFrame) {
      for (const prefix of ["target-sky-add", "target-sky-save"]) {
        field(`${prefix}-ra`).value = (current.ra / 15.0).toFixed(4);
        field(`${prefix}-dec`).value = current.dec.toFixed(4);
        field(`${prefix}-scale`).value = scale().toFixed(1);
        field(`${prefix}-angle`).value = angle();
      }
    }
    if (editing) field("target-sky-save-id").value = current.target.id;
  }

  function fit() {
    const rect = view.getBoundingClientRect();
    aladin.setProjection("SIN");
    aladin.setFoV(
      window.FrameGeometry.fitFov(fovW, fovH, scale(), angle(), rect.width, rect.height)
    );
  }

  function goTo(ra, dec) {
    current.ra = ra;
    current.dec = dec;
    aladin.gotoRaDec(ra, dec);
    fit();
    draw();
    showControls();
    // The whole point of looking at a frame: can it be imaged from here at all?
    field("target-sky-frame-reach").textContent = reachText({ r: ra, d: dec });
    if (skyObjects) skyObjects.changed();
  }

  function select(target) {
    current.target = target;
    if (skyObjects) skyObjects.clear();
    field("target-sky-scale").value = target.scale;
    field("target-sky-angle").value = target.angle;
    setStatus("");
    goTo(target.ra, target.dec);
  }

  // The midpoint of the targets, and how far the furthest is from it.
  function spread() {
    let x = 0;
    let y = 0;
    let z = 0;
    const unit = targets.map(function (t) {
      const ra = (t.ra * Math.PI) / 180;
      const dec = (t.dec * Math.PI) / 180;
      const v = [Math.cos(dec) * Math.cos(ra), Math.cos(dec) * Math.sin(ra), Math.sin(dec)];
      x += v[0];
      y += v[1];
      z += v[2];
      return v;
    });
    const length = Math.hypot(x, y, z) || 1;
    const middle = [x / length, y / length, z / length];
    let furthest = 0;
    unit.forEach(function (v) {
      const cos = v[0] * middle[0] + v[1] * middle[1] + v[2] * middle[2];
      furthest = Math.max(furthest, (Math.acos(Math.min(1, cos)) * 180) / Math.PI);
    });
    return {
      ra: ((Math.atan2(middle[1], middle[0]) * 180) / Math.PI + 360) % 360,
      dec: (Math.asin(middle[2]) * 180) / Math.PI,
      furthest: furthest,
    };
  }

  function showAll() {
    current.target = null;
    current.ra = null;
    current.dec = null;
    if (skyObjects) skyObjects.clear();
    field("target-sky-scale").value = 1;
    field("target-sky-angle").value = 0;
    setStatus("");
    showControls();
    draw();
    if (!targets.length) {
      aladin.setProjection("SIN");
      aladin.setFoV(60);
      return;
    }
    const middle = spread();
    const rect = view.getBoundingClientRect();
    const wanted = Math.max(10, (middle.furthest + 5) * 2 * (rect.width / Math.max(rect.height, 1)));
    aladin.gotoRaDec(middle.ra, middle.dec);
    // Past about a hemisphere the usual projection can't show the whole view.
    aladin.setProjection(wanted > 150 ? "MOL" : "SIN");
    aladin.setFoV(Math.min(wanted, 360));
    if (skyObjects) skyObjects.changed();
  }

  // Decimal degrees "313.1 30.7", or sexagesimal "20 52 22 +30 28 54".
  function parseCoordinates(text) {
    const words = text.trim().split(/[\s,:]+/);
    const parts = words.map(Number);
    if (parts.some(Number.isNaN)) return null;
    if (parts.length === 2) return parts;
    if (parts.length === 6) {
      const sign = words[3].startsWith("-") ? -1 : 1;
      return [
        (parts[0] + parts[1] / 60 + parts[2] / 3600) * 15,
        sign * (Math.abs(parts[3]) + parts[4] / 60 + parts[5] / 3600),
      ];
    }
    return null;
  }

  function startNew(ra, dec, name) {
    current.target = null;
    if (skyObjects) skyObjects.clear();
    field("target-sky-add-name").value = name;
    setStatus("");
    goTo(ra, dec);
  }

  function search() {
    const text = field("target-sky-search").value.trim();
    if (!text || !aladin) return;
    const listed = targets.find((t) => t.name.toLowerCase() === text.toLowerCase());
    if (listed) {
      select(listed);
      return;
    }
    const coordinates = parseCoordinates(text);
    if (coordinates) {
      startNew(coordinates[0], coordinates[1], "");
      return;
    }
    setStatus("Looking up...");
    fetch(`${card.dataset.simbad}?name=${encodeURIComponent(text)}`)
      .then(function (reply) {
        if (!reply.ok) throw new Error(String(reply.status));
        return reply.text();
      })
      .then(function (found) {
        // "20h45m38.0s +30d42m30.0s on". The last word is ALP's LP filter advice, which
        // isn't used: it says off for NGC 7000, an emission nebula.
        const position = found.trim().split(/\s+/).slice(0, 2).join(" ");
        const parsed = parseCoordinates(position.replace(/[hmsd]/g, " "));
        if (!parsed) throw new Error("unreadable coordinates");
        startNew(parsed[0], parsed[1], text);
      })
      .catch(function () {
        setStatus(`Could not find "${text}". You can enter coordinates instead, such as 20 52 22 +30 28 54.`);
      });
  }

  function onSlider() {
    showControls();
    draw();
  }

  function refit() {
    onSlider();
    if (aladin && current.ra !== null) fit();
  }

  function labels() {
    const palette = colors();
    labelLayer = window.A.catalog({
      name: "Targets",
      sourceSize: 10,
      color: palette.other,
      selectionColor: palette.other,
      displayLabel: true,
      labelColumn: "name",
      labelColor: palette.label,
      labelFont: "13px -apple-system, system-ui, sans-serif",
      onClick: "none",
    });
    aladin.addCatalog(labelLayer);
    labelLayer.addSources(targets.map((t) => window.A.source(t.ra, t.dec, { name: t.name, id: t.id })));
  }

  // The theme can change while the page is open, and Aladin needs concrete colours.
  function applyTheme() {
    const palette = colors();
    labelLayer.labelColor = palette.label;
    labelLayer.setSelectionColor(palette.other);
    labelLayer.setColor(palette.other);
    draw();
  }

  function targetAt(x, y) {
    let best = null;
    let bestDistance = TOUCH_RADIUS;
    targets.forEach(function (t) {
      const source = labelLayer.getSources().find((s) => s.data.id === t.id);
      if (!source || !source.x || !source.y) return;
      const distance = Math.hypot(source.x - x, source.y - y);
      if (distance <= bestDistance) {
        best = t;
        bestDistance = distance;
      }
    });
    return best;
  }

  // Where a press began, in view pixels. Aladin stops these events reaching the page, so this
  // listens on the way down. A second finger means a pinch, which is never a tap.
  function trackPress() {
    function onPress(event) {
      const fingers = event.touches ? event.touches.length : 1;
      const point = event.touches ? event.touches[0] : event;
      const rect = view.getBoundingClientRect();
      press = fingers > 1 ? null : { x: point.clientX - rect.left, y: point.clientY - rect.top };
    }
    view.addEventListener("mousedown", onPress, true);
    view.addEventListener("touchstart", onPress, { capture: true, passive: true });
  }

  // Everything tappable goes through here, so a target always wins over a sky object.
  function onSkyClick(event) {
    const start = press;
    press = null;
    if (!start || Math.hypot(event.x - start.x, event.y - start.y) > TAP_MAX_MOVE) return;
    const target = targetAt(event.x, event.y);
    if (target) {
      select(target);
    } else if (skyObjects) {
      skyObjects.pickAt(event.x, event.y);
    }
  }

  // Aladin keeps one listener per event, so a second on() silently replaces the first.
  // The markers and the shading both need to hear the view move.
  function shareViewEvents() {
    const register = aladin.on.bind(aladin);
    const listeners = {};
    aladin.on = function (name, listener) {
      if (!listeners[name]) {
        listeners[name] = [];
        register(name, (...args) => listeners[name].forEach((heard) => heard(...args)));
      }
      listeners[name].push(listener);
    };
  }

  function start() {
    aladin = window.A.aladin("#target-sky-view", {
      survey: window.ALADIN_SURVEY,
      fov: 60,
      target: "0 0",
      showGotoControl: false,
      showReticle: true,
    });
    shareViewEvents();
    setUpReach();
    frames = window.A.graphicOverlay({ color: colors().other, lineWidth: 1 });
    aladin.addOverlay(frames);
    // Aladin draws catalogs in the order added, then every overlay on top. So the owner's
    // frames and labels stay above the sky objects as long as those go first.
    if (window.SkyObjects) {
      skyObjects = window.SkyObjects.start(aladin, {
        url: card.dataset.skyCatalog,
        targets: targets,
        onFrame: (entry) => startNew(entry.r, entry.d, catalogName(entry)),
        onShow: select,
        isOut: isOut,
        reachText: reachText,
      });
    }
    labels();
    trackPress();
    aladin.on("click", onSkyClick);
    new MutationObserver(() => {
      applyTheme();
      drawShade();
    }).observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    showAll();
    const wanted = new URLSearchParams(window.location.search).get("show");
    const target = targets.find((t) => t.id === wanted);
    if (target) select(target);
  }

  field("target-sky-find").addEventListener("click", search);
  field("target-sky-search").addEventListener("keydown", function (event) {
    if (event.key === "Enter") {
      event.preventDefault();
      search();
    }
  });
  field("target-sky-all").addEventListener("click", function () {
    if (aladin) showAll();
  });
  field("target-sky-center").addEventListener("click", function () {
    if (!aladin) return;
    const [ra, dec] = aladin.getRaDec();
    current.ra = ra;
    current.dec = dec;
    draw();
    showControls();
  });
  field("target-sky-scale").addEventListener("input", refit);
  field("target-sky-angle").addEventListener("input", refit);
  document.querySelectorAll("[data-sky-show]").forEach(function (button) {
    button.addEventListener("click", function () {
      const target = targets.find((t) => t.id === button.dataset.skyShow);
      if (aladin && target) {
        select(target);
        card.scrollIntoView({ block: "start", behavior: "smooth" });
      }
    });
  });

  showControls();
  window.aladinReady.then(start).catch(function (e) {
    setStatus("The sky view could not load.");
    console.log(e);
  });
})();
