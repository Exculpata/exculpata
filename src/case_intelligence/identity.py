"""Provider-neutral principals, organization sign-in, and server-side sessions."""
from __future__ import annotations

import base64
import grp
import hashlib
import hmac
import ipaddress
import json
import os
import pwd
import re
import secrets
import stat
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol
from urllib.parse import parse_qs, urlsplit

import httpx
from authlib.integrations.base_client.errors import AuthlibBaseError
from authlib.integrations.starlette_client import OAuth
from joserfc.errors import JoseError

from .branding import PRODUCT_NAME
from .local_accounts import LocalAccount, LocalAccountRepository
from .compat_env import env
from .workspace_store import PrincipalRecord, SessionRecord, WorkspaceProblem, WorkspaceStore

SESSION_COOKIE = "case_intelligence_session"
LOGIN_CHALLENGE_COOKIE = "case_intelligence_login_challenge"
OIDC_STATE_COOKIE = "case_intelligence_oidc_state"
KERBEROS_USER_HEADER, KERBEROS_SECRET_HEADER = "X-RecordBench-Authenticated-User", "X-RecordBench-Proxy-Secret"


def resolve_auth_mode(auth_mode: str | None = None) -> str:
    """Require deliberate authentication before creating runtime or identity state."""
    configured = os.getenv("CASE_INTELLIGENCE_AUTH_MODE", "") if auth_mode is None else auth_mode
    selected = configured.strip().lower()
    if selected not in {"preview", "test", "local", "oidc", "kerberos"}:
        raise RuntimeError(
            "An explicit supported identity provider is required "
            "(CASE_INTELLIGENCE_AUTH_MODE)."
        )
    if env("RECORDBENCH_ALLOW_CONTAINER_BIND", "") == "1" and selected in {"preview", "test"}:
        raise RuntimeError("An installed service requires local, OIDC or Kerberos authentication.")
    return selected


_CLAIM_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,79}$")
_SAFE_SIGNING_ALGORITHMS = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES384",
        "ES512",
        "EdDSA",
    }
)


@dataclass(frozen=True)
class PreviewIdentity:
    subject: str
    display_name: str
    login_name: str
    preferred_principal_id: str


PREVIEW_IDENTITIES = (
    PreviewIdentity(
        "taylor-morgan",
        "Taylor Morgan",
        "taylor.morgan@example.test",
        "development-taylor-morgan",
    ),
    PreviewIdentity(
        "jordan-lee",
        "Jordan Lee",
        "jordan.lee@example.test",
        "development-jordan-lee",
    ),
    PreviewIdentity(
        "alex-rivera",
        "Alex Rivera",
        "alex.rivera@example.test",
        "development-alex-rivera",
    ),
)


class OidcAuthenticationError(WorkspaceProblem):
    """A staff-safe OIDC failure with no provider token or claim content."""


class KerberosAuthenticationError(WorkspaceProblem):
    """A staff-safe Kerberos failure with no ticket or proxy-secret content."""


class LocalAuthenticationError(WorkspaceProblem):
    """A generic local-account failure that never reveals account existence."""


def _strict_https_url(value: str, *, label: str, origin_only: bool = False) -> str:
    candidate = (value or "").strip().rstrip("/")
    if not candidate or len(candidate) > 1_024:
        raise RuntimeError(f"{label} is missing or invalid")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError(f"{label} must be an exact HTTPS URL")
    if origin_only and parsed.path not in {"", "/"}:
        raise RuntimeError(f"{label} must not contain a path")
    hostname = parsed.hostname.casefold()
    if hostname == "localhost":
        raise RuntimeError(f"{label} must not use a loopback host")
    try:
        if ipaddress.ip_address(hostname).is_loopback:
            raise RuntimeError(f"{label} must not use a loopback host")
    except ValueError:
        pass
    return candidate


def _regular_file(path: Path, *, label: str, exact_mode: int | None = None) -> Path:
    candidate = Path(path)
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise RuntimeError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"{label} path is unsafe")
    if exact_mode is not None and stat.S_IMODE(metadata.st_mode) != exact_mode:
        raise RuntimeError(f"{label} must have mode {exact_mode:04o}")
    if exact_mode is not None and metadata.st_uid != os.geteuid():
        raise RuntimeError(f"{label} must be owned by the service account")
    return candidate


_KERBEROS_REALM = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$"
)
_KERBEROS_LOCAL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_KERBEROS_GROUP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@\\ -]{0,254}$")
_PROXY_SECRET = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
_LOCAL_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._@-]{2,127}$")


def _normalized_realm(value: str) -> str:
    realm = (value or "").strip().upper()
    if not _KERBEROS_REALM.fullmatch(realm) or "." not in realm:
        raise RuntimeError("Kerberos realm is missing or invalid")
    return realm


def _normalized_kerberos_principal(value: str, *, realm: str) -> str:
    candidate = (value or "").strip()
    if len(candidate) > 255 or candidate.count("@") != 1:
        raise KerberosAuthenticationError("Windows sign-in could not be verified.")
    local_name, supplied_realm = candidate.rsplit("@", 1)
    if (
        not _KERBEROS_LOCAL_NAME.fullmatch(local_name)
        or not _KERBEROS_REALM.fullmatch(supplied_realm)
        or supplied_realm.upper() != realm
    ):
        raise KerberosAuthenticationError("Windows sign-in could not be verified.")
    return f"{local_name.casefold()}@{realm}"


def _normalized_kerberos_group(value: str) -> str:
    candidate = (value or "").strip().casefold()
    if not _KERBEROS_GROUP_NAME.fullmatch(candidate):
        raise RuntimeError("Kerberos group configuration is invalid")
    return candidate


def _system_identity_profile(principal: str) -> tuple[str, str]:
    login_name = principal.casefold()
    display_name = ""
    try:
        account = pwd.getpwnam(login_name)
        display_name = account.pw_gecos.split(",", 1)[0].strip()
        login_name = account.pw_name.strip().casefold() or login_name
    except (KeyError, OSError):
        pass
    if not display_name:
        local_name = principal.split("@", 1)[0]
        display_name = " ".join(part.capitalize() for part in local_name.split(".") if part)
    return display_name or principal, login_name


