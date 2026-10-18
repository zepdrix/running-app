// The setup page: choose Garmin or Strava, sign in, and (later) reconnect or switch.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const errorBox = $("setup-error");

function showError(message) {
  errorBox.textContent = message || "";
  errorBox.hidden = !message;
  if (message) errorBox.scrollIntoView({ block: "nearest" });
}

// POST some JSON. Answers with the reply, or throws the app's own explanation of what went wrong.
async function post(url, body) {
  let res;
  try {
    res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  } catch {
    throw new Error("Couldn't reach the app. Is it still running on your computer?");
  }
  if (res.status === 401 && !url.startsWith("/api/setup")) location.replace("/login");
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "That didn't work. Check what you entered and try again.");
  return data;
}

// Run a form's submit: the button shows what's happening and can't be pressed twice.
function onSubmit(form, busyLabel, action) {
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const button = form.querySelector("button[type=submit]");
    const label = button.textContent;
    button.disabled = true;
    button.textContent = busyLabel;
    showError("");
    try {
      await action(form);
    } catch (err) {
      showError(err.message);
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  });
}

function pick(choice) {
  document.querySelectorAll(".choice").forEach((c) => c.setAttribute("aria-checked", String(c.dataset.choice === choice)));
  $("form-garmin").hidden = choice !== "garmin";
  $("form-strava").hidden = choice !== "strava";
  showError("");
  const form = $(`form-${choice}`);
  form.scrollIntoView({ behavior: "smooth", block: "start" });
  form.querySelector("input").focus({ preventScroll: true });
}
document.querySelectorAll(".choice").forEach((c) => c.addEventListener("click", () => pick(c.dataset.choice)));

onSubmit($("garmin-form"), "Signing in to Garmin…", async (form) => {
  const r = await post("/api/setup/garmin", { email: form.email.value, password: form.password.value });
  form.password.value = "";
  if (r.connected) return location.replace("/");
  form.hidden = true; // Garmin wants the code it just sent
  $("mfa-form").hidden = false;
  $("mfa-form").code.focus();
});
onSubmit($("mfa-form"), "Checking the code…", async (form) => {
  await post("/api/setup/garmin/mfa", { code: form.code.value });
  location.replace("/");
});
onSubmit($("strava-form"), "Opening Strava…", async (form) => {
  const r = await post("/api/setup/strava", { client_id: form.client_id.value, client_secret: form.client_secret.value });
  location.assign(r.authorize_url); // Strava sends you back here once you approve
});

$("disconnect-btn").addEventListener("click", async () => {
  const label = $("disconnect-btn").dataset.label;
  if (!confirm(`Disconnect ${label}?\n\nThe runs downloaded from ${label} are removed from this app (nothing changes in your ${label} account). Your plan, races, goals and notes stay.`)) return;
  try {
    await post("/api/source/disconnect");
    location.replace("/setup");
  } catch (err) {
    showError(err.message);
  }
});

async function start() {
  // Strava's sign-in page only accepts the address typed into the app's settings there.
  $("callback-domain").textContent = location.hostname;
  let state;
  try {
    const res = await fetch("/api/setup");
    if (res.status === 401) return location.replace("/login");
    state = await res.json();
  } catch {
    return showError("Couldn't reach the app. Is it still running on your computer?");
  }
  showError(new URLSearchParams(location.search).get("error"));
  if (state.demo) {
    $("setup-sub").textContent = "This is the demo with made-up runs. Start the app normally to connect your own account.";
    return;
  }
  if (!state.coach_ready) {
    $("coach-note").hidden = false;
    $("coach-note").innerHTML = `<b>Optional:</b> the AI coach, the daily status and sending workouts to a watch need <a href="https://claude.com/claude-code" target="_blank" rel="noopener">Claude Code</a> installed and signed in on this computer. Everything else works without it.`;
  }
  const src = state.source;
  if (!src) {
    $("setup-sub").textContent = "Two steps: choose where your runs are, then sign in. Your data stays on this computer.";
    $("choose").hidden = false;
    return;
  }
  // Connected already: offer the app, signing in again, or switching.
  const other = Object.entries(state.sources).find(([key]) => key !== src.key)[1].label;
  $("setup-title").textContent = "Your connection";
  $("connected").hidden = false;
  $("connected-title").textContent = state.connected ? `Connected to ${src.label}` : `${src.label} needs signing in again`;
  $("connected-sub").textContent = state.connected
    ? `${state.activities.toLocaleString()} activities downloaded so far.${state.syncing ? " A sync is running now." : ""}`
    : `The saved sign-in is missing. Sign in again below.`;
  $("reconnect-summary").textContent = `Sign in to ${src.label} again`;
  $("reconnect-sub").textContent = `Use this if syncing says your ${src.label} sign-in has expired. Nothing downloaded is lost.`;
  $("reconnect-slot").append($(`form-${src.key}`));
  $(`form-${src.key}`).hidden = false;
  $(`form-${src.key}`).classList.remove("card", "setup-card");
  $(`form-${src.key}`).querySelector(".setup-step").hidden = true;
  if (!state.connected) document.querySelector(".setup-more").open = true;
  $("switch-summary").textContent = `Use ${other} instead`;
  $("switch-sub").textContent = `The app reads from one service at a time. To use ${other}, disconnect ${src.label} first: the downloaded runs are removed and fetched again from ${other}. Your plan, races, goals and notes stay.`;
  $("disconnect-btn").textContent = `Disconnect ${src.label}`;
  $("disconnect-btn").dataset.label = src.label;
}
start();
