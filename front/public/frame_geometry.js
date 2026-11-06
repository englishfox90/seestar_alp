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
