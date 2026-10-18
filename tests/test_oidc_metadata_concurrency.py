"""Interleave real Authlib discovery loads without opening network sockets."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from case_intelligence.identity import AuthlibOidcClient, OidcAuthenticationError, OidcSettings


def configured_client(monkeypatch):
    monkeypatch.delenv("CASE_INTELLIGENCE_OIDC_PROXY", raising=False)
    settings = OidcSettings(issuer="https://issuer.example.test", external_origin="https://app.example.test",
                            client_id="synthetic", client_secret="synthetic-secret")
    metadata = {"issuer": settings.issuer, "authorization_endpoint": settings.issuer + "/authorize",
                "token_endpoint": settings.issuer + "/token", "jwks_uri": settings.issuer + "/jwks",
                "code_challenge_methods_supported": ["S256"], "id_token_signing_alg_values_supported": ["RS256"],
                "verify": False, "follow_redirects": True, "trust_env": True,
                "proxy": "http://foreign.example.test:3128"}
    return AuthlibOidcClient(settings), metadata


def test_interleaved_initial_discovery_cannot_restore_transport_overrides(monkeypatch):
    async def exercise():
        client, metadata = configured_client(monkeypatch)
        first_started, first_release, second_scheduled, second_release = [asyncio.Event() for _ in range(4)]
        calls = []
        async def handler(request):
            calls.append(request.url.path)
            if len(calls) == 1:
                first_started.set()
                await first_release.wait()
            else:
                await second_release.wait()
            return httpx.Response(200, json=metadata)
        client.remote.client_kwargs["transport"] = httpx.MockTransport(handler)
        first = asyncio.create_task(client._validated_metadata())
        await first_started.wait()
        async def second_load():
            second_scheduled.set()
            return await client._validated_metadata()
        second = asyncio.create_task(second_load())
        await second_scheduled.wait()
        first_release.set()
        await first  # The delayed response arrives only after the first sanitization.
        second_release.set()
        await second
        options = {}
        def capture(**kwargs):
            options.update(kwargs)
            return SimpleNamespace(headers={})  # Observe the real merge without opening sockets.
        client.remote.client_cls = capture
        client.remote._get_oauth_client(**client.remote.server_metadata)
        assert options["verify"] is True
        assert options["follow_redirects"] is False
        assert options["trust_env"] is False and options["proxy"] is None
        assert len(calls) == 1
        assert not {"verify", "follow_redirects", "trust_env", "proxy"} & client.remote.server_metadata.keys()
    asyncio.run(asyncio.wait_for(exercise(), timeout=5))


@pytest.mark.parametrize("cancel", [False, True])
def test_failed_or_cancelled_initial_discovery_releases_waiters(monkeypatch, cancel):
    async def exercise():
        client, metadata = configured_client(monkeypatch)
        started, never = asyncio.Event(), asyncio.Event()
        calls = []
        async def handler(request):
            calls.append(request.url.path)
            if len(calls) == 1:
                started.set()
                if cancel:
                    await never.wait()
                return httpx.Response(503)
            return httpx.Response(200, json=metadata)
        client.remote.client_kwargs["transport"] = httpx.MockTransport(handler)
        first = asyncio.create_task(client._validated_metadata())
        await started.wait()
        second = asyncio.create_task(client._validated_metadata())
        await asyncio.sleep(0)
        if cancel:
            first.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else OidcAuthenticationError):
            await first
        clean = await second
        assert clean["issuer"] == client.settings.issuer
        assert not {"verify", "follow_redirects", "trust_env", "proxy"} & clean.keys()
        await client._validated_metadata()
        assert len(calls) == 2
    asyncio.run(asyncio.wait_for(exercise(), timeout=5))


@pytest.mark.parametrize("key", ["authorization_endpoint", "token_endpoint", "jwks_uri"])
def test_unicode_casefold_does_not_authorize_a_different_wire_host(monkeypatch, key):
    async def exercise():
        client, metadata = configured_client(monkeypatch)
        settings = OidcSettings(issuer="https://faß.example.test", external_origin="https://app.example.test",
                                client_id="synthetic", client_secret="synthetic-secret")
        client = AuthlibOidcClient(settings)
        metadata.update(issuer=settings.issuer, authorization_endpoint=settings.issuer + "/authorize",
                        token_endpoint=settings.issuer + "/token", jwks_uri=settings.issuer + "/jwks")
        metadata[key] = metadata[key].replace("faß", "fass")
        client.remote.client_kwargs["transport"] = httpx.MockTransport(lambda request: httpx.Response(200, json=metadata))
        with pytest.raises(OidcAuthenticationError):
            await client._validated_metadata()
    asyncio.run(exercise())


def test_unicode_and_ascii_idna_endpoints_share_the_exact_wire_origin(monkeypatch):
    async def exercise():
        _, metadata = configured_client(monkeypatch)
        settings = OidcSettings(issuer="https://faß.example.test", external_origin="https://app.example.test",
                                client_id="synthetic", client_secret="synthetic-secret")
        client = AuthlibOidcClient(settings)
        wire_origin = "https://" + httpx.URL(settings.issuer).raw_host.decode("ascii")
        metadata.update(issuer=settings.issuer, authorization_endpoint=wire_origin + "/authorize",
                        token_endpoint=wire_origin + "/token", jwks_uri=wire_origin + "/jwks")
        client.remote.client_kwargs["transport"] = httpx.MockTransport(lambda request: httpx.Response(200, json=metadata))
        assert (await client._validated_metadata())["issuer"] == settings.issuer
    asyncio.run(exercise())
