// Logged out (or session expired): any API call answering 401 sends you to the login page.
const _fetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const res = await _fetch(...args);
  if (res.status === 401 && !String(args[0]).startsWith("/api/login")) location.replace("/login");
  // The service worker marks saved copies it serves while the Mac is unreachable.
  if (String(args[0]).startsWith("/api/dashboard")) showOffline(res.headers.get("X-Offline") ? res.headers.get("X-Offline-Cached-At") : null);
  return res;
};

function showOffline(savedAt) {
  const el = document.getElementById("offline-banner");
  if (!savedAt) {
    el.hidden = true;
    return;
  }
  const when = new Date(savedAt).toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" });
  el.innerHTML = `<b>! Offline.</b> Can't reach your Mac, so this is your data as of <b>${when}</b>. The coach, sync and saving changes need the Mac to be on and running the app.`;
  el.hidden = false;
}

const KM_PER_MI = 1.609344;
const TABS = ["overview", "plan", "workouts", "form", "training", "records", "activities"];
const state = { data: null, unit: "km", weeks: 26, tab: "overview", charts: {} };
try {
  state.unit = localStorage.getItem("unit") || "km";
  state.weeks = Number(localStorage.getItem("weeks")) || 26;
  state.tab = localStorage.getItem("tab") || "overview";
} catch {}
if (TABS.includes(location.hash.slice(1))) state.tab = location.hash.slice(1);
if (!TABS.includes(state.tab)) state.tab = "overview";

// ---------- formatting ----------
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const dist = (m) => (m == null ? null : state.unit === "km" ? m / 1000 : m / 1000 / KM_PER_MI);
const fmtDist = (m, digits = 1) => (m == null ? "–" : dist(m).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits }));
function fmtTime(s) {
  if (s == null) return "–";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const mm = String(m).padStart(h ? 2 : 1, "0"), ss = String(sec).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}
// Pace is always shown in minutes per mile, whatever unit distances use.
function fmtPace(mps) {
  if (!mps) return "–";
  return fmtTime((1000 * KM_PER_MI) / mps);
}
const PACE_UNIT = "/mi";
const unitLabel = () => state.unit;
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
function fmtDate(d) {
  // Date-only strings would be parsed as UTC midnight (the previous day in the Americas); read them as local.
  const iso = /^\d{4}-\d{2}-\d{2}$/.test(d) ? `${d}T00:00` : d.replace(" ", "T");
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}
function parseHms(str) {
  const parts = str.trim().split(":").map(Number);
  if (!parts.length || parts.some(isNaN)) return null;
  return parts.reduce((acc, p) => acc * 60 + p, 0);
}

// ---------- weather ----------
// Heat-adjusted numbers remove the cost of temperature + dew point (see weather.py).
try { state.heatAdj = localStorage.getItem("heatAdj") !== "0"; } catch { state.heatAdj = true; }
const wxShort = (w) => (w ? `${Math.round(w.temp_f)}°F${w.dew_point_f != null ? ` · dew ${Math.round(w.dew_point_f)}` : ""}` : "");
const heatText = (w) => (w && w.heat_cost_pct ? `heat cost ${w.heat_cost_pct.toFixed(1)}%` : w && w.heat_cost_pct === 0 ? "no heat cost" : "");
function heatToggle(id) {
  return `<div class="segmented heat-toggle" role="group" aria-label="Heat adjustment" data-heat-toggle="${id}">
    <button type="button" data-heat="0" aria-pressed="${!state.heatAdj}">Raw</button><button type="button" data-heat="1" aria-pressed="${state.heatAdj}">Heat-adjusted</button></div>`;
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-heat]");
  if (!b) return;
  state.heatAdj = b.dataset.heat === "1";
  try { localStorage.setItem("heatAdj", state.heatAdj ? "1" : "0"); } catch {}
  document.querySelectorAll("[data-heat]").forEach((x) => x.setAttribute("aria-pressed", String((x.dataset.heat === "1") === state.heatAdj)));
  // Redraw from the data already loaded; nothing needs fetching again.
  if (state.tab === "training") renderCharts();
  if (state.tab === "workouts" && woData) { renderWorkoutTrend(); renderWorkoutDetail(); }
});

// ---------- icons & kinds ----------
const ICONS = {
  pulse: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  zap: '<path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>',
  flag: '<path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><path d="M4 22v-7"/>',
  route: '<circle cx="6" cy="19" r="2.5"/><circle cx="18" cy="5" r="2.5"/><path d="M8.5 19H17a3.5 3.5 0 0 0 0-7H7a3.5 3.5 0 0 1 0-7h8.5"/>',
  heart: '<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21.2l8.8-8.8a5.5 5.5 0 0 0 0-7.8z"/>',
  trend: '<path d="m23 6-9.5 9.5-5-5L1 18"/><path d="M17 6h6v6"/>',
  calendar: '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
  award: '<circle cx="12" cy="8" r="7"/><path d="M8.2 13.9 7 23l5-3 5 3-1.2-9.1"/>',
  clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  chat: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
  gauge: '<path d="M12 14 15.5 10.5"/><path d="M3.3 17a9 9 0 1 1 17.4 0"/>',
  wind: '<path d="M9.6 4.6A2 2 0 1 1 11 8H2M12.6 19.4A2 2 0 1 0 14 16H2M17.5 8a2.5 2.5 0 1 1 2 4H2"/>',
  shoe: '<path d="M3 17h17a2 2 0 0 0 2-2v-.5a2.5 2.5 0 0 0-2-2.45L15 11l-3-4H9v2H6V6H3z"/><path d="M3 17v2h19v-2M11.5 9l-1.5 1.5M13.5 11.5 12 13"/>',
  leaf: '<path d="M11 20A7 7 0 0 1 9.8 6.1C15.5 5 17 4.5 19 2c1 2 2 4.2 2 8 0 5.5-4.8 10-10 10Z"/><path d="M2 21c0-3 1.9-5.4 5.2-6"/>',
};
const icon = (name) =>
  `<span class="icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${ICONS[name] || ""}</svg></span>`;
function hydrateIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((el) => (el.outerHTML = icon(el.dataset.icon)));
}
hydrateIcons();
document.querySelectorAll("[data-heat-slot]").forEach((el, i) => (el.outerHTML = heatToggle(i)));

// Color = intensity: the same hues mark kinds of run and HR zones.
const KINDS = {
  easy: { label: "Easy", color: "--c-easy" },
  long: { label: "Long", color: "--c-long" },
  threshold: { label: "Threshold", color: "--c-threshold" },
  intervals: { label: "Intervals", color: "--c-intervals" },
  race: { label: "Race", color: "--c-race" },
  cross: { label: "Cross-training", color: "--c-cross" },
  rest: { label: "Rest", color: "--c-rest" },
};
const RUN_KINDS = ["easy", "long", "threshold", "intervals", "race"];
const kindChip = (k) => `<span class="kind k-${KINDS[k] ? k : "easy"}">${(KINDS[k] || KINDS.easy).label}</span>`;
const kindLegend = (kinds) => kinds.map((k) => `<span><i class="swatch" style="background:${css(KINDS[k].color)}"></i>${KINDS[k].label}</span>`).join("");

// ---------- data ----------
// GET some JSON; throws when the app can't be reached or answers with an error.
async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url}: ${res.status}`);
  return res.json();
}

async function load() {
  const first = !state.data;
  try {
    state.data = await getJSON(`/api/dashboard?weeks=${state.weeks}`);
  } catch {
    // Keep showing what's already on screen; with nothing to show, say why the page is empty.
    if (first) {
      const el = document.getElementById("offline-banner");
      el.innerHTML = `<b>! Couldn't load your data.</b> Is your Mac on and running the app? <button type="button" class="link-btn" onclick="location.reload()">Try again</button>`;
      el.hidden = false;
    }
    return;
  }
  render();
  if (!first && !planEditing) loadPlan(); // new data can change what was done on each planned day
}

function render() {
  const d = state.data;
  document.getElementById("demo-banner").hidden = !d.demo;
  document.getElementById("logout-btn").hidden = !d.auth;
  document.querySelectorAll("#units button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.unit === state.unit));
  document.querySelectorAll(".range button").forEach((b) => b.setAttribute("aria-pressed", Number(b.dataset.weeks) === state.weeks));
  document.querySelectorAll(".unit-name").forEach((el) => (el.textContent = unitLabel()));
  const o = d.overview;
  document.getElementById("data-range").textContent = o.activity_count
    ? `${o.activity_count.toLocaleString()} activities (${o.run_count.toLocaleString()} runs) since ${fmtDate(o.first_activity)}`
    : "No activities yet. Click Sync Garmin to download your history.";
  if (o.last_sync && !syncPolling) setSyncStatus(`Last sync ${fmtDate(o.last_sync.finished_at.replace(" ", "T") + "Z")}: ${o.last_sync.message}`);
  renderTiles(o);
  fillZoneForm();
  renderGoals(d.goals);
  renderRecords(d.records);
  renderPredictions(d.predictions);
  renderActivities(d.activities);
  renderCharts();
  if (planLoaded && !planEditing) renderPlan(); // distances there follow the unit setting
}

// Charts can only measure themselves when visible, so draw just the active tab's.
function renderCharts() {
  const d = state.data;
  if (!d) return;
  if (state.tab === "overview") renderWeekly(d.weekly, d.goals);
  if (state.tab === "training") {
    renderZones(d.weekly);
    renderEfficiency(d.efficiency);
    renderMonthly(d.monthly);
  }
  if (state.tab === "workouts") loadWorkouts();
  if (state.tab === "form") loadForm();
  if (state.tab === "records") loadRaces();
}

function showTab(tab) {
  state.tab = tab;
  try { localStorage.setItem("tab", tab); } catch {}
  history.replaceState(null, "", `#${tab}`);
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === tab));
  document.querySelectorAll(".panel").forEach((p) => (p.hidden = p.dataset.panel !== tab));
  document.getElementById("view-title").textContent = { overview: "Today", plan: "Plan", workouts: "Workouts", form: "Form", training: "Training", records: "Records", activities: "Activities" }[tab];
  renderCharts();
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
showTab(state.tab);

// ---------- tiles ----------
function renderTiles(o) {
  const ratio = o.load.ratio;
  let load = { cls: "info", text: "Not enough data" };
  if (ratio != null) {
    load = ratio > 1.5 ? { cls: "bad", text: "✕ Sharp jump" } : ratio > 1.3 ? { cls: "warn", text: "! Building fast" }
      : ratio < 0.8 ? { cls: "info", text: "↓ Below usual" } : { cls: "good", text: "✓ Steady build" };
  }
  const easy = o.zones_28d.easy_share;
  const easyGoal = (state.data.goals.find((g) => g.kind === "easy_hr_share") || {}).target || 0.8;
  const easyPill = easy == null ? "" : easy >= easyGoal ? `<span class="pill good">✓ On target</span>` : `<span class="pill warn">! Under ${Math.round(easyGoal * 100)}%</span>`;
  const diff = o.this_week_m - o.prev_4wk_avg_m;
  const weekPill = o.prev_4wk_avg_m ? `<span class="pill info">${diff >= 0 ? "▲" : "▼"} ${fmtDist(Math.abs(diff))} ${unitLabel()} vs avg</span>` : "";
  const tiles = [
    { acc: "acc-train", icon: "route", label: "This week", value: `${fmtDist(o.this_week_m)}<small>${unitLabel()}</small>`,
      delta: `${weekPill}<span>4-wk avg ${fmtDist(o.prev_4wk_avg_m)}</span>` },
    { acc: "acc-intensity", icon: "gauge", label: "Acute : chronic load", value: ratio == null ? "–" : ratio.toFixed(2),
      delta: `<span class="pill ${load.cls}">${load.text}</span><span>7 days vs 4-wk avg</span>` },
    { acc: "acc-body", icon: "leaf", label: "Easy running · 28 days", value: easy == null ? "–" : `${Math.round(easy * 100)}<small>%</small>`,
      delta: `${easyPill}<span>time in zones 1–2 (under ${o.hr_zones.bounds[2]} bpm)</span>` },
    { acc: "acc-goal", icon: "wind", label: "VO₂ max", value: o.vo2max ? o.vo2max.vo2max.toFixed(1) : "–",
      delta: o.vo2max ? `<span>Garmin estimate · ${fmtDate(o.vo2max.start_local)}</span>` : "<span>No estimate yet</span>" },
  ];
  document.getElementById("tiles").innerHTML = tiles
    .map((t) => `<div class="card tile ${t.acc}">
      <div class="tile-top"><span class="badge-icon">${icon(t.icon)}</span><span class="label">${t.label}</span></div>
      <div class="value">${t.value}</div><div class="delta">${t.delta}</div></div>`)
    .join("");
}

