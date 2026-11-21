(() => {
  const suffixOf = file => /\.(dat|opt|lfp|dii)$/i.exec(file.name)?.[1].toLowerCase();
  const node = (tag, text) => {
    const element = document.createElement(tag);
    element.textContent = text;
    return element;
  };

  // Compare inert selection names. The browser's top-level folder is the scope
  // used by the receipt-backed production importer after admission.
  function selectedPaths(files, items, index) {
    const relativePath = file => file.webkitRelativePath || file.name;
    const rootOf = file => file.webkitRelativePath?.split("/")[0] || "";
    const root = rootOf(files[index]);
    const paths = new Map();
    const names = new Map();
    const add = (map, key, item) => {
      const previous = map.get(key);
      map.set(key, previous ? { ambiguous: true } : item);
    };
    for (const item of items) {
      const file = files[item.index];
      if (item.index === index || !item.path_safety_validated || rootOf(file) !== root) continue;
      const path = relativePath(file);
      const relative = root ? path.slice(root.length + 1) : path;
      const match = { path, eligible: item.eligible };
      add(paths, relative.toLowerCase(), match);
      add(names, file.name.toLowerCase(), match);
    }
    return reference => {
      const supplied = reference.replaceAll("\\", "/");
      if (/^[~/]|[:\p{Cc}\p{Cf}]/u.test(supplied) || supplied.split("/").includes("..")) {
        return "Cannot compare this path as a relative selection name.";
      }
      const parts = supplied.split("/").filter(part => part && part !== ".");
      if (!parts.length) return "No relative path was supplied.";
      if (!root && parts.length > 1) return "Choose the production folder to compare directory paths.";
      const key = parts.join("/").toLowerCase();
      const rooted = root && parts.length > 1 && parts[0].toLowerCase() === root.toLowerCase();
      const direct = paths.get(key);
      const anchored = rooted ? paths.get(parts.slice(1).join("/").toLowerCase()) : null;
      const byName = !direct && !anchored && parts.length === 1;
      const match = direct && anchored ? { ambiguous: true } : direct || anchored || (byName && names.get(key));
      if (!match) return "Missing from the selected paths.";
      if (match.ambiguous) return "Ambiguous in the selection.";
      return `${byName ? "Filename match" : "Selected path"}: ${match.path}${match.eligible ? "" : " (ineligible for upload)"}`;
    };
  }

  function showReport(report, details, rows, compare) {
    details.append(node("p", "Reference findings compare only the paths shown below with the current selection. Companion bytes are checked during upload."));
    for (const [code, count] of Object.entries(report.diagnostic_counts)) {
      details.append(node("p", `${code}: ${count.toLocaleString()}`));
    }
    for (const row of report.rows) {
      const entry = node("li", "");
      const record = node("details", "");
      record.open = !rows.childElementCount;
      record.append(node("summary", `Record ${row.ordinal} · line ${row.line}`));
      const fields = node("dl", "");
      for (const field of row.fields) fields.append(node("dt", field.label), node("dd", field.value));
      record.append(fields);
      if (row.fields_truncated) record.append(node("p", "Some field content is omitted from this sample."));
      for (const ref of row.references) {
        record.append(node("p", `${ref.role}: ${ref.path} — ${compare(ref.path)}`));
      }
      if (row.references_truncated) record.append(node("p", "Additional references in this record were not compared."));
      entry.append(record);
      rows.append(entry);
    }
  }

  class ProductionInspection extends HTMLElement {
    connectedCallback() {
      if (this.chooser) return;
      this.mapping = this.querySelector("exculpata-dat-mapping");
      this.chooser = this.querySelector("select");
      this.inspect = this.querySelector("[data-inspect-production]");
      this.cancel = this.querySelector("[data-cancel-inspection]");
      this.status = this.querySelector("[data-production-inspection-status]");
      this.details = this.querySelector("[data-production-inspection-details]");
      this.rows = this.querySelector("[data-production-inspection-rows]");
      this.chooser.addEventListener("change", () => this.resetReport());
      this.inspect.addEventListener("click", () => this.inspectFile());
      this.cancel.addEventListener("click", () => {
        this.resetReport();
        this.status.textContent = "Inspection canceled.";
        this.inspect.focus();
      });
    }

    resetReport() {
      this.mapping?.close?.();
      this.request?.abort();
      this.request = null;
      this.cancel.hidden = true;
      this.inspect.disabled = false;
      this.status.textContent = "";
      this.details.replaceChildren();
      this.rows.replaceChildren();
    }

    clearSelection() {
      this.mapping?.drafts?.clear();
      this.resetReport();
      this.hidden = true;
      this.files = [];
      this.items = [];
      this.chooser.replaceChildren();
    }

    setSelection(files, items) {
      this.clearSelection();
      this.files = files;
      this.items = items;
      for (const item of items) {
        if (!item.eligible || !suffixOf(files[item.index])) continue;
        const option = node("option", item.display_path || item.display_name);
        option.value = String(item.index);
        this.chooser.append(option);
      }
      this.hidden = !this.chooser.length;
    }

    async inspectFile() {
      if (this.hidden || !this.chooser.length) return;
      this.resetReport();
      const index = Number(this.chooser.value);
      const file = this.files[index];
      const label = this.chooser.selectedOptions[0].textContent;
      const request = new AbortController();
      this.request = request;
      this.inspect.disabled = true;
      this.cancel.hidden = false;
      this.status.textContent = `Inspecting ${label}…`;
      try {
        const response = await fetch(this.dataset.inspectionUrl + suffixOf(file), {
          method: "POST",
          headers: { "Content-Type": "application/octet-stream", "X-CSRF-Token": document.body.dataset.csrfToken },
          signal: request.signal, cache: "no-store", body: file,
        });
        const report = await response.json().catch(() => ({}));
        if (request.signal.aborted) return;
        if (!response.ok) throw new Error(report.message || "The inspection could not be completed.");
        showReport(report, this.details, this.rows, selectedPaths(this.files, this.items, index));
        this.mapping?.open?.(index, label, report);
        const kind = report.record_kind + (report.record_count === 1 ? "" : "s");
        const encoding = report.encoding ? ` · ${report.encoding}` : "";
        this.status.textContent = `${label} · ${report.format.toUpperCase()}${encoding}. Showing ${report.rows.length} of ${report.record_count.toLocaleString()} ${kind}.`;
      } catch (error) {
        if (!request.signal.aborted) {
          this.details.replaceChildren();
          this.rows.replaceChildren();
          this.status.textContent = `Could not inspect ${label}. ${error.message}`;
        }
      } finally {
        if (!request.signal.aborted) {
          this.request = null;
          this.inspect.disabled = false;
          this.cancel.hidden = true;
        }
      }
    }
  }

  customElements.define("exculpata-production-inspection", ProductionInspection);
})();
