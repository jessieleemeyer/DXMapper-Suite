// Station Details sections, shared by the map's details panel (app.js)
// and the Log Book's Modern side panel (logbook.html).
const DXDetails = (() => {
const esc = (v) => String(v ?? "").replace(/[&<>"']/g,
  (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const fmtTime = (t) => t ? new Date(t * 1000).toLocaleString() : "";
const fmtFreq = (f) => f ? Number(f).toFixed(1) : "—";
const kv = (rows) => '<table class="kv">' + rows.filter((r) => r[1] !== undefined &&
  r[1] !== null && r[1] !== "").map((r) => `<tr><td>${esc(r[0])}</td><td>${r[2] ? r[1] : esc(r[1])}</td></tr>`).join("") + "</table>";
function sUnits(dbfs, unit = true, units = "s") {
  if (dbfs == null || dbfs <= -999) return "";
  if (units === "db") return dbfs.toFixed(1) + (unit ? " dBFS" : "");
  const s = (dbfs + 70) / 6 + 1;
  if (s < 1) return "S0";
  return s < 9.5 ? `S${Math.round(s)}` : `S9+${Math.round((s - 9) * 6)}`;
}

// o: {hide: hide your location, fmtSig, manage: show log delete buttons}
function build(st, o = {}) {
  const hide = !!o.hide;
  const fmtSig = o.fmtSig || ((d, u = true) => sUnits(d, u, o.units));
  const call = st.callsign || "";
  const fcc = st.fcc || {};
  const fccFound = st.fcc && !st.fcc.not_found;
  const links = [];
  if (call) {
    links.push(`<a target="_blank" href="https://en.wikipedia.org/wiki/${encodeURIComponent(call + " (FM)")}">Wikipedia</a>`);
    links.push(`<a target="_blank" href="https://transition.fcc.gov/fcc-bin/fmq?call=${encodeURIComponent(call)}">FCC FM Query</a>`);
    if (fcc.facility_id) links.push(`<a target="_blank" href="https://enterpriseefiling.fcc.gov/dataentry/public/tv/publicFacilityDetails.html?facilityId=${encodeURIComponent(fcc.facility_id)}">FCC LMS</a>`);
    links.push(`<a target="_blank" href="https://www.fmlist.org/ul_search.php?keyword=${encodeURIComponent(call)}">FMLIST</a>`);
    links.push(`<a target="_blank" href="https://www.google.com/search?q=${encodeURIComponent(call + " " + fmtFreq(st.freq_mhz) + " FM")}">Google</a>`);
  }
  const book = st.logbook || {};
  const bookUsed = new Set(book.used || []);
  // HD1 is the analog station's own entry; HD2-HD8 link both ways.
  const fam = st.hd_family || [];
  const hdLink = (f) => f.id == null
    ? `<span title="Listed on Wikipedia - not logged yet">${esc(f.hd || "HD1")}${f.slogan ? " · " + esc(f.slogan) : ""}</span>`
    : `<a href="#" class="hd-link" data-id="${f.id}" title="Open ${esc(f.callsign)}">${esc(f.hd || "HD1")}${f.slogan ? " · " + esc(f.slogan) : ""}</a>`;
  const stationLink = (id, text, title) => id == null ? esc(text)
    : `<a href="#" class="hd-link" data-id="${id}" title="${esc(title || "Open")}">${esc(text)}</a>`;
  const fedBy = st.fed_by ? stationLink(st.fed_by.id, st.fed_by.callsign + (st.fed_by.freq_mhz ? " " + fmtFreq(st.fed_by.freq_mhz) : ""), "Open the station this translator relays") : null;
  const translators = (st.translators || []).length ? st.translators.map((x) =>
    stationLink(x.id, [x.freq_mhz ? fmtFreq(x.freq_mhz) : null, x.callsign, x.city].filter(Boolean).join(" "),
      x.id == null ? "Listed on Wikipedia - not logged yet" : "Open this translator")).join(", ") : null;
  const ownHd = ((call.match(/-HD([2-8])$/i) || [])[0] || "-HD1").slice(1).toUpperCase();
  const hdChannels = fam.length ? [{own: true, hd: ownHd}, ...fam.map((f) => ({f, hd: f.hd || "HD1"}))]
    .sort((a, b) => a.hd.localeCompare(b.hd))
    .map((x) => x.own ? `${esc(x.hd)} (this entry)` : hdLink(x.f)).join(", ") : null;
  const bookRows = [["Frequency", book.freq], ["Call Letters", book.callsign], ["HD", book.hd],
    ["HD Channels", book.hd_channels],
    ["Slogan / Format", book.slogan], ["PI Code", book.pi], ["City", book.city], ["State", book.state],
    ["Calibrated Distance", hide ? null : book.distance], ["Range Type", book.range_type],
    ["Owner / Licensee", book.owner], ["Time Decoding", book.time_decoding],
    ["RT+ Capabilities", book.rt_plus], ["RadioText", book.radiotext],
    ["Group Sequence", book.group_seq ? `<code class="d-seq" title="RDS group types in the order they arrived - each station's encoder repeats its own pattern">${esc(book.group_seq)}</code>` : null, true],
    ["Live Stream", book.stream ? `${esc(st.stream && st.stream.source ? st.stream.source : "found")} - use Stream above` : null, true]].filter((r) => r[1]);
  const logs = st.logs || [];
  const rep = logs.find((lg) => lg.details && lg.details.report_info);
  // RDS features over every logged report: ✓ only for apps actually decoded.
  // ✓ decoded, ● data seen but no decoder yet; the best over all logs wins.
  const feat = {list: new Map(), adv: new Map(), any: false};
  for (const lg of logs) {
    const f = lg.details && lg.details.report_features;
    if (!f) continue;
    feat.any = true;
    for (const e of f.features || []) {
      const had = feat.list.get(e.name);
      if (!had || (had.state !== "decoded" && e.state === "decoded")) feat.list.set(e.name, e);
    }
    for (const a of f.advertised) feat.adv.set(a.aid + a.group, a);
  }
  const featRows = [...feat.list.values()].map((e) => [e.name, (e.state === "decoded"
    ? '<span class="feat-yes" title="decoded">✓</span>'
    : '<span class="feat-seen" title="data received, no decoder yet">●</span>')
    + (e.detail ? ` <span class="muted">${esc(e.detail)}</span>` : ""), true]);
  const advRows = [...feat.adv.values()].filter((a) => a.known !== false)
    .map((a) => [`0x${a.aid} (${a.group})`, esc(a.app), true]);
  // Decode badges like the RDS Monitor's, only for what was decoded; the best
  // result over all logged reports wins (green beats yellow, bigger count).
  const BADGE_ORDER = ["PS", "RT", "PTY", "PTYN", "RT+", "TMC", "AF", "EON", "CT"];
  const badges = new Map();
  for (const lg of logs) {
    for (const b of (lg.details && lg.details.report_badges) || []) {
      const had = badges.get(b.name);
      badges.set(b.name, !had ? {...b} : {...had,
        state: had.state === "ok" || b.state === "ok" ? "ok" : "part",
        count: Math.max(had.count || 0, b.count || 0) || null});
    }
  }
  for (const b of book.log_badges || []) if (!badges.has(b.name)) badges.set(b.name, b);
  const badgeHtml = [...badges.values()]
    .sort((a, b) => ((BADGE_ORDER.indexOf(a.name) + 1) || 99) - ((BADGE_ORDER.indexOf(b.name) + 1) || 99))
    .map((b) => `<span class="badge ${b.state === "ok" ? "b-ok" : "b-part"}" title="${b.from_log ? "decoded (from your log)" : b.state === "ok" ? "decoded" : "partly decoded"}">${esc(b.name)}${b.count ? `(${b.count})` : ""}</span>`)
    .join("");
  const coordsText = st.lat != null ? `${st.lat.toFixed(4)}, ${st.lon.toFixed(4)}` : null;
  // Analog and HD each get their own section: every item that can be
  // decoded, ticked once any log observed it (the values stay in Full report).
  const isSub = /-HD[2-8]$/i.test(call);
  const baseCall = call.replace(/-HD[1-8]$/i, "");
  const checks = (items) => `<div class="checks">${items.map(([name, seen, title]) =>
    `<div class="ck ${seen ? "ck-yes" : "ck-no"}" title="${esc(title || (seen ? "observed" : "not observed yet"))}"><span>${seen ? "✓" : "–"}</span>${esc(name)}</div>`).join("")}</div>`;
  const seenFeat = new Set([...feat.list.values()].filter((e) => e.state === "decoded").map((e) => e.name));
  const has = (badge, f) => badges.has(badge) || seenFeat.has(f || badge);
  const rdsItems = [["PI code", !!st.pi], ["PS", badges.has("PS") || !!st.ps], ["PTY", badges.has("PTY") || st.pty != null],
    ["RadioText (RT)", badges.has("RT")], ["RT+ (song / artist)", has("RT+")], ["PTYN", has("PTYN")],
    ["Long PS", seenFeat.has("Long PS")], ["AF list", has("AF")], ["EON", has("EON")], ["TMC traffic", has("TMC")],
    ["Clock time (CT)", has("CT", "CT (clock time)")], ["TP", seenFeat.has("TP")], ["TA", seenFeat.has("TA")],
    ["DI", seenFeat.has("DI")]];
  const listed = new Set(["RT+", "PTYN", "Long PS", "AF", "EON", "TMC", "CT (clock time)", "TP", "TA", "DI", "HD Radio"]);
  for (const name of seenFeat) if (!listed.has(name)) rdsItems.push([name, true]);
  const builtIn = new Set(["4BD7", "4BD8", "CD46", "CD47"]);  // RT+ / TMC already listed
  for (const x of feat.adv.values())
    if (x.known !== false && x.app && !builtIn.has(String(x.aid).toUpperCase()) && !seenFeat.has(x.app))
      rdsItems.push([x.app, true, `announced on ${x.group}`]);
  const analogHtml = isSub ? "" : `<div class="d-section" data-fold="analog"><h3>${esc(baseCall || st.pi || "?")} · Analog</h3>
    <h4>Decoded RDS</h4>${checks(rdsItems)}</div>`;
  const hdc = st.hd_checklist || {items: []};
  const showHd = isSub || hdc.locked || fam.length > 0 || book.hd === "1";
  const merGrade = (m) => m >= 10 ? ["mer-good", "good"] : m >= 6 ? ["mer-fair", "fair"] : ["mer-weak", "weak"];
  const hdRows = [];
  if (hdc.best_mer != null) {
    const g = merGrade(hdc.best_mer);
    hdRows.push(["Best MER", `<span class="${g[0]}">${hdc.best_mer.toFixed(1)} dB ${g[1]}</span>`, true]);
  }
  if (hdc.last_lock) hdRows.push(["Last HD lock", fmtTime(hdc.last_lock)]);
  if (hdChannels) hdRows.push(["HD Channels", hdChannels, true]);
  const hdHtml = showHd ? `<div class="d-section" data-fold="hd"><h3>${esc(isSub ? call.toUpperCase() : (baseCall || "?") + "-HD")} · HD Radio</h3>
    ${hdRows.length ? kv(hdRows) : ""}<h4>Decoded HD</h4>${checks(hdc.items.map((x) => [x.name, x.seen]))}
    ${hdc.locked ? "" : '<p class="muted">No HD lock logged yet.</p>'}</div>` : "";
  const locHtml = `<div class="d-section" data-fold="location"><h3>Location</h3>${kv([
    ["Location", st.location], ["Country", st.country],
    ["Distance", st.distance_mi != null && !hide ? `${st.distance_mi} mi (${(st.distance_mi * 1.609344).toFixed(0)} km) ${st.compass} ${String(st.bearing_deg).padStart(3, "0")}°` : null],
    ["Transmitter", coordsText], ["Coords from", st.coord_source],
    ["Location check", st.location_check && !hide ? (st.location_check.ok
      ? `✓ matches your log (${esc(st.location_check.logged_mi)} mi)`
      : `<span class="err">⚠ your log says ${esc(st.location_check.logged_mi)} mi, the map says ${esc(st.location_check.map_mi)} mi from Home – fix the coordinates below</span>`) : null, true],
    ["Propagation", st.mode]])}</div>`;
  const repLog = logs.find((lg) => lg.details && lg.details.report) || st.parent_report;
  const fullRep = repLog ? `<div class="d-fullrep"><a href="#" id="d-fullrep" title="The complete report, as the RDS Monitor wrote it">Full report</a>
    <span class="muted">latest, ${esc(fmtTime(repLog.start_ts))}</span></div>` : "";
  const bookHtml = bookRows.length ? `<div class="d-section"><h3>Log book</h3>${kv(bookRows)}</div>` : "";
  const extraHtml = `
    <div class="d-section"><h3>Reception</h3>${kv([
      ["First logged", fmtTime(st.first_heard)], ["Last logged", fmtTime(st.last_heard)],
      ["Times logged", st.times_logged],
      ["Best signal", fmtSig(st.best_signal_dbfs) || null],
      ["Best usable info", st.best_usable_info != null ? Math.round(st.best_usable_info) + "%" : null],
      ["Best block quality", st.best_block_pct != null ? st.best_block_pct.toFixed(1) + "%" : null],
      ["Sources", (st.sources || []).join(", ")]])}</div>
    <div class="d-section"><h3>FCC</h3>${fccFound ? kv([
      ["Translator", fcc.relays ? fcc.callsign : null], ["Relays", fcc.relays ? fcc.relays + (fcc.parent && fcc.parent.freq_mhz ? " (" + fcc.parent.freq_mhz + " MHz)" : "") : null], ["Licensee", fcc.licensee], ["City of license", fcc.location], ["Service / class", [fcc.service, fcc.station_class].filter(Boolean).join(" / ")],
      ["ERP", fcc.erp_kw != null ? fcc.erp_kw + " kW" : null], ["HAAT", fcc.haat_m != null ? fcc.haat_m + " m" : null],
      ["Antenna", fcc.antenna], ["Status", fcc.status], ["Facility ID", fcc.facility_id],
      ["FCC coords", fcc.lat != null ? `${fcc.lat.toFixed(4)}, ${fcc.lon.toFixed(4)}` : null]])
      : `<p class="muted">${st.fcc && st.fcc.not_found ? "No FCC record found for this callsign." : call ? "Looking up FCC record…" : "No callsign – add one below to look it up."}</p>`}
      ${call ? '<button id="d-fcc">Refresh FCC data</button>' : ""}
      ${links.length ? `<div class="links" style="margin-top:6px">${links.join("")}</div>` : ""}</div>
    ${st.wtfda && st.wtfda.callsign ? `<div class="d-section" data-fold="wtfda"><h3>WTFDA database</h3>${kv([
      ["Format", st.wtfda.format], ["Slogan", st.wtfda.slogan], ["PI", st.wtfda.pi], ["PS", st.wtfda.ps],
      ["RadioText", st.wtfda.radiotext], ["PTY", st.wtfda.pty], ["Relay of", st.wtfda.relay_of],
      ["ERP", st.wtfda.erp_h ? st.wtfda.erp_h + (st.wtfda.erp_v && st.wtfda.erp_v !== st.wtfda.erp_h ? " / " + st.wtfda.erp_v : "") + " kW" : null],
      ["HAAT", st.wtfda.haat_h ? st.wtfda.haat_h + " m" : null], ["Mode", st.wtfda.mode],
      ["Language", st.wtfda.language], ["Remarks", st.wtfda.remarks]])}</div>` : ""}
    ${fedBy || translators ? `<div class="d-section"><h3>Translators</h3>${kv([["Fed by", fedBy, true], ["Translators", translators, true]])}</div>` : ""}`;
  const logsHtml = `
    <div class="d-section"><h3>Log history (${logs.length})</h3>
      <table class="logs"><tr><th>When</th><th>From</th><th>Source</th><th>Signal</th><th>Usable</th><th>PS / RT</th><th></th></tr>
      ${logs.map((lg) => `<tr${lg.view_only ? ' class="vo"' : ""}><td>${esc(fmtTime(lg.start_ts))}${lg.log && st.also_in && st.also_in.length || st.view_only ? `<br><span class="badge">${esc(lg.log)}</span>` : ""}${lg.end_ts && lg.end_ts - lg.start_ts > 60 ? `<br><span class="muted">to ${esc(new Date(lg.end_ts * 1000).toLocaleTimeString())}</span>` : ""}</td>
        <td>${esc(lg.rx_name || "")}${lg.rx_distance_mi != null && !hide ? `<br><span class="muted">${lg.rx_distance_mi} mi ${esc(lg.rx_compass || "")}</span>` : ""}</td>
        <td>${esc(lg.source)}${lg.auto ? " (auto)" : ""}</td>
        <td>${fmtSig(lg.signal_dbfs, false)}</td>
        <td>${lg.usable_info != null && lg.usable_info >= 0 ? Math.round(lg.usable_info) + "%" : ""}</td>
        <td>${esc(lg.ps || "")}${lg.rt ? `<br><span class="muted">${esc(lg.rt)}</span>` : ""}</td>
        <td>${lg.details && lg.details.report ? `<button data-report="${lg.id}" title="Full RDS report sent with this log">Report</button>` : ""}${o.manage && lg.view_only && st.write_log ? `<button data-copylog="${lg.id}" title="Copy this catch into ${esc(st.write_log)}, with the station's HD channels and their entries from the same catch">→ ${esc(st.write_log)}</button>` : ""}${o.manage && !lg.view_only ? `<button data-log="${lg.id}" title="Delete this log entry">×</button>` : ""}</td></tr>`).join("")}</table></div>`;
  const also = st.also_in || [];
  const copyHtml = st.view_only
    ? `<div class="d-section vo-box"><b>From your ${esc(st.log)} log – view only.</b> Edits, Delete and new catches don't change it.${o.manage && st.write_log ? `<div class="p-btns"><button class="primary" data-copy="${st.id}" title="Copy this station and its HD channels into ${esc(st.write_log)}. If it's already there, only its blanks get filled.">Copy to ${esc(st.write_log)}</button></div>` : ""}</div>`
    : also.length ? `<div class="d-section vo-box">In <b>${esc(st.log)}</b>. Also in ${also.map((a) => `<b>${esc(a.log)}</b>${a.logs != null ? ` (${a.logs} log${a.logs === 1 ? "" : "s"}${a.first ? ", first heard " + esc(new Date(a.first * 1000).toLocaleDateString()) : ""})` : ""}`).join(", ")}; anything missing here is filled in from there.</div>` : "";
  return {copyHtml, book, bookRows, bookHtml, logs, repLog, fullRep, badgeHtml, analogHtml, hdHtml, locHtml,
          extraHtml, logsHtml, coordsText, fam};
}
function copiedText(x) {
  return `Copied ${x.stations} station${x.stations === 1 ? "" : "s"}${x.logs ? ` and ${x.logs} log entr${x.logs === 1 ? "y" : "ies"}` : ""} from ${x.from} into ${x.into}`;
}
// Sister stations / Same market: logged ones open the station, the rest
// are plain text. d is /api/station/<id>/neighbors.
const NB_BOX = '<div class="d-section" data-fold="neighbors"><h3>Sister stations &amp; same market</h3><div id="d-nb" class="muted">Looking up…</div></div>';
function neighborsHtml(d) {
  const one = (x) => {
    const text = [x.freq_mhz ? fmtFreq(x.freq_mhz) : null, x.callsign].filter(Boolean).join(" ");
    return x.id != null
      ? `<a href="#" class="hd-link" data-id="${x.id}" title="Open ${esc(x.callsign)} (in your log)">${esc(text)}</a>`
      : `<span title="Not in your log yet">${esc(text)}</span>`;
  };
  const list = (xs) => xs && xs.length ? xs.map(one).join(", ") : '<span class="muted">None found</span>';
  return kv([["Sister stations", list(d.sisters), true], ["Same market", list(d.market), true]])
    + (d.error ? `<div class="muted">${esc(d.error)} - showing what's in your log.</div>` : "");
}
return {build, kv, esc, copiedText, NB_BOX, neighborsHtml};
})();
