(() => {
  const body = document.body;
  const csrfToken = body.dataset.csrfToken || "";
  const railToggle = document.querySelector("[data-rail-toggle]");
  const railSwitchers = Array.from(document.querySelectorAll("[data-rail-switcher]"));
  const railCollapse = document.querySelector("[data-rail-collapse]");
  const railScrim = document.querySelector("[data-rail-scrim]");
  const matterRail = document.querySelector("[data-matter-rail]");
  const matterList = document.querySelector(".matter-list");
  const activeMatterLink = matterList?.querySelector('[aria-current="page"]');
  const desktopRailMedia = window.matchMedia("(min-width: 901px)");
  const railPreferenceKey = "case-intelligence:matter-rail-collapsed";

  // Match controls-to-spaces-v1 at this DOM boundary. Structured excerpts and
  // their full-unit digests remain exact; the Unicode flag preserves valid
  // supplementary characters while replacing lone surrogate code points.
  const presentCitedText = (value) => typeof value === "string"
    ? value.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f\ud800-\udfff\ufffe\uffff]/gu, " ")
    : "";
  const citedContextRequests = new WeakMap();
  const clearCitedContext = (details) => {
    citedContextRequests.get(details)?.abort();
    citedContextRequests.delete(details);
    details.removeAttribute("aria-busy");
    details.querySelector("[data-context-result]").hidden = true;
    ["source", "excerpt", "version", "digest"].forEach((field) => {
      details.querySelector(`[data-context-${field}]`).textContent = "";
    });
    const sourceLink = details.querySelector("[data-context-source-link]");
    sourceLink.hidden = true;
    sourceLink.removeAttribute("href");
    const retry = details.querySelector("[data-context-retry]");
    retry.hidden = true;
    retry.removeAttribute("aria-disabled");
    details.querySelector("[data-context-status]").textContent = "";
  };

  const loadCitedContext = async (details) => {
    const retry = details.querySelector("[data-context-retry]");
    const retryHadFocus = document.activeElement === retry;
    clearCitedContext(details);
    if (!details.open || !details.isConnected) return;
    const controller = new AbortController();
    citedContextRequests.set(details, controller);
    details.setAttribute("aria-busy", "true");
    const status = details.querySelector("[data-context-status]");
    status.textContent = "Checking access and cited source version…";
    if (retryHadFocus) {
      retry.hidden = false;
      retry.setAttribute("aria-disabled", "true");
      retry.focus({ preventScroll: true });
    }
    const isCurrent = () => (
      details.open && details.isConnected && citedContextRequests.get(details) === controller
    );
    const finishRetry = (visible) => {
      if (!visible && document.activeElement === retry) details.querySelector("summary").focus({ preventScroll: true });
      retry.hidden = !visible;
      retry.removeAttribute("aria-disabled");
    };
    try {
      const response = await fetch(`${details.dataset.contextUrl}?format=json`, {
        cache: "no-store",
        credentials: "same-origin",
        redirect: "error",
        headers: { Accept: "application/json" },
        signal: controller.signal,
      });
      if (!response.ok) throw new Error("Cited context unavailable");
      const context = await response.json();
      if (!isCurrent()) return;
      if (!context || !["available", "unavailable"].includes(context.state)) throw new Error("Invalid cited context");
      status.textContent = typeof context.notice === "string" ? context.notice : "";
      if (context.state === "unavailable") {
        if (!status.textContent) status.textContent = "Cited context is unavailable. Review the comparison page for details.";
        finishRetry(true);
        return;
      }
      if (typeof context.excerpt !== "string" || !context.excerpt) throw new Error("Missing cited context");
      const text = presentCitedText;
      details.querySelector("[data-context-source]").textContent = [text(context.source_name), text(context.location)].filter(Boolean).join(" · ");
      details.querySelector("[data-context-excerpt]").textContent = Array.from(presentCitedText(context.excerpt)).slice(0, 6000).join("");
      details.querySelector("[data-context-version]").textContent = text(context.source_version_id) || "Not recorded";
      details.querySelector("[data-context-digest]").textContent = text(context.excerpt_digest) || "Not recorded";
      if (typeof context.source_href === "string" && context.source_href.startsWith("/matters/")) {
        const sourceURL = new URL(context.source_href, window.location.origin);
        if (sourceURL.origin === window.location.origin) {
          const sourceLink = details.querySelector("[data-context-source-link]");
          sourceLink.href = sourceURL.href;
          sourceLink.hidden = false;
        }
      }
      details.querySelector("[data-context-result]").hidden = false;
      if (!status.textContent) status.textContent = "Cited context loaded. Compare it with the passage above.";
      finishRetry(false);
    } catch (_error) {
      if (!isCurrent()) return;
      status.textContent = "Cited context could not be loaded. Try again or open the comparison page.";
      finishRetry(true);
    } finally {
      if (isCurrent()) details.removeAttribute("aria-busy");
    }
  };

  // Capture native toggle events so replaced assistant fragments work as well.
  document.addEventListener("toggle", (event) => {
    const details = event.target;
    if (!(details instanceof HTMLDetailsElement) || !details.matches("[data-cited-context]")) return;
    if (details.open) loadCitedContext(details);
    else clearCitedContext(details);
  }, true);
  document.addEventListener("click", (event) => {
    if (!(event.target instanceof Element)) return;
    const retry = event.target.closest("[data-context-retry]");
    if (!retry || retry.getAttribute("aria-disabled") === "true") return;
    const details = retry.closest("[data-cited-context]");
    if (details?.open) loadCitedContext(details);
  });

  const readSessionValue = (key) => {
    if (!key) return "";
    try {
      return window.sessionStorage.getItem(key) || "";
    } catch (_error) {
      return "";
    }
  };

  const writeSessionValue = (key, value) => {
    if (!key) return false;
    try {
      window.sessionStorage.setItem(key, value);
      return true;
    } catch (_error) {
      return false;
    }
  };

  const removeSessionValue = (key) => {
    if (!key) return false;
    try {
      window.sessionStorage.removeItem(key);
      return true;
    } catch (_error) {
      return false;
    }
  };

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm || "Continue?")) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    });
  });

  const revealActiveMatter = () => {
    if (!matterList || !activeMatterLink) return;
    const listBounds = matterList.getBoundingClientRect();
    const activeBounds = activeMatterLink.getBoundingClientRect();
    const activeTop = activeBounds.top - listBounds.top + matterList.scrollTop;
    const activeBottom = activeTop + activeBounds.height;
    if (activeTop < matterList.scrollTop) {
      matterList.scrollTop = Math.max(0, activeTop - 4);
    } else if (activeBottom > matterList.scrollTop + matterList.clientHeight) {
      matterList.scrollTop = activeBottom - matterList.clientHeight + 4;
    }
  };

  document.querySelectorAll("[data-working-form]").forEach((form) => {
    form.addEventListener("submit", () => {
      form.setAttribute("aria-busy", "true");
      const button = form.querySelector('button[type="submit"]');
      if (!button) return;
      button.disabled = true;
      button.textContent = form.dataset.workingLabel || "Working…";
    });
  });

  const criterionTemplates = {
    person: {
      title: "References to [person or organization]",
      instructions: "Mark a source for review when it names or clearly refers to [person or organization] in connection with [topic or event].",
      include: "Count aliases, titles, initials, and clear contextual references to the same person or organization.",
      exclude: "Leave out unrelated people or organizations with a similar name and incidental directory-style mentions.",
    },
    date: {
      title: "Events during [date range]",
      instructions: "Mark a source for review when it describes an event that occurred between [start date] and [end date].",
      include: "Count explicit dates and events whose date can be established from the surrounding passage.",
      exclude: "Leave out document creation dates and dates outside the range unless they describe an event inside the range.",
    },
    topic: {
      title: "Materials about [topic or event]",
      instructions: "Mark a source for review when it contains substantive information about [topic or event].",
      include: "Count direct descriptions, attributed statements, and records that materially explain the topic or event.",
      exclude: "Leave out passing mentions that add no substantive information and unrelated uses of the same words.",
    },
  };
  document.querySelectorAll("[data-criterion-form]").forEach((form) => {
    const fields = {
      title: form.querySelector("[data-criterion-title]"),
      instructions: form.querySelector("[data-criterion-instructions]"),
      include: form.querySelector("[data-criterion-include]"),
      exclude: form.querySelector("[data-criterion-exclude]"),
    };
    form.querySelectorAll("[data-criterion-template]").forEach((button) => {
      button.setAttribute("aria-pressed", "false");
      button.addEventListener("click", () => {
        const template = criterionTemplates[button.dataset.criterionTemplate];
        if (!template) return;
        Object.entries(fields).forEach(([key, field]) => {
          if (field) field.value = template[key] || "";
        });
        form.querySelectorAll("[data-criterion-template]").forEach((choice) => {
          choice.setAttribute("aria-pressed", choice === button ? "true" : "false");
        });
        fields.title?.focus({ preventScroll: true });
        fields.title?.select();
      });
    });
  });

  const notebookContext = document.querySelector(".composer-notebook-context");
  const notebookSelections = Array.from(
    notebookContext?.querySelectorAll('input[name="notebook_item"]') || [],
  );
  const notebookModes = Array.from(
    notebookContext?.querySelectorAll('input[name="notebook_mode"]') || [],
  );
  notebookSelections.forEach((selection) => {
    selection.addEventListener("change", () => {
      if (selection.checked) notebookModes.forEach((mode) => { mode.checked = false; });
      if (!notebookSelections.some((item) => item.checked) && !notebookModes.some((mode) => mode.checked)) {
        const off = notebookModes.find((mode) => mode.value === "");
        if (off) off.checked = true;
      }
    });
  });
  notebookModes.forEach((mode) => {
    mode.addEventListener("change", () => {
      if (mode.checked) notebookSelections.forEach((selection) => { selection.checked = false; });
    });
  });

  const downloadStatus = document.querySelector("[data-download-status]");
  let downloadStatusTimer;
  document.querySelectorAll("[data-download]").forEach((link) => {
    link.addEventListener("click", () => {
      if (!downloadStatus) return;
      window.clearTimeout(downloadStatusTimer);
      downloadStatus.hidden = false;
      downloadStatus.textContent = "Preparing your download. Your browser will show the file when it is ready.";
      downloadStatusTimer = window.setTimeout(() => {
        downloadStatus.textContent = "Download requested. If it did not appear, choose the export again.";
      }, 2500);
    });
  });

  const readDesktopRailPreference = () => {
    try {
      return window.localStorage.getItem(railPreferenceKey) === "true";
    } catch (_error) {
      return false;
    }
  };

  const saveDesktopRailPreference = (collapsed) => {
    try {
      window.localStorage.setItem(railPreferenceKey, collapsed ? "true" : "false");
    } catch (_error) {
      // The navigation still works when browser storage is unavailable.
    }
  };

  const railIsExpanded = () => (
    desktopRailMedia.matches
      ? !body.classList.contains("rail-collapsed")
      : body.classList.contains("rail-open")
  );

  const syncRailState = () => {
    const expanded = railIsExpanded();
    [railToggle, ...railSwitchers].forEach((control) => {
      control?.setAttribute("aria-expanded", expanded ? "true" : "false");
    });
    railToggle?.setAttribute("aria-label", expanded ? "Hide matters" : "Show matters");
    railSwitchers.forEach((control) => {
      control.setAttribute(
        "aria-label",
        expanded ? "Hide matter navigation" : "Show matter navigation",
      );
    });
    if (matterRail) {
      matterRail.setAttribute("aria-hidden", expanded ? "false" : "true");
      matterRail.inert = !expanded;
    }
  };

  const focusActiveMatter = () => {
    revealActiveMatter();
    activeMatterLink?.focus({ preventScroll: true });
  };

  const setRailOpen = (open, { focusMatter = false, restoreFocus = false } = {}) => {
    if (!desktopRailMedia.matches && !open && matterRail?.contains(document.activeElement)) {
      railToggle?.focus({ preventScroll: true });
    }
    body.classList.toggle("rail-open", open);
    syncRailState();
    if (open) {
      window.requestAnimationFrame(focusMatter ? focusActiveMatter : revealActiveMatter);
    } else if (restoreFocus) {
      railToggle?.focus({ preventScroll: true });
    }
  };

  const setDesktopRailCollapsed = (collapsed) => {
    if (collapsed && matterRail?.contains(document.activeElement)) {
      railToggle?.focus({ preventScroll: true });
    }
    body.classList.toggle("rail-collapsed", collapsed);
    saveDesktopRailPreference(collapsed);
    syncRailState();
    if (!collapsed) window.requestAnimationFrame(revealActiveMatter);
  };

  const toggleRail = ({ focusMatter = false } = {}) => {
    if (!desktopRailMedia.matches) {
      setRailOpen(!body.classList.contains("rail-open"), { focusMatter });
    } else {
      setDesktopRailCollapsed(!body.classList.contains("rail-collapsed"));
    }
  };

  if (desktopRailMedia.matches && readDesktopRailPreference()) {
    body.classList.add("rail-collapsed");
  }
  syncRailState();
  if (railIsExpanded()) window.requestAnimationFrame(revealActiveMatter);

  railToggle?.addEventListener("click", () => toggleRail({ focusMatter: true }));
  railSwitchers.forEach((control) => {
    control.addEventListener("click", () => toggleRail({ focusMatter: true }));
  });
  railCollapse?.addEventListener("click", () => {
    railToggle?.focus({ preventScroll: true });
    if (desktopRailMedia.matches) setDesktopRailCollapsed(true);
    else setRailOpen(false);
  });
  railScrim?.addEventListener("click", () => setRailOpen(false, { restoreFocus: true }));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && body.classList.contains("rail-open")) {
      setRailOpen(false, { restoreFocus: true });
    }
  });
  const handleRailBreakpoint = () => {
    if (matterRail?.contains(document.activeElement)) railToggle?.focus({ preventScroll: true });
    body.classList.remove("rail-open");
    syncRailState();
    if (railIsExpanded()) window.requestAnimationFrame(revealActiveMatter);
  };
  if (typeof desktopRailMedia.addEventListener === "function") {
    desktopRailMedia.addEventListener("change", handleRailBreakpoint);
  } else {
    desktopRailMedia.addListener(handleRailBreakpoint);
  }

  const conversationLibrary = document.querySelector("[data-conversation-library]");
  const conversationFilter = conversationLibrary?.querySelector("[data-conversation-filter]");
  const conversationGroups = Array.from(
    conversationLibrary?.querySelectorAll("[data-conversation-group]") || [],
  );
  const conversationFilterEmpty = conversationLibrary?.querySelector(
    "[data-conversation-filter-empty]",
  );

  const filterConversations = () => {
    const query = (conversationFilter?.value || "").trim().toLocaleLowerCase();
    let visibleTotal = 0;
    conversationGroups.forEach((group) => {
      const items = Array.from(group.querySelectorAll("[data-conversation-item]"));
      let visibleInGroup = 0;
      items.forEach((item) => {
        const title = item.querySelector(".conversation-history-item > span")?.textContent || "";
        const visible = !query || title.toLocaleLowerCase().includes(query);
        item.hidden = !visible;
        if (visible) visibleInGroup += 1;
      });
      group.hidden = visibleInGroup === 0;
      const count = group.querySelector("[data-conversation-group-count]");
      if (count) count.textContent = String(visibleInGroup);
      if (query && visibleInGroup && group.matches("details")) group.open = true;
      visibleTotal += visibleInGroup;
    });
    if (conversationFilterEmpty) conversationFilterEmpty.hidden = visibleTotal !== 0;
  };

  conversationFilter?.addEventListener("input", filterConversations);
  conversationFilter?.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !conversationFilter.value) return;
    conversationFilter.value = "";
    filterConversations();
  });

  const uploadForm = document.querySelector("[data-upload-form]");
  const uploadDrop = document.querySelector("[data-upload-drop]");
  const fileInput = document.querySelector("[data-file-input]");
  const folderInput = document.querySelector("[data-folder-input]");
  const fileChooser = document.querySelector("[data-file-chooser]");
  if (fileChooser) fileChooser.hidden = false;
  const folderChooser = document.querySelector("[data-folder-chooser]");
  const fileSummary = document.querySelector("[data-file-summary]");
  const uploadCollectionName = document.querySelector("[data-upload-collection-name]");
  let collectionNameEdited = false;
  uploadCollectionName?.addEventListener("input", () => { collectionNameEdited = true; });
  const uploadProgress = document.querySelector("[data-upload-progress]");
  const uploadTitle = document.querySelector("[data-upload-title]");
  const uploadStatus = document.querySelector("[data-upload-status]");
  const uploadProgressBar = document.querySelector("[data-upload-progress-bar]");
  const uploadCount = document.querySelector("[data-upload-count]");
  const uploadBytes = document.querySelector("[data-upload-bytes]");
  const uploadErrors = document.querySelector("[data-upload-errors]");
  const uploadCancel = document.querySelector("[data-upload-cancel]");
  const uploadReview = document.querySelector("[data-upload-review]");
  const uploadPreflight = document.querySelector("[data-upload-preflight]");
  const uploadPreflightState = document.querySelector("[data-upload-preflight-state]");
  const uploadPreflightStatus = document.querySelector("[data-upload-preflight-status]");
  const uploadPreflightCounts = document.querySelector("[data-upload-preflight-counts]");
  const uploadPreflightItems = document.querySelector("[data-upload-preflight-items]");
  const uploadPreflightConfirm = document.querySelector("[data-upload-preflight-confirm]");
  const uploadPreflightCancel = document.querySelector("[data-upload-preflight-cancel]");
  let confirmingUploadPreflight = false;
  const uploadPreflightRetry = document.querySelector("[data-upload-preflight-retry]");
  const uploadSessionKey = uploadForm?.dataset.matterSlug
    ? `case-intelligence:upload:${uploadForm.dataset.matterSlug}`
    : "";
  const uploadChunkBytes = 2 * 1024 * 1024;
  const configuredUploadLimit = (name, fallback) => {
    const value = Number(uploadForm?.dataset[name]);
    return Number.isSafeInteger(value) && value > 0 ? value : fallback;
  };
  const maximumUploadItems = configuredUploadLimit("maxUploadItems", 2000);
  const maximumUploadBatchItems = configuredUploadLimit("maxUploadBatchItems", 2000);
  const maximumPreflightRequestBytes = configuredUploadLimit("maxPreflightRequestBytes", 6 * 1024 * 1024);
  const maximumUploadFileBytes = configuredUploadLimit("maxDocumentBytes", 25 * 1024 * 1024);
  const maximumMediaFileBytes = configuredUploadLimit("maxMediaBytes", 5 * 1024 * 1024 * 1024);
  const maximumUploadSessionBytes = configuredUploadLimit("maxCollectionBytes", 5 * 1024 * 1024 * 1024);
  const documentLimitLabel = uploadForm?.dataset.documentLimitLabel || "25 MiB";
  const mediaLimitLabel = uploadForm?.dataset.mediaLimitLabel || "5 GiB";
  const collectionLimitLabel = uploadForm?.dataset.collectionLimitLabel || "5 GiB";
  let selectedUploadFiles = [];
  let uploadBatches = [];
  let activeUploadBatchIndex = 0;
  let selectedUploadTotalBytes = 0;
  let activeUpload = null;
  let uploadAbortController = null;
  let uploadProcessingTimer = 0;
  let preflightFiles = [];
  let activePreflight = null;
  let preflightAbortController = null;
  let preflightVersion = 0;
  const securityCheckedSuffixes = new Set(["jpg", "jpeg", "png", "tif", "tiff", "eml", "csv", "tsv", "xlsx"]);
  const requiresSecurityCheck = (name) => {
    const normalized = String(name || "").toLowerCase();
    const dot = normalized.lastIndexOf(".");
    return dot >= 0 && securityCheckedSuffixes.has(normalized.slice(dot + 1));
  };

  if (folderInput && folderChooser) {
    folderInput.removeAttribute("hidden");
    folderInput.removeAttribute("disabled");
    folderInput.removeAttribute("tabindex");
    folderChooser.hidden = false;
  }

  const formatBytes = (value) => {
    if (!Number.isFinite(value) || value <= 0) return "0 B";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
    const amount = value / (1024 ** index);
    return `${amount >= 10 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
  };

  const readUploadJson = async (response) => {
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(payload.message || payload.detail || "The upload could not continue.");
      error.status = response.status;
      error.code = String(payload.code || "");
      throw error;
    }
    if (payload.delta && activeUpload?.upload_session_id === payload.upload_session_id) {
      const changed = payload.items?.[0];
      const items = changed
        ? (activeUpload.items || []).map((item) => (
          item.upload_item_id === changed.upload_item_id ? { ...item, ...changed } : item
        ))
        : (activeUpload.items || []);
      return { ...activeUpload, ...payload, items, delta: false };
    }
    return payload;
  };

  const preflightStateLabels = {
    valid: "Ready",
    needs_attention: "Needs attention",
    unsupported: "Unsupported",
    duplicate_candidate: "Repeated path",
    over_limit: "Over limit",
    failed: "Cannot use",
  };

  const revealUploadPreflight = () => {
    if (!uploadPreflight || uploadPreflight.hidden) return;
    window.requestAnimationFrame(() => {
      uploadPreflight.scrollIntoView({
        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
        block: "start",
      });
    });
  };

  const renderUploadPreflight = (preview) => {
    if (!uploadPreflight || !preview) return;
    uploadPreflight.hidden = false;
    const eligible = Array.isArray(preview.eligible_indexes) ? preview.eligible_indexes.length : 0;
    const selected = Number(preview.selected_count || 0);
    const counts = preview.counts || {};
    if (uploadPreflightState) {
      uploadPreflightState.textContent = preview.status === "ready"
        ? "Ready to confirm"
        : preview.status === "partial"
          ? "Some files need attention"
          : "Nothing ready yet";
    }
    if (uploadPreflightStatus) {
      uploadPreflightStatus.textContent = preview.status === "ready"
        ? `${selected.toLocaleString()} selected ${selected === 1 ? "file is" : "files are"} ready. Nothing has been copied yet.`
        : preview.status === "partial"
          ? `${eligible.toLocaleString()} of ${selected.toLocaleString()} selected files can proceed. Files needing attention will stay visible and will not be uploaded.`
          : `None of the ${selected.toLocaleString()} selected files can proceed. Nothing has been copied.`;
    }
    if (uploadPreflightCounts) {
      const labels = [
        ["valid", "ready"],
        ["needs_attention", "need attention"],
        ["unsupported", "unsupported"],
        ["duplicate_candidate", "repeated paths"],
        ["over_limit", "over limit"],
        ["failed", "cannot use"],
      ];
      uploadPreflightCounts.replaceChildren();
      labels.forEach(([key, label]) => {
        const count = Number(counts[key] || 0);
        const item = document.createElement("span");
        item.textContent = `${count.toLocaleString()} ${label}`;
        uploadPreflightCounts.append(item);
      });
    }
    if (uploadPreflightItems) {
      uploadPreflightItems.replaceChildren();
      (preview.items || []).forEach((item) => {
        const row = document.createElement("li");
        row.className = "upload-preflight-item";
        row.dataset.state = String(item.state || "failed");

        const identity = document.createElement("div");
        const name = document.createElement("strong");
        name.textContent = String(item.display_path || item.display_name || "Selected file");
        const state = document.createElement("span");
        state.className = "upload-preflight-item-state";
        state.textContent = preflightStateLabels[item.state] || "Cannot use";
        identity.append(name, state);

        const copy = document.createElement("div");
        copy.className = "upload-preflight-item-copy";
        const facts = document.createElement("span");
        const expected = item.expected_type
          ? `Expected from filename: ${item.expected_type}`
          : "Expected type: unavailable";
        const supplied = item.supplied_type ? `Supplied by browser: ${item.supplied_type}` : "Browser type not supplied";
        facts.textContent = `${formatBytes(Number(item.size || 0))} · ${expected} · ${supplied}`;
        const pending = document.createElement("span");
        const scan = item.scan || {};
        const scanText = scan.required
          ? scan.capability === "ready"
            ? "Security scan required and not run yet"
            : "Security scan required, unavailable, and not run"
          : "Security scan not required";
        pending.textContent = `Detected type, readability, source version, and content duplicates pending upload · ${scanText}`;
        const message = document.createElement("span");
        message.textContent = String(item.message || "Choose this file again.");
        copy.append(facts, pending, message);
        row.append(identity, copy);
        uploadPreflightItems.append(row);
      });
    }
    if (uploadPreflightConfirm) {
      uploadPreflightConfirm.textContent = eligible ? `Upload ${eligible.toLocaleString()} ready ${eligible === 1 ? "file" : "files"}` : "Save selection receipt";
      uploadPreflightConfirm.disabled = selected === 0;
    }
    if (uploadPreflightRetry) uploadPreflightRetry.hidden = true;
    revealUploadPreflight();
  };

  const preflightDescriptor = (file) => ({
    name: file.name,
    relative_path: file.webkitRelativePath || file.name,
    size: file.size,
    media_type: file.type,
  });

  let memoryUploadCheckpoint = null;
  const storedUploadCheckpoint = () => {
    const fingerprintPattern = /^[0-9a-f]{64}$/;
    const sessionPattern = /^upload-session-[0-9a-f]{32}$/;
    const collectionPattern = /^source-collection-[0-9a-f]{32}$/;
    if (!uploadSessionKey) return null;
    try {
      let parsed = memoryUploadCheckpoint;
      try { parsed = JSON.parse(window.localStorage.getItem(uploadSessionKey) || "null"); } catch (_error) { /* retain same-page recovery when storage is unavailable */ }
      const sessionId = String(parsed?.session_id || "");
      const collectionId = String(parsed?.collection_id || "");
      const identifiersValid = sessionPattern.test(sessionId) && collectionPattern.test(collectionId);
      if (
        parsed?.version === 3
        && fingerprintPattern.test(String(parsed.plan_fingerprint || ""))
        && identifiersValid
        && Number.isSafeInteger(parsed.batch_index)
        && parsed.batch_index >= 0
      ) {
        return {
          version: 3,
          plan_fingerprint: String(parsed.plan_fingerprint),
          session_id: sessionId,
          collection_id: collectionId,
          batch_index: parsed.batch_index,
        };
      }
      const eligibleIndexes = parsed?.eligible_indexes;
      if (
        parsed?.version === 4
        && fingerprintPattern.test(String(parsed.raw_selection_fingerprint || ""))
        && fingerprintPattern.test(String(parsed.structure_fingerprint || ""))
        && fingerprintPattern.test(String(parsed.plan_fingerprint || ""))
        && identifiersValid
        && Array.isArray(eligibleIndexes)
        && eligibleIndexes.length > 0
        && eligibleIndexes.length <= maximumUploadItems
        && eligibleIndexes.every((index, offset) => (
          Number.isSafeInteger(index)
          && index >= 0
          && index < maximumUploadItems
          && (offset === 0 || index > eligibleIndexes[offset - 1])
        ))
        && Number.isSafeInteger(parsed.batch_count)
        && parsed.batch_count > 0
        && parsed.batch_count <= maximumUploadItems
        && Number.isSafeInteger(parsed.batch_index)
        && parsed.batch_index >= 0
        && parsed.batch_index < parsed.batch_count
      ) {
        return {
          version: 4,
          raw_selection_fingerprint: String(parsed.raw_selection_fingerprint),
          structure_fingerprint: String(parsed.structure_fingerprint),
          eligible_indexes: eligibleIndexes.slice(),
          plan_fingerprint: String(parsed.plan_fingerprint),
          batch_count: parsed.batch_count,
          session_id: sessionId,
          collection_id: collectionId,
          batch_index: parsed.batch_index,
        };
      }
    } catch (_error) {
      // Untrusted browser storage cannot authorize a capacity credit.
    }
    return null;
  };

  const storedPreflightCheckpoint = () => {
    const checkpoint = storedUploadCheckpoint();
    return checkpoint || { version: 0, session_id: "", collection_id: "" };
  };

  const safeFolderDisplayPath = (relativePath, displayName, pathSafetyValidated) => {
    if (typeof relativePath !== "string" || typeof displayName !== "string") return "";
    if (pathSafetyValidated !== true) return "";
    const normalized = relativePath.normalize("NFC");
    const normalizedName = displayName.normalize("NFC");
    if (
      !normalized.includes("/")
      || normalized.startsWith("/")
      || normalized.startsWith("\\")
      || normalized.includes("\\")
      || normalized.includes(":")
    ) return "";
    const parts = normalized.split("/");
    if (parts.some((part) => !part || part === "." || part === "..")) return "";
    return parts[parts.length - 1] === normalizedName ? normalized : "";
  };

  const validatedScannerCapability = (item) => {
    const scan = item?.scan;
    const invalid = () => {
      throw new Error("The selection review returned an incomplete security check.");
    };
    if (!scan || typeof scan !== "object" || Array.isArray(scan) || typeof scan.required !== "boolean") {
      return invalid();
    }
    if ((item.state === "valid") !== (item.eligible === true)) return invalid();
    if (scan.required) {
      if (!["ready", "unavailable"].includes(scan.capability) || scan.result !== "not_run") {
        return invalid();
      }
      if (
        (item.state === "valid" && scan.capability !== "ready")
        || (item.state === "needs_attention" && scan.capability !== "unavailable")
      ) return invalid();
      return scan.capability;
    }
    if (
      scan.capability !== "not_required"
      || !["not_required", "not_run"].includes(scan.result)
      || item.state === "needs_attention"
      || (item.state === "valid" && scan.result !== "not_required")
    ) return invalid();
    return "";
  };

  const scannerSnapshotCapability = (responses) => {
    let capability = "";
    for (const response of responses) {
      if (!Array.isArray(response?.items)) continue;
      for (const item of response.items) {
        const candidate = validatedScannerCapability(item);
        if (!capability && candidate) capability = candidate;
      }
    }
    return capability;
  };

  const applyScannerSnapshot = (item, capability) => {
    const itemCapability = validatedScannerCapability(item);
    const wasDuplicateCandidate = item.state === "duplicate_candidate";
    if (!itemCapability) {
      if (!wasDuplicateCandidate) return item;
      return {
        ...item,
        state: "valid",
        eligible: true,
        duplicate: "not_evaluated",
        message: "Ready to upload. File contents will be checked after transfer.",
      };
    }
    if (!capability) {
      throw new Error("The selection review returned an incomplete security check.");
    }
    const normalized = {
      ...item,
      scan: { ...item.scan, capability, result: "not_run" },
    };
    if (!["valid", "needs_attention", "duplicate_candidate"].includes(normalized.state)) return normalized;
    if (capability === "ready") {
      normalized.state = "valid";
      normalized.eligible = true;
      normalized.duplicate = "not_evaluated";
      normalized.message = "Ready to upload. File contents will be checked after transfer.";
    } else {
      normalized.state = "needs_attention";
      normalized.eligible = false;
      normalized.message = "A security scan is required but has not run because scanning is unavailable. Retry when security checks are ready.";
    }
    return normalized;
  };

  const localPreflightFailure = (index) => ({
    index,
    display_name: `Selected file ${index + 1}`,
    state: "failed",
    eligible: false,
    supplied_type: null,
    expected_type: null,
    detected_type: null,
    size: null,
    readability: "pending_upload",
    source_version: "pending_upload",
    scan: { required: false, capability: "not_required", result: "not_run" },
    duplicate: "not_evaluated",
    message: "This selection entry is too large to review safely. Choose it again.",
  });

  const selectionNonce = () => {
    if (!window.crypto?.getRandomValues) {
      throw new Error("Selection review is unavailable in this browser.");
    }
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  };

  const sha256Fingerprint = async (value) => {
    if (!window.crypto?.subtle || typeof TextEncoder === "undefined") return "";
    try {
      const digest = await window.crypto.subtle.digest(
        "SHA-256",
        new TextEncoder().encode(JSON.stringify(value)),
      );
      return Array.from(
        new Uint8Array(digest),
        (byte) => byte.toString(16).padStart(2, "0"),
      ).join("");
    } catch (_error) {
      return "";
    }
  };

  const normalizedFileBinding = (file) => ({
    name: String(file.name || "").normalize("NFC"),
    relative_path: String(file.webkitRelativePath || file.name || "").normalize("NFC"),
    size: Number(file.size),
    media_type: String(file.type || "").trim().toLowerCase(),
    last_modified: Number(file.lastModified),
  });

  const rawSelectionFingerprint = (files) => sha256Fingerprint({
    version: 1,
    files: Array.from(files, normalizedFileBinding),
  });

  const buildPreflightBatches = (files, nonce, checkpoint) => {
    const encoder = new TextEncoder();
    const context = { selection_nonce: nonce };
    if (checkpoint.session_id && checkpoint.collection_id) {
      context.checkpoint_session_id = checkpoint.session_id;
      context.checkpoint_collection_id = checkpoint.collection_id;
    }
    const serializedContext = JSON.stringify(context);
    const prefix = `${serializedContext.slice(0, -1)},"files":[`;
    const suffix = "]}";
    const emptyBytes = encoder.encode(prefix + suffix).byteLength;
    const batches = [];
    const failures = new Map();
    let entries = [];
    let bodyBytes = emptyBytes;
    const flush = () => {
      if (!entries.length) return;
      const body = prefix + entries.map((entry) => entry.serialized).join(",") + suffix;
      batches.push({ entries, body, bytes: encoder.encode(body).byteLength });
      entries = [];
      bodyBytes = emptyBytes;
    };
    files.forEach((file, index) => {
      const descriptor = preflightDescriptor(file);
      const serialized = JSON.stringify(descriptor);
      const descriptorBytes = encoder.encode(serialized).byteLength;
      const addition = descriptorBytes + (entries.length ? 1 : 0);
      if (
        entries.length
        && (entries.length >= maximumUploadBatchItems || bodyBytes + addition > maximumPreflightRequestBytes)
      ) {
        flush();
      }
      if (emptyBytes + descriptorBytes > maximumPreflightRequestBytes) {
        failures.set(index, localPreflightFailure(index));
        return;
      }
      entries.push({
        index,
        serialized,
        folderRelativePath: file.webkitRelativePath ? descriptor.relative_path : "",
      });
      bodyBytes += descriptorBytes + (entries.length > 1 ? 1 : 0);
    });
    flush();
    return { batches, failures };
  };

  const validatedMatterCapacity = (response) => {
    const capacity = response?.matter_capacity;
    const quota = capacity?.quota_bytes;
    const used = capacity?.used_bytes;
    const totalReserved = capacity?.total_reserved_bytes;
    const freshAvailable = capacity?.fresh_available_bytes;
    const checkpointValidated = capacity?.checkpoint_validated;
    const checkpointRemaining = capacity?.checkpoint_remaining_bytes;
    const otherReserved = capacity?.other_reserved_bytes;
    const available = capacity?.available_bytes;
    if (
      !capacity
      || typeof capacity !== "object"
      || Array.isArray(capacity)
      || capacity.version !== 2
      || !Number.isSafeInteger(quota)
      || quota <= 0
      || !Number.isSafeInteger(used)
      || used < 0
      || !Number.isSafeInteger(totalReserved)
      || totalReserved < 0
      || !Number.isSafeInteger(freshAvailable)
      || freshAvailable < 0
      || typeof checkpointValidated !== "boolean"
      || !Number.isSafeInteger(checkpointRemaining)
      || checkpointRemaining < 0
      || checkpointRemaining > totalReserved
      || !Number.isSafeInteger(otherReserved)
      || otherReserved < 0
      || !Number.isSafeInteger(available)
      || available < 0
      || freshAvailable !== Math.max(quota - used - totalReserved, 0)
      || (!checkpointValidated && checkpointRemaining !== 0)
      || otherReserved !== totalReserved - checkpointRemaining
      || available !== Math.max(quota - used - otherReserved, 0)
    ) {
      throw new Error("The selection review returned an incomplete matter-capacity check.");
    }
    return {
      available,
      checkpointRemaining,
      checkpointValidated,
      freshAvailable,
    };
  };

  const consolidatePreflight = async (files, batches, responses, failures, checkpoint) => {
    const selectedCount = files.length;
    const items = Array(selectedCount);
    const scannerCapability = scannerSnapshotCapability(responses);
    const capacitySnapshots = responses.map(validatedMatterCapacity);
    const capacity = {
      available: capacitySnapshots.length
        ? Math.min(...capacitySnapshots.map((snapshot) => snapshot.available))
        : 0,
      checkpointRemaining: capacitySnapshots.length
        ? Math.max(...capacitySnapshots.map((snapshot) => snapshot.checkpointRemaining))
        : 0,
      checkpointValidated: capacitySnapshots.length > 0
        && capacitySnapshots.every((snapshot) => snapshot.checkpointValidated),
      freshAvailable: capacitySnapshots.length
        ? Math.min(...capacitySnapshots.map((snapshot) => snapshot.freshAvailable))
        : 0,
    };
    failures.forEach((item, index) => { items[index] = item; });
    batches.forEach((batch, batchIndex) => {
      const responseItems = responses[batchIndex]?.items;
      if (!Array.isArray(responseItems) || responseItems.length !== batch.entries.length) {
        throw new Error("The selection review returned an incomplete result.");
      }
      responseItems.forEach((item, localIndex) => {
        if (!item || item.index !== localIndex) {
          throw new Error("The selection review returned files out of order.");
        }
        if (
          typeof item.path_safety_validated !== "boolean"
          || (item.path_safety_validated !== true && item.state !== "failed")
        ) {
          throw new Error("The selection review returned an incomplete path check.");
        }
        const entry = batch.entries[localIndex];
        const index = entry.index;
        const displayPath = safeFolderDisplayPath(
          entry.folderRelativePath,
          item.display_name,
          item.path_safety_validated,
        );
        const normalizedItem = { ...item, index };
        delete normalizedItem.display_path;
        const normalized = applyScannerSnapshot(normalizedItem, scannerCapability);
        if (displayPath) normalized.display_path = displayPath;
        items[index] = normalized;
      });
    });
    if (Array.from(items).some((item) => !item)) {
      throw new Error("The selection review returned an incomplete result.");
    }
    const tokens = [];
    const tokenGroups = new Map();
    const duplicateGroups = [];
    items.forEach((item, index) => {
      const token = typeof item.duplicate_token === "string"
        && /^[0-9a-f]{64}$/.test(item.duplicate_token)
        ? item.duplicate_token
        : "";
      if (["valid", "needs_attention", "duplicate_candidate", "over_limit"].includes(item.state) && !token) {
        throw new Error("The selection review returned an incomplete duplicate check.");
      }
      tokens[index] = token;
      if (token && !tokenGroups.has(token)) tokenGroups.set(token, tokenGroups.size);
      duplicateGroups[index] = token ? tokenGroups.get(token) : -1;
    });
    const [rawFingerprint, structureFingerprint] = await Promise.all([
      rawSelectionFingerprint(files),
      sha256Fingerprint({
        version: 1,
        items: items.map((item, index) => ({
          index,
          state: String(item.state || "failed"),
          eligible: item.eligible === true,
          size: Number.isSafeInteger(item.size) ? item.size : null,
          path_safety_validated: item.path_safety_validated === true,
          scan: {
            required: item.scan?.required === true,
            capability: String(item.scan?.capability || ""),
            result: String(item.scan?.result || ""),
          },
          duplicate_group: duplicateGroups[index],
        })),
      }),
    ]);
    let frozenIndexes = null;
    const checkpointStructureMatches = Boolean(
      checkpoint?.version === 4
      && rawFingerprint
      && structureFingerprint
      && checkpoint.raw_selection_fingerprint === rawFingerprint
      && checkpoint.structure_fingerprint === structureFingerprint
    );
    if (checkpointStructureMatches) {
      const candidateIndexes = checkpoint.eligible_indexes;
      if (
        candidateIndexes.some((index) => index >= selectedCount)
        || candidateIndexes.some((index) => (
          items[index]?.state !== "valid" || items[index]?.eligible !== true
        ))
      ) {
        throw new Error("The saved reviewed upload plan no longer matches this selection.");
      }
      const candidateFiles = candidateIndexes.map((index) => files[index]);
      const candidateBatches = buildUploadBatches(candidateFiles);
      const candidatePlanFingerprint = await uploadPlanFingerprint(candidateBatches);
      if (
        !candidatePlanFingerprint
        || candidatePlanFingerprint !== checkpoint.plan_fingerprint
        || candidateBatches.length !== checkpoint.batch_count
        || checkpoint.batch_index >= candidateBatches.length
      ) {
        throw new Error("The saved reviewed upload plan no longer matches this selection.");
      }
      const seenFrozenGroups = new Set();
      for (const index of candidateIndexes) {
        const group = duplicateGroups[index];
        if (group >= 0 && seenFrozenGroups.has(group)) {
          throw new Error("The saved reviewed upload plan contains a repeated path.");
        }
        if (group >= 0) seenFrozenGroups.add(group);
      }
      if (capacity.checkpointValidated) {
        const currentBatchBytes = candidateBatches[checkpoint.batch_index]
          .reduce((sum, file) => sum + Number(file.size || 0), 0);
        const laterBatchBytes = candidateBatches
          .slice(checkpoint.batch_index + 1)
          .reduce(
            (sum, batch) => sum + batch.reduce(
              (batchSum, file) => batchSum + Number(file.size || 0),
              0,
            ),
            0,
          );
        const remainingPlanBytes = capacity.checkpointRemaining + laterBatchBytes;
        if (
          !Number.isSafeInteger(currentBatchBytes)
          || !Number.isSafeInteger(laterBatchBytes)
          || !Number.isSafeInteger(remainingPlanBytes)
          || capacity.checkpointRemaining > currentBatchBytes
          || laterBatchBytes > capacity.freshAvailable
          || remainingPlanBytes > capacity.available
        ) {
          throw new Error("The saved reviewed upload plan no longer fits this matter.");
        }
        frozenIndexes = candidateIndexes;
      }
    }
    const counts = Object.fromEntries(Object.keys(preflightStateLabels).map((state) => [state, 0]));
    const eligibleIndexes = [];
    const seenTokens = new Set();
    const frozenSet = frozenIndexes ? new Set(frozenIndexes) : null;
    const frozenGroups = frozenIndexes
      ? new Set(frozenIndexes.map((index) => duplicateGroups[index]).filter((group) => group >= 0))
      : null;
    let remainingCapacity = checkpoint?.version === 4 && !frozenSet
      ? capacity.freshAvailable
      : capacity.available;
    items.forEach((item, index) => {
      const token = tokens[index];
      if (item.state === "valid" && item.eligible === true) {
        if (!Number.isSafeInteger(item.size) || item.size <= 0) {
          throw new Error("The selection review returned an incomplete matter-capacity check.");
        }
        if (frozenSet && !frozenSet.has(index)) {
          item.state = frozenGroups.has(duplicateGroups[index])
            ? "duplicate_candidate"
            : "over_limit";
          item.eligible = false;
          item.message = item.state === "duplicate_candidate"
            ? "This relative path appears more than once in the selection. Keep one copy or rename it before upload."
            : "This file remains outside the saved capacity-limited upload plan. Finish or release that plan before changing its reviewed files.";
        } else if (!frozenSet && item.size > remainingCapacity) {
          item.state = "over_limit";
          item.eligible = false;
          item.message = `This matter has ${formatBytes(remainingCapacity)} of upload capacity remaining. Remove unneeded sources or choose another matter; smaller later files may still fit.`;
        } else if (token && seenTokens.has(token)) {
          item.state = "duplicate_candidate";
          item.eligible = false;
          item.duplicate = "selection_collision";
          item.message = "This relative path appears more than once in the selection. Keep one copy or rename it before upload.";
        } else if (token) {
          seenTokens.add(token);
          if (!frozenSet) remainingCapacity -= item.size;
        }
      }
      delete item.duplicate_token;
      if (!(item.state in counts)) item.state = "failed";
      counts[item.state] += 1;
      if (item.state === "valid" && item.eligible === true) eligibleIndexes.push(index);
      else item.eligible = false;
    });
    if (
      checkpointStructureMatches
      && !frozenSet
      && (
        eligibleIndexes.length !== checkpoint.eligible_indexes.length
        || eligibleIndexes.some(
          (index, offset) => index !== checkpoint.eligible_indexes[offset],
        )
      )
    ) {
      throw new Error("The saved reviewed upload plan no longer fits this matter.");
    }
    const eligibleFiles = eligibleIndexes.map((index) => files[index]);
    const uploadPlanBatches = buildUploadBatches(eligibleFiles);
    const planFingerprint = await uploadPlanFingerprint(uploadPlanBatches);
    if (
      checkpoint?.version === 3
      && checkpoint.batch_index > 0
      && (!planFingerprint || checkpoint.plan_fingerprint !== planFingerprint)
    ) {
      throw new Error("The saved multi-batch upload cannot be safely resumed from this capacity snapshot.");
    }
    const checkpointBinding = (
      rawFingerprint
      && structureFingerprint
      && planFingerprint
      && eligibleIndexes.length > 0
      && uploadPlanBatches.length > 0
    ) ? {
        raw_selection_fingerprint: rawFingerprint,
        structure_fingerprint: structureFingerprint,
        eligible_indexes: eligibleIndexes.slice(),
        plan_fingerprint: planFingerprint,
        batch_count: uploadPlanBatches.length,
      }
      : null;
    return {
      status: eligibleIndexes.length === 0
        ? "blocked"
        : eligibleIndexes.length === selectedCount
          ? "ready"
          : "partial",
      selected_count: selectedCount,
      counts,
      eligible_indexes: eligibleIndexes,
      items,
      checkpoint_binding: checkpointBinding,
    };
  };

  const previewSelectedFiles = async (files) => {
    if (!uploadForm?.dataset.preflightUrl || !uploadPreflight || confirmingUploadPreflight) return;
    const priorReceiptLink = document.querySelector('[data-intake-receipt-link]');
    if (priorReceiptLink) priorReceiptLink.hidden = true;
    document.querySelector('[data-intake-receipt-open]')?.removeAttribute('href');
    const version = preflightVersion + 1;
    preflightVersion = version;
    preflightAbortController?.abort();
    const controller = new AbortController();
    preflightAbortController = controller;
    preflightFiles = Array.from(files || []);
    activePreflight = null;
    uploadPreflight.hidden = false;
    uploadDrop?.classList.remove("upload-error");
    if (uploadPreflightCounts) uploadPreflightCounts.replaceChildren();
    if (uploadPreflightItems) uploadPreflightItems.replaceChildren();
    if (uploadPreflightConfirm) {
      uploadPreflightConfirm.disabled = true;
      uploadPreflightConfirm.textContent = "Upload 0 ready files";
    }
    if (uploadPreflightRetry) uploadPreflightRetry.hidden = true;
    if (!preflightFiles.length) {
      uploadPreflight.removeAttribute("aria-busy");
      if (uploadPreflightState) uploadPreflightState.textContent = "Waiting for a selection";
      if (uploadPreflightStatus) uploadPreflightStatus.textContent = "Choose files to see what can proceed.";
      if (fileSummary) fileSummary.textContent = "Only records you deliberately select are copied into this temporary matter.";
      return;
    }
    if (fileSummary) {
      fileSummary.textContent = `${preflightFiles.length.toLocaleString()} selected ${preflightFiles.length === 1 ? "file" : "files"} · reviewing before upload`;
    }
    uploadPreflight.setAttribute("aria-busy", "true");
    if (uploadPreflightState) uploadPreflightState.textContent = "Reviewing selection";
    if (uploadPreflightStatus) uploadPreflightStatus.textContent = "Checking filenames, sizes, format support, and required capabilities. No file bytes are being copied.";
    try {
      if (preflightFiles.length > maximumUploadItems) {
        throw new Error(`Choose no more than ${maximumUploadItems.toLocaleString()} items in one collection.`);
      }
      const checkpoint = storedPreflightCheckpoint();
      const { batches, failures } = buildPreflightBatches(
        preflightFiles,
        selectionNonce(),
        checkpoint,
      );
      const responses = [];
      for (const batch of batches) {
        if (version !== preflightVersion) return;
        const response = await fetch(uploadForm.dataset.preflightUrl, {
          method: "POST",
          headers: {
            Accept: "application/json",
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
          },
          body: batch.body,
          cache: "no-store",
          signal: controller.signal,
        });
        responses.push(await readUploadJson(response));
      }
      if (version !== preflightVersion) return;
      const preview = await consolidatePreflight(
        preflightFiles,
        batches,
        responses,
        failures,
        checkpoint,
      );
      if (version !== preflightVersion) return;
      activePreflight = preview;
      renderUploadPreflight(preview);
      const eligible = preview.eligible_indexes.length;
      const total = preflightFiles.reduce((sum, file) => sum + Number(file.size || 0), 0);
      if (fileSummary) fileSummary.textContent = `${preflightFiles.length.toLocaleString()} selected · ${eligible.toLocaleString()} ready · ${formatBytes(total)}`;
    } catch (error) {
      if (error.name === "AbortError" || version !== preflightVersion) return;
      uploadDrop?.classList.add("upload-error");
      if (uploadPreflightState) uploadPreflightState.textContent = "Selection review paused";
      if (uploadPreflightStatus) uploadPreflightStatus.textContent = `${error.message} Your selection is still here; retry before uploading.`;
      if (uploadPreflightRetry) uploadPreflightRetry.hidden = false;
      revealUploadPreflight();
    } finally {
      if (version === preflightVersion) uploadPreflight.removeAttribute("aria-busy");
    }
  };

  const renderUpload = (session, errorMessage = "") => {
    if (!session || !uploadProgress) return;
    activeUpload = session;
    uploadProgress.hidden = false;
    const queued = Number(session.queued_count || 0);
    const failed = Number(session.failed_count || 0);
    const processing = Number(session.processing_count || 0);
    const ready = Number(session.ready_count || 0);
    const playbackOnly = Number(session.playback_only_count || 0);
    const attention = Number(session.attention_count || 0);
    const total = Number(session.item_count || 0);
    const received = Number(session.received_bytes || 0);
    const totalBytes = Number(session.total_bytes || 0);
    const batchCount = Math.max(uploadBatches.length, 1);
    const batchNumber = Math.min(activeUploadBatchIndex + 1, batchCount);
    const batchPrefix = batchCount > 1 ? `Batch ${batchNumber} of ${batchCount} · ` : "";
    const completedBeforeItems = uploadBatches
      .slice(0, activeUploadBatchIndex)
      .reduce((sum, batch) => sum + batch.length, 0);
    const completedBeforeBytes = uploadBatches
      .slice(0, activeUploadBatchIndex)
      .reduce((sum, batch) => sum + batch.reduce((batchSum, file) => batchSum + file.size, 0), 0);
    const intakeItems = uploadBatches.reduce((sum, batch) => sum + batch.length, 0) || total;
    const intakeReceived = completedBeforeBytes + received;
    const terminal = ["complete", "partial", "cancelled"].includes(session.state);
    const finalBatch = activeUploadBatchIndex >= batchCount - 1;
    const finalizing = (session.items || []).some((item) => item.state === "uploaded");
    const securityChecking = (session.items || []).filter((item) => item.state === "uploaded" && requiresSecurityCheck(item.relative_path));
    const activeWork = (session.items || []).find((item) => ["queued", "processing"].includes(item.work_state));
    const workProgress = total
      ? (session.items || []).reduce((sum, item) => sum + Math.max(0, Math.min(1, Number(item.work_progress || 0))), 0) / total
      : 0;
    const percent = batchCount > 1
      ? selectedUploadTotalBytes
        ? Math.min(100, (intakeReceived / selectedUploadTotalBytes) * 100)
        : 0
      : terminal
        ? Math.min(100, workProgress * 100)
        : totalBytes
          ? Math.min(100, (received / totalBytes) * 100)
          : 0;
    if (uploadProgressBar) uploadProgressBar.style.width = `${percent}%`;
    if (uploadCount) {
      uploadCount.textContent = batchCount > 1
        ? `${batchPrefix}${Math.min(completedBeforeItems + queued + failed, intakeItems).toLocaleString()} of ${intakeItems.toLocaleString()} saved${failed ? ` · ${failed} need attention in this batch` : ""}`
        : finalizing
          ? `${total} uploaded · finalizing`
          : terminal
            ? `${ready} ready · ${processing} processing${playbackOnly ? ` · ${playbackOnly} playback only` : ""}${attention ? ` · ${attention} need attention` : ""}`
            : `${queued} of ${total} queued${failed ? ` · ${failed} need attention` : ""}`;
    }
    if (uploadBytes) {
      uploadBytes.textContent = batchCount > 1
        ? `${formatBytes(intakeReceived)} of ${formatBytes(selectedUploadTotalBytes)}`
        : terminal
          ? `Upload complete · ${formatBytes(totalBytes)} saved`
          : `${formatBytes(received)} of ${formatBytes(totalBytes)}`;
    }
    if (uploadTitle) {
      uploadTitle.textContent = batchCount > 1 && terminal && finalBatch
        ? `${intakeItems.toLocaleString()} sources saved`
        : securityChecking.length
        ? "Security checking uploads"
        : finalizing
        ? "Finalizing upload collection"
        : processing
        ? session.contains_media ? "Transcription in progress" : "Source processing in progress"
        : ready === total && total
          ? session.contains_media ? "Transcript ready" : "Sources ready"
          : attention
            ? "Upload collection needs attention"
            : playbackOnly && ready + playbackOnly === total
              ? "Sources available for review"
            : session.state === "complete"
              ? "Upload collection queued"
        : session.state === "partial"
          ? "Upload collection needs attention"
          : "Uploading selected records";
    }
    if (uploadStatus) {
      uploadStatus.textContent = errorMessage || (
        batchCount > 1 && terminal && finalBatch
          ? "The full collection is durable. Source processing continues in the background while you review the matter."
          : securityChecking.length
          ? `${securityChecking.length.toLocaleString()} ${securityChecking.length === 1 ? "record is" : "records are"} being scanned before entering the matter. Large files may take a moment.`
          : finalizing
          ? "The selected records are saved. Checking them and preparing their processing tasks."
          : session.work_complete && (playbackOnly || attention)
            ? "Processing finished. Open sources to review available items and choose any needed next steps."
          : activeWork
          ? `${activeWork.work_stage || "Processing"}${activeWork.work_progress ? ` · ${Math.round(Number(activeWork.work_progress) * 100)}%` : ""}. You may leave this page; the saved work will continue.`
          : ready === total && total
            ? session.contains_media
              ? "The transcript is searchable and ready to review with the recording."
              : "Every source is searchable and ready to review."
            : session.state === "complete"
          ? "Every source is saved in the durable processing queue."
          : session.state === "partial"
            ? "Usable sources were retained. Review the items that could not be added."
            : "You may keep working in another matter; this saved collection can resume after a connection interruption."
      );
    }
    const failures = (session.items || []).filter((item) => item.work_state === "attention" || ["failed", "cancelled"].includes(item.state));
    if (uploadErrors) {
      uploadErrors.replaceChildren();
      failures.slice(0, 10).forEach((item) => {
        const row = document.createElement("li");
        row.textContent = `${item.relative_path}: ${item.work_stage || item.message || "Processing did not finish."}`;
        uploadErrors.append(row);
      });
      if (failures.length > 10) {
        const row = document.createElement("li");
        row.textContent = `${failures.length - 10} additional item(s) need attention.`;
        uploadErrors.append(row);
      }
      uploadErrors.hidden = failures.length === 0;
    }
    if (uploadCancel) uploadCancel.hidden = terminal;
    if (uploadReview) {
      uploadReview.textContent = processing && session.contains_media
        ? "View transcription status"
        : ready && session.contains_media
          ? "Review transcript"
          : "Review uploaded sources";
      if (session.primary_review_url || session.library_url) {
        uploadReview.href = session.primary_review_url || session.library_url;
      }
      uploadReview.hidden = !session.review_ready;
    }
  };

  const uploadStatusRefresh = async (compact = true) => {
    if (!activeUpload?.status_url) return activeUpload;
    const separator = activeUpload.status_url.includes("?") ? "&" : "?";
    const target = compact
      ? `${activeUpload.status_url}${separator}compact=1`
      : activeUpload.status_url;
    const response = await fetch(target, {
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal: uploadAbortController?.signal,
    });
    const session = await readUploadJson(response);
    renderUpload(session);
    return session;
  };

  const uploadOneFile = async (file, ordinal) => {
    let item = activeUpload.items.find((candidate) => candidate.ordinal === ordinal);
    if (!item || ["queued", "failed", "cancelled"].includes(item.state)) return;
    let offset = Number(item.received_size || 0);
    let failures = 0;
    while (offset < file.size) {
      const body = file.slice(offset, Math.min(offset + uploadChunkBytes, file.size));
      try {
        const response = await fetch(item.chunk_url, {
          method: "PUT",
          body,
          headers: {
            Accept: "application/json",
            "Content-Type": "application/octet-stream",
            "X-CSRF-Token": csrfToken,
            "X-Upload-Offset": String(offset),
          },
          signal: uploadAbortController.signal,
        });
        activeUpload = await readUploadJson(response);
        item = activeUpload.items.find((candidate) => candidate.ordinal === ordinal);
        offset = Number(item.received_size || 0);
        failures = 0;
        renderUpload(activeUpload);
      } catch (error) {
        if (error.name === "AbortError") throw error;
        failures += 1;
        try {
          activeUpload = await uploadStatusRefresh();
          item = activeUpload.items.find((candidate) => candidate.ordinal === ordinal);
          offset = Number(item?.received_size || offset);
        } catch (_statusError) {
          // Retrying the exact offset is safe; the server will return its saved offset.
        }
        if (failures >= 4) throw error;
        await new Promise((resolve) => window.setTimeout(resolve, failures * 500));
      }
    }
    if (item?.state === "uploaded") {
      let finalizeFailures = 0;
      while (item?.state === "uploaded") {
        try {
          const response = await fetch(item.finalize_url, {
            method: "POST",
            headers: { Accept: "application/json", "X-CSRF-Token": csrfToken },
            signal: uploadAbortController.signal,
          });
          activeUpload = await readUploadJson(response);
          renderUpload(activeUpload);
          return;
        } catch (error) {
          if (error.name === "AbortError") throw error;
          finalizeFailures += 1;
          try {
            // A compact poll intentionally omits item rows. After a finalize
            // rejection, fetch the full session once so a terminal failed or
            // isolated item replaces the browser's stale "uploaded" copy.
            activeUpload = await uploadStatusRefresh(false);
            item = activeUpload.items.find((candidate) => candidate.ordinal === ordinal);
            if (["queued", "failed", "cancelled"].includes(item?.state)) return;
          } catch (_statusError) {
            // The finalization endpoint is retry-safe after a lost response.
          }
          if (finalizeFailures >= 4) throw error;
          await new Promise((resolve) => window.setTimeout(resolve, finalizeFailures * 500));
        }
      }
    }
  };

  const runUploadQueue = async (files) => {
    let nextOrdinal = 1;
    const worker = async () => {
      while (nextOrdinal <= files.length) {
        const ordinal = nextOrdinal;
        nextOrdinal += 1;
        await uploadOneFile(files[ordinal - 1], ordinal);
      }
    };
    await Promise.all(Array.from({ length: Math.min(3, files.length) }, worker));
    activeUpload = await uploadStatusRefresh();
  };

  const pollUploadProcessing = async () => {
    window.clearTimeout(uploadProcessingTimer);
    if (!activeUpload?.status_url || !Number(activeUpload.processing_count || 0)) return;
    try {
      activeUpload = await uploadStatusRefresh();
    } catch (error) {
      if (error.name === "AbortError") return;
      if (uploadStatus) uploadStatus.textContent = "Status reconnecting; the saved preparation is still running.";
    }
    if (Number(activeUpload?.processing_count || 0)) {
      uploadProcessingTimer = window.setTimeout(pollUploadProcessing, 2500);
    }
  };

  const validateSelectedFiles = (files) => {
    if (!files.length) throw new Error("Choose at least one supported file.");
    if (files.length > maximumUploadItems) throw new Error(`Choose no more than ${maximumUploadItems.toLocaleString()} items in one collection.`);
    const paths = new Set();
    let total = 0;
    files.forEach((file) => {
      const path = file.webkitRelativePath || file.name;
      const suffix = file.name.toLocaleLowerCase().split(".").pop();
      const mediaSuffixes = ["wav", "mp3", "m4a", "ogg", "opus", "mp4", "mov", "webm"];
      const reviewSuffixes = ["pdf", "docx", "txt", "jpg", "jpeg", "png", "tif", "tiff", "eml", "csv", "tsv", "xlsx"];
      if (![...reviewSuffixes, ...mediaSuffixes].includes(suffix)) throw new Error(`${file.name} is not a supported review source.`);
      if (!file.size) throw new Error(`${file.name} is empty.`);
      const maximum = mediaSuffixes.includes(suffix) ? maximumMediaFileBytes : maximumUploadFileBytes;
      if (file.size > maximum) throw new Error(`${file.name} is larger than ${mediaSuffixes.includes(suffix) ? mediaLimitLabel : documentLimitLabel}.`);
      const key = path.toLocaleLowerCase();
      if (paths.has(key)) throw new Error(`The selected records contain ${path} twice.`);
      paths.add(key);
      total += file.size;
    });
    return total;
  };

  const buildUploadBatches = (files) => {
    const batches = [];
    let batch = [];
    let batchBytes = 0;
    files.forEach((file) => {
      if (
        batch.length
        && (batch.length >= maximumUploadBatchItems || batchBytes + file.size > maximumUploadSessionBytes)
      ) {
        batches.push(batch);
        batch = [];
        batchBytes = 0;
      }
      batch.push(file);
      batchBytes += file.size;
    });
    if (batch.length) batches.push(batch);
    return batches;
  };

  const clearUploadResumeState = () => {
    memoryUploadCheckpoint = null;
    if (!uploadSessionKey) return;
    try { window.localStorage.removeItem(uploadSessionKey); } catch (_error) { /* no-op */ }
  };

  const uploadPlanFingerprint = async (batches) => {
    const plan = batches.map((batch) => batch.map(normalizedFileBinding));
    return sha256Fingerprint({ version: 1, batches: plan });
  };

  const readUploadResumeState = (planFingerprint, batchCount, checkpointBinding) => {
    const empty = { version: 0, session_id: "", collection_id: "", batch_index: 0, stale: false };
    if (!uploadSessionKey) return empty;
    const parsed = storedUploadCheckpoint();
    if (parsed?.version === 3) {
      return {
        ...parsed,
        stale: (
          !planFingerprint
          || parsed.plan_fingerprint !== planFingerprint
          || parsed.batch_index >= batchCount
        ),
      };
    }
    if (parsed?.version === 4) {
      const bindingMatches = (
        checkpointBinding
        && parsed.raw_selection_fingerprint === checkpointBinding.raw_selection_fingerprint
        && parsed.structure_fingerprint === checkpointBinding.structure_fingerprint
        && parsed.plan_fingerprint === checkpointBinding.plan_fingerprint
        && parsed.batch_count === checkpointBinding.batch_count
        && parsed.eligible_indexes.length === checkpointBinding.eligible_indexes.length
        && parsed.eligible_indexes.every(
          (index, offset) => index === checkpointBinding.eligible_indexes[offset],
        )
      );
      return {
        ...parsed,
        stale: (
          !bindingMatches
          || !planFingerprint
          || parsed.plan_fingerprint !== planFingerprint
          || parsed.batch_count !== batchCount
          || parsed.batch_index >= batchCount
        ),
      };
    }
    clearUploadResumeState();
    return empty;
  };

  const reconcileStaleUploadResumeState = async (resumeState) => {
    if (!resumeState.stale) return resumeState;
    const sessionRoot = String(uploadForm?.dataset.sessionUrl || "").replace(/\/+$/, "");
    const failureMessage = "The earlier interrupted upload could not be released. Retry this selection before starting new work.";
    if (!sessionRoot) throw new Error(failureMessage);
    const sessionUrl = `${sessionRoot}/${encodeURIComponent(resumeState.session_id)}`;
    const fresh = { version: 0, session_id: "", collection_id: "", batch_index: 0, stale: false };
    try {
      const statusResponse = await fetch(`${sessionUrl}?compact=true`, {
        headers: { Accept: "application/json" },
        cache: "no-store",
        signal: uploadAbortController.signal,
      });
      if (statusResponse.status === 404) {
        clearUploadResumeState();
        activeUpload = null;
        return fresh;
      }
      const status = await statusResponse.json().catch(() => ({}));
      if (
        !statusResponse.ok
        || status.upload_session_id !== resumeState.session_id
        || status.collection_id !== resumeState.collection_id
        || !["open", "complete", "partial", "cancelled"].includes(status.state)
      ) {
        throw new Error(failureMessage);
      }
      if (["complete", "partial", "cancelled"].includes(status.state)) {
        clearUploadResumeState();
        activeUpload = null;
        return fresh;
      }
      const cancelResponse = await fetch(
        `${sessionUrl}/cancel`,
        {
          method: "POST",
          headers: { Accept: "application/json", "X-CSRF-Token": csrfToken },
          cache: "no-store",
          signal: uploadAbortController.signal,
        },
      );
      const cancelled = await cancelResponse.json().catch(() => ({}));
      if (
        !cancelResponse.ok
        || cancelled.upload_session_id !== resumeState.session_id
        || cancelled.collection_id !== resumeState.collection_id
        || cancelled.state !== "cancelled"
      ) {
        throw new Error(failureMessage);
      }
    } catch (error) {
      if (error.name === "AbortError") throw error;
      throw new Error(failureMessage);
    }
    clearUploadResumeState();
    activeUpload = null;
    return fresh;
  };

  const writeUploadResumeState = (
    planFingerprint,
    sessionId,
    collectionId,
    batchIndex,
    batchCount,
    checkpointBinding,
  ) => {
    if (!uploadSessionKey || !planFingerprint) return;
    if (
      !/^upload-session-[0-9a-f]{32}$/.test(sessionId)
      || !/^source-collection-[0-9a-f]{32}$/.test(collectionId)
      || !Number.isSafeInteger(batchIndex)
      || batchIndex < 0
    ) {
      clearUploadResumeState();
      return;
    }
    try {
      const shared = {
        plan_fingerprint: planFingerprint,
        session_id: sessionId,
        collection_id: collectionId,
        batch_index: batchIndex,
      };
      const value = (
        checkpointBinding
        && checkpointBinding.plan_fingerprint === planFingerprint
        && checkpointBinding.batch_count === batchCount
      ) ? {
          version: 4,
          raw_selection_fingerprint: checkpointBinding.raw_selection_fingerprint,
          structure_fingerprint: checkpointBinding.structure_fingerprint,
          eligible_indexes: checkpointBinding.eligible_indexes.slice(),
          batch_count: batchCount,
          ...shared,
        }
        : { version: 3, ...shared };
      memoryUploadCheckpoint = value;
      window.localStorage.setItem(uploadSessionKey, JSON.stringify(value));
    } catch (_error) { /* no-op */ }
  };

  const intakeResumeKey = uploadSessionKey ? `${uploadSessionKey}:selection-receipt` : "";
  let memoryIntakeCheckpoint = null;
  const clearIntakeResumeState = () => {
    memoryIntakeCheckpoint = null;
    if (!intakeResumeKey) return;
    try { window.localStorage.removeItem(intakeResumeKey); } catch (_error) { /* the durable receipt remains available */ }
  };
  const intakeCollectionName = (name) => {
    if (typeof name !== "string") throw new Error("Enter a collection name.");
    const normalized = name.normalize("NFC").trim();
    if (!normalized) throw new Error("Enter a collection name.");
    if (Array.from(normalized).length > 160) throw new Error("Use a collection name of 160 characters or fewer.");
    if (/[\p{Cc}\p{Cf}\p{Cs}]/u.test(normalized)) throw new Error("The collection name contains unsupported characters. Edit the name and try again.");
    return normalized;
  };
  const recordConfirmedSelection = async (files, preview, version) => {
    const root = uploadForm?.dataset.intakeUrl;
    if (!root) throw new Error("The selected-file receipt is unavailable. Refresh Sources and try again.");
    const fingerprint = await rawSelectionFingerprint(files);
    if (!fingerprint) throw new Error("This browser cannot save a resumable selection receipt. Use a current browser and try again.");
    const indexes = preview.eligible_indexes.slice();
    let checkpoint = null;
    let saved = memoryIntakeCheckpoint;
    try { saved ||= JSON.parse(window.localStorage.getItem(intakeResumeKey) || "null"); } catch (_error) { /* use same-page checkpoint */ }
    if (saved) {
      try { saved.collection_name = intakeCollectionName(saved.collection_name); }
      catch (_error) { saved = null; clearIntakeResumeState(); }
    }
    {
      if (saved?.version === 1 && /^[0-9a-f]{32}$/.test(saved.selection_key)
        && saved.selection_fingerprint === fingerprint && saved.selected_count === files.length
        && Array.isArray(saved.eligible_indexes) && JSON.stringify(saved.eligible_indexes) === JSON.stringify(indexes)
        && typeof saved.collection_name === "string" && saved.collection_name.length <= 160) checkpoint = saved;
    }
    checkpoint ||= { version: 1, selection_key: selectionNonce(), selection_fingerprint: fingerprint,
      selected_count: files.length, eligible_indexes: indexes,
      collection_name: intakeCollectionName(uploadCollectionName?.value || "Uploaded sources") };
    memoryIntakeCheckpoint = checkpoint;
    if (uploadCollectionName) uploadCollectionName.value = checkpoint.collection_name;
    try { window.localStorage.setItem(intakeResumeKey, JSON.stringify(checkpoint)); } catch (_error) { /* same-page retries remain possible */ }
    const post = async (url, payload) => {
      if (version !== preflightVersion) throw new Error("The selection changed. Review the current files before uploading.");
      const response = await fetch(url, { method: "POST", headers: { Accept: "application/json", "Content-Type": "application/json", "X-CSRF-Token": csrfToken }, body: JSON.stringify(payload), cache: "no-store" });
      return readUploadJson(response);
    };
    let receipt = await post(root, checkpoint);
    const receiptId = receipt.receipt_id;
    if (!/^intake-[0-9a-f]{32}$/.test(receiptId)) throw new Error("The selection receipt could not be verified. Try again.");
    const linkBox = document.querySelector("[data-intake-receipt-link]");
    const link = document.querySelector("[data-intake-receipt-open]");
    if (link && linkBox) {
      // Build the local route from a validated opaque identity.
      link.href = `${root.replace(/\/intake-receipts$/, "/intake")}/${receiptId}`;
      linkBox.hidden = false;
    }
    const descriptors = files.map((file, index) => preview.items[index]?.path_safety_validated
      ? preflightDescriptor(file)
      : { name: "", relative_path: "", size: Number.isSafeInteger(file.size) ? file.size : null,
          media_type: preview.items[index]?.supplied_type || "" });
    const encoder = new TextEncoder();
    let start = 0;
    while (start < files.length) {
      let count = Math.min(maximumUploadBatchItems, files.length - start);
      let payload;
      while (count > 0) {
        payload = { start, files: descriptors.slice(start, start + count),
          reviewed_states: preview.items.slice(start, start + count).map(item => item.state) };
        if (encoder.encode(JSON.stringify(payload)).byteLength <= maximumPreflightRequestBytes) break;
        count = Math.floor(count / 2);
      }
      if (!count) throw new Error("A selection entry is too large to record. Review that entry again.");
      if (uploadPreflightStatus) uploadPreflightStatus.textContent = `Saving selection receipt: ${start.toLocaleString()} of ${files.length.toLocaleString()} files recorded.`;
      receipt = await post(`${root}/${receiptId}/items`, payload);
      start += count;
    }
    receipt = await post(`${root}/${receiptId}/seal`, {});
    if (receipt.state !== "ready" || receipt.recorded_count !== files.length) throw new Error("The selection receipt is incomplete. Try again before uploading.");
    if (version !== preflightVersion) throw new Error("The selection changed. Review the current files before uploading.");
    return { receipt_id: receiptId, ordinals: new Map(files.map((file, ordinal) => [file, ordinal])) };
  };

  const startResumableUpload = async (files, intake, version) => {
    if (!uploadForm?.dataset.sessionUrl) return;
    selectedUploadFiles = Array.from(files);
    const checkpointBinding = activePreflight?.checkpoint_binding || null;
    let totalBytes;
    try {
      totalBytes = validateSelectedFiles(selectedUploadFiles);
    } catch (error) {
      if (fileSummary) fileSummary.textContent = error.message;
      uploadDrop?.classList.add("upload-error");
      return;
    }
    uploadBatches = buildUploadBatches(selectedUploadFiles);
    const planFingerprint = await uploadPlanFingerprint(uploadBatches);
    selectedUploadTotalBytes = totalBytes;
    const names = selectedUploadFiles.slice(0, 3).map((file) => file.name);
    const remainder = selectedUploadFiles.length - names.length;
    if (fileSummary) fileSummary.textContent = `${names.join(", ")}${remainder > 0 ? ` and ${remainder.toLocaleString()} more` : ""} · ${formatBytes(totalBytes)}`;
    uploadDrop?.classList.remove("upload-error");
    uploadDrop?.classList.add("uploading");
    uploadForm.setAttribute("aria-busy", "true");
    uploadAbortController = new AbortController();
    if (uploadProgress) uploadProgress.hidden = false;
    if (uploadTitle) uploadTitle.textContent = "Saving upload collection";
    if (uploadStatus) uploadStatus.textContent = "Creating a durable queue for the selected records.";
    let resumeState = readUploadResumeState(
      planFingerprint,
      uploadBatches.length,
      checkpointBinding,
    );
    try {
      resumeState = await reconcileStaleUploadResumeState(resumeState);
      let retriedFresh = false;
      while (true) {
        let collectionId = resumeState.collection_id;
        try {
          for (
            activeUploadBatchIndex = resumeState.batch_index;
            activeUploadBatchIndex < uploadBatches.length;
            activeUploadBatchIndex += 1
          ) {
            selectedUploadFiles = uploadBatches[activeUploadBatchIndex];
            if (uploadTitle && uploadBatches.length > 1) {
              uploadTitle.textContent = `Creating batch ${activeUploadBatchIndex + 1} of ${uploadBatches.length}`;
            }
            const resumeSessionId = activeUploadBatchIndex === resumeState.batch_index
              ? resumeState.session_id
              : "";
            const response = await fetch(uploadForm.dataset.sessionUrl, {
              method: "POST",
              headers: {
                Accept: "application/json",
                "Content-Type": "application/json",
                "X-CSRF-Token": csrfToken,
              },
              body: JSON.stringify({
                collection_name: uploadCollectionName?.value || "Uploaded sources",
                collection_id: collectionId,
                resume_session_id: resumeSessionId,
                files: selectedUploadFiles.map(preflightDescriptor),
                intake_receipt_id: intake.receipt_id,
                intake_ordinals: selectedUploadFiles.map(file => intake.ordinals.get(file)),
              }),
              signal: uploadAbortController.signal,
            });
            activeUpload = await readUploadJson(response);
            collectionId = activeUpload.collection_id;
            writeUploadResumeState(
              planFingerprint,
              activeUpload.upload_session_id,
              collectionId,
              activeUploadBatchIndex,
              uploadBatches.length,
              checkpointBinding,
            );
            renderUpload(activeUpload);
            await runUploadQueue(selectedUploadFiles);
          }
          break;
        } catch (error) {
          if (
            error.code === "upload_resume_mismatch"
            && resumeState.session_id
            && resumeState.version !== 4
            && !retriedFresh
          ) {
            clearUploadResumeState();
            resumeState = {
              version: 0,
              session_id: "",
              collection_id: "",
              batch_index: 0,
            };
            activeUpload = null;
            retriedFresh = true;
            continue;
          }
          throw error;
        }
      }
      activeUploadBatchIndex = Math.max(uploadBatches.length - 1, 0);
      renderUpload(activeUpload);
      pollUploadProcessing();
      if (["complete", "partial"].includes(activeUpload.state)) {
        clearUploadResumeState();
        clearIntakeResumeState();
      }
    } catch (error) {
      if (error.name !== "AbortError") {
        if (uploadTitle) uploadTitle.textContent = "Upload paused";
        if (uploadStatus) uploadStatus.textContent = `${error.message} Try again with this selection, or reselect the same records to resume from the saved offsets.`;
        if (version === preflightVersion && uploadPreflightConfirm) uploadPreflightConfirm.disabled = false;
      }
    } finally {
      uploadDrop?.classList.remove("uploading");
      uploadForm.removeAttribute("aria-busy");
    }
  };

  const setUploadConfirmationBusy = (busy) => {
    confirmingUploadPreflight = busy;
    [fileInput, folderInput, fileChooser, folderChooser, uploadCollectionName,
      uploadPreflightCancel, uploadPreflightRetry].forEach((control) => {
      if (control) control.disabled = busy;
    });
  };

  const confirmUploadPreflight = async () => {
    if (confirmingUploadPreflight) return;
    if (uploadCollectionName) {
      uploadCollectionName.value = uploadCollectionName.value.trim();
      if (!uploadCollectionName.reportValidity()) return;
    }
    const preview = activePreflight;
    const files = preflightFiles.slice();
    const version = preflightVersion;
    const indexes = Array.isArray(preview?.eligible_indexes) ? preview.eligible_indexes : [];
    const eligibleFiles = indexes.filter(index => Number.isSafeInteger(index) && index >= 0 && index < files.length).map(index => files[index]);
    if (!preview || !files.length || eligibleFiles.length !== indexes.length) {
      previewSelectedFiles(files);
      return;
    }
    setUploadConfirmationBusy(true);
    if (uploadPreflightConfirm) uploadPreflightConfirm.disabled = true;
    if (uploadPreflightState) uploadPreflightState.textContent = "Saving selected-file receipt";
    try {
      const intake = await recordConfirmedSelection(files, preview, version);
      collectionNameEdited = false;
      if (!eligibleFiles.length) {
        clearIntakeResumeState();
        if (uploadPreflightState) uploadPreflightState.textContent = "Selection receipt saved";
        if (uploadPreflightStatus) uploadPreflightStatus.textContent = `All ${files.length.toLocaleString()} selected files are recorded. No file bytes were uploaded. Open the receipt to review what needs attention.`;
        return;
      }
      if (uploadPreflightState) uploadPreflightState.textContent = "Uploading reviewed files";
      if (uploadPreflightStatus) uploadPreflightStatus.textContent = `${files.length.toLocaleString()} selected files are recorded. ${eligibleFiles.length.toLocaleString()} ready files are starting upload; the other files remain in the receipt.`;
      await startResumableUpload(eligibleFiles, intake, version);
    } catch (error) {
      if (version !== preflightVersion) return;
      if (uploadPreflightState) uploadPreflightState.textContent = "Selection receipt paused";
      if (uploadPreflightStatus) uploadPreflightStatus.textContent = `${error.message} Your selection is still here. Try confirming again, or reselect the same files to resume.`;
      if (uploadPreflightConfirm) uploadPreflightConfirm.disabled = false;
    } finally {
      setUploadConfirmationBusy(false);
    }
  };

  fileChooser?.addEventListener("click", () => {
    if (!confirmingUploadPreflight) fileInput?.click();
  });
  folderChooser?.addEventListener("click", () => {
    if (!confirmingUploadPreflight) folderInput?.click();
  });
  uploadPreflightCancel?.addEventListener("click", () => {
    if (confirmingUploadPreflight) return;
    preflightVersion += 1;
    preflightAbortController?.abort();
    preflightAbortController = null;
    preflightFiles = [];
    activePreflight = null;
    if (fileInput) fileInput.value = "";
    if (folderInput) folderInput.value = "";
    uploadPreflightCounts?.replaceChildren();
    uploadPreflightItems?.replaceChildren();
    if (uploadPreflight) {
      uploadPreflight.hidden = true;
      uploadPreflight.removeAttribute("aria-busy");
    }
    if (uploadPreflightConfirm) uploadPreflightConfirm.disabled = true;
    if (uploadPreflightRetry) uploadPreflightRetry.hidden = true;
    uploadDrop?.classList.remove("upload-error");
    if (fileSummary) fileSummary.textContent = "Selection cleared. Previously saved uploads and receipts are unchanged.";
    fileChooser?.focus();
  });

  uploadForm?.addEventListener("submit", (event) => {
    event.preventDefault();
    if (activePreflight?.selected_count) {
      confirmUploadPreflight();
    } else if (preflightFiles.length) {
      previewSelectedFiles(preflightFiles);
    } else if (fileInput?.files?.length) {
      previewSelectedFiles(fileInput.files);
    }
  });
  uploadPreflightConfirm?.addEventListener("click", confirmUploadPreflight);
  uploadPreflightRetry?.addEventListener("click", () => previewSelectedFiles(preflightFiles));
  fileInput?.addEventListener("change", () => previewSelectedFiles(fileInput.files));
  folderInput?.addEventListener("change", () => previewSelectedFiles(folderInput.files));
  ["dragenter", "dragover"].forEach((name) => uploadDrop?.addEventListener(name, (event) => {
    event.preventDefault();
    uploadDrop.classList.add("drag-active");
  }));
  ["dragleave", "drop"].forEach((name) => uploadDrop?.addEventListener(name, (event) => {
    event.preventDefault();
    uploadDrop.classList.remove("drag-active");
  }));
  uploadDrop?.addEventListener("drop", (event) => {
    if (!event.dataTransfer?.files?.length) return;
    previewSelectedFiles(event.dataTransfer.files);
  });

  uploadCancel?.addEventListener("click", async () => {
    const cancelUrl = activeUpload?.cancel_url;
    uploadAbortController?.abort();
    if (!cancelUrl) return;
    try {
      const response = await fetch(cancelUrl, {
        method: "POST",
        headers: { Accept: "application/json", "X-CSRF-Token": csrfToken },
      });
      activeUpload = await readUploadJson(response);
      renderUpload(activeUpload, "Upload cancelled. Sources already queued remain in this matter.");
      clearUploadResumeState();
      clearIntakeResumeState();
    } catch (error) {
      if (uploadStatus) uploadStatus.textContent = error.message;
    }
  });

  if (document.querySelector("[data-ingestion-active]")) {
    const refreshIngestion = () => {
      if ((collectionNameEdited && uploadCollectionName?.value.trim())
          || confirmingUploadPreflight
          || (preflightFiles.length && (!activePreflight || !uploadPreflightConfirm?.disabled))
          || uploadPreflight?.getAttribute("aria-busy") === "true"
          || uploadForm?.getAttribute("aria-busy") === "true") {
        window.setTimeout(refreshIngestion, 3000);
        return;
      }
      window.location.reload();
    };
    window.setTimeout(refreshIngestion, 5000);
  }

  const sourceBulkForm = document.querySelector("[data-source-bulk-form]");
  const sourceSelections = Array.from(document.querySelectorAll("[data-source-select]"));
  const sourceSelectAll = document.querySelector("[data-source-select-all]");
  const sourceSelectedCount = document.querySelector("[data-source-selected-count]");
  const sourceBulkAction = document.querySelector("[data-source-bulk-action]");
  const sourceBulkCollection = document.querySelector("[data-bulk-collection]");
  const sourceBulkSet = document.querySelector("[data-bulk-source-set]");
  const sourceBulkSetName = document.querySelector("[data-bulk-set-name]");
  const sourceBulkSubmit = document.querySelector("[data-source-bulk-submit]");

  const syncSourceBulk = () => {
    const checked = sourceSelections.filter((item) => item.checked).length;
    if (sourceSelectedCount) sourceSelectedCount.textContent = String(checked);
    if (sourceSelectAll) {
      sourceSelectAll.checked = Boolean(sourceSelections.length) && checked === sourceSelections.length;
      sourceSelectAll.indeterminate = checked > 0 && checked < sourceSelections.length;
    }
    const action = sourceBulkAction?.value || "";
    if (sourceBulkCollection) {
      sourceBulkCollection.hidden = action !== "move";
      sourceBulkCollection.required = action === "move";
    }
    if (sourceBulkSet) {
      sourceBulkSet.hidden = action !== "add_to_set";
      sourceBulkSet.required = action === "add_to_set";
    }
    if (sourceBulkSetName) {
      sourceBulkSetName.hidden = action !== "create_set";
      sourceBulkSetName.required = action === "create_set";
    }
    if (sourceBulkSubmit) sourceBulkSubmit.disabled = checked === 0 || !action;
    sourceBulkForm?.classList.toggle("has-selection", checked > 0);
  };

  sourceSelections.forEach((item) => item.addEventListener("change", syncSourceBulk));
  sourceSelectAll?.addEventListener("change", () => {
    sourceSelections.forEach((item) => { item.checked = sourceSelectAll.checked; });
    syncSourceBulk();
  });
  sourceBulkAction?.addEventListener("change", syncSourceBulk);
  syncSourceBulk();

  const textarea = document.querySelector("#matter-question");
  const resizeTextarea = () => {
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 126)}px`;
  };
  textarea?.addEventListener("input", resizeTextarea);
  resizeTextarea();

  const reviewPane = document.querySelector(".workspace-main");
  const reviewComposer = reviewPane?.querySelector(".composer-wrap");
  if (reviewPane && reviewComposer) {
    // Keep conversation navigation clear of the sticky composer as its text
    // field, progress notices, or responsive rows change height.
    const measureReviewComposer = () => {
      // Classic scrollbars consume layout space; viewport height includes it.
      reviewPane.style.setProperty("--review-pane-height", `${reviewPane.clientHeight}px`);
      reviewPane.style.setProperty("--review-composer-height", `${Math.ceil(reviewComposer.getBoundingClientRect().height)}px`);
      reviewPane.style.setProperty("--review-composer-offset", `${parseFloat(getComputedStyle(reviewComposer).bottom) || 0}px`);
    };
    measureReviewComposer();
    if (typeof ResizeObserver !== "undefined") {
      const observer = new ResizeObserver(measureReviewComposer);
      observer.observe(reviewComposer);
      observer.observe(reviewPane);
    }
    // The tablet support drawer changes the sticky bottom offset on resize,
    // even when the composer itself keeps the same dimensions.
    window.addEventListener("resize", measureReviewComposer);
  }

  const questionForm = document.querySelector("[data-question-form]");
  const answerWaitStatus = document.querySelector("[data-answer-wait-status]");
  const answerWaitTitle = document.querySelector("[data-answer-wait-title]");
  const answerWaitMessage = document.querySelector("[data-answer-wait-message]");
  const answerWaitElapsed = document.querySelector("[data-answer-wait-elapsed]");
  const answerWaitStages = Array.from(document.querySelectorAll("[data-answer-stage]"));
  const answerCancelButton = document.querySelector("[data-answer-cancel]");
  const answerRetryButton = document.querySelector("[data-answer-retry]");
  const answerRequestKey = questionForm?.querySelector("[data-answer-request-key]");
  const askButton = questionForm?.querySelector(".ask-button");
  const investigateButton = questionForm?.querySelector(".investigate-button");
  const reviewSubmitButtons = Array.from(questionForm?.querySelectorAll("[name='review_task']") || []);
  const askButtonLabel = askButton?.querySelector("span");
  let sourcesReady = questionForm?.dataset.sourcesReady === "true";
  const durableReviewWorking = questionForm?.dataset.reviewWorkActive === "true";
  let answerWaitTimer;
  let answerPollTimer;
  let answerCreatedAt = Date.parse(answerWaitStatus?.dataset.answerCreatedAt || "");
  let activeAnswerStatusUrl = answerWaitStatus?.dataset.answerStatusUrl || "";

  const newAnswerRequestKey = () => {
    if (!answerRequestKey) return;
    const identifier = window.crypto?.randomUUID?.();
    const random = identifier?.replaceAll("-", "");
    if (random) answerRequestKey.value = `answer-request-${random}`;
  };

  const setComposerWorking = (working) => {
    questionForm?.classList.toggle("is-working", working);
    if (working) {
      questionForm?.setAttribute("aria-busy", "true");
      textarea?.setAttribute("readonly", "");
    } else {
      questionForm?.removeAttribute("aria-busy");
      textarea?.removeAttribute("readonly");
    }
    if (textarea) textarea.disabled = !sourcesReady;
    reviewSubmitButtons.forEach((button) => {
      button.disabled = working || !sourcesReady;
    });
    if (askButton) {
      if (working) askButton.setAttribute("aria-label", "Answer request saved and in progress");
      else askButton.removeAttribute("aria-label");
    }
    if (investigateButton) {
      if (working) investigateButton.setAttribute("aria-label", "Review work is already in progress");
      else investigateButton.removeAttribute("aria-label");
    }
    if (askButtonLabel) askButtonLabel.textContent = working ? "In progress" : "Answer";
  };

  window.addEventListener("recordbench:readiness", (event) => {
    const readiness = event.detail || {};
    sourcesReady = readiness.can_query === true;
    if (questionForm) questionForm.dataset.sourcesReady = sourcesReady ? "true" : "false";
    const working = questionForm?.getAttribute("aria-busy") === "true" || durableReviewWorking;
    setComposerWorking(working);
    const hint = document.querySelector("[data-composer-readiness-hint]");
    if (hint) {
      const partial = readiness.partial_query === true;
      hint.classList.toggle("is-partial", partial);
      hint.textContent = partial
        ? readiness.coverage_notice || "Questions use the searchable sources; affected sources are excluded."
        : sourcesReady
          ? "Answer uses the strongest matching passages. Investigate makes several searches and takes longer. Neither checks every source."
          : readiness.state === "preparing"
            ? "Questions will be available when active preparation finishes."
            : readiness.guidance || "No source is searchable yet.";
    }
  });

  const updateAnswerElapsed = (terminal = false) => {
    if (!answerWaitElapsed || !Number.isFinite(answerCreatedAt)) return;
    const seconds = Math.max(0, Math.floor((Date.now() - answerCreatedAt) / 1000));
    answerWaitElapsed.textContent = terminal
      ? `Finished after ${seconds} ${seconds === 1 ? "second" : "seconds"}`
      : `Waiting ${seconds} ${seconds === 1 ? "second" : "seconds"}`;
  };

  const startAnswerElapsed = () => {
    window.clearInterval(answerWaitTimer);
    updateAnswerElapsed();
    answerWaitTimer = window.setInterval(() => updateAnswerElapsed(), 1000);
  };

  const renderAnswerJob = (job) => {
    if (!answerWaitStatus || !job) return;
    const terminal = ["succeeded", "failed", "cancelled"].includes(job.state);
    activeAnswerStatusUrl = job.status_url || activeAnswerStatusUrl;
    answerCreatedAt = Date.parse(job.created_at || "") || answerCreatedAt || Date.now();
    answerWaitStatus.hidden = false;
    answerWaitStatus.dataset.answerStatusUrl = activeAnswerStatusUrl;
    answerWaitStatus.dataset.answerState = job.state;
    answerWaitStatus.classList.toggle("is-terminal", terminal);
    answerWaitStatus.classList.toggle("is-failed", job.state === "failed");
    answerWaitStatus.classList.toggle("is-cancelled", job.state === "cancelled");
    if (answerWaitTitle) answerWaitTitle.textContent = job.stage_label || "Answer in progress";
    if (answerWaitMessage) {
      answerWaitMessage.textContent = job.queue_position
        ? `Currently ${job.queue_position} in the answer queue.`
        : (job.message || "The saved answer request is being processed.");
    }
    const observedStages = new Set((job.events || []).map((event) => event.stage));
    answerWaitStages.forEach((item) => {
      const stage = item.dataset.answerStage;
      item.classList.toggle("is-current", stage === job.stage && job.state === "running");
      item.classList.toggle(
        "is-complete",
        observedStages.has(stage) && !(stage === job.stage && job.state === "running"),
      );
    });
    if (answerCancelButton) {
      answerCancelButton.hidden = !job.can_cancel;
      answerCancelButton.dataset.actionUrl = job.cancel_url || "";
      answerCancelButton.disabled = false;
    }
    if (answerRetryButton) {
      answerRetryButton.hidden = !job.can_retry;
      answerRetryButton.dataset.actionUrl = job.retry_url || "";
      answerRetryButton.disabled = false;
    }
    setComposerWorking(job.state === "queued" || job.state === "running");
    window.clearInterval(answerWaitTimer);
    if (terminal) {
      updateAnswerElapsed(true);
      if (job.state !== "succeeded") newAnswerRequestKey();
    } else {
      startAnswerElapsed();
    }
  };

  const readJson = async (response) => {
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.message || payload.detail || "The request could not be completed.");
    return payload;
  };

  const pollAnswerJob = async () => {
    window.clearTimeout(answerPollTimer);
    if (!activeAnswerStatusUrl) return;
    try {
      const response = await fetch(activeAnswerStatusUrl, {
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      const job = await readJson(response);
      renderAnswerJob(job);
      if (job.state === "succeeded") {
        // The result route is a distinct request target. Navigating through it
        // forces fresh server-rendered conversation HTML even when the final
        // workspace URL differs only by a fragment.
        const completion = new CustomEvent("recordbench:answer-complete", { detail: job, cancelable: true });
        if (window.dispatchEvent(completion)) window.location.assign(job.result_url || job.workspace_url);
        return;
      }
      if (["failed", "cancelled"].includes(job.state)) return;
      answerPollTimer = window.setTimeout(pollAnswerJob, 1000);
    } catch (_error) {
      if (answerWaitTitle) answerWaitTitle.textContent = "Reconnecting to saved request";
      if (answerWaitMessage) {
        answerWaitMessage.textContent = "Status is temporarily unavailable. The request remains saved; reconnecting automatically.";
      }
      answerPollTimer = window.setTimeout(pollAnswerJob, 4000);
    }
  };

  questionForm?.addEventListener("submit", async (event) => {
    // Broader investigations are durable server-side workflows. Let the
    // browser follow the normal redirect back to this conversation, where the
    // shared workflow monitor can show progress without pretending it is the
    // faster answer queue.
    if (event.submitter?.value === "research") return;
    event.preventDefault();
    if (!textarea?.value.trim()) {
      textarea?.focus();
      return;
    }
    setComposerWorking(true);
    if (answerWaitStatus) {
      answerWaitStatus.hidden = false;
      answerWaitStatus.classList.remove("is-terminal", "is-failed", "is-cancelled");
    }
    if (answerWaitTitle) answerWaitTitle.textContent = "Saving request";
    if (answerWaitMessage) answerWaitMessage.textContent = "Adding this question to the durable answer queue.";
    answerCreatedAt = Date.now();
    startAnswerElapsed();
    try {
      const response = await fetch(questionForm.action, {
        method: "POST",
        body: new FormData(questionForm),
        headers: { Accept: "application/json", "X-CSRF-Token": csrfToken },
      });
      const job = await readJson(response);
      textarea.value = "";
      resizeTextarea();
      renderAnswerJob(job);
      pollAnswerJob();
    } catch (error) {
      window.clearInterval(answerWaitTimer);
      setComposerWorking(false);
      if (answerWaitStatus) answerWaitStatus.classList.add("is-terminal", "is-failed");
      if (answerWaitTitle) answerWaitTitle.textContent = "Request not confirmed";
      if (answerWaitMessage) {
        answerWaitMessage.textContent = `${error.message} You can safely send it again; the request key prevents duplicates.`;
      }
    }
  });

  const runAnswerAction = async (button) => {
    const url = button?.dataset.actionUrl;
    if (!url) return;
    button.disabled = true;
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { Accept: "application/json", "X-CSRF-Token": csrfToken },
      });
      const job = await readJson(response);
      renderAnswerJob(job);
      if (["queued", "running"].includes(job.state)) pollAnswerJob();
    } catch (error) {
      button.disabled = false;
      if (answerWaitTitle) answerWaitTitle.textContent = "Action not completed";
      if (answerWaitMessage) answerWaitMessage.textContent = error.message;
    }
  };

  answerCancelButton?.addEventListener("click", () => runAnswerAction(answerCancelButton));
  answerRetryButton?.addEventListener("click", () => runAnswerAction(answerRetryButton));

  if (activeAnswerStatusUrl) {
    const activeState = answerWaitStatus?.dataset.answerState;
    setComposerWorking(activeState === "queued" || activeState === "running");
    startAnswerElapsed();
    pollAnswerJob();
  }

  const mediaActivity = document.querySelector("[data-media-activity]");
  const mediaActivityTitle = mediaActivity?.querySelector("[data-media-activity-title]");
  const mediaActivityList = mediaActivity?.querySelector("[data-media-activity-list]");
  const mediaActivityNote = mediaActivity?.querySelector("[data-media-activity-note]");
  let mediaActivityTimer = 0;

  const renderMediaActivity = (payload) => {
    if (!mediaActivity || !mediaActivityList) return;
    const active = Number(payload.active_count || 0);
    const attention = Number(payload.attention_count || 0);
    if (mediaActivityTitle) {
      mediaActivityTitle.textContent = active
        ? `Media processing · ${active} active`
        : attention
          ? "Media needs attention"
          : "Transcript ready";
    }
    mediaActivityList.replaceChildren();
    (payload.items || []).forEach((item) => {
      const row = document.createElement("article");
      const marker = document.createElement("span");
      marker.className = ["queued", "running"].includes(item.state)
        ? "processing-pulse"
        : ["failed", "needs_review"].includes(item.state)
          ? "media-activity-marker attention"
          : "media-activity-marker ready";
      marker.setAttribute("aria-hidden", "true");
      const copy = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = item.source_name || "Recording";
      const stage = document.createElement("small");
      const percent = ["queued", "running"].includes(item.state)
        ? ` · ${Math.round(Math.max(0, Math.min(1, Number(item.progress || 0))) * 100)}%`
        : "";
      stage.textContent = `${item.stage || "Processing"}${percent}`;
      copy.append(name, stage);
      const link = document.createElement("a");
      link.href = item.review_url;
      link.textContent = item.state === "failed"
        ? "Review issue"
        : item.state === "needs_review"
          ? "Review recording"
          : ["succeeded", "degraded"].includes(item.state)
            ? "Open transcript"
            : "View status";
      row.append(marker, copy, link);
      mediaActivityList.append(row);
    });
    if (mediaActivityNote) {
      mediaActivityNote.textContent = active
        ? "This progress is saved. You may leave this conversation and return later."
        : attention
          ? "Open the recording to review its status and choose the next step."
          : "The transcript is now searchable and ready beside the recording.";
    }
  };

  const pollMediaActivity = async () => {
    window.clearTimeout(mediaActivityTimer);
    if (!mediaActivity?.dataset.statusUrl) return;
    try {
      const response = await fetch(mediaActivity.dataset.statusUrl, {
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "Status unavailable");
      renderMediaActivity(payload);
      if (Number(payload.active_count || 0)) {
        mediaActivityTimer = window.setTimeout(pollMediaActivity, 2500);
      }
    } catch (_error) {
      if (mediaActivityNote) mediaActivityNote.textContent = "Status reconnecting; the saved media preparation will continue.";
      mediaActivityTimer = window.setTimeout(pollMediaActivity, 3500);
    }
  };
  if (mediaActivity) pollMediaActivity();

  const mediaReview = document.querySelector("[data-media-review]");
  const mediaPlayer = document.querySelector("[data-media-player]");
  const mediaTime = document.querySelector("[data-media-time]");
  const transcriptSegments = Array.from(document.querySelectorAll("[data-transcript-segment]"));
  const transcriptFollow = document.querySelector("[data-transcript-follow]");
  const mediaJob = document.querySelector("[data-media-job]");
  const mediaStage = mediaJob?.querySelector("[data-media-stage]");
  const mediaMessage = mediaJob?.querySelector("[data-media-message]");
  const mediaPercent = mediaJob?.querySelector("[data-media-percent]");
  const mediaProgress = mediaJob?.querySelector("[data-media-progress]");
  const playbackStatus = document.querySelector("[data-playback-status]");
  const playbackTitle = playbackStatus?.querySelector("[data-playback-title]");
  const playbackMessage = playbackStatus?.querySelector("[data-playback-message]");
  const mediaStatusUrl = mediaReview?.dataset.mediaStatusUrl || mediaJob?.dataset.statusUrl || "";
  const mediaResumeUrl = mediaReview?.dataset.mediaResumeUrl || "";
  let observedMediaJobState = mediaReview?.dataset.mediaJobState || "unavailable";
  let observedPlaybackState = mediaReview?.dataset.playbackState || "unknown";
  let observedSummaryState = mediaReview?.dataset.summaryState || "not_created";
  let mediaPollTimer = 0;
  let activeTranscriptSegment = null;
  let mediaPageNavigation = false;
  let mediaResumeApplied = false;
  let mediaResumeLastSentAt = 0;
  let mediaResumeLastPosition = Number(mediaReview?.dataset.startMs || 0);

  const transcriptPage = Math.max(1, Number(mediaReview?.dataset.transcriptPage || 1));
  const transcriptPages = Math.max(1, Number(mediaReview?.dataset.transcriptPages || 1));
  const transcriptFiltered = mediaReview?.dataset.transcriptFiltered === "true";
  const mediaFollowResumeKey = mediaReview
    ? `case-intelligence:media-follow:${window.location.pathname}`
    : "";
  const mediaToolTabs = Array.from(document.querySelectorAll("[data-media-tool-tab]"));
  const mediaToolPanels = Array.from(document.querySelectorAll("[data-media-tool-panel]"));
  const mediaToolResumeKey = mediaReview
    ? `case-intelligence:media-tool:${window.location.pathname}`
    : "";
  const mediaToolFromHash = {
    "#media-playback": "playback",
    "#media-summary": "summary",
    "#media-export": "export",
    "#media-clips": "clips",
  };

  const activateMediaTool = (name, { focus = false, updateHash = false } = {}) => {
    const panel = mediaToolPanels.find((candidate) => candidate.dataset.mediaTool === name);
    const tab = mediaToolTabs.find((candidate) => candidate.dataset.mediaToolTab === name);
    if (!panel || !tab) return false;
    mediaToolPanels.forEach((candidate) => {
      candidate.hidden = candidate !== panel;
    });
    mediaToolTabs.forEach((candidate) => {
      const selected = candidate === tab;
      candidate.setAttribute("aria-selected", selected ? "true" : "false");
      candidate.tabIndex = selected ? 0 : -1;
    });
    writeSessionValue(mediaToolResumeKey, name);
    if (updateHash) {
      const url = new URL(window.location.href);
      url.hash = `media-${name}`;
      window.history.replaceState(window.history.state, "", url);
    }
    if (focus) tab.focus({ preventScroll: true });
    return true;
  };

  mediaToolTabs.forEach((tab, index) => {
    tab.addEventListener("click", () => {
      activateMediaTool(tab.dataset.mediaToolTab || "playback", { updateHash: true });
    });
    tab.addEventListener("keydown", (event) => {
      let nextIndex = index;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") {
        nextIndex = (index + 1) % mediaToolTabs.length;
      } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
        nextIndex = (index - 1 + mediaToolTabs.length) % mediaToolTabs.length;
      } else if (event.key === "Home") {
        nextIndex = 0;
      } else if (event.key === "End") {
        nextIndex = mediaToolTabs.length - 1;
      } else {
        return;
      }
      event.preventDefault();
      activateMediaTool(mediaToolTabs[nextIndex].dataset.mediaToolTab || "playback", {
        focus: true,
        updateHash: true,
      });
    });
  });

  if (mediaToolTabs.length) {
    const requestedTool = mediaToolFromHash[window.location.hash]
      || readSessionValue(mediaToolResumeKey)
      || "playback";
    if (!activateMediaTool(requestedTool)) activateMediaTool("playback");
    window.addEventListener("hashchange", () => {
      const hashedTool = mediaToolFromHash[window.location.hash];
      if (hashedTool) activateMediaTool(hashedTool);
    });
  }

  const formatMediaTime = (milliseconds) => {
    const value = Math.max(0, Math.round(Number(milliseconds) || 0));
    const hours = Math.floor(value / 3600000);
    const minutes = Math.floor((value % 3600000) / 60000);
    const seconds = Math.floor((value % 60000) / 1000);
    return hours
      ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`
      : `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  };

  const seekMedia = (milliseconds, play = true) => {
    if (!mediaPlayer) return;
    activateMediaTool("playback");
    const seconds = Math.max(0, Number(milliseconds) / 1000);
    mediaPlayer.currentTime = seconds;
    if (play) mediaPlayer.play().catch(() => {});
  };

  const saveMediaResume = (force = false) => {
    if (!mediaResumeUrl || !mediaPlayer || !Number.isFinite(mediaPlayer.currentTime)) return;
    const position = Math.max(0, Math.round(mediaPlayer.currentTime * 1000));
    const now = Date.now();
    if (
      !force
      && (now - mediaResumeLastSentAt < 15000 || Math.abs(position - mediaResumeLastPosition) < 5000)
    ) return;
    mediaResumeLastSentAt = now;
    mediaResumeLastPosition = position;
    fetch(mediaResumeUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken },
      body: JSON.stringify({ position_ms: position }),
      credentials: "same-origin",
      keepalive: true,
    }).catch(() => {});
  };

  const speakerReviewPanel = document.querySelector("[data-speaker-review]");
  const speakerReviewStatus = speakerReviewPanel?.querySelector("[data-speaker-review-status]");
  const speakerReviewOpeners = Array.from(document.querySelectorAll("[data-open-speaker-review]"));
  const speakerReviewCloser = speakerReviewPanel?.querySelector("[data-close-speaker-review]");
  const speakerMappingForms = Array.from(document.querySelectorAll("[data-speaker-mapping-form]"));
  const mediaSummaryCard = document.querySelector("[data-media-summary-card]");
  const summaryStateLabel = mediaSummaryCard?.querySelector("[data-summary-state-label]");
  const summaryRefreshNotice = mediaSummaryCard?.querySelector("[data-summary-refresh-notice]");
  let speakerReviewReturnFocus = null;
  let speakerReviewReturnPosition = null;

  const speakerForm = (cluster) => speakerMappingForms.find(
    (form) => form.dataset.speakerCluster === cluster,
  ) || speakerMappingForms[0] || null;

  const focusSpeakerInput = (cluster = "", reveal = false) => {
    const form = speakerForm(cluster);
    const input = form?.querySelector("[data-speaker-label-input]");
    window.requestAnimationFrame(() => {
      if (reveal) form?.scrollIntoView({ block: "nearest" });
      input?.focus({ preventScroll: true });
    });
  };

  const restoreSpeakerReviewContext = () => {
    const target = speakerReviewReturnFocus;
    const position = speakerReviewReturnPosition;
    speakerReviewReturnFocus = null;
    speakerReviewReturnPosition = null;
    window.requestAnimationFrame(() => {
      if (position) window.scrollTo(position.x, position.y);
      target?.focus({ preventScroll: true });
    });
  };

  const openSpeakerReview = (cluster = "", returnFocus = null) => {
    if (!speakerReviewPanel) return;
    if (!speakerReviewPanel.open && returnFocus) {
      speakerReviewReturnFocus = returnFocus;
      speakerReviewReturnPosition = { x: window.scrollX, y: window.scrollY };
    }
    speakerReviewPanel.open = true;
    speakerReviewPanel.dataset.focusSpeaker = cluster;
    speakerReviewOpeners.forEach((button) => button.setAttribute("aria-expanded", "true"));
    focusSpeakerInput(cluster, true);
  };

  speakerReviewOpeners.forEach((button) => {
    button.addEventListener("click", () => openSpeakerReview("", button));
  });
  document.querySelectorAll("[data-speaker-review-target]").forEach((button) => {
    button.addEventListener("click", () => openSpeakerReview(
      button.dataset.speakerReviewTarget || "",
      button,
    ));
  });
  speakerReviewCloser?.addEventListener("click", () => {
    speakerReviewPanel.open = false;
  });
  speakerReviewPanel?.addEventListener("toggle", () => {
    const expanded = speakerReviewPanel.open ? "true" : "false";
    speakerReviewOpeners.forEach((button) => button.setAttribute("aria-expanded", expanded));
    if (!speakerReviewPanel.open) restoreSpeakerReviewContext();
  });

  const setSpeakerReviewStatus = (message, state = "") => {
    if (!speakerReviewStatus) return;
    speakerReviewStatus.hidden = !message;
    speakerReviewStatus.textContent = message;
    speakerReviewStatus.dataset.state = state;
  };

  speakerMappingForms.forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const cluster = form.dataset.speakerCluster || "";
      const input = form.querySelector("[data-speaker-label-input]");
      const saveButton = form.querySelector("[data-speaker-save]");
      const returnStart = form.querySelector("[data-speaker-return-start]");
      const returnSegment = form.querySelector("[data-speaker-return-segment]");
      if (returnStart && mediaPlayer && Number.isFinite(mediaPlayer.currentTime)) {
        returnStart.value = String(Math.max(0, Math.round(mediaPlayer.currentTime * 1000)));
      }
      if (returnSegment) {
        returnSegment.value = activeTranscriptSegment?.dataset.segmentId || returnSegment.value;
      }
      saveButton?.setAttribute("aria-busy", "true");
      if (saveButton) saveButton.disabled = true;
      setSpeakerReviewStatus("Saving this label across the transcript…", "saving");
      try {
        const response = await fetch(form.action, {
          method: "POST",
          headers: { Accept: "application/json" },
          body: new FormData(form),
          credentials: "same-origin",
        });
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload.message || payload.detail || "Speaker label could not be saved.");
        form.querySelector('[name="expected_revision"]').value = String(payload.revision);
        if (input) input.value = payload.display_name;
        document.querySelectorAll("[data-speaker-label]").forEach((label) => {
          if (label.dataset.speakerLabel !== cluster) return;
          label.textContent = payload.display_name;
          if (label.matches("button")) {
            label.setAttribute("aria-label", `Review speaker label ${payload.display_name}`);
          }
        });
        document.querySelectorAll("[data-speaker-state-for]").forEach((state) => {
          if (state.dataset.speakerStateFor !== cluster) return;
          state.classList.remove("speaker-unconfirmed", "speaker-confirmed");
          state.classList.add(payload.identity_state === "confirmed" ? "speaker-confirmed" : "speaker-unconfirmed");
          state.textContent = payload.identity_state === "confirmed" ? "Confirmed" : "Unconfirmed";
        });
        if (saveButton) saveButton.textContent = "Save correction";
        if (payload.overview_refreshing && mediaSummaryCard) {
          mediaSummaryCard.classList.add("media-summary-stale");
          if (summaryStateLabel) {
            summaryStateLabel.className = "summary-state summary-stale";
            summaryStateLabel.textContent = "Refreshing";
          }
          if (summaryRefreshNotice) summaryRefreshNotice.hidden = false;
        }
        setSpeakerReviewStatus(payload.message || "Speaker label saved across this transcript.", "saved");
        focusSpeakerInput(cluster);
      } catch (error) {
        setSpeakerReviewStatus(error.message || "Speaker label could not be saved.", "error");
        input?.focus({ preventScroll: true });
      } finally {
        saveButton?.removeAttribute("aria-busy");
        if (saveButton) saveButton.disabled = false;
      }
    });
  });

  if (speakerReviewPanel?.open || window.location.hash === "#speaker-review") {
    openSpeakerReview(speakerReviewPanel?.dataset.focusSpeaker || "");
  }

  document.querySelectorAll("[data-seek-ms]").forEach((button) => {
    button.addEventListener("click", () => seekMedia(button.dataset.seekMs));
  });

  const followTranscriptRow = (row) => {
    if (!row || !transcriptFollow?.checked) return;
    if (speakerReviewPanel?.open) return;
    if (document.activeElement?.closest?.(".transcript-editor")) return;
    const bounds = row.getBoundingClientRect();
    const topbar = Number.parseFloat(
      getComputedStyle(document.documentElement).getPropertyValue("--topbar-height"),
    ) || 0;
    const safeTop = topbar + 18;
    const safeBottom = window.innerHeight - 24;
    if (bounds.top >= safeTop && bounds.bottom <= safeBottom) return;
    row.scrollIntoView({
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
      block: "center",
    });
  };

  const continueTranscriptOnPage = (targetPage, milliseconds) => {
    if (mediaPageNavigation || transcriptFiltered || !mediaFollowResumeKey) return;
    if (targetPage < 1 || targetPage > transcriptPages) return;
    mediaPageNavigation = true;
    writeSessionValue(mediaFollowResumeKey, JSON.stringify({
      startMs: Math.max(0, Math.round(milliseconds)),
      playing: !mediaPlayer.paused,
    }));
    const url = new URL(window.location.href);
    url.searchParams.set("page", String(targetPage));
    url.searchParams.set("start_ms", String(Math.max(0, Math.round(milliseconds))));
    url.searchParams.delete("segment");
    url.hash = "";
    window.location.assign(url.toString());
  };

  const followTranscriptPage = (milliseconds, activeRow) => {
    if (
      activeRow
      || !mediaResumeApplied
      || mediaPlayer?.paused
      || !transcriptFollow?.checked
      || transcriptFiltered
      || !transcriptSegments.length
    ) return;
    const firstStart = Number(transcriptSegments[0].dataset.startMs || 0);
    const last = transcriptSegments[transcriptSegments.length - 1];
    const lastEnd = Number(last.dataset.endMs || last.dataset.startMs || 0);
    if (milliseconds >= lastEnd && transcriptPage < transcriptPages) {
      continueTranscriptOnPage(transcriptPage + 1, milliseconds);
    } else if (milliseconds < firstStart && transcriptPage > 1) {
      continueTranscriptOnPage(transcriptPage - 1, milliseconds);
    }
  };

  const updateTranscriptPosition = () => {
    if (!mediaPlayer) return;
    const milliseconds = mediaPlayer.currentTime * 1000;
    saveMediaResume();
    if (mediaTime) mediaTime.textContent = formatMediaTime(milliseconds);
    const next = transcriptSegments.find((row) => {
      const start = Number(row.dataset.startMs || 0);
      const end = Number(row.dataset.endMs || start);
      return start <= milliseconds && milliseconds < end;
    }) || null;
    followTranscriptPage(milliseconds, next);
    if (next === activeTranscriptSegment) return;
    activeTranscriptSegment?.classList.remove("is-active");
    next?.classList.add("is-active");
    activeTranscriptSegment = next;
    followTranscriptRow(next);
  };

  if (mediaPlayer && mediaReview) {
    const initialStart = Number(mediaReview.dataset.startMs || 0);
    const applyInitialStart = () => {
      let resume = null;
      if (mediaFollowResumeKey) {
        try {
          resume = JSON.parse(readSessionValue(mediaFollowResumeKey) || "null");
          removeSessionValue(mediaFollowResumeKey);
        } catch (_error) {
          resume = null;
        }
      }
      const resumeStart = Number(resume?.startMs || initialStart);
      if (resumeStart > 0) mediaPlayer.currentTime = resumeStart / 1000;
      mediaResumeApplied = true;
      updateTranscriptPosition();
      if (resume?.playing) mediaPlayer.play().catch(() => {});
    };
    if (mediaPlayer.readyState >= 1) applyInitialStart();
    else mediaPlayer.addEventListener("loadedmetadata", applyInitialStart, { once: true });
    mediaPlayer.addEventListener("timeupdate", updateTranscriptPosition);
    mediaPlayer.addEventListener("seeked", updateTranscriptPosition);
  }

  transcriptFollow?.addEventListener("change", () => {
    if (transcriptFollow.checked) followTranscriptRow(activeTranscriptSegment);
  });

  const clipStart = document.querySelector("[data-clip-start]");
  const clipEnd = document.querySelector("[data-clip-end]");
  const clipStartDisplay = document.querySelector("[data-clip-start-display]");
  const clipEndDisplay = document.querySelector("[data-clip-end-display]");
  const setClipBoundary = (input, display) => {
    if (!mediaPlayer || !input || !display) return;
    const milliseconds = Math.max(0, Math.round(mediaPlayer.currentTime * 1000));
    input.value = String(milliseconds);
    display.value = formatMediaTime(milliseconds);
  };
  document.querySelector("[data-set-clip-start]")?.addEventListener("click", () => {
    setClipBoundary(clipStart, clipStartDisplay);
  });
  document.querySelector("[data-set-clip-end]")?.addEventListener("click", () => {
    setClipBoundary(clipEnd, clipEndDisplay);
  });

  document.querySelector("[data-clip-form]")?.addEventListener("submit", (event) => {
    const start = Number(clipStart?.value || 0);
    const end = Number(clipEnd?.value || 0);
    if (end - start < 1000 || end - start > 600000) {
      event.preventDefault();
      window.alert("Choose a clip between one second and ten minutes.");
    }
  });

  const pollMediaJob = async () => {
    if (!mediaStatusUrl) return;
    try {
      const response = await fetch(mediaStatusUrl, {
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || "Status unavailable");
      const progress = Math.max(0, Math.min(1, Number(payload.progress || 0)));
      if (mediaStage) mediaStage.textContent = payload.stage || "Transcribing";
      if (mediaMessage && payload.message) mediaMessage.textContent = payload.message;
      if (mediaPercent) mediaPercent.textContent = `${Math.round(progress * 100)}%`;
      if (mediaProgress) mediaProgress.style.width = `${progress * 100}%`;
      const nextPlaybackState = payload.playback_state || "unknown";
      if (playbackStatus) {
        playbackStatus.dataset.state = nextPlaybackState;
        playbackStatus.className = `playback-compatibility playback-${nextPlaybackState}`;
        if (playbackTitle) {
          playbackTitle.textContent = nextPlaybackState === "ready"
            ? "Browser playback ready"
            : nextPlaybackState === "original"
              ? "Original format ready"
              : nextPlaybackState === "failed"
                ? "Compatibility copy needs attention"
                : "Preparing browser playback";
        }
        if (playbackMessage && payload.playback_message) {
          playbackMessage.textContent = payload.playback_message;
        }
      }
      const transcriptChanged = payload.state !== observedMediaJobState
        && ["succeeded", "degraded", "failed", "needs_review", "playback_only"].includes(payload.state);
      const playbackChanged = nextPlaybackState !== observedPlaybackState
        && ["original", "ready", "failed"].includes(nextPlaybackState);
      const summaryChanged = payload.summary_state !== observedSummaryState
        && ["ready", "failed", "stale"].includes(payload.summary_state);
      observedMediaJobState = payload.state;
      observedPlaybackState = nextPlaybackState;
      observedSummaryState = payload.summary_state;
      if (transcriptChanged || playbackChanged || summaryChanged) {
        const url = new URL(window.location.href);
        if (mediaPlayer && Number.isFinite(mediaPlayer.currentTime)) {
          url.searchParams.set("start_ms", String(Math.max(0, Math.round(mediaPlayer.currentTime * 1000))));
        }
        const nextUrl = url.toString();
        if (nextUrl === window.location.href) window.location.reload();
        else window.location.replace(nextUrl);
        return;
      }
      const active = ["queued", "running"].includes(payload.state)
        || ["queued", "processing", "unknown"].includes(nextPlaybackState)
        || ["queued", "running", "stale"].includes(payload.summary_state);
      if (!active) return;
    } catch (_error) {
      if (mediaMessage) mediaMessage.textContent = "Status reconnecting; this work is still saved.";
      if (playbackMessage) playbackMessage.textContent = "Playback status is reconnecting; the original video remains saved.";
    }
    mediaPollTimer = window.setTimeout(pollMediaJob, 2500);
  };
  if (
    mediaStatusUrl
    && (
      ["queued", "running"].includes(observedMediaJobState)
      || ["queued", "processing", "unknown"].includes(observedPlaybackState)
      || ["queued", "running", "stale"].includes(observedSummaryState)
    )
  ) pollMediaJob();

  const focusedHash = /^#segment-\d+$/.test(window.location.hash) ? window.location.hash : "";
  const focusedTranscript = document.querySelector(".transcript-segment.focus-segment")
    || (focusedHash ? document.querySelector(focusedHash) : null);
  if (focusedTranscript) {
    window.requestAnimationFrame(() => focusedTranscript.scrollIntoView({ block: "center" }));
  }

  window.caseIntelligenceMedia = { seekMedia, formatMediaTime, updateTranscriptPosition };

  window.addEventListener("pagehide", () => {
    saveMediaResume(true);
    window.clearInterval(answerWaitTimer);
    window.clearTimeout(answerPollTimer);
    window.clearTimeout(mediaPollTimer);
    window.clearTimeout(uploadProcessingTimer);
    window.clearTimeout(mediaActivityTimer);
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted && activeAnswerStatusUrl) pollAnswerJob();
  });

  const supportPane = document.querySelector("[data-support-pane]");
  const supportMedia = supportPane?.querySelector("[data-support-media]");
  const supportMediaPlayer = supportPane?.querySelector("[data-support-media-player]");
  const supportPlayButton = supportPane?.querySelector("[data-support-play]");
  const positionSupportMedia = (play = false) => {
    if (!supportMedia || !supportMediaPlayer) return;
    const seconds = Math.max(0, Number(supportMedia.dataset.startMs || 0) / 1000);
    supportMediaPlayer.currentTime = seconds;
    if (play) supportMediaPlayer.play().catch(() => {});
  };
  if (supportMediaPlayer) {
    const applySupportStart = () => positionSupportMedia(
      supportMedia?.dataset.autoplay === "true",
    );
    if (supportMediaPlayer.readyState >= 1) applySupportStart();
    else supportMediaPlayer.addEventListener("loadedmetadata", applySupportStart, { once: true });
    supportPlayButton?.addEventListener("click", () => positionSupportMedia(true));
  }
  if (supportPane && window.location.hash === "#support-pane") {
    window.requestAnimationFrame(() => {
      if (window.matchMedia("(max-width: 900px)").matches) {
        const latestAnswer = document.querySelector("#latest");
        const workspace = document.querySelector(".workspace-main");
        if (latestAnswer && workspace) {
          const topbar = Number.parseFloat(
            getComputedStyle(document.documentElement).getPropertyValue("--topbar-height")
          ) || 62;
          workspace.scrollTop += latestAnswer.getBoundingClientRect().top - topbar - 64;
        }
      }
      supportPane.focus({ preventScroll: true });
    });
  }
})();

