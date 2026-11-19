# Matter approval for routine external model requests

## Kickoff

Let an authorized operator decide which model service may receive material from a particular matter. Show the capability, destination and model in matter setup, and support revocation. [Issue 563](https://github.com/Exculpata/exculpata/issues/563)

Expected changes cover the workspace store, matter setup, the generation and retrieval request paths, indexing jobs, model configuration documentation and synthetic regression tests.

With two synthetic matters, authorize one and capture the provider requests. Only the approved matter's text may arrive. Repeat after revoking the grant on a queued job and after changing the destination or model. In both cases the provider must receive zero case-data requests. Intake, manual review, word search and content-free endpoint diagnostics must remain usable.

## Problem and approach

An installation-level configuration identifies a provider but cannot establish permission for an individual matter. Store approvals durably with their matter, capability, canonical origin and model. Check the current grant at dispatch, including retries and each background batch. Record the operator's changes in the matter audit trail.

Existing matters have no standing external approval on upgrade. Generation should report that approval is needed. Retrieval and indexing should keep local word search available. The installation-wide rebuild keeps its own reviewed plan.
