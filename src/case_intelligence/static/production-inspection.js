(() => {
  const suffixOf = file => /\.(dat|opt|lfp|dii)$/i.exec(file.name)?.[1].toLowerCase();
  const node = (tag, text) => {
    const element = document.createElement(tag);
    element.textContent = text;
    return element;
  };

  const statuses = {
    matched: "Exact path match",
    name_only: "Filename only",
    missing: "Missing from selection",
    ambiguous: "Ambiguous in selection",
    ineligible: "Selected but ineligible",
    unsafe: "Unsafe reference",
  };

  function inspectionBody(files, items, index, form, profile) {
    const file = files[index];
    const fallback = reason => ({ body: file, manifestSize: 0, reason });
    const maxRows = Number(form.dataset.maxUploadItems);
    const maxBytes = Number(form.dataset.maxPreflightRequestBytes);
    if (!Number.isSafeInteger(maxRows) || !Number.isSafeInteger(maxBytes)
        || files.length > maxRows || files.length !== items.length) {
      return fallback("The complete selection could not be included within the inspection limits.");
    }
    const metadata = { index };
    if (profile) metadata.dat_profile = { sha256: profile.sha256, fields: profile.fields };
    const encoder = new TextEncoder();
    const prefix = encoder.encode(JSON.stringify(metadata).slice(0, -1) + ',"files":[');
    const parts = [prefix];
    let size = prefix.byteLength + 2;
    for (const [position, item] of items.entries()) {
      if (item.index !== position || !item.path_safety_validated || typeof item.eligible !== "boolean") {
        return fallback("The selected files include a path that could not be validated.");
      }
      const path = files[position].webkitRelativePath || files[position].name;
      if (position) {
        parts.push(",");
        size++;
      }
      const row = encoder.encode(JSON.stringify([path, item.eligible]));
      size += row.byteLength;
      if (size > maxBytes) {
        return fallback("The selected filenames exceed the inspection metadata limit.");
      }
      parts.push(row);
    }
    parts.push("]}", file);
    return { body: new Blob(parts), manifestSize: size, reason: "" };
  }

  function showReferenceQc(report, details, fallbackReason) {
    const qc = report.reference_qc;
    if (!qc) {
      details.append(node("p", `Only the index was inspected. ${fallbackReason || "A complete comparison with the selection was unavailable."}`));
      return;
    }
    const heading = node("h3", "References in the complete load file");
    const scope = node("p", `${qc.total.toLocaleString()} path references checked against the selected files. A name match does not verify file contents.`);
    details.append(heading, scope);
    if (report.format === "dat") {
      const basis = node("p", qc.dat_mapping === "custom"
        ? "This check used the producer field mapping selected for this DAT."
        : "This check used standard DAT headings for native and produced text paths.");
      basis.dataset.referenceMapping = qc.dat_mapping;
      details.append(basis);
    }
    const counts = node("dl", "");
    for (const [status, label] of Object.entries(statuses)) {
      const count = node("dd", qc.counts[status].toLocaleString());
      count.dataset.referenceCount = status;
      counts.append(node("dt", label), count);
    }
    details.append(counts);
    if (qc.examples.length) {
      const findings = node("ul", "");
      findings.dataset.referenceFindings = "";
      for (const example of qc.examples) {
        const path = example.path + (example.path_truncated ? "…" : "");
        findings.append(node("li", `Record ${example.ordinal} · line ${example.line} · ${example.role} · ${path} · ${statuses[example.status]}`));
      }
      details.append(node("h4", "Reference findings"), findings);
      if (qc.examples_truncated) details.append(node("p", "Additional findings are included in the totals above."));
    }
  }

  function showReport(report, details, rows, fallbackReason) {
    showReferenceQc(report, details, fallbackReason);
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
        const finding = ref.status ? ` · ${statuses[ref.status]}` : "";
        record.append(node("p", `${ref.role} · ${ref.path}${finding}`));
      }
      if (row.references_truncated) record.append(node("p", "Additional references in this record are omitted from the sample."));
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

    async inspectFile(reviewHeadings = false) {
      if (this.hidden || !this.chooser.length) return;
      const index = Number(this.chooser.value);
      const draft = reviewHeadings ? null : this.mapping?.collect?.(index);
      if (draft?.message && this.mapping?.current?.index === index) {
        this.status.textContent = draft.message;
        return;
      }
      this.resetReport();
      const file = this.files[index];
      const label = this.chooser.selectedOptions[0].textContent;
      const request = new AbortController();
      this.request = request;
      this.inspect.disabled = true;
      this.cancel.hidden = false;
      this.status.textContent = `Inspecting ${label}…`;
      try {
        const profile = draft?.profiles?.get(index);
        const payload = inspectionBody(this.files, this.items, index, this.closest("form"), profile);
        const headers = { "Content-Type": "application/octet-stream", "X-CSRF-Token": document.body.dataset.csrfToken };
        if (payload.manifestSize) headers["X-Selection-Manifest-Bytes"] = String(payload.manifestSize);
        const response = await fetch(this.dataset.inspectionUrl + suffixOf(file), {
          method: "POST", headers, signal: request.signal, cache: "no-store", body: payload.body,
        });
        const report = await response.json().catch(() => ({}));
        if (request.signal.aborted) return;
        if (response.status === 409 && profile && payload.manifestSize) {
          this.status.textContent = report.message || "The selected DAT mapping could not be applied.";
          const review = node("button", "Review current DAT headings");
          review.type = "button";
          review.addEventListener("click", () => this.inspectFile(true));
          this.details.append(review);
          return;
        }
        if (!response.ok) throw new Error(report.message || "The inspection could not be completed.");
        showReport(report, this.details, this.rows, payload.reason);
        this.mapping?.open?.(index, label, report);
        if (draft?.message && this.mapping?.current) this.mapping.status.textContent = draft.message;
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
