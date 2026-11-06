// Live preview of a Framed Mosaic (scale + rotation) over an Aladin sky view.
// Base FOV is the telephoto camera of the model picked in the planner's
// Seestar dropdown (see seestar_models.js); this is a visual planning aid
// only — it does not compute device commands.
(function () {
  // Read per use: the model can change while the page is open.
  const baseFovXDeg = () => window.seestarFov().fov_x / 60.0;
  const baseFovYDeg = () => window.seestarFov().fov_y / 60.0;

  let aladin = null;
  let overlay = null;
  let aladinDiv = null;
  let currentRaDeg = 10.68;
  let currentDecDeg = 41.27;
  let isDragging = false;
  let dragOffset = [0, 0];

  function rectangleCorners(raDeg, decDeg, scale, angleDeg) {
    return window.FrameGeometry.corners(raDeg, decDeg, baseFovXDeg(), baseFovYDeg(), scale, angleDeg);
  }

  // Zoom so the whole frame shows. It can be far larger than a default view.
  function fitFrame() {
    if (!aladin) return;
    const rect = aladinDiv.getBoundingClientRect();
    aladin.setFoV(
      window.FrameGeometry.fitFov(
        baseFovXDeg(),
        baseFovYDeg(),
        parseFloat(document.getElementById("framed_mosaic_scale").value),
        parseFloat(document.getElementById("framed_mosaic_angle").value),
        rect.width,
        rect.height
      )
    );
  }

  // Inverse of rectangleCorners' rotation: is (raDeg, decDeg) inside the
  // current frame's (possibly rotated) rectangle?
  function isInsideFrame(raDeg, decDeg) {
    const scale = parseFloat(document.getElementById("framed_mosaic_scale").value);
    const angle = parseFloat(document.getElementById("framed_mosaic_angle").value);
    const cosDec = Math.cos((currentDecDeg * Math.PI) / 180.0) || 1e-6;
    const dRa = (raDeg - currentRaDeg) * cosDec;
    const dDec = decDeg - currentDecDeg;
    const angleRad = (angle * Math.PI) / 180.0;
    const cosA = Math.cos(angleRad);
    const sinA = Math.sin(angleRad);
    const localX = dRa * cosA + dDec * sinA;
    const localY = -dRa * sinA + dDec * cosA;
    const halfW = (baseFovXDeg() * scale) / 2.0;
    const halfH = (baseFovYDeg() * scale) / 2.0;
    return Math.abs(localX) <= halfW && Math.abs(localY) <= halfH;
  }

  // Mouse event -> [ra, dec] in degrees at that screen position, using the
  // aladin container's own bounding box rather than event.offsetX/offsetY
  // (which is relative to whatever inner canvas layer received the event).
  function eventToRaDec(evt) {
    const rect = aladinDiv.getBoundingClientRect();
    return aladin.pix2world(evt.clientX - rect.left, evt.clientY - rect.top);
  }

  window.update_framed_mosaic = function update_framed_mosaic() {
    const scale = parseFloat(document.getElementById("framed_mosaic_scale").value);
    const angle = parseFloat(document.getElementById("framed_mosaic_angle").value);
    document.getElementById("framed_mosaic_scale_display").textContent = scale.toFixed(1);
    document.getElementById("framed_mosaic_angle_display").textContent = angle;

    // Keep the hidden form inputs (actually submitted to the schedule form)
    // in sync with the sliders unconditionally, even if the Aladin preview
    // itself failed to load or hasn't initialized yet.
    document.getElementById("fm_mosaicScale").value = scale.toFixed(1);
    document.getElementById("fm_mosaicAngle").value = angle;

    if (!aladin) return;

    overlay.removeAll();
    const corners = rectangleCorners(currentRaDeg, currentDecDeg, scale, angle);
    overlay.add(A.polygon(corners, { color: "#f59e0b", lineWidth: 2 }));
  };

  function setSearchStatus(text) {
    const status = document.getElementById("framed_mosaic_search_status");
    if (status) status.textContent = text;
  }

  function showAt(raDeg, decDeg) {
    currentRaDeg = raDeg;
    currentDecDeg = decDeg;
    aladin.gotoRaDec(raDeg, decDeg);
    update_framed_mosaic();
    fitFrame();
    setSearchStatus("");
  }

  // Decimal degrees "313.1 30.7", or sexagesimal "20 52 24 +30 42 00".
  function parseCoordinates(text) {
    const parts = text.trim().split(/[\s,:]+/).map(Number);
    if (parts.some(Number.isNaN)) return null;
    if (parts.length === 2) return parts;
    if (parts.length === 6) {
      const sign = text.trim().split(/[\s,:]+/)[3].startsWith("-") ? -1 : 1;
      return [
        (parts[0] + parts[1] / 60 + parts[2] / 3600) * 15,
        sign * (Math.abs(parts[3]) + parts[4] / 60 + parts[5] / 3600),
      ];
    }
    return null;
  }

  // Names go through ALP's own lookup, because the browser can't reach Aladin's
  // name resolver while the CDS hosts are down.
  function findTarget(text) {
    const coordinates = parseCoordinates(text);
    if (coordinates) {
      showAt(coordinates[0], coordinates[1]);
      return;
    }
    setSearchStatus("Looking up...");
    const root = window.location.pathname.replace(/(\/\d+)?\/planning.*$/, "");
    fetch(`${root}/simbad?name=${encodeURIComponent(text)}`)
      .then(function (reply) {
        if (!reply.ok) throw new Error(String(reply.status));
        return reply.text();
      })
      .then(function (found) {
        // "20h52m24.0s +30d42m0.0s on": the last word is ALP's LP filter advice.
        const position = found.trim().split(/\s+/).slice(0, 2).join(" ");
        const parsed = parseCoordinates(position.replace(/[hmsd]/g, " "));
        if (!parsed) throw new Error("unreadable coordinates");
        showAt(parsed[0], parsed[1]);
      })
      .catch(function () {
        setSearchStatus(`Could not find "${text}". You can enter coordinates instead, such as 20 52 24 +30 42 00.`);
      });
  }

  window.init_framed_mosaic_aladin = function init_framed_mosaic_aladin() {
    aladin = A.aladin("#framed-mosaic-aladin-div", {
      survey: window.ALADIN_SURVEY,
      fov: 2,
      target: `${currentRaDeg} ${currentDecDeg}`,
    });
    aladinDiv = document.getElementById("framed-mosaic-aladin-div");
    overlay = A.graphicOverlay({ color: "#f59e0b", lineWidth: 2 });
    aladin.addOverlay(overlay);
    update_framed_mosaic();
    fitFrame();
    setUpFrameDrag();
  };

  // Click-and-drag the rectangle itself to move its center, independent of
  // panning the sky view. Registered in the capture phase so we can
  // stopPropagation before Aladin's own view-pan handler (bound directly on
  // its canvas, in the bubble phase) ever sees the mousedown -- otherwise
  // both a frame-drag and a view-pan would start from the same click.
  function setUpFrameDrag() {
    aladinDiv.addEventListener(
      "mousedown",
      function (evt) {
        // Aladin's own buttons sit over the sky, often inside the frame.
        if (evt.target.closest("button, select, input, a, .aladin-box")) return;
        const [ra, dec] = eventToRaDec(evt);
        if (!isInsideFrame(ra, dec)) return;
        isDragging = true;
        // Keep the frame where it is under the cursor rather than jumping to it.
        dragOffset = [currentRaDeg - ra, currentDecDeg - dec];
        evt.stopPropagation();
        evt.preventDefault();
      },
      true
    );

    aladinDiv.addEventListener("mousemove", function (evt) {
      if (isDragging) return;
      const [ra, dec] = eventToRaDec(evt);
      aladinDiv.style.cursor = isInsideFrame(ra, dec) ? "move" : "";
    });

    document.addEventListener("mousemove", function (evt) {
      if (!isDragging) return;
      const [ra, dec] = eventToRaDec(evt);
      currentRaDeg = ra + dragOffset[0];
      currentDecDeg = dec + dragOffset[1];
      update_framed_mosaic();
    });

    document.addEventListener("mouseup", function () {
      isDragging = false;
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    window.aladinReady.then(init_framed_mosaic_aladin).catch(function (e) {
      setSearchStatus("The sky view could not load.");
      console.log(e);
    });

    ["framed_mosaic_scale", "framed_mosaic_angle"].forEach(function (id) {
      document.getElementById(id).addEventListener("input", fitFrame);
    });

    // Redraw the frame when the planner's Seestar model changes.
    const modelSelect = document.getElementById("seestar");
    if (modelSelect) {
      modelSelect.addEventListener("change", update_framed_mosaic);
    }

    const searchBtn = document.getElementById("framed_mosaic_search_button");
    if (searchBtn) {
      searchBtn.addEventListener("click", function () {
        if (!aladin) return;
        // Aladin Lite v3's gotoObject takes an options object with
        // success/error callbacks, not a bare callback function -- passing
        // a bare function silently registers nothing, so the view pans but
        // our overlay never redraws at the new position.
        findTarget(document.getElementById("framed_mosaic_search_text").value);
      });
    }

    const openBtn = document.getElementById("open_framed_mosaic_schedule_modal_btn");
    const modal = document.getElementById("framed_mosaic_schedule_modal");
    const closeBtn = document.getElementById("close_framed_mosaic_schedule_modal_btn");
    if (openBtn && modal) {
      openBtn.addEventListener("click", function () {
        document.getElementById("fm_targetName").value =
          document.getElementById("framed_mosaic_search_text").value;
        if (aladin) {
          // Submit the frame's own center (currentRaDeg/currentDecDeg), not
          // the view's center (aladin.getRaDec()) -- once the frame can be
          // dragged independently of the view, those two can differ, and
          // it's the frame's position the user actually wants scheduled.
          // Aladin Lite returns RA in degrees, but the schedule form (and the
          // device layer's Util.parse_coordinate for numeric RA) expects RA
          // in hours. Dec is already in degrees, which is what's expected.
          document.getElementById("fm_ra").value = (currentRaDeg / 15).toFixed(6);
          // dec is already in degrees (no unit conversion needed), but fix
          // its precision too: raw JS floats very close to 0 stringify in
          // exponential notation (e.g. "1e-7"), which fails the server's
          // check_dec_value() regexes and would wrongly reject the target.
          document.getElementById("fm_dec").value = currentDecDeg.toFixed(6);
        }
        modal.showModal();
      });
    }
    if (closeBtn && modal) {
      closeBtn.addEventListener("click", function () {
        modal.close();
      });
    }
  });
})();
