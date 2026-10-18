"use strict";
// DX Mapper front end: Leaflet map + station list + details panel.

const PTY = ["No Program Type", "News", "Information", "Sports", "Talk", "Rock",
  "Classic Rock", "Adult Hits", "Soft Rock", "Top 40", "Country", "Oldies",
  "Soft", "Nostalgia", "Jazz", "Classical", "Rhythm and Blues",
  "Soft Rhythm and Blues", "Foreign Language", "Religious Music",
  "Religious Talk", "Personality", "Public", "College", "(unused)", "(unused)",
  "(unused)", "(unused)", "(unused)", "Weather", "Emergency Test", "Emergency"];

const BANDS = [
  [50, "#3ccf6a", "< 50 mi"], [150, "#e5d33a", "50–150"],
  [400, "#f39a2b", "150–400"], [800, "#ec4d3c", "400–800"],
  [Infinity, "#c45cf0", "800+ mi"]];
const NOGEO = "#7b8893";

const TILE_PORTS = (window.DX_TILE_PORTS || []).map(String);
const TILE_OPTS = TILE_PORTS.length > 1 ? {subdomains: TILE_PORTS} : {};
function tileUrl(layer) {
  return TILE_PORTS.length > 1
    ? `${location.protocol}//${location.hostname}:{s}/tiles/${layer}/{z}/{x}/{y}`
    : `/tiles/${layer}/{z}/{x}/{y}`;
}

// A tile that failed to download (busy map server) is asked for again a few
// times instead of leaving a hole in the map.
function retrying(layer) {
  layer.on("tileerror", (e) => {
    const img = e.tile;
    const n = (+img.dataset.tries || 0) + 1;
    if (n > 8) return;
    img.dataset.tries = n;
    const src = e.tile.src.split("?")[0];
    setTimeout(() => { img.src = `${src}?retry=${n}`; }, Math.min(30000, 1000 * 2 ** n));
  });
  return layer;
}

const LAYERS = {
  normal: () => retrying(L.tileLayer(tileUrl("normal"), {...TILE_OPTS,
    maxZoom: 19, attribution: "&copy; OpenStreetMap contributors"})),
  // Topo map with hillshading laid over it, so hills and valleys look raised.
  terrain: () => L.layerGroup([
    retrying(L.tileLayer(tileUrl("terrain"), {...TILE_OPTS,
      maxZoom: 19, attribution: "Topo &amp; hillshade &copy; Esri, USGS, NASA, CGIAR and contributors"})),
    retrying(L.tileLayer(tileUrl("hillshade"), {...TILE_OPTS,
      maxZoom: 19, maxNativeZoom: 16, opacity: 0.55, className: "relief-tiles"}))]),
  satellite: () => L.layerGroup([
    retrying(L.tileLayer(tileUrl("satellite"), {...TILE_OPTS,
      maxZoom: 19, attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics"})),
    retrying(L.tileLayer(tileUrl("labels"), {...TILE_OPTS,
      maxZoom: 19}))]),
};
const LAYER_NAMES = {normal: "Normal", terrain: "Terrain", satellite: "Satellite"};

const S = {
  stations: [], byId: {}, markers: {}, rev: -1, home: null, selected: null,
  pathLine: null, homeMarker: null, rings: null, outerRings: null, firstLoad: true,
  layer: localStorage.getItem("dxmap.layer") || null,
  view: localStorage.getItem("dxmap.view") || "current",
  reach: localStorage.getItem("dxmap.reach") || "all", localMi: 40, extMi: 80, homeSource: null,
  viewKey: null, startedAt: null, busy: false, again: false, lastTick: 0,
  logs: [], logSort: {key: "start_ts", dir: -1}, days: [],
};

const $ = (id) => document.getElementById(id);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const fmtTime = (t) => t ? new Date(t * 1000).toLocaleString() : "";
const fmtFreq = (f) => f ? Number(f).toFixed(1) : "—";
const colour = (st) => {
  if (st.distance_mi == null) return NOGEO;
  return BANDS.find((b) => st.distance_mi < b[0])[1];
};

// Signal as the FM Tuner shows it (View > Signal units): S-units or dBFS.
function fmtSig(dbfs, unit = true) {
  if (dbfs == null || dbfs <= -999) return "";
  if ((S.settings || {}).signal_units === "db") return dbfs.toFixed(1) + (unit ? " dBFS" : "");
  const s = (dbfs + 70) / 6 + 1;
  if (s < 1) return "S0";
  return s < 9.5 ? `S${Math.round(s)}` : `S9+${Math.round((s - 9) * 6)}`;
}

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}
const post = (path, body) => api(path, {method: "POST",
  headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});

function toast(msg, ms = 3500) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}

// ---------------------------------------------------------------- map ----
const map = L.map("map", {zoomControl: true, worldCopyJump: true});
const baseLayers = {};
for (const key of Object.keys(LAYERS)) baseLayers[LAYER_NAMES[key]] = LAYERS[key]();
L.control.layers(baseLayers, {}, {position: "topright", collapsed: false}).addTo(map);
L.control.scale({imperial: true, metric: true}).addTo(map);
const markerLayer = L.layerGroup().addTo(map);

map.on("baselayerchange", (e) => {
  const key = Object.keys(LAYER_NAMES).find((k) => LAYER_NAMES[k] === e.name);
  if (!key) return;
  S.layer = key;
  localStorage.setItem("dxmap.layer", key);
  post("/api/settings", {map_layer: key}).catch(() => {});
});

function setBaseLayer(key) {
  key = LAYERS[key] ? key : "normal";
  for (const [name, layer] of Object.entries(baseLayers)) {
    if (name === LAYER_NAMES[key]) layer.addTo(map); else map.removeLayer(layer);
  }
  S.layer = key;
}

function drawHome() {
  const h = [S.home.lat, S.home.lon];
  // House at Home, car when the RDS Monitor is using its Away location.
  const away = !!(S.home.label && S.home.label !== "Home");
  const tip = away ? S.home.label : "Home";
  const icon = L.divIcon({className: "", iconSize: [26, 26], iconAnchor: [13, 13],
    html: `<div style="font-size:22px;line-height:26px;text-align:center;filter:drop-shadow(0 0 2px #000)">${away ? "🚗" : "🏠"}</div>`});
  if (S.homeMarker) {
    S.homeMarker.setLatLng(h);
    S.homeMarker.setIcon(icon);
    S.homeMarker.setTooltipContent(tip);
  } else {
    S.homeMarker = L.marker(h, {zIndexOffset: 1000, icon})
      .addTo(map).bindTooltip(tip, {direction: "top"});
  }
  for (const g of [S.rings, S.outerRings]) if (g) map.removeLayer(g);
  S.rings = L.layerGroup();
  S.outerRings = L.layerGroup();
  // Local and Extended local edges (Home… settings), then 100–400 mi: solid
  // every 100, dashed at the 50s (own checkbox).
  const tiers = [[S.localMi, "Local"], [S.extMi, "Ext"]]
    .filter(([mi], i) => mi > 0 && (i !== 1 || mi > S.localMi));
  const dx = [100, 150, 200, 250, 300, 350, 400].map((mi) => [mi])
    .filter(([mi]) => !tiers.some(([t]) => Math.abs(t - mi) < 3));
  for (const [mi, name] of [...tiers, ...dx]) {
    const tierRing = !!name;
    const group = tierRing ? S.rings : S.outerRings;
    L.circle(h, {radius: mi * 1609.344, color: "#000", weight: tierRing ? 5 : 4, opacity: 0.45,
      fill: false, interactive: false}).addTo(group);
    L.circle(h, {radius: mi * 1609.344, color: tierRing ? "#7fe0ff" : "#ffffff", weight: 2,
      opacity: 0.95, fill: false, dashArray: tierRing || mi % 100 === 0 ? null : "6 6", interactive: false}).addTo(group);
    const lab = L.latLng(h[0] + mi / 69.05, h[1]);
    L.marker(lab, {interactive: false, icon: L.divIcon({className: "", iconSize: null,
      html: `<div class="ring-label">${name ? name + " " : ""}${Math.round(mi)} mi</div>`})}).addTo(group);
  }
  // Hide my location: one Local-size circle, shifted a little off-center
  // (fixed random direction) so its middle isn't your house.
  if (S.fuzz) map.removeLayer(S.fuzz);
  const off = fuzzOffset(), r = Math.max(S.localMi || 40, 10), shift = r * off.f;
  const c = [h[0] + shift * Math.cos(off.b) / 69.05,
    h[1] + shift * Math.sin(off.b) / (69.05 * Math.cos(h[0] * Math.PI / 180))];
  S.fuzz = L.layerGroup([
    L.circle(c, {radius: r * 1609.344, color: "#000", weight: 5, opacity: 0.45, fill: false, interactive: false}),
    L.circle(c, {radius: r * 1609.344, color: "#7fe0ff", weight: 2, opacity: 0.95,
      fillColor: "#7fe0ff", fillOpacity: 0.08, interactive: false}),
  ]);
  showRings();
}
function fuzzOffset() {
  try {
    const o = JSON.parse(localStorage.getItem("dxmap.fuzz"));
    if (o && o.b != null && o.f > 0 && o.f <= 0.5) return o;
  } catch (e) { /* new one below */ }
  const o = {b: Math.random() * 2 * Math.PI, f: 0.25 + Math.random() * 0.25};
  localStorage.setItem("dxmap.fuzz", JSON.stringify(o));
  return o;
}
function hidingLoc() {
  const box = document.getElementById("hide-loc");
  return !!(box && box.checked);
}
// Each ring checkbox is remembered.
const RING_BOXES = {rings: "dxmap.rings", outerRings: "dxmap.outerRings"};
function showRings() {
  const hide = hidingLoc();
  for (const [id, key] of Object.entries(RING_BOXES)) {
    const group = S[id];
    if (!group) continue;
    if ($(id).checked && !hide) group.addTo(map); else map.removeLayer(group);
  }
  if (S.homeMarker) { if (hide) map.removeLayer(S.homeMarker); else S.homeMarker.addTo(map); }
  if (S.fuzz) { if (hide) S.fuzz.addTo(map); else map.removeLayer(S.fuzz); }
}
for (const [id, key] of Object.entries(RING_BOXES)) {
  if (localStorage.getItem(key) === "0") $(id).checked = false;
  $(id).onchange = () => {
    localStorage.setItem(key, $(id).checked ? "1" : "0");
    showRings();
  };
}

// Hide my location (remembered) and the Screenshot button.
async function privacyChanged() {
  showRings();
  for (const st of S.stations || []) upsertMarker(st);
  renderList();
  if (S.view === "all") renderLogs();
  S.nowKey = null;
  const jobs = [refresh()];
  if (S.selected != null && !$("details").classList.contains("hidden")) jobs.push(select(S.selected, false));
  else drawPath(S.byId[S.selected]);
  await Promise.all(jobs);
}
if (localStorage.getItem("dxmap.hideLoc") === "1") $("hide-loc").checked = true;
$("hide-loc").onchange = () => {
  localStorage.setItem("dxmap.hideLoc", $("hide-loc").checked ? "1" : "0");
  privacyChanged();
};
$("btn-shot").onclick = async () => {
  const box = $("hide-loc"), was = box.checked, btn = $("btn-shot");
  btn.disabled = true;
  try {
    if (!was) { box.checked = true; await privacyChanged(); }
    await new Promise((r) => setTimeout(r, 800));
    // cacheBust: map tiles the browser cached before tiles allowed copying.
    const png = await htmlToImage.toPng(document.body, {pixelRatio: 1, cacheBust: true,
      imagePlaceholder: "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==",
      filter: (el) => el.id !== "toast"});
    const res = await post("/api/screenshot", {png});
    toast(`Saved "${res.file}" in settings\\Screenshots`, 6000);
  } catch (e) {
    toast("Screenshot failed: " + (e.message || e), 6000);
  } finally {
    if (!was) { box.checked = false; await privacyChanged(); }
    btn.disabled = false;
  }
};

function legend() {
  const c = L.control({position: "bottomleft"});
  c.onAdd = () => {
    const d = L.DomUtil.create("div", "legend");
    d.innerHTML = BANDS.map((b) =>
      `<span><i class="dot" style="background:${b[1]}"></i>${b[2]}</span>`).join("") +
      `<span><i class="dot" style="background:${NOGEO}"></i>no location</span>`;
    return d;
  };
  c.addTo(map);
}

// --------------------------------------------------------------- views ---
const pad2 = (n) => String(n).padStart(2, "0");
const dateStr = (offsetDays = 0) => {
  const d = new Date();
  d.setDate(d.getDate() + offsetDays);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
};
const dayStart = (str) => {
  const [y, m, d] = str.split("-").map(Number);
  return Math.floor(new Date(y, m - 1, d).getTime() / 1000);
};
const fmtDay = (str) => new Date(dayStart(str) * 1000).toLocaleDateString(undefined,
  {weekday: "short", year: "numeric", month: "short", day: "numeric"});

function viewQuery() {
  if (S.view === "current") {
    const v = $("cur-span").value;
    const since = v === "session" ? (S.startedAt || 0) : Date.now() / 1000 - Number(v) * 3600;
    const label = v === "session" ? "Current: logged since DX Mapper opened"
      : `Current: logged in the last ${v} hour${v === "1" ? "" : "s"}`;
    return {key: "current:" + v, q: `?live=1&since=${Math.floor(since)}`, label};
  }
  if (S.view === "history") {
    const r = $("hist-range").value;
    let since = null, until = null, label = "History: all time";
    if (r === "today") { since = dayStart(dateStr(0)); label = "History: today"; }
    else if (r === "yesterday") { since = dayStart(dateStr(-1)); until = dayStart(dateStr(0)); label = "History: yesterday"; }
    else if (r === "7" || r === "30") { since = dayStart(dateStr(1 - Number(r))); label = `History: last ${r} days`; }
    else if (r === "year") { since = Math.floor(new Date(new Date().getFullYear(), 0, 1).getTime() / 1000); label = "History: this year"; }
    else if (r === "custom") {
      const f = $("hist-from").value, t = $("hist-to").value;
      if (f) since = dayStart(f);
      if (t) until = dayStart(t) + 86400;
      label = f && f === t ? `History: ${fmtDay(f)}`
        : `History: ${f ? fmtDay(f) : "start"} – ${t ? fmtDay(t) : "now"}`;
    }
    const q = [since != null && `since=${since}`, until != null && `until=${until}`].filter(Boolean).join("&");
    return {key: `history:${since}:${until}`, q: q ? "?" + q : "", label};
  }
  return {key: "all", q: "", label: "All Log: everything logged"};
}

function setView(v) {
  S.view = ["current", "history", "all"].includes(v) ? v : "current";
  localStorage.setItem("dxmap.view", S.view);
  document.querySelectorAll("#views button").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === S.view));
  $("opt-current").classList.toggle("hidden", S.view !== "current");
  $("opt-history").classList.toggle("hidden", S.view !== "history");
  $("alllog").classList.toggle("hidden", S.view !== "all");
  setTimeout(() => map.invalidateSize(), 60);
  refresh(true);
}
document.querySelectorAll("#views button").forEach((b) => b.onclick = () => setView(b.dataset.view));

