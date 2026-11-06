const $ = (id) => document.getElementById(id);
const log = (msg) => {
  const el = $("log");
  el.textContent = `[${new Date().toLocaleTimeString()}] ${msg}\n` + el.textContent;
};

let current = null;
/** null = all chips on (unconstrained); Set = allow-list. */
let enabledGenres = null;
let availableGenres = [];

function apiBase() {
  return ($("apiBase").value || "http://127.0.0.1:8765").replace(/\/$/, "");
}

async function api(path, opts = {}) {
  const res = await fetch(apiBase() + path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const text = await res.text();
  let data;
  try { data = JSON.parse(text); } catch { data = { raw: text }; }
  if (!res.ok) throw new Error(data.detail || data.error || res.statusText || text);
  return data;
}

function chipOn(g) {
  return enabledGenres === null || enabledGenres.has(g);
}

function render(rec) {
  current = rec;
  const card = $("card");
  card.classList.remove("empty");
  $("status").textContent = "";
  $("title").textContent = rec.title || "";
  $("artist").textContent = rec.artist || "";
  const handoff = rec.ytm_handoff || (rec.video_id ? "watch" : "search_fallback");
  $("meta").textContent = `[${rec.lane || "?"}] confidence=${(rec.confidence ?? 0).toFixed(3)} · ${handoff}${rec.video_id ? " · " + rec.video_id : ""}`;
  $("why").textContent = rec.why || "";
  $("btnPlay").disabled = !(rec.ytm_url || rec.ytm_search_url);
}

async function nextRec() {
  $("status").textContent = "Fetching…";
  const lane = $("lane").value || "auto";
  const qs = new URLSearchParams({ lane });
  if (enabledGenres !== null) qs.set("enabled_genres", [...enabledGenres].join(","));
  const data = await api(`/next?${qs.toString()}`);
  render(data.recommendation);
  log(`next: ${data.recommendation.artist} — ${data.recommendation.title}`);
}

async function rate(rating) {
  if (!current) return;
  await api("/rate", {
    method: "POST",
    body: JSON.stringify({
      artist: current.artist,
      title: current.title,
      rating,
      lane: current.lane || "",
      confidence: current.confidence,
    }),
  });
  log(`rated ${rating}`);
  await nextRec();
}

async function skip() {
  await rate("skip");
}

async function notNow() {
  if (!current) return;
  await api("/not_now", {
    method: "POST",
    body: JSON.stringify({ artist: current.artist, title: current.title, lane: current.lane || "" }),
  });
  log("not_now");
  await nextRec();
}

function playYTM() {
  if (!current) return;
  const url = (current.video_id && current.ytm_url && current.ytm_url.includes("watch?v="))
    ? current.ytm_url
    : (current.ytm_url || current.ytm_search_url);
  window.open(url, "_blank", "noopener,noreferrer");
  log(`handoff YTM (${current.ytm_handoff || "?"}): ${url}`);
}

function renderGenreChips() {
  const box = $("genreChips");
  box.innerHTML = "";
  availableGenres.forEach((g) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = g;
    b.className = chipOn(g) ? "chip on" : "chip off";
    b.onclick = () => toggleGenre(g);
    box.appendChild(b);
  });
}

async function toggleGenre(g) {
  const on = new Set(enabledGenres === null ? availableGenres : [...enabledGenres]);
  if (on.has(g)) on.delete(g);
  else on.add(g);
  const allOn = availableGenres.length > 0 && availableGenres.every((x) => on.has(x));
  enabledGenres = allOn ? null : on;
  renderGenreChips();
  await applyDials();
  await nextRec().catch((e) => { $("status").textContent = e.message; log(e.message); });
}

async function loadDials() {
  const data = await api("/dials");
  const preset = $("preset");
  preset.innerHTML = '<option value="">(none)</option>';
  (data.presets || []).forEach((p) => {
    const o = document.createElement("option");
    o.value = p; o.textContent = p;
    if (p === data.preset) o.selected = true;
    preset.appendChild(o);
  });
  if (data.lane) $("lane").value = data.lane;
  availableGenres = data.available_genres || [];
  enabledGenres = data.enabled_genres == null ? null : new Set(data.enabled_genres);
  renderGenreChips();
  const box = $("tagDials");
  box.innerHTML = "";
  (data.available_tag_dials || []).slice(0, 40).forEach((t) => {
    const s = document.createElement("span");
    s.textContent = t;
    box.appendChild(s);
  });
}

async function applyDials() {
  await api("/dials", {
    method: "POST",
    body: JSON.stringify({
      lane: $("lane").value,
      preset: $("preset").value,
      filters: {},
      targets: {},
      ignore: [],
      enabled_genres: enabledGenres === null ? null : [...enabledGenres],
    }),
  });
  log("dials applied");
  await loadDials();
}

$("btnSkip").onclick = () => skip().catch((e) => { $("status").textContent = e.message; log(e.message); });
$("btnPlay").onclick = playYTM;
$("btnNotNow").onclick = () => notNow().catch((e) => log(e.message));
$("btnApplyDials").onclick = () => applyDials().catch((e) => log(e.message));
$("btnHealth").onclick = () => api("/health").then((d) => log(JSON.stringify(d))).catch((e) => log(e.message));
document.querySelectorAll(".rates [data-rate]").forEach((btn) => {
  btn.onclick = () => rate(btn.dataset.rate).catch((e) => log(e.message));
});

loadDials()
  .then(() => nextRec())
  .catch((e) => { $("status").textContent = e.message; log("dials: " + e.message); });
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("sw.js").catch(() => {});
}
