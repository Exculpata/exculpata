/* Review list: select a document, read it beside the list, move with the keyboard. */
(() => {
  const root = document.querySelector("[data-discovery-review]");
  if (!root) return;
  const panel = root.querySelector("[data-review-panel]");
  const status = root.querySelector("[data-review-status]");
  const position = root.querySelector("[data-review-position]");
  const chooser = root.querySelector("[data-review-chooser]");
  const opener = root.querySelector("[data-review-chooser-open]");
  const rows = () => Array.from(root.querySelectorAll("[data-review-row]"));
  const current = () => root.querySelector("[data-review-row].selected");
  let preferred = "";
  let request = 0;
  try { preferred = sessionStorage.getItem("discovery-review-view") || ""; } catch { preferred = ""; }

  const say = (message) => { if (status) status.textContent = message; };
  const withView = (url) => {
    const target = new URL(url, window.location.origin);
    if (preferred && !target.searchParams.has("view")) target.searchParams.set("view", preferred);
    return target.toString();
  };
  const pageLink = (forward) => root.querySelector(forward ? '[data-review-page="next"]' : '[data-review-page="previous"]');
  const neighbor = (row, forward) => {
    const other = forward ? row?.nextElementSibling : row?.previousElementSibling;
    return other?.matches("[data-review-row]") ? other : null;
  };
  const load = async (url) => {
    const response = await fetch(url, { cache: "no-store", credentials: "same-origin", headers: { Accept: "text/html" } });
    if (!response.ok) throw new Error(String(response.status));
    const html = await response.text();
    return html;
  };
  const localTimes = (scope) => scope.querySelectorAll("[data-review-time]").forEach((time) => {
    const date = new Date(time.dateTime);
    if (!Number.isNaN(date.getTime())) time.textContent = date.toLocaleString(undefined, { timeZoneName: "short" });
  });
  localTimes(root);
  const show = async (url, fallback, wanted) => {
    const mine = ++request;
    try {
      const target = new URL(url, window.location.origin);
      const selected = new URL(current()?.dataset.url || window.location.href, window.location.origin);
      ["q", "sort", "dir", "all_columns"].forEach((key) => {
        if (selected.searchParams.has(key)) target.searchParams.set(key, selected.searchParams.get(key));
      });
      const html = await load(target.toString());
      if (mine !== request) return;
      panel.innerHTML = html;
      localTimes(panel);
      const shown = panel.querySelector("[data-review-item]")?.dataset.view;
      say(wanted && shown !== wanted ? missingView[wanted] : "");
    } catch {
      if (mine !== request) return;
      // The server-rendered page is always a complete answer, so fall back to it.
      if (fallback) window.location.assign(fallback);
      else say("This document could not be opened. Reload the page and try again.");
    }
  };
  const select = (row, focusList) => {
    if (!row || !panel) return;
    const all = rows();
    all.forEach((other) => { other.classList.toggle("selected", other === row); other.removeAttribute("aria-current"); });
    row.setAttribute("aria-current", "true");
    row.scrollIntoView({ block: "nearest" });
    history.replaceState(null, "", row.dataset.url);
    root.querySelectorAll("[data-review-step]").forEach((link) => {
      const forward = link.dataset.reviewStep === "next";
      const target = neighbor(row, forward)?.dataset.url || pageLink(forward)?.href;
      if (target) link.href = target; else link.removeAttribute("href");
    });
    if (position) position.textContent = row.dataset.positionLabel;
    show(withView(row.dataset.fragment), row.dataset.url);
    if (focusList) root.querySelector("[data-review-list]")?.focus({ preventScroll: true });
  };
  const step = (forward) => {
    const selected = current();
    const target = selected ? neighbor(selected, forward) : rows()[0];
    if (target) return select(target, true);
    const edge = pageLink(forward);
    if (edge) window.location.assign(edge.href);
    else say(forward ? "This is the last document in the list." : "This is the first document in the list.");
  };
  const missingView = {
    document: "This item has no in-page document view. Use Open source page.",
    text: "This item has no extracted text.",
    fields: "This item has no fields view.",
  };
  // A view choice always applies to the selected row, even while its panel is still loading.
  const openView = (name) => {
    const row = current();
    if (!row || !panel) return;
    preferred = name;
    try { sessionStorage.setItem("discovery-review-view", name); } catch { /* the choice lasts for this page */ }
    show(withView(row.dataset.fragment), row.dataset.url, name);
  };
  const shownToken = () => panel?.querySelector("[data-review-item]")?.dataset.token;

  root.addEventListener("submit", (event) => {
    if (event.target.matches("[data-code-form]") && shownToken() !== current()?.dataset.token) {
      event.preventDefault();
      say("Wait for the selected item to open before applying a code.");
    }
  });

  root.addEventListener("click", (event) => {
    const fragment = event.target.closest("[data-review-fragment]");
    if (fragment && panel?.contains(fragment)) {
      event.preventDefault();
      if (fragment.dataset.reviewView) openView(fragment.dataset.reviewView);
      else if (shownToken() === current()?.dataset.token) show(fragment.dataset.reviewFragment, fragment.href);
      return;
    }
    const stepper = event.target.closest("[data-review-step]");
    if (stepper) { event.preventDefault(); step(stepper.dataset.reviewStep === "next"); return; }
    const row = event.target.closest("[data-review-row]");
    if (row && !event.metaKey && !event.ctrlKey && !event.shiftKey) { event.preventDefault(); select(row, false); return; }
    const move = event.target.closest("[data-review-move]");
    if (move) {
      const item = move.closest("li");
      const sibling = move.dataset.reviewMove === "up" ? item.previousElementSibling : item.nextElementSibling;
      if (sibling) item.parentNode.insertBefore(item, move.dataset.reviewMove === "up" ? sibling : sibling.nextElementSibling);
      move.focus();
    }
  });

  root.addEventListener("input", (event) => {
    if (!event.target.matches("[data-field-filter]")) return;
    const needle = event.target.value.trim().toLowerCase();
    const scope = event.target.closest("[data-review-fields]");
    scope.querySelectorAll("[data-field-row]").forEach((row) => { row.hidden = !!needle && !row.textContent.toLowerCase().includes(needle); });
    if (needle) scope.querySelectorAll("details").forEach((details) => { details.open = true; });
  });

  const toggleChooser = (open) => {
    if (!chooser) return;
    chooser.hidden = !open;
    opener?.setAttribute("aria-expanded", String(open));
    if (open) chooser.querySelector("[data-review-chooser-close]")?.focus();
    else opener?.focus();
  };
  if (chooser && opener) {
    chooser.classList.add("dr-sheet");
    chooser.hidden = true;
    opener.addEventListener("click", () => toggleChooser(chooser.hidden));
    chooser.querySelector("[data-review-chooser-close]")?.addEventListener("click", () => toggleChooser(false));
  }

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && chooser && !chooser.hidden && chooser.classList.contains("dr-sheet")) { toggleChooser(false); return; }
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const target = event.target;
    if (target.closest("input, textarea, select, [contenteditable], [data-review-chooser]")) return;
    if (target.closest("button, summary, a") && (event.key === " " || event.key === "Enter")) return;
    if (/^[1-9]$/.test(event.key)) {
      if (event.repeat || shownToken() !== current()?.dataset.token) return;
      const button = Array.from(panel?.querySelectorAll("[data-code-key]") || [])
        .find((candidate) => candidate.dataset.codeKey === event.key);
      if (button) { event.preventDefault(); button.click(); }
      return;
    }
    const actions = {
      j: () => step(true), ArrowDown: () => step(true), k: () => step(false), ArrowUp: () => step(false),
      d: () => openView("document"), t: () => openView("text"), f: () => openView("fields"),
      "/": () => root.querySelector("[data-review-filter]")?.focus(),
      c: () => toggleChooser(true),
      " ": () => {
        const link = panel?.querySelector(event.shiftKey ? '[data-review-unit="previous"]' : '[data-review-unit="next"]');
        if (link) link.click();
        else if (panel?.querySelector(".dr-text")) say(event.shiftKey ? "This is the first page of text." : "This is the last page of text.");
        else return false;
      },
    };
    const action = actions[event.key];
    if (!action || (event.shiftKey && event.key !== " ")) return;
    if (action() !== false) event.preventDefault();
  });
})();