function restoreViewOptions() {
  for (const id of ["cur-span", "hist-range", "hist-from", "hist-to"]) {
    const saved = localStorage.getItem("dxmap." + id);
    if (saved !== null) $(id).value = saved;
  }
  if (!$("cur-span").value) $("cur-span").value = "3";
  $("hist-dates").classList.toggle("hidden", $("hist-range").value !== "custom");
}
for (const id of ["cur-span", "hist-range", "hist-from", "hist-to"]) {
  $(id).onchange = () => {
    localStorage.setItem("dxmap." + id, $(id).value);
    if (id === "hist-range" && $(id).value === "custom" && !$("hist-from").value) {
      $("hist-from").value = $("hist-to").value = dateStr(0);
    }
    $("hist-dates").classList.toggle("hidden", $("hist-range").value !== "custom");
    refresh(true);
  };
}

async function loadDays() {
  try { S.days = (await api("/api/days")).days; } catch (e) { return; }
  const cur = $("hist-range").value === "custom" && $("hist-from").value === $("hist-to").value
    ? $("hist-from").value : null;
  $("days").innerHTML = S.days.length ? S.days.map((d) =>
    `<li data-day="${d.day}" class="${d.day === cur ? "sel" : ""}"><span>${esc(fmtDay(d.day))}</span>
      <span class="muted">${d.stations} stn · ${d.logs} log${d.logs === 1 ? "" : "s"}</span></li>`).join("")
    : '<li class="muted">Nothing logged yet</li>';
}
$("days").onclick = (e) => {
  const li = e.target.closest("li[data-day]");
  if (!li) return;
  $("hist-range").value = "custom";
  $("hist-from").value = $("hist-to").value = li.dataset.day;
  for (const id of ["hist-range", "hist-from", "hist-to"]) localStorage.setItem("dxmap." + id, $(id).value);
  $("hist-dates").classList.remove("hidden");
  refresh(true);
};

// ------------------------------------------------------------- all log ---
const LOG_COLS = [
  ["start_ts", "Logged", (lg) => fmtTime(lg.start_ts)],
  ["callsign", "Call", (lg) => lg.callsign || ""],
  ["pi", "PI", (lg) => lg.pi || ""],
  ["freq_mhz", "MHz", (lg) => lg.freq_mhz ? fmtFreq(lg.freq_mhz) : ""],
  ["ps", "PS", (lg) => lg.ps || ""],
  ["rt", "RadioText", (lg) => lg.rt || ""],
  ["signal_dbfs", "Signal", (lg) => fmtSig(lg.signal_dbfs, false)],
  ["usable_info", "Usable", (lg) => lg.usable_info != null && lg.usable_info >= 0 ? Math.round(lg.usable_info) + "%" : ""],
  ["block_pct", "Blocks", (lg) => lg.block_pct != null && lg.block_pct >= 0 ? lg.block_pct.toFixed(1) + "%" : ""],
  ["distance_mi", "Miles", (lg) => lg.distance_mi != null && !hidingLoc() ? Math.round(lg.distance_mi) : ""],
  ["compass", "Dir", (lg) => hidingLoc() ? "" : lg.compass || ""],
  ["rx_name", "From", (lg) => lg.rx_name || ""],
  ["location", "Location", (lg) => lg.location || ""],
  ["source", "Source", (lg) => (lg.source || "") + (lg.auto ? " (auto)" : "")],
];

async function loadLogs() {
  const data = await api("/api/logs");
  S.logs = data.logs.map((lg) => {
    const st = S.byId[lg.station_id] || {};
    // Distance from where it was logged (Home/Away) when known.
    return Object.assign(lg, lg.rx_distance_mi != null
      ? {distance_mi: lg.rx_distance_mi, compass: lg.rx_compass}
      : {distance_mi: st.distance_mi, compass: st.compass});
  });
  renderLogs();
}

function renderLogs() {
  const q = $("log-search").value.trim().toLowerCase();
  const {key, dir} = S.logSort;
  let rows = S.logs.filter((lg) => inReach(lg) && (!q || LOG_COLS.some((c) =>
    String(c[2](lg)).toLowerCase().includes(q))));
  rows.sort((a, b) => {
    const x = a[key], y = b[key];
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    return (typeof x === "number" ? x - y : String(x).localeCompare(String(y))) * dir;
  });
  const shown = rows.slice(0, 3000);
  $("log-count").textContent = `${rows.length} of ${S.logs.length} log entries` +
    (rows.length > shown.length ? ` (showing first ${shown.length})` : "") + " · click a row to show it on the map";
  $("log-table").innerHTML = `<thead><tr>${LOG_COLS.map((c) =>
    `<th data-key="${c[0]}">${c[1]}${c[0] === key ? (dir > 0 ? " ▲" : " ▼") : ""}</th>`).join("")}</tr></thead>
    <tbody>${shown.map((lg) => `<tr data-sid="${lg.station_id}" class="${lg.station_id === S.selected ? "sel" : ""}">${LOG_COLS.map((c) =>
      `<td title="${esc(c[2](lg))}">${esc(c[2](lg))}</td>`).join("")}</tr>`).join("")}</tbody>`;
}
$("log-table").onclick = (e) => {
  const th = e.target.closest("th[data-key]");
  if (th) {
    const k = th.dataset.key;
    S.logSort = {key: k, dir: S.logSort.key === k ? -S.logSort.dir : (k === "start_ts" ? -1 : 1)};
    return renderLogs();
  }
  const tr = e.target.closest("tr[data-sid]");
  if (tr) select(Number(tr.dataset.sid), true);
};
$("log-search").oninput = renderLogs;