// ---------- goals ----------
function goalCard(g) {
  const title = esc(g.label) || { race: "Race goal", weekly_distance: "Weekly distance", monthly_distance: "Monthly distance", easy_hr_share: "Easy running share" }[g.kind];
  const pill = (ok, good, bad) => (ok ? `<span class="pill good">✓ ${good}</span>` : `<span class="pill bad">✕ ${bad}</span>`);
  let body = "", cls = "";
  if (g.kind === "race") {
    const raceName = { 5000: "5k", 10000: "10k", 21097.5: "Half marathon", 42195: "Marathon" }[g.distance_m] || `${fmtDist(g.distance_m)} ${unitLabel()}`;
    body = `<div class="goal-meta"><span>${raceName} in <b>${fmtTime(g.target)}</b></span>${g.days_left != null ? `<span class="days-left">${g.days_left} days to go</span>` : ""}</div>
      <div class="big">${g.current ? fmtTime(g.current) : "–"}</div>
      <div class="caption">Current projection${g.basis ? ` · based on ${esc(g.basis)}` : ""}</div>
      ${g.current ? pill(g.on_track, `${fmtTime(-g.gap_s)} to spare`, `${fmtTime(g.gap_s)} to find`) : '<span class="pill info">Needs a recent 3k+ effort</span>'}`;
  } else if (g.kind === "weekly_distance" || g.kind === "monthly_distance") {
    cls = "distance";
    const pct = Math.min(100, Math.round(g.progress * 100));
    body = `<div class="big">${fmtDist(g.current)} <small>/ ${fmtDist(g.target, 0)} ${unitLabel()}</small></div>
      <div class="meter" role="meter" aria-valuenow="${pct}" aria-valuemin="0" aria-valuemax="100"><div style="width:${pct}%"></div></div>
      <div class="caption">${pct}% this ${g.kind === "weekly_distance" ? "week" : "month"}${g.weeks_total ? ` · hit ${g.weeks_hit} of the last ${g.weeks_total} weeks` : ""}</div>`;
  } else if (g.kind === "easy_hr_share") {
    cls = "easy-share";
    body = `<div class="big">${g.current == null ? "–" : Math.round(g.current * 100) + "%"} <small>target ${Math.round(g.target * 100)}%</small></div>
      <div class="caption">Run time in zones 1–2, last 28 days</div>
      ${g.current == null ? "" : pill(g.on_track, "On target", "Too much hard running")}`;
  }
  return `<div class="goal ${cls}"><h3>${title}</h3><button class="remove" data-goal="${g.id}" aria-label="Remove goal" title="Remove goal">✕</button>${body}</div>`;
}

function renderGoals(goals) {
  const el = document.getElementById("goals");
  el.innerHTML = goals.length ? goals.map(goalCard).join("") : '<div class="muted">No goals yet. Add one below.</div>';
  el.querySelectorAll(".remove").forEach((b) =>
    b.addEventListener("click", async () => {
      await fetch(`/api/goals/${b.dataset.goal}`, { method: "DELETE" });
      load();
    })
  );
}

const form = document.getElementById("goal-form");
function syncFormFields() {
  const kind = form.kind.value;
  form.querySelectorAll("[data-for]").forEach((el) => (el.hidden = !el.dataset.for.split(" ").includes(kind)));
}
form.kind.addEventListener("change", syncFormFields);
syncFormFields();
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const kind = form.kind.value;
  const goal = { kind, label: form.label.value || null };
  if (kind === "race") {
    goal.distance_m = Number(form.race_distance.value);
    goal.target = parseHms(form.time.value);
    goal.target_date = form.date.value || null;
    if (!goal.target) return alert("Enter a target time like 1:55:00");
  } else if (kind === "easy_hr_share") {
    goal.target = Number(form.share.value) / 100;
  } else {
    const v = Number(form.distance.value);
    if (!v) return alert("Enter a distance");
    goal.target = v * 1000 * (state.unit === "mi" ? KM_PER_MI : 1);
  }
  const res = await fetch("/api/goals", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(goal) });
  if (!res.ok) return alert("Could not save goal");
  form.reset();
  syncFormFields();
  load();
});

// ---------- charts ----------
function baseOptions(extra = {}) {
  const grid = css("--grid"), muted = css("--muted");
  Chart.defaults.font.family = css("--font-ui");
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: css("--surface"),
        titleColor: css("--text-primary"),
        bodyColor: css("--text-secondary"),
        borderColor: css("--axis"),
        borderWidth: 1,
        padding: 10,
        boxPadding: 4,
        usePointStyle: true,
        titleFont: { family: css("--font-display"), size: 13, weight: "600" },
      },
    },
    scales: {
      x: { grid: { display: false }, border: { color: css("--axis") }, ticks: { color: muted, maxRotation: 0, autoSkipPadding: 12 } },
      y: { beginAtZero: true, grid: { color: grid }, border: { display: false }, ticks: { color: muted, precision: 0 } },
    },
    ...extra,
  };
}

function draw(id, config) {
  state.charts[id]?.destroy();
  state.charts[id] = new Chart(document.getElementById(id), config);
}

