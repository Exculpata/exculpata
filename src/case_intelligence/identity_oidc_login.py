"""Shared OIDC callback, including narrowly scoped case-deletion reauthentication."""
import hmac

from .branding import PRODUCT_NAME
from .store_records import WorkspaceProblem


async def finish_oidc_login(self, *, cookie_state, returned_state, code, provider_error=None):
    from .identity import OidcAuthenticationError
    from . import case_oidc
    if (
        self.auth_mode != "oidc"
        or self.oidc_client is None
        or self.oidc_settings is None
    ):
        raise OidcAuthenticationError("Organization sign-in is unavailable.")
    if (
        not cookie_state
        or not returned_state
        or len(returned_state) > 256
        or not hmac.compare_digest(cookie_state, returned_state)
    ):
        raise OidcAuthenticationError("Organization sign-in expired. Please try again.")
    now = self._now()
    destination = self.store.consume_oidc_transaction(
        self.token_digest(returned_state),
        now=self._iso(now),
    )
    if destination is None:
        raise OidcAuthenticationError("Organization sign-in expired. Please try again.")
    reauthorization = case_oidc.take_request(self, returned_state)
    if provider_error or not code or len(code) > 4_096:
        raise OidcAuthenticationError("Organization sign-in was not completed.")
    verifier = self._derive_oidc_value(b"pkce:", returned_state)
    nonce = self._derive_oidc_value(b"nonce:", returned_state)
    claims = await self.oidc_client.authenticate(
        code=code,
        nonce=nonce,
        code_verifier=verifier,
    )
    subject = self._claim_text(claims.get("sub"), maximum=512)
    if not subject:
        raise OidcAuthenticationError("Organization sign-in did not include a stable identity.")
    if claims.get("iss") != self.oidc_settings.issuer:
        raise OidcAuthenticationError("Organization sign-in issuer did not match.")
    case_oidc.invalidate_verified_subject(self, subject)
    oidc_groups: frozenset[str] = frozenset()
    if self.oidc_settings.allowed_groups or self.oidc_settings.administrator_groups:
        raw_groups = claims.get(self.oidc_settings.groups_claim)
        if not isinstance(raw_groups, list) or any(
            not isinstance(value, str) for value in raw_groups
        ):
            raise OidcAuthenticationError(f"This account is not approved for {PRODUCT_NAME}.")
        if len(raw_groups) > 2_048 or any(
            not value
            or len(value) > 256
            or any(ord(character) < 32 for character in value)
            for value in raw_groups
        ):
            raise OidcAuthenticationError(f"This account is not approved for {PRODUCT_NAME}.")
        oidc_groups = frozenset(raw_groups)
        administrator = not self.oidc_settings.administrator_groups.isdisjoint(
            oidc_groups
        )
        admitted = (
            administrator
            or not self.oidc_settings.allowed_groups
            or not self.oidc_settings.allowed_groups.isdisjoint(oidc_groups)
        )
        if not admitted:
            raise OidcAuthenticationError(f"This account is not approved for {PRODUCT_NAME}.")
    case_oidc.validate_claims(self, reauthorization, claims)
    display_name = self._claim_text(
        claims.get(self.oidc_settings.display_name_claim),
        maximum=160,
    )
    login_name = self._claim_text(
        claims.get(self.oidc_settings.login_name_claim),
        maximum=255,
    )
    display_name = display_name or login_name
    login_name = login_name or display_name
    if not display_name or not login_name:
        raise OidcAuthenticationError("Organization sign-in did not include profile claims.")
    principal = self.store.upsert_principal(
        self.oidc_settings.provider_key,
        subject,
        display_name,
        login_name,
    )
    roles = (
        frozenset({"administrator"})
        if not self.oidc_settings.administrator_groups.isdisjoint(oidc_groups)
        else frozenset()
    )
    context, raw_token = self._issue_session(principal, "oidc", roles)
    try:
        case_oidc.finish(self, reauthorization, context, claims)
    except (WorkspaceProblem, KeyError):
        self.store.revoke_session(context.session.session_id)
        raise case_oidc._error() from None
    return context, raw_token, destination