/* One matter-wide, durable preparation projection shared by every workspace. */
(() => {
  const readiness = document.querySelector("[data-matter-readiness]");
  if (!readiness) return;
  // Ready matters show one line; the extraction caveat stays one click away.
  const limitsToggle = readiness.querySelector("[data-readiness-limits-toggle]");
  if (limitsToggle) {
    readiness.classList.add("readiness-enhanced");
    limitsToggle.hidden = false;
    limitsToggle.addEventListener("click", () => {
      const open = limitsToggle.getAttribute("aria-expanded") !== "true";
      limitsToggle.setAttribute("aria-expanded", open ? "true" : "false");
      readiness.classList.toggle("show-guidance", open);
    });
  }
  let readinessTimer = 0;
  const details = readiness.querySelector("[data-processing-details]");

  const setDetailsOpen = (open) => {
    if (details) details.hidden = !open;
    readiness.querySelectorAll("[data-processing-toggle]").forEach((control) => {
      control.setAttribute("aria-expanded", open ? "true" : "false");
    });
  };

  readiness.addEventListener("click", (event) => {
    const control = event.target.closest("[data-processing-toggle]");
    if (!control || !readiness.contains(control)) return;
    setDetailsOpen(details?.hidden !== false);
  });

  const readinessMark = (state) => {
    if (state === "review") return '<svg viewBox="0 0 24 24"><path d="m9 5 11 7-11 7Z"/></svg>';
    if (state === "ready") return '<svg viewBox="0 0 24 24"><path d="m5 12 4 4 10-10"/></svg>';
    if (state === "attention") return '<svg viewBox="0 0 24 24"><path d="M12 4 3.5 20h17L12 4Z"/><path d="M12 9v5m0 3h.01"/></svg>';
    if (state === "empty") return '<svg viewBox="0 0 24 24"><path d="M6 3h9l3 3v15H6zM9 11h6M12 8v6"/></svg>';
    return '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8"/><path d="M12 4a8 8 0 0 1 8 8"/></svg>';
  };

  const renderActions = (payload) => {
    const holder = readiness.querySelector(".matter-readiness-actions");
    if (!holder) return;
    const focused = document.activeElement;
    let primary = holder.querySelector(":scope > a, :scope > button:not(.readiness-details-action):not(.readiness-limits-toggle)");
    const tag = payload.action_url ? "A" : "BUTTON";
    let restorePrimaryFocus = false;
    if (!primary || primary.tagName !== tag) {
      const replacement = document.createElement(tag.toLowerCase());
      restorePrimaryFocus = primary?.contains(focused) || false;
      if (primary) primary.replaceWith(replacement);
      else holder.prepend(replacement);
      primary = replacement;
    }
    if (payload.action_url) {
      primary.href = payload.action_url;
      primary.dataset.readinessAction = "true";
    } else {
      primary.type = "button";
      primary.dataset.processingToggle = "true";
      primary.setAttribute("aria-expanded", details?.hidden === false ? "true" : "false");
      primary.setAttribute("aria-controls", "matter-processing-center");
    }
    // Keep the same focusable node across ordinary polls, even if its label
    // or destination changes. WebDriver and keyboard focus retain that node.
    if (primary.textContent !== payload.action_label) primary.textContent = payload.action_label;
    // Configure the new action before focusing it (an anchor needs its href).
    if (restorePrimaryFocus) primary.focus({ preventScroll: true });
    let detailButton = holder.querySelector(".readiness-details-action");
    if (payload.state === "attention") {
      if (!detailButton) {
        detailButton = document.createElement("button");
        detailButton.type = "button";
        detailButton.className = "readiness-details-action";
        detailButton.dataset.processingToggle = "true";
        detailButton.setAttribute("aria-controls", "matter-processing-center");
        detailButton.textContent = "Details";
        holder.append(detailButton);
      }
      detailButton.setAttribute("aria-expanded", details?.hidden === false ? "true" : "false");
    } else if (detailButton) {
      const restoreFocus = detailButton.contains(focused);
      detailButton.remove();
      if (restoreFocus) primary.focus({ preventScroll: true });
    }
  };

  const renderReadiness = (payload) => {
    ["empty", "preparing", "attention", "ready", "review"].forEach((state) => {
      readiness.classList.toggle(`state-${state}`, payload.state === state);
    });
    readiness.dataset.state = payload.state || "";
    readiness.dataset.canQuery = payload.can_query === true ? "true" : "false";
    readiness.dataset.pollAfterMs = String(payload.poll_after_ms || 8000);
    const headline = readiness.querySelector("[data-readiness-headline]");
    const summary = readiness.querySelector("[data-readiness-summary]");
    const guidance = readiness.querySelector("[data-readiness-guidance]");
    const mark = readiness.querySelector("[data-readiness-mark]");
    const progress = readiness.querySelector("[data-readiness-progress]");
    if (headline) headline.textContent = payload.headline || "Matter status";
    if (summary) summary.textContent = payload.summary || "";
    if (guidance) guidance.textContent = payload.guidance || "";
    if (mark) mark.innerHTML = readinessMark(payload.state);
    if (progress) progress.style.width = `${Math.max(0, Math.min(100, payload.progress_percent || 0))}%`;
    renderActions(payload);

    const activity = readiness.querySelector("[data-readiness-activity]");
    if (activity) {
      activity.replaceChildren();
      (payload.active_work || []).forEach((item) => {
        const line = document.createElement("span");
        line.dataset.workKey = item.key || "";
        const count = document.createElement("strong");
        count.textContent = String(item.count || 0);
        line.append(count, document.createTextNode(` ${item.label || "Working"}`));
        activity.append(line);
      });
      activity.hidden = !(payload.active_work || []).length;
    }
    (payload.stages || []).forEach((stage) => {
      const row = readiness.querySelector(`[data-processing-stage="${stage.key}"]`);
      if (!row) return;
      ["waiting", "working", "complete", "attention", "review"].forEach((state) => {
        row.classList.toggle(`state-${state}`, stage.state === state);
      });
      const count = row.querySelector("[data-stage-count]");
      const state = row.querySelector("[data-stage-state]");
      if (count) count.textContent = stage.count_label || "";
      if (state) state.textContent = stage.state_label || "";
    });
    const discovery = readiness.querySelector("[data-readiness-discovery]");
    if (discovery) {
      const found = payload.discovery || {};
      discovery.textContent = found.label || "";
      if (found.href) discovery.setAttribute("href", found.href);
      discovery.classList.toggle("is-working", found.working === true);
      discovery.hidden = !found.label;
    }
    const overview = readiness.querySelector("[data-processing-overview-note]");
    if (overview) {
      overview.textContent = payload.overview_note || "";
      overview.hidden = !payload.overview_note;
    }
    const updated = readiness.querySelector("[data-readiness-updated]");
    if (updated) updated.textContent = payload.state === "review"
      ? "Saved · Recordings are available for review."
      : "Updated just now · Work continues if you leave this page.";

    document.querySelectorAll(".matter-search-form input[name=\"q\"], .matter-search-form button[type=\"submit\"]").forEach((control) => {
      control.disabled = payload.can_query !== true;
    });
    const partial = payload.partial_query === true;
    const searchHint = document.querySelector("[data-search-readiness-hint]");
    if (searchHint) {
      searchHint.classList.toggle("is-partial", partial);
      searchHint.textContent = partial
        ? payload.coverage_notice || "Search uses the searchable sources; affected sources are excluded."
        : payload.state === "preparing"
          ? "Search will be available when active preparation finishes. You can continue reviewing individual sources in the meantime."
          : payload.guidance || "No source is searchable yet.";
      searchHint.hidden = payload.can_query === true && !partial;
    }
    const conversationCoverage = document.querySelector("[data-conversation-coverage]");
    if (conversationCoverage) {
      const copy = conversationCoverage.querySelector("[data-conversation-coverage-copy]");
      const action = conversationCoverage.querySelector("[data-conversation-coverage-action]");
      if (copy) copy.textContent = payload.coverage_notice || "";
      if (action && payload.sources_url) action.href = payload.sources_url;
      conversationCoverage.hidden = !partial;
    }
    window.dispatchEvent(new CustomEvent("recordbench:readiness", { detail: payload }));
  };

  const pollReadiness = async () => {
    window.clearTimeout(readinessTimer);
    try {
      const response = await fetch(readiness.dataset.statusUrl, {
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.message || "Matter status is unavailable.");
      renderReadiness(payload);
      readinessTimer = window.setTimeout(pollReadiness, payload.poll_after_ms || 8000);
    } catch (_error) {
      const updated = readiness.querySelector("[data-readiness-updated]");
      if (updated) updated.textContent = "Status update paused · Reconnecting automatically. Background work continues.";
      readinessTimer = window.setTimeout(pollReadiness, 5000);
    }
  };

  readinessTimer = window.setTimeout(pollReadiness, 1200);
  window.addEventListener("pagehide", () => window.clearTimeout(readinessTimer));
})();

(() => {
  const main = document.querySelector("#main-content");
  const skip = document.querySelector(".skip-link");
  if (!main || !skip) return;
  skip.addEventListener("click", (event) => {
    event.preventDefault();
    main.focus({ preventScroll: true });
    main.scrollIntoView({ block: "start", behavior: "instant" });
  });
})();

/* One set of destinations: direct links when the workspace has room, a named
   section navigator when panels or enlarged text leave less working space. */
(() => {
  const tabs = document.querySelector(".matter-section-tabs");
  if (!tabs) return;
  const links = Array.from(tabs.querySelectorAll(":scope > a"));
  if (!links.length) return;
  const disclosure = document.createElement("details");
  disclosure.className = "matter-section-disclosure";
  const summary = document.createElement("summary");
  const current = links.find((link) => link.getAttribute("aria-current") === "page");
  const title = document.createElement("span");
  title.textContent = current ? current.textContent.trim() : "Case workspace";
  const caption = document.createElement("small");
  caption.textContent = "Current section";
  const copy = document.createElement("span");
  copy.className = "section-current";
  copy.append(caption, title);
  summary.append(copy);
  summary.setAttribute("aria-label", `Current section: ${title.textContent}. Change section`);
  const choices = document.createElement("div");
  choices.className = "section-destinations";
  disclosure.append(summary, choices);
  links.forEach((link) => {
    const description = link.dataset.sectionDescription;
    if (!description) return;
    const detail = document.createElement("small");
    detail.className = "section-description";
    detail.textContent = description;
    link.append(detail);
  });
  let closedWidth = tabs.clientWidth;
  const place = () => {
    // Opening the navigator widens its row. Keep the closed measurement so the
    // menu does not look wide enough for direct links and detach itself.
    if (!(disclosure.isConnected && disclosure.open)) closedWidth = tabs.clientWidth;
    const width = disclosure.isConnected && disclosure.open ? closedWidth : tabs.clientWidth;
    // A content breakpoint, scaled with the user's root font size. Direct links
    // also wrap as a fallback, so a long label can never become unreachable.
    const narrow = width < 26 * parseFloat(getComputedStyle(document.documentElement).fontSize);
    const focused = document.activeElement;
    if (narrow && !disclosure.isConnected) {
      tabs.classList.add("sections-compact");
      tabs.prepend(disclosure);
      links.forEach((link) => choices.append(link));
      if (links.some((link) => link.contains(focused))) {
        disclosure.open = true;
        focused.focus({ preventScroll: true });
      }
    } else if (!narrow && disclosure.isConnected) {
      links.forEach((link) => tabs.append(link));
      disclosure.remove();
      tabs.classList.remove("sections-compact");
      if (focused === summary) (current || links[0]).focus({ preventScroll: true });
      else if (links.some((link) => link.contains(focused))) focused.focus({ preventScroll: true });
    }
  };
  disclosure.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || !disclosure.open) return;
    event.preventDefault();
    disclosure.open = false;
    summary.focus();
  });
  place();
  new ResizeObserver(place).observe(tabs);
  window.addEventListener("resize", place);
  document.fonts?.ready.then(place);
})();