const weekLabel = (w) => new Date(w + "T00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });

function renderWeekly(weeks, goals) {
  const goal = goals.find((g) => g.kind === "weekly_distance");
  const surface = css("--surface");
  const datasets = RUN_KINDS.map((k) => ({
    type: "bar",
    label: KINDS[k].label,
    data: weeks.map((w) => +dist(w.by_kind_m[k] || 0).toFixed(1)),
    backgroundColor: css(KINDS[k].color),
    borderColor: surface,
    borderWidth: { top: 2 },
    borderSkipped: "start",
    maxBarThickness: 24,
    stack: "km",
    order: 2,
  }));
  let legend = kindLegend(RUN_KINDS);
  if (goal) {
    datasets.push({
      type: "line",
      label: "Weekly goal",
      data: weeks.map(() => +dist(goal.target).toFixed(1)),
      borderColor: css("--text-primary"),
      borderWidth: 2,
      borderDash: [6, 4],
      pointRadius: 0,
      order: 1,
    });
    legend += `<span><i class="line-key" style="background:${css("--text-primary")}"></i>Weekly goal</span>`;
  }
  document.getElementById("weekly-legend").innerHTML = legend;
  draw("weekly-chart", {
    data: { labels: weeks.map((w) => weekLabel(w.week)), datasets },
    options: baseOptions({
      scales: {
        ...baseOptions().scales,
        x: { ...baseOptions().scales.x, stacked: true },
        y: { ...baseOptions().scales.y, stacked: true, ticks: { color: css("--muted"), callback: (v) => `${v} ${unitLabel()}` } },
      },
      plugins: {
        ...baseOptions().plugins,
        tooltip: {
          ...baseOptions().plugins.tooltip,
          filter: (item) => item.raw > 0,
          itemSort: (a, b) => b.datasetIndex - a.datasetIndex,
          callbacks: {
            title: (items) => `Week of ${items[0].label}`,
            label: (item) => ` ${item.dataset.label}: ${item.raw} ${unitLabel()}`,
            afterBody: (items) => {
              const w = weeks[items[0].dataIndex];
              return [`Total ${fmtDist(w.distance_m)} ${unitLabel()} · ${w.runs} runs · ${fmtTime(w.duration_s)}`];
            },
          },
        },
      },
    }),
  });
}

const ZONES = ["--z1", "--z2", "--z3", "--z4", "--z5"];
function renderZones(weeks) {
  document.getElementById("zone-legend").innerHTML = ZONES.map(
    (z, i) => `<span><i class="swatch" style="background:${css(z)}"></i>Zone ${i + 1} · ${state.data.overview.hr_zones.ranges[i]} bpm${i < 2 ? " · easy" : i === 2 ? " · sub-threshold" : ""}</span>`
  ).join("");
  const surface = css("--surface");
  draw("zone-chart", {
    type: "bar",
    data: {
      labels: weeks.map((w) => weekLabel(w.week)),
      datasets: ZONES.map((z, i) => ({
        label: `Zone ${i + 1}`,
        data: weeks.map((w) => +(w.zones_s[i] / 3600).toFixed(2)),
        backgroundColor: css(z),
        borderColor: surface,
        borderWidth: { top: 2 },
        borderSkipped: "start",
        maxBarThickness: 24,
      })),
    },
    options: baseOptions({
      scales: {
        ...baseOptions().scales,
        x: { ...baseOptions().scales.x, stacked: true },
        y: { ...baseOptions().scales.y, stacked: true, ticks: { color: css("--muted"), callback: (v) => `${v} h` } },
      },
      plugins: {
        ...baseOptions().plugins,
        tooltip: {
          ...baseOptions().plugins.tooltip,
          itemSort: (a, b) => b.datasetIndex - a.datasetIndex,
          callbacks: {
            title: (items) => `Week of ${items[0].label}`,
            label: (item) => ` ${item.dataset.label}: ${fmtTime(item.raw * 3600)}`,
          },
        },
      },
    }),
  });
}

function renderEfficiency(points) {
  const adj = state.heatAdj;
  const val = (p) => (adj ? p.ef_adj : p.ef);
  // 28-day rolling mean of easy runs only, so hard sessions don't swing the trend.
  const easy = points.filter((p) => p.easy);
  const rolling = (f) => easy.map((p) => {
    const t = new Date(p.date).getTime();
    const win = easy.filter((q) => { const dt = t - new Date(q.date).getTime(); return dt >= 0 && dt < 28 * 864e5; });
    return { x: p.date, y: +(win.reduce((s, q) => s + f(q), 0) / win.length).toFixed(3) };
  });
  const shown = RUN_KINDS.filter((k) => points.some((p) => p.kind === k));
  document.getElementById("ef-legend").innerHTML = shown
    .map((k) => `<span><i class="swatch" style="background:${css(KINDS[k].color)};border-radius:50%"></i>${KINDS[k].label}</span>`)
    .join("") +
    `<span><i class="line-key" style="background:${css("--text-primary")}"></i>Easy-run trend${adj ? ", heat-adjusted" : ""}</span>` +
    (adj ? `<span><i class="line-key" style="background:${css("--muted")}"></i>Raw trend</span>` : "");
  const datasets = [
    { label: "Easy-run trend", data: rolling(val), borderColor: css("--text-primary"), borderWidth: 2, pointRadius: 0, tension: 0.3, order: 1 },
    {
      label: "Run",
      data: points.map((p) => ({ x: p.date, y: +val(p).toFixed(3), p })),
      showLine: false, pointRadius: 4, pointHoverRadius: 6, order: 2,
      pointBackgroundColor: points.map((p) => css((KINDS[p.kind] || KINDS.easy).color)),
      pointBorderColor: css("--surface"), pointBorderWidth: 2,
    },
  ];
  if (adj) datasets.splice(1, 0, { label: "Raw trend", data: rolling((p) => p.ef), borderColor: css("--muted"), borderWidth: 1.5, borderDash: [5, 4], pointRadius: 0, tension: 0.3, order: 1 });
  draw("ef-chart", {
    type: "line",
    data: { datasets },
    options: baseOptions({
      interaction: { mode: "nearest", intersect: false },
      scales: {
        x: { type: "category", labels: [...new Set(points.map((p) => p.date))], grid: { display: false }, border: { color: css("--axis") },
             ticks: { color: css("--muted"), maxRotation: 0, autoSkipPadding: 24, callback(v) { return weekLabel(this.getLabelForValue(v)); } } },
        y: { beginAtZero: false, grid: { color: css("--grid") }, border: { display: false }, ticks: { color: css("--muted") } },
      },
      plugins: {
        ...baseOptions().plugins,
        tooltip: {
          ...baseOptions().plugins.tooltip,
          callbacks: {
            title: (items) => fmtDate(items[0].raw.x),
            label: (item) => {
              const p = item.raw.p;
              if (!p) return ` ${item.dataset.label}: ${item.raw.y}`;
              const lines = [` ${p.name} · ${(KINDS[p.kind] || KINDS.easy).label}`,
                ` Efficiency ${p.ef.toFixed(2)}${p.ef_adj !== p.ef ? ` → ${p.ef_adj.toFixed(2)} heat-adjusted` : ""}`,
                ` ${fmtPace(1000 / p.pace_s_per_km)} ${PACE_UNIT} at ${Math.round(p.avg_hr)} bpm`];
              if (p.weather) lines.push(` ${wxShort(p.weather)}${heatText(p.weather) ? ` · ${heatText(p.weather)}` : ""}`);
              else if (p.indoor) lines.push(" Indoors");
              return lines;
            },
          },
        },
      },
    }),
  });
}

function renderMonthly(months) {
  draw("monthly-chart", {
    type: "bar",
    data: {
      labels: months.map((m) => new Date(m.month + "-01T00:00").toLocaleDateString(undefined, { month: "short", year: "2-digit" })),
      datasets: [
        {
          label: `Distance (${unitLabel()})`,
          data: months.map((m) => +dist(m.distance_m).toFixed(1)),
          backgroundColor: css("--c-long"),
          borderRadius: { topLeft: 4, topRight: 4 },
          borderSkipped: "start",
          maxBarThickness: 24,
        },
      ],
    },
    options: baseOptions({
      plugins: {
        ...baseOptions().plugins,
        tooltip: {
          ...baseOptions().plugins.tooltip,
          callbacks: { afterBody: (items) => `${months[items[0].dataIndex].runs} runs · ${fmtTime(months[items[0].dataIndex].duration_s)}` },
        },
      },
    }),
  });
}

// ---------- tables ----------
function renderRecords(records) {
  const paceOf = (r, d) => (r ? `${fmtPace(d / r.time_s)}` : "–");
  document.getElementById("records").innerHTML = `
    <thead><tr><th>Distance</th><th>All-time best</th><th>Pace ${PACE_UNIT}</th><th>Date</th><th>Last 90 days</th></tr></thead>
    <tbody>${records
      .map((r) => {
        const fresh = r.recent && r.all_time && r.recent.activity_id === r.all_time.activity_id;
        return `<tr><td class="race-label">${r.label}</td>
        <td>${r.all_time ? `<span class="strong" title="${esc(r.all_time.name)}">${fmtTime(r.all_time.time_s)}</span>` : "–"}</td>
        <td>${paceOf(r.all_time, r.distance_m)}</td>
        <td>${r.all_time ? dayOf(r.all_time.date).toLocaleDateString(undefined, { month: "short", year: "2-digit" }) : "–"}</td>
        <td>${fresh ? `<span class="pill warn">★ New PR</span>` : r.recent ? fmtTime(r.recent.time_s) : "–"}</td></tr>`;
      })
      .join("")}</tbody>`;
}

function renderPredictions(preds) {
  const cell = (p) => (p ? `<span class="strong">${fmtTime(p.time_s)}</span>` : "–");
  const basis = preds.find((p) => p.from_race)?.from_race.from;
  document.getElementById("pred-sub").textContent = basis
    ? `From races: based on ${basis}, converted to cool-weather conditions where heat slowed you. From training: best efforts in the last 90 days.`
    : "From your best training efforts in the last 90 days, next to Garmin's predictor.";
  document.getElementById("predictions").innerHTML = `
    <thead><tr><th>Race</th><th>From races</th><th>Pace</th><th>Training</th><th>Garmin</th></tr></thead>
    <tbody>${preds
      .map((p) => `<tr><td class="race-label">${p.label}</td><td>${cell(p.from_race)}</td>
        <td>${p.from_race ? `${fmtPace(p.distance_m / p.from_race.time_s)}/mi` : "–"}</td>
        <td>${p.riegel ? fmtTime(p.riegel.time_s) : "–"}</td><td>${fmtTime(p.garmin_s)}</td></tr>`)
      .join("")}</tbody>`;
}

function renderActivities(acts) {
  const el = document.getElementById("activities");
  document.getElementById("kind-legend").innerHTML = acts.length ? kindLegend([...RUN_KINDS, "cross"]) : "";
  if (!acts.length) {
    el.innerHTML = '<tbody><tr><td class="empty">No activities yet</td></tr></tbody>';
    return;
  }
  el.innerHTML = `
    <thead><tr><th>Activity</th><th>Kind</th><th>Date</th><th>Distance (${unitLabel()})</th><th>Time</th><th>Pace ${PACE_UNIT}</th><th>Avg HR</th><th>Weather</th><th>Load</th></tr></thead>
    <tbody>${acts
      .map((a) => `<tr><td class="name"><a href="https://connect.garmin.com/modern/activity/${a.id}" target="_blank" rel="noopener">${esc(a.name) || "Untitled"}</a></td>
        <td class="kind-cell">${a.kind === "cross" ? `<span class="kind k-cross">${esc((a.type || "Other").replace(/_/g, " "))}</span>` : kindChip(a.kind)}</td>
        <td>${fmtDate(a.start_local)}</td>
        <td>${a.distance_m ? fmtDist(a.distance_m, 2) : "–"}</td><td>${fmtTime(a.duration_s)}</td>
        <td>${a.is_run ? fmtPace(a.avg_speed_mps) : "–"}</td><td>${a.avg_hr ? Math.round(a.avg_hr) : "–"}</td>
        <td title="${esc(heatText(a.weather))}">${a.weather ? `${wxShort(a.weather)}${a.weather.heat_cost_pct >= 1 ? ` <span class="pill warn">+${a.weather.heat_cost_pct.toFixed(0)}%</span>` : ""}` : "–"}</td>
        <td>${a.training_load != null ? Math.round(a.training_load) : "–"}</td></tr>`)
      .join("")}</tbody>`;
}

// ---------- sync ----------
let syncPolling = false;
const setSyncStatus = (msg) => (document.getElementById("sync-status").textContent = msg || "");
const syncBtn = document.getElementById("sync-btn");

async function pollSync() {
  syncPolling = true;
  syncBtn.disabled = true;
  try {
    while (true) {
      const s = await getJSON("/api/sync");
      setSyncStatus(s.message);
      if (!s.running) break;
      await new Promise((r) => setTimeout(r, 1500));
    }
  } catch {
    setSyncStatus("Lost contact with the app while it was syncing.");
    return;
  } finally {
    syncPolling = false;
    syncBtn.disabled = false;
  }
  load();
  refreshStatus(false);
}

syncBtn.addEventListener("click", async () => {
  try {
    const s = await (await fetch("/api/sync", { method: "POST" })).json();
    setSyncStatus(s.message);
    if (s.running) pollSync();
  } catch {
    setSyncStatus("Couldn't reach the app server.");
  }
});

// ---------- controls ----------
document.querySelectorAll("#units button").forEach((b) =>
  b.addEventListener("click", () => {
    state.unit = b.dataset.unit;
    try { localStorage.setItem("unit", state.unit); } catch {}
    render();
  })
);
document.querySelectorAll(".range button").forEach((b) =>
  b.addEventListener("click", () => {
    state.weeks = Number(b.dataset.weeks);
    try { localStorage.setItem("weeks", state.weeks); } catch {}
    load();
  })
);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);

load();
getJSON("/api/sync").then((s) => s.running && pollSync()).catch(() => {}); // offline: nothing to resume

// ---------- coach ----------
const coachLog = document.getElementById("coach-log");
const coachForm = document.getElementById("coach-form");
const coachSuggestions = document.getElementById("coach-suggestions");
const coachEmpty = document.getElementById("coach-empty");

function renderMarkdown(text) {
  return DOMPurify.sanitize(marked.parse(text, { breaks: true }));
}

function addMessage(role, content) {
  const el = document.createElement("div");
  el.className = `msg msg-${role}`;
  if (role === "coach") el.innerHTML = renderMarkdown(content);
  else el.textContent = content;
  coachLog.appendChild(el);
  coachLog.scrollTop = coachLog.scrollHeight;
  return el;
}

async function loadCoach() {
  let messages;
  try {
    ({ messages } = await getJSON("/api/coach"));
  } catch {
    return;
  }
  coachLog.innerHTML = "";
  messages.forEach((m) => addMessage(m.role, m.content));
  renderProposals();
  coachEmpty.hidden = messages.length > 0;
}

async function sendToCoach(message) {
  const button = coachForm.querySelector("button");
  button.disabled = true;
  coachEmpty.hidden = true;
  addMessage("user", message);
  const started = Date.now();
  const thinking = addMessage("thinking", "Looking at your training…");
  const timer = setInterval(() => (thinking.textContent = `Looking at your training… ${Math.round((Date.now() - started) / 1000)}s`), 1000);
  try {
    const res = await fetch("/api/coach", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, units: state.unit }),
    });
    const body = await res.json();
    thinking.remove();
    if (res.ok) {
      addMessage("coach", body.reply);
      loadProposals(); // the coach may have proposed plan changes
    }
    else addMessage("error", body.detail || "The coach couldn't answer.");
  } catch {
    thinking.remove();
    addMessage("error", "Couldn't reach the app server.");
  } finally {
    clearInterval(timer);
    button.disabled = false;
  }
}

coachForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const message = coachForm.message.value.trim();
  if (!message || coachForm.querySelector("button").disabled) return;
  coachForm.message.value = "";
  sendToCoach(message);
});
coachForm.message.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    coachForm.requestSubmit();
  }
});
coachSuggestions.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => sendToCoach(b.textContent)));
document.getElementById("coach-reset").addEventListener("click", async () => {
  if (!confirm("Start a new conversation? The current one will be cleared.")) return;
  await fetch("/api/coach", { method: "DELETE" });
  loadCoach();
});

loadCoach();

// ---------- coach notes ----------
const notesText = document.getElementById("notes-text");
const notesPanel = document.getElementById("coach-notes");
const notesToggle = document.getElementById("notes-toggle");
const notesSaved = document.getElementById("notes-saved");

