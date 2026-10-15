Not legal advice. Verify the rules in your jurisdiction.

# Data handling for the beta candidate

This statement describes the reference source at `6ebc292` (reviewed October 15,
2026), not a certification of an installed system. The beta is still an
unreleased evaluation candidate; use synthetic material for qualification
([release boundary](../BETA_RELEASE_NOTES.md)). Complete the
[vendor checklist](VENDOR_VETTING_CHECKLIST.md) before adapting the
[client disclosure](CLIENT_DISCLOSURE_TEMPLATE.md).

## Where information goes

In the reference Compose deployment, the operator's server stores managed source
files and working state; SQLite holds control records and PostgreSQL holds a
derived retrieval index. Transcription has a separate temporary queue. These
may be different disks or host-mounted network storage, not necessarily the
reviewer's computer. Browser uploads travel to the server; opening sources and
exporting work sends data back to the authorized browser. Backups and downloaded
exports create additional copies under the operator's control.
([Storage contract](../STORAGE_AND_BACKUP.md), [architecture](../ARCHITECTURE.md),
[Compose mounts and service wiring, lines 186–380][compose])

The reference local inference paths send prompts/selected evidence to the local
generator, text to the retrieval worker, recording bytes to the transcription
API, and uploaded bytes to ClamAV. They do not send those inputs to a hosted AI
vendor for inference or model training. This is a bounded description of these
paths and the unmodified reference topology, not a promise about arbitrary
allowlisted endpoints, administrator access, exported files, dependency behavior
outside isolation, or a compromised host. The endpoint validator checks an exact
configured hostname; it does **not** establish who owns it, whether it is inside
the building, or how it handles data.
([Generation transport, lines 242–291][generation],
[retrieval inference/transport, lines 218–336][retrieval],
[transcription transport, lines 247–305][media],
[scan streaming, lines 188–318][malware], [endpoint validator, lines 22–78][endpoints],
[network contract test](../../tests/test_reference_network_boundary.py))

