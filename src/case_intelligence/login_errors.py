"""Closed vocabulary for unauthenticated sign-in feedback."""
from types import MappingProxyType
from .branding import PRODUCT_NAME

LOGIN_ERRORS = MappingProxyType({
    "sign_in_failed": "Sign-in could not be completed. Please try again.",
    "preview_unavailable": "Local preview sign-in is unavailable.",
    "preview_identity": "Choose an available preview identity.",
    "preview_expired": "The sign-in page expired. Please choose your preview identity again.",
    "login_expired": "The sign-in page expired. Please try again.",
    "local_unavailable": "Local account sign-in is unavailable.",
    "local_credentials": "The username or password is incorrect.",
    "local_throttled": "Sign-in is temporarily unavailable. Wait a few minutes and try again.",
    "local_temporary": "Local account sign-in is temporarily unavailable.",
    "windows_unverified": "Windows sign-in could not be verified.",
    "windows_unavailable": "Windows sign-in is unavailable.",
    "windows_groups": "Your Windows group membership could not be verified.",
    "windows_profile": "Your Windows account profile could not be loaded.",
    "account_unapproved": f"This account is not approved for {PRODUCT_NAME}.",
    "oidc_temporary": "Organization sign-in is temporarily unavailable.",
    "oidc_configuration": "Organization sign-in configuration could not be verified.",
    "oidc_incomplete": "Organization sign-in configuration is incomplete.",
    "oidc_endpoints": "Organization sign-in endpoints do not match the approved issuer.",
    "oidc_code_flow": "Organization sign-in does not support authorization code flow.",
    "oidc_pkce": "Organization sign-in does not advertise required PKCE support.",
    "oidc_client_method": "Organization sign-in does not support the configured client authentication method.",
    "oidc_algorithms": "OIDC signing algorithms are invalid.",
    "oidc_signing": "Organization sign-in has no approved token signing algorithm.",
    "oidc_start": "Organization sign-in could not be started.",
    "oidc_unverified": "Organization sign-in could not be verified.",
    "oidc_unavailable": "Organization sign-in is unavailable.",
    "oidc_expired": "Organization sign-in expired. Please try again.",
    "oidc_incomplete_login": "Organization sign-in was not completed.",
    "oidc_subject": "Organization sign-in did not include a stable identity.",
    "oidc_issuer": "Organization sign-in issuer did not match.",
    "oidc_profile": "Organization sign-in did not include profile claims.",
    "oidc_fresh": "Fresh organization sign-in could not authorize this closure. Start again with the same administrator account.",
})
_CODES = {message: code for code, message in LOGIN_ERRORS.items()}


def login_error_code(error: Exception) -> str:
    """Unknown exceptions never become public message text or URL content."""
    return _CODES.get(str(error), "sign_in_failed")


def login_error_message(codes: list[str]) -> str:
    """Ignore absent, repeated, oversized and unknown caller-supplied values."""
    if len(codes) != 1 or len(codes[0]) > 64:
        return ""
    return LOGIN_ERRORS.get(codes[0], "")