async function loadNotes() {
  let notes;
  try {
    ({ notes } = await getJSON("/api/coach/notes"));
  } catch {
    return;
  }
  notesText.value = notes;
  // First visit: open the notes so they get filled in.
  if (!notes) setNotesOpen(true);
}

document.getElementById("notes-save").addEventListener("click", async () => {
  const res = await fetch("/api/coach/notes", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ notes: notesText.value }),
  });
  notesSaved.textContent = res.ok ? "Saved. The coach will use this from the next message." : "Couldn't save notes.";
  if (res.ok) setTimeout(() => setNotesOpen(false), 900);
});

function setNotesOpen(open) {
  notesPanel.hidden = !open;
  notesToggle.setAttribute("aria-expanded", open);
  if (open) notesSaved.textContent = "";
}
notesToggle.addEventListener("click", () => setNotesOpen(notesPanel.hidden));

loadNotes();

// ---------- coach on narrow screens ----------
const coachPanel = document.getElementById("coach");
const coachOpen = document.getElementById("coach-open");
function setCoachOpen(open) {
  coachPanel.classList.toggle("open", open);
  coachOpen.hidden = open;
  if (open) {
    coachLog.scrollTop = coachLog.scrollHeight;
    coachForm.message.focus();
  }
}
coachOpen.addEventListener("click", () => setCoachOpen(true));
document.getElementById("coach-close").addEventListener("click", () => setCoachOpen(false));
document.addEventListener("keydown", (e) => e.key === "Escape" && coachPanel.classList.contains("open") && setCoachOpen(false));


// ---------- training plan ----------
const planEl = {
  table: document.getElementById("plan-table"),
  notes: document.getElementById("plan-notes"),
  summary: document.getElementById("plan-summary"),
  actions: document.getElementById("plan-actions"),
  saved: document.getElementById("plan-saved"),
};
let planData = { notes: "", sessions: [] };
let planLoaded = false;
let planEditing = false;

const isoToday = () => new Date().toLocaleDateString("en-CA"); // YYYY-MM-DD in local time
const dayOf = (iso) => new Date(`${iso}T00:00`);
const mondayOf = (iso) => {
  const d = dayOf(iso);
  d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
  return d.toLocaleDateString("en-CA");
};
const addDays = (iso, n) => {
  const d = dayOf(iso);
  d.setDate(d.getDate() + n);
  return d.toLocaleDateString("en-CA");
};
const shortDate = (iso) => dayOf(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });

const STATE_PILL = {
  done: '<span class="pill good">✓ Done</span>',
  rested: '<span class="pill good">✓ Rested</span>',
  harder: '<span class="pill warn">! Harder than planned</span>',
  ran: '<span class="pill warn">! Ran on a rest day</span>',
  missed: '<span class="pill info">– Not done</span>',
  today: '<span class="pill info" style="color:var(--text-primary)">● Today</span>',
  upcoming: "",
};

function actualCell(s) {
  const acts = (s.actual || [])
    .map((a) => `<div class="act">${a.kind === "cross" ? '<span class="kind k-cross">Cross</span>' : kindChip(a.kind)}<span>${a.distance_m ? `${fmtDist(a.distance_m)} ${unitLabel()}` : ""}${a.duration_s ? ` · ${fmtTime(a.duration_s)}` : ""}</span></div>`)
    .join("");
  return `<div class="actual-list">${STATE_PILL[s.state] || ""}${acts}</div>`;
}

function renderPlanView() {
  const sessions = planData.sessions;
  const today = isoToday();
  planEl.actions.innerHTML = `<button type="button" id="plan-edit">${sessions.length ? "Edit plan" : "Create plan"}</button>`;
  document.getElementById("plan-edit").addEventListener("click", () => { planEditing = true; renderPlan(); });

  if (!sessions.length) {
    planEl.summary.innerHTML = "";
    planEl.table.innerHTML = `<div class="plan-empty">No sessions planned yet. Click <b>Create plan</b>, or ask the coach to suggest one and enter it here.</div>`;
  } else {
    // Summary: this week's progress and the next race.
    const thisWeek = sessions.filter((s) => mondayOf(s.date) === mondayOf(today) && s.kind !== "rest");
    const doneThisWeek = thisWeek.filter((s) => s.state === "done" || s.state === "harder").length;
    const nextRace = sessions.find((s) => s.kind === "race" && s.date >= today);
    const daysTo = nextRace ? Math.round((dayOf(nextRace.date) - dayOf(today)) / 864e5) : null;
    planEl.summary.innerHTML = [
      thisWeek.length ? `<span class="metric" style="--m:${css("--c-easy")}">This week <b>${doneThisWeek}/${thisWeek.length}</b> sessions done</span>` : "",
      nextRace ? `<span class="metric" style="--m:${css("--c-race")}">Next race <b>${esc(nextRace.title)}</b> ${daysTo === 0 ? "today" : `in ${daysTo} day${daysTo === 1 ? "" : "s"}`}</span>` : "",
    ].join("");

    const weeks = {};
    sessions.forEach((s) => (weeks[mondayOf(s.date)] ||= []).push(s));
    planEl.table.innerHTML = Object.entries(weeks)
      .map(([monday, list]) => {
        const quality = list.filter((s) => ["threshold", "intervals", "race"].includes(s.kind)).length;
        const current = monday === mondayOf(today);
        const rows = list
          .map((s) => {
            const cls = [s.date < today ? "past" : "", s.date === today ? "today" : "", s.kind === "race" ? "race-day" : ""].join(" ");
            return `<tr class="${cls}">
              <td class="day"><b>${dayOf(s.date).toLocaleDateString(undefined, { weekday: "short" })}</b><span>${shortDate(s.date)}</span></td>
              <td class="type">${kindChip(s.kind)}</td>
              <td><div class="w-title">${esc(s.title)}</div>${s.details ? `<div class="w-details">${esc(s.details)}</div>` : ""}</td>
              <td class="actual">${actualCell(s)}</td></tr>`;
          })
          .join("");
        return `<div class="plan-week${current ? " current" : ""}">
          <div class="plan-week-head"><h3>Week of ${shortDate(monday)}</h3>
            <span class="muted">${list.filter((s) => s.kind !== "rest").length} sessions · ${quality} quality${current ? " · this week" : ""}</span></div>
          <div class="table-wrap"><table class="plan-table">
            <thead><tr><th>Day</th><th>Type</th><th>Workout</th><th>What you did</th></tr></thead>
            <tbody>${rows}</tbody></table></div></div>`;
      })
      .join("");
  }
  planEl.notes.innerHTML = planData.notes
    ? renderMarkdown(planData.notes)
    : `<div class="plan-empty">No notes yet. Races, training zones and rules go here.</div>`;
}

const KIND_OPTIONS = ["easy", "long", "threshold", "intervals", "race", "rest", "cross"];
function editRow(s = {}) {
  return `<tr>
    <td><input type="date" name="date" value="${esc(s.date || "")}" required aria-label="Date"></td>
    <td><select name="kind" aria-label="Type">${KIND_OPTIONS.map((k) => `<option value="${k}"${k === (s.kind || "easy") ? " selected" : ""}>${KINDS[k].label}</option>`).join("")}</select></td>
    <td><input name="title" value="${esc(s.title || "")}" placeholder="e.g. 5×6 min sub-threshold, HR ≤160" aria-label="Workout"></td>
    <td><textarea name="details" rows="1" placeholder="Warm-up, paces, notes" aria-label="Details">${esc(s.details || "")}</textarea></td>
    <td class="del"><button type="button" title="Remove session" aria-label="Remove session">✕</button></td></tr>`;
}

function renderPlanEdit() {
  planEl.summary.innerHTML = "";
  planEl.actions.innerHTML = `<button type="button" id="plan-add">+ Add session</button><button type="button" id="plan-cancel">Cancel</button><button type="button" class="primary" id="plan-save">Save plan</button>`;
  planEl.table.innerHTML = `<div class="table-wrap"><table class="plan-table" id="plan-edit-table">
    <thead><tr><th>Date</th><th>Type</th><th>Workout</th><th>Details</th><th></th></tr></thead>
    <tbody>${planData.sessions.map(editRow).join("")}</tbody></table></div>`;
  planEl.notes.innerHTML = `<textarea id="plan-notes-text" aria-label="Plan notes" placeholder="Races, zones, rules…">${esc(planData.notes)}</textarea>`;
  const tbody = planEl.table.querySelector("tbody");
  tbody.addEventListener("click", (e) => e.target.closest(".del button") && e.target.closest("tr").remove());
  document.getElementById("plan-add").addEventListener("click", () => {
    const dates = [...tbody.querySelectorAll('input[name="date"]')].map((i) => i.value).filter(Boolean).sort();
    tbody.insertAdjacentHTML("beforeend", editRow({ date: dates.length ? addDays(dates.at(-1), 1) : isoToday() }));
    tbody.lastElementChild.querySelector('input[name="title"]').focus();
  });
  document.getElementById("plan-cancel").addEventListener("click", () => { planEditing = false; renderPlan(); });
  document.getElementById("plan-save").addEventListener("click", savePlan);
}

async function savePlan() {
  const rows = [...planEl.table.querySelectorAll("tbody tr")].map((tr) => ({
    date: tr.querySelector('[name="date"]').value,
    kind: tr.querySelector('[name="kind"]').value,
    title: tr.querySelector('[name="title"]').value.trim(),
    details: tr.querySelector('[name="details"]').value.trim(),
  }));
  const incomplete = rows.filter((r) => !r.date || !r.title);
  if (incomplete.length && !confirm(`${incomplete.length} row(s) are missing a date or workout and will be dropped. Save anyway?`)) return;
  const res = await fetch("/api/plan", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ notes: document.getElementById("plan-notes-text").value, sessions: rows.filter((r) => r.date && r.title) }),
  });
  if (!res.ok) {
    planEl.saved.textContent = "Couldn't save the plan. Check the dates.";
    return;
  }
  planData = await res.json();
  planEditing = false;
  renderPlan();
  planEl.saved.textContent = "Saved. Refresh the Status card to update your next workout.";
}

function renderPlan() {
  planEl.saved.textContent = "";
  if (planEditing) renderPlanEdit();
  else renderPlanView();
}

async function loadPlan() {
  try {
    planData = await getJSON("/api/plan");
  } catch {
    return;
  }
  planLoaded = true;
  if (!planEditing) renderPlan();
}

document.getElementById("plan-toggle").addEventListener("click", () => {
  showTab("plan");
  if (coachPanel.classList.contains("open")) setCoachOpen(false);
  document.querySelector('[data-panel="plan"]').scrollIntoView({ behavior: "smooth", block: "start" });
});

loadPlan();

// ---------- status ----------
const STATUS_STALE_MS = 3 * 3600 * 1000;
const LEVELS = {
  ready: { label: "Ready to train", color: "--good", icon: "✓" },
  moderate: { label: "Train with control", color: "--warning", icon: "!", ink: "#0b0b0b" },
  low: { label: "Take it easy", color: "--critical", icon: "✕" },
};
const statusTime = document.getElementById("status-time");
const statusMetrics = document.getElementById("status-metrics");
const statusRefresh = document.getElementById("status-refresh");
let statusUpdating = false;