The inspected model paths perform inference, not fine-tuning on matter data.
Read-only model mounts and offline flags protect the reference workers; stored
answers, embeddings, notes and context are still retained work, not evidence of
weight training. This does not certify a separately supplied service's training
or retention policy, nor the provenance of its original training data.
([Reference retrieval worker, lines 111–168][retrieval-worker],
[retrieval encode/predict, lines 218–277][retrieval],
[generator configuration, lines 132–180][compose],
[Nemotron inference, lines 28–43][nemotron],
[transcription offline requirement, lines 126–168][pipeline],
[recorded context](../SECURITY_MODEL.md#recorded-context-in-focused-answers))

## Network inventory and operator choices

“Local-first” does not mean a disconnected installation. The following inventory
covers checked first-party runtime, staging, installation and optional services.
DNS, proxies, host mounts, identity infrastructure and browser/OS behavior must
also be inspected on the actual node. Disable a capability only after planning
its replacement; blocking egress can prevent installation, sign-in or readiness.

| Touchpoint and trigger | Information and boundary | How to avoid or restrict it; evidence |
| --- | --- | --- |
| Browser use, uploads, source viewing and exports | Case content travels between the staff browser and configured gateway. Only the gateway publishes a host port, loopback by default. | Keep the gateway private; control client devices and downloaded copies. These transfers are necessary for browser use. [Compose lines 382–415][compose]; [export contract](../EXPORT_READINESS.md). |
| Retrieval, generation, transcription, ClamAV and database operations; health probes | Text, prompts, media, file bytes and derived records reach the configured companions. Reference `models` and `services` networks are internal; model workers publish no ports. App health also probes the configured transcription endpoint. | Keep exact allowlists on private operator-controlled services; disable unused optional model/transcription profiles and their settings. Do not disable mandatory malware scanning as a privacy shortcut. A remote allowlisted companion changes this statement. [Compose][compose]; [endpoint validation][endpoints]; [health probe, lines 1208–1229][workbench]. |
| OIDC sign-in, if selected | Provider discovery, authorization redirect, token exchange and signing-key retrieval convey authentication information, scopes and sign-in metadata. These methods do not take matter text. | Use local accounts to avoid the configured OIDC provider; otherwise approve its HTTPS issuer and network access, including browser access. [OIDC client, lines 572–733][identity]; [authentication modes](../AUTHENTICATION.md). |
| Optional Kerberos/domain sign-in | Proxy authentication and host identity/group resolution depend on operator domain services. Container isolation does not eliminate host SSSD/DNS/domain traffic. | Use local accounts if domain integration is not intended. Qualify the host's domain traffic separately. [Kerberos mounts/network][kerberos]; [authentication runbook](../AUTHENTICATION.md). |
| ClamAV signature updater at startup and periodically (`--checks=6`) | `freshclam` receives the separate `updates` network and a signatures-only volume; uploaded evidence is not mounted in it. The scanner disables its own updater. Update/DNS requests and connection metadata leave this boundary. | Restrict update destinations at the operator network, or supply verified fresh signatures through a separately qualified offline process. There is no supported one-click offline replacement here: stopping updates can fail the freshness health gate and scanner startup. [Compose lines 25–92][compose]; [existing updater test](../../tests/test_oss_installer.py). |
| Explicit model staging or missing/changed-artifact acquisition on install/resume | Hugging Face snapshots, PyTorch alignment files and the pinned NLTK tokenizer archive are downloaded; origins/CDNs receive artifact requests and connection metadata, and gated acquisition uses a token. Staging has a writable model mount and no matter-data mount. | Prestage and verify the exact inventory; avoid invoking acquisition on the confidential runtime. `plan` and `verify` are offline; do not treat `stage` as offline. [Stager download paths, lines 104–149 and 392–490][stager]; [catalog URLs, lines 99–128][catalog]; [acquisition guide](../MODEL_ACQUISITION.md). |
| Installation, image build/pull and explicit update | Docker registries, distribution repositories and Python indexes supply images/packages; transcription's compatibility build also obtains its upstream source. Source checkout/download is a separate network step. | Use reviewed, preloaded artifacts or qualified internal mirrors and test the prepared node. The ordinary installer is **not** network-free. [Installer build/acquisition, lines 2590–2747 and 3288–3355][installer]; [application Dockerfile][dockerfile]; [transcription Dockerfile][transcription-docker]; [compatibility builder][compatibility]. |
| Browser assets and application updates | The workbench base template serves its scripts/styles/icons locally; bundled font URLs are relative. Inspection found no external CDN/font loader or automatic release-check client in the checked workbench source. The explicit installer `update` stages the supplied source and builds replacement images. | Preserve local assets; reassess custom UI/dependencies and operator update tooling. This source inspection is not a capture of every browser or dependency request. [Base template, lines 1–22][template]; [font definitions, lines 1–5][fonts]; [installer update, lines 3288–3355][installer]. |
| Optional standalone Transcript Studio | The separate Streamlit UI sends requests to its configured transcription API; it is not a service in the root Compose deployment. Its framework/browser behavior is outside the reference network proof. | Do not run the standalone UI under this statement without a separate dependency, telemetry and destination review. [Studio API client, lines 425–460][studio-client]; [Studio URL setting, lines 532–545][studio]; [studio image target][transcription-docker]. |
| Network-mounted storage, backup, support and exports | Host-mounted NAS/backup storage and external backup tools can transfer confidential copies. The bundled backup accepts a filesystem repository path, which may itself be a network mount. Staff can also transmit exports or diagnostics outside the app. | Choose and vet storage/backup providers; restrict host traffic; control export recipients. Do not send case data to public support channels. [Storage/backup contract](../STORAGE_AND_BACKUP.md); [backup path validation, lines 256–305][backup]; [public reporting rules](../../SECURITY.md). |

## Retention, access and incident responsibilities

Matter closure requires authorized human steps and a cancellation window.
**Export and exit** requires a preservation archive; **Delete without export**
requires a separate no-export acknowledgement. Neither is immediate secure erasure. Minimal closure/audit records remain,
and originals outside managed storage are preserved. A downloaded archive or
backup has its own lifecycle. Transcription queue expiry is separate from retained
matter transcripts. Set retention, preservation holds, backup disposal and access
procedures for the actual matter; do not infer a legal retention period from
application defaults. ([Lifecycle contract](../CASE_LIFECYCLE.md),
[security boundary](../SECURITY_MODEL.md#security-model),
[transcription retention](../../services/transcription/README.md))

Local inference does not eliminate unauthorized access, model errors, omitted
material or cross-matter disclosure caused by operational mistakes. Verify
sources and outputs and train reviewers. This repository's vulnerability-reporting
instructions are not a contractual breach-notice service, data-processing
agreement or promise of a response deadline. Obtain appropriate commitments from
any operator or outside provider. ([Security model](../SECURITY_MODEL.md),
[security reporting](../../SECURITY.md), [license warranty boundary](../../LICENSE))

## Bounded network evidence

Run from the repository root with the development environment:

```sh
python -m pytest -q tests/test_reference_network_boundary.py \
  tests/test_model_acquisition_plan.py \
  tests/test_tokenizer_staging.py \
  tests/test_generation.py::test_generator_endpoint_must_use_an_explicitly_allowed_host \
  tests/test_oss_installer.py::test_clamav_retains_only_capabilities_needed_by_upstream_entrypoint \
  tests/test_cleanup_service_contract.py
./scripts/compose-check.sh
```

The focused test enumerates every root Compose service, the internal networks,
no-network roles, unpublished companions, offline flags, and the updater's
signature-only mount. It deliberately asserts the egress-capable exceptions:
`app`, `gateway`, `clamav-updater` and the opt-in `model-stager`. Existing tests
block download calls in specific synthetic plan/tokenizer paths and check endpoint
allowlisting. Compose validation checks rendered standard and supplied overlays.
These are configuration and bounded unit-test proofs, **not** packet captures,
firewall enforcement on `app/front`, exhaustive dependency audits or evidence that
an install makes zero outbound calls. No live GPU inference or private deployment
is certified by this kit. Re-run the proof after changing code, images, model
inventory, environment, overlays or destinations; record a synthetic egress test
of the actual topology before making stronger claims.

[compose]: https://github.com/Exculpata/exculpata/blob/6ebc292/compose.yaml#L25-L478
[generation]: https://github.com/Exculpata/exculpata/blob/6ebc292/src/case_intelligence/generation.py#L242-L291
[retrieval]: https://github.com/Exculpata/exculpata/blob/6ebc292/src/case_intelligence/review_bench_v2.py#L218-L336
[media]: https://github.com/Exculpata/exculpata/blob/6ebc292/src/case_intelligence/media_evidence.py#L247-L305
[malware]: https://github.com/Exculpata/exculpata/blob/6ebc292/src/case_intelligence/malware_scan.py#L188-L318
[endpoints]: https://github.com/Exculpata/exculpata/blob/6ebc292/src/case_intelligence/service_endpoints.py#L22-L78
[nemotron]: https://github.com/Exculpata/exculpata/blob/6ebc292/services/transcription/src/transcription_v2/nemotron_runner.py#L28-L43
[pipeline]: https://github.com/Exculpata/exculpata/blob/6ebc292/services/transcription/src/transcription_v2/pipeline.py#L126-L168
[workbench]: https://github.com/Exculpata/exculpata/blob/6ebc292/src/case_intelligence/workbench.py#L1208-L1229
[identity]: https://github.com/Exculpata/exculpata/blob/6ebc292/src/case_intelligence/identity.py#L572-L733
[kerberos]: https://github.com/Exculpata/exculpata/blob/6ebc292/compose.kerberos.yaml#L1-L39
[stager]: https://github.com/Exculpata/exculpata/blob/6ebc292/scripts/stage-models.py#L104-L490
[catalog]: https://github.com/Exculpata/exculpata/blob/6ebc292/config/models.json#L99-L128
[installer]: https://github.com/Exculpata/exculpata/blob/6ebc292/scripts/exculpata_install.py#L2590-L3355
[dockerfile]: https://github.com/Exculpata/exculpata/blob/6ebc292/Dockerfile#L1-L51
[transcription-docker]: https://github.com/Exculpata/exculpata/blob/6ebc292/services/transcription/Dockerfile#L1-L54
[compatibility]: https://github.com/Exculpata/exculpata/blob/6ebc292/services/transcription/compatibility/build_whisperx.py
[template]: https://github.com/Exculpata/exculpata/blob/6ebc292/src/case_intelligence/templates/workbench_base.html#L1-L22
[fonts]: https://github.com/Exculpata/exculpata/blob/6ebc292/src/case_intelligence/static/theme-tokens.css#L1-L5
[studio-client]: https://github.com/Exculpata/exculpata/blob/6ebc292/services/transcription/ui/api_client.py#L425-L460
[studio]: https://github.com/Exculpata/exculpata/blob/6ebc292/services/transcription/ui/streamlit_app.py#L532-L545
[backup]: https://github.com/Exculpata/exculpata/blob/6ebc292/scripts/recordbench_backup.py#L256-L305

[retrieval-worker]: https://github.com/Exculpata/exculpata/blob/6ebc292/src/case_intelligence/retrieval_worker.py#L111-L168