/* A workspace-wide activity drawer keeps background work visible across pages. */
(() => {
  const body = document.body;
  const drawer = document.querySelector("[data-activity-drawer]");
  const contentSlot = drawer?.querySelector("[data-activity-body]");
  const toggle = document.querySelector("[data-activity-toggle]");
  const scrim = document.querySelector("[data-activity-scrim]");
  const badge = document.querySelector("[data-activity-badge]");
  if (!drawer || !contentSlot || !toggle) return;

  const closeButton = drawer.querySelector("[data-activity-close]");
  const backgroundState = new Map();
  let activityTimer = 0;
  let loaded = false;
  let loading = false;
  let opener = null;
  let pageHidden = false;

  const isOpen = () => body.classList.contains("activity-open");
  const canFocus = (element) => element instanceof HTMLElement && element.isConnected
    && (element.tabIndex >= 0 || element.hasAttribute("tabindex") || element.isContentEditable)
    && !element.matches(":disabled") && !element.closest("[inert], [hidden]") && element.getClientRects().length > 0
    && getComputedStyle(element).visibility !== "hidden";
  const focusable = () => Array.from(drawer.querySelectorAll('a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')).filter(canFocus);
  const focusClose = () => (canFocus(closeButton) ? closeButton : drawer).focus({ preventScroll: true });

  // Only siblings along the dialog's ancestor path are background. Making the
  // whole app frame inert would also disable the dialog nested inside it.
  const suspendBackground = () => {
    for (let active = drawer; active && active !== body; active = active.parentElement) {
      for (const sibling of active.parentElement.children) {
        if (sibling === active || sibling === scrim || !(sibling instanceof HTMLElement)) continue;
        if (!backgroundState.has(sibling)) backgroundState.set(sibling, sibling.getAttribute("inert"));
        sibling.inert = true;
      }
    }
  };
  const restoreBackground = () => {
    for (const [element, previous] of backgroundState) {
      if (previous === null) element.removeAttribute("inert");
      else element.setAttribute("inert", previous);
    }
    backgroundState.clear();
  };

  const setActivityOpen = (open) => {
    if (open === isOpen()) return;
    if (open) opener = document.activeElement;
    body.classList.toggle("activity-open", open);
    drawer.setAttribute("aria-hidden", open ? "false" : "true");
    drawer.inert = !open;
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    toggle.setAttribute("aria-label", open ? "Close background activity" : "Open background activity");
    if (open) {
      suspendBackground();
      focusClose();
      refreshActivity();
    } else {
      restoreBackground();
      const target = canFocus(opener) ? opener : canFocus(toggle) ? toggle : document.querySelector("#main-content");
      target?.focus({ preventScroll: true });
      opener = null;
    }
  };

  const updateActivityBadge = (content) => {
    if (!badge || !content) return;
    const count = Number(content.dataset.badgeCount || 0);
    badge.textContent = count > 99 ? "99+" : String(count);
    badge.hidden = count <= 0;
    toggle.classList.toggle("has-attention", Number(content.dataset.attentionCount || 0) > 0);
    toggle.classList.toggle("is-working", Number(content.dataset.activeCount || 0) > 0);
  };

  const scheduleActivity = (delay) => {
    window.clearTimeout(activityTimer);
    if (!pageHidden) activityTimer = window.setTimeout(refreshActivity, Math.max(Number(delay) || 15000, 1500));
  };

  async function refreshActivity() {
    if (loading || document.visibilityState === "hidden") {
      scheduleActivity(3000);
      return;
    }
    loading = true;
    const loadingPanel = contentSlot.querySelector("[data-activity-loading]");
    if (loadingPanel) {
      loadingPanel.querySelector("strong").textContent = "Checking background work…";
      loadingPanel.querySelector(".activity-loading-mark").hidden = false;
      loadingPanel.querySelector("[data-activity-error]").hidden = true;
      // Keep a focused Retry button in place until a successful response lands.
      loadingPanel.querySelector("[data-activity-retry]").setAttribute("aria-disabled", "true");
    }
    try {
      const response = await fetch(drawer.dataset.activityUrl, {
        headers: { Accept: "text/html" },
        cache: "no-store",
      });
      if (!response.ok) throw new Error("Activity is temporarily unavailable.");
      const holder = document.createElement("div");
      holder.innerHTML = await response.text();
      const content = holder.querySelector("[data-activity-content]");
      if (!content) throw new Error("Activity response was incomplete.");
      const focused = document.activeElement;
      const replaceFocused = isOpen() && contentSlot.contains(focused);
      const action = focused?.dataset?.activityAction;
      const scrollTop = contentSlot.querySelector(".activity-list")?.scrollTop || 0;
      contentSlot.replaceChildren(content);
      // The permanent header never loses focus. A replaced record action keeps
      // its identity, even when a new status changes its position in the list.
      if (replaceFocused) {
        const replacement = action && Array.from(content.querySelectorAll("[data-activity-action]"))
          .find((element) => element.dataset.activityAction === action);
        if (canFocus(replacement)) replacement.focus({ preventScroll: true });
        else focusClose();
      }
      const list = content.querySelector(".activity-list");
      if (list) list.scrollTop = scrollTop;
      updateActivityBadge(content);
      loaded = true;
      scheduleActivity(content.dataset.pollAfterMs);
    } catch (_error) {
      if (loadingPanel?.isConnected) {
        loadingPanel.querySelector("strong").textContent = "Activity couldn’t be updated.";
        loadingPanel.querySelector(".activity-loading-mark").hidden = true;
        loadingPanel.querySelector("[data-activity-error]").hidden = false;
        const retry = loadingPanel.querySelector("[data-activity-retry]");
        retry.hidden = false;
        retry.removeAttribute("aria-disabled");
      }
      if (loaded) {
        contentSlot.querySelector("[data-activity-content]")?.classList.add("is-stale");
        const updated = contentSlot.querySelector("[data-activity-updated]");
        if (updated) updated.textContent = "Updates paused. Retrying…";
      }
      scheduleActivity(5000);
    } finally {
      loading = false;
    }
  }

  toggle.addEventListener("click", () => setActivityOpen(!isOpen()));
  scrim?.addEventListener("click", () => setActivityOpen(false));
  drawer.addEventListener("click", (event) => {
    if (event.target.closest("[data-activity-close]")) setActivityOpen(false);
    if (event.target.closest("[data-activity-retry]")) refreshActivity();
  });
  document.addEventListener("focusin", (event) => {
    if (isOpen() && !drawer.contains(event.target)) focusClose();
  });
  document.addEventListener("keydown", (event) => {
    if (!isOpen()) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      setActivityOpen(false);
    } else if (event.key === "Tab") {
      const items = focusable();
      const first = items[0], last = items.at(-1);
      if (!items.length) {
        event.preventDefault();
        drawer.focus({ preventScroll: true });
      } else if (!drawer.contains(document.activeElement) || document.activeElement === drawer) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus({ preventScroll: true });
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus({ preventScroll: true });
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus({ preventScroll: true });
      }
    }
  }, true);
  window.addEventListener("recordbench:readiness", () => scheduleActivity(400));
  window.addEventListener("pagehide", () => {
    pageHidden = true;
    window.clearTimeout(activityTimer);
  });
  window.addEventListener("pageshow", () => {
    pageHidden = false;
    scheduleActivity(600);
  });
  scheduleActivity(600);
})();

