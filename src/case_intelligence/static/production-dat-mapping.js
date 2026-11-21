(() => {
  const roles = [
    ["begin_bates", "Begin Bates"], ["end_bates", "End Bates"],
    ["begin_attachment", "Family begins"], ["end_attachment", "Family ends"],
    ["native_path", "Native file path"], ["text_path", "Produced text path"], ["date", "Document date"],
  ];

  class DatMapping extends HTMLElement {
    connectedCallback() {
      if (this.drafts) return;
      this.drafts = new Map();
      this.producer = this.querySelector("[data-profile-producer]");
      this.status = this.querySelector("[role=status]");
      this.choices = this.querySelector("datalist");
      this.inputs = new Map();
      const group = this.querySelector("[data-field-roles]");
      for (const [role, label] of roles) {
        const wrapper = document.createElement("label");
        wrapper.textContent = label;
        const input = document.createElement("input");
        input.setAttribute("list", this.choices.id);
        input.setAttribute("autocomplete", "off");
        input.dataset.role = role;
        input.addEventListener("focus", () => this.offerHeadings(input));
        input.addEventListener("input", () => { this.offerHeadings(input); this.remember(); });
        wrapper.append(input);
        group.append(wrapper);
        this.inputs.set(role, input);
      }
      this.producer.addEventListener("input", () => this.remember());
      this.querySelector("[data-load-profile]").addEventListener("click", () => this.reuse());
      this.querySelector("[data-clear-profile]").addEventListener("click", () => {
        this.producer.value = "";
        for (const input of this.inputs.values()) input.value = "";
        this.remember();
        this.status.textContent = "Standard field recognition will apply.";
      });
    }

    close() {
      this.lookup?.abort();
      this.hidden = true;
      this.current = null;
      this.choices.replaceChildren();
      this.producer.value = "";
      for (const input of this.inputs.values()) {
        input.value = "";
        input.placeholder = "";
      }
      this.status.textContent = "";
    }

    open(index, label, report) {
      this.close();
      if (report.format !== "dat" || !report.mapping || !/^[0-9a-f]{64}$/.test(report.sha256)) return;
      const draft = this.drafts.get(index);
      if (draft && draft.sha256 !== report.sha256) {
        this.drafts.delete(index);
        this.status.textContent = "The inspected bytes changed. Review the headings and enter a new mapping.";
      }
      const saved = this.drafts.get(index);
      this.current = { index, label, sha256: report.sha256, headers: new Set(report.mapping.headers) };
      this.producer.value = saved?.producer || "";
      const suggestions = new Map(Object.entries(report.mapping.suggestions).map(([header, role]) => [role, header]));
      for (const [role, input] of this.inputs) {
        input.value = saved?.values[role] || "";
        input.placeholder = suggestions.has(role) ? `Suggested: ${suggestions.get(role)}` : "Standard recognition";
      }
      const omitted = report.mapping.header_count - this.current.headers.size;
      this.querySelector("[data-header-coverage]").textContent = omitted
        ? `${omitted} headings exceed the permitted length or contain unsupported characters and cannot be mapped.` : "";
      this.hidden = false;
    }

    offerHeadings(input) {
      const query = input.value.toLowerCase();
      const fragment = document.createDocumentFragment();
      for (const header of this.current.headers) {
        if (!header.toLowerCase().includes(query)) continue;
        const option = document.createElement("option");
        option.value = header;
        fragment.append(option);
        if (fragment.childElementCount === 50) break;
      }
      this.choices.replaceChildren(fragment);
    }

    remember() {
      this.lookup?.abort();
      this.status.textContent = "";
      const producer = this.producer.value.trim();
      const values = Object.fromEntries([...this.inputs].map(([role, input]) => [role, input.value]));
      if (!producer && Object.values(values).every(value => !value)) this.drafts.delete(this.current.index);
      else this.drafts.set(this.current.index, {
        label: this.current.label, sha256: this.current.sha256, producer, values,
        unknownHeading: Object.values(values).some(header => header && !this.current.headers.has(header)),
      });
    }

    collect() {
      const profiles = new Map();
      if (this.lookup && !this.lookup.signal.aborted) return { index: this.current.index, message: "Wait for the producer lookup before confirming." };
      for (const [index, draft] of this.drafts) {
        const fields = new Map();
        let problem = draft.unknownHeading ? "Choose exact headings from the inspected DAT." : "";
        for (const [role, header] of Object.entries(draft.values)) {
          if (!header) continue;
          if (fields.has(header)) problem = "Assign each heading only once.";
          fields.set(header, role);
        }
        if (!draft.producer || !fields.size) problem = "Enter a producer and at least one heading, or clear the mapping.";
        if (Array.from(draft.producer).length > 160) problem = "Enter a producer name with 160 characters or fewer.";
        if (/[\p{Cc}\p{Cf}\p{Cs}]/u.test(draft.producer)) problem = "The producer name contains unsupported characters.";
        if (problem) return { index, message: `${draft.label}: ${problem}` };
        profiles.set(index, { producer: draft.producer, sha256: draft.sha256, fields: Object.fromEntries(fields) });
      }
      return { profiles };
    }

    async reuse() {
      const producer = this.producer.value.trim();
      if (!producer) { this.status.textContent = "Enter the name of the producer whose mapping you want to reuse."; this.producer.focus(); return; }
      this.lookup?.abort();
      const request = new AbortController();
      this.lookup = request;
      this.status.textContent = "Looking up this producer in the current matter…";
      try {
        const response = await fetch(this.dataset.lookupUrl, {
          method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": document.body.dataset.csrfToken },
          signal: request.signal, cache: "no-store", body: JSON.stringify({ producer }),
        });
        const result = await response.json();
        if (request.signal.aborted) return;
        if (!response.ok) throw new Error(result.message || "The mapping could not be retrieved.");
        if (!result.profile) { this.status.textContent = "No saved mapping for this producer in this matter."; return; }
        const fields = Object.entries(result.profile.fields);
        if (fields.some(([, role]) => !this.inputs.has(role))) throw new Error("This mapping contains an unsupported field role.");
        const missing = fields.filter(([header]) => !this.current.headers.has(header));
        for (const input of this.inputs.values()) input.value = "";
        for (const [header, role] of fields) this.inputs.get(role).value = header;
        this.remember();
        if (missing.length) {
          this.status.textContent = `Saved mapping could not be applied. Unavailable headings: ${missing.map(([header]) => header).join(", ")}. Review the mapping for this DAT.`;
          return;
        }
        this.status.textContent = "Saved mapping loaded. Review the headings before confirming upload.";
      } catch (error) {
        if (!request.signal.aborted) this.status.textContent = error.message;
      } finally {
        if (this.lookup === request) this.lookup = null;
      }
    }
  }

  customElements.define("exculpata-dat-mapping", DatMapping);
})();