function timeAgo(isoLocal) {
  const mins = Math.round((Date.now() - new Date(isoLocal.replace(" ", "T")).getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  if (mins < 48 * 60) return `${Math.round(mins / 60)} h ago`;
  return `${Math.round(mins / 1440)} days ago`;
}

const METRIC_COLORS = {
  Readiness: "--c-easy", Sleep: "--c-race", "Resting HR": "--c-intervals", HRV: "--c-long",
  "Body Battery": "--c-threshold", "Garmin status": "--muted",
};
// Long AI text is clamped to a few lines on the dashboard; tap to expand.
const clamp = (text, lines, cls = "") =>
  `<p class="clamp ${cls}" style="--lines:${lines}" title="Tap to expand" tabindex="0">${esc(text)}</p>`;
function renderStatus(s) {
  statusTime.textContent = s.generated_at ? `Updated ${timeAgo(s.generated_at)}` : "";
  statusMetrics.innerHTML = s.metrics
    .map((m) => `<span class="metric" style="--m:${css(METRIC_COLORS[m.label] || "--accent")}" title="${esc(m.date)}">${esc(m.label)} <b>${esc(m.value)}</b></span>`)
    .join("");
  const bodyCard = document.getElementById("st-body-card");
  const nextCard = document.getElementById("st-next-card");
  const r = s.report;
  if (!r) {
    bodyCard.style.removeProperty("--lvl");
    document.getElementById("st-body").innerHTML = `<div class="status-empty">No status yet. Click <b>Refresh</b> to get one (about 30 seconds).</div>`;
    document.getElementById("st-next").innerHTML = `<div class="status-empty">Your next session appears here once the status is generated.</div>`;
    document.getElementById("st-last").innerHTML = "";
    document.getElementById("st-next-when").textContent = "";
    return;
  }
  const lvl = LEVELS[r.body.level] || LEVELS.moderate;
  bodyCard.style.setProperty("--lvl", css(lvl.color));
  document.getElementById("status-badge").style.setProperty("--acc", css(lvl.color));
  document.getElementById("st-body").innerHTML = `
    <span class="level"><span class="level-icon" style="background:${css(lvl.color)};color:${lvl.ink || "#fff"}" aria-hidden="true">${lvl.icon}</span>${lvl.label}</span>
    <div class="status-headline">${esc(r.body.headline)}</div>
    ${clamp(r.body.summary, 3)}`;
  const w = r.next_workout;
  const k = KINDS[w.kind] ? w.kind : "easy";
  nextCard.className = `card status-card st-next k-${k}`;
  document.getElementById("st-next-when").innerHTML = `${kindChip(k)} <span>${esc(w.when)}</span>`;
  document.getElementById("st-next").innerHTML = `
    <div class="status-headline big">${esc(w.title)}</div>
    <div class="status-details">${esc(w.details)}</div>
    ${w.adjustment ? `<div class="status-adjust"><b aria-hidden="true">↻</b>${clamp(w.adjustment, 2, "adjust-text")}</div>` : ""}`;
  document.getElementById("st-last").innerHTML = `
    <div class="status-headline">${esc(r.last_12_hours.headline)}</div>
    ${clamp(r.last_12_hours.summary, 6)}`;
}
document.querySelector(".view-today").addEventListener("click", (e) => {
  const p = e.target.closest(".clamp");
  if (p) p.classList.toggle("open");
});

async function loadStatus() {
  let s;
  try {
    s = await getJSON("/api/status");
  } catch {
    return;
  }
  renderStatus(s);
  const stale = !s.generated_at || Date.now() - new Date(s.generated_at.replace(" ", "T")).getTime() > STATUS_STALE_MS;
  if (stale && !syncPolling) refreshStatus(true);
}

async function refreshStatus(syncFirst = true) {
  if (statusUpdating) return;
  statusUpdating = true;
  statusRefresh.disabled = true;
  const started = Date.now();
  const tick = () => (statusTime.textContent = `Updating${syncFirst ? " (syncing Garmin first)" : ""}… ${Math.round((Date.now() - started) / 1000)}s`);
  tick();
  const timer = setInterval(tick, 1000);
  try {
    const res = await fetch(`/api/status?units=${state.unit}&sync_first=${syncFirst}`, { method: "POST" });
    const body = await res.json();
    if (res.ok) {
      renderStatus(body);
      if (syncFirst) await load();
      // The status was still written, from the data already saved; say why it may be out of date.
      if (body.sync_error) setSyncStatus(body.sync_error);
      // A sync that outlasted the wait is still going: follow it; the status refreshes again when it ends.
      if (syncFirst && !syncPolling) getJSON("/api/sync").then((s) => s.running && pollSync()).catch(() => {});
    } else {
      statusTime.textContent = body.detail || "Couldn't update the status.";
    }
  } catch {
    statusTime.textContent = "Couldn't reach the app server.";
  } finally {
    clearInterval(timer);
    statusUpdating = false;
    statusRefresh.disabled = false;
  }
}

statusRefresh.addEventListener("click", () => refreshStatus(true));
loadStatus();


// ---------- resizable coach panel ----------
const COACH_DEFAULT_W = 400;
const COACH_MIN_W = 300;
const MAIN_MIN_W = 520; // keep the dashboard usable
const appEl = document.querySelector(".app");
const resizer = document.getElementById("coach-resizer");

const coachMaxW = () => Math.max(COACH_MIN_W, Math.min(900, window.innerWidth - MAIN_MIN_W));
function setCoachWidth(w, save = true) {
  const width = Math.round(Math.min(coachMaxW(), Math.max(COACH_MIN_W, w)));
  appEl.style.setProperty("--coach-w", `${width}px`);
  resizer.setAttribute("aria-valuenow", width);
  resizer.setAttribute("aria-valuemin", COACH_MIN_W);
  resizer.setAttribute("aria-valuemax", coachMaxW());
  if (save) {
    try { localStorage.setItem("coachWidth", width); } catch {}
  }
  return width;
}
let savedCoachW = COACH_DEFAULT_W;
try { savedCoachW = Number(localStorage.getItem("coachWidth")) || COACH_DEFAULT_W; } catch {}
setCoachWidth(savedCoachW, false);

resizer.addEventListener("pointerdown", (e) => {
  e.preventDefault();
  resizer.setPointerCapture(e.pointerId);
  document.body.classList.add("resizing");
  const move = (ev) => setCoachWidth(window.innerWidth - ev.clientX, false);
  const up = (ev) => {
    resizer.releasePointerCapture(ev.pointerId);
    resizer.removeEventListener("pointermove", move);
    resizer.removeEventListener("pointerup", up);
    resizer.removeEventListener("pointercancel", up);
    document.body.classList.remove("resizing");
    setCoachWidth(window.innerWidth - ev.clientX);
  };
  resizer.addEventListener("pointermove", move);
  resizer.addEventListener("pointerup", up);
  resizer.addEventListener("pointercancel", up);
});
resizer.addEventListener("dblclick", () => setCoachWidth(COACH_DEFAULT_W));
resizer.addEventListener("keydown", (e) => {
  const current = Number(resizer.getAttribute("aria-valuenow")) || COACH_DEFAULT_W;
  const step = e.shiftKey ? 80 : 20;
  // The handle is on the panel's left edge: moving it left widens the panel.
  const next = { ArrowLeft: current + step, ArrowRight: current - step, Home: coachMaxW(), End: COACH_MIN_W }[e.key];
  if (next != null) {
    e.preventDefault();
    setCoachWidth(next);
  }
});
// Keep the saved width within bounds when the window gets smaller.
window.addEventListener("resize", () => {
  let w = COACH_DEFAULT_W;
  try { w = Number(localStorage.getItem("coachWidth")) || COACH_DEFAULT_W; } catch {}
  setCoachWidth(w, false);
});


// ---------- heart-rate zone settings ----------
const zoneForm = document.getElementById("zone-form");
function fillZoneForm() {
  if (state.data && document.activeElement !== zoneForm.max_hr) zoneForm.max_hr.value = state.data.overview.hr_zones.max_hr;
}
zoneForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = document.getElementById("zone-msg");
  msg.textContent = "Recalculating every run…";
  const res = await fetch("/api/zones", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ max_hr: Number(zoneForm.max_hr.value) }),
  });
  if (!res.ok) {
    msg.textContent = "Enter a max heart rate between 140 and 230.";
    return;
  }
  const z = await res.json();
  msg.textContent = `Updated: easy is now under ${z.bounds[2]} bpm. Refresh the Status card to use the new zones.`;
  load();
});


document.getElementById("logout-btn").addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST" });
  // Don't leave training data cached on this device.
  navigator.serviceWorker?.controller?.postMessage("clear-data");
  if (window.caches) for (const k of await caches.keys()) if (k.startsWith("runapp-data-")) await caches.delete(k);
  location.replace("/login");
});


// ---------- progressive web app ----------
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {}); // needs https (Tailscale) or localhost
}

// Install button: Chrome/Edge/Android offer a prompt; iPhone needs Share -> Add to Home Screen.
const installBtn = document.getElementById("install-btn");
const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone;
const isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent);
let installPrompt = null;
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  installPrompt = e;
  installBtn.hidden = false;
});
window.addEventListener("appinstalled", () => (installBtn.hidden = true));
if (isIOS && !standalone) installBtn.hidden = false;
installBtn.addEventListener("click", async () => {
  if (installPrompt) {
    installPrompt.prompt();
    await installPrompt.userChoice;
    installPrompt = null;
    installBtn.hidden = true;
  } else if (isIOS) {
    alert("To install: tap the Share button in Safari, then “Add to Home Screen”.");
  }
});

// Home-screen shortcut "Ask the coach" opens the coach.
if (location.hash === "#coach") {
  if (matchMedia("(max-width: 980px)").matches) setCoachOpen(true);
  else coachForm.message.focus();
}


// ---------- workouts: threshold & interval sessions ----------
const VERDICTS = {
  controlled: { cls: "good", text: "✓ Controlled", color: "--good" },
  hot: { cls: "warn", text: "! Hot", color: "--warning" },
  too_hard: { cls: "bad", text: "✕ Too hard", color: "--critical" },
};
const verdictPill = (v) => (VERDICTS[v] ? `<span class="pill ${VERDICTS[v].cls}">${VERDICTS[v].text}</span>` : "");
const zoneTag = (z) => (z ? `<span class="zone-tag"><i style="background:${css(`--z${z}`)}"></i>Z${z}</span>` : "–");
const signed = (n, unit = "") => (n == null ? "–" : `${n > 0 ? "+" : n < 0 ? "−" : "±"}${Math.abs(Math.round(n))}${unit}`);
const signedPace = (s) => (s == null ? "–" : `${s > 0 ? "+" : s < 0 ? "−" : "±"}${fmtTime(Math.abs(s))}/mi`);
let woData = null;
let woSelected = null;

async function loadWorkouts() {
  try {
    woData = await (await fetch("/api/workouts")).json();
  } catch {
    return;
  }
  if (!woData.sessions.length) {
    document.getElementById("wo-sessions").innerHTML = '<div class="empty">No threshold or interval sessions in the last year.</div>';
    return;
  }
  if (!woData.sessions.some((s) => s.activity_id === woSelected)) woSelected = woData.sessions[0].activity_id;
  renderWorkoutList();
  renderWorkoutTrend();
  renderWorkoutDetail();
}

