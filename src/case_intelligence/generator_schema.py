"""Strict JSON-schema probe for operator answer servers.

Ollama receives the schema as the native ``format`` field on ``/api/chat``.
llama.cpp, LM Studio, and vLLM receive the nested ``response_format.json_schema.schema``
form. The top-level ``response_format.schema`` key is not sent: llama.cpp
ignores that shape and still returns HTTP 200.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Mapping

from jsonschema import Draft202012Validator


PROBE_NAME = "exculpata_schema_probe"
PROBE_MARKER = "exculpata-schema-ok"
PROBE_ATTEMPTS = 3
SCHEMA_SERVERS = ("ollama", "llamacpp", "lmstudio", "vllm")
OPENAI_SERVERS = ("llamacpp", "lmstudio", "vllm")

PROBE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["marker"],
    "properties": {"marker": {"type": "string", "const": PROBE_MARKER}},
}

_PROMPT = (
    "Return only a JSON object with one field, marker, set to exculpata-schema-ok."
)


class SchemaTransportError(RuntimeError):
    """The schema check did not receive an HTTP response body."""


@dataclass(frozen=True)
class SchemaProbe:
    status: str
    attempts: int
    detail: str
    fix: str

    @property
    def label(self) -> str:
        return {
            "verified": "verified",
            "validate_and_retry": "not enforced",
            "refused": "refused",
            "unreachable": "not checked",
        }.get(self.status, "not checked")


def openai_response_format(name: str, schema: Mapping[str, object]) -> dict[str, object]:
    """Nested schema object used by llama.cpp and by OpenAI-compatible servers."""

    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": dict(schema),
        },
    }


def schema_chat_request(
    server: str,
    *,
    model: str,
    schema: Mapping[str, object],
    prompt: str,
    name: str,
    max_tokens: int = 64,
) -> tuple[str, dict[str, object]]:
    """Return the path and body for one strict-schema chat request."""

    kind = _server(server)
    if kind == "ollama":
        return "/api/chat", {
            "model": model,
            "stream": False,
            "format": dict(schema),
            "messages": [{"role": "user", "content": prompt}],
            "options": {"temperature": 0, "num_predict": max_tokens},
        }
    return "/v1/chat/completions", {
        "model": model,
        "temperature": 0,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": openai_response_format(name, schema),
    }


def schema_error(instance: object, schema: Mapping[str, object]) -> str | None:
    validator = Draft202012Validator(dict(schema))
    if next(validator.iter_errors(instance), None) is None:
        return None
    return "The reply did not match the JSON schema."


def probe_schema_enforcement(
    endpoint: str,
    server: str,
    model: str,
    *,
    transport: Callable[[str, Mapping[str, object], Mapping[str, str]], Mapping[str, object]] | None = None,
    timeout: float = 15.0,
    attempts: int = PROBE_ATTEMPTS,
    headers: Mapping[str, str] | None = None,
) -> SchemaProbe:
    """Send a tiny schema request. HTTP 200 is not treated as enforcement."""

    kind = _server(server)
    origin = endpoint.rstrip("/")
    path, _ = schema_chat_request(kind, model=model, schema=PROBE_SCHEMA, prompt=_PROMPT, name=PROBE_NAME)
    sender = transport or _urllib_transport(timeout)
    header_map = dict(headers or {})
    saw_invalid = False
    sent = 0
    prompt = _PROMPT
    for _attempt in range(max(1, attempts)):
        sent += 1
        path, body = schema_chat_request(
            kind, model=model, schema=PROBE_SCHEMA, prompt=prompt, name=PROBE_NAME,
        )
        try:
            payload = sender(origin + path, body, header_map)
        except SchemaTransportError:
            if saw_invalid:
                continue
            return SchemaProbe(
                "unreachable",
                sent,
                "The schema check could not reach the answer server.",
                "Confirm the server is running and that this host can open the configured URL. "
                "A container name such as generator resolves only inside the application network.",
            )
        if _matches_probe(payload, kind):
            if sent == 1:
                return SchemaProbe(
                    "verified",
                    sent,
                    "The server returned the required JSON object on the first check.",
                    "",
                )
            return SchemaProbe(
                "validate_and_retry",
                sent,
                "The server accepted the request but did not enforce the schema. "
                "Later replies are checked locally and retried.",
                "Prefer a server build that enforces the nested JSON schema. "
                "Answers still run, with local checks.",
            )
        saw_invalid = True
        prompt = (
            "The previous reply did not match the JSON schema. "
            "Return only a JSON object with one field, marker, set to exculpata-schema-ok."
        )
    return SchemaProbe(
        "refused",
        sent,
        "The server did not return the required JSON object after retries. Answering stays off.",
        "Use a server that enforces JSON schema, or a model that returns the requested object. "
        "Search and source review still work.",
    )


def _matches_probe(payload: Mapping[str, object], server: str) -> bool:
    content = _message_content(payload, server)
    if isinstance(content, str):
        text = content.strip()
        if text.startswith("```"):
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            content = json.loads(text)
        except json.JSONDecodeError:
            return False
    return schema_error(content, PROBE_SCHEMA) is None


def _message_content(payload: Mapping[str, object], server: str) -> object:
    if server == "ollama":
        message = payload.get("message")
        if isinstance(message, dict):
            return message.get("content")
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None
    return message.get("content")


def _server(value: str) -> str:
    server = value.strip().casefold()
    if server not in SCHEMA_SERVERS:
        raise ValueError("unsupported answer server")
    return server


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


def _urllib_transport(
    timeout: float,
) -> Callable[[str, Mapping[str, object], Mapping[str, str]], Mapping[str, object]]:
    opener = urllib.request.build_opener(_NoRedirect)

    def send(
        url: str,
        payload: Mapping[str, object],
        headers: Mapping[str, str],
    ) -> Mapping[str, object]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        request_headers = {"Content-Type": "application/json", "Accept": "application/json"}
        request_headers.update(dict(headers))
        request = urllib.request.Request(url, data=body, headers=request_headers, method="POST")
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read(65_536)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise SchemaTransportError("unreachable") from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SchemaTransportError("unreadable") from exc
        if not isinstance(value, dict):
            raise SchemaTransportError("unreadable")
        return value

    return send