/* Compact matter navigation, persisted appearance, and the cross-workspace assistant. */
(() => {
  const body = document.body;
  const csrfToken = body.dataset.csrfToken || "";

  const matterFilter = document.querySelector("[data-matter-filter]");
  const matterItems = Array.from(document.querySelectorAll("[data-matter-item]"));
  const matterFilterEmpty = document.querySelector("[data-matter-filter-empty]");
  matterFilter?.addEventListener("input", () => {
    const query = matterFilter.value.trim().toLocaleLowerCase();
    let visible = 0;
    matterItems.forEach((item) => {
      const matches = !query || (item.dataset.searchText || "").toLocaleLowerCase().includes(query);
      item.hidden = !matches;
      if (matches) visible += 1;
    });
    if (matterFilterEmpty) matterFilterEmpty.hidden = visible !== 0;
  });

  document.querySelectorAll("[data-theme-picker]").forEach((themePicker) => {
    themePicker.addEventListener("change", () => {
      document.documentElement.dataset.theme = themePicker.value;
      themePicker.closest("[data-theme-form]")?.requestSubmit();
    });
  });

  const assistantPreferenceKey = "recordbench:assistant:display:v2";
  const compactAssistant = window.matchMedia("(max-width: 900px)");
  const assistantDisplayKey = () => compactAssistant.matches
    ? `${assistantPreferenceKey}:compact` : assistantPreferenceKey;
  let assistantDock = document.querySelector("[data-assistant-dock]");
  let assistantPollTimer = 0;
  let assistantDraft = false;
  let assistantDraftBuffer = null;
  let assistantEpoch = 0;
  // Draft saves and dock refreshes are coordinated: no save starts while a refresh is in
  // flight, and a refresh waits for pending saves, then keeps their results visible.
  let assistantRefreshesInFlight = 0;
  let assistantSavesSettled = [];
  // Open, unsaved question drafts keyed by their form's action, carried into a refreshed dock.
  let assistantCarriedDrafts = new Map();
  const assistantQuestionWritesPending = () => Number(assistantDock?.dataset.questionSavesPending || 0)
    + Number(assistantDock?.dataset.questionDraftsPending || 0);
  const settleAssistantQuestionWrites = () => {
    if (!assistantQuestionWritesPending()) assistantSavesSettled.splice(0).forEach((resolve) => resolve());
  };
  // Changing or starting a conversation would replace the dock and hide a pending draft or
  // save, so the picker and New chat are held until every pending draft and save is known.
  const holdAssistantNavigation = (dock) => {
    const picker = dock.querySelector("[data-assistant-conversation-picker]");
    const holds = Number(dock.dataset.questionNavigationHolds || 0);
    if (!holds) dock.dataset.questionPickerWasDisabled = String(Boolean(picker?.disabled));
    dock.dataset.questionNavigationHolds = String(holds + 1);
    if (picker) picker.disabled = true;
    dock.querySelector("[data-assistant-new-chat]")?.setAttribute("aria-disabled", "true");
  };
  const releaseAssistantNavigation = (dock) => {
    const remaining = Number(dock.dataset.questionNavigationHolds || 1) - 1;
    if (remaining > 0) {
      dock.dataset.questionNavigationHolds = String(remaining);
      return;
    }
    const picker = dock.querySelector("[data-assistant-conversation-picker]");
    const pickerWasDisabled = dock.dataset.questionPickerWasDisabled === "true";
    delete dock.dataset.questionNavigationHolds;
    delete dock.dataset.questionPickerWasDisabled;
    if (picker && !pickerWasDisabled) picker.disabled = false;
    dock.querySelector("[data-assistant-new-chat]")?.removeAttribute("aria-disabled");
  };
  let assistantHistorySwitch = null;
  let reviewQuestionScope = null;
  // Set by the source reader: limits the next question to the open source.
  let selectReviewSource = null;

  const releaseAssistantHistorySwitch = (operation = assistantHistorySwitch) => {
    if (!operation || assistantHistorySwitch !== operation) return;
    assistantHistorySwitch = null;
    if (operation.dock !== assistantDock || !operation.picker.isConnected) return;
    operation.picker.disabled = false;
    operation.picker.value = assistantDraft ? "__draft__" : assistantDock.dataset.conversationId || "";
  };

  const assistantConversationPreferenceKey = () => {
    const matterId = assistantDock?.dataset.matterId || "";
    return matterId ? `recordbench:assistant:conversation:${matterId}` : "";
  };

  const readAssistantPreference = () => {
    try {
      const saved = window.localStorage.getItem(assistantDisplayKey());
      if (saved === "collapsed") return true;
      if (saved === "open") return false;
      return body.dataset.assistantDefault === "collapsed"
        || window.matchMedia("(max-width: 900px)").matches;
    } catch (_error) {
      return body.dataset.assistantDefault === "collapsed"
        || window.matchMedia("(max-width: 900px)").matches;
    }
  };

  const saveAssistantPreference = (collapsed) => {
    try {
      window.localStorage.setItem(assistantDisplayKey(), collapsed ? "collapsed" : "open");
    } catch (_error) {
      // The assistant remains usable when browser preference storage is unavailable.
    }
  };

  const readAssistantConversationPreference = () => {
    try {
      const key = assistantConversationPreferenceKey();
      return key ? window.localStorage.getItem(key) || "" : "";
    } catch (_error) {
      return "";
    }
  };

  const saveAssistantConversationPreference = (conversationId) => {
    try {
      const key = assistantConversationPreferenceKey();
      if (key && conversationId) window.localStorage.setItem(key, conversationId);
    } catch (_error) {
      // The selected chat still works for this page without local storage.
    }
  };

  const assistantFragmentUrl = (conversationId) => {
    const current = assistantDock?.dataset.fragmentUrl;
    if (!current) return "";
    const url = new URL(current, window.location.origin);
    url.searchParams.set("conversation", conversationId);
    return `${url.pathname}${url.search}`;
  };

  const setAssistantCollapsed = (collapsed, { focus = false, persist = true, preserveFocus = false } = {}) => {
    const pageX = window.scrollX;
    const pageY = window.scrollY;
    const previousFocus = document.activeElement;
    body.classList.toggle("assistant-collapsed", collapsed);
    if (persist) saveAssistantPreference(collapsed);
    window.scrollTo(pageX, pageY);
    if (focus) {
      window.requestAnimationFrame(() => {
        window.scrollTo(pageX, pageY);
        const previousVisible = preserveFocus && previousFocus?.isConnected
          && !previousFocus.disabled && previousFocus.getClientRects().length
          && getComputedStyle(previousFocus).visibility !== "hidden";
        const target = previousVisible ? previousFocus : collapsed
          ? assistantDock?.querySelector("[data-assistant-expand]")
          : assistantDock?.querySelector("textarea:not(:disabled)")
            || assistantDock?.querySelector("[data-assistant-collapse]");
        target?.focus({ preventScroll: true });
      });
    }
  };

  const assistantJson = async (response) => {
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.message || payload.detail || "The request could not be completed.");
    }
    return payload;
  };

  const assistantStatusMessage = (job) => {
    if (job.queue_position) return `${job.stage_label || "Request saved"} · ${job.queue_position} in queue`;
    if (["failed", "cancelled"].includes(job.state)) return job.message || (job.state === "cancelled" ? "Request cancelled" : "Request needs attention");
    return job.stage_label || job.message || "Working from the matter sources";
  };

  const newAssistantRequestKey = () => {
    const field = assistantDock?.querySelector("[data-assistant-request-key]");
    if (!field || !window.crypto?.getRandomValues) return;
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    field.value = `answer-request-${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}`;
  };

  const appendAssistantUserMessage = (question) => {
    const thread = assistantDock?.querySelector("[data-assistant-thread]");
    if (!thread) return;
    if (thread.dataset.hasMessages !== "true") thread.replaceChildren();
    const article = document.createElement("article");
    article.className = "assistant-message assistant-user-message";
    const avatar = document.createElement("span");
    avatar.className = "assistant-message-avatar";
    avatar.setAttribute("aria-hidden", "true");
    avatar.textContent = "You";
    const content = document.createElement("div");
    const label = document.createElement("strong");
    label.textContent = "You";
    const bodyCopy = document.createElement("p");
    bodyCopy.textContent = question;
    content.append(label, bodyCopy);
    article.append(avatar, content);
    thread.append(article);
    thread.dataset.hasMessages = "true";
    thread.scrollTop = thread.scrollHeight;
  };

  const ensureAssistantDraftOption = () => {
    if (!assistantDraftBuffer || !assistantDock) return;
    const picker = assistantDock.querySelector("[data-assistant-conversation-picker]");
    if (!picker || picker.querySelector('[value="__draft__"]')) return;
    const option = document.createElement("option");
    option.value = "__draft__";
    option.textContent = "Draft · Unsaved";
    option.dataset.assistantDraftOption = "true";
    picker.prepend(option);
  };

  const beginAssistantDraft = () => {
    if (!assistantDock) return;
    assistantEpoch += 1;
    window.clearTimeout(assistantPollTimer);
    releaseAssistantHistorySwitch();
    if (assistantDraft && assistantDock.querySelector("[data-assistant-question-form]")?.getAttribute("aria-busy") !== "true") {
      const picker = assistantDock.querySelector("[data-assistant-conversation-picker]");
      if (picker) { picker.value = "__draft__"; picker.disabled = false; }
      assistantDraftBuffer = null;
      assistantDock.querySelector("textarea")?.focus({ preventScroll: true });
      return;
    }
    const buffered = assistantDraftBuffer;
    assistantDraftBuffer = null;
    window.clearTimeout(assistantPollTimer);
    assistantDraft = true;
    assistantDock.classList.add("is-draft");
    assistantDock.dataset.conversationId = "";
    const picker = assistantDock.querySelector("[data-assistant-conversation-picker]");
    picker?.querySelector("[data-assistant-draft-option]")?.remove();
    if (picker) {
      const option = document.createElement("option");
      option.value = "__draft__";
      option.textContent = "Draft · Unsaved";
      option.dataset.assistantDraftOption = "true";
      picker.prepend(option);
      picker.value = "__draft__";
      picker.disabled = false;
    }
    const thread = assistantDock.querySelector("[data-assistant-thread]");
    if (thread) {
      const existingMark = assistantDock.querySelector(".assistant-brand-mark");
      const mark = existingMark
        ? existingMark.outerHTML
        : '<span class="assistant-brand-mark" aria-hidden="true">EX</span>';
      thread.innerHTML = `<div class="assistant-empty assistant-draft-empty">${mark}<h2>New chat</h2><p>This draft is saved with the matter when you send its first question.</p></div>`;
      thread.dataset.hasMessages = "false";
      const suggestions = assistantDock.querySelector("[data-assistant-suggestions-template]");
      const empty = thread.querySelector(".assistant-draft-empty");
      if (suggestions && empty) {
        empty.append(suggestions.content.cloneNode(true));
        bindAssistantSuggestions(empty);
      }
    }
    const status = assistantDock.querySelector("[data-assistant-status]");
    if (status) {
      status.hidden = true;
      status.dataset.statusUrl = "";
      status.dataset.state = "";
      status.classList.remove("is-terminal", "is-failed");
    }
    const recovery = assistantDock.querySelector("[data-assistant-recovery]");
    if (recovery) recovery.hidden = true;
    const form = assistantDock.querySelector("[data-assistant-question-form]");
    const conversation = form?.querySelector('input[name="conversation"]');
    const textarea = form?.querySelector("textarea");
    const submit = form?.querySelector('button[type="submit"]');
    if (conversation) conversation.value = "";
    if (textarea) {
      textarea.value = buffered?.question || "";
      textarea.disabled = form?.dataset.sourcesReady !== "true";
      resizeAssistantTextarea(textarea);
    }
    if (submit) submit.disabled = form?.dataset.sourcesReady !== "true";
    form?.removeAttribute("aria-busy");
    const scope = form?.querySelector('select[name="source_set"]');
    if (scope) {
      scope.value = buffered?.sourceSet || "";
      if (scope.selectedIndex < 0) scope.selectedIndex = 0;
      reviewQuestionScope = { id: scope.value, name: scope.selectedOptions[0]?.textContent || "" };
    }
    const requestKey = form?.querySelector("[data-assistant-request-key]");
    if (requestKey && buffered?.requestKey) requestKey.value = buffered.requestKey;
    else newAssistantRequestKey();
    textarea?.focus({ preventScroll: true });
  };

  const saveAcceptedAssistantDraft = (job) => {
    if (!assistantDock || !job?.conversation_id) return;
    assistantDraft = false;
    assistantDraftBuffer = null;
    assistantDock.classList.remove("is-draft");
    assistantDock.dataset.conversationId = job.conversation_id;
    if (job.fragment_url) {
      // Keep the page the dock is shown on, which the job's own URL does not carry.
      const next = new URL(job.fragment_url, window.location.origin);
      const page = new URL(assistantDock.dataset.fragmentUrl || job.fragment_url, window.location.origin).searchParams.get("assistant_from");
      if (page && !next.searchParams.has("assistant_from")) next.searchParams.set("assistant_from", page);
      assistantDock.dataset.fragmentUrl = `${next.pathname}${next.search}`;
    }
    const form = assistantDock.querySelector("[data-assistant-question-form]");
    const conversation = form?.querySelector('input[name="conversation"]');
    if (conversation) conversation.value = job.conversation_id;
    const picker = assistantDock.querySelector("[data-assistant-conversation-picker]");
    const draftOption = picker?.querySelector("[data-assistant-draft-option]");
    if (draftOption) {
      draftOption.value = job.conversation_id;
      draftOption.textContent = `${job.conversation_title || "New chat"} · 1 message`;
      draftOption.removeAttribute("data-assistant-draft-option");
      draftOption.selected = true;
    }
    saveAssistantConversationPreference(job.conversation_id);
  };

  const renderAssistantJob = (job) => {
    if (!assistantDock || !job) return;
    const status = assistantDock.querySelector("[data-assistant-status]");
    const copy = assistantDock.querySelector("[data-assistant-status-copy]");
    const form = assistantDock.querySelector("[data-assistant-question-form]");
    const textarea = form?.querySelector("textarea");
    const submit = form?.querySelector('button[type="submit"]');
    const working = ["queued", "running"].includes(job.state);
    if (status) {
      status.hidden = false;
      status.dataset.statusUrl = job.status_url || "";
      status.dataset.state = job.state || "";
      status.classList.toggle("is-terminal", !working);
      status.classList.toggle("is-failed", job.state === "failed");
      let cancel = status.querySelector("[data-assistant-cancel]");
      if (working && job.can_cancel && job.cancel_url) {
        if (!cancel) { cancel = document.createElement("button"); cancel.type = "button"; cancel.dataset.assistantCancel = ""; cancel.textContent = "Cancel"; status.append(cancel); }
        cancel.dataset.actionUrl = job.cancel_url;
        cancel.disabled = false;
      } else cancel?.remove();
    }
    if (copy) copy.textContent = assistantStatusMessage(job);
    if (textarea) textarea.disabled = working || form.dataset.sourcesReady !== "true";
    if (submit) submit.disabled = working || form.dataset.sourcesReady !== "true";
    form?.toggleAttribute("aria-busy", working);
  };

  const ownsAssistantOperation = (operation) => operation.epoch === assistantEpoch
    && operation.dock === assistantDock;

  const assistantHistoryFailure = "The request finished, but its history could not refresh. Your draft is kept. Open Full conversation to inspect the saved result.";

  const recoverAssistantRead = (message) => {
    const status = assistantDock?.querySelector("[data-assistant-status]");
    const copy = status?.querySelector("[data-assistant-status-copy]");
    if (status) status.hidden = false;
    if (copy) copy.textContent = message;
    window.clearTimeout(assistantPollTimer);
    // Resume reads only. An uncertain cancellation must never retry its POST,
    // and terminal jobs must not be resurrected by a recovery timer.
    if (status?.dataset.statusUrl && ["queued", "running"].includes(status.dataset.state)) {
      assistantPollTimer = window.setTimeout(pollAssistant, 1000);
    }
  };

  const replaceAssistantDock = async (requestedFragmentUrl, operation) => {
    window.clearTimeout(assistantPollTimer);
    if (!requestedFragmentUrl) releaseAssistantHistorySwitch();
    const fragmentUrl = requestedFragmentUrl || assistantDock?.dataset.fragmentUrl;
    // Transfer ownership synchronously to the refresh, including its failure
    // path. Callers never predict which epoch this function will allocate.
    operation.epoch = ++assistantEpoch;
    operation.dock = assistantDock;
    if (!fragmentUrl) throw new Error("Assistant history is unavailable. Open Full conversation.");
    const originatingDock = operation.dock;
    const originatingComposer = originatingDock.querySelector("[data-assistant-question-form]");
    const response = await fetch(fragmentUrl, {
      headers: { Accept: "text/html" },
      cache: "no-store",
    });
    if (!response.ok) throw new Error("Assistant status is temporarily unavailable.");
    const holder = document.createElement("div");
    holder.innerHTML = await response.text();
    if (!ownsAssistantOperation(operation)) return;
    const replacement = holder.querySelector("[data-assistant-dock]");
    if (!replacement || !assistantDock) throw new Error("Assistant response was incomplete.");
    // Replacing the dock would hide a pending draft or draft save's result, so wait for every one.
    while (assistantQuestionWritesPending() > 0) {
      await new Promise((resolve) => assistantSavesSettled.push(resolve));
      if (!ownsAssistantOperation(operation)) return;
    }
    // Open, unsaved drafts stay open for review in the refreshed dock.
    assistantCarriedDrafts = new Map(Array.from(assistantDock.querySelectorAll("[data-assistant-draft-questions]"))
      .map((form) => [form.getAttribute("action"), form.nextElementSibling])
      .filter(([, card]) => card && !card.hidden && card.questionDraft)
      .map(([action, card]) => [action, card.questionDraft]));
    let savedNotices = [];
    try {
      savedNotices = JSON.parse(assistantDock.dataset.questionSaveNotices || "[]");
    } catch (_error) {
      savedNotices = [];
    }
    // A terminal job enables writing before its refreshed history arrives.
    // Preserve the current composer state, including choices made before the
    // fetch, only within the same conversation and epoch (scope changes and
    // New chat invalidate the refresh above).
    const sameConversation = replacement.dataset.conversationId === originatingDock.dataset.conversationId;
    const currentTextarea = originatingComposer?.querySelector("textarea");
    const replacementComposer = replacement.querySelector("[data-assistant-question-form]");
    const replacementTextarea = replacementComposer?.querySelector("textarea");
    const retainFocus = sameConversation && document.activeElement === currentTextarea;
    const selection = retainFocus ? [currentTextarea.selectionStart, currentTextarea.selectionEnd] : null;
    if (sameConversation && currentTextarea && replacementTextarea) {
      replacementTextarea.value = currentTextarea.value;
      const currentContext = originatingComposer.querySelector('[name="use_saved_context"]');
      const replacementContext = replacementComposer.querySelector('[name="use_saved_context"]');
      if (currentContext && replacementContext) {
        replacementContext.checked = currentContext.checked;
        const revision = originatingComposer.querySelector('[name="expected_selection_revision"]');
        const replacementRevision = replacementComposer.querySelector('[name="expected_selection_revision"]');
        if (revision && replacementRevision) replacementRevision.value = revision.value;
      }
    }
    assistantDock.replaceWith(replacement);
    assistantDock = replacement;
    assistantDraft = false;
    saveAssistantConversationPreference(assistantDock.dataset.conversationId || "");
    ensureAssistantDraftOption();
    bindAssistant();
    // A draft save that finished while this refresh waited keeps its result and link.
    savedNotices.forEach((notice) => {
      const draftForm = Array.from(assistantDock.querySelectorAll("[data-assistant-draft-questions]"))
        .find((candidate) => candidate.getAttribute("action") === notice.action);
      const feedback = draftForm?.querySelector("[data-question-draft-status]");
      if (!feedback) return;
      const link = document.createElement("a");
      link.href = notice.url;
      link.textContent = "Open case notes";
      feedback.replaceChildren(document.createTextNode(`${notice.message} `), link);
    });
    if (retainFocus && replacementTextarea && !replacementTextarea.disabled) {
      replacementTextarea.focus({ preventScroll: true });
      replacementTextarea.setSelectionRange(...selection);
    }
    const thread = assistantDock.querySelector("[data-assistant-thread]");
    if (thread?.dataset.hasMessages === "true") thread.scrollTop = thread.scrollHeight;
  };

  const refreshAssistant = async (requestedFragmentUrl, operation) => {
    assistantRefreshesInFlight += 1;
    try {
      return await replaceAssistantDock(requestedFragmentUrl, operation);
    } finally {
      assistantRefreshesInFlight -= 1;
    }
  };

  const pollAssistant = async () => {
    window.clearTimeout(assistantPollTimer);
    const status = assistantDock?.querySelector("[data-assistant-status]");
    const statusUrl = status?.dataset.statusUrl;
    // Terminal jobs have already restored the composer. Re-polling them can
    // replace a newer draft after a scope change or a bfcache restoration.
    if (!statusUrl || !["queued", "running"].includes(status?.dataset.state || "")) return;
    const operation = { epoch: assistantEpoch, dock: assistantDock };
    try {
      const response = await fetch(statusUrl, {
        headers: { Accept: "application/json" },
        cache: "no-store",
      });
      const job = await assistantJson(response);
      if (!ownsAssistantOperation(operation)) return;
      renderAssistantJob(job);
      if (["succeeded", "failed", "cancelled"].includes(job.state)) {
        await refreshAssistant("", operation);
        return;
      }
      assistantPollTimer = window.setTimeout(pollAssistant, 1000);
    } catch (_error) {
      if (!ownsAssistantOperation(operation)) return;
      const copy = assistantDock?.querySelector("[data-assistant-status-copy]");
      if (!["queued", "running"].includes(status.dataset.state || "")) {
        if (copy) copy.textContent = assistantHistoryFailure;
        return;
      }
      if (copy) copy.textContent = "Reconnecting to the saved request…";
      assistantPollTimer = window.setTimeout(pollAssistant, 3500);
    }
  };

  // Suggestions only fill the question box; a source suggestion first limits
  // the next question to the open source.
  const bindAssistantSuggestions = (root) => {
    root.querySelectorAll("[data-assistant-suggestion]").forEach((button) => {
      button.addEventListener("click", async () => {
        const textarea = assistantDock?.querySelector("[data-assistant-question-form] textarea");
        if (!textarea || textarea.disabled) return;
        if (button.dataset.assistantSuggestionScope === "source") {
          const group = button.closest(".assistant-suggestions");
          const status = group?.querySelector("[data-assistant-suggestion-status]");
          const sourceForm = document.querySelector("[data-review-ask-source]");
          // Nothing can be sent, typed or chosen until the scope is settled, so a
          // question cannot go out with the previous scope or be overwritten.
          // The flag also keeps readiness updates and submission from reopening it.
          const composer = textarea.form;
          const submit = composer?.querySelector('button[type="submit"]');
          const choices = [...(group?.querySelectorAll("[data-assistant-suggestion]") || [])];
          const heldChoices = choices.filter((control) => !control.disabled);
          if (composer) composer.dataset.scopePending = "true";
          [textarea, submit, ...heldChoices].forEach((control) => { if (control) control.disabled = true; });
          group?.setAttribute("aria-busy", "true");
          if (status) status.textContent = "Limiting your next question to this source…";
          let selected = false;
          try {
            selected = Boolean(sourceForm && selectReviewSource && await selectReviewSource(sourceForm));
          } finally {
            if (composer) delete composer.dataset.scopePending;
            heldChoices.forEach((control) => { control.disabled = false; });
            group?.removeAttribute("aria-busy");
            // Restore the composer from current readiness and any answer in progress.
            const state = assistantDock?.querySelector("[data-assistant-status]")?.dataset.state || "";
            const blocked = composer?.dataset.sourcesReady !== "true" || composer?.getAttribute("aria-busy") === "true"
              || ["queued", "running"].includes(state);
            textarea.disabled = blocked;
            if (submit) submit.disabled = blocked;
          }
          if (status) status.textContent = selected ? "Your next question is limited to this source."
            : "This source could not be selected, so nothing was filled in. Try again or choose Ask using this source.";
          if (!selected || !textarea.isConnected) return;
        }
        textarea.value = button.dataset.assistantSuggestion || "";
        resizeAssistantTextarea(textarea);
        textarea.focus({ preventScroll: true });
      });
    });
  };

  const resizeAssistantTextarea = (textarea) => {
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 102)}px`;
  };

  const resumeAssistantAfterScopeChange = () => {
    const status = assistantDock?.querySelector("[data-assistant-status]");
    if (status?.dataset.statusUrl && ["queued", "running"].includes(status.dataset.state)) {
      pollAssistant();
      return;
    }
    const composer = assistantDock?.querySelector("[data-assistant-question-form]");
    if (composer?.getAttribute("aria-busy") === "true") {
      composer.removeAttribute("aria-busy");
      composer.querySelectorAll("textarea, button[type=submit]").forEach((control) => { control.disabled = composer.dataset.sourcesReady !== "true"; });
      newAssistantRequestKey();
      const copy = status?.querySelector("[data-assistant-status-copy]");
      if (copy) copy.textContent = "Scope changed. Any earlier submitted question remains in its original conversation.";
    }
  };

  const applyReviewQuestionScope = () => {
    if (!reviewQuestionScope || !assistantDock) return;
    const select = assistantDock.querySelector('select[name="source_set"]');
    if (!select) return;
    if (!Array.from(select.options).some((option) => option.value === reviewQuestionScope.id)) {
      select.add(new Option(reviewQuestionScope.name, reviewQuestionScope.id));
    }
    select.value = reviewQuestionScope.id;
  };

  function bindAssistant() {
    if (!assistantDock || assistantDock.dataset.bound === "true") return;
    assistantDock.dataset.bound = "true";
    applyReviewQuestionScope();
    assistantDock.querySelector('select[name="source_set"]')?.addEventListener("change", (event) => {
      assistantEpoch += 1;
      window.clearTimeout(assistantPollTimer);
      releaseAssistantHistorySwitch();
      reviewQuestionScope = { id: event.target.value, name: event.target.selectedOptions[0]?.textContent || "" };
      resumeAssistantAfterScopeChange();
    });
    assistantDock.querySelector("[data-assistant-collapse]")?.addEventListener("click", () => {
      setAssistantCollapsed(true, { focus: true });
    });
    assistantDock.querySelector("[data-assistant-expand]")?.addEventListener("click", () => {
      setAssistantCollapsed(false, { focus: true });
    });

    assistantDock.querySelectorAll("[data-assistant-save-passage]").forEach((saveForm) => {
      saveForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (saveForm.getAttribute("aria-busy") === "true") return;
        const button = saveForm.querySelector('button[type="submit"]');
        const feedback = saveForm.querySelector("[data-save-passage-status]");
        const submission = new FormData(saveForm);
        saveForm.setAttribute("aria-busy", "true");
        button.setAttribute("aria-disabled", "true");
        feedback.textContent = "Saving passage…";
        try {
          const response = await fetch(saveForm.action, {
            method: "POST", body: submission,
            headers: { Accept: "application/json", "X-CSRF-Token": csrfToken },
          });
          const result = await assistantJson(response);
          if (!result.item_id) throw new Error("The save could not be confirmed.");
          // A late response belongs only to its original claim, never a newly
          // selected conversation or reader. Do not refresh either surface.
          if (saveForm.isConnected) feedback.textContent = result.message;
        } catch (error) {
          if (saveForm.isConnected) feedback.textContent = `${error.message} You can safely try saving again.`;
        } finally {
          saveForm.removeAttribute("aria-busy");
          button.removeAttribute("aria-disabled");
        }
      });
    });

    // Suggest from an answer passage: preview what would be added, then Add or Dismiss.
    // Nothing is written until Add; found names are only ever set as text.
    assistantDock.querySelectorAll("[data-assistant-suggest-passage]").forEach((suggestForm) => {
      const button = suggestForm.querySelector('button[type="submit"]');
      const feedback = suggestForm.querySelector("[data-suggest-passage-status]");
      const inbox = suggestForm.querySelector("[data-suggest-inbox]");
      const timeline = suggestForm.querySelector("[data-suggest-timeline]");
      const card = suggestForm.nextElementSibling;
      const busy = async (form, controls, message, work) => {
        if (form.getAttribute("aria-busy") === "true") return;
        form.setAttribute("aria-busy", "true");
        controls.forEach((control) => control.setAttribute("aria-disabled", "true"));
        feedback.textContent = message;
        try {
          await work();
        } catch (error) {
          if (form.isConnected) feedback.textContent = `${error.message} You can safely try again.`;
        } finally {
          form.removeAttribute("aria-busy");
          controls.forEach((control) => control.removeAttribute("aria-disabled"));
        }
      };
      const closeCard = () => {
        card.replaceChildren();
        card.hidden = true;
      };
      const showPreview = (preview) => {
        card.replaceChildren();
        const heading = document.createElement("h3");
        heading.textContent = "Suggestions from this passage";
        heading.tabIndex = -1;
        const list = document.createElement("ul");
        preview.items.forEach((item) => {
          const row = document.createElement("li");
          const label = document.createElement("strong");
          label.textContent = item.label;
          row.append(label, ` · ${item.kind_label}${item.count > 1 ? ` · found ${item.count} times` : ""}`);
          list.append(row);
        });
        const sources = document.createElement("p");
        sources.className = "suggestion-preview-sources";
        sources.textContent = `From: ${preview.passages.join("; ")}`;
        const addForm = document.createElement("form");
        addForm.method = "post";
        addForm.action = preview.save_url;
        const basis = document.createElement("input");
        basis.type = "hidden";
        basis.name = "basis";
        basis.value = preview.basis;
        const add = document.createElement("button");
        add.type = "submit";
        add.className = "button-primary";
        add.textContent = `Add ${preview.count} to suggestions`;
        const dismiss = document.createElement("button");
        dismiss.type = "button";
        dismiss.textContent = "Dismiss";
        const actions = document.createElement("div");
        actions.className = "suggestion-preview-actions";
        actions.append(add, dismiss);
        addForm.append(basis, actions);
        card.append(heading, list, sources, addForm);
        card.hidden = false;
        heading.focus();
        dismiss.addEventListener("click", () => {
          // While Add is pending its outcome is unknown, so Dismiss cannot claim nothing was added.
          if (addForm.getAttribute("aria-busy") === "true") return;
          closeCard();
          feedback.textContent = "Dismissed; nothing was added.";
          button.focus();
        });
        addForm.addEventListener("submit", (event) => {
          event.preventDefault();
          busy(addForm, [add, dismiss, button], "Adding suggestions…", async () => {
            const body = new FormData(addForm);
            const response = await fetch(addForm.action, {
              method: "POST", body, headers: { Accept: "application/json", "X-CSRF-Token": csrfToken },
            });
            const result = await assistantJson(response);
            if (typeof result.added !== "number") throw new Error("The suggestions could not be confirmed.");
            if (!suggestForm.isConnected) return;
            closeCard();
            feedback.textContent = result.message;
            if (inbox) inbox.hidden = false;
            if (timeline) timeline.hidden = !result.timeline_url;
            button.focus();
          });
        });
      };
      suggestForm.addEventListener("submit", (event) => {
        event.preventDefault();
        // A new preview is not requested while an Add from the current card is pending.
        if (card.querySelector('form[aria-busy="true"]')) return;
        busy(suggestForm, [button], "Finding people, things and dates in the cited passages…", async () => {
          const response = await fetch(suggestForm.action, {
            method: "POST", body: new FormData(suggestForm),
            headers: { Accept: "application/json", "X-CSRF-Token": csrfToken },
          });
          const preview = await assistantJson(response);
          if (!Array.isArray(preview.items) || !Array.isArray(preview.passages)) throw new Error("The suggestions could not be confirmed.");
          if (!suggestForm.isConnected) return;
          feedback.textContent = preview.message;
          // With nothing new, what was found may already be waiting in the inbox.
          if (inbox) inbox.hidden = Boolean(preview.items.length);
          if (timeline) timeline.hidden = true;
          if (preview.items.length) showPreview(preview);
          else closeCard();
        });
      });
    });

    // Draft questions from an answer's passages: a proposal card that saves nothing
    // until the reviewer chooses Save. Drafted text is only ever set as text.
    assistantDock.querySelectorAll("[data-assistant-draft-questions]").forEach((draftForm) => {
      const card = draftForm.nextElementSibling;
      const feedback = draftForm.querySelector("[data-question-draft-status]");
      const element = (tag, text, className) => {
        const node = document.createElement(tag);
        if (text) node.textContent = text;
        if (className) node.className = className;
        return node;
      };
      const hidden = (name, value) => {
        const input = element("input");
        input.type = "hidden";
        input.name = name;
        input.value = value;
        return input;
      };
      const showDraft = (draft, opener, { focus = true } = {}) => {
        card.replaceChildren();
        card.questionDraft = draft;
        const heading = element("h3", `${draft.label} · draft for review`);
        heading.tabIndex = -1;
        card.append(heading, element("p", "Nothing is saved yet. Check each question against its sources.", "question-draft-hint"));
        if (draft.notice) card.append(element("p", draft.notice, "question-draft-notice"));
        const saveForm = element("form");
        saveForm.method = "post";
        saveForm.action = draft.save_url;
        saveForm.append(hidden("csrf_token", csrfToken), hidden("purpose", draft.purpose), hidden("basis", draft.basis || ""));
        const list = element("ol", "", "question-draft-list");
        draft.questions.forEach((question) => {
          const item = element("li");
          item.append(element("p", question.text), element("small", `Sources: ${question.sources.join("; ")}`));
          saveForm.append(hidden("question", question.text), hidden("passages", question.passages.join(",")));
          list.append(item);
        });
        const actions = element("div", "", "question-draft-card-actions");
        const save = element("button", "Save to case notes", "button-primary");
        save.type = "submit";
        const dismiss = element("button", "Dismiss");
        dismiss.type = "button";
        const status = element("span", "", "question-draft-save-status");
        status.setAttribute("role", "status");
        status.setAttribute("aria-live", "polite");
        actions.append(save, dismiss, status);
        saveForm.append(list, actions);
        card.append(saveForm);
        card.hidden = false;
        if (focus) heading.focus();
        dismiss.addEventListener("click", () => {
          // While Save is pending its outcome is unknown, so Dismiss cannot claim nothing was saved;
          // while a replacement draft is pending, this card is about to be replaced.
          if (saveForm.getAttribute("aria-busy") === "true" || draftForm.getAttribute("aria-busy") === "true") return;
          card.replaceChildren();
          card.hidden = true;
          card.questionDraft = null;
          feedback.textContent = "Draft dismissed; nothing was saved.";
          opener?.focus();
        });
        saveForm.addEventListener("submit", async (event) => {
          event.preventDefault();
          // No save starts while a replacement draft is pending: its result would be lost with this card.
          if (saveForm.getAttribute("aria-busy") === "true" || draftForm.getAttribute("aria-busy") === "true") return;
          // Nor while the dock is being refreshed: the refresh would replace this card.
          if (assistantRefreshesInFlight > 0) {
            status.textContent = "The assistant is updating. Try Save again in a moment.";
            return;
          }
          saveForm.setAttribute("aria-busy", "true");
          const held = [save, dismiss, ...draftForm.querySelectorAll("button")];
          held.forEach((control) => control.setAttribute("aria-disabled", "true"));
          const dock = assistantDock;
          dock.dataset.questionSavesPending = String(Number(dock.dataset.questionSavesPending || 0) + 1);
          holdAssistantNavigation(dock);
          status.textContent = "Saving…";
          try {
            const response = await fetch(saveForm.action, {
              method: "POST", body: new FormData(saveForm),
              headers: { Accept: "application/json", "X-CSRF-Token": csrfToken },
            });
            const result = await assistantJson(response);
            if (!result.item_id) throw new Error("The note could not be confirmed.");
            if (!saveForm.isConnected) return;
            status.textContent = result.message;
            const link = element("a", "Open case notes");
            link.href = result.notebook_url;
            actions.replaceChildren(status, link);
            card.questionDraft = null;
            if (assistantRefreshesInFlight > 0) {
              // A refresh is waiting to replace the dock; it carries this result over.
              const notices = JSON.parse(assistantDock.dataset.questionSaveNotices || "[]");
              notices.push({ action: draftForm.getAttribute("action"), message: result.message, url: result.notebook_url });
              assistantDock.dataset.questionSaveNotices = JSON.stringify(notices);
            }
          } catch (error) {
            if (saveForm.isConnected) status.textContent = `${error.message} You can safely try again.`;
          } finally {
            saveForm.removeAttribute("aria-busy");
            held.forEach((control) => control.removeAttribute("aria-disabled"));
            const remaining = Number(dock.dataset.questionSavesPending || 1) - 1;
            if (remaining > 0) dock.dataset.questionSavesPending = String(remaining);
            else delete dock.dataset.questionSavesPending;
            releaseAssistantNavigation(dock);
            settleAssistantQuestionWrites();
          }
        });
      };
      draftForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        // No new draft replaces a card whose Save is still pending.
        if (draftForm.getAttribute("aria-busy") === "true" || card.querySelector('form[aria-busy="true"]')) return;
        // Nor starts while the dock is being refreshed, which would replace this form.
        if (assistantRefreshesInFlight > 0) {
          feedback.textContent = "The assistant is updating. Try drafting again in a moment.";
          return;
        }
        const opener = event.submitter;
        const body = new FormData(draftForm);
        body.set("purpose", opener?.value || "witness");
        draftForm.setAttribute("aria-busy", "true");
        // An open card's Save and Dismiss are held too, until the replacement arrives or fails.
        const held = [...draftForm.querySelectorAll("button"), ...card.querySelectorAll("button")];
        held.forEach((button) => button.setAttribute("aria-disabled", "true"));
        feedback.textContent = "Drafting from the cited passages… this can take a minute.";
        // A refresh that arrives meanwhile waits for this draft, then carries it over.
        const dock = assistantDock;
        dock.dataset.questionDraftsPending = String(Number(dock.dataset.questionDraftsPending || 0) + 1);
        holdAssistantNavigation(dock);
        try {
          const response = await fetch(draftForm.action, {
            method: "POST", body,
            headers: { Accept: "application/json", "X-CSRF-Token": csrfToken },
          });
          const draft = await assistantJson(response);
          if (!Array.isArray(draft.questions) || !draft.questions.length || !draft.save_url || !draft.basis) {
            throw new Error("The draft could not be confirmed.");
          }
          if (!draftForm.isConnected) return;
          feedback.textContent = `Drafted ${draft.questions.length} question${draft.questions.length === 1 ? "" : "s"} for review.`;
          showDraft(draft, opener);
        } catch (error) {
          if (draftForm.isConnected) feedback.textContent = `${error.message} You can safely try again.`;
        } finally {
          draftForm.removeAttribute("aria-busy");
          held.forEach((button) => button.removeAttribute("aria-disabled"));
          const remaining = Number(dock.dataset.questionDraftsPending || 1) - 1;
          if (remaining > 0) dock.dataset.questionDraftsPending = String(remaining);
          else delete dock.dataset.questionDraftsPending;
          releaseAssistantNavigation(dock);
          settleAssistantQuestionWrites();
        }
      });
      // A draft that was open when the dock refreshed stays open for review.
      const carried = assistantCarriedDrafts.get(draftForm.getAttribute("action"));
      if (carried) {
        assistantCarriedDrafts.delete(draftForm.getAttribute("action"));
        showDraft(carried, null, { focus: false });
        feedback.textContent = "The chat updated; this draft is still open for review.";
      }
    });

    const conversationPicker = assistantDock.querySelector("[data-assistant-conversation-picker]");
    conversationPicker?.addEventListener("change", async () => {
      if (assistantQuestionWritesPending() > 0) return;
      const conversationId = conversationPicker.value;
      if (conversationId === "__draft__") {
        beginAssistantDraft();
        return;
      }
      if (!conversationId || conversationId === assistantDock?.dataset.conversationId) return;
      if (assistantDraft) {
        const form = assistantDock.querySelector("[data-assistant-question-form]");
        const question = form?.querySelector("textarea")?.value || "";
        if (question.trim()) {
          assistantDraftBuffer = {
            question,
            requestKey: form?.querySelector("[data-assistant-request-key]")?.value || "",
            sourceSet: form?.querySelector('select[name="source_set"]')?.value || "",
          };
        }
      }
      releaseAssistantHistorySwitch();
      const historyOperation = { dock: assistantDock, picker: conversationPicker };
      assistantHistorySwitch = historyOperation;
      conversationPicker.disabled = true;
      try {
        await refreshAssistant(assistantFragmentUrl(conversationId), historyOperation);
      } catch (_error) {
        if (!ownsAssistantOperation(historyOperation)) return;
        recoverAssistantRead("That saved chat could not be opened. Try again. Any active request is still being checked.");
      } finally {
        releaseAssistantHistorySwitch(historyOperation);
      }
    });

    assistantDock.querySelector("[data-assistant-new-chat]")?.addEventListener("click", () => {
      // A pending draft or draft save keeps this dock until its result is shown.
      if (assistantQuestionWritesPending() > 0) return;
      beginAssistantDraft();
    });

    const form = assistantDock.querySelector("[data-assistant-question-form]");
    const textarea = form?.querySelector("textarea");
    textarea?.addEventListener("input", () => resizeAssistantTextarea(textarea));
    resizeAssistantTextarea(textarea);
    bindAssistantSuggestions(assistantDock);

    form?.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!textarea?.value.trim()) {
        textarea?.focus();
        return;
      }
      if (form.getAttribute("aria-busy") === "true" || form.dataset.scopePending === "true") return;
      // A new submission supersedes history work even when that work began
      // at the terminal transition of the previous question.
      assistantEpoch += 1;
      window.clearTimeout(assistantPollTimer);
      releaseAssistantHistorySwitch();
      const epoch = assistantEpoch;
      const originatingDock = assistantDock;
      const submittedQuestion = textarea.value.trim();
      const submittedDraft = !form.querySelector('input[name="conversation"]')?.value;
      // Capture successful controls before the busy state disables the
      // textarea. Disabled controls are intentionally omitted by FormData.
      const submission = new FormData(form);
      const status = assistantDock.querySelector("[data-assistant-status]");
      const copy = assistantDock.querySelector("[data-assistant-status-copy]");
      const submit = form.querySelector('button[type="submit"]');
      const recovery = assistantDock.querySelector("[data-assistant-recovery]");
      if (recovery) recovery.hidden = true;
      status.hidden = false;
      status.classList.remove("is-terminal", "is-failed");
      if (copy) copy.textContent = "Saving your question…";
      textarea.disabled = true;
      if (submit) submit.disabled = true;
      form.setAttribute("aria-busy", "true");
      try {
        const response = await fetch(form.action, {
          method: "POST",
          body: submission,
          headers: { Accept: "application/json", "X-CSRF-Token": csrfToken },
        });
        const job = await assistantJson(response);
        if (epoch !== assistantEpoch || originatingDock !== assistantDock) return;
        if (submittedDraft) saveAcceptedAssistantDraft(job);
        // The accepted request keeps its captured key. The next intentional
        // question needs a new key even before terminal history refreshes.
        newAssistantRequestKey();
        textarea.value = "";
        resizeAssistantTextarea(textarea);
        appendAssistantUserMessage(submittedQuestion);
        renderAssistantJob(job);
        pollAssistant();
      } catch (error) {
        if (epoch !== assistantEpoch || originatingDock !== assistantDock) return;
        form.removeAttribute("aria-busy");
        textarea.disabled = form.dataset.sourcesReady !== "true";
        if (submit) submit.disabled = form.dataset.sourcesReady !== "true";
        if (submittedDraft || assistantDraft) {
          status.hidden = true;
          if (recovery) recovery.hidden = false;
        } else {
          status.classList.add("is-terminal", "is-failed");
          if (copy) copy.textContent = `${error.message} You can safely send the question again.`;
        }
      }
    });

    assistantDock.querySelector("[data-assistant-retry-submit]")?.addEventListener("click", () => {
      const currentForm = assistantDock?.querySelector("[data-assistant-question-form]");
      if (currentForm?.querySelector("textarea")?.value.trim()) currentForm.requestSubmit();
    });
    assistantDock.querySelector("[data-assistant-dismiss-error]")?.addEventListener("click", () => {
      const recovery = assistantDock?.querySelector("[data-assistant-recovery]");
      if (recovery) recovery.hidden = true;
      assistantDock?.querySelector("textarea")?.focus({ preventScroll: true });
    });

    assistantDock.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-assistant-cancel]");
      if (!button || button.disabled) return;
      const actionUrl = button.dataset.actionUrl;
      if (!actionUrl) return;
      // A cancellation supersedes pending status reads as well as their timer.
      const operation = { epoch: ++assistantEpoch, dock: assistantDock };
      window.clearTimeout(assistantPollTimer);
      releaseAssistantHistorySwitch();
      button.disabled = true;
      let accepted = false;
      try {
        const response = await fetch(actionUrl, {
          method: "POST",
          headers: { Accept: "application/json", "X-CSRF-Token": csrfToken },
        });
        const job = await assistantJson(response);
        if (!ownsAssistantOperation(operation)) return;
        accepted = true;
        renderAssistantJob(job);
        await refreshAssistant("", operation);
      } catch (_error) {
        if (!ownsAssistantOperation(operation)) return;
        if (button.isConnected) button.disabled = false;
        const active = ["queued", "running"].includes(assistantDock.querySelector("[data-assistant-status]")?.dataset.state);
        recoverAssistantRead(accepted
          ? active ? "Cancellation was requested, but history could not refresh. Checking the saved request…" : assistantHistoryFailure
          : "Cancellation could not be confirmed. Checking the saved request; open Full conversation to inspect its status.");
      }
    });

    const status = assistantDock.querySelector("[data-assistant-status]");
    if (["queued", "running"].includes(status?.dataset.state || "")) pollAssistant();
  }

  window.addEventListener("recordbench:readiness", (event) => {
    if (!assistantDock) return;
    const readiness = event.detail || {};
    const ready = readiness.can_query === true;
    assistantDock.dataset.matterReady = ready ? "true" : "false";
    const form = assistantDock.querySelector("[data-assistant-question-form]");
    if (form) form.dataset.sourcesReady = ready ? "true" : "false";
    const status = assistantDock.querySelector("[data-assistant-status]");
    const working = form?.getAttribute("aria-busy") === "true" || form?.dataset.scopePending === "true"
      || ["queued", "running"].includes(status?.dataset.state || "");
    const textarea = form?.querySelector("textarea");
    const submit = form?.querySelector('button[type="submit"]');
    if (textarea) textarea.disabled = !ready || working;
    if (submit) submit.disabled = !ready || working;
    const hint = assistantDock.querySelector("[data-assistant-readiness-hint]");
    if (hint) {
      hint.textContent = readiness.partial_query === true
        ? "Partial searchable text only. Inspect Search coverage & extraction limits above."
        : ready
          ? "Generated answers use selected passages; check claims against the original sources."
          : readiness.state === "preparing"
            ? "Questions will be available when active preparation finishes."
            : readiness.guidance || "No source is searchable yet.";
    }
    const collapsedCopy = assistantDock.querySelector("[data-assistant-readiness-copy]");
    if (collapsedCopy) {
      const excluded = Number(readiness.excluded_count || 0) > 0
        ? ` · ${readiness.excluded_count || 0} excluded`
        : "";
      collapsedCopy.textContent = `${readiness.searchable_count || 0} of ${readiness.total_count || 0} searchable${excluded}`;
    }
    const coverage = assistantDock.querySelector("[data-assistant-coverage]");
    if (coverage) {
      const copy = coverage.querySelector("[data-assistant-coverage-copy]");
      const action = coverage.querySelector("[data-assistant-coverage-action]");
      if (copy) copy.textContent = readiness.coverage_notice || "";
      if (action && readiness.sources_url) action.href = readiness.sources_url;
      coverage.hidden = readiness.partial_query !== true;
    }
    const allSources = form?.querySelector('select[name="source_set"] option[value=""]');
    if (allSources) allSources.textContent = `All searchable sources (${readiness.searchable_count || 0})`;
  });

  if (assistantDock) {
    setAssistantCollapsed(readAssistantPreference(), { persist: false });
    bindAssistant();
    compactAssistant.addEventListener("change", () => {
      setAssistantCollapsed(readAssistantPreference(), {
        persist: false,
        focus: assistantDock.contains(document.activeElement),
        preserveFocus: true,
      });
    });
    // A mobile workspace should not compete with two open surfaces. Choosing
    // a section or account action tucks the Assistant away without changing
    // its conversation, draft, work in progress, or desktop display preference.
    document.addEventListener("toggle", (event) => {
      if (compactAssistant.matches && event.target.open
          && event.target.matches(".matter-section-disclosure, .account-menu")) {
        setAssistantCollapsed(true);
      }
    }, true);
    const thread = assistantDock.querySelector("[data-assistant-thread]");
    if (thread?.dataset.hasMessages === "true") thread.scrollTop = thread.scrollHeight;
    const preferredConversation = readAssistantConversationPreference();
    const currentConversation = assistantDock.dataset.conversationId || "";
    const preferredOption = preferredConversation
      ? Array.from(assistantDock.querySelectorAll("[data-assistant-conversation-picker] option"))
          .some((option) => option.value === preferredConversation)
      : false;
    if (preferredConversation && preferredConversation !== currentConversation && preferredOption) {
      const preferenceOperation = {};
      refreshAssistant(assistantFragmentUrl(preferredConversation), preferenceOperation).catch(() => {
        if (!ownsAssistantOperation(preferenceOperation)) return;
        saveAssistantConversationPreference(currentConversation);
        recoverAssistantRead("Your preferred chat could not be opened. The current chat is kept and any active request is still being checked.");
      });
    } else {
      saveAssistantConversationPreference(currentConversation);
    }
  }

  window.addEventListener("pagehide", () => { assistantEpoch += 1; window.clearTimeout(assistantPollTimer); releaseAssistantHistorySwitch(); });
  window.addEventListener("pageshow", (event) => { if (event.persisted && assistantDock) pollAssistant(); });

  const reviewWorkspace = document.querySelector("[data-review-workspace]");
  if (reviewWorkspace) {
    body.classList.add("review-enhanced");
    const preference = `recordbench:review-layout:${reviewWorkspace.dataset.reviewMatter}`;
    const queueToggle = reviewWorkspace.querySelector("[data-review-queue-toggle]");
    const paneWidth = reviewWorkspace.querySelector("[data-review-pane-width]");
    const notePanel = reviewWorkspace.querySelector("[data-source-note-panel]");
    const noteForm = notePanel?.querySelector("form");
    const noteBody = noteForm?.querySelector("textarea");
    const noteTime = noteForm?.querySelector("[data-note-media-time]");
    let noteSubmission = null;
    let savedNote = false;
    let noteRetryMetadata = null;
    let noteRestoreProblem = "";
    const restoreNoteAttempt = () => {
      const metadata = window.history.state?.recordbenchSourceNote;
      if (!metadata || !noteForm) return;
      // Only retry identity and source coordinates enter browser history. The
      // prose is never stored here; the server checks it against any saved key.
      const sameSource = typeof metadata === "object" && metadata.action === noteForm.action
        && ["source_version_id", "source_basis", "unit"].every((name) =>
          (metadata[name] || "") === (noteForm.querySelector(`[name="${name}"]`)?.value || ""))
        && /^[A-Za-z0-9_-]{16,80}$/.test(metadata.request_key || "")
        && (!noteTime || /^\d+$/.test(metadata.start_ms || ""));
      if (!sameSource) {
        noteRestoreProblem = "The earlier note’s source context could not be restored safely. Inspect saved case notes, then explicitly start another note.";
      } else {
        noteRetryMetadata = metadata;
        noteForm.querySelector('[name="request_key"]').value = metadata.request_key;
        if (noteTime) noteTime.value = metadata.start_ms;
      }
      noteForm.querySelector("[data-source-note-new]").hidden = false;
      noteForm.querySelector("[data-source-note-status]").textContent = noteRestoreProblem
        || "Earlier save attempt restored. Saving retries that same note and position. If its text was not restored, open saved case notes or explicitly write another note.";
    };
    restoreNoteAttempt();
    window.addEventListener("pageshow", () => window.setTimeout(() => {
      // Chromium restores form values after pageshow. Making a new textarea
      // readonly earlier can prevent that native restoration altogether.
      restoreNoteAttempt();
      if (noteRetryMetadata || noteRestoreProblem) {
        noteBody.readOnly = true;
        noteForm.querySelector('button[type="submit"]').disabled = savedNote || !noteBody.value.trim() || Boolean(noteRestoreProblem);
        if (!noteBody.value.trim()) noteForm.querySelector("[data-source-note-status]").textContent = "An earlier save attempt exists, but this browser did not restore its text. Open saved case notes to inspect it, or explicitly write another note.";
      }
    }, 0));
    const persistNoteAttempt = () => {
      const metadata = { action: noteForm.action };
      ["request_key", "source_version_id", "source_basis", "unit", "start_ms"].forEach((name) => { metadata[name] = noteSubmission.get(name) || ""; });
      window.history.replaceState({ ...(window.history.state || {}), recordbenchSourceNote: metadata }, "");
      noteRetryMetadata = metadata;
    };
    let layout = {};
    try {
      const savedLayout = JSON.parse(localStorage.getItem(preference) || "{}");
      if (savedLayout && typeof savedLayout === "object" && !Array.isArray(savedLayout)) layout = savedLayout;
    } catch (_error) { /* Optional preference. */ }
    const saveLayout = () => { try { localStorage.setItem(preference, JSON.stringify(layout)); } catch (_error) { /* No persistence required. */ } };
    const showQueue = (open) => {
      body.classList.toggle("review-queue-open", open);
      queueToggle?.setAttribute("aria-expanded", String(open));
      layout.queue = open;
    };
    showQueue(layout.queue === true);
    queueToggle?.addEventListener("click", () => { showQueue(!layout.queue); saveLayout(); });
    const setWidth = (value) => {
      const width = Math.max(300, Math.min(520, Number(value) || 380));
      body.style.setProperty("--assistant-width", `${width}px`);
      if (paneWidth) paneWidth.value = String(width);
      layout.width = width;
    };
    setWidth(layout.width);
    paneWidth?.addEventListener("input", () => { setWidth(paneWidth.value); saveLayout(); });
    // The PDF opens at whole-page fit; a reviewer may prefer page width instead.
    const pdfViewer = reviewWorkspace.querySelector("[data-pdf-viewer]");
    const fitToggle = reviewWorkspace.querySelector("[data-pdf-fit-toggle]");
    const setFit = (value) => {
      const fit = value === "width" ? "width" : "page";
      fitToggle?.querySelectorAll("[data-pdf-fit]").forEach((button) => {
        button.setAttribute("aria-pressed", String(button.dataset.pdfFit === fit));
      });
      if (pdfViewer?.dataset.pdfBase) {
        const base = pdfViewer.dataset.pdfBase;
        const source = `${base}${base.includes("#") ? "&" : "#"}view=${fit === "width" ? "FitH" : "Fit"}&navpanes=0`;
        if (pdfViewer.getAttribute("src") !== source) pdfViewer.setAttribute("src", source);
      }
      layout.pdfFit = fit;
    };
    if (pdfViewer && fitToggle) {
      fitToggle.hidden = false;
      setFit(layout.pdfFit);
      fitToggle.addEventListener("click", (event) => {
        const button = event.target.closest("[data-pdf-fit]");
        if (!button) return;
        setFit(button.dataset.pdfFit);
        saveLayout();
      });
    }
    const snapshotPlayback = () => {
      if (noteRetryMetadata || noteRestoreProblem) return;
      const player = document.querySelector("[data-media-player]");
      if (noteTime && player && Number.isFinite(player.currentTime)) noteTime.value = String(Math.floor(player.currentTime * 1000));
    };
    if (noteTime) noteTime.readOnly = true;
    document.querySelector("[data-media-player]")?.addEventListener("timeupdate", () => {
      if (!noteSubmission && !notePanel?.hidden) snapshotPlayback();
    });
    const showNote = (open, focus = true) => {
      if (!notePanel) return;
      notePanel.hidden = !open;
      reviewWorkspace.querySelector("[data-review-note-open]")?.setAttribute("aria-expanded", String(open));
      body.classList.toggle("review-notes-open", open);
      layout.notes = open;
      saveLayout();
      if (open && !noteSubmission) snapshotPlayback();
      if (focus) (open ? noteBody : reviewWorkspace.querySelector("[data-review-note-open]"))?.focus({ preventScroll: true });
    };
    showNote(Boolean(noteBody?.value) || layout.notes === true, false);
    window.addEventListener("beforeunload", (event) => {
      if (noteBody?.value.trim() && !savedNote) { event.preventDefault(); event.returnValue = ""; }
    });
    reviewWorkspace.querySelector("[data-review-note-open]")?.addEventListener("click", (event) => { event.preventDefault(); showNote(true); });
    notePanel?.querySelector("[data-review-note-close]")?.addEventListener("click", () => showNote(false));
    notePanel?.addEventListener("keydown", (event) => { if (event.key === "Escape") { event.preventDefault(); showNote(false); } });
    selectReviewSource = async (form) => {
      const button = form.querySelector("button");
      if (button.disabled) return false;
      button.disabled = true;
      let feedback = form.querySelector("[role=status]");
      if (!feedback) { feedback = document.createElement("span"); feedback.setAttribute("role", "status"); form.append(feedback); }
      feedback.textContent = "Selecting source…";
      const epoch = assistantEpoch;
      const originatingDock = assistantDock;
      try {
        const response = await fetch(form.action, { method: "POST", body: new FormData(form), headers: { Accept: "application/json", "X-CSRF-Token": csrfToken } });
        const result = await assistantJson(response);
        if (!result.source_set_id || typeof result.name !== "string") throw new Error("The source selection could not be confirmed. Try again.");
        if (epoch !== assistantEpoch || originatingDock !== assistantDock) {
          feedback.textContent = "The conversation changed. Select this source again for the current question.";
          return false;
        }
        assistantEpoch += 1;
        window.clearTimeout(assistantPollTimer);
        releaseAssistantHistorySwitch();
        reviewQuestionScope = { id: result.source_set_id, name: result.name };
        applyReviewQuestionScope();
        resumeAssistantAfterScopeChange();
        showNote(false, false);
        setAssistantCollapsed(false, { focus: true });
        feedback.textContent = "Source selected for your next question.";
        return true;
      } catch (error) { feedback.textContent = error.message; return false; }
      finally { button.disabled = false; }
    };
    reviewWorkspace.querySelector("[data-review-ask-source]")?.addEventListener("submit", (event) => {
      event.preventDefault();
      selectReviewSource(event.currentTarget);
    });
    noteForm?.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (noteForm.getAttribute("aria-busy") === "true" || savedNote) return;
      const button = noteForm.querySelector('button[type="submit"]');
      const feedback = noteForm.querySelector("[data-source-note-status]");
      if (!noteBody.value.trim()) {
        feedback.textContent = "No note text is available to retry. Open saved case notes or explicitly write another note.";
        return;
      }
      // Freeze body, version, section and timestamp across lost-response retries.
      // An explicit new note is the only action that starts a fresh save identity.
      restoreNoteAttempt();
      if (!noteSubmission) { snapshotPlayback(); noteSubmission = new FormData(noteForm); }
      noteBody.readOnly = true;
      if (noteTime) noteTime.readOnly = true;
      noteForm.setAttribute("aria-busy", "true");
      button.disabled = true;
      feedback.textContent = "Saving your source-linked note…";
      try {
        if (noteRestoreProblem) throw new Error(noteRestoreProblem);
        // Persist the immutable retry identity before a request can commit.
        // If this fails, no write is sent under an identity Back could lose.
        persistNoteAttempt();
        const response = await fetch(noteForm.action, { method: "POST", body: noteSubmission, headers: { Accept: "application/json", "X-CSRF-Token": csrfToken } });
        const result = await assistantJson(response);
        if (!result.item_id) throw new Error("The save could not be confirmed.");
        if (!noteForm.isConnected) return;
        savedNote = true;
        feedback.textContent = result.message || "Human note saved as Needs review.";
      } catch (error) { feedback.textContent = `${error.message} Your original note and position are kept. Retry the same save, or explicitly start another note.`; }
      finally {
        noteForm.removeAttribute("aria-busy");
        button.disabled = savedNote;
        noteForm.querySelector("[data-source-note-new]").hidden = false;
      }
    });
    noteForm?.querySelector("[data-source-note-new]")?.addEventListener("click", () => {
      if (noteForm.getAttribute("aria-busy") === "true") return;
      try {
        const state = { ...(window.history.state || {}) };
        delete state.recordbenchSourceNote;
        window.history.replaceState(state, "");
      } catch (_error) {
        noteForm.querySelector("[data-source-note-status]").textContent = "The earlier retry identity could not be cleared. Inspect saved case notes before starting another note.";
        return;
      }
      noteSubmission = null; savedNote = false;
      noteRetryMetadata = null; noteRestoreProblem = "";
      const bytes = new Uint8Array(16); window.crypto.getRandomValues(bytes);
      noteForm.querySelector('[name="request_key"]').value = Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("");
      noteBody.readOnly = false; noteBody.value = "";
      if (noteTime) noteTime.readOnly = true;
      snapshotPlayback();
      noteForm.querySelector('button[type="submit"]').disabled = false;
      noteForm.querySelector("[data-source-note-new]").hidden = true;
      noteForm.querySelector("[data-source-note-status]").textContent = "";
      noteBody.focus({ preventScroll: true });
    });
  }

  const workflowMonitors = Array.from(document.querySelectorAll("[data-workflow-monitor]"));
  const workflowTimers = [];
  const monitorWorkflow = (panel) => {
    if (!panel?.dataset.statusUrl || panel.dataset.terminal === "true") return;
    panel.setAttribute("aria-live", "polite");
    const poll = async () => {
      if (document.visibilityState === "hidden") {
        workflowTimers.push(window.setTimeout(poll, 2500));
        return;
      }
      try {
        const response = await fetch(panel.dataset.statusUrl, {
          headers: { Accept: "application/json" },
          cache: "no-store",
        });
        if (!response.ok) throw new Error("Workflow status unavailable");
        const status = await response.json();
        const state = panel.querySelector("[data-workflow-state]");
        const stage = panel.querySelector("[data-workflow-stage]");
        const label = panel.querySelector("[data-workflow-progress-label]");
        const progress = panel.querySelector("[data-workflow-progress]");
        let eta = panel.querySelector("[data-workflow-eta]");
        if (!eta && progress?.parentElement) {
          eta = document.createElement("small");
          eta.className = "workflow-eta";
          eta.dataset.workflowEta = "";
          progress.parentElement.append(eta);
        }
        if (state) {
          state.textContent = String(status.state || "working").replaceAll("_", " ");
          state.className = `workflow-state-badge state-${status.state || "running"}`;
        }
        if (stage) stage.textContent = status.message || "Working…";
        const budget = panel.querySelector("[data-workflow-budget]");
        if (budget && status.review_budget_description) {
          budget.textContent = status.review_budget_description;
        }
        const completed = status.completed_steps ?? status.reviewed_count ?? 0;
        const total = status.total_steps ?? status.snapshot_count ?? 0;
        if (label) {
          label.textContent = `${Number(completed).toLocaleString()} / ${total ? Number(total).toLocaleString() : "—"} ${"snapshot_count" in status ? "sources" : "steps"}`;
        }
        if (progress) {
          progress.max = Math.max(Number(total) || 1, 1);
          progress.value = Number(completed) || 0;
        }
        if (eta) eta.textContent = status.eta_label || "";
        const counts = panel.querySelectorAll(".run-counts strong");
        const liveValues = "snapshot_count" in status
          ? [status.included_count, status.excluded_count, status.attention_count]
          : [status.candidate_count, status.evidence_count];
        liveValues.forEach((value, index) => {
          if (counts[index] && value !== undefined) {
            counts[index].textContent = Number(value).toLocaleString();
          }
        });
        if (status.terminal === true) {
          panel.dataset.terminal = "true";
          if (status.result_url) window.location.assign(status.result_url);
          return;
        }
      } catch (_error) {
        // Durable workflow state remains available after refresh; a transient
        // polling failure should not imply that the underlying run failed.
      }
      workflowTimers.push(window.setTimeout(poll, 2000));
    };
    workflowTimers.push(window.setTimeout(poll, 900));
  };
  workflowMonitors.forEach(monitorWorkflow);

  window.addEventListener("pagehide", () => {
    window.clearTimeout(assistantPollTimer);
    workflowTimers.forEach((timer) => window.clearTimeout(timer));
  });
})();

// List-only shortcuts never intercept typing, media controls, or reader keys.
document.querySelectorAll('[data-source-browser] nav').forEach((nav) => {
  nav.addEventListener('keydown', (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const links = Array.from(nav.querySelectorAll('li a'));
    const index = links.indexOf(event.target);
    if (index < 0) return;
    let target;
    if (event.key === 'ArrowDown') target = Math.min(index + 1, links.length - 1);
    else if (event.key === 'ArrowUp') target = Math.max(index - 1, 0);
    else if (event.key === 'Home') target = 0;
    else if (event.key === 'End') target = links.length - 1;
    else return;
    event.preventDefault();
    links[target].focus();
  });
});
