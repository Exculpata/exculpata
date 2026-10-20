"""Bound callback input before provider use and clean up failed browser transactions."""
import re

from .identity import IdentityService, OIDC_STATE_COOKIE, OidcAuthenticationError


class OidcCallbackFailure(OidcAuthenticationError):
    def __init__(self, error: OidcAuthenticationError, destination: str | None):
        super().__init__(str(error))
        self.destination = IdentityService.safe_next(destination)


def _state(value: str | None) -> str | None:
    return value if value and re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value) else None


def _field(query, name: str, maximum: int) -> str | None:
    values = query.getlist(name)
    return values[0] if len(values) == 1 and 0 < len(values[0]) <= maximum else None


async def finish_oidc_callback(identity, request):
    cookie_state = _state(request.cookies.get(OIDC_STATE_COOKIE))
    try:
        return await identity.finish_oidc_login(
            cookie_state=cookie_state,
            returned_state=_state(_field(request.query_params, "state", 256)),
            code=_field(request.query_params, "code", 4_096),
            provider_error="provider_error" if "error" in request.query_params else None,
        )
    except OidcAuthenticationError as error:
        destination = (identity.store.cancel_oidc_transaction(identity.token_digest(cookie_state))
                       if cookie_state else None)
        raise OidcCallbackFailure(error, destination) from None
