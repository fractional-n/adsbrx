'use strict';

(() => {
  const POLL_MS = 1000;
  const STATS_MS = 3000;
  const STALE_S = 15;
  const NM_M = 1852;
  const MAX_TRAIL = 600;
  const DEFAULT_VIEW = { lat: 36, lon: 138, zoom: 5 };

  const $ = (id) => document.getElementById(id);
  const i18n = window.adsbI18n;
  const t = i18n.t;

  const UNITS = {
    aviation: {
      alt: ['ft', 1, 0], speed: ['kt', 1, 0], dist: ['NM', 1, 1], vrate: ['ft/min', 1, 0],
      rings: [50, 100, 150, 200], ringMeters: NM_M,
    },
    metric: {
      alt: ['m', 0.3048, 0], speed: ['km/h', 1.852, 0], dist: ['km', 1.852, 1], vrate: ['m/s', 0.00508, 1],
      rings: [100, 200, 300, 400], ringMeters: 1000,
    },
  };

  const PLANE_SVG =
    '<svg viewBox="0 0 32 32" aria-hidden="true"><path d="M16 1.5c1.2 0 2.1 1.5 2.1 3.3V11l11.4 7.3v3.1' +
    'l-11.4-3.3v6.1l3.1 2.4v2.6L16 28.5 10.8 31.2v-2.6l3.1-2.4v-6.1L2.5 21.4v-3.1L13.9 11V4.8c0-1.8.9-3.3 2.1-3.3z" ' +
    'fill="currentColor" stroke="#000" stroke-width="1.1" stroke-linejoin="round"/></svg>';

  // ---------------------------------------------------------------- settings

  const defaults = {
    units: 'aviation', trails: 'selected', tiles: 'osm', labels: true, onlyPos: false, sortKey: 'dist', sortDir: 1,
    levels: true, lang: 'ja',
  };

  function readSettings() {
    try {
      return Object.assign({}, defaults, JSON.parse(localStorage.getItem('adsbrx.settings')) || {});
    } catch (e) {
      return Object.assign({}, defaults);
    }
  }

  function saveSettings() {
    try {
      localStorage.setItem('adsbrx.settings', JSON.stringify(settings));
    } catch (e) {
      /* storage may be unavailable (private mode) */
    }
  }

  const settings = readSettings();
  if (!UNITS[settings.units]) settings.units = defaults.units;
  if (!i18n.LANGS.includes(settings.lang)) settings.lang = defaults.lang;

  // ---------------------------------------------------------------- helpers

  async function getJSON(url, timeout = 5000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeout);
    try {
      const res = await fetch(url, { cache: 'no-store', signal: controller.signal });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return await res.json();
    } finally {
      clearTimeout(timer);
    }
  }

  const unit = () => UNITS[settings.units];

  function fmt(kind, value, signed) {
    if (value === undefined || value === null) return '';
    const [, factor, digits] = unit()[kind];
    const text = (value * factor).toFixed(digits);
    return signed && value > 0 ? '+' + text : text;
  }

  const isNumber = (v) => typeof v === 'number';
  const vrateOf = (a) => (a.baro_rate !== undefined ? a.baro_rate : a.geom_rate);

  function altText(a) {
    if (a.alt_baro === 'ground') return t('alt.ground');
    return isNumber(a.alt_baro) ? fmt('alt', a.alt_baro) : '';
  }

  function altLabel(a) {
    if (a.alt_baro === 'ground') return t('alt.ground');
    if (!isNumber(a.alt_baro)) return '';
    if (settings.units === 'aviation' && a.alt_baro >= 14000) {
      return 'FL' + String(Math.round(a.alt_baro / 100)).padStart(3, '0');
    }
    return fmt('alt', a.alt_baro) + unit().alt[0];
  }

  function altColor(a) {
    if (a.alt_baro === 'ground') return '#8a99a8';
    if (!isNumber(a.alt_baro)) return '#b0b8c0';
    const f = Math.min(Math.max(a.alt_baro / 40000, 0), 1);
    return 'hsl(' + Math.round(12 + 270 * f) + ', 90%, 58%)';
  }

  function setText(el, text) {
    if (el.textContent !== text) el.textContent = text;
  }

  // Text for a code sent by the server (emergency state, source type); an unknown code is shown as it is.
  function lookup(group, code) {
    const key = group + '.' + code;
    return i18n.has(key) ? t(key) : code;
  }

  // ---------------------------------------------------------------- map

  const map = L.map('map', { worldCopyJump: true }).setView([DEFAULT_VIEW.lat, DEFAULT_VIEW.lon], DEFAULT_VIEW.zoom);
  const receiverLayer = L.layerGroup().addTo(map);
  let tileLayer = null;
  let receiver = null;
  let fitted = false;

  function applyTiles() {
    if (tileLayer) {
      tileLayer.remove();
      tileLayer = null;
    }
    if (settings.tiles === 'osm') {
      tileLayer = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 18,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      }).addTo(map);
    }
  }

  function textIcon(className, text) {
    const span = document.createElement('span');
    span.textContent = text;
    return L.divIcon({ className, html: span, iconSize: [0, 0] });
  }

  function drawReceiver() {
    receiverLayer.clearLayers();
    if (!receiver) return;
    const dot = document.createElement('div');
    dot.className = 'receiver-dot';
    receiverLayer.addLayer(
      L.marker([receiver.lat, receiver.lon], {
        icon: L.divIcon({ className: 'receiver-icon', html: dot, iconSize: [14, 14], iconAnchor: [7, 7] }),
        interactive: false, keyboard: false,
      }),
    );
    const u = unit();
    for (const r of u.rings) {
      const meters = r * u.ringMeters;
      receiverLayer.addLayer(
        L.circle([receiver.lat, receiver.lon], {
          radius: meters, color: '#4f8a68', weight: 1, fill: false, dashArray: '4 6', interactive: false,
        }),
      );
      const north = receiver.lat + meters / 111320;
      if (north < 85) {
        receiverLayer.addLayer(
          L.marker([north, receiver.lon], {
            icon: textIcon('ring-label', r + ' ' + u.dist[0]), interactive: false, keyboard: false,
          }),
        );
      }
    }
  }

  function fitAll() {
    const points = [];
    for (const rec of records.values()) {
      if (rec.a && rec.a.lat !== undefined) points.push([rec.a.lat, rec.a.lon]);
    }
    if (receiver) points.push([receiver.lat, receiver.lon]);
    if (points.length > 1) {
      map.fitBounds(L.latLngBounds(points), { padding: [40, 40], maxZoom: 10 });
    } else if (points.length === 1) {
      map.setView(points[0], 8);
    } else {
      map.setView([DEFAULT_VIEW.lat, DEFAULT_VIEW.lon], DEFAULT_VIEW.zoom);
    }
  }

  // ---------------------------------------------------------------- aircraft records

  const records = new Map();
  let selected = null;
  let pendingTrails = {};

  function createRecord(hex) {
    const trail = (pendingTrails[hex] || []).map((p) => [p[1], p[2], p[3]]);
    delete pendingTrails[hex];
    return { hex, a: null, trail, trailDirty: true, marker: null, line: null, row: null };
  }

  function ensureMarker(rec) {
    if (rec.marker) return;
    const wrap = document.createElement('div');
    wrap.className = 'plane';
    const rot = document.createElement('div');
    rot.className = 'plane-rot';
    rot.innerHTML = PLANE_SVG; // constant markup only; no data is ever inserted as HTML
    const label = document.createElement('div');
    label.className = 'plane-label';
    const l1 = document.createElement('b');
    const l2 = document.createElement('span');
    label.append(l1, l2);
    wrap.append(rot, label);
    const icon = L.divIcon({ className: 'plane-icon', html: wrap, iconSize: [28, 28], iconAnchor: [14, 14] });
    const marker = L.marker([rec.a.lat, rec.a.lon], { icon, keyboard: false });
    marker.on('click', () => select(rec.hex, false));
    marker.addTo(map);
    Object.assign(rec, { marker, wrap, rot, l1, l2 });
  }

  function updateMarker(rec) {
    const a = rec.a;
    if (a.lat === undefined) {
      if (rec.marker) {
        rec.marker.remove();
        rec.marker = null;
      }
      return;
    }
    ensureMarker(rec);
    rec.marker.setLatLng([a.lat, a.lon]);
    rec.marker.setZIndexOffset((isNumber(a.alt_baro) ? Math.round(a.alt_baro / 10) : 0) + (rec.hex === selected ? 100000 : 0));
    const onGround = a.alt_baro === 'ground';
    rec.rot.style.transform = 'rotate(' + (a.track !== undefined ? a.track : 0) + 'deg)' + (onGround ? ' scale(.7)' : '');
    rec.rot.style.color = altColor(a);
    rec.wrap.classList.toggle('stale', a.seen > STALE_S);
    rec.wrap.classList.toggle('emergency', a.emergency !== 'none');
    rec.wrap.classList.toggle('selected', rec.hex === selected);
    setText(rec.l1, a.flight || a.hex.toUpperCase());
    const speed = a.gs !== undefined ? ' ' + fmt('speed', a.gs) : '';
    setText(rec.l2, altLabel(a) + speed);
  }

  function updateTrail(rec) {
    const a = rec.a;
    if (a.lat === undefined) return;
    const last = rec.trail[rec.trail.length - 1];
    if (last && last[0] === a.lat && last[1] === a.lon) return;
    rec.trail.push([a.lat, a.lon, isNumber(a.alt_baro) ? a.alt_baro : null]);
    if (rec.trail.length > MAX_TRAIL) rec.trail.splice(0, rec.trail.length - MAX_TRAIL);
    rec.trailDirty = true;
  }

  function drawTrail(rec) {
    const show = settings.trails === 'all' || (settings.trails === 'selected' && rec.hex === selected);
    if (!show || rec.trail.length < 2) {
      if (rec.line) {
        rec.line.remove();
        rec.line = null;
        rec.trailDirty = true;
      }
      return;
    }
    const isSelected = rec.hex === selected;
    const style = { color: altColor(rec.a), weight: isSelected ? 3 : 1.5, opacity: isSelected ? 0.95 : 0.55, interactive: false };
    if (!rec.line) {
      rec.line = L.polyline(rec.trail.map((p) => [p[0], p[1]]), style).addTo(map);
    } else {
      if (rec.trailDirty) rec.line.setLatLngs(rec.trail.map((p) => [p[0], p[1]]));
      rec.line.setStyle(style);
    }
    rec.trailDirty = false;
  }

  function removeRecord(rec) {
    if (rec.marker) rec.marker.remove();
    if (rec.line) rec.line.remove();
    records.delete(rec.hex);
    if (selected === rec.hex) selected = null;
  }

  // ---------------------------------------------------------------- table

  const tbody = document.querySelector('#list tbody');
  const COLUMNS = ['hex', 'flight', 'alt', 'gs', 'track', 'vrate', 'dist', 'rssi', 'seen'];
  const SORTERS = {
    hex: (a) => a.hex,
    flight: (a) => a.flight || undefined,
    alt: (a) => (a.alt_baro === 'ground' ? -1 : isNumber(a.alt_baro) ? a.alt_baro : undefined),
    gs: (a) => a.gs,
    track: (a) => a.track,
    vrate: vrateOf,
    dist: (a) => a.r_dst,
    rssi: (a) => a.rssi,
    seen: (a) => a.seen,
  };
  let shownRows = [];
  let maxRangeNm = 0;

  function createRow(rec) {
    const tr = document.createElement('tr');
    tr.dataset.hex = rec.hex;
    tr.tabIndex = 0;
    rec.cells = COLUMNS.map((key) => {
      const td = document.createElement('td');
      td.className = key === 'hex' || key === 'flight' ? key : 'num';
      tr.append(td);
      return td;
    });
    rec.row = tr;
  }

  function matchesFilter(a, needle) {
    if (settings.onlyPos && a.lat === undefined) return false;
    if (!needle) return true;
    return a.hex.toUpperCase().includes(needle) ||
      (a.flight || '').toUpperCase().includes(needle) ||
      (a.squawk || '').includes(needle);
  }

  function compare(x, y) {
    const sorter = SORTERS[settings.sortKey] || SORTERS.dist;
    const a = sorter(x);
    const b = sorter(y);
    if (a === undefined && b === undefined) {
      return x.messages !== y.messages ? y.messages - x.messages : x.hex < y.hex ? -1 : 1;
    }
    if (a === undefined) return 1;
    if (b === undefined) return -1;
    if (a === b) return x.hex < y.hex ? -1 : 1;
    return (a < b ? -1 : 1) * settings.sortDir;
  }

  function renderTable() {
    const needle = $('filter').value.trim().toUpperCase();
    const list = [];
    for (const rec of records.values()) {
      if (matchesFilter(rec.a, needle)) list.push(rec);
    }
    list.sort((r1, r2) => compare(r1.a, r2.a));

    const rows = [];
    for (const rec of list) {
      if (!rec.row) createRow(rec);
      const a = rec.a;
      const values = [
        a.hex.toUpperCase(), a.flight || '', altText(a), fmt('speed', a.gs),
        a.track !== undefined ? Math.round(a.track) + '°' : '', fmt('vrate', vrateOf(a), true),
        fmt('dist', a.r_dst), a.rssi !== undefined ? a.rssi.toFixed(1) : '', a.seen.toFixed(0),
      ];
      values.forEach((text, i) => setText(rec.cells[i], text));
      rec.row.classList.toggle('selected', rec.hex === selected);
      rec.row.classList.toggle('stale', a.seen > STALE_S);
      rec.row.classList.toggle('emergency', a.emergency !== 'none');
      rows.push(rec.row);
    }
    if (rows.length !== shownRows.length || rows.some((row, i) => row !== shownRows[i])) {
      tbody.replaceChildren(...rows);
      shownRows = rows;
    }
    $('empty').hidden = rows.length > 0;
    $('empty').textContent = t(records.size === 0 ? 'empty.none' : 'empty.nomatch');
  }

  function renderSortHeaders() {
    for (const th of document.querySelectorAll('#list thead th')) {
      th.classList.toggle('sorted-asc', th.dataset.key === settings.sortKey && settings.sortDir === 1);
      th.classList.toggle('sorted-desc', th.dataset.key === settings.sortKey && settings.sortDir === -1);
    }
  }

  function renderUnitLabels() {
    for (const el of document.querySelectorAll('[data-unit]')) {
      el.textContent = unit()[el.dataset.unit][0];
    }
  }

  // ---------------------------------------------------------------- detail panel

  const DETAIL_FIELDS = [
    'flight', 'hex', 'alt', 'geo', 'speed', 'track', 'vrate', 'squawk',
    'position', 'range', 'category', 'source', 'rssi', 'messages', 'seen', 'emergency',
  ];
  const detailCells = {};

  function buildDetail() {
    const dl = $('d-list');
    for (const key of DETAIL_FIELDS) {
      const dt = document.createElement('dt');
      dt.dataset.i18n = 'detail.' + key; // the label text is filled in by renderLanguage()
      const dd = document.createElement('dd');
      dl.append(dt, dd);
      detailCells[key] = dd;
    }
  }

  function renderDetail() {
    const rec = selected && records.get(selected);
    $('detail').hidden = !rec;
    if (!rec) return;
    const a = rec.a;
    const set = (key, text) => setText(detailCells[key], text);
    setText($('d-title'), (a.flight || a.hex.toUpperCase()) + (a.flight ? '  ' + a.hex.toUpperCase() : ''));
    set('flight', a.flight || '-');
    set('hex', a.hex.toUpperCase());
    set('alt', a.alt_baro === 'ground' ? t('detail.ground') : isNumber(a.alt_baro) ? fmt('alt', a.alt_baro) + ' ' + unit().alt[0] : '-');
    set('geo', isNumber(a.alt_geom) ? fmt('alt', a.alt_geom) + ' ' + unit().alt[0] : '-');
    set('speed', a.gs !== undefined ? fmt('speed', a.gs) + ' ' + unit().speed[0] : '-');
    set('track', a.track !== undefined ? a.track.toFixed(0) + '°' : '-');
    set('vrate', vrateOf(a) !== undefined ? fmt('vrate', vrateOf(a), true) + ' ' + unit().vrate[0] : '-');
    set('squawk', a.squawk || '-');
    set('position', a.lat !== undefined ? a.lat.toFixed(5) + ', ' + a.lon.toFixed(5) : '-');
    set('range', a.r_dst !== undefined ? fmt('dist', a.r_dst) + ' ' + unit().dist[0] + ' / ' + a.r_dir.toFixed(0) + '°' : '-');
    set('category', a.category ? a.category + (i18n.has('category.' + a.category) ? ' ' + t('category.' + a.category) : '') : '-');
    set('source', lookup('source', a.type));
    set('rssi', a.rssi !== undefined ? a.rssi.toFixed(1) + ' dBFS' : '-');
    set('messages', String(a.messages));
    set('seen', t('detail.secondsAgo', { n: a.seen.toFixed(1) }));
    set('emergency', a.emergency !== 'none' ? lookup('emergency', a.emergency) : t('detail.none'));
  }

  function select(hex, pan) {
    selected = hex;
    for (const rec of records.values()) {
      if (rec.a) {
        updateMarker(rec);
        drawTrail(rec);
      }
    }
    const rec = hex && records.get(hex);
    if (rec && pan && rec.a.lat !== undefined) map.panTo([rec.a.lat, rec.a.lon]);
    renderTable();
    renderDetail();
  }

  // ---------------------------------------------------------------- polling

  let titleCount = null; // aircraft count shown in the page title; null until the first poll
  let linkOk = null; // result of the last aircraft poll; null until it has run
  let polled = false;

  function renderAlert(list) {
    const banner = $('alert');
    const emergencies = list.filter((a) => a.emergency !== 'none');
    banner.hidden = emergencies.length === 0;
    if (emergencies.length) {
      const names = emergencies
        .map((a) => (a.flight || a.hex.toUpperCase()) + ' ' + lookup('emergency', a.emergency))
        .join(' / ');
      banner.textContent = t('alert.text', { list: names });
    }
  }

  function applyAircraft(doc) {
    polled = true;
    const present = new Set();
    for (const a of doc.aircraft) {
      present.add(a.hex);
      let rec = records.get(a.hex);
      if (!rec) {
        rec = createRecord(a.hex);
        records.set(a.hex, rec);
      }
      rec.a = a;
      updateTrail(rec);
      updateMarker(rec);
      drawTrail(rec);
    }
    for (const rec of Array.from(records.values())) {
      if (!present.has(rec.hex)) removeRecord(rec);
    }

    const located = doc.aircraft.filter((a) => a.lat !== undefined).length;
    setText($('st-aircraft'), String(doc.aircraft.length));
    setText($('st-located'), String(located));
    titleCount = doc.aircraft.length;
    renderTitle();
    if (!fitted && !receiver && located > 0) {
      fitted = true;
      fitAll();
    }
    renderAlert(doc.aircraft);
    renderTable();
    renderDetail();
  }

  function setLink(ok) {
    linkOk = ok;
    $('link').classList.toggle('off', !ok);
    $('link').title = t(ok ? 'link.ok' : 'link.error');
  }

  async function pollAircraft() {
    try {
      applyAircraft(await getJSON('/data/aircraft.json'));
      setLink(true);
    } catch (e) {
      setLink(false);
    } finally {
      setTimeout(pollAircraft, POLL_MS);
    }
  }

  async function pollStats() {
    try {
      const stats = await getJSON('/data/stats.json');
      setText($('st-rate'), stats.message_rate !== undefined ? stats.message_rate.toFixed(0) : '-');
      maxRangeNm = stats.max_range_nm || 0;
      renderRange();
    } catch (e) {
      /* the aircraft poll reports connectivity */
    } finally {
      setTimeout(pollStats, STATS_MS);
    }
  }

  function renderRange() {
    setText($('st-range'), maxRangeNm ? fmt('dist', maxRangeNm) + ' ' + unit().dist[0] : '-');
  }

  // ---------------------------------------------------------------- receive level and noise

  const LEVEL_MS = 1000;
  const LEVEL_STALE_MS = 4000;
  const CHART_SPAN_S = 300;
  const METER_MIN_DB = -60; // the left end of the level bars; the right end is 0 dBFS
  const CHART_PAD = { left: 30, right: 8, top: 8, bottom: 16 };
  const SERIES = [ // columns of a history row: [t, noise, signal, peak, frames]
    { index: 3, color: '#6fd3a0', dots: true },
    { index: 2, color: '#4da3ff', line: true, dots: true },
    { index: 1, color: '#e0a030', line: true },
  ];
  let levelDoc = {};
  let levelHistory = [];
  let levelNow = null;
  let levelUpdated = performance.now();

  const dbText = (v) => (isNumber(v) ? v.toFixed(1) : '-');
  const barPercent = (v) => Math.min(100, Math.max(0, ((v - METER_MIN_DB) / -METER_MIN_DB) * 100));

  function setBar(id, v) {
    $(id).style.width = isNumber(v) ? barPercent(v).toFixed(1) + '%' : '0%';
  }

  function chartRange(rows) {
    let lo = Infinity;
    let hi = -Infinity;
    for (const row of rows) {
      for (const s of SERIES) {
        const v = row[s.index];
        if (isNumber(v)) {
          lo = Math.min(lo, v);
          hi = Math.max(hi, v);
        }
      }
    }
    if (lo > hi) return { lo: METER_MIN_DB, hi: 0, step: 10 };
    const step = hi - lo + 2 <= 30 ? 5 : 10;
    lo = Math.floor((lo - 1) / step) * step;
    hi = Math.ceil((hi + 1) / step) * step;
    if (hi - lo < 20) hi = lo + 20;
    return { lo, hi, step };
  }

  function drawLevelChart() {
    const canvas = $('lv-chart');
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (!w || !h) return; // the panel is collapsed
    const ratio = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(w * ratio) || canvas.height !== Math.round(h * ratio)) {
      canvas.width = Math.round(w * ratio);
      canvas.height = Math.round(h * ratio);
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const end = levelNow === null ? 0 : levelNow;
    const start = end - CHART_SPAN_S;
    const rows = levelHistory.filter((row) => row[0] >= start);
    const { lo, hi, step } = chartRange(rows);
    const left = CHART_PAD.left;
    const top = CHART_PAD.top;
    const plotW = w - left - CHART_PAD.right;
    const plotH = h - top - CHART_PAD.bottom;
    const x = (t) => left + ((t - start) / CHART_SPAN_S) * plotW;
    const y = (v) => top + ((hi - v) / (hi - lo)) * plotH;

    ctx.font = '10px "Segoe UI", "Yu Gothic UI", sans-serif';
    ctx.fillStyle = '#8a99a8';
    ctx.strokeStyle = '#2a3541';
    ctx.lineWidth = 1;
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    ctx.beginPath();
    for (let v = lo; v <= hi; v += step) {
      const py = Math.round(y(v)) + 0.5;
      ctx.moveTo(left, py);
      ctx.lineTo(left + plotW, py);
      ctx.fillText(String(v), left - 5, py);
    }
    ctx.textBaseline = 'bottom';
    for (let s = 60; s <= CHART_SPAN_S; s += 60) {
      const px = Math.round(x(end - s)) + 0.5;
      ctx.textAlign = s === CHART_SPAN_S ? 'left' : 'center';
      ctx.moveTo(px, top);
      ctx.lineTo(px, top + plotH);
      ctx.fillText(t('chart.minutes', { n: s / 60 }), px, h - 1);
    }
    ctx.stroke();
    ctx.textAlign = 'right';
    ctx.fillText(t('chart.now'), left + plotW + CHART_PAD.right - 1, h - 1);

    ctx.save();
    ctx.beginPath();
    ctx.rect(left, top, plotW, plotH);
    ctx.clip();
    ctx.lineWidth = 1.6;
    ctx.lineJoin = 'round';
    for (const s of SERIES) {
      ctx.strokeStyle = s.color;
      ctx.fillStyle = s.color;
      if (s.line) {
        ctx.beginPath();
        let prev = null;
        for (const row of rows) {
          const v = row[s.index];
          if (!isNumber(v)) {
            prev = null;
          } else {
            if (prev !== null && row[0] - prev <= 2) ctx.lineTo(x(row[0]), y(v));
            else ctx.moveTo(x(row[0]), y(v));
            prev = row[0];
          }
        }
        ctx.stroke();
      }
      if (s.dots) {
        for (const row of rows) {
          const v = row[s.index];
          if (isNumber(v)) {
            ctx.beginPath();
            ctx.arc(x(row[0]), y(v), 1.7, 0, 2 * Math.PI);
            ctx.fill();
          }
        }
      }
    }
    ctx.restore();
  }

  function renderLevels() {
    const doc = levelDoc;
    setText($('st-signal'), dbText(doc.signal));
    setText($('st-noise'), dbText(doc.noise));
    setText($('lv-signal'), isNumber(doc.signal) ? dbText(doc.signal) + ' dBFS' : '-');
    setText($('lv-noise'), isNumber(doc.noise) ? dbText(doc.noise) + ' dBFS' : '-');
    setBar('lv-signal-fill', doc.signal);
    setBar('lv-noise-fill', doc.noise);
    const mark = $('lv-peak-mark');
    mark.hidden = !isNumber(doc.peak);
    if (!mark.hidden) mark.style.left = barPercent(doc.peak).toFixed(1) + '%';
    setText($('lv-snr'), dbText(doc.snr));
    setText($('lv-peak'), dbText(doc.peak));
    setText($('lv-count'), String(doc.messages || 0));
    setText($('lv-window'), String(doc.window || 10));
    $('levels').classList.toggle('stale', performance.now() - levelUpdated > LEVEL_STALE_MS);
    drawLevelChart();
  }

  function applyLevels(doc) {
    if (isNumber(doc.now)) {
      if (levelNow !== null && doc.now < levelNow) levelHistory = []; // the receiver was restarted
      if (doc.now !== levelNow) levelUpdated = performance.now();
      levelNow = doc.now;
    }
    for (const row of doc.history || []) levelHistory.push(row);
    const oldest = (levelNow === null ? 0 : levelNow) - CHART_SPAN_S;
    while (levelHistory.length && levelHistory[0][0] < oldest) levelHistory.shift();
    levelDoc = doc;
  }

  async function pollLevels() {
    try {
      const last = levelHistory.length ? levelHistory[levelHistory.length - 1][0] : null;
      applyLevels(await getJSON('/data/levels.json' + (last === null ? '' : '?since=' + last)));
    } catch (e) {
      /* the aircraft poll reports connectivity */
    } finally {
      renderLevels();
      setTimeout(pollLevels, LEVEL_MS);
    }
  }

  // ---------------------------------------------------------------- UI wiring

  function renderTitle() {
    document.title = t('app.title') + (titleCount === null ? '' : ' (' + titleCount + ')');
  }

  function refreshAll() {
    for (const rec of records.values()) {
      updateMarker(rec);
      drawTrail(rec);
    }
    renderUnitLabels();
    renderTable();
    renderDetail();
  }

  // Applies settings.lang to the static text and to everything that script puts on the page.
  function renderLanguage() {
    i18n.setLang(settings.lang);
    i18n.apply(document);
    renderTitle();
    if (linkOk !== null) setLink(linkOk);
    drawLevelChart();
    if (!polled) return; // nothing to redraw yet; avoids flashing the "no aircraft" message at startup
    refreshAll();
    renderAlert(Array.from(records.values(), (rec) => rec.a));
  }

  function bindControls() {
    const units = $('opt-units');
    const trails = $('opt-trails');
    const tiles = $('opt-tiles');
    const labels = $('opt-labels');
    const lang = $('opt-lang');
    units.value = settings.units;
    trails.value = settings.trails;
    tiles.value = settings.tiles;
    labels.checked = settings.labels;
    lang.value = settings.lang;
    $('only-pos').checked = settings.onlyPos;
    $('map').classList.toggle('labels-off', !settings.labels);

    units.addEventListener('change', () => {
      settings.units = units.value;
      saveSettings();
      drawReceiver();
      renderRange();
      refreshAll();
    });
    trails.addEventListener('change', () => {
      settings.trails = trails.value;
      saveSettings();
      refreshAll();
    });
    tiles.addEventListener('change', () => {
      settings.tiles = tiles.value;
      saveSettings();
      applyTiles();
    });
    labels.addEventListener('change', () => {
      settings.labels = labels.checked;
      saveSettings();
      $('map').classList.toggle('labels-off', !settings.labels);
    });
    lang.addEventListener('change', () => {
      settings.lang = lang.value;
      saveSettings();
      renderLanguage();
    });
    $('only-pos').addEventListener('change', () => {
      settings.onlyPos = $('only-pos').checked;
      saveSettings();
      renderTable();
    });
    $('filter').addEventListener('input', renderTable);
    $('btn-fit').addEventListener('click', fitAll);
    $('d-close').addEventListener('click', () => select(null, false));

    const levels = $('levels');
    levels.open = settings.levels;
    levels.addEventListener('toggle', () => {
      settings.levels = levels.open;
      saveSettings();
      drawLevelChart();
    });
    window.addEventListener('resize', drawLevelChart);

    tbody.addEventListener('click', (ev) => {
      const tr = ev.target.closest('tr');
      if (tr) select(tr.dataset.hex, true);
    });
    tbody.addEventListener('keydown', (ev) => {
      const tr = ev.target.closest('tr');
      if (tr && (ev.key === 'Enter' || ev.key === ' ')) {
        ev.preventDefault();
        select(tr.dataset.hex, true);
      }
    });
    document.querySelector('#list thead').addEventListener('click', (ev) => {
      const th = ev.target.closest('th');
      if (!th || !th.dataset.key) return;
      if (settings.sortKey === th.dataset.key) {
        settings.sortDir = -settings.sortDir;
      } else {
        settings.sortKey = th.dataset.key;
        settings.sortDir = 1;
      }
      saveSettings();
      renderSortHeaders();
      renderTable();
    });
    map.on('click', () => select(null, false));
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && selected) select(null, false);
    });
  }

  async function start() {
    buildDetail();
    bindControls();
    renderLanguage();
    renderUnitLabels();
    renderSortHeaders();
    applyTiles();
    try {
      const info = await getJSON('/data/receiver.json');
      if (info.lat !== undefined && info.lon !== undefined) {
        receiver = { lat: info.lat, lon: info.lon };
        map.setView([receiver.lat, receiver.lon], 8);
        fitted = true;
        drawReceiver();
      }
    } catch (e) {
      /* the map still works without a receiver position */
    }
    try {
      pendingTrails = (await getJSON('/data/trails.json')).trails || {};
    } catch (e) {
      pendingTrails = {};
    }
    pollAircraft();
    pollStats();
    pollLevels();
  }

  start();
})();