function upsertMarker(st) {
  let m = S.markers[st.id];
  if (st.lat == null || st.lon == null || !inReach(st)) {
    if (m) { markerLayer.removeLayer(m); delete S.markers[st.id]; }
    return;
  }
  const style = {radius: 7, color: "#0d1217", weight: 1.5, fillOpacity: 0.95,
    fillColor: colour(st)};
  const label = `${st.callsign || st.pi || "?"} ${st.freq_mhz ? fmtFreq(st.freq_mhz) : ""}`;
  if (!m) {
    m = L.circleMarker([st.lat, st.lon], style).addTo(markerLayer);
    m.on("click", () => select(st.id, false));
    S.markers[st.id] = m;
  } else {
    m.setLatLng([st.lat, st.lon]);
    m.setStyle(style);
  }
  // Basics only on the marker; everything else is in the details window.
  const permanent = map.getZoom() >= 7;
  const basics = `<b>${esc(label)}</b>` + (st.ps ? ` “${esc(st.ps.trim())}”` : "") +
    (st.location ? `<br>${esc(st.location)}` : "") +
    (st.distance_mi != null && !hidingLoc() ? `${st.location ? " · " : "<br>"}${st.distance_mi} mi ${esc(st.compass || "")}` : "");
  m.unbindTooltip();
  m.bindTooltip(permanent ? esc(label) : basics, {permanent, direction: "right",
    className: "call-label", offset: [6, 0]});
}
map.on("zoomend", () => S.stations.forEach(upsertMarker));

// ---------------------------------------------------------------- list ---
function filtered() {
  const q = $("search").value.trim().toLowerCase();
  const f = $("filter").value;
  let list = S.stations.filter((st) => {
    if (!inReach(st)) return false;
    const src = (st.sources || []).join(" ");
    if (f === "monitor" && !/monitor|map/.test(src)) return false;
    if (f === "import" && !/import/.test(src)) return false;
    if (f === "nogeo" && st.lat != null) return false;
    if (!q) return true;
    return [st.callsign, st.pi, st.ps, st.location, st.state, st.country,
      st.freq_mhz && fmtFreq(st.freq_mhz)]
      .some((v) => v && String(v).toLowerCase().includes(q));
  });
  const s = $("sort").value;
  const byFreq = (a, b) => (a.freq_mhz || 999) - (b.freq_mhz || 999);
  const byCall = (a, b) => String(a.callsign || a.pi || "").localeCompare(
    String(b.callsign || b.pi || ""), undefined, {numeric: true});
  const byLast = (a, b) => (b.last_heard || 0) - (a.last_heard || 0);
  const cmp = {
    last: (a, b) => byLast(a, b) || byFreq(a, b),
    dist: (a, b) => (b.distance_mi ?? -1) - (a.distance_mi ?? -1) || byFreq(a, b),
    freq: (a, b) => byFreq(a, b) || byCall(a, b),
    call: (a, b) => byCall(a, b) || byFreq(a, b),
  }[s] || byLast;
  return list.sort(cmp);
}

function renderList(flashIds = []) {
  const list = filtered();
  $("count").textContent = `${list.length} of ${S.stations.length} stations` +
    (S.stations.length ? ` · ${S.stations.filter((s) => s.lat == null).length} without location` : "") +
    (S.stations.some((s) => s.location_check && !s.location_check.ok)
      ? ` · ${S.stations.filter((s) => s.location_check && !s.location_check.ok).length} to check` : "");
  const row = (st) => {
    const sub = [st.ps && `“${st.ps}”`, st.location,
      !hidingLoc() && st.distance_mi != null && `${Math.round(st.distance_mi)} mi ${st.compass}`]
      .filter(Boolean).join(" · ");
    const badges = (st.lat == null ? '<span class="badge warn">no location</span>' : "") +
      ((st.sources || []).some((s) => s.startsWith("import")) ? '<span class="badge">imported</span>' : "") +
      (st.view_only ? `<span class="badge" title="From your ${esc(st.log)} log - view only">${esc(st.log)}</span>` : "");
    return `<li data-id="${st.id}" class="${st.id === S.selected ? "sel" : ""} ${flashIds.includes(st.id) ? "flash" : ""}">
      <i class="dot" style="background:${colour(st)}"></i>
      <div class="li-main"><div class="li-top"><span class="li-call">${esc(st.callsign || st.pi || "?")}${badges}</span>
      <span class="li-freq">${fmtFreq(st.freq_mhz)}</span></div>
      <div class="li-sub">${esc(sub) || "&nbsp;"}</div></div>
      ${st.stream && st.stream.url ? `<button class="li-tune li-listen" data-id="${st.id}" title="Listen to ${esc(st.callsign || "")}'s live internet stream (${esc(st.stream.source || "")}) in your web browser">Stream</button>` : ""}
      ${st.freq_mhz ? `<button class="li-tune" data-freq="${st.freq_mhz}" title="Tune the FM Tuner to ${fmtFreq(st.freq_mhz)} MHz">Tune</button>` : ""}</li>`;
  };
  if (S.reach !== "all") { $("list").innerHTML = list.map(row).join(""); return; }
  // All: Locals first as a by-frequency preset list, then DX in Sort order.
  const byFreq = (a, b) => (a.freq_mhz || 999) - (b.freq_mhz || 999);
  const mi = Math.round(S.localMi), ext = Math.round(S.extMi);
  const secs = [
    ["local", `Locals (0–${mi} mi)`, list.filter((st) => tier(st) === "local").sort(byFreq)],
    ["ext", `Extended local (${mi + 1}–${ext} mi)`, list.filter((st) => tier(st) === "ext").sort(byFreq)],
    ["dx", `DX (${ext + 1}+ mi)`, list.filter((st) => tier(st) === "dx")],
    ["nogeo", "No location yet", list.filter((st) => st.distance_mi == null)],
  ];
  $("list").innerHTML = secs.filter(([, , sts]) => sts.length).map(([key, title, sts]) => {
    const shut = S.collapsed.includes(key);
    return `<li class="sec" data-sec="${key}"><span>${shut ? "▸" : "▾"} ${esc(title)}</span><span class="muted">${sts.length}</span></li>` +
      (shut ? "" : sts.map(row).join(""));
  }).join("");
}
S.collapsed = JSON.parse(localStorage.getItem("dxmap.collapsed") || "[]");
async function listenTo(btn, id, what = "stream") {
  btn.disabled = true;
  try {
    await post("/api/listen", {id, what});
    toast(what === "website" ? "Opening the station's website…" : "Opening the live stream in your web browser…");
  } catch (e) { toast(e.message); }
  btn.disabled = false;
}
async function tuneTo(btn, freq) {
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = "Tuning…";
  try {
    const r = await post("/api/tune", {freq_mhz: freq});
    toast(`Tuned to ${fmtFreq(r.frequency_mhz)} MHz`);
  } catch (e) { toast(e.message); }
  btn.disabled = false; btn.textContent = label;
}
$("list").onclick = (e) => {
  const listen = e.target.closest(".li-listen");
  if (listen) return listenTo(listen, Number(listen.dataset.id));
  const tune = e.target.closest(".li-tune");
  if (tune) return tuneTo(tune, Number(tune.dataset.freq));
  const sec = e.target.closest("li.sec");
  if (sec) {
    const k = sec.dataset.sec;
    S.collapsed = S.collapsed.includes(k) ? S.collapsed.filter((x) => x !== k) : [...S.collapsed, k];
    localStorage.setItem("dxmap.collapsed", JSON.stringify(S.collapsed));
    return renderList();
  }
  const li = e.target.closest("li");
  if (li) select(Number(li.dataset.id), true);
};
for (const id of ["search", "sort", "filter"]) $(id).oninput = () => renderList();
if (["last", "freq", "call", "dist"].includes(localStorage.getItem("dxmap.sort"))) {
  $("sort").value = localStorage.getItem("dxmap.sort");
}
$("sort").onchange = () => { localStorage.setItem("dxmap.sort", $("sort").value); renderList(); };

// ------------------------------------------------------------- details ---
async function select(id, pan) {
  S.selected = id;
  fetch("/api/map/pick", {method: "POST", headers: {"Content-Type": "application/json"},
                          body: JSON.stringify({id})}).catch(() => {});  // the Log Book follows the map
  renderList();
  if (S.view === "all") renderLogs();
  const st = S.byId[id];
  if (pan && st && st.lat != null) frameFromTuner(st.lat, st.lon);
  drawPath(st);
  $("details").classList.remove("hidden");
  $("details-body").innerHTML = '<p class="muted">Loading…</p>';
  try {
    const full = await api(`/api/station/${id}`);
    if (!st && pan && full.lat != null) {  // not in this view (e.g. imported)
      frameFromTuner(full.lat, full.lon);
      drawPath(full);
    }
    renderDetails(full);
  } catch (e) {
    $("details-body").innerHTML = `<p class="err">${esc(e.message)}</p>`;
  }
}

// Right-hand map padding that keeps markers clear of the details panel.
function detailsPad() {
  const d = $("details"), hidden = d.classList.contains("hidden");
  if (hidden) d.style.visibility = "hidden", d.classList.remove("hidden");
  const m = map.getContainer().getBoundingClientRect(), r = d.getBoundingClientRect();
  if (hidden) d.classList.add("hidden"), d.style.visibility = "";
  const pad = Math.ceil(m.right - r.left) + 30;
  return m.width - pad >= 120 ? pad : 40;  // panel fills a tiny map anyway
}

// Keep the tuner (home or away location) in the middle and zoom so the
// transmitter fits: frame the transmitter and its mirror image across home.
function frameFromTuner(lat, lon, rxLat, rxLon, panel = true) {
  if (!$("autocenter").checked) return;
  if (hidingLoc()) { map.setView([lat, lon], Math.max(map.getZoom(), 7)); return; }
  if (rxLat == null && S.home) { rxLat = S.home.lat; rxLon = S.home.lon; }
  if (rxLat == null) { map.setView([lat, lon], Math.max(map.getZoom(), 7)); return; }
  const mLat = Math.max(-85, Math.min(85, 2 * rxLat - lat));
  const right = panel || !$("details").classList.contains("hidden") ? detailsPad() : 40;
  map.fitBounds([[lat, lon], [mLat, 2 * rxLon - lon]],
    {paddingTopLeft: [40, 40], paddingBottomRight: [right, 40], maxZoom: 11});
}

