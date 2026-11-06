// Marks popular objects on the Target List's sky view from ssc/sky_catalog.json, and says
// what a tapped one is, outside the image. Nothing here adds a target: its action hands the
// object to target_sky.js, which treats it like a search result.
//
// The whole catalog never reaches Aladin. refresh() gives it only what suits the current
// view, so each frame of a drag projects a hundred or so markers rather than two thousand.
window.SkyObjects = (function () {
  const STORE_KEY = "ssc-sky-objects";
  // Softer than the owner's frames and white labels, which stay the loudest thing on the sky.
  const MARKER = { night: "#E8D48AD0", red: "#E00000" };
  const LABEL_HALO = "rgba(0, 0, 0, 0.75)";
  const MARKER_RADIUS = 6;
  const MARKER_RADIUS_PICKED = 8;
  const TOUCH_RADIUS = 22; // half of the 44px hit area
  const HOVER_RADIUS = 14;
  const PHONE_WIDTH = 768;
  const LABEL_CHAR_WIDTH = 6.5;
  const LABEL_HEIGHT = 16;
  const LABEL_GAP = 4;
  const LABEL_MAX_FOV = 50;
  const MARGIN = 1.3; // select a little past the edges, so a drag doesn't show a bare rim
  const REFRESH_MS = 150;
  const LABELS_MS = 200;
  const LISTED_WITHIN_DEG = 2 / 60;
  // The widest view, in degrees, that shows each level. See scripts/build_sky_catalog.py.
  const LEVEL_MAX_FOV = [Infinity, 45, 18, 7];
  const MAX_MARKERS = { phone: 60, wide: 120 };
  const CROWDED_FOV = 90; // past this, a view holds the whole catalog's best in very little room
  const CROWDED_SHARE = 0.6;
  const MAX_LABELS = { phone: 14, wide: 40 };

  // Type names, and which marker shape each uses. Shape carries the type, since Red has no hues.
  const TYPES = {
    Neb: ["nebula", "nebula"],
    EmN: ["emission nebula", "nebula"],
    HII: ["emission nebula", "nebula"],
    RfN: ["reflection nebula", "nebula"],
    DrkN: ["dark nebula", "nebula"],
    "Cl+N": ["cluster with nebula", "nebula"],
    SNR: ["supernova remnant", "remnant"],
    PN: ["planetary nebula", "planetary"],
    OCl: ["open cluster", "open"],
    "*Ass": ["star association", "open"],
    "**": ["double star", "open"],
    Other: ["star group", "open"],
    GCl: ["globular cluster", "globular"],
    G: ["galaxy", "galaxy"],
    GPair: ["galaxy pair", "galaxy"],
    GTrpl: ["galaxy triplet", "galaxy"],
    GGroup: ["galaxy group", "galaxy"],
  };

  const SHAPES = {
    nebula(ctx, x, y, r) {
      ctx.rect(x - r, y - r, 2 * r, 2 * r);
    },
    remnant(ctx, x, y, r) {
      ctx.moveTo(x, y - r);
      ctx.lineTo(x + r, y + r);
      ctx.lineTo(x - r, y + r);
      ctx.closePath();
    },
    planetary(ctx, x, y, r) {
      ctx.moveTo(x, y - r);
      ctx.lineTo(x + r, y);
      ctx.lineTo(x, y + r);
      ctx.lineTo(x - r, y);
      ctx.closePath();
    },
    open(ctx, x, y, r) {
      ctx.setLineDash([2, 3]);
      ctx.arc(x, y, r, 0, 2 * Math.PI);
    },
    globular(ctx, x, y, r) {
      ctx.arc(x, y, r, 0, 2 * Math.PI);
      ctx.moveTo(x - r, y);
      ctx.lineTo(x + r, y);
      ctx.moveTo(x, y - r);
      ctx.lineTo(x, y + r);
    },
    galaxy(ctx, x, y, r) {
      ctx.ellipse(x, y, r, r * 0.45, -Math.PI / 6, 0, 2 * Math.PI);
    },
  };

  function isRed() {
    return document.documentElement.dataset.theme === "red";
  }

  function readChoice() {
    try {
      return localStorage.getItem(STORE_KEY) !== "off";
    } catch (e) {
      return true;
    }
  }

  function saveChoice(on) {
    try {
      localStorage.setItem(STORE_KEY, on ? "on" : "off");
    } catch (e) {
      console.log("Could not save the sky objects choice", e);
    }
  }

  function unit(raDeg, decDeg) {
    const ra = (raDeg * Math.PI) / 180;
    const dec = (decDeg * Math.PI) / 180;
    return [Math.cos(dec) * Math.cos(ra), Math.cos(dec) * Math.sin(ra), Math.sin(dec)];
  }

  // "Sharpless 157", "SH 2-157" and "sh2-0157" all become sh2157.
  function plain(name) {
    return String(name)
      .toLowerCase()
      .replace(/\bmessier\b/g, "m")
      .replace(/\bsharpless(\s*2)?\b|\bsh\s*2\b/g, "sh2")
      .replace(/[^a-z0-9]/g, "")
      .replace(/([a-z])0+(\d)/g, "$1$2")
      .replace(/^sh20+/, "sh2");
  }

  function withoutKind(name) {
    return name.replace(/(nebula|galaxy|galaxies|cluster)$/, "");
  }

  // What a target's name could match: the whole name, and any designation inside it, so
  // "IC 1805 Heart" counts as IC 1805. A trailing letter is another object: IC 1396A.
  function namesIn(targetName) {
    const whole = plain(targetName);
    const names = new Set([whole, withoutKind(whole)]);
    const pattern = /\b(messier|m|ngc|ic|sharpless(?:\s*2)?|sh\s*2)[\s-]*(\d+)(?![a-z0-9])/gi;
    for (const match of String(targetName).matchAll(pattern)) {
      names.add(plain(`${match[1]} ${match[2]}`));
    }
    return names;
  }

  function sizeText(arcmin) {
    if (!arcmin) return "";
    if (arcmin < 60) return `${arcmin}′`;
    return `${Number((arcmin / 60).toFixed(1))}°`;
  }

  function factsText(entry) {
    const parts = [TYPES[entry.t] ? TYPES[entry.t][0] : "object"];
    if (entry.c) parts.unshift(entry.n);
    const size = sizeText(entry.s);
    if (size) parts.push(size);
    const text = parts.join(", ");
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  // options: url, targets, onFrame(entry), onShow(target). All of the page's elements are optional
  // to the page: if the catalog can't load, the control stays hidden and the view works as before.
  function start(aladin, options) {
    const field = (id) => document.getElementById(id);
    const control = field("target-sky-objects");
    const toggle = field("target-sky-objects-on");
    const legend = field("target-sky-legend");
    const panel = field("target-sky-pick");
    const view = field("target-sky-view");
    const listedNames = options.targets.map((t) => namesIn(t.name));

    let entries = [];
    let shown = [];
    let picked = null;
    let hovered = null;
    let on = readChoice();
    let refreshTimer = null;
    let labelTimer = null;

    function paint(source, ctx) {
      const entry = source.data.entry;
      const lit = entry === picked || entry === hovered;
      if (entry.covered && !lit) return;
      const color = isRed() ? MARKER.red : MARKER.night;
      ctx.beginPath();
      ctx.strokeStyle = color;
      ctx.lineWidth = lit ? 2.5 : 1.25;
      SHAPES[entry.shape](ctx, source.x, source.y, lit ? MARKER_RADIUS_PICKED : MARKER_RADIUS);
      ctx.stroke();
      ctx.setLineDash([]);
      const text = lit ? entry.c || entry.n : entry.label;
      if (!text) return;
      ctx.font = isRed() ? "600 13px -apple-system, system-ui, sans-serif" : "12px -apple-system, system-ui, sans-serif";
      ctx.textBaseline = "middle";
      const x = source.x + MARKER_RADIUS + LABEL_GAP;
      ctx.lineWidth = 3;
      ctx.strokeStyle = LABEL_HALO;
      ctx.strokeText(text, x, source.y);
      ctx.fillStyle = color;
      ctx.fillText(text, x, source.y);
    }

    // readOnly leaves these out of Aladin's own hover and selection, which would hold on to
    // markers after refresh() has replaced them. Tapping goes through pickAt() instead.
    const layer = window.A.catalog({ name: "Sky objects", shape: paint, sourceSize: 12, readOnly: true, onClick: "none" });
    aladin.addCatalog(layer);

    function wanted() {
      const [fovW, fovH] = aladin.getFov();
      const fov = Math.max(fovW, fovH);
      let top = 0;
      while (top < LEVEL_MAX_FOV.length - 1 && fov <= LEVEL_MAX_FOV[top + 1]) top += 1;
      const [ra, dec] = aladin.getRaDec();
      const middle = unit(ra, dec);
      const radius = Math.min(180, (Math.hypot(fovW, fovH) / 2) * MARGIN);
      const least = Math.cos((radius * Math.PI) / 180);
      const room = view.clientWidth < PHONE_WIDTH ? MAX_MARKERS.phone : MAX_MARKERS.wide;
      const most = fov > CROWDED_FOV ? Math.round(room * CROWDED_SHARE) : room;
      const found = [];
      if (picked) found.push(picked);
      // entries are in priority order, so stopping at the cap keeps the best.
      for (const entry of entries) {
        if (found.length >= most) break;
        if (entry.l > top || entry === picked) continue;
        const v = entry.v;
        if (v[0] * middle[0] + v[1] * middle[1] + v[2] * middle[2] >= least) found.push(entry);
      }
      return found;
    }

    function refresh() {
      refreshTimer = null;
      if (!on || !entries.length) return;
      const next = wanted();
      if (next.length === shown.length && next.every((entry, i) => entry === shown[i])) return;
      shown = next;
      layer.removeAll();
      layer.addSources(
        shown.map(function (entry) {
          entry.source = entry.source || window.A.source(entry.r, entry.d, { entry: entry });
          return entry.source;
        })
      );
    }

    function pixel(ra, dec) {
      try {
        const place = aladin.world2pix(ra, dec);
        return place && Number.isFinite(place[0]) && Number.isFinite(place[1]) ? place : null;
      } catch (e) {
        return null; // behind the projection
      }
    }

    function overlaps(a, boxes) {
      return boxes.some((b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h);
    }

    function box(x, y, w, h) {
      return { x: x, y: y, w: w, h: h };
    }

    // Greedy, in priority order: an object gets a label only where it hits nothing already placed.
    function placeLabels() {
      labelTimer = null;
      const width = view.clientWidth;
      const height = view.clientHeight;
      // The owner's own markers and labels come first. A sky object that would sit on one
      // isn't drawn at all, so nothing muddies them. Zooming in separates the two.
      const owned = [];
      options.targets.forEach(function (t) {
        const place = pixel(t.ra, t.dec);
        if (!place) return;
        owned.push(box(place[0] - MARKER_RADIUS, place[1] - LABEL_HEIGHT / 2, t.name.length * LABEL_CHAR_WIDTH * 1.2 + 2 * MARKER_RADIUS, LABEL_HEIGHT));
      });
      const boxes = owned.slice();
      const placed = [];
      shown.forEach(function (entry) {
        entry.label = null;
        entry.covered = false;
        const place = pixel(entry.r, entry.d);
        if (!place || place[0] < 0 || place[1] < 0 || place[0] > width || place[1] > height) return;
        const marker = box(place[0] - MARKER_RADIUS, place[1] - MARKER_RADIUS, 2 * MARKER_RADIUS, 2 * MARKER_RADIUS);
        entry.covered = overlaps(marker, owned);
        if (entry.covered) return;
        placed.push([entry, place]);
        boxes.push(marker);
      });
      const fov = Math.max(...aladin.getFov());
      if (on && fov <= LABEL_MAX_FOV) {
        const most = width < PHONE_WIDTH ? MAX_LABELS.phone : MAX_LABELS.wide;
        let count = 0;
        for (const [entry, place] of placed) {
          if (count >= most) break;
          const text = entry.c || entry.n;
          const left = place[0] + MARKER_RADIUS + LABEL_GAP;
          const area = box(left, place[1] - LABEL_HEIGHT / 2, text.length * LABEL_CHAR_WIDTH, LABEL_HEIGHT);
          if (entry === picked) {
            boxes.push(area); // always labelled, and first in the order, so others keep clear
            continue;
          }
          if (listed(entry)) continue; // its name is already on the sky, in the owner's own label
          if (left + area.w > width || overlaps(area, boxes)) continue;
          boxes.push(area);
          entry.label = text;
          count += 1;
        }
      }
      layer.reportChange();
    }

    function onViewChanged() {
      if (!on) return;
      if (refreshTimer === null) refreshTimer = setTimeout(refresh, REFRESH_MS);
      clearTimeout(labelTimer);
      labelTimer = setTimeout(placeLabels, LABELS_MS);
    }

    // The owner's listed target that is this object: by name, or sitting right on it.
    function listed(entry) {
      if (entry.listed !== undefined) return entry.listed;
      const names = [entry.n, ...(entry.a || [])].map(plain);
      if (entry.c) names.push(plain(entry.c), withoutKind(plain(entry.c)));
      const cosDec = Math.cos((entry.d * Math.PI) / 180);
      entry.listed =
        options.targets.find(function (t, i) {
          if (names.some((name) => listedNames[i].has(name))) return true;
          let dRa = Math.abs(t.ra - entry.r);
          if (dRa > 180) dRa = 360 - dRa;
          return Math.hypot(dRa * cosDec, t.dec - entry.d) <= LISTED_WITHIN_DEG;
        }) || null;
      return entry.listed;
    }

    function closest(x, y, reach) {
      let best = null;
      let bestDistance = reach;
      for (const entry of shown) {
        const source = entry.source;
        if (!source || !source.x || !source.y || entry.covered) continue;
        const distance = Math.hypot(source.x - x, source.y - y);
        if (distance <= bestDistance) {
          best = entry;
          bestDistance = distance;
        }
      }
      return best;
    }

    function setPicked(entry) {
      picked = entry;
      panel.hidden = !entry;
      if (!entry) {
        placeLabels();
        return;
      }
      field("target-sky-pick-name").textContent = entry.c || entry.n;
      field("target-sky-pick-facts").textContent = factsText(entry);
      const target = listed(entry);
      field("target-sky-pick-listed").hidden = !target;
      field("target-sky-pick-listed").textContent = target ? `In your targets as ${target.name}` : "";
      field("target-sky-pick-action").textContent = target ? `Show ${target.name}` : `Frame ${entry.n}`;
      refresh();
      placeLabels();
      panel.scrollIntoView({ block: "nearest" });
    }

    function setOn(next) {
      on = next;
      saveChoice(on);
      toggle.checked = on;
      legend.hidden = !on;
      if (on) {
        layer.show();
        shown = [];
        refresh();
        placeLabels();
      } else {
        setPicked(null);
        layer.hide();
      }
    }

    function onHover(event) {
      const entry = on ? closest(event.x, event.y, HOVER_RADIUS) : null;
      if (entry !== hovered) {
        hovered = entry;
        layer.reportChange();
      }
      if (entry) {
        // Aladin sets the cursor back to default after this callback returns.
        queueMicrotask(function () {
          view.querySelector(".aladin-catalogCanvas").style.cursor = "pointer";
        });
      }
    }

    toggle.checked = on;
    toggle.addEventListener("change", () => setOn(toggle.checked));
    field("target-sky-pick-action").addEventListener("click", function () {
      const entry = picked;
      if (!entry) return;
      const target = listed(entry);
      setPicked(null);
      if (target) options.onShow(target);
      else options.onFrame(entry);
    });
    new MutationObserver(() => layer.reportChange()).observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });

    fetch(options.url)
      .then(function (reply) {
        if (!reply.ok) throw new Error(String(reply.status));
        return reply.json();
      })
      .then(function (catalog) {
        entries = catalog.objects.map(function (entry) {
          entry.v = unit(entry.r, entry.d);
          entry.shape = (TYPES[entry.t] || TYPES.Other)[1];
          return entry;
        });
        control.hidden = false;
        legend.hidden = !on;
        if (!on) layer.hide();
        ["positionChanged", "zoomChanged", "projectionChanged", "resizeChanged"].forEach((name) => aladin.on(name, onViewChanged));
        if (window.matchMedia("(hover: hover)").matches) aladin.on("mouseMove", onHover);
        refresh();
        placeLabels();
      })
      .catch(function (e) {
        console.log("The sky objects could not load", e);
      });

    return {
      // Shows what's at this pixel of the view, or clears the panel. Says whether it found one.
      pickAt(x, y) {
        const entry = on ? closest(x, y, TOUCH_RADIUS) : null;
        setPicked(entry);
        return Boolean(entry);
      },
      clear() {
        if (picked) setPicked(null);
      },
      // For the page's own redraws, such as after a jump.
      changed: onViewChanged,
    };
  }

  return { start: start };
})();
