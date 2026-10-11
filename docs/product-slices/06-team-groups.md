# 06: Add reusable team groups with explicit matter access

Status: implemented on `main` as
[#65](https://github.com/Exculpata/exculpata/pull/65). See
[reusable team groups](../TEAM_GROUPS.md) and
[synthetic acceptance](../TEAM_GROUPS_ACCEPTANCE.md). Depends on 04 and 05.

## Finding and outcome

OIDC/Kerberos groups govern admission and administrator mapping. Existing
case-team membership selects individual principals. These are not a reusable
application-managed team group that can be assigned to a matter.

## Small implementation

Add named application team groups, member management, and explicit group grants
to matters. Keep identity-provider groups distinct. Show why a person has access:
owner, direct member, or a named group grant. Group removal must not remove a
separate direct grant; removing one grant must not silently preserve access if
it was the last valid grant.

Use live membership checks and defined revocation behavior for requests,
background work, exports, and open sessions. Preserve the existing distinction
between system administration and matter content access. First version has no
nested groups or automatic external-directory synchronization.

## Code and acceptance

Start with `workspace_store.py` membership authorization, `identity.py`, matter
membership routes in `workbench.py`, and setup/settings templates. Implement
the shared data-layer check, not just UI filtering.

Synthetic tests cover two groups/two matters, mixed direct/group access,
revocation during queued work, removed/disabled principals, concurrent grants,
and attribution. Browser acceptance creates a group and proves access with two
real test sessions. Add mirrored migrations, backup and clean-restore evidence,
rollback constraints, and updated authentication/security documentation.
