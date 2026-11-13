// Telephoto field of view (arcmin) per Seestar model, shared by the planning
// cards. fov_x is the short sensor axis and fov_y the long one.
// S50 Pro: 3840x2160 px at 2.9 um on a 260 mm lens (ZWO lists the 2.8 deg
// diagonal). S30 Pro: same sensor on a 160 mm lens (ZWO lists 4.6 deg).
// S30: ZWO publishes its spec table only as an image. All four agree with
// AstroMosaic's Seestar table (https://ruuth.xyz/AstroMosaic.html).
// wide_fov_x/y (arcmin, same axes) is the wide angle camera. ZWO publishes only a
// single 63 deg figure, so it is listed only where it has been measured: S30 Pro
// from the plate solve of a wide stack (2160x3840 px at 53"/px, a 6 mm lens on
// 1.6 um pixels; 31.1 x 52.7 deg, 59 deg diagonal).
window.SEESTAR_MODELS = {
  "S50": { fov_x: 43.8, fov_y: 77.4 },
  "S50 Pro": { fov_x: 82.8, fov_y: 147.2 },
  "S30": { fov_x: 73.2, fov_y: 130.2 },
  "S30 Pro": { fov_x: 134.6, fov_y: 239.2, wide_fov_x: 1866, wide_fov_y: 3159 },
};

// FOV of the model currently selected in the planner's Seestar dropdown.
window.seestarFov = function seestarFov() {
  const select = document.getElementById("seestar");
  return window.SEESTAR_MODELS[select ? select.value : "S50"] || window.SEESTAR_MODELS["S50"];
};
