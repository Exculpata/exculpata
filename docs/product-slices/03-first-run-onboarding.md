# 03: Guide the first administrator through team setup

Status: implemented on `main` via
[#59](https://github.com/Exculpata/exculpata/pull/59)
([#57](https://github.com/Exculpata/exculpata/pull/57) team setup).
See [installation handoff and team setup](../FIRST_RUN.md). Depends on 02.
People (05) and team groups (06) have also landed.

## Finding and outcome

The installer creates the initial local administrator. The page named
`workbench_setup.html` is matter/source setup, not installation onboarding.
The administrator page describes known domain users and AD groups even though
the application supports local accounts and OIDC as well.

## Small implementation

After authenticated administrator login, show a resumable setup checklist:
check enabled capabilities, explain identity management for the selected mode,
create a synthetic first matter, and verify another user's access. Link to
existing operational readiness instead of duplicating it. Report incomplete
items honestly and allow returning to setup later.

Local mode links to account management when slice 05 lands. OIDC/Kerberos
explain provider-managed identity and distinguish admission, administrator
roles, and matter membership. First ship useful onboarding around existing
capabilities; unavailable group/account UI must not be presented as completed.

Keep initial administrator creation in the authenticated installer boundary.
This slice must not add an unauthenticated web endpoint that lets the first
visitor claim administrator access.

## Code and acceptance

Start with `workbench.py` login/administrator routes, `identity.py`,
`templates/workbench_admin.html`, and `docs/AUTHENTICATION.md`.

Browser checks cover local/OIDC/Kerberos copy, administrator-only access,
keyboard navigation, restart/resume, and two synthetic users with separate
matter access. Readiness is derived from actual state, not checkbox completion.
If preferences are persisted, document migration and restore behavior.
