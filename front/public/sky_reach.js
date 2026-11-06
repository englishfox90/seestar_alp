// What can be imaged from the site on a night: pure sums on what /targets/sky.json gives.
// No page, so a test can run it next to the planner's own answer.
(function (root) {
  const RAD = Math.PI / 180;

  function altAz(lat, ra, dec, lst) {
    const h = (lst - ra) * RAD;
    const sinLat = Math.sin(lat * RAD);
    const cosLat = Math.cos(lat * RAD);
    const sinDec = Math.sin(dec * RAD);
    const cosDec = Math.cos(dec * RAD);
    const sinAlt = sinLat * sinDec + cosLat * cosDec * Math.cos(h);
    const alt = Math.asin(Math.max(-1, Math.min(1, sinAlt)));
    const az = Math.atan2(-cosDec * Math.sin(h), sinDec * cosLat - cosDec * sinLat * Math.cos(h));
    return { alt: alt / RAD, az: ((az / RAD) % 360 + 360) % 360 };
  }

  function floorAt(win, az) {
    if (!win.horizon) return win.floor;
    const n = win.horizon.length;
    const place = (az / 360) * n;
    const low = Math.floor(place) % n;
    const high = (low + 1) % n;
    const part = place - Math.floor(place);
    return Math.max(win.floor, win.horizon[low] * (1 - part) + win.horizon[high] * part);
  }

  // The most this declination can ever reach from the site, whatever the date.
  function ceiling(lat, dec) {
    return 90 - Math.abs(lat - dec);
  }

  // first and last are indexes into win.steps, or null if it's never high enough.
  function reach(win, ra, dec) {
    let first = null;
    let last = null;
    let peak = -90;
    win.steps.forEach(function (step, index) {
      const place = altAz(win.lat, ra, dec, step.lst);
      if (place.alt > peak) peak = place.alt;
      if (place.alt >= floorAt(win, place.az)) {
        if (first === null) first = index;
        last = index;
      }
    });
    return { first: first, last: last, peak: peak, never: ceiling(win.lat, dec) < win.floor };
  }

  // Runs of out-of-reach sky, in bands of size degrees: [{dec, raStart, raEnd}].
  function outOfReach(win, size) {
    const bands = [];
    for (let dec = -90; dec < 90; dec += size) {
      let start = null;
      for (let ra = 0; ra <= 360; ra += size) {
        const out = ra < 360 && reach(win, ra + size / 2, dec + size / 2).first === null;
        if (out && start === null) start = ra;
        if (!out && start !== null) {
          bands.push({ dec: dec, raStart: start, raEnd: ra });
          start = null;
        }
      }
    }
    return bands;
  }

  const api = { altAz: altAz, ceiling: ceiling, reach: reach, outOfReach: outOfReach };
  root.SkyReach = api;
  if (typeof module !== "undefined") module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
