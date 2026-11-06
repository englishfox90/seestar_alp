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

  // How far above its limit a point gets in the dark, in degrees. Negative is out of reach.
  function margin(win, ra, dec) {
    let best = -Infinity;
    win.steps.forEach(function (step) {
      const place = altAz(win.lat, ra, dec, step.lst);
      best = Math.max(best, place.alt - floorAt(win, place.az));
    });
    return best;
  }

  function joinSegments(segments) {
    const key = (point) => `${point[0].toFixed(3)},${point[1].toFixed(3)}`;
    const atPoint = new Map();
    segments.forEach(function (segment, index) {
      segment.forEach(function (point) {
        const name = key(point);
        if (!atPoint.has(name)) atPoint.set(name, []);
        atPoint.get(name).push(index);
      });
    });
    const used = new Set();
    const lines = [];
    function extend(line) {
      for (;;) {
        const tip = line[line.length - 1];
        const next = (atPoint.get(key(tip)) || []).find((index) => !used.has(index));
        if (next === undefined) return;
        used.add(next);
        const [a, b] = segments[next];
        line.push(key(a) === key(tip) ? b : a);
      }
    }
    segments.forEach(function (segment, index) {
      if (used.has(index)) return;
      used.add(index);
      const line = [segment[0], segment[1]];
      extend(line);
      line.reverse();
      extend(line);
      lines.push(line);
    });
    return lines;
  }

  // The out-of-reach sky as smooth shapes: where the margin crosses zero is worked out along
  // each side of a grid cell, so the edge follows the real limit and not the grid.
  // Returns {cells: [polygon of [ra, dec]], lines: [polyline of [ra, dec]]}.
  function outOfReach(win, size) {
    const columns = Math.round(360 / size);
    const rows = Math.round(180 / size);
    const heights = [];
    for (let row = 0; row <= rows; row++) {
      const line = [];
      for (let column = 0; column < columns; column++) {
        line.push(margin(win, column * size, -90 + row * size));
      }
      line.push(line[0]); // right round the sky
      heights.push(line);
    }
    const cells = [];
    const segments = [];
    for (let row = 0; row < rows; row++) {
      for (let column = 0; column < columns; column++) {
        const ra = column * size;
        const dec = -90 + row * size;
        const corners = [
          [ra, dec, heights[row][column]],
          [ra + size, dec, heights[row][column + 1]],
          [ra + size, dec + size, heights[row + 1][column + 1]],
          [ra, dec + size, heights[row + 1][column]],
        ];
        const polygon = [];
        const crossings = [];
        corners.forEach(function (corner, index) {
          const next = corners[(index + 1) % 4];
          if (corner[2] < 0) polygon.push([corner[0], corner[1]]);
          if (corner[2] < 0 !== next[2] < 0) {
            const part = corner[2] / (corner[2] - next[2]);
            const crossing = [
              corner[0] + (next[0] - corner[0]) * part,
              corner[1] + (next[1] - corner[1]) * part,
            ];
            polygon.push(crossing);
            crossings.push(crossing);
          }
        });
        if (polygon.length >= 3) cells.push(polygon);
        for (let i = 0; i + 1 < crossings.length; i += 2) {
          segments.push([crossings[i], crossings[i + 1]]);
        }
      }
    }
    return { cells: cells, lines: joinSegments(segments) };
  }

  const api = { altAz: altAz, ceiling: ceiling, reach: reach, outOfReach: outOfReach };
  root.SkyReach = api;
  if (typeof module !== "undefined") module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);