function drawPath(st) {
  if (S.pathLine) { map.removeLayer(S.pathLine); S.pathLine = null; }
  if (!st || st.lat == null || !S.home || hidingLoc()) return;
  const pts = [[S.home.lat, S.home.lon], [st.lat, st.lon]];
  S.pathLine = L.layerGroup([
    L.polyline(pts, {color: "#000", weight: 6, opacity: 0.5, interactive: false}),
    L.polyline(pts, {color: colour(st), weight: 3, opacity: 1, interactive: false}),
  ]).addTo(map);
}

$("details-close").onclick = () => {
  $("details").classList.add("hidden");
  S.selected = null;
  drawPath(null);
  renderList();
};

const kv = (rows) => '<table class="kv">' + rows.filter((r) => r[1] !== undefined &&
  r[1] !== null && r[1] !== "").map((r) => `<tr><td>${esc(r[0])}</td><td>${r[2] ? r[1] : esc(r[1])}</td></tr>`).join("") + "</table>";

// Scrolling now-playing bar on the open station's card while it's on the air.
function detailsTicker() {
  const box = $("d-now");
  if (!box) return;
  const st = S.byId[S.selected], n = S.now;
  const live = !!(n && st && (n.station_id != null ? n.station_id === st.id
    : n.callsign && st.callsign && n.callsign.toUpperCase() === st.callsign.toUpperCase()));
  const text = !live ? "" : n.now_playing ? `Now playing: ${n.now_playing}` : n.rt ? `RadioText: ${n.rt}` : "";
  box.classList.toggle("hidden", !text);
  const el = box.firstElementChild;
  if (!text || el.dataset.text === text) return;
  el.dataset.text = el.textContent = text;
  el.style.animationDuration = Math.max(10, text.length * 0.2) + "s";
}

function renderDetails(st) {
  if (S.selected !== st.id) return;
  const call = st.callsign || "";
  const D = DXDetails.build(st, {hide: hidingLoc(), fmtSig, manage: true});
  const {book, logs, repLog, fullRep, badgeHtml, analogHtml, hdHtml, locHtml} = D;
  $("details-body").innerHTML = `
    ${call ? `<div id="d-logo-box"><img class="d-logo" src="/api/logo/${encodeURIComponent(call)}?t=${Date.now()}" alt="" onerror="logoFallback(this)"><button id="d-logo-dl" class="logo-dl" title="Look up this station's logo online and save it in the RDS Monitor's logos folder">Download logo</button><button id="d-logo-pick" class="logo-pick" title="See every logo found online, or upload your own, and pick the one to use">Change logo</button></div>` : ""}
    <div><span class="d-call">${esc(call || st.pi || "?")}</span><span class="d-freq">${fmtFreq(st.freq_mhz)} MHz</span>
      ${st.freq_mhz ? '<button class="primary" id="d-tune" title="Tune the FM Tuner to this station (off the air)">Tune</button>' : ""}
      ${/^https?:\/\//.test(book.stream || "") ? `<button id="d-listen" title="Listen to the live internet stream in your web browser${st.stream && st.stream.source ? " (from " + esc(st.stream.source) + ")" : ""}">Stream</button>` : ""}
      ${/^https?:\/\//.test(book.website || "") ? `<button id="d-web" title="${esc(book.website)}">Website</button>` : ""}
      <button id="d-card" title="Open this station's card in the Log Book's Modern view">Open station card</button></div>
    <div id="d-now" class="d-ticker hidden" title="Live song / RadioText while the RDS Monitor decodes this station"><span class="tk-in"></span></div>
    ${st.slogan ? `<div class="d-slogan">${esc(st.slogan)}${st.slogan_source ? ` <span class="muted" title="Wikipedia isn't always current - type your own in Edit to replace it">from ${esc(st.slogan_source)}</span>` : ""}</div>` : ""}
    ${badgeHtml ? `<div class="d-badges"><span class="muted">Decode:</span>${badgeHtml}</div>` : ""}
    <div class="d-where">${esc([st.location || "location unknown",
      st.distance_mi != null && !hidingLoc() ? `${Math.round(st.distance_mi)} mi ${st.compass || ""}`.trim() : ""].filter(Boolean).join(", "))}</div>
    ${D.copyHtml}${D.bookHtml}
    ${fullRep}${analogHtml}${hdHtml}${locHtml}
    ${D.extraHtml}
    ${DXDetails.NB_BOX}
    <div class="d-section"${st.view_only ? ' style="display:none"' : ""}><h3>Edit</h3>
      <div class="edit-row"><input id="d-call" type="text" placeholder="Callsign" value="${esc(call)}">
        <input id="d-freq" type="text" placeholder="MHz" value="${st.freq_mhz ? fmtFreq(st.freq_mhz) : ""}"></div>
      <div class="edit-row"><input id="d-lat" type="text" placeholder="Latitude" value="${st.lat ?? ""}">
        <input id="d-lon" type="text" placeholder="Longitude" value="${st.lon ?? ""}"></div>
      <div class="edit-row"><input id="d-slogan" type="text" placeholder="Slogan / branding (The Wave…)" value="${esc(st.slogan || "")}"></div>
      <div class="edit-row"><input id="d-fedby" type="text" placeholder="Fed by - for a translator (WBUL-HD2…)" title="The station or HD channel this translator relays, when it isn't found automatically" value="${esc(Object.entries(st.extra || {}).find(([k]) => k.toLowerCase() === "fed by")?.[1] || "")}"></div>
      <div class="edit-row"><input id="d-mode" type="text" placeholder="Propagation (Tropo, Es, MS…)" value="${esc(st.mode || "")}"></div>
      <div class="edit-row"><button class="primary" id="d-save">Save</button><button class="danger" id="d-del">Delete station</button></div></div>
    ${D.logsHtml}`;
  detailsTicker();
  document.querySelectorAll("[data-report]").forEach((b) => {
    b.onclick = () => showReport(logs.find((lg) => String(lg.id) === b.dataset.report));
  });
  const fullRepLink = $("d-fullrep");
  if (fullRepLink) fullRepLink.onclick = (e) => { e.preventDefault(); showReport(repLog); };
  // Click a section heading to fold it; folds are remembered.
  const shut = new Set(JSON.parse(localStorage.getItem("dxmap.detailsShut") || "[]"));
  for (const sec of $("details-body").querySelectorAll(".d-section")) {
    const h = sec.querySelector("h3");
    if (!h) continue;
    const key = sec.dataset.fold || h.textContent.replace(/\(.*?\)/g, "").trim().toLowerCase();
    sec.classList.toggle("shut", shut.has(key));
    h.title = "Click to fold / unfold";
    h.onclick = () => {
      if (shut.has(key)) shut.delete(key); else shut.add(key);
      sec.classList.toggle("shut", shut.has(key));
      localStorage.setItem("dxmap.detailsShut", JSON.stringify([...shut]));
    };
  }
  const logoBtn = $("d-logo-dl");
  if (logoBtn) logoBtn.onclick = async () => {
    logoBtn.disabled = true; logoBtn.textContent = "Looking up logo…";
    try {
      await post(`/api/logo/${encodeURIComponent(call)}`, {});
      const box = $("d-logo-box");
      box.querySelector("img").src = `/api/logo/${encodeURIComponent(call)}?t=${Date.now()}`;
      box.classList.remove("nologo");
      toast(`Logo saved for ${call}`);
    } catch (e) { toast(e.message); }
    logoBtn.disabled = false; logoBtn.textContent = "Download logo";
  };
  const pickBtn = $("d-logo-pick");
  if (pickBtn) pickBtn.onclick = () => logoPicker(call);
  const listenBtn = $("d-listen");
  if (listenBtn) listenBtn.onclick = () => listenTo(listenBtn, st.id);
  const webBtn = $("d-web");
  if (webBtn) webBtn.onclick = () => listenTo(webBtn, st.id, "website");
  for (const a of $("details-body").querySelectorAll(".hd-link")) {
    a.onclick = (e) => { e.preventDefault(); select(Number(a.dataset.id), true); };
  }
  fetch(`/api/station/${st.id}/neighbors`).then((x) => x.json()).then((d) => {
    const box = $("d-nb");
    if (S.selected !== st.id || !box) return;
    box.className = ""; box.innerHTML = DXDetails.neighborsHtml(d);
    for (const a of box.querySelectorAll(".hd-link"))
      a.onclick = (e) => { e.preventDefault(); select(Number(a.dataset.id), true); };
  }).catch(() => { if ($("d-nb")) $("d-nb").textContent = "Couldn't look these up."; });
  $("d-card").onclick = async () => {
    if (!window.pywebview) return window.open(`/logbook?card=${st.id}`, "dxlogbook");
    try {  // the app window: an open Log Book shows it, else the browser opens one
      const r = await post("/api/open-card", {id: st.id, browser: true});
      toast(r.seen ? "Opened in the Log Book" : "Log Book opened in your web browser");
    } catch (e) { toast(e.message); }
  };
  const tuneBtn = $("d-tune");
  if (tuneBtn) tuneBtn.onclick = () => tuneTo(tuneBtn, st.freq_mhz);
  const fccBtn = $("d-fcc");
  if (fccBtn) fccBtn.onclick = async () => {
    fccBtn.disabled = true; fccBtn.textContent = "Looking up…";
    try { renderDetails(await post(`/api/station/${st.id}/fcc`, {})); refresh(true); }
    catch (e) { toast(e.message); fccBtn.disabled = false; }
  };
  $("d-save").onclick = async () => {
    const body = {callsign: $("d-call").value,
      freq_mhz: $("d-freq").value, mode: $("d-mode").value,
      slogan: $("d-slogan").value, fed_by: $("d-fedby").value};
    const lat = $("d-lat").value.trim(), lon = $("d-lon").value.trim();
    if (lat && lon && (Number(lat) !== st.lat || Number(lon) !== st.lon)) {
      body.lat = lat; body.lon = lon;  // only a real change counts as set by hand
    }
    if (body.callsign.trim().toUpperCase() !== (st.callsign || "").toUpperCase()) {
      $("d-save").disabled = true; $("d-save").textContent = "Looking up location…";
    }
    try { const r = await post(`/api/station/${st.id}`, body); renderDetails(r);
      const where = {fcc: " - location from FCC", "not found": " - not in FCC, kept the old location",
        offline: " - offline, location not updated"}[r.relocated] || "";
      toast((r.monitor_updated ? "Saved - RDS Monitor updated" : "Saved") + where); refresh(true); }
    catch (e) { toast(e.message); }
  };
  $("d-del").onclick = async () => {
    if (!confirm(`Delete ${call || st.pi} and all its log entries?`)) return;
    await api(`/api/station/${st.id}`, {method: "DELETE"});
    $("details-close").onclick();
    refresh(true);
  };
  $("details-body").querySelectorAll("button[data-log]").forEach((b) => b.onclick = async () => {
    if (!confirm("Delete this log entry?")) return;
    await api(`/api/log/${b.dataset.log}`, {method: "DELETE"});
    select(st.id, false);
    refresh(true);
  });
  $("details-body").querySelectorAll("button[data-copy],button[data-copylog]").forEach((b) => b.onclick = async () => {
    b.disabled = true;
    try {
      const x = await post("/api/logbooks/copy", b.dataset.copy ? {id: Number(b.dataset.copy)} : {log_id: Number(b.dataset.copylog)});
      toast(DXDetails.copiedText(x));
    } catch (e) { b.disabled = false; return toast(e.message); }
    if (!st.view_only) select(st.id, false);
    refresh(true);
  });
  if (!st.view_only && st.fcc === null && call && !renderDetails._retry) {
    renderDetails._retry = setTimeout(() => {
      renderDetails._retry = null;
      if (S.selected === st.id) select(st.id, false);
    }, 2500);
  }
}

