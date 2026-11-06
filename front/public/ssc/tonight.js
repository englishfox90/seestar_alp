// Tonight's images, swap, freshness and Stop. Every time shown comes from the server,
// because the browser may be in another time zone. Images are never swapped by htmx:
// the old one stays until the new one has loaded.
(function () {
  const page = document.getElementById("tonight");
  const root = page.dataset.root;
  const images = document.getElementById("t-images");
  const stack = document.getElementById("t-stack");
  const pier = document.getElementById("t-pier");
  const stackCaption = document.getElementById("t-stack-caption");
  const pierCaption = document.getElementById("t-pier-caption");
  const STALE_MS = 60000;
  const FADE_MS = 650;
  let shownStack = null;
  let shownPier = null;
  let chosen = null; // the image the user swapped to, until the state's hero changes
  let lastHero = page.dataset.hero;
  let lastGood = Date.now();

  function arrange() {
    const hasStack = !stack.hidden;
    const hasPier = !pier.hidden;
    const hero = page.dataset.hero;
    const focusing = !!document.querySelector("#t-focus > *");
    let front = chosen || hero;
    if (front === "stack" && !hasStack) front = "pier";
    if (front === "pier" && !hasPier) front = hasStack ? "stack" : "none";
    if (front === "focus" || front === "plan") front = "none";
    if (focusing) front = "none";
    stack.dataset.role = front === "stack" ? "front" : "thumb";
    pier.dataset.role = front === "pier" ? "front" : "thumb";
    if (front === "none") {
      images.dataset.layout = "thumbs";
      return;
    }
    images.dataset.layout = "below";
  }

  function setRatio(name, img) {
    if (img.naturalWidth && img.naturalHeight) {
      page.style.setProperty(name, String(img.naturalWidth / img.naturalHeight));
    }
  }

  // Load first, then cross-fade over the old one, so there is never an empty frame.
  function showImage(figure, src, alt, ratioName, done) {
    const frame = figure.querySelector(".t-frame");
    const next = new Image();
    next.alt = alt;
    next.onload = function () {
      const old = frame.querySelectorAll("img");
      const first = old.length === 0;
      if (!first) next.dataset.entering = "1";
      frame.appendChild(next);
      figure.hidden = false;
      setRatio(ratioName, next);
      arrange();
      done();
      if (first) return;
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          delete next.dataset.entering;
        });
      });
      setTimeout(function () {
        old.forEach(function (img) {
          img.remove();
        });
      }, FADE_MS);
    };
    next.src = src;
  }

  // While stacking, the sentence and the Kept metric already give the target and frames,
  // unless this image is from another target. The viewer has neither, so it says all.
  function stackText(meta, whole) {
    const said = !whole && meta.target && meta.target === page.dataset.target;
    const parts = [];
    if (!said) {
      parts.push(meta.target || "Unknown target");
      if (meta.stacked) parts.push(meta.stacked + " frames");
    }
    if (meta.snr !== null && meta.snr !== undefined) parts.push("SNR " + meta.snr);
    parts.push(meta.captured_clock);
    const text = parts.join(" · ");
    return page.dataset.last === "1" ? "Last image: " + text : text;
  }

  function pierText(meta) {
    if (!meta.online) return "Sentinel is not responding. Image from " + meta.captured_clock;
    if (meta.stale) return "Image is out of date, from " + meta.captured_clock;
    return "Updated " + meta.captured_clock;
  }

  let stackMeta = null;
  let pierMeta = null;

  function applyStack(meta) {
    stackMeta = meta;
    stackCaption.textContent = stackText(meta);
  }

  function applyPier(meta) {
    pierMeta = meta;
    pierCaption.textContent = pierText(meta);
    const stale = !meta.online || meta.stale;
    if (stale) pier.dataset.stale = "1";
    else delete pier.dataset.stale;
  }

  async function pollStack() {
    if (document.hidden) return;
    try {
      const reply = await fetch(root + "/image.json", { cache: "no-store" });
      if (!reply.ok) return;
      const meta = await reply.json();
      if (meta.frame === null || meta.frame === undefined) return;
      if (meta.frame !== shownStack) {
        showImage(stack, root + "/image.jpg?v=" + encodeURIComponent(meta.frame), "Latest stacked image", "--t-stack-ratio", function () {
          shownStack = meta.frame;
          applyStack(meta);
        });
      } else {
        applyStack(meta);
      }
    } catch (err) {
      // The freshness line already reports a lost connection.
    }
  }

  async function pollPier() {
    if (document.hidden) return;
    try {
      const reply = await fetch(root + "/pier.json", { cache: "no-store" });
      if (!reply.ok) return;
      const meta = await reply.json();
      if (!meta.available) return;
      if (meta.frame !== shownPier) {
        showImage(pier, root + "/pier.jpg?v=" + encodeURIComponent(meta.frame), "Latest pier camera image from PFR Sentinel", "--t-pier-ratio", function () {
          shownPier = meta.frame;
          applyPier(meta);
        });
      } else {
        applyPier(meta);
      }
    } catch (err) {
      // The freshness line already reports a lost connection.
    }
  }

  function showFreshness() {
    const lost = Date.now() - lastGood > STALE_MS;
    document.getElementById("t-updated-word").textContent = lost ? "Connection lost. Last update" : "Updated";
    document.getElementById("t-updated").dataset.tone = lost ? "danger" : "neutral";
  }

  const stopSheet = document.getElementById("t-stop-sheet");
  const stopForm = document.getElementById("t-stop-form");
  const stopConfirm = document.getElementById("t-stop-confirm");
  const stopError = document.getElementById("t-stop-error");
  let stopBusy = false;

  function afterState() {
    const data = document.getElementById("t-state-data");
    if (!data) return;
    page.dataset.state = data.dataset.state;
    page.dataset.hero = data.dataset.hero;
    page.dataset.dim = data.dataset.dim;
    page.dataset.last = data.dataset.last;
    page.dataset.target = data.dataset.target;
    if (data.dataset.hero !== lastHero) {
      lastHero = data.dataset.hero;
      chosen = null;
    }
    if (stackMeta) applyStack(stackMeta);
    arrange();
    // Stop can stop being possible while the sheet is open.
    if (stopSheet.open && !stopBusy && data.dataset.stop !== "1") stopSheet.close();
    lastGood = Date.now();
    showFreshness();
  }

  // The parts of /tonight/state that arrive out of band morph as well, so a focused link
  // survives. htmx can't name "morph:innerHTML" in hx-swap-oob, because it reads what's
  // after the colon as a selector. Without idiomorph this leaves htmx's plain swap.
  document.body.addEventListener("htmx:oobBeforeSwap", function (event) {
    if (typeof Idiomorph === "undefined") return;
    event.detail.shouldSwap = false;
    Idiomorph.morph(event.detail.target, Array.from(event.detail.fragment.childNodes), { morphStyle: "innerHTML" });
  });

  // An empty answer must not blank the state or the hardware rows. Focus and auth are
  // different: for them, empty is how the server says there's nothing to show.
  document.body.addEventListener("htmx:beforeSwap", function (event) {
    const target = event.detail.target;
    if (!target || (target.id !== "t-now" && target.id !== "t-hardware")) return;
    if (event.detail.xhr.responseText.trim() === "") event.detail.shouldSwap = false;
  });

  document.body.addEventListener("htmx:afterSettle", function (event) {
    if (event.detail.target && event.detail.target.id === "t-now") afterState();
    if (event.detail.target && event.detail.target.id === "t-focus") arrange();
  });

  images.addEventListener("click", function (event) {
    const swap = event.target.closest("[data-swap]");
    const thumb = event.target.closest('.t-image[data-role="thumb"] .t-frame');
    const compact = window.matchMedia("(max-width: 767.98px)").matches;
    if (swap) {
      chosen = swap.dataset.swap;
      arrange();
    } else if (thumb && compact && images.dataset.layout !== "thumbs") {
      event.preventDefault();
      chosen = thumb.closest(".t-image").dataset.name;
      arrange();
    } else {
      const frame = event.target.closest(".t-frame");
      if (frame && openViewer(frame.closest(".t-image").dataset.name)) event.preventDefault();
    }
  });

  // ---- the viewer: pinch and drag on phones, wheel and drag with a mouse ----------------
  const viewer = document.getElementById("t-viewer");
  const stage = document.getElementById("t-viewer-stage");
  const viewerText = document.getElementById("t-viewer-text");
  const viewerSave = document.getElementById("t-viewer-save");
  const MAX_ZOOM = 6;
  const DOUBLE_TAP_ZOOM = 2.5;
  const DOUBLE_TAP_MS = 300;
  const view = { scale: 1, x: 0, y: 0 };
  const pointers = new Map();
  let gesture = null;
  let lastTap = 0;
  let shown = null; // the img on the stage
  let loading = null; // the full-size image on its way

  function clamp(value, low, high) {
    return Math.min(high, Math.max(low, value));
  }

  // Keeps the image over the stage: it can't be dragged out of sight.
  function applyView() {
    if (!shown) return;
    const box = stage.getBoundingClientRect();
    const fit = shown.naturalWidth ? Math.min(box.width / shown.naturalWidth, box.height / shown.naturalHeight) : 1;
    const roomX = Math.max(0, ((shown.naturalWidth || box.width) * fit * view.scale - box.width) / 2);
    const roomY = Math.max(0, ((shown.naturalHeight || box.height) * fit * view.scale - box.height) / 2);
    view.x = clamp(view.x, -roomX, roomX);
    view.y = clamp(view.y, -roomY, roomY);
    shown.style.transform = "translate(" + view.x + "px, " + view.y + "px) scale(" + view.scale + ")";
  }

  // Zoom keeping one point of the stage still, given as client coordinates.
  function zoomAt(scale, clientX, clientY) {
    const box = stage.getBoundingClientRect();
    const px = clientX - box.left - box.width / 2;
    const py = clientY - box.top - box.height / 2;
    const next = clamp(scale, 1, MAX_ZOOM);
    view.x = px - ((px - view.x) * next) / view.scale;
    view.y = py - ((py - view.y) * next) / view.scale;
    view.scale = next;
    applyView();
  }

  function startGesture() {
    const points = Array.from(pointers.values());
    if (points.length === 2) {
      gesture = {
        distance: Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y),
        midX: (points[0].x + points[1].x) / 2,
        midY: (points[0].y + points[1].y) / 2,
      };
    } else if (points.length === 1) {
      gesture = { midX: points[0].x, midY: points[0].y };
    } else {
      gesture = null;
    }
  }

  stage.addEventListener("pointerdown", function (event) {
    stage.setPointerCapture(event.pointerId);
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    startGesture();
    if (pointers.size === 1) {
      const now = event.timeStamp;
      if (now - lastTap < DOUBLE_TAP_MS) {
        zoomAt(view.scale > 1 ? 1 : DOUBLE_TAP_ZOOM, event.clientX, event.clientY);
        lastTap = 0;
      } else {
        lastTap = now;
      }
    }
  });

  stage.addEventListener("pointermove", function (event) {
    if (!pointers.has(event.pointerId) || !gesture) return;
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    const points = Array.from(pointers.values());
    if (points.length === 2 && gesture.distance) {
      const distance = Math.hypot(points[0].x - points[1].x, points[0].y - points[1].y);
      const midX = (points[0].x + points[1].x) / 2;
      const midY = (points[0].y + points[1].y) / 2;
      view.x += midX - gesture.midX;
      view.y += midY - gesture.midY;
      zoomAt((view.scale * distance) / gesture.distance, midX, midY);
      gesture = { distance: distance, midX: midX, midY: midY };
    } else if (points.length === 1) {
      view.x += points[0].x - gesture.midX;
      view.y += points[0].y - gesture.midY;
      gesture = { midX: points[0].x, midY: points[0].y };
      applyView();
    }
  });

  function endPointer(event) {
    pointers.delete(event.pointerId);
    startGesture();
  }
  stage.addEventListener("pointerup", endPointer);
  stage.addEventListener("pointercancel", endPointer);
  stage.addEventListener("wheel", function (event) {
    event.preventDefault();
    zoomAt(view.scale * Math.exp(-event.deltaY * 0.002), event.clientX, event.clientY);
  }, { passive: false });

  function showOnStage(img) {
    stage.replaceChildren(img);
    shown = img;
    img.draggable = false;
    applyView();
  }

  function openViewer(name) {
    const figure = name === "stack" ? stack : pier;
    const held = figure.querySelector(".t-frame img:last-child");
    if (!held || viewer.open) return false;
    const isStack = name === "stack";
    const meta = isStack ? stackMeta : pierMeta;
    const stamp = meta ? encodeURIComponent(meta.frame) : "";
    const text = meta ? (isStack ? stackText(meta, true) : "Pier camera · " + pierText(meta)) : "";

    view.scale = 1;
    view.x = 0;
    view.y = 0;
    viewerText.textContent = text;
    viewerSave.href = root + (isStack ? "/image.jpg?full=true" : "/pier.jpg?full=true");
    viewer.dataset.theme = document.documentElement.dataset.theme === "red" ? "red" : "night";

    // The preview at once, then the full image over it, fetched only now because it's large.
    const preview = new Image();
    preview.alt = held.alt;
    preview.src = held.currentSrc || held.src;
    viewer.showModal();
    showOnStage(preview);

    const full = new Image();
    full.alt = held.alt;
    loading = full;
    full.onload = function () {
      if (loading === full) showOnStage(full);
    };
    full.onerror = function () {
      if (loading === full) viewerText.textContent = text + " · Full size did not load, this is the preview";
    };
    full.src = root + (isStack ? "/image-view.jpg?v=" + stamp : "/pier.jpg?full=true&v=" + stamp);
    return true;
  }

  viewer.addEventListener("close", function () {
    // The event comes a moment after the dialog closes, so it may already be open again.
    if (viewer.open) return;
    // A full frame is large. Let it go.
    loading = null;
    shown = null;
    pointers.clear();
    gesture = null;
    stage.replaceChildren();
  });

  // Pressing Stop twice repeats the whole park sequence, so the button is disabled at
  // once. The server keeps it disabled across a reload until the loop has answered.
  stopForm.addEventListener("submit", async function (event) {
    event.preventDefault();
    if (stopBusy) return;
    stopBusy = true;
    stopConfirm.disabled = true;
    stopConfirm.textContent = "Stopping…";
    stopError.hidden = true;
    try {
      const reply = await fetch(stopForm.action, {
        method: "POST",
        body: new URLSearchParams(new FormData(stopForm)),
        redirect: "manual",
      });
      // The route answers with a redirect, which a manual fetch reports this way.
      if (reply.type !== "opaqueredirect" && !reply.ok) throw new Error("refused: " + reply.status);
      stopSheet.close();
      htmx.trigger("#t-now", "tonight-refresh");
    } catch (err) {
      stopError.textContent = "The stop may not have been sent. Check the state, then try again.";
      stopError.hidden = false;
    }
    stopConfirm.textContent = "Stop & park";
    stopConfirm.disabled = false;
    stopBusy = false;
  });

  document.querySelectorAll(".t-frame img").forEach(function (img) {
    const name = img.closest(".t-image").dataset.name === "stack" ? "--t-stack-ratio" : "--t-pier-ratio";
    if (img.complete) setRatio(name, img);
    else img.addEventListener("load", function () {
      setRatio(name, img);
      arrange();
    });
  });

  const first = window.sscTonight || {};
  if (first.stack) {
    shownStack = first.stack.frame;
    applyStack(first.stack);
  }
  if (first.pier) {
    shownPier = first.pier.frame;
    applyPier(first.pier);
  }
  arrange();
  pollStack();
  pollPier();
  async function pollSite() {
    if (document.hidden || !document.getElementById("t-site")) return;
    try {
      const reply = await fetch(root + "/site.json", { cache: "no-store" });
      if (!reply.ok) return;
      showSite(await reply.json());
    } catch (err) {
      // It's a courtesy from another service. Whatever was showing stays.
    }
  }

  function showSite(site) {
    const parts = [];
    if (site.building) {
      const b = site.building;
      const when = b.updated ? `, as of ${b.updated}` : "";
      parts.push(`${b.name} roof ${b.open ? "open" : "closed"}${when}${b.stale ? " (may be out of date)" : ""}.`);
    }
    if (site.schedule) {
      parts.push(`Scheduled ${site.schedule.open} to ${site.schedule.close}, weather permitting.`);
    }
    document.getElementById("t-site-roof").textContent = parts.join(" ");
    const warning = document.getElementById("t-site-warning");
    warning.hidden = !site.disagree;
    warning.textContent = site.disagree ? "Sentinel reads the roof safe, but the building reports it closed." : "";
    // Text from another service: only ever set as text.
    const news = document.getElementById("t-site-news");
    news.replaceChildren(
      ...site.news.map(function (item) {
        const notice = document.createElement("p");
        notice.className = "ssc-notice t-site__news";
        notice.dataset.tone = item.tone;
        const title = document.createElement("strong");
        title.textContent = item.title;
        const body = document.createElement("span");
        body.textContent = [item.body, item.when].filter(Boolean).join(" ");
        notice.append(title, " ", body);
        return notice;
      })
    );
  }

  const chart = document.getElementById("t-site-chart");
  if (chart) {
    chart.addEventListener("error", () => chart.closest("a").setAttribute("hidden", ""));
    // The chart changes every few hours, and a browser would otherwise hold on to it.
    setInterval(function () {
      if (!document.hidden) chart.src = chart.src.replace(/&t=\d+/, "") + "&t=" + Date.now();
    }, 900000);
  }
  pollSite();
  setInterval(pollSite, 60000);
  setInterval(pollStack, 15000);
  setInterval(pollPier, 30000);
  setInterval(showFreshness, 5000);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) {
      pollStack();
      pollPier();
      pollSite();
      htmx.trigger("#t-now", "tonight-refresh");
    }
  });
})();
