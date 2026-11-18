# Operator-configured model endpoints

## Kickoff

Reviewers should be able to tell whether search can find related passages, rerank word matches, or only search by word. Operators should be able to move the bundled answer workload to their own vLLM API and check it from the installed application.

This first increment of [issue 268](https://github.com/Exculpata/exculpata/issues/268) covers retrieval diagnostics and operator-supplied vLLM. It builds on [the existing retrieval endpoints](https://github.com/Exculpata/exculpata/pull/383). The separate [reranker evaluation](https://github.com/Exculpata/exculpata/pull/437) concerns the bundled worker.

Expected changes include the retrieval adapters, workbench status and search paths, setup or administrator views, generator installer and doctor, model configuration guide, and synthetic regression tests.

Acceptance requires independent checks of embeddings, reranking, and index compatibility. With only reranking, the workspace can order word matches. With an incompatible index, users can still ingest and search source text. An operator can configure an external vLLM server on an installation with no bundled models and check the API from the app through its existing relay.

## Problem and root cause

The workbench uses one flag for two independent retrieval capabilities. The embedding diagnostic also checks the index before contacting the endpoint, so a working server can appear broken during migration. The answer client already speaks vLLM's protocol, but the installer omits it from the external-server choices and its external diagnostic runs on the host.

## Approach and risks

Keep the existing environment and secret-file configuration. Probe configured retrieval roles with synthetic inputs. Distinguish a successful endpoint check from an index that can accept its vectors. Preserve the source text and lexical search path when learned vectors are unavailable. Keep the existing HTTPS, destination pins, host allowlists, relay, and consent checks for every external call.

Current installations may use native Ollama, a bundled worker, or separate endpoints. Exercise the existing paths alongside the new setup. Check that an unavailable capability never silently changes the model used for a request and that an old index cannot acquire an unrelated model identity.

## Migration after this increment

Before retiring the default retrieval worker, provide a rebuild path for existing installations and verify existing vectors against a synthetic fingerprint. Bring per-matter authorization into interactive and background calls before expanding external case-data processing. Keep synthetic checks distinct from permission to send case content.

For speech, retain the app's durable job service and configure its upstream transcription, alignment, translation, and diarization capabilities separately. Cover interrupted uploads and reviewer edits before retiring local engines. Decide explicitly how OCR for scanned evidence is provided. Remove staging, model profiles, and model services capability by capability after operators have a verified replacement. Preserve intake, manual review, and exact search throughout the migration.