def _system_group_memberships(principal: str) -> frozenset[str]:
    """Resolve the validated domain principal through the host NSS/SSSD join."""

    account = pwd.getpwnam(principal.casefold())
    group_ids = os.getgrouplist(account.pw_name, account.pw_gid)
    groups: set[str] = set()
    for group_id in group_ids:
        try:
            name = grp.getgrgid(group_id).gr_name
        except KeyError:
            continue
        groups.add(_normalized_kerberos_group(name))
    if not groups:
        raise OSError("Windows group membership is unavailable")
    return frozenset(groups)


@dataclass(frozen=True)
class KerberosSettings:
    realm: str
    proxy_secret: str = field(repr=False)
    allowed_principals: frozenset[str] = frozenset()
    allowed_groups: frozenset[str] = frozenset()
    administrator_principals: frozenset[str] = frozenset()
    administrator_groups: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        realm = _normalized_realm(self.realm)
        object.__setattr__(self, "realm", realm)
        if not isinstance(self.proxy_secret, str) or not _PROXY_SECRET.fullmatch(
            self.proxy_secret
        ):
            raise RuntimeError("Kerberos proxy secret is missing or invalid")
        try:
            principals = frozenset(
                _normalized_kerberos_principal(value, realm=realm)
                for value in self.allowed_principals
                if isinstance(value, str) and value.strip()
            )
        except KerberosAuthenticationError as exc:
            raise RuntimeError("Kerberos principal allowlist is invalid") from exc
        if len(principals) > 200:
            raise RuntimeError("Kerberos principal allowlist is invalid")
        object.__setattr__(self, "allowed_principals", principals)
        try:
            administrator_principals = frozenset(
                _normalized_kerberos_principal(value, realm=realm)
                for value in self.administrator_principals
                if isinstance(value, str) and value.strip()
            )
        except KerberosAuthenticationError as exc:
            raise RuntimeError("Kerberos administrator principal list is invalid") from exc
        if len(administrator_principals) > 50:
            raise RuntimeError("Kerberos administrator principal list is invalid")
        object.__setattr__(self, "administrator_principals", administrator_principals)
        allowed_groups = frozenset(
            _normalized_kerberos_group(value)
            for value in self.allowed_groups
            if isinstance(value, str) and value.strip()
        )
        administrator_groups = frozenset(
            _normalized_kerberos_group(value)
            for value in self.administrator_groups
            if isinstance(value, str) and value.strip()
        )
        if len(allowed_groups) > 100 or len(administrator_groups) > 50:
            raise RuntimeError("Kerberos group configuration is invalid")
        object.__setattr__(self, "allowed_groups", allowed_groups)
        object.__setattr__(self, "administrator_groups", administrator_groups)
        if not (
            principals
            or allowed_groups
            or administrator_principals
            or administrator_groups
        ):
            raise RuntimeError("Kerberos admission policy is missing or invalid")

    @property
    def provider_key(self) -> str:
        digest = hashlib.sha256(self.realm.encode("ascii")).hexdigest()[:16]
        return f"kerberos-{digest}"

    def normalize_principal(self, value: str) -> str:
        return _normalized_kerberos_principal(value, realm=self.realm)

    @classmethod
    def from_env(cls) -> "KerberosSettings":
        secret_path_value = os.getenv(
            "CASE_INTELLIGENCE_KERBEROS_PROXY_SECRET_FILE", ""
        ).strip()
        if not secret_path_value:
            raise RuntimeError("Kerberos proxy secret file is required")
        secret_path = _regular_file(
            Path(secret_path_value),
            label="Kerberos proxy secret file",
            exact_mode=0o600,
        )
        try:
            proxy_secret = secret_path.read_text(encoding="ascii").rstrip("\r\n")
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError("Kerberos proxy secret file is unreadable") from exc
        principals = frozenset(
            value.strip()
            for value in os.getenv(
                "CASE_INTELLIGENCE_KERBEROS_ALLOWED_PRINCIPALS", ""
            ).split(",")
            if value.strip()
        )
        allowed_groups = frozenset(
            value.strip()
            for value in os.getenv(
                "CASE_INTELLIGENCE_KERBEROS_ALLOWED_GROUPS", ""
            ).split(",")
            if value.strip()
        )
        administrator_principals = frozenset(
            value.strip()
            for value in os.getenv(
                "CASE_INTELLIGENCE_KERBEROS_ADMIN_PRINCIPALS", ""
            ).split(",")
            if value.strip()
        )
        administrator_groups = frozenset(
            value.strip()
            for value in os.getenv(
                "CASE_INTELLIGENCE_KERBEROS_ADMIN_GROUPS", ""
            ).split(",")
            if value.strip()
        )
        return cls(
            realm=os.getenv("CASE_INTELLIGENCE_KERBEROS_REALM", ""),
            proxy_secret=proxy_secret,
            allowed_principals=principals,
            allowed_groups=allowed_groups,
            administrator_principals=administrator_principals,
            administrator_groups=administrator_groups,
        )


