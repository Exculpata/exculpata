/* Optional, read-only briefing: selection only fills an existing composer. */
(() => {
  let region = document.querySelector("[data-briefing-region]");
  if (!region) return;
  let processing = region.dataset.briefingState === "processing";
  let needsRefresh = processing;
  let refreshing = false;
  let generation = 0;
  const isProcessing = (value) => value.state === "preparing" || Number(value.processing_count || 0) > 0
    || Number(value.overview_processing_count || 0) > 0 || value.discovery?.working === true;

  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-briefing-question]");
    if (!button || !region.contains(button) || button.disabled || region.hidden) return;
    const oneBox = document.querySelector("#one-box-question");
    const target = oneBox || document.querySelector("#assistant-question");
    const form = target?.form;
    const status = region.querySelector("[data-briefing-question-status]");
    if (!target || target.disabled || target.readOnly || form?.getAttribute("aria-busy") === "true"
        || form?.dataset.scopePending === "true" || !button.value || button.value.length > target.maxLength) {
      if (status) status.textContent = "The question box is busy or unavailable. Try again when it is ready.";
      return;
    }
    target.value = button.value;
    target.dispatchEvent(new Event("input", { bubbles: true }));
    if (status) status.textContent = "Question filled. Review it, then submit when you are ready.";
    if (!oneBox) document.querySelector("[data-assistant-expand]")?.click();
    target.focus();
    target.scrollIntoView({ block: "nearest" });
  });

  const refresh = async () => {
    if (refreshing || !needsRefresh || processing) return;
    refreshing = true;
    const started = generation;
    // One attempt per observed processing→finished transition. Existing
    // readiness polling owns the schedule; errors never start a retry loop.
    needsRefresh = false;
    try {
      const response = await fetch(region.dataset.briefingUrl, { cache: "no-store" });
      if (!response.ok) throw new Error("Home unavailable");
      const page = new DOMParser().parseFromString(await response.text(), "text/html");
      const next = page.querySelector("[data-briefing-region]");
      if (!next) throw new Error("Briefing unavailable");
      if (processing || generation !== started) { needsRefresh = true; return; }
      region.replaceWith(next);
      region = next;
      processing = next.dataset.briefingState === "processing";
      needsRefresh = processing;
    } catch (_error) {
      if (generation !== started) { needsRefresh = true; return; }
      if (!processing) {
        const line = document.createElement("p");
        line.className = "briefing-unavailable";
        line.dataset.briefingUnavailable = "";
        line.append("The briefing could not refresh. ");
        for (const [label, href] of [["Review sources", region.dataset.briefingSourcesUrl], ["Refresh Home", region.dataset.briefingUrl]]) {
          const link = document.createElement("a");
          link.href = href;
          link.textContent = label;
          line.append(link, " ");
        }
        region.replaceChildren(line);
        region.hidden = false;
        region.dataset.briefingState = "unavailable";
      }
    } finally {
      refreshing = false;
      if (!processing && needsRefresh && generation !== started) refresh();
    }
  };
  window.addEventListener("recordbench:readiness", (event) => {
    if (!event.detail) return;
    const nextProcessing = isProcessing(event.detail);
    if (nextProcessing && !processing) generation += 1;
    processing = nextProcessing;
    if (processing) {
      needsRefresh = true;
      region.hidden = true;
      region.dataset.briefingState = "processing";
    } else { refresh(); }
  });
})();