function renderWorkoutList() {
  document.getElementById("wo-sessions").innerHTML = woData.sessions
    .map((s) => {
      const d = new Date(`${s.date}T00:00`);
      const strip = s.reps.map((r) => `<i style="background:${r.zone ? css(`--z${r.zone}`) : css("--muted")}" title="Rep ${r.n}: zone ${r.zone}"></i>`).join("");
      return `<button type="button" class="wo-row" data-id="${s.activity_id}" aria-pressed="${s.activity_id === woSelected}">
        <span class="wo-date">${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })}<span>${d.toLocaleDateString(undefined, { weekday: "short", year: "numeric" })}</span></span>
        <span><span class="wo-name">${esc(s.name)}</span><span class="wo-meta">${kindChip(s.kind)}<span>${esc(s.pattern)}</span><span class="rep-strip">${strip}</span></span></span>
        ${verdictPill(s.verdict)}</button>`;
    })
    .join("");
}
document.getElementById("wo-sessions").addEventListener("click", (e) => {
  const row = e.target.closest(".wo-row");
  if (!row) return;
  woSelected = Number(row.dataset.id);
  renderWorkoutList();
  renderWorkoutDetail();
});

function renderWorkoutTrend() {
  const adj = state.heatAdj;
  const pace160 = (s) => (adj && s.pace_at_160_adj_s_per_mi) || s.pace_at_160_s_per_mi;
  const sessions = [...woData.sessions].filter((s) => s.pace_at_160_s_per_mi).reverse();
  const labels = sessions.map((s) => s.date);
  const series = [
    { key: "long", label: "Long reps (2½ min+)", color: css("--text-primary"), dash: [] },
    { key: "short", label: "Short reps", color: css("--muted"), dash: [5, 4] },
  ];
  document.getElementById("wo-legend").innerHTML =
    series.map((x) => `<span><i class="line-key" style="background:${x.color}"></i>${x.label}</span>`).join("") +
    Object.values(VERDICTS).map((v) => `<span><i class="swatch" style="background:${css(v.color)};border-radius:50%"></i>${v.text.slice(2)}</span>`).join("");
  draw("wo-trend-chart", {
    type: "line",
    data: {
      labels,
      datasets: series.map((x) => {
        const pts = sessions.filter((s) => s.rep_length === x.key);
        return {
          label: x.label,
          data: pts.map((s) => ({ x: s.date, y: Math.round(pace160(s)), s })),
          borderColor: x.color,
          borderDash: x.dash,
          borderWidth: 2,
          tension: 0.25,
          spanGaps: true,
          pointRadius: 5,
          pointHoverRadius: 7,
          pointBackgroundColor: pts.map((s) => css(VERDICTS[s.verdict].color)),
          pointBorderColor: css("--surface"),
          pointBorderWidth: 2,
        };
      }),
    },
    options: baseOptions({
      interaction: { mode: "nearest", intersect: false },
      onClick: (_, els, chart) => {
        if (!els.length) return;
        const p = chart.data.datasets[els[0].datasetIndex].data[els[0].index];
        woSelected = p.s.activity_id;
        renderWorkoutList();
        renderWorkoutDetail();
      },
      scales: {
        x: { type: "category", grid: { display: false }, border: { color: css("--axis") },
             ticks: { color: css("--muted"), maxRotation: 0, autoSkipPadding: 20, callback(v) { return weekLabel(this.getLabelForValue(v)); } } },
        y: { reverse: true, grid: { color: css("--grid") }, border: { display: false },
             ticks: { color: css("--muted"), callback: (v) => `${fmtTime(v)}/mi` } },
      },
      plugins: {
        ...baseOptions().plugins,
        tooltip: {
          ...baseOptions().plugins.tooltip,
          callbacks: {
            title: (items) => fmtDate(items[0].raw.x),
            label: (item) => {
              const s = item.raw.s;
              const lines = [` ${s.name}`, ` ${s.pattern} · ${VERDICTS[s.verdict].text}`, ` ${fmtTime(item.raw.y)}/mi at 160 bpm (est.${adj && s.weather?.heat_cost_pct ? ", heat-adjusted" : ""})`];
              if (s.weather) lines.push(` ${wxShort(s.weather)}${heatText(s.weather) ? ` · ${heatText(s.weather)}` : ""}`);
              return lines;
            },
          },
        },
      },
    }),
  });
}

function renderWorkoutDetail() {
  const s = woData.sessions.find((x) => x.activity_id === woSelected);
  if (!s) return;
  const z = woData.zones;
  document.getElementById("wo-title").textContent = s.name;
  document.getElementById("wo-sub").innerHTML = `${fmtDate(s.date)} · ${esc(s.pattern)} · ${kindChip(s.kind)} ${verdictPill(s.verdict)}`;
  const m = (label, value, color) => `<span class="metric" style="--m:${css(color)}">${label} <b>${value}</b></span>`;
  document.getElementById("wo-metrics").innerHTML = [
    m("Avg rep pace", s.avg_rep_pace_s_per_mi ? `${fmtTime(s.avg_rep_pace_s_per_mi)}/mi` : "–", "--c-long"),
    m("Avg rep HR", s.avg_rep_hr ? `${Math.round(s.avg_rep_hr)} bpm` : "–", "--c-intervals"),
    m("HR drift", signed(s.hr_drift, " bpm"), "--c-threshold"),
    m("Pace fade", signedPace(s.pace_fade_s), "--c-threshold"),
    m("Pace at 160", s.pace_at_160_s_per_mi ? `${fmtTime(s.pace_at_160_s_per_mi)}/mi` : "–", "--c-easy"),
    s.weather?.heat_cost_pct ? m("Heat-adjusted", `${fmtTime(s.pace_at_160_adj_s_per_mi)}/mi`, "--c-easy") : "",
    s.weather ? m("Weather", `${wxShort(s.weather)}${s.weather.heat_cost_pct != null ? ` · +${s.weather.heat_cost_pct.toFixed(1)}%` : ""}`, "--muted") : "",
  ].join("");
  document.getElementById("wo-rep-legend").innerHTML =
    `<span><i class="line-key" style="background:${css("--z3")}"></i>Zone 3 starts (${z.bounds[2]})</span>` +
    `<span><i class="line-key" style="background:${css("--z4")}"></i>Zone 4 starts (${z.bounds[3]})</span>` +
    `<span><i class="line-key" style="background:${css("--z5")}"></i>Zone 5 starts (${z.bounds[4]})</span>`;
  const labels = s.reps.map((r) => `Rep ${r.n}`);
  const hrs = s.reps.map((r) => r.avg_hr);
  const flat = (v, color) => ({ type: "line", data: labels.map(() => v), borderColor: color, borderWidth: 1.5, borderDash: [5, 4], pointRadius: 0, order: 1 });
  draw("wo-rep-chart", {
    data: {
      labels,
      datasets: [
        { type: "bar", label: "Avg HR", data: hrs, backgroundColor: s.reps.map((r) => css(`--z${r.zone || 1}`)),
          borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: "start", maxBarThickness: 28, order: 2 },
        { ...flat(z.bounds[2], css("--z3")), label: "Zone 3 starts" },
        { ...flat(z.bounds[3], css("--z4")), label: "Zone 4 starts" },
        { ...flat(z.bounds[4], css("--z5")), label: "Zone 5 starts" },
      ],
    },
    options: baseOptions({
      scales: {
        ...baseOptions().scales,
        y: { beginAtZero: false, suggestedMin: Math.min(...hrs.filter(Boolean), z.bounds[2]) - 8, suggestedMax: Math.max(...hrs.filter(Boolean), z.bounds[4]) + 3,
             grid: { color: css("--grid") }, border: { display: false }, ticks: { color: css("--muted"), callback: (v) => `${v}` } },
      },
      plugins: {
        ...baseOptions().plugins,
        tooltip: {
          ...baseOptions().plugins.tooltip,
          filter: (item) => item.datasetIndex === 0,
          callbacks: {
            label: (item) => {
              const r = s.reps[item.dataIndex];
              return [` ${Math.round(r.avg_hr)} bpm · zone ${r.zone}`, ` ${fmtTime(r.pace_s_per_mi)}/mi for ${fmtTime(r.duration_s)}`];
            },
          },
        },
      },
    }),
  });
  document.getElementById("wo-reps").innerHTML = `<table>
    <thead><tr><th>Rep</th><th>Time</th><th>Distance</th><th>Pace</th><th>Avg HR</th><th>Zone</th><th>Cadence</th><th>Contact</th><th></th></tr></thead>
    <tbody>${s.reps
      .map((r) => `<tr><td>${r.n}</td><td>${fmtTime(r.duration_s)}</td><td>${r.distance_m ? `${(r.distance_m / 1609.344).toFixed(2)} mi` : "–"}</td>
        <td>${fmtTime(r.pace_s_per_mi)}/mi</td><td>${r.avg_hr ? Math.round(r.avg_hr) : "–"}</td><td>${zoneTag(r.zone)}</td>
        <td>${r.cadence ? Math.round(r.cadence) : "–"}</td><td>${r.gct_ms ? `${Math.round(r.gct_ms)} ms` : "–"}</td><td>${verdictPill(r.verdict)}</td></tr>`)
      .join("")}</tbody></table>
    <p class="sub" style="margin-top:10px">✓ Zone 3 or below is controlled sub-threshold work · ! zone 4 is running hot · ✕ zone 5 is too hard for threshold work.</p>`;
}


// ---------- race log ----------
const RACE_DISTANCES_LABEL = { 5000: "5k", 10000: "10k", 16093: "10 mi", 21098: "Half", 42195: "Marathon" };
const EFFORT_LABEL = { all_out: "All out", controlled: "Controlled", easy: "Easy" };
const raceDist = (m) => RACE_DISTANCES_LABEL[Math.round(m)] || `${(m / 1000).toFixed(1)} km`;
const effortChip = (e) => `<span class="kind e-${e}">${EFFORT_LABEL[e] || e}</span>`;
// Race times keep hundredths when the official result has them (22:47.88).
function fmtRaceTime(s) {
  if (s == null) return "–";
  const frac = s % 1;
  return frac > 0.004 ? `${fmtTime(Math.floor(s))}.${String(Math.round(frac * 100)).padStart(2, "0")}` : fmtTime(s);
}
let raceData = null;

async function loadRaces() {
  try {
    raceData = await (await fetch("/api/races")).json();
  } catch {
    return;
  }
  renderRaces();
}