class LocalAccountSettings:
    """Read-only live account snapshots; the operator repository owns mutations."""

    provider_key = "local"

    def __init__(self, accounts_file: Path, *, management_root: Path | None = None) -> None:
        from argon2 import PasswordHasher

        self.accounts_file = Path(accounts_file)
        self.repository = LocalAccountRepository(self.accounts_file)
        self.repository.read()  # Fail startup closed on unsafe or invalid files.
        self.management_root = Path(management_root) if management_root is not None else None
        if self.management_root is not None:
            self._validate_management_boundary()
        self._hasher = PasswordHasher()
        self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(32))

    def _validate_management_boundary(self) -> None:
        root = self.management_root
        if root is None:
            raise RuntimeError("Browser account management has not been enabled")
        if (not root.is_absolute() or self.accounts_file != root / "local-accounts.json"
                or root.is_symlink() or any(parent.is_symlink() for parent in root.parents)):
            raise RuntimeError("Browser account management requires its dedicated account directory")
        metadata = root.stat()
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise RuntimeError("Browser account directory must be owner-only with mode 0700")
        allowed = re.compile(r"^(?:local-accounts\.json|\.local-accounts\.json\.lock|\.local-accounts\.json-[0-9a-f]{32}\.tmp)$")
        if any(not allowed.fullmatch(path.name) or path.is_symlink() or not path.is_file() for path in root.iterdir()):
            raise RuntimeError("Browser account directory must contain only its account and lock files")
        if any(account.session_revision == "legacy" for account in self.repository.read().values()):
            raise RuntimeError("Migrate local accounts before enabling browser management")

    @property
    def management_enabled(self) -> bool:
        return self.management_root is not None

    @property
    def accounts(self) -> Mapping[str, LocalAccount]:
        # Atomic file replacement gives each request a complete snapshot without
        # requiring the application's read-only secrets mount to create a lock.
        return self.repository.read()

    @classmethod
    def from_env(cls) -> "LocalAccountSettings":
        value = os.getenv("CASE_INTELLIGENCE_LOCAL_ACCOUNTS_FILE", "").strip()
        if not value:
            raise RuntimeError("Local account file is required")
        management = os.getenv("CASE_INTELLIGENCE_LOCAL_ACCOUNT_MANAGEMENT_ROOT", "").strip()
        return cls(Path(value), management_root=Path(management) if management else None)

    def account(self, username: str) -> LocalAccount | None:
        normalized = (username or "").strip().casefold()
        return self.accounts.get(normalized)

    def authenticate(self, username: str, password: str) -> LocalAccount | None:
        account = self.account(username)
        expected_hash = (
            account.password_hash
            if account is not None and account.enabled
            else self._dummy_hash
        )
        verified = False
        try:
            verified = bool(self._hasher.verify(expected_hash, password))
        except Exception:
            verified = False
        return account if verified and account is not None and account.enabled else None


@dataclass(frozen=True)
class OidcSettings:
    issuer: str
    external_origin: str
    client_id: str
    client_secret: str = field(repr=False)
    scopes: tuple[str, ...] = ("openid", "profile", "email")
    allowed_groups: frozenset[str] = frozenset()
    administrator_groups: frozenset[str] = frozenset()
    groups_claim: str = "groups"
    display_name_claim: str = "name"
    login_name_claim: str = "preferred_username"
    token_auth_method: str = "client_secret_post"
    ca_file: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "issuer", _strict_https_url(self.issuer, label="OIDC issuer"))
        object.__setattr__(
            self,
            "external_origin",
            _strict_https_url(
                self.external_origin,
                label=f"{PRODUCT_NAME} external origin",
                origin_only=True,
            ),
        )
        client_id = (self.client_id or "").strip()
        if not client_id or len(client_id) > 512 or any(ord(value) < 32 for value in client_id):
            raise RuntimeError("OIDC client ID is missing or invalid")
        object.__setattr__(self, "client_id", client_id)
        if not isinstance(self.client_secret, str) or not 16 <= len(self.client_secret) <= 4_096:
            raise RuntimeError("OIDC client secret is missing or invalid")
        if any(value in self.client_secret for value in ("\x00", "\r", "\n")):
            raise RuntimeError("OIDC client secret is invalid")
        normalized_scopes = tuple(dict.fromkeys(value.strip() for value in self.scopes if value.strip()))
        if "openid" not in normalized_scopes or len(normalized_scopes) > 12:
            raise RuntimeError("OIDC scopes must include openid")
        if any(not re.fullmatch(r"[A-Za-z0-9:._/-]{1,80}", value) for value in normalized_scopes):
            raise RuntimeError("OIDC scopes are invalid")
        object.__setattr__(self, "scopes", normalized_scopes)
        for value in (self.groups_claim, self.display_name_claim, self.login_name_claim):
            if not _CLAIM_NAME.fullmatch(value):
                raise RuntimeError("OIDC claim configuration is invalid")
        groups = frozenset(value.strip() for value in self.allowed_groups if value.strip())
        if len(groups) > 100 or any(
            len(value) > 256 or any(ord(character) < 32 for character in value)
            for value in groups
        ):
            raise RuntimeError("OIDC allowed-group configuration is invalid")
        object.__setattr__(self, "allowed_groups", groups)
        administrator_groups = frozenset(
            value.strip() for value in self.administrator_groups if value.strip()
        )
        if len(administrator_groups) > 50 or any(
            len(value) > 256 or any(ord(character) < 32 for character in value)
            for value in administrator_groups
        ):
            raise RuntimeError("OIDC administrator-group configuration is invalid")
        object.__setattr__(self, "administrator_groups", administrator_groups)
        if self.token_auth_method not in {"client_secret_basic", "client_secret_post"}:
            raise RuntimeError("OIDC token authentication method is invalid")
        if self.ca_file is not None:
            object.__setattr__(
                self,
                "ca_file",
                _regular_file(Path(self.ca_file), label="OIDC CA file"),
            )

    @property
    def metadata_url(self) -> str:
        return self.issuer + "/.well-known/openid-configuration"

    @property
    def callback_url(self) -> str:
        return self.external_origin + "/auth/oidc/callback"

    @property
    def provider_key(self) -> str:
        digest = hashlib.sha256(self.issuer.encode("utf-8")).hexdigest()[:16]
        return f"oidc-{digest}"

    @classmethod
    def from_env(cls) -> "OidcSettings":
        secret_path_value = os.getenv("CASE_INTELLIGENCE_OIDC_CLIENT_SECRET_FILE", "").strip()
        if not secret_path_value:
            raise RuntimeError("OIDC client secret file is required")
        secret_path = _regular_file(
            Path(secret_path_value),
            label="OIDC client secret file",
            exact_mode=0o600,
        )
        try:
            raw_secret = secret_path.read_bytes()
            secret = raw_secret.decode("utf-8").rstrip("\r\n")
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError("OIDC client secret file is unreadable") from exc
        ca_value = os.getenv("CASE_INTELLIGENCE_OIDC_CA_FILE", "").strip()
        groups = frozenset(
            value.strip()
            for value in os.getenv("CASE_INTELLIGENCE_OIDC_ALLOWED_GROUPS", "").split(",")
            if value.strip()
        )
        administrator_groups = frozenset(
            value.strip()
            for value in os.getenv(
                "CASE_INTELLIGENCE_OIDC_ADMIN_GROUPS", ""
            ).split(",")
            if value.strip()
        )
        scopes = tuple(
            value
            for value in os.getenv(
                "CASE_INTELLIGENCE_OIDC_SCOPES", "openid profile email"
            ).split()
            if value
        )
        return cls(
            issuer=os.getenv("CASE_INTELLIGENCE_OIDC_ISSUER", ""),
            external_origin=os.getenv("CASE_INTELLIGENCE_EXTERNAL_ORIGIN", ""),
            client_id=os.getenv("CASE_INTELLIGENCE_OIDC_CLIENT_ID", ""),
            client_secret=secret,
            scopes=scopes,
            allowed_groups=groups,
            administrator_groups=administrator_groups,
            groups_claim=os.getenv("CASE_INTELLIGENCE_OIDC_GROUPS_CLAIM", "groups").strip(),
            display_name_claim=os.getenv(
                "CASE_INTELLIGENCE_OIDC_DISPLAY_NAME_CLAIM", "name"
            ).strip(),
            login_name_claim=os.getenv(
                "CASE_INTELLIGENCE_OIDC_LOGIN_NAME_CLAIM", "preferred_username"
            ).strip(),
            token_auth_method=os.getenv(
                "CASE_INTELLIGENCE_OIDC_TOKEN_AUTH_METHOD", "client_secret_post"
            ).strip(),
            ca_file=Path(ca_value) if ca_value else None,
        )


