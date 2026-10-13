/* Show viewer-local time alongside the permanent, unambiguous UTC fallback. */
(() => {
  let formatter;
  let zone;
  try {
    zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (!zone) return;
    formatter = new Intl.DateTimeFormat(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: 'numeric', minute: '2-digit', second: '2-digit', timeZoneName: 'short',
    });
  } catch (_) {
    return;
  }
  document.querySelectorAll('[data-lifecycle-local-time]').forEach((element) => {
    const instant = new Date(element.dataset.lifecycleLocalTime);
    if (Number.isNaN(instant.getTime())) return;
    element.textContent = ` · Your time: ${formatter.format(instant)} (${zone})`;
    element.hidden = false;
  });
})();
