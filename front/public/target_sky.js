// The Target List's sky view: every target's frame on one sky, adding new targets
// from a search, and adjusting the framing of a listed one. Nothing here commands
// the scope. Saving posts to the page's normal forms.
(function () {
  const COLOR_SELECTED = "#0A84FF";
  const COLOR_OTHER = "#AEAEB2";
  const MODELS_FALLBACK = "S30 Pro";

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
  // The frame being adjusted. target is null while it's a new one from a search.
  const current = { target: null, ra: null, dec: null };

  function setStatus(text) {
    field("target-sky-status").textContent = text;
  }

  function scale() {
    return parseFloat(field("target-sky-scale").value);
  }

  function angle() {
    return parseFloat(field("target-sky-angle").value);
  }

  function polygon(ra, dec, frameScale, frameAngle, color, lineWidth) {
    const corners = window.FrameGeometry.corners(ra, dec, fovW, fovH, frameScale, frameAngle);
    return window.A.polygon(corners, { color: color, lineWidth: lineWidth });
  }

  function draw() {
    if (!aladin) return;
    frames.removeAll();
    targets.forEach(function (t) {
      if (current.target && t.id === current.target.id) return;
      frames.add(polygon(t.ra, t.dec, t.scale, t.angle, COLOR_OTHER, 1));
    });
    if (current.ra !== null) {
      frames.add(polygon(current.ra, current.dec, scale(), angle(), COLOR_SELECTED, 3));
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
  }

  function select(target) {
    current.target = target;
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
    const catalog = window.A.catalog({
      name: "Targets",
      sourceSize: 10,
      color: COLOR_OTHER,
      displayLabel: true,
      labelColumn: "name",
      labelColor: "#FFFFFF",
      labelFont: "13px -apple-system, system-ui, sans-serif",
      onClick: "none",
    });
    aladin.addCatalog(catalog);
    catalog.addSources(targets.map((t) => window.A.source(t.ra, t.dec, { name: t.name, id: t.id })));
    aladin.on("objectClicked", function (object) {
      const id = object && object.data && object.data.id;
      const target = targets.find((t) => t.id === id);
      if (target) select(target);
    });
  }

  function start() {
    aladin = window.A.aladin("#target-sky-view", {
      survey: window.ALADIN_SURVEY,
      fov: 60,
      target: "0 0",
      showGotoControl: false,
      showReticle: true,
    });
    frames = window.A.graphicOverlay({ color: COLOR_OTHER, lineWidth: 1 });
    aladin.addOverlay(frames);
    labels();
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