class OidcProviderClient(Protocol):
    async def authorization_url(
        self,
        *,
        state: str,
        nonce: str,
        code_verifier: str,
    ) -> str: ...

    async def authenticate(
        self,
        *,
        code: str,
        nonce: str,
        code_verifier: str,
    ) -> Mapping[str, Any]: ...


class AuthlibOidcClient:
    """Small, strict adapter around Authlib's maintained OIDC implementation."""

    def __init__(self, settings: OidcSettings) -> None:
        self.settings = settings
        verify: bool | str = str(settings.ca_file) if settings.ca_file else True
        registry = OAuth()
        self.remote = registry.register(
            name="district",
            client_id=settings.client_id,
            client_secret=settings.client_secret,
            server_metadata_url=settings.metadata_url,
            client_kwargs={
                "scope": " ".join(settings.scopes),
                "code_challenge_method": "S256",
                "token_endpoint_auth_method": settings.token_auth_method,
                "timeout": httpx.Timeout(10.0, connect=5.0),
                "verify": verify,
                "follow_redirects": False,
            },
        )
        self._metadata_ready = False

    @staticmethod
    def _origin(value: str) -> tuple[str, str, int]:
        parsed = urlsplit(value)
        return parsed.scheme, (parsed.hostname or "").casefold(), parsed.port or 443

    async def _validated_metadata(self) -> Mapping[str, Any]:
        try:
            metadata = await self.remote.load_server_metadata()
        except (AuthlibBaseError, httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            raise OidcAuthenticationError("Organization sign-in is temporarily unavailable.") from exc
        if self._metadata_ready:
            return metadata
        if metadata.get("issuer") != self.settings.issuer:
            raise OidcAuthenticationError("Organization sign-in configuration could not be verified.")
        issuer_origin = self._origin(self.settings.issuer)
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            value = metadata.get(key)
            if not isinstance(value, str):
                raise OidcAuthenticationError("Organization sign-in configuration is incomplete.")
            try:
                normalized = _strict_https_url(value, label=f"OIDC {key}")
            except RuntimeError as exc:
                raise OidcAuthenticationError(
                    "Organization sign-in configuration could not be verified."
                ) from exc
            if self._origin(normalized) != issuer_origin:
                raise OidcAuthenticationError(
                    "Organization sign-in endpoints do not match the approved issuer."
                )
        response_types = metadata.get("response_types_supported")
        if isinstance(response_types, list) and "code" not in response_types:
            raise OidcAuthenticationError("Organization sign-in does not support authorization code flow.")
        challenges = metadata.get("code_challenge_methods_supported")
        if not isinstance(challenges, list) or "S256" not in challenges:
            raise OidcAuthenticationError("Organization sign-in does not advertise required PKCE support.")
        auth_methods = metadata.get("token_endpoint_auth_methods_supported")
        if isinstance(auth_methods, list) and self.settings.token_auth_method not in auth_methods:
            raise OidcAuthenticationError(
                "Organization sign-in does not support the configured client authentication method."
            )
        algorithms = metadata.get("id_token_signing_alg_values_supported") or ["RS256"]
        if not isinstance(algorithms, list):
            raise OidcAuthenticationError("OIDC signing algorithms are invalid.")
        safe_algorithms = [value for value in algorithms if value in _SAFE_SIGNING_ALGORITHMS]
        if not safe_algorithms:
            raise OidcAuthenticationError("Organization sign-in has no approved token signing algorithm.")
        self.remote.server_metadata["id_token_signing_alg_values_supported"] = safe_algorithms
        self._metadata_ready = True
        return metadata

    async def authorization_url(
        self,
        *,
        state: str,
        nonce: str,
        code_verifier: str,
    ) -> str:
        await self._validated_metadata()
        try:
            result = await self.remote.create_authorization_url(
                redirect_uri=self.settings.callback_url,
                state=state,
                nonce=nonce,
                code_verifier=code_verifier,
            )
            url = result.get("url")
            returned_state = result.get("state")
            if not isinstance(url, str) or returned_state != state:
                raise ValueError("invalid authorization response")
            query = parse_qs(urlsplit(url).query)
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(code_verifier.encode("ascii")).digest()
            ).rstrip(b"=").decode("ascii")
            if (
                query.get("state") != [state]
                or query.get("nonce") != [nonce]
                or query.get("code_challenge_method") != ["S256"]
                or query.get("code_challenge") != [challenge]
                or query.get("response_type") != ["code"]
                or query.get("client_id") != [self.settings.client_id]
                or query.get("redirect_uri") != [self.settings.callback_url]
                or "openid" not in " ".join(query.get("scope", [])).split()
            ):
                raise ValueError("authorization protections are missing")
            return url
        except (AuthlibBaseError, httpx.HTTPError, KeyError, TypeError, ValueError, RuntimeError) as exc:
            raise OidcAuthenticationError("Organization sign-in could not be started.") from exc

    async def authenticate(
        self,
        *,
        code: str,
        nonce: str,
        code_verifier: str,
    ) -> Mapping[str, Any]:
        await self._validated_metadata()
        try:
            token = await self.remote.fetch_access_token(
                redirect_uri=self.settings.callback_url,
                grant_type="authorization_code",
                code=code,
                code_verifier=code_verifier,
            )
            if (
                not isinstance(token, Mapping)
                or not isinstance(token.get("id_token"), str)
                or not isinstance(token.get("access_token"), str)
            ):
                raise ValueError("OIDC token response is incomplete")
            claims = await self.remote.parse_id_token(
                token,
                nonce=nonce,
                claims_options={
                    "iss": {"essential": True, "values": [self.settings.issuer]},
                    "sub": {"essential": True},
                    "aud": {"essential": True, "values": [self.settings.client_id]},
                    "exp": {"essential": True},
                    "iat": {"essential": True},
                },
                leeway=60,
            )
            token_nonce = claims.get("nonce")
            if not isinstance(token_nonce, str) or not hmac.compare_digest(token_nonce, nonce):
                raise ValueError("OIDC nonce did not match")
            return dict(claims)
        except (
            AuthlibBaseError,
            JoseError,
            httpx.HTTPError,
            KeyError,
            TypeError,
            ValueError,
            RuntimeError,
        ) as exc:
            raise OidcAuthenticationError("Organization sign-in could not be verified.") from exc


