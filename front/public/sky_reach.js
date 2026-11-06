// What can be imaged from the site on a night: pure sums on what /targets/sky.json gives.
// No page, so a test can run it next to the planner's own answer.
(function (root) {
  const RAD = Math.PI / 180;
  // Until the server says otherwise, as a page can be newer than a running server.
  const DEFAULT_MIN_HOURS = 1;
  const DEFAULT_STEP_MINUTES = 15;

  function stepMinutes(win) {
    return win.step_minutes || DEFAULT_STEP_MINUTES;
  }

  // How many steps in a row make the minimum usable time. 5 steps span 4 gaps: an hour.
  function stepsNeeded(win) {
    const hours = win.min_hours === undefined ? DEFAULT_MIN_HOURS : win.min_hours;
    return Math.ceil((hours * 60) / stepMinutes(win)) + 1;
  }

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

  // The longest unbroken time above the limit. first and last are indexes into win.steps, or
  // null if it's never high enough. usable: it lasts the minimum time, so worth planning on.
  function reach(win, ra, dec) {
    let first = null;
    let last = null;
    let runStart = null;
    let peak = -90;
    win.steps.forEach(function (step, index) {
      const place = altAz(win.lat, ra, dec, step.lst);
      if (place.alt > peak) peak = place.alt;
      if (place.alt >= floorAt(win, place.az)) {
        if (runStart === null) runStart = index;
        if (first === null || index - runStart > last - first) {
          first = runStart;
          last = index;
        }
      } else {
        runStart = null;
      }
    });
    const minutes = first === null ? 0 : (last - first) * stepMinutes(win);
    return {
      first: first,
      last: last,
      peak: peak,
      minutes: minutes,
      usable: first !== null && last - first + 1 >= stepsNeeded(win),
      never: ceiling(win.lat, dec) < win.floor,
    };
  }

  // How far above its limit a point stays for the minimum usable time, in degrees: the best
  // hour's lowest point, so it's smooth across the sky. Negative is out of reach.
  function margin(win, ra, dec) {
    const heights = win.steps.map(function (step) {
      const place = altAz(win.lat, ra, dec, step.lst);
      return place.alt - floorAt(win, place.az);
    });
    const needed = stepsNeeded(win);
    let best = -Infinity;
    for (let start = 0; start + needed <= heights.length; start++) {
      let lowest = Infinity;
      for (let i = start; i < start + needed; i++) lowest = Math.min(lowest, heights[i]);
      best = Math.max(best, lowest);
    }
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