// ----------------------------------------------------- Update all panel --
function updBar(key, done, total, state, text) {
  const bar = $("upd-" + key);
  bar.max = Math.max(total || 0, 1);
  bar.value = state === "done" ? bar.max : state === "run" ? Math.min(done || 0, bar.max) : 0;
  $("upd-" + key + "-t").textContent =
    state === "skip" ? "skipped - no RDS Monitor folder next to DX Mapper"
    : state === "wait" ? "waiting…"
    : state === "run" && !total ? "starting…"
    : `${done}/${total}${text ? " · " + text : ""}${state === "done" ? " ✓" : ""}`;
}
function showUpdate(ms) {
  S.lastUpdate = ms;
  const lo = ms.logos || {}, sm = ms.streams || {};
  const key = String(ms.started || ms.finished || "");
  $("btn-monfile").disabled = ms.running;
  $("btn-monfile").textContent = ms.running ? "Updating…" : "Update all";
  const closed = !ms.running && localStorage.getItem("dxmap.updClosed") === key;
  $("upd").classList.toggle("hidden", closed);
  if (closed) return;
  const mon = ms.monitor !== false;
  const detailsDone = !!ms.details_finished || (!ms.running && mon);
  updBar("details", ms.done, ms.total, !mon ? "skip" : detailsDone ? "done" : "run",
         `${ms.written} written to stations.txt`);
  updBar("logos", lo.done, lo.total, !mon ? "skip" : lo.finished ? "done" : detailsDone ? "run" : "wait",
         `${lo.found} found`);
  updBar("streams", sm.done, sm.total, sm.finished || !ms.running ? "done" : "run",
         `${sm.found} streams, ${sm.websites || 0} websites${sm.failed ? `, ${sm.failed} couldn't be checked` : ""}`);
  $("upd-title").textContent = ms.running ? "Update all - working…"
    : `Update all finished ${ms.finished ? new Date(ms.finished * 1000).toLocaleTimeString() : ""}`;
  const parts = [];
  if (!ms.running) {
    if (mon) parts.push(`Details: ${ms.total} checked, ${ms.written} written to stations.txt, ${ms.slogans} with a slogan`);
    if (mon) parts.push(lo.total ? `Logos: ${lo.found} of ${lo.total} missing logos found` : "Logos: none missing");
    parts.push(sm.total ? `Streams: ${sm.found} of ${sm.total} found · Websites: ${sm.websites || 0}` : "Streams: every station already has one");
  }
  $("upd-sum").textContent = parts.join("\n");
  $("upd-sum").style.whiteSpace = "pre-line";
  const errs = (ms.errors || []).slice();
  if (sm.failed) errs.push(`${sm.failed} station${sm.failed === 1 ? "" : "s"} couldn't be checked (no internet or the directories are down) - they're tried again next time. Last error: ${sm.error || "?"}`);
  $("upd-err").textContent = errs.join("\n");
}
$("upd-close").onclick = () => {
  const ms = S.lastUpdate;
  $("upd").classList.add("hidden");
  if (ms) localStorage.setItem("dxmap.updClosed", String(ms.started || ms.finished || ""));
};

// -------------------------------------------------------------- polling --
function stationTitle(n) {
  return `DX Mapper - ${n || 0} station${n === 1 ? "" : "s"}`;
}

async function refresh(force = false) {
  let state;
  try { state = await api("/api/state"); }
  catch (e) {
    $("feed").className = "feed off";
    $("feed").textContent = "DX Mapper program not running";
    return;
  }
  document.title = stationTitle(state.station_count);
  if (state.logs_shown) {
    $("logs-now").textContent = "Log: " + state.logs_shown.join(" + ");
    $("logs-now").title = `New catches go into ${state.log}` + (state.logs_shown.length > 1 ? `; ${state.logs_shown.slice(1).join(", ")} view only` : "");
  }
  document.body.classList.toggle("lite", !!state.lite);
  const ms = state.monitor_sync;
  if (ms) {
    showUpdate(ms);
    if (S.monSync && !ms.running) {
      S.monSync = false;
      refresh(true);
    }
  }
  const f = state.feed;
  const last = f.last_message ? ` · last log ${f.last_station || ""} ${new Date(f.last_message * 1000).toLocaleTimeString()}` : "";
  $("feed").className = "feed " + (f.listening ? "on" : "off");
  $("feed").textContent = f.listening
    ? `Listening on port ${f.port}${f.clients ? " · monitor connected" : ""}${last}`
    : `Feed port ${f.port} unavailable: ${f.error || ""}`;
  const x = state.excel;
  if (x) {
    $("excel").className = "excel" + (x.error ? " err" : "");
    $("excel").textContent = x.error ? `Excel copy: ${x.error}`
      : x.last_saved ? `Excel copy saved ${new Date(x.last_saved * 1000).toLocaleTimeString()} – ${x.path.split(/[\\/]/).pop()}` : "";
    $("excel").title = x.path;
    const bt = backupText(state.backup);
    if (bt) $("excel").textContent += ($("excel").textContent ? " · " : "") + bt;
    if (state.backup && state.backup.error) $("excel").className = "excel err";
  }
  const job = state.import, jobs = [];
  if (job && !job.finished) jobs.push(`Importing ${job.name}: ${job.done} of ${job.rows}…`);
  else if (job && Date.now() / 1000 - job.finished < 600)
    jobs.push(`Imported ${job.name}: ${job.added} new, ${job.updated} merged, ${job.skipped} skipped`);
  if (state.geo_pending) jobs.push(`Looking up locations: ${state.geo_pending} left`);
  $("jobs").textContent = jobs.join(" · ");
  $("jobs").title = job && job.errors.length ? "Skipped: " + job.errors.join("; ") : "";
  $("jobs").classList.toggle("hidden", !jobs.length);
  const homeChanged = !S.home || S.home.lat !== state.home.lat || S.home.lon !== state.home.lon;
  S.home = state.home;
  if (S.firstLoad) {
    setBaseLayer(S.layer || state.settings.map_layer);
    map.setView([S.home.lat, S.home.lon], 6);
  }
  if (homeChanged) drawHome();
  S.startedAt = state.started_at;
  S.homeSource = state.home.source;
  S.locations = state.locations;
  S.backup = state.backup;
  S.settings = state.settings;
  S.now = state.now;
  drawNow(state.now);
  detailsTicker();
  const fo = state.focus;  // a station clicked in the Log Book window
  if (fo && fo.at !== S.focusAt) {
    const first = S.focusAt === undefined;
    S.focusAt = fo.at;
    if (!first) select(fo.id, true);
  } else if (!fo) S.focusAt = null;
  const mi = Number(state.settings.local_radius_mi) || 40;
  const ext = Math.max(mi, Number(state.settings.extended_radius_mi) || 80);
  if (mi !== S.localMi || ext !== S.extMi) {
    S.localMi = mi; S.extMi = ext; applyReach(); force = true;
    if (S.home) drawHome();
  }
  const vq = viewQuery();
  const viewChanged = vq.key !== S.viewKey;
  const tick = S.view === "current" && Date.now() - S.lastTick > 30000;
  if (!force && !homeChanged && !viewChanged && !tick && state.rev === S.rev) return;
  if (S.busy) { S.again = true; return; }
  S.busy = true;
  try {
    const before = S.byId;
    const revChanged = state.rev !== S.rev;
    S.rev = state.rev;
    S.lastTick = Date.now();
    const data = await api("/api/stations" + vq.q);
    S.stations = data.stations;
    S.byId = Object.fromEntries(S.stations.map((s) => [s.id, s]));
    for (const id of Object.keys(S.markers)) {
      if (!S.byId[id]) { markerLayer.removeLayer(S.markers[id]); delete S.markers[id]; }
    }
    S.stations.forEach(upsertMarker);
    $("period").textContent = vq.label;
    const live = state.last_live;
    const liveNew = !S.firstLoad && live && live.at !== S.lastLiveAt;
    S.lastLiveAt = live ? live.at : null;
    const fresh = S.firstLoad || viewChanged || !revChanged ? [] : S.stations.filter((s) =>
      !before[s.id] || (before[s.id].times_logged !== s.times_logged)).map((s) => s.id);
    S.viewKey = vq.key;
    renderList(fresh);
    if (S.view === "all") await loadLogs();
    if (S.view === "history") loadDays();
    if (liveNew && S.byId[live.id] && $("follow").checked) {
      // Only live logs (Log button / RDS Monitor) jump the map, not imports.
      const st = S.byId[live.id];
      toast(`Logged ${st.callsign || st.pi} ${fmtFreq(st.freq_mhz)}`);
      select(st.id, true);
    } else if (S.selected && S.byId[S.selected]) {
      drawPath(S.byId[S.selected]);
    }
    S.firstLoad = false;
  } finally {
    S.busy = false;
    if (S.again) { S.again = false; setTimeout(() => refresh(true), 0); }
  }
}

// --------------------------------------------------------------- dialogs -
// Logo picker: every logo found online (or your own picture); the one you
// click replaces the station's logo and the automatic lookup leaves it be.
async function logoPicker(call) {
  const enc = encodeURIComponent(call);
  openModal(`<h3>Change logo: ${esc(call)}</h3>
    <p class="muted">Click a logo to use it. It replaces the current one, and the automatic lookup won't change it back.</p>
    <div id="lp-grid" class="lp-grid">Looking up logos…</div>
    <p><label class="lp-up">Upload your own picture: <input type="file" id="lp-file" accept="image/*"></label></p>`);
  const done = () => {
    closeModal();
    const box = $("d-logo-box");
    if (box) { box.querySelector("img").src = `/api/logo/${enc}?t=${Date.now()}`; box.classList.remove("nologo"); }
    toast(`Logo saved for ${call}`);
  };
  const pick = async (body) => {
    try { await post(`/api/logo/${enc}/pick`, body); done(); }
    catch (e) { toast(e.message); }
  };
  $("lp-file").onchange = (e) => {
    const f = e.target.files[0];
    if (!f) return;
    const r = new FileReader();
    r.onload = () => pick({data: r.result});
    r.readAsDataURL(f);
  };
  let res;
  try { res = await api(`/api/logo/${enc}/choices`); }
  catch (e) { if ($("lp-grid")) $("lp-grid").textContent = e.message; return; }
  const grid = $("lp-grid");
  if (!grid) return;
  if (!res.choices.length) { grid.style.display = "block"; grid.textContent = "No logos found online. You can upload your own below."; return; }
  grid.innerHTML = res.choices.map((c, i) =>
    `<button class="lp-item" data-i="${i}" title="${esc(c.source)}"><img src="${esc(c.url)}" referrerpolicy="no-referrer" alt="" onerror="this.parentNode.remove()"><span>${esc(c.source)}</span></button>`).join("");
  grid.querySelectorAll(".lp-item").forEach(b => b.onclick = () => {
    grid.querySelectorAll(".lp-item").forEach(x => x.disabled = true);
    pick({url: res.choices[b.dataset.i].url}).then(() =>
      grid.querySelectorAll(".lp-item").forEach(x => x.disabled = false));
  });
}

function openModal(html) {
  $("modal-body").innerHTML = html;
  $("modal").classList.remove("hidden");
}
const closeModal = () => $("modal").classList.add("hidden");
$("modal-close").onclick = closeModal;

$("btn-home").onclick = () => {
  const fromMonitor = S.homeSource === "RDS Monitor";
  openModal(`<h2>Home &amp; Local / DX</h2>
    <p>Home: <b>${Number(S.home.lat).toFixed(5)}, ${Number(S.home.lon).toFixed(5)}</b></p>
    ${fromMonitor
      ? `<p class="muted">Same as the RDS Monitor's home location. Change it there
         (Tools › Set Home Location…) and the map follows.</p>`
      : `<p class="muted">No RDS Monitor folder found next to DX Mapper, so home is set here.
         Put the DX-Mapper folder next to RDS-Monitor to use its home location.</p>
         <div class="edit-row"><input id="h-lat" type="text" value="${S.home.lat}"><input id="h-lon" type="text" value="${S.home.lon}"></div>`}
    <p>Local = within <input id="h-mi" type="number" min="1" max="5000" value="${S.localMi}" style="width:80px"> miles of home.<br>
       Extended local = up to <input id="h-ext" type="number" min="1" max="5000" value="${S.extMi}" style="width:80px"> miles. Farther is DX.</p>
    <div class="edit-row"><button class="primary" id="h-save">Save</button>${fromMonitor ? "" : '<button id="h-centre">Use map centre</button>'}</div>
    <p id="h-msg"></p>`);
  if (!fromMonitor) $("h-centre").onclick = () => {
    const c = map.getCenter();
    $("h-lat").value = c.lat.toFixed(6); $("h-lon").value = c.lng.toFixed(6);
  };
  $("h-save").onclick = async () => {
    const body = {local_radius_mi: $("h-mi").value, extended_radius_mi: $("h-ext").value};
    if (!fromMonitor) Object.assign(body, {home_lat: $("h-lat").value, home_lon: $("h-lon").value});
    try {
      await post("/api/settings", body);
      closeModal(); refresh(true); toast("Saved");
    } catch (e) { $("h-msg").innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  };
};

// Add station: log a station with no RDS. Starts from the tuned frequency;
// every FCC station on it is listed closest first, since on the edge of
// coverage two can share a frequency - you pick the one you hear.
$("btn-addst").onclick = async () => {
  openModal(`<h2>Add station</h2>
    <p class="muted">For stations with no RDS. Every station on the frequency is listed, closest first.
      Type a call, slogan, city or state (e.g. "Springfield, IL", "central il") to narrow it down.</p>
    <div class="edit-row"><input id="as-freq" type="text" placeholder="MHz" style="width:80px">
      <input id="as-q" type="text" placeholder="Call, slogan, city, state (optional)" style="flex:1">
      <button class="primary" id="as-go">Search</button></div>
    <div id="as-list" class="as-list"></div>`);
  const list = $("as-list");
  const search = async () => {
    const freq = $("as-freq").value.trim(), q = $("as-q").value.trim();
    if (!freq && !q) { list.textContent = "Type a frequency or something to search for."; return; }
    list.textContent = "Searching the FCC…";
    let res;
    try {
      res = await api(`/api/stations/find?freq=${encodeURIComponent(freq)}&q=${encodeURIComponent(q)}`);
    } catch (e) { list.innerHTML = `<span class="err">${esc(e.message)}</span>`; return; }
    if (!res.stations.length) { list.textContent = "No stations found. Check the frequency or try fewer words."; return; }
    list.innerHTML = `<table class="as-table"><tr><th>Call</th><th>MHz</th><th>City</th><th>Type</th><th>ERP</th><th>Distance</th><th></th></tr>` +
      res.stations.map((r, i) => `<tr><td><b>${esc(r.callsign)}</b></td><td>${Number(r.freq_mhz).toFixed(1)}</td>
        <td>${esc(r.location || "")}</td><td>${esc(r.service || "")}${r.status === "CP" ? " (CP)" : ""}</td>
        <td>${r.erp_kw != null ? esc(r.erp_kw) + " kW" : ""}</td><td>${r.distance_mi} mi ${esc(r.compass)}</td>
        <td>${r.logged ? `<button data-open="${r.station_id}">Logged</button>` : `<button class="primary" data-i="${i}">Add</button>`}</td></tr>`).join("") + "</table>";
    list.querySelectorAll("button[data-i]").forEach(b => b.onclick = async () => {
      b.disabled = true;
      try {
        const out = await post("/api/stations/add", res.stations[b.dataset.i]);
        closeModal(); toast(`Logged ${res.stations[b.dataset.i].callsign}`);
        await refresh(true); select(out.station_id, true);
      } catch (e) { b.disabled = false; toast(e.message); }
    });
    list.querySelectorAll("button[data-open]").forEach(b => b.onclick = () => {
      closeModal(); select(Number(b.dataset.open), true);
    });
  };
  $("as-go").onclick = search;
  ["as-freq", "as-q"].forEach(id => $(id).onkeydown = (e) => { if (e.key === "Enter") search(); });
  try {
    const t = await api("/api/tuner/freq");
    if (t.freq_mhz && !$("as-freq").value) $("as-freq").value = Number(t.freq_mhz).toFixed(1);
  } catch (e) { /* tuner not running: type the frequency */ }
  if ($("as-freq").value) search(); else $("as-freq").focus();
};

$("btn-import").onclick = () => {
  openModal(`<h2>Import stations</h2>
    <div class="opt"><b>From a file</b> – Google Sheets download (File › Download › Microsoft Excel .xlsx, or CSV), any Excel .xlsx or CSV/TSV,
      an RDS Spy <code>.spy</code> file, or a <code>.txt</code> (RDS Spy group log or RDS Monitor report).
      <div class="edit-row"><input id="i-file" type="file" accept=".xlsx,.xlsm,.csv,.tsv,.txt,.spy,.tab"></div></div>
    <div class="opt"><b>From a Google Sheets link</b> – the sheet must be shared as “Anyone with the link can view”.
      Open the tab you want first so the link includes its <code>gid</code>.
      <div class="edit-row"><input id="i-url" type="text" placeholder="https://docs.google.com/spreadsheets/d/…"><button id="i-url-go">Load</button></div></div>
    <div id="i-preview"></div>`);
  $("i-file").onchange = () => {
    const file = $("i-file").files[0];
    if (!file) return;
    const reader = new FileReader();
    if (/\.xls[xm]$/i.test(file.name)) {
      reader.onload = () => preview({filename: file.name, data_b64: reader.result});
      reader.readAsDataURL(file);
    } else {
      reader.onload = () => preview({filename: file.name, text: reader.result});
      reader.readAsText(file);
    }
  };
  $("i-url-go").onclick = () => preview({url: $("i-url").value});
};

async function preview(body) {
  $("i-preview").innerHTML = '<p class="muted">Reading…</p>';
  let p;
  try { p = await post("/api/import/preview", body); }
  catch (e) { $("i-preview").innerHTML = `<p class="err">${esc(e.message)}</p>`; return; }
  const what = `${p.total} ${p.kind === "records" ? "station(s)" : "row(s)"} from ${p.name}`;
  if (p.kind === "records" || Object.values(p.mapping).some((v) => v === "callsign" || v === "pi")) {
    // Headers recognised: import straight away in the background.
    try {
      await post("/api/import/commit", {token: p.token, mapping: p.mapping,
                                         rx: (S.locations || {}).home ? "home" : ""});
      closeModal(); toast(`Importing ${what} in the background`); refresh(true);
    } catch (e) { $("i-preview").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
    return;
  }
  if (p.kind === "records") {
    $("i-preview").innerHTML = `<h2>${esc(p.name)} – ${esc(p.format)}</h2>
      <p>${p.total} station(s) found.</p>
      <div class="scroll-x"><table class="preview"><tr><th>PI</th><th>Call</th><th>MHz</th><th>PS</th><th>Location</th></tr>
      ${p.sample.map((r) => `<tr><td>${esc(r.pi)}</td><td>${esc(r.callsign)}</td><td>${esc(r.freq_mhz)}</td><td>${esc(r.ps)}</td><td>${esc(r.location)}</td></tr>`).join("")}</table></div>
      ${p.format === "RDS Spy groups" && !p.sample.some((r) => r.freq_mhz) ? '<p class="muted">The file has no frequency – it will be filled from the FCC record for the callsign.</p>' : ""}
      ${rxPicker()}<button class="primary" id="i-commit">Import ${p.total} station(s)</button> <span id="i-msg"></span>`;
  } else {
    const opts = (sel) => ['<option value="">(keep)</option>',
      `<option value="-"${sel === "-" ? " selected" : ""}>(ignore column)</option>`]
      .concat(p.fields.map((f) => `<option value="${f.key}"${sel === f.key ? " selected" : ""}>${esc(f.label)}</option>`)).join("");
    $("i-preview").innerHTML = `<h2>${esc(p.name)} – ${p.total} row(s)</h2>
      <p class="muted">Check what each column is. Detected columns are pre-selected; only your log book columns are kept, so notes and other columns are skipped.
      A row needs a callsign or PI code.</p>
      <div class="scroll-x"><table class="preview">
        <tr>${p.headers.map((h, i) => `<th><select data-col="${i}">${opts(p.mapping[i] || "")}</select></th>`).join("")}</tr>
        <tr>${p.headers.map((h) => `<th>${esc(h)}</th>`).join("")}</tr>
        ${p.sample.map((r) => `<tr>${p.headers.map((h, i) => `<td title="${esc(r[i])}">${esc(r[i])}</td>`).join("")}</tr>`).join("")}
      </table></div>
      ${rxPicker()}<button class="primary" id="i-commit">Import ${p.total} row(s)</button> <span id="i-msg"></span>`;
  }
  $("i-commit").onclick = async () => {
    const mapping = {};
    $("i-preview").querySelectorAll("select[data-col]").forEach((s) => mapping[s.dataset.col] = s.value);
    $("i-commit").disabled = true;
    try {
      await post("/api/import/commit", {token: p.token, mapping, rx: $("i-rx").value});
      closeModal(); toast(`Importing ${what} in the background`); refresh(true);
    } catch (e) {
      $("i-msg").innerHTML = `<span class="err">${esc(e.message)}</span>`;
      $("i-commit").disabled = false;
    }
  };
}

function showReport(lg) {
  let box = $("report-box");
  if (!box) {
    box = document.createElement("div");
    box.id = "report-box";
    box.className = "report-box";
    document.body.appendChild(box);
  }
  box.innerHTML = `<div class="report-inner"><div class="report-head"><b>RDS report – ${esc(fmtTime(lg.start_ts))}</b>
    <button id="report-close">Close</button></div><pre>${esc(lg.details.report)}</pre></div>`;
  box.classList.remove("hidden");
  $("report-close").onclick = () => box.classList.add("hidden");
}

// --------------------------------------------------------- now hearing ---
// The station the RDS Monitor is decoding right now (shown, not logged).
function drawNow(n) {
  const box = $("now");
  box.classList.toggle("hidden", !n);
  if (n && n.no_rds) box.innerHTML = tunedHtml(n);
  else if (n) {
    box.innerHTML = `<b>Now hearing:</b> ${esc(n.callsign || n.pi || "?")}${n.freq_mhz ? " " + fmtFreq(n.freq_mhz) : ""}` +
      (n.ps ? ` “${esc(n.ps.trim())}”` : "") + (n.location ? ` · ${esc(n.location)}` : "") +
      (hidingLoc() ? "" : n.distance_mi != null ? ` · ${n.distance_mi} mi ${esc(n.compass)} from ${esc(n.rx_name || "Home")}` : " · transmitter location unknown");
  }
  followTuner(n);
  drawCoverage(n);
  const key = n && n.lat != null ? [n.callsign || n.pi, n.freq_mhz, n.lat, n.lon, n.rx_lat, n.rx_lon, hidingLoc()].join("|") : "";
  if (key === S.nowKey) return;
  S.nowKey = key;
  if (S.nowLayer) { map.removeLayer(S.nowLayer); S.nowLayer = null; }
  if (!key) return;
  const label = `Now hearing ${esc(n.callsign || n.pi || "")}${n.freq_mhz ? " " + fmtFreq(n.freq_mhz) : ""}`;
  S.nowLayer = L.layerGroup([
    ...(hidingLoc() ? [] : [L.polyline([[n.rx_lat, n.rx_lon], [n.lat, n.lon]],
      {color: "#ff4fd8", weight: 3, dashArray: "2 8", interactive: false})]),
    L.marker([n.lat, n.lon], {zIndexOffset: 2000, interactive: false,
      icon: L.divIcon({className: "", iconSize: [26, 26], iconAnchor: [13, 13], html: '<div class="now-pulse"></div>'})})
      .bindTooltip(label, {direction: "top", permanent: true, offset: [0, -12], className: "now-tip"}),
  ]).addTo(map);
}

// No RDS: the Tuner's frequency and the station(s) logged on it. Several
// (fringe area) -> you pick which one you're hearing; it never guesses.
function tunedHtml(n) {
  const f = fmtFreq(n.freq_mhz), many = n.choices.length > 1;
  if (n.station_id) {
    return `<b>Now hearing:</b> ${esc(n.callsign || "?")} ${f} <i>(no RDS)</i>` +
      (n.location ? ` · ${esc(n.location)}` : "") +
      (hidingLoc() || n.distance_mi == null ? "" : ` · ${n.distance_mi} mi ${esc(n.compass)} from ${esc(n.rx_name || "Home")}`) +
      (many ? ` · <a href="#" data-pick="" title="Pick another station logged on ${f}">change</a>` : "");
  }
  if (many) {
    return `<b>Tuned to ${f}</b> <i>(no RDS)</i> · ${n.choices.length} logged stations - which one are you hearing? ` +
      n.choices.map((c) => `<button class="now-pick" data-pick="${c.id}" title="${esc([c.location, c.distance_mi != null && !hidingLoc() ? `${c.distance_mi} mi ${c.compass}` : ""].filter(Boolean).join(" · "))}">${esc(c.callsign || "?")}</button>`).join(" ");
  }
  return `<b>Tuned to ${f}</b> <i>(no RDS)</i> · nothing logged here yet · <button class="now-pick" data-addst>Add station…</button>`;
}
$("now").addEventListener("click", (e) => {
  const t = e.target.closest("[data-pick],[data-addst]");
  if (!t) return;
  e.preventDefault();
  if (t.hasAttribute("data-addst")) return $("btn-addst").click();
  post("/api/now/pick", {station_id: t.dataset.pick || null})
    .then(() => refresh(true)).catch((err) => toast(err.message));
});

// FCC service contour of the tuned station.
function drawCoverage(n) {
  const key = n && n.callsign && $("coverage").checked ? `${n.callsign}|${n.freq_mhz || ""}` : "";
  if (key === S.covKey) return;
  S.covKey = key;
  if (S.covLayer) { map.removeLayer(S.covLayer); S.covLayer = null; }
  if (!key) return;
  api(`/api/coverage?call=${encodeURIComponent(n.callsign)}&freq=${n.freq_mhz || ""}`).then((c) => {
    if (S.covKey !== key || !c.points) return;
    const top = c.points.reduce((a, b) => (b[0] > a[0] ? b : a));
    S.covLayer = L.layerGroup([
      L.polygon(c.points, {color: "#ff4fd8", weight: 2, opacity: 0.9, fillColor: "#ff4fd8",
        fillOpacity: 0.08, interactive: false}),
      L.marker(top, {interactive: false, icon: L.divIcon({className: "", iconSize: null,
        html: `<div class="ring-label">${esc(c.callsign)} coverage (${c.dbu} dBu, FCC)</div>`})}),
    ]).addTo(map);
  }).catch(() => {});
}
for (const [id, key] of [["coverage", "dxmap.coverage"], ["autocenter", "dxmap.autocenter"]]) {
  if (localStorage.getItem(key) === "0") $(id).checked = false;
  $(id).addEventListener("change", () => {
    localStorage.setItem(key, $(id).checked ? "1" : "0");
    if (id === "coverage") { S.covKey = null; drawCoverage(S.now); }
  });
}

// County and state lines over every map type (Census shapes, shipped with DX Mapper).
map.createPane("borders").style.zIndex = 350;
map.getPane("borders").style.pointerEvents = "none";
let bordersLayer = null;
function showBorders() {
  if (!$("borders").checked) { if (bordersLayer) map.removeLayer(bordersLayer); return; }
  if (bordersLayer) { bordersLayer.addTo(map); return; }
  fetch("/static/us-borders.json").then((r) => r.json()).then((us) => {
    if (bordersLayer || typeof topojson === "undefined") return;
    const sameState = (a, b) => Math.floor(a.id / 1000) === Math.floor(b.id / 1000);
    const counties = topojson.mesh(us, us.objects.counties, (a, b) => a !== b && sameState(a, b));
    const states = topojson.mesh(us, us.objects.states, (a, b) => a !== b);
    const line = (geo, style) => L.geoJSON(geo, {pane: "borders", interactive: false, style});
    bordersLayer = L.layerGroup([
      line(counties, {color: "#000", weight: 2.5, opacity: 0.25}),
      line(counties, {color: "#ffe680", weight: 1, opacity: 0.8, dashArray: "4 3"}),
      line(states, {color: "#000", weight: 4, opacity: 0.45}),
      line(states, {color: "#ffffff", weight: 2, opacity: 0.95}),
    ]);
    if ($("borders").checked) bordersLayer.addTo(map);
  }).catch(() => {});
}
if (localStorage.getItem("dxmap.borders") === "0") $("borders").checked = false;
$("borders").addEventListener("change", () => {
  localStorage.setItem("dxmap.borders", $("borders").checked ? "1" : "0");
  showBorders();
});
showBorders();

// Live NEXRAD radar (Iowa State Mesonet), fetched fresh every 5 minutes.
const RADAR_URL = "https://mesonet.agron.iastate.edu/cache/tile.py/1.0.0/nexrad-n0q-900913/{z}/{x}/{y}.png";
map.createPane("radar").style.zIndex = 340;
map.getPane("radar").style.pointerEvents = "none";
let radarLayer = null, radarTimer = null;
function showRadar() {
  if (!$("radar").checked) {
    if (radarLayer) map.removeLayer(radarLayer);
    clearInterval(radarTimer); radarTimer = null;
    return;
  }
  if (!radarLayer) radarLayer = L.tileLayer(RADAR_URL + "?t=" + Date.now(), {pane: "radar",
    opacity: 0.6, maxZoom: 19, maxNativeZoom: 12,
    attribution: "Radar &copy; Iowa Environmental Mesonet (NWS NEXRAD)"});
  radarLayer.addTo(map);
  if (!radarTimer) radarTimer = setInterval(() => radarLayer.setUrl(RADAR_URL + "?t=" + Date.now()), 5 * 60 * 1000);
}
if (localStorage.getItem("dxmap.radar") === "1") $("radar").checked = true;
$("radar").addEventListener("change", () => {
  localStorage.setItem("dxmap.radar", $("radar").checked ? "1" : "0");
  showRadar();
});
showRadar();

// Follow tuner: open whatever station the tuner is on, known or not, once per
// change of station (so you can still click around while it stays tuned).
function followTuner(n) {
  const key = n ? String(n.station_id || [n.pi, n.freq_mhz].join("|")) : "";
  if (key === S.followKey) return;
  S.followKey = key;
  if (!n || !$("tune-follow").checked) return;
  if (n.station_id && S.byId[n.station_id]) select(n.station_id, true);
  else if (n.lat != null) frameFromTuner(n.lat, n.lon, n.rx_lat, n.rx_lon, false);
}
$("tune-follow").checked = localStorage.getItem("dxmap.followTuner") === "1";
$("tune-follow").onchange = () => {
  localStorage.setItem("dxmap.followTuner", $("tune-follow").checked ? "1" : "0");
  S.followKey = null;
  if ($("tune-follow").checked) followTuner(S.now);
};

// ------------------------------------------------------------- backup ---
function backupText(b) {
  if (!b) return "";
  if (b.error) return `Backup problem: ${b.error}`;
  return b.last ? `master backup ${new Date(b.last * 1000).toLocaleTimeString()}` : "";
}
function backupStatus() {
  const b = S.backup || {};
  return (b.dirs ? `Backing up to: ${b.dirs.join("  and  ")}. ` : "") + backupText(b);
}
async function backupPost(url, body) {
  try {
    const st = await post(url, body);
    S.backup = st.backup;
    S.settings = st.settings;
    $("b-status").className = st.backup.error ? "err" : "ok";
    $("b-status").textContent = backupStatus();
  } catch (e) {
    $("b-status").className = "err";
    $("b-status").textContent = e.message;
  }
}
$("btn-monfile").onclick = async () => {
  try {
    await post("/api/monitor/sync", {});
    S.monSync = true;
    localStorage.removeItem("dxmap.updClosed");
    refresh();
  } catch (e) { toast(e.message); }
};
$("btn-backup").onclick = () => {
  $("b-dir").value = (S.settings && S.settings.backup_dir) || "";
  $("b-daily").checked = !!(S.settings && S.settings.daily_backups);
  $("r-dir").value = (S.backup && S.backup.dirs && S.backup.dirs[S.backup.dirs.length - 1]) || "";
  $("b-status").className = "muted";
  $("b-status").textContent = backupStatus();
  $("backup-dlg").showModal();
};
$("b-save").onclick = () => backupPost("/api/settings",
  {backup_dir: $("b-dir").value.trim(), daily_backups: $("b-daily").checked});
$("b-now").onclick = () => backupPost("/api/backup", {});
$("b-close").onclick = () => $("backup-dlg").close();
$("btn-conditions").onclick = () => $("cond-dlg").showModal();
$("cond-close").onclick = () => $("cond-dlg").close();
for (const b of document.querySelectorAll(".cond-site")) {
  b.onclick = () => post("/api/open-site", {site: b.dataset.site})
    .then(() => toast("Opening it in your web browser…"))
    .catch((e) => toast(e.message || "Couldn't open the website"));
}
$("r-go").onclick = async () => {
  if (!confirm("Replace the whole log with the backup in " + $("r-dir").value + "?")) return;
  try {
    const st = await post("/api/restore", {path: $("r-dir").value});
    S.backup = st.backup;
    $("b-status").className = "ok";
    $("b-status").textContent = `Restored ${st.restored.stations} station(s), ${st.restored.logs} log(s).`;
    refresh(true);
  } catch (e) {
    $("b-status").className = "err";
    $("b-status").textContent = e.message;
  }
};

legend();
restoreViewOptions();
setView(S.view);
setInterval(refresh, 1500);

function rxPicker() {
  const L = S.locations || {};
  const opt = (k, loc) => loc ? `<option value="${k}">${esc(loc[2])} (${loc[0].toFixed(3)}, ${loc[1].toFixed(3)})</option>` : "";
  return `<p>Logged from: <select id="i-rx"><option value="">not recorded</option>${opt("home", L.home)}${opt("away", L.away)}</select>
    <span class="muted">Where you were for these logs (Home/Away are set in the RDS Monitor's Tools menu).</span></p>`;
}

// --------------------------------------------------------- local / DX ---
function tier(st) {
  const d = st.distance_mi;
  if (d == null) return null;
  return d <= S.localMi ? "local" : d <= S.extMi ? "ext" : "dx";
}
function inReach(st) {
  return S.reach === "all" || tier(st) === S.reach;
}
function applyReach() {
  const mi = Math.round(S.localMi), ext = Math.round(S.extMi);
  const labels = {all: "All", local: `Local 0–${mi}`, ext: `Ext ${mi + 1}–${ext}`, dx: `DX ${ext + 1}+`};
  for (const b of document.querySelectorAll("#reach button")) {
    b.textContent = labels[b.dataset.reach];
    b.classList.toggle("active", b.dataset.reach === S.reach);
  }
}
$("reach").onclick = (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  S.reach = b.dataset.reach;
  localStorage.setItem("dxmap.reach", S.reach);
  applyReach();
  S.stations.forEach(upsertMarker);
  if (S.selected && S.byId[S.selected] && !inReach(S.byId[S.selected])) $("details-close").click();
  renderList();
  renderLogs();
};
applyReach();

$("btn-logbook").onclick = async () => {
  if (window.pywebview) {  // the app window: open it in the web browser
    try { await post("/api/open-logbook", {}); toast("Log Book opened in your web browser"); }
    catch (e) { toast(e.message); }
  } else {
    window.open("/logbook", "dxlogbook");
  }
};

// An HD2-HD8 entry without its own logo shows its station's.
function logoFallback(img) {
  const m = img.src.match(/\/api\/logo\/([A-Za-z0-9]+)-HD[2-8]/i);
  if (m && !img.dataset.base) { img.dataset.base = "1"; img.src = `/api/logo/${m[1]}?t=${Date.now()}`; return; }
  img.parentNode.classList.add("nologo");
}

// ------------------------------------------------------------- Logs --
// Tick logs to show together; the first one ticked gets catches and edits.
const LG = {order: [], logs: []};
function logsList() {
  $("logs-list").innerHTML = LG.logs.map((n) => {
    const i = LG.order.indexOf(n);
    return `<li><label><input type="checkbox" data-tick="${esc(n)}"${i >= 0 ? " checked" : ""}> <b>${esc(n)}</b></label>
      <span class="muted">${i === 0 ? "✎ new catches and edits go here" : i > 0 ? `#${i + 1}, view only` : ""}</span>
      <span class="lg-btns"><button data-ren="${esc(n)}">Rename…</button><button class="danger" data-del="${esc(n)}">Delete…</button></span></li>`;
  }).join("");
}
function logsLoad(r) {
  LG.logs = r.logs; LG.order = r.logs_shown.slice(); logsList();
  if ("log_to_all" in r) $("logs-all").checked = !!r.log_to_all;
}
$("logs-all").onchange = (e) => {
  if (!e.target.checked) return;
  e.target.checked = false;  // only the popup's Turn it on ticks it
  const rest = LG.order.slice(1);
  $("logall-which").textContent = rest.length
    ? `With ${LG.order.join(" + ")} ticked, a catch goes into ${LG.order[0]} and also into ${rest.join(" and ")}, so ${rest.join(" and ")} would get catches from where you are now.`
    : "Right now only one log is ticked, so nothing changes until you tick more.";
  $("logall-dlg").showModal();
};
$("logall-on").onclick = () => { $("logs-all").checked = true; $("logall-dlg").close(); };
$("logall-off").onclick = () => { $("logs-all").checked = false; $("logall-dlg").close(); };
const logsMsg = (t) => { $("logs-status").textContent = t; };
$("btn-logs").onclick = async () => {
  try { logsLoad(await api("/api/state")); } catch (e) { return toast(e.message); }
  logsMsg(""); $("logs-dlg").showModal();
};
$("logs-list").onchange = (e) => {
  const n = e.target.dataset.tick;
  if (n == null) return;
  LG.order = LG.order.filter((x) => x !== n);
  if (e.target.checked) LG.order.push(n);
  logsList();
};
$("logs-list").onclick = async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  try {
    if (b.dataset.ren) {
      const nn = prompt(`New name for the ${b.dataset.ren} log:`, b.dataset.ren);
      if (!nn || nn.trim() === b.dataset.ren) return;
      logsLoad(await post("/api/logbooks/rename", {old: b.dataset.ren, new: nn}));
      logsMsg(`Renamed to ${nn.trim()}`);
    } else if (b.dataset.del) {
      const n = b.dataset.del;
      if (!confirm(`Delete the ${n} log?\n\nThis removes ${n}'s stations, log entries, Excel copy and backups. Your other logs aren't touched. This can't be undone.`)) return;
      if (prompt(`To be sure, type ${n} to delete it:`) !== n) return logsMsg("Not deleted");
      logsLoad(await post("/api/logbooks/delete", {name: n}));
      logsMsg(`Deleted the ${n} log`);
    }
    refresh(true);
  } catch (err) { logsMsg(err.message); }
};
$("logs-new").onclick = async () => {
  const n = $("logs-name").value.trim();
  try {
    logsLoad(await post("/api/logbooks/new", {name: n}));
    $("logs-name").value = "";
    logsMsg(`Made ${n}. Tick it to show it; tick it first to log into it.`);
  } catch (err) { logsMsg(err.message); }
};
$("logs-save").onclick = async () => {
  try { logsLoad(await post("/api/logbooks/show", {names: LG.order, log_to_all: $("logs-all").checked})); }
  catch (err) { return logsMsg(err.message); }
  $("logs-dlg").close();
  if (S.selected != null) $("details-close").onclick();
  toast(`Showing ${LG.order.join(" + ")}. New catches go into ${$("logs-all").checked && LG.order.length > 1 ? "all of them" : LG.order[0]}.`);
  refresh(true);
};
$("logs-close").onclick = () => $("logs-dlg").close();