@dataclass(frozen=True)
class AuthContext:
    principal: PrincipalRecord
    session: SessionRecord | None
    csrf_token: str
    auth_method: str
    application_roles: frozenset[str] = frozenset()

    @property
    def principal_id(self) -> str:
        return self.principal.principal_id

    @property
    def display_name(self) -> str:
        return self.principal.display_name

    @property
    def initials(self) -> str:
        parts = [part for part in self.display_name.split() if part]
        return "".join(part[0].upper() for part in parts[:2]) or "CI"

    @property
    def is_administrator(self) -> bool:
        return "administrator" in self.application_roles


@dataclass(frozen=True)
class OidcLoginStart:
    authorization_url: str
    state: str


def local_principal_enabled(settings: LocalAccountSettings | None, provider: str, subject: str) -> bool:
    """Fail closed on missing/disabled local identities, including worker startup."""
    if provider != LocalAccountSettings.provider_key:
        return True
    if settings is None:
        return False
    try:
        account = settings.accounts.get(subject)
        return account is not None and account.enabled
    except (OSError, RuntimeError, ValueError):
        return False


class IdentityService:
    """Owns provider mapping, organization sign-in, opaque sessions, and CSRF."""

    def __init__(
        self,
        store: WorkspaceStore,
        runtime_dir: Path,
        *,
        auth_mode: str | None = None,
        secure_cookie: bool | None = None,
        local_settings: LocalAccountSettings | None = None,
        oidc_settings: OidcSettings | None = None,
        oidc_client: OidcProviderClient | None = None,
        kerberos_settings: KerberosSettings | None = None,
        kerberos_profile_resolver: Callable[[str], tuple[str, str]] | None = None,
        kerberos_group_resolver: Callable[[str], Iterable[str]] | None = None,
        clock: Callable[[], datetime] | None = None,
        idle_timeout: timedelta = timedelta(minutes=30),
        absolute_timeout: timedelta = timedelta(hours=12),
    ) -> None:
        self.store = store
        self.runtime_dir = Path(runtime_dir)
        self.auth_mode = resolve_auth_mode(auth_mode)
        if secure_cookie is None:
            configured_cookie = os.getenv("CASE_INTELLIGENCE_SECURE_COOKIE", "").strip()
            if self.auth_mode in {"local", "oidc", "kerberos"} and configured_cookie != "1":
                raise RuntimeError(
                    "Authenticated access requires secure cookies "
                    "(CASE_INTELLIGENCE_SECURE_COOKIE=1)"
                )
            secure_cookie = configured_cookie == "1"
        self.secure_cookie = bool(secure_cookie)
        if self.auth_mode in {"local", "oidc", "kerberos"} and not self.secure_cookie:
            raise RuntimeError("Authenticated access requires secure cookies")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if idle_timeout <= timedelta(0) or absolute_timeout <= idle_timeout:
            raise ValueError("session timeouts are invalid")
        self.idle_timeout = idle_timeout
        self.absolute_timeout = absolute_timeout
        self._secret = self._load_or_create_secret()
        self._preview_by_subject = {identity.subject: identity for identity in PREVIEW_IDENTITIES}
        self.local_settings = local_settings
        self._local_failure_lock = threading.Lock()
        self._local_failures: dict[str, list[datetime]] = {}
        self.oidc_settings = oidc_settings
        self.oidc_client = oidc_client
        self.kerberos_settings = kerberos_settings
        self.kerberos_profile_resolver = (
            kerberos_profile_resolver or _system_identity_profile
        )
        self.kerberos_group_resolver = (
            kerberos_group_resolver or _system_group_memberships
        )
        if self.auth_mode == "local":
            self.local_settings = self.local_settings or LocalAccountSettings.from_env()
        elif self.local_settings is not None:
            raise RuntimeError("Local account configuration is only valid in local mode")
        if self.auth_mode == "oidc":
            self.oidc_settings = self.oidc_settings or OidcSettings.from_env()
            self.oidc_client = self.oidc_client or AuthlibOidcClient(self.oidc_settings)
        elif self.oidc_settings is not None or self.oidc_client is not None:
            raise RuntimeError("OIDC configuration is only valid in OIDC mode")
        if self.auth_mode == "kerberos":
            self.kerberos_settings = self.kerberos_settings or KerberosSettings.from_env()
        elif self.kerberos_settings is not None:
            raise RuntimeError("Kerberos configuration is only valid in Kerberos mode")
        if self.auth_mode in {"preview", "test"}:
            self._seed_preview_identities()
        self.store.principal_enabled = self.principal_enabled

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise RuntimeError("identity clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _load_or_create_secret(self) -> bytes:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        if self.runtime_dir.is_symlink():
            raise RuntimeError("identity runtime directory is unsafe")
        path = self.runtime_dir / "identity-session.key"
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise RuntimeError("identity session key path is unsafe")
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode != 0o600:
                raise RuntimeError("identity session key must have mode 0600")
            value = path.read_bytes()
            if len(value) != 32:
                raise RuntimeError("identity session key is invalid")
            return value
        value = secrets.token_bytes(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            return self._load_or_create_secret()
        try:
            os.write(descriptor, value)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise RuntimeError("identity session key permissions are invalid")
        return value

    def _seed_preview_identities(self) -> None:
        for identity in PREVIEW_IDENTITIES:
            self.store.upsert_principal(
                "preview",
                identity.subject,
                identity.display_name,
                identity.login_name,
                preferred_principal_id=identity.preferred_principal_id,
            )

    def auth_diagnostic_authorized(self, proof: str | None) -> bool:
        """A domain-separated operator proof; never a browser/session credential."""
        if proof is None or not re.fullmatch(r"[0-9a-f]{64}", proof):
            return False
        expected = hmac.new(
            self._secret, b"recordbench/auth-mode-diagnostic/v1", hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(proof, expected)

    def preview_identities(self) -> tuple[PrincipalRecord, ...]:
        if self.auth_mode not in {"preview", "test"}:
            return ()
        result = []
        for identity in PREVIEW_IDENTITIES:
            principal = self.store.upsert_principal(
                "preview",
                identity.subject,
                identity.display_name,
                identity.login_name,
                preferred_principal_id=identity.preferred_principal_id,
            )
            if principal.active:
                result.append(principal)
        return tuple(result)

    def principal_enabled(self, provider: str, subject: str) -> bool:
        """Live local eligibility; provider admission keeps its authentication lifetime."""
        return local_principal_enabled(self.local_settings, provider, subject)

    def membership_candidates(self, *, include_disabled: bool = False) -> tuple[PrincipalRecord, ...]:
        if self.auth_mode in {"preview", "test"}:
            provider = "preview"
        elif self.auth_mode == "local":
            provider = LocalAccountSettings.provider_key
        elif self.auth_mode == "oidc":
            if self.oidc_settings is None:
                return ()
            provider = self.oidc_settings.provider_key
        else:
            if self.kerberos_settings is None:
                return ()
            provider = self.kerberos_settings.provider_key
        return tuple(
            principal
            for principal in self.store.active_principals()
            if principal.provider == provider and (include_disabled or self.principal_enabled(provider, principal.provider_subject))
        )

    @staticmethod
    def token_digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _csrf(self, token: str) -> str:
        return hmac.new(
            self._secret,
            b"csrf:" + token.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _derive_oidc_value(self, domain: bytes, state: str) -> str:
        digest = hmac.new(
            self._secret,
            domain + state.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def new_login_challenge(self) -> str:
        nonce = secrets.token_urlsafe(32)
        signature = hmac.new(
            self._secret,
            b"login:" + nonce.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return f"{nonce}.{signature}"

    def valid_login_challenge(self, cookie_value: str | None, submitted: str | None) -> bool:
        if not cookie_value or not submitted or not hmac.compare_digest(cookie_value, submitted):
            return False
        try:
            nonce, signature = submitted.rsplit(".", 1)
            encoded_nonce = nonce.encode("ascii", "strict")
        except (ValueError, UnicodeEncodeError):
            return False
        expected = hmac.new(
            self._secret,
            b"login:" + encoded_nonce,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(signature, expected)

    def _issue_session(
        self,
        principal: PrincipalRecord,
        auth_method: str,
        application_roles: frozenset[str] = frozenset(),
        local_account: LocalAccount | None = None,
    ) -> tuple[AuthContext, str]:
        if not principal.active:
            raise WorkspaceProblem("This identity is not active.")
        raw_token = secrets.token_urlsafe(32)
        if auth_method == "local":
            if local_account is None:
                raise LocalAuthenticationError("Local account sign-in is unavailable.")
            raw_token = f"local.{self._local_session_binding(local_account)}.{raw_token}"
        now = self._now()
        session = self.store.create_session(
            principal.principal_id,
            self.token_digest(raw_token),
            auth_method,
            self._iso(now + self.idle_timeout),
            self._iso(now + self.absolute_timeout),
            application_roles,
        )
        return (
            AuthContext(
                principal,
                session,
                self._csrf(raw_token),
                auth_method,
                application_roles,
            ),
            raw_token,
        )

    def login_preview(self, provider_subject: str) -> tuple[AuthContext, str]:
        if self.auth_mode != "preview":
            raise WorkspaceProblem("Local preview sign-in is unavailable.")
        identity = self._preview_by_subject.get(provider_subject)
        if identity is None:
            raise WorkspaceProblem("Choose an available preview identity.")
        principal = self.store.upsert_principal(
            "preview",
            identity.subject,
            identity.display_name,
            identity.login_name,
            preferred_principal_id=identity.preferred_principal_id,
        )
        return self._issue_session(principal, "preview")

    def _local_attempt_key(self, username: str, client_key: str) -> str:
        material = (
            (username or "").strip().casefold()[:128]
            + "\0"
            + (client_key or "unknown")[:128]
        ).encode("utf-8", errors="replace")
        return hmac.new(self._secret, b"local-login:" + material, hashlib.sha256).hexdigest()

    def _local_attempt_allowed(self, key: str, now: datetime) -> bool:
        cutoff = now - timedelta(minutes=15)
        with self._local_failure_lock:
            failures = [value for value in self._local_failures.get(key, ()) if value >= cutoff]
            if failures:
                self._local_failures[key] = failures
            else:
                self._local_failures.pop(key, None)
            return len(failures) < 10

    def _record_local_failure(self, key: str, now: datetime) -> None:
        cutoff = now - timedelta(minutes=15)
        with self._local_failure_lock:
            failures = [value for value in self._local_failures.get(key, ()) if value >= cutoff]
            failures.append(now)
            self._local_failures[key] = failures[-10:]
            if len(self._local_failures) > 10_000:
                stale = [
                    value
                    for value, timestamps in self._local_failures.items()
                    if not timestamps or timestamps[-1] < cutoff
                ]
                for value in stale[:1_000]:
                    self._local_failures.pop(value, None)

    def _local_session_binding(self, account: LocalAccount) -> str:
        # A keyed digest exposes neither password hashes nor stored revisions in
        # the opaque cookie. The session table continues to store only its digest.
        material = json.dumps([account.username, account.session_revision,
                               account.password_hash, sorted(account.roles), account.enabled],
                              separators=(",", ":")).encode()
        return hmac.new(self._secret, b"local-account-session:" + material, hashlib.sha256).hexdigest()

    def local_account_edit_token(self, account: LocalAccount) -> str:
        material = json.dumps([account.username, account.display_name, account.session_revision,
                               account.password_hash, sorted(account.roles), account.enabled],
                              separators=(",", ":")).encode()
        return hmac.new(self._secret, b"local-account-edit:" + material, hashlib.sha256).hexdigest()

    def login_local(
        self,
        username: str,
        password: str,
        *,
        client_key: str = "",
    ) -> tuple[AuthContext, str]:
        if self.auth_mode != "local" or self.local_settings is None:
            raise LocalAuthenticationError("Local account sign-in is unavailable.")
        if (
            not isinstance(username, str)
            or not isinstance(password, str)
            or len(username) > 128
            or not 1 <= len(password) <= 1_024
        ):
            raise LocalAuthenticationError("The username or password is incorrect.")
        now = self._now()
        attempt_key = self._local_attempt_key(username, client_key)
        if not self._local_attempt_allowed(attempt_key, now):
            raise LocalAuthenticationError(
                "Sign-in is temporarily unavailable. Wait a few minutes and try again."
            )
        try:
            account = self.local_settings.authenticate(username, password)
        except (OSError, RuntimeError):
            raise LocalAuthenticationError("Local account sign-in is temporarily unavailable.") from None
        if account is None:
            self._record_local_failure(attempt_key, now)
            raise LocalAuthenticationError("The username or password is incorrect.")
        with self._local_failure_lock:
            self._local_failures.pop(attempt_key, None)
        principal = self.store.upsert_principal(
            self.local_settings.provider_key,
            account.username,
            account.display_name,
            account.username,
        )
        return self._issue_session(principal, "local", account.roles, local_account=account)

    def _resolved_kerberos_groups(self, principal: str) -> frozenset[str]:
        try:
            raw_groups = self.kerberos_group_resolver(principal)
            if isinstance(raw_groups, (str, bytes)):
                raise TypeError("group resolver returned text")
            groups: set[str] = set()
            for index, value in enumerate(raw_groups):
                if index >= 2048:
                    raise ValueError("group resolver returned too many groups")
                if not isinstance(value, str):
                    raise TypeError("group resolver returned a non-text group")
                if value.strip():
                    groups.add(_normalized_kerberos_group(value))
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise KerberosAuthenticationError(
                "Your Windows group membership could not be verified."
            ) from exc
        return frozenset(groups)

    def _verified_kerberos_authorization(
        self,
        authenticated_user: str | None,
        proxy_secret: str | None,
    ) -> tuple[str, frozenset[str]]:
        if self.auth_mode != "kerberos" or self.kerberos_settings is None:
            raise KerberosAuthenticationError("Windows sign-in is unavailable.")
        expected_secret = self.kerberos_settings.proxy_secret
        if (
            not authenticated_user
            or not proxy_secret
            or len(proxy_secret) > 512
            or not hmac.compare_digest(proxy_secret, expected_secret)
        ):
            raise KerberosAuthenticationError("Windows sign-in could not be verified.")
        principal = self.kerberos_settings.normalize_principal(authenticated_user)
        exact_administrator = (
            principal in self.kerberos_settings.administrator_principals
        )
        exact_admission = (
            principal in self.kerberos_settings.allowed_principals
            or exact_administrator
        )
        configured_groups = bool(
            self.kerberos_settings.allowed_groups
            or self.kerberos_settings.administrator_groups
        )
        groups: frozenset[str] = frozenset()
        if configured_groups:
            try:
                groups = self._resolved_kerberos_groups(principal)
            except KerberosAuthenticationError:
                if not exact_admission:
                    raise
        group_administrator = not self.kerberos_settings.administrator_groups.isdisjoint(
            groups
        )
        group_admission = group_administrator or not self.kerberos_settings.allowed_groups.isdisjoint(
            groups
        )
        if not (exact_admission or group_admission):
            raise KerberosAuthenticationError(
                f"This account is not approved for {PRODUCT_NAME}."
            )
        roles = (
            frozenset({"administrator"})
            if exact_administrator or group_administrator
            else frozenset()
        )
        return principal, roles

    def _verified_kerberos_principal(
        self,
        authenticated_user: str | None,
        proxy_secret: str | None,
    ) -> str:
        return self._verified_kerberos_authorization(
            authenticated_user,
            proxy_secret,
        )[0]

    def login_kerberos(
        self,
        authenticated_user: str | None,
        proxy_secret: str | None,
    ) -> tuple[AuthContext, str]:
        principal_subject, application_roles = self._verified_kerberos_authorization(
            authenticated_user,
            proxy_secret,
        )
        if self.kerberos_settings is None:
            raise KerberosAuthenticationError("Windows sign-in is unavailable.")
        try:
            display_name, login_name = self.kerberos_profile_resolver(principal_subject)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise KerberosAuthenticationError(
                "Your Windows account profile could not be loaded."
            ) from exc
        display_name = self._claim_text(display_name, maximum=160)
        login_name = self._claim_text(login_name, maximum=255)
        if not display_name or not login_name:
            raise KerberosAuthenticationError(
                "Your Windows account profile could not be loaded."
            )
        principal = self.store.upsert_principal(
            self.kerberos_settings.provider_key,
            principal_subject,
            display_name,
            login_name,
        )
        return self._issue_session(principal, "spnego", application_roles)

    def bind_kerberos_request(
        self,
        context: AuthContext,
        authenticated_user: str | None,
        proxy_secret: str | None,
    ) -> AuthContext | None:
        if self.auth_mode != "kerberos" or self.kerberos_settings is None:
            return context
        try:
            principal_subject, application_roles = self._verified_kerberos_authorization(
                authenticated_user,
                proxy_secret,
            )
        except KerberosAuthenticationError:
            return None
        if not (
            context.auth_method == "spnego"
            and context.principal.provider == self.kerberos_settings.provider_key
            and hmac.compare_digest(
                context.principal.provider_subject,
                principal_subject,
            )
        ):
            return None
        return AuthContext(
            context.principal,
            context.session,
            context.csrf_token,
            context.auth_method,
            application_roles,
        )

    def kerberos_request_valid(
        self,
        context: AuthContext,
        authenticated_user: str | None,
        proxy_secret: str | None,
    ) -> bool:
        return self.bind_kerberos_request(
            context,
            authenticated_user,
            proxy_secret,
        ) is not None

    def kerberos_session_identity_matches(
        self,
        context: AuthContext,
        authenticated_user: str | None,
        proxy_secret: str | None,
    ) -> bool:
        """Return whether a trusted proxy still presents this session's user."""

        if self.auth_mode != "kerberos" or self.kerberos_settings is None:
            return False
        if (
            not authenticated_user
            or not proxy_secret
            or len(proxy_secret) > 512
            or not hmac.compare_digest(
                proxy_secret,
                self.kerberos_settings.proxy_secret,
            )
        ):
            return False
        try:
            principal_subject = self.kerberos_settings.normalize_principal(
                authenticated_user
            )
        except KerberosAuthenticationError:
            return False
        return bool(
            context.auth_method == "spnego"
            and context.principal.provider == self.kerberos_settings.provider_key
            and hmac.compare_digest(
                context.principal.provider_subject,
                principal_subject,
            )
        )

    async def begin_oidc_login(self, next_path: str | None) -> OidcLoginStart:
        if self.auth_mode != "oidc" or self.oidc_client is None:
            raise OidcAuthenticationError("Organization sign-in is unavailable.")
        destination = self.safe_next(next_path)
        state = secrets.token_urlsafe(32)
        verifier = self._derive_oidc_value(b"pkce:", state)
        nonce = self._derive_oidc_value(b"nonce:", state)
        authorization_url = await self.oidc_client.authorization_url(
            state=state,
            nonce=nonce,
            code_verifier=verifier,
        )
        self.store.create_oidc_transaction(
            self.token_digest(state),
            destination,
            self._iso(self._now() + timedelta(minutes=5)),
        )
        return OidcLoginStart(authorization_url, state)

    @staticmethod
    def _claim_text(value: Any, *, maximum: int) -> str:
        if not isinstance(value, str):
            return ""
        normalized = value.strip()
        if not normalized or len(normalized) > maximum:
            return ""
        if any(ord(character) < 32 for character in normalized):
            return ""
        return normalized

    async def finish_oidc_login(
        self,
        *,
        cookie_state: str | None,
        returned_state: str | None,
        code: str | None,
        provider_error: str | None = None,
    ) -> tuple[AuthContext, str, str]:
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
        return context, raw_token, destination

    def resolve(self, raw_token: str | None, *, read_only: bool = False) -> AuthContext | None:
        if self.auth_mode == "test":
            principal = self.store.get_principal("development-taylor-morgan")
            return AuthContext(principal, None, "test-csrf", "test")
        if not raw_token or len(raw_token) > 256:
            return None
        now = self._now()
        resolved = self.store.resolve_session(
            self.token_digest(raw_token),
            now=self._iso(now),
            next_idle_expires_at=self._iso(now + self.idle_timeout),
            read_only=read_only,
        )
        if resolved is None:
            return None
        session, principal = resolved
        try:
            raw_roles = json.loads(session.application_roles)
        except json.JSONDecodeError:
            if not read_only:
                self.store.revoke_session(session.session_id)
            return None
        if (
            not isinstance(raw_roles, list)
            or any(value != "administrator" for value in raw_roles)
            or len(raw_roles) != len(set(raw_roles))
        ):
            if not read_only:
                self.store.revoke_session(session.session_id)
            return None
        roles = frozenset(raw_roles)
        if self.auth_mode == "local":
            if (
                self.local_settings is None
                or principal.provider != self.local_settings.provider_key
            ):
                return None
            try:
                account = self.local_settings.account(principal.provider_subject)
            except (OSError, RuntimeError):
                account = None
            binding = raw_token.split(".", 2)
            if (account is None or not account.enabled or session.auth_method != "local"
                    or len(binding) != 3 or binding[0] != "local"
                    or not hmac.compare_digest(binding[1], self._local_session_binding(account))):
                if not read_only:
                    self.store.revoke_session(session.session_id)
                return None
            roles = account.roles
            if not read_only and principal.display_name != account.display_name:
                self.store.refresh_principal_display_name(
                    self.local_settings.provider_key, account.username, account.display_name,
                    expected_display_name=principal.display_name,
                )
                # An account rename may have refreshed the projection since this
                # request read it. Keep that newer name rather than overwriting it.
                principal = self.store.get_principal(principal.principal_id)
        return AuthContext(
            principal,
            session,
            self._csrf(raw_token),
            session.auth_method,
            roles,
        )

    def csrf_valid(self, context: AuthContext, submitted: str | None) -> bool:
        if context.auth_method == "test":
            return True
        return bool(submitted) and hmac.compare_digest(context.csrf_token, submitted)

    def logout(self, context: AuthContext) -> bool:
        if context.session is None:
            return False
        return self.store.revoke_session(context.session.session_id)

    @property
    def session_cookie_options(self) -> dict[str, object]:
        return {
            "httponly": True,
            "secure": self.secure_cookie,
            "samesite": "lax",
            "path": "/",
            "max_age": int(self.absolute_timeout.total_seconds()),
        }

    @property
    def login_cookie_options(self) -> dict[str, object]:
        return {
            "httponly": True,
            "secure": self.secure_cookie,
            "samesite": "lax",
            "path": "/auth",
            "max_age": 300,
        }

    @property
    def oidc_state_cookie_options(self) -> dict[str, object]:
        return {
            "httponly": True,
            "secure": True,
            "samesite": "lax",
            "path": "/auth/oidc",
            "max_age": 300,
        }

    @staticmethod
    def safe_next(value: str | None, *, default: str = "/") -> str:
        candidate = value or default
        if len(candidate) > 2_048 or not candidate.startswith("/") or candidate.startswith("//"):
            return default
        if "\\" in candidate or any(ord(character) < 32 for character in candidate):
            return default
        parsed = urlsplit(candidate)
        if parsed.scheme or parsed.netloc:
            return default
        return candidate
