# Approve the matters in an external embedding rebuild

## Kickoff

An operator can preview the matter IDs, embedding origin, and model involved in a rebuild, review them locally, and supply explicit approval for that exact scope before case text leaves the installation. [Issue 559](https://github.com/Exculpata/exculpata/issues/559)

Expected files are the retrieval rebuild command, the model configuration guide, and the synthetic endpoint and PostgreSQL rebuild tests.

Acceptance requires zero requests for a preview or rejected approval. An approved run sends one matter per request and commits a complete projection. A provider failure preserves the existing vectors and index identity.

## Problem and root cause

The command reads ordered chunks in batches across the installation. A batch can span matters. Installation-wide destination approval cannot establish which matters the operator intended to send.

## Approach

Add a text-free preview and a local JSON approval file binding the embedding capability, destination, model, and complete set of matter IDs. Check the file against the locked projection before opening the source-text cursor. Preserve the existing transaction, credential handling, transport checks, and bundled-worker rebuild command.

## What could break

External rebuild scripts need to supply the reviewed file. An index that changes matter membership between preview and execution requires fresh review. The index has one installation-wide identity, so all affected matters must be approved together. Use two synthetic matters to prove the denied path, separate outbound batches, rollback, and successful recovery.
