// Where a framed mosaic's rectangle sits on the sky, shared by the sky views.
// Width and height are the camera's field of view in degrees, before scaling.
window.FrameGeometry = {
  corners: function (raDeg, decDeg, widthDeg, heightDeg, scale, angleDeg) {
    const halfW = (widthDeg * scale) / 2.0;
    const halfH = (heightDeg * scale) / 2.0;
    const angleRad = (angleDeg * Math.PI) / 180.0;
    const cosA = Math.cos(angleRad);
    const sinA = Math.sin(angleRad);
    const cosDec = Math.cos((decDeg * Math.PI) / 180.0) || 1e-6;
    return [
      [-halfW, -halfH],
      [halfW, -halfH],
      [halfW, halfH],
      [-halfW, halfH],
    ].map(function ([x, y]) {
      const rotatedX = x * cosA - y * sinA;
      const rotatedY = x * sinA + y * cosA;
      return [raDeg + rotatedX / cosDec, decDeg + rotatedY];
    });
  },

  // Panel centres of a traditional mosaic, [raDeg, decDeg, "21"], stepped exactly as
  // ALP's mosaic_thread_fn steps them: the field's width in RA (wider by 1/cos(dec) on
  // each row) and its height in Dec, less the overlap, centred for even counts.
  panelCentres: function (raDeg, decDeg, nRa, nDec, overlapPercent, widthDeg, heightDeg) {
    const keep = (100 - overlapPercent) / 100;
    const raStep = function (dec) {
      return Math.abs(dec) > 85 ? 15 : (widthDeg * keep) / Math.cos((dec * Math.PI) / 180);
    };
    const decStep = heightDeg * keep;
    let centreRa = raDeg;
    let centreDec = decDeg;
    if (nRa % 2 === 0) centreRa += raStep(decDeg) / 2;
    if (nDec % 2 === 0) centreDec += decStep / 2;
    const out = [];
    let dec = centreDec - Math.floor(nDec / 2) * decStep;
    for (let d = 0; d < nDec; d++) {
      const step = raStep(dec);
      let ra = centreRa - Math.floor(nRa / 2) * step;
      for (let r = 0; r < nRa; r++) {
        out.push([ra, dec, `${r + 1}${d + 1}`]);
        ra += step;
      }
      dec += decStep;
    }
    return out;
  },

  // The view width, in degrees, that shows the whole rectangle with some margin.
  // Aladin's field of view is measured across the width of the view.
  fitFov: function (widthDeg, heightDeg, scale, angleDeg, viewWidthPx, viewHeightPx) {
    const angleRad = (angleDeg * Math.PI) / 180.0;
    const w = widthDeg * scale;
    const h = heightDeg * scale;
    const boxW = Math.abs(w * Math.cos(angleRad)) + Math.abs(h * Math.sin(angleRad));
    const boxH = Math.abs(w * Math.sin(angleRad)) + Math.abs(h * Math.cos(angleRad));
    const aspect = viewWidthPx / Math.max(viewHeightPx, 1);
    return Math.min(180, Math.max(boxW, boxH * aspect) * 1.25);
  },
};
