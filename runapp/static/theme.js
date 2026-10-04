// Appearance: "auto" follows the device, "light" and "dark" override it. Loaded before the page paints
// (not deferred), so a chosen theme never flashes the other one first.
(function () {
  const BAR = { light: "#f3f5f9", dark: "#121720" }; // the browser's own bars, matched to the page
  const systemDark = matchMedia("(prefers-color-scheme: dark)");
  function saved() {
    try {
      const t = localStorage.getItem("theme");
      return t === "light" || t === "dark" ? t : "auto";
    } catch {
      return "auto";
    }
  }
  function apply(choice) {
    const root = document.documentElement;
    if (choice === "auto") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", choice);
    const shown = choice === "auto" ? (systemDark.matches ? "dark" : "light") : choice;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", BAR[shown]);
  }
  window.appTheme = {
    get: saved,
    set(choice) {
      try { localStorage.setItem("theme", choice); } catch {}
      apply(choice);
    },
  };
  apply(saved());
  systemDark.addEventListener("change", () => apply(saved()));
})();