function renderRaces() {
  const d = raceData;
  const longDate = (iso) => dayOf(iso).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric" });
  const unlogged = d.unlogged.length
    ? `<div class="unlogged banner">Garmin marked ${d.unlogged.length} run${d.unlogged.length === 1 ? "" : "s"} as a race that ${d.unlogged.length === 1 ? "isn't" : "aren't"} in your log:
        ${d.unlogged.map((u, i) => `<button type="button" class="link-btn" data-unlogged="${i}">+ ${esc(u.name)} (${fmtDate(u.date)})</button>`).join("")}</div>`
    : "";
  const upcoming = d.upcoming.length
    ? `<div class="race-section">Upcoming</div><div class="race-up-list">${d.upcoming
        .map((r) => {
          const hist = r.course_history;
          return `<div class="race-up">
            <h3>${esc(r.name)}</h3><div class="race-when">${longDate(r.date)}${r.course ? ` · ${esc(r.course)}` : ""}</div>
            <div class="race-days">${r.days_to_go === 0 ? "Today" : r.days_to_go}<small>${r.days_to_go === 0 ? "" : ` day${r.days_to_go === 1 ? "" : "s"} to go`}</small></div>
            <div class="race-facts">
              <span>Target <b>${r.target_time_s ? fmtRaceTime(r.target_time_s) : "not set"}</b> ${effortChip(r.effort)}</span>
              ${r.course_pr_s ? `<span>Course PR <b>${fmtRaceTime(r.course_pr_s)}</b></span>` : ""}
              ${hist ? `<span>Last time here: <b>${fmtRaceTime(hist.time_s)}</b> (${esc(hist.name)}, ${fmtDate(hist.date)})</span>` : ""}
            </div>
            <div class="race-actions"><button type="button" class="primary" data-pace="${r.id}">Pacing card</button><button type="button" data-edit-race="${r.id}">Edit</button></div>
          </div>`;
        })
        .join("")}</div>`
    : "";
  const past = d.past.length
    ? `<div class="race-section">Results</div><table class="races-table"><thead><tr><th>Date</th><th>Race</th><th>Time</th><th>Pace</th><th></th></tr></thead><tbody>${d.past
        .map((r) => `<tr class="${r.effort === "easy" ? "easy-race" : ""}">
          <td>${dayOf(r.date).toLocaleDateString(undefined, { month: "short", day: "numeric" })}<div class="sub" style="margin:0;font-size:11px">${r.date.slice(0, 4)}</div></td>
          <td class="race-name"><b>${esc(r.name)}</b>${r.is_pr ? '<span class="pr-badge pill warn">★ PR</span>' : r.is_course_pr ? '<span class="pr-badge pill info">Course PR</span>' : ""}
            <small>${raceDist(r.distance_m)}${r.course ? ` · ${esc(r.course)}` : ""} ${effortChip(r.effort)}${r.notes ? `<span>${esc(r.notes)}</span>` : ""}</small></td>
          <td><span class="strong">${fmtRaceTime(r.time_s)}</span>${r.time_source === "watch" ? '<div class="time-src" title="No official time entered yet">watch</div>' : ""}
            ${r.weather ? `<div class="wx-line" title="${esc(heatText(r.weather))}">${wxShort(r.weather)}</div>` : ""}
            ${r.adjusted_time_s && r.time_s - r.adjusted_time_s >= 5 ? `<div class="wx-line">≈ ${fmtTime(r.adjusted_time_s)} in cool weather</div>` : ""}</td>
          <td>${r.pace_s_per_mi ? `${fmtTime(r.pace_s_per_mi)}/mi` : "–"}</td>
          <td><button type="button" class="link-btn" data-edit-race="${r.id}" aria-label="Edit ${esc(r.name)}">Edit</button></td></tr>`)
        .join("")}</tbody></table>`
    : '<div class="empty">No races yet. Click <b>+ Add race</b>.</div>';
  document.getElementById("races").innerHTML = unlogged + upcoming + past;
}

const raceDialog = document.getElementById("race-dialog");
const raceForm = document.getElementById("race-form");
let editingRace = null;

function openRaceForm(race = null, prefill = {}) {
  editingRace = race;
  const r = race || prefill;
  document.getElementById("race-form-title").textContent = race ? "Edit race" : "Add race";
  document.getElementById("race-delete").hidden = !race;
  raceForm.date.value = r.date || isoToday();
  raceForm.name.value = r.name || "";
  const known = ["5000", "10000", "16093.44", "21097.5", "42195"].find((v) => Math.abs(Number(v) - (r.distance_m || 5000)) < 1);
  raceForm.distance.value = known || "other";
  raceForm.distance_km.value = known ? "" : ((r.distance_m || 0) / 1000).toFixed(2);
  raceForm.querySelector("[data-other-distance]").hidden = !!known;
  raceForm.course.value = r.course || "";
  raceForm.effort.value = r.effort || "all_out";
  raceForm.official.value = r.official_time_s ? fmtRaceTime(r.official_time_s) : "";
  raceForm.target.value = r.target_time_s ? fmtRaceTime(r.target_time_s) : "";
  raceForm.notes.value = r.notes || "";
  raceDialog.showModal();
}
raceForm.distance.addEventListener("change", () => (raceForm.querySelector("[data-other-distance]").hidden = raceForm.distance.value !== "other"));
document.getElementById("race-add").addEventListener("click", () => openRaceForm());
document.getElementById("race-cancel").addEventListener("click", () => raceDialog.close());
document.getElementById("race-delete").addEventListener("click", async () => {
  if (!editingRace || !confirm(`Delete ${editingRace.name}?`)) return;
  raceData = await (await fetch(`/api/races/${editingRace.id}`, { method: "DELETE" })).json();
  raceDialog.close();
  renderRaces();
  load();
});
raceForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const time = (v) => (v.trim() ? parseHms(v) : null);
  const distance_m = raceForm.distance.value === "other" ? Number(raceForm.distance_km.value) * 1000 : Number(raceForm.distance.value);
  const body = {
    date: raceForm.date.value, name: raceForm.name.value.trim(), distance_m, course: raceForm.course.value.trim() || null,
    effort: raceForm.effort.value, official_time_s: time(raceForm.official.value), target_time_s: time(raceForm.target.value),
    notes: raceForm.notes.value.trim() || null, activity_id: editingRace ? editingRace.activity_id : (raceForm.dataset.activityId ? Number(raceForm.dataset.activityId) : null),
  };
  if (!distance_m) return alert("Enter the race distance.");
  if ((raceForm.official.value.trim() && !body.official_time_s) || (raceForm.target.value.trim() && !body.target_time_s)) return alert("Enter times like 22:47.88 or 1:49:04.");
  const res = await fetch(editingRace ? `/api/races/${editingRace.id}` : "/api/races", {
    method: editingRace ? "PUT" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!res.ok) return alert("Couldn't save the race. Check the date and times.");
  raceData = await res.json();
  delete raceForm.dataset.activityId;
  raceDialog.close();
  renderRaces();
  load(); // goals and predictions use race results
});
document.getElementById("races").addEventListener("click", (e) => {
  const all = [...raceData.past, ...raceData.upcoming];
  const edit = e.target.closest("[data-edit-race]");
  if (edit) return openRaceForm(all.find((r) => r.id === Number(edit.dataset.editRace)));
  const pace = e.target.closest("[data-pace]");
  if (pace) return openPacing(all.find((r) => r.id === Number(pace.dataset.pace)));
  const u = e.target.closest("[data-unlogged]");
  if (u) {
    const g = raceData.unlogged[Number(u.dataset.unlogged)];
    raceForm.dataset.activityId = g.activity_id;
    openRaceForm(null, { date: g.date, name: g.name, distance_m: g.distance_m });
  }
});

// ---------- pacing card ----------
const paceDialog = document.getElementById("pace-dialog");
let pacingRace = null;

function openPacing(race) {
  pacingRace = race;
  const fiveK = (state.data?.predictions || []).find((p) => Math.abs(p.distance_m - race.distance_m) < 1);
  const guess = race.target_time_s || race.course_pr_s || fiveK?.from_race?.time_s || null;
  document.getElementById("pace-title").textContent = race.name;
  document.getElementById("pace-sub").textContent =
    `${dayOf(race.date).toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric", year: "numeric" })} · ${raceDist(race.distance_m)}${race.course ? ` · ${race.course}` : ""}`;
  document.getElementById("pace-target").value = guess ? fmtRaceTime(Math.round(guess)) : "";
  renderPacing();
  paceDialog.showModal();
}

function renderPacing() {
  const body = document.getElementById("pace-body");
  const target = parseHms(document.getElementById("pace-target").value || "");
  if (!target) {
    body.innerHTML = '<p class="sub">Enter a target time, like 22:30.</p>';
    return;
  }
  const r = pacingRace;
  const miles = r.distance_m / 1609.344;
  const pace = target / miles;
  const rows = [];
  const whole = Math.floor(miles + 0.005); // a 10-mile race finishes at the mile 10 marker, with no stub after it
  const tail = miles - whole;
  for (let i = 1; i <= whole; i++) if (i < whole || tail >= 0.005) rows.push({ label: `Mile ${i}`, at: pace * i });
  rows.push({ label: `Finish (${miles.toFixed(2)} mi)`, at: target, finish: true });
  const factor = r.course_history?.watch_distance_factor;
  const long = factor && factor > 1.015;
  body.innerHTML = `
    <div class="pace-summary">
      <span class="metric" style="--m:${css("--c-race")}">Target <b>${fmtRaceTime(target)}</b></span>
      <span class="metric" style="--m:${css("--c-long")}">Even pace <b>${fmtTime(pace)}/mi</b></span>
      ${r.course_pr_s ? `<span class="metric" style="--m:${css("--c-threshold")}">Course PR <b>${fmtRaceTime(r.course_pr_s)}</b></span>` : ""}
    </div>
    <table class="pace-table"><thead><tr><th>Marker</th><th>Clock at marker</th><th>That mile</th></tr></thead>
    <tbody>${rows.map((x, i) => `<tr><td>${x.label}</td><td class="split">${fmtTime(x.at)}</td><td>${x.finish && tail >= 0.005 ? `${fmtTime(x.at - (rows[i - 1]?.at || 0))} for the last ${tail.toFixed(2)} mi` : `${fmtTime(pace)}`}</td></tr>`).join("")}</tbody></table>
    ${long ? `<div class="pace-note"><b>Watch note:</b> last time on this course your watch measured ${Math.round((factor - 1) * 100)}% long (${esc(r.course_history.name)}). Pace off the official mile markers and race clock. On target, your watch will show about <b>${fmtTime(pace / factor)}/mi</b>, which looks faster than it is.</div>` : ""}
    <p class="sub" style="margin-top:10px">Even splits. Ask the coach to shape them for the course, e.g. a hill late in the race.</p>`;
}
document.getElementById("pace-target").addEventListener("input", renderPacing);
document.getElementById("pace-close").addEventListener("click", () => paceDialog.close());
document.getElementById("pace-print").addEventListener("click", () => window.print());

// ---------- coach plan proposals ----------
const OP_LABEL = { set: "Replace", add: "Add", remove: "Remove" };
let pendingProposals = [];

function proposalCard(p) {
  const items = p.changes
    .map((c) => {
      const d = dayOf(c.date);
      const was = c.current.length ? `<span class="p-was">${c.current.map((x) => `${KINDS[x.kind]?.label || x.kind}: ${esc(x.title)}`).join(" · ")}</span>` : "";
      const what = c.op === "remove" ? `<span>Clear this day</span>` : `<span>${kindChip(c.kind)} <b>${esc(c.title)}</b></span>${c.details ? `<span class="p-details">${esc(c.details)}</span>` : ""}`;
      return `<li><span class="p-date">${d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })}<small>${OP_LABEL[c.op]}</small></span><span>${what}${was}</span></li>`;
    })
    .join("");
  return `<div class="proposal" data-proposal="${p.id}">
    <div class="proposal-head">${icon("calendar")}Proposed plan changes</div>
    <p>${esc(p.summary)}</p><ul>${items}</ul>
    <div class="proposal-actions"><button type="button" class="primary" data-apply="${p.id}">Apply ${p.changes.length} change${p.changes.length === 1 ? "" : "s"}</button><button type="button" data-dismiss="${p.id}">Dismiss</button></div>
  </div>`;
}

function renderProposals() {
  coachLog.querySelectorAll(".proposal").forEach((el) => el.remove());
  const html = pendingProposals.map(proposalCard).join("");
  coachLog.insertAdjacentHTML("beforeend", html);
  document.getElementById("plan-proposals").innerHTML = html;
  if (pendingProposals.length) coachEmpty.hidden = true;
}

async function loadProposals() {
  try {
    pendingProposals = (await (await fetch("/api/plan/proposals")).json()).proposals;
  } catch {
    return;
  }
  renderProposals();
}

