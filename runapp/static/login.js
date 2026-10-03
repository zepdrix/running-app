const form = document.getElementById("login-form");
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const error = document.getElementById("error");
  error.textContent = "";
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: form.password.value }),
    });
    if (res.ok) location.replace("/");
    else error.textContent = res.status === 401 ? "Wrong password." : "Couldn't log in.";
  } catch {
    error.textContent = "Couldn't reach the app. Is your Mac on and the app running?";
  } finally {
    button.disabled = false;
  }
});
