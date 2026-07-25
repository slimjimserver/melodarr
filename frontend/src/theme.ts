(() => {
  const storageKey = "melodarr-theme";
  let theme = "midnight";
  try {
    if (window.localStorage.getItem(storageKey) === "warm") theme = "warm";
  } catch {
    // Storage can be unavailable in strict privacy modes; Midnight remains
    // the dependable no-storage default.
  }
  document.documentElement.dataset.theme = theme;
  document.querySelector('meta[name="theme-color"]')?.setAttribute(
    "content",
    theme === "warm" ? "#f6f0e7" : "#050506",
  );
})();