let proposalBusy = false; // a second click while the first is still being handled is ignored
async function actOnProposal(id, action) {
  if (proposalBusy) return;
  proposalBusy = true;
  try {
    const res = await fetch(`/api/plan/proposals/${id}/${action}`, { method: "POST" });
    if (action === "apply" && res.ok) {
      planData = await res.json();
      renderPlan();
      planEl.saved.textContent = "Applied the coach's changes. Refresh the Status card to update your next workout.";
    }
    if (!res.ok) alert((await res.json()).detail || "Couldn't update the proposal.");
    await loadProposals();
    if (action === "apply" && res.ok) addMessage("thinking", "✓ Applied to your plan.");
  } catch {
    alert("Couldn't reach the app server.");
  } finally {
    proposalBusy = false;
  }
}
document.addEventListener("click", (e) => {
  const a = e.target.closest("[data-apply]");
  if (a) return actOnProposal(Number(a.dataset.apply), "apply");
  const d = e.target.closest("[data-dismiss]");
  if (d) return actOnProposal(Number(d.dataset.dismiss), "dismiss");
});
loadProposals();
loadRaces();


// ---------- form (running dynamics) ----------
const FORM_METRICS = {
  cadence: { label: "Cadence", unit: "spm", digits: 0, note: "Steps per minute at each pace. Higher at a given pace usually means less overstriding." },
  gct: { label: "Ground contact", unit: "ms", digits: 0, note: "Time each foot spends on the ground. Shorter at a given pace = springier." },
  vr: { label: "Vertical ratio", unit: "%", digits: 1, note: "Bounce as a share of stride length. Lower = more of your effort goes forward." },
  stride: { label: "Stride", unit: "m", digits: 2, note: "Distance per step at each pace." },
};
const BAND_COLORS = { easy: "--c-easy", steady: "--c-long", threshold: "--c-threshold", race: "--c-race" };
let formData = null;
let formMetric = "cadence";
try { formMetric = localStorage.getItem("formMetric") || "cadence"; } catch {}
if (!FORM_METRICS[formMetric]) formMetric = "cadence";
const fmVal = (v, m = formMetric) => (v == null ? "–" : `${v.toFixed(FORM_METRICS[m].digits)} ${FORM_METRICS[m].unit}`);

async function loadForm() {
  try {
    formData = await (await fetch("/api/form")).json();
  } catch {
    return;
  }
  renderForm();
}
document.getElementById("fm-metric").addEventListener("click", (e) => {
  const b = e.target.closest("[data-metric]");
  if (!b) return;
  formMetric = b.dataset.metric;
  try { localStorage.setItem("formMetric", formMetric); } catch {}
  renderForm();
});

function renderForm() {
  if (!formData) return;
  const meta = FORM_METRICS[formMetric];
  document.querySelectorAll("#fm-metric button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.metric === formMetric));
  document.getElementById("fm-metric-sub").textContent = `${meta.note} Monthly, in four pace bands.`;
  document.getElementById("fm-curve-title").textContent = `${meta.label} vs pace`;

  // 1. metric by month per pace band
  const months = [...new Set(Object.values(formData.by_band).flat().map((r) => r.month))].sort().slice(-18);
  const bands = formData.bands.filter((b) => formData.by_band[b.key].length);
  document.getElementById("fm-band-legend").innerHTML = bands
    .map((b) => `<span><i class="line-key" style="background:${css(BAND_COLORS[b.key])}"></i>${esc(b.label)}</span>`).join("");
  draw("fm-band-chart", {
    type: "line",
    data: {
      labels: months.map((m) => new Date(`${m}-01T00:00`).toLocaleDateString(undefined, { month: "short", year: "2-digit" })),
      datasets: bands.map((b) => {
        const byMonth = Object.fromEntries(formData.by_band[b.key].map((r) => [r.month, r]));
        return {
          label: b.label, data: months.map((m) => byMonth[m]?.[formMetric] ?? null), rows: months.map((m) => byMonth[m]),
          borderColor: css(BAND_COLORS[b.key]), backgroundColor: css(BAND_COLORS[b.key]), borderWidth: 2, tension: 0.25,
          spanGaps: true, pointRadius: 3, pointHoverRadius: 6,
        };
      }),
    },
    options: baseOptions({
      scales: { ...baseOptions().scales, y: { beginAtZero: false, grid: { color: css("--grid") }, border: { display: false },
        ticks: { color: css("--muted"), callback: (v) => `${v} ${meta.unit}` } } },
      plugins: { ...baseOptions().plugins, tooltip: { ...baseOptions().plugins.tooltip, callbacks: {
        label: (item) => {
          const r = item.dataset.rows[item.dataIndex];
          return r ? ` ${item.dataset.label.split(" · ")[0]}: ${fmVal(item.raw)} (${r.runs} run${r.runs === 1 ? "" : "s"}, avg ${fmtTime(r.pace)}/mi)` : "";
        } } } },
    }),
  });

  // 2. curve vs pace
  const paces = [...new Set([...formData.curve.recent, ...formData.curve.earlier].map((c) => c.pace))].sort((a, b) => b - a);
  const series = [
    { label: "Last 90 days", rows: formData.curve.recent, color: css("--text-primary"), dash: [] },
    { label: "The year before", rows: formData.curve.earlier, color: css("--muted"), dash: [5, 4] },
  ];
  document.getElementById("fm-curve-legend").innerHTML = series.map((x) => `<span><i class="line-key" style="background:${x.color}"></i>${x.label}</span>`).join("");
  draw("fm-curve-chart", {
    type: "line",
    data: {
      labels: paces.map((p) => fmtTime(p)),
      datasets: series.map((x) => {
        const by = Object.fromEntries(x.rows.map((c) => [c.pace, c]));
        return { label: x.label, data: paces.map((p) => by[p]?.[formMetric] ?? null), borderColor: x.color, backgroundColor: x.color,
                 borderDash: x.dash, borderWidth: 2, tension: 0.3, spanGaps: true, pointRadius: 2, pointHoverRadius: 5 };
      }),
    },
    options: baseOptions({
      scales: {
        x: { ...baseOptions().scales.x, title: { display: true, text: "Pace (min/mi), slower → faster", color: css("--muted"), font: { size: 11 } } },
        y: { beginAtZero: false, grid: { color: css("--grid") }, border: { display: false }, ticks: { color: css("--muted"), callback: (v) => `${v} ${meta.unit}` } },
      },
      plugins: { ...baseOptions().plugins, tooltip: { ...baseOptions().plugins.tooltip, callbacks: {
        title: (items) => `${items[0].label}/mi`, label: (item) => ` ${item.dataset.label}: ${fmVal(item.raw)}` } } },
    }),
  });

  // 3. fatigue table
  const t = formData.thresholds;
  const d = (v, unit, bad, digits = 0) => (v == null ? "–" : `<span class="${bad ? "d-bad" : ""}">${v > 0 ? "+" : v < 0 ? "−" : "±"}${Math.abs(v).toFixed(digits)}${unit}</span>`);
  document.getElementById("fm-fatigue").innerHTML = formData.fatigue.length
    ? `<table><thead><tr><th>Run</th><th>Pace</th><th>Contact</th><th>Cadence</th><th>V. ratio</th></tr></thead><tbody>${formData.fatigue
        .map((f) => `<tr><td class="run"><b>${dayOf(f.date).toLocaleDateString(undefined, { month: "short", day: "numeric" })}</b>
            ${f.verdict === "fading" ? `<span class="pill warn" title="${esc(f.signs.join(", "))}">! Fading</span>` : '<span class="pill good">✓ Held</span>'}
            <small title="${esc(f.name)}">${KINDS[f.kind]?.label || f.kind} · first vs last ${f.method.replace("first vs last ", "")}</small></td>
          <td title="${fmtTime(f.pace_first)} → ${fmtTime(f.pace_last)}/mi">${f.pace_change_pct > 0 ? "+" : ""}${f.pace_change_pct}%</td>
          <td>${d(f.delta.gct, " ms", f.delta.gct >= t.gct_fade_ms)}</td>
          <td>${d(f.delta.cadence, "", f.delta.cadence <= -t.cadence_fade_spm, 1)}</td>
          <td>${d(f.delta.vr, "", f.delta.vr >= t.vr_fade_pct, 2)}</td></tr>`)
        .join("")}</tbody></table>
        <p class="sub" style="margin-top:8px">Changes are relative to your normal form at each pace, so slowing down doesn't count against you. Fading = contact +${t.gct_fade_ms} ms, cadence −${t.cadence_fade_spm} or vertical ratio +${t.vr_fade_pct} or more.</p>`
    : '<div class="empty">No runs with form data yet.</div>';

  // 4. balance
  const bal = formData.balance.slice(-60);
  document.getElementById("fm-balance-legend").innerHTML =
    `<span><i class="swatch" style="background:${css("--good")};border-radius:50%"></i>Within 2% of even</span>` +
    `<span><i class="swatch" style="background:${css("--warning")};border-radius:50%"></i>Off balance</span>` +
    `<span><i class="line-key" style="background:${css("--axis")}"></i>49–51% band</span>`;
  const flat = (v) => ({ type: "line", data: bal.map(() => v), borderColor: css("--axis"), borderWidth: 1, borderDash: [4, 4], pointRadius: 0 });
  draw("fm-balance-chart", {
    type: "line",
    data: {
      labels: bal.map((b) => b.date),
      datasets: [
        { label: "Left %", data: bal.map((b) => b.balance), borderColor: css("--text-primary"), borderWidth: 2, tension: 0.2,
          pointRadius: 4, pointHoverRadius: 6, pointBackgroundColor: bal.map((b) => css(b.alert ? "--warning" : "--good")), pointBorderColor: css("--surface"), pointBorderWidth: 2 },
        flat(49), flat(51),
      ],
    },
    options: baseOptions({
      interaction: { mode: "nearest", intersect: false },
      scales: {
        x: { ...baseOptions().scales.x, ticks: { color: css("--muted"), maxRotation: 0, autoSkipPadding: 24, callback(v) { return weekLabel(this.getLabelForValue(v)); } } },
        y: { suggestedMin: 48, suggestedMax: 52, grid: { color: css("--grid") }, border: { display: false }, ticks: { color: css("--muted"), callback: (v) => `${v}%` } },
      },
      plugins: { ...baseOptions().plugins, tooltip: { ...baseOptions().plugins.tooltip, filter: (i) => i.datasetIndex === 0, callbacks: {
        title: (items) => fmtDate(items[0].label),
        label: (item) => {
          const b = bal[item.dataIndex];
          return [` ${b.name}`, ` ${b.balance.toFixed(1)}% left / ${(100 - b.balance).toFixed(1)}% right`, b.late_balance != null ? ` Last quarter of the run: ${b.late_balance.toFixed(1)}% left` : ""].filter(Boolean);
        } } } },
    }),
  });
  document.getElementById("fm-alerts").innerHTML = !formData.balance.length
    ? "Balance needs a chest strap or footpod that records it."
    : formData.alerts.length
      ? `<span class="pill warn">! ${formData.alerts.length} run${formData.alerts.length === 1 ? "" : "s"} off balance</span>` +
        formData.alerts.slice(-4).map((a) => `<span>${fmtDate(a.date)}: ${a.balance.toFixed(1)}% left${a.late_balance != null ? `, ${a.late_balance.toFixed(1)}% late` : ""}</span>`).join("")
      : `<span class="pill good">✓ Balanced</span><span>No run in your history is more than ${t.balance_alert}% from even while running.</span>`;
}
