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
    const ratio = parseFloat(getComputedStyle(page).getPropertyValue(front === "stack" ? "--t-stack-ratio" : "--t-pier-ratio"));
    const other = front === "stack" ? hasPier : hasStack;
    // Beside it only if there is another image, and this one is tall enough to leave room.
    images.dataset.layout = other && ratio < 0.8 ? "side" : "below";
    images.style.setProperty("--t-front-ratio", String(ratio));
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

  function stackText(meta) {
    const parts = [meta.target || "Unknown target"];
    if (meta.stacked) parts.push(meta.stacked + " frames");
    if (meta.snr !== null && meta.snr !== undefined) parts.push("SNR " + meta.snr);
    parts.push(meta.captured_clock);
    const text = parts.join(" · ");
    return page.dataset.dim === "1" ? "Last image: " + text : text;
  }

  function pierText(meta) {
    if (!meta.online) return "Sentinel is not responding. Image from " + meta.captured_clock;
    if (meta.stale) return "Image is out of date, from " + meta.captured_clock;
    return "Updated " + meta.captured_clock;
  }

  let stackMeta = null;

  function applyStack(meta) {
    stackMeta = meta;
    stackCaption.textContent = stackText(meta);
  }

  function applyPier(meta) {
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
    }
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
  setInterval(pollStack, 15000);
  setInterval(pollPier, 30000);
  setInterval(showFreshness, 5000);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) {
      pollStack();
      pollPier();
      htmx.trigger("#t-now", "tonight-refresh");
    }
  });
})();
