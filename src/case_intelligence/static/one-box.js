/* Reuse the existing readiness poll; native forms retain keyboard/no-script use. */
(() => {
  window.addEventListener("recordbench:answer-complete", (event) => {
    const query = new URLSearchParams(window.location.search);
    if (!document.querySelector(".one-box-route") || query.get("one_box_kind") !== "question"
        || query.get("conversation") !== event.detail?.conversation_id) return;
    // This page is already a GET of the canonical conversation. A full reload
    // gets its completed answer while retaining the entry-point explanation.
    event.preventDefault();
    window.location.reload();
  });
  const form = document.querySelector("[data-one-box-form]");
  if (!form || form.dataset.readOnly === "true") return;
  const hint = form.querySelector("[data-one-box-readiness-hint]");
  window.addEventListener("recordbench:readiness", (event) => {
    const readiness = event.detail;
    if (!readiness) return;
    form.querySelectorAll("input[name=question], button[type=submit]").forEach((control) => {
      control.disabled = readiness.can_query !== true;
    });
    hint.hidden = readiness.can_query === true && !readiness.partial_query;
    hint.textContent = readiness.partial_query ? readiness.coverage_notice
      : readiness.state === "preparing" ? "Questions will be available when active preparation finishes."
      : readiness.guidance;
  });
})();
