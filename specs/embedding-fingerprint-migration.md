# Verify a bundled embedding index before switching services

## Kickoff

Operators migrating a populated retrieval index should be able to keep its vectors and citations when the running pinned worker and the replacement endpoint produce matching embeddings for a fixed synthetic fixture. A failed comparison must leave the index and its identity untouched. This is a prerequisite in the [model API migration](https://github.com/Exculpata/exculpata/issues/558).

Expected files include the embedding index record, retrieval rebuild and dispatch paths, bundled worker health, operator guide and synthetic tests. Verify matching and changed vectors, a changed worker revision, older and incomplete projections, provider failure, grant checks, and the actual PostgreSQL-backed command. Compare source rows and vectors before and after migration.

## Approach and risks

With application writers stopped, require the recorded producing worker and a complete, current-format projection. Compare fixed synthetic vectors through the local worker and the new endpoint. Publish a versioned fingerprint and the destination identity atomically only after both match. Recheck the fingerprint in diagnostics and when using the migrated index. Preserve the approved rebuild for unknown provenance, missing vectors, unavailable workers or incompatible output. A finite compatibility sample cannot attest the provider's model weights.
