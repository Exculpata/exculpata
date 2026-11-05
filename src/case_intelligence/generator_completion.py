"""Local schema checks for an answer server that does not enforce JSON schema."""
from __future__ import annotations

import json
import os
import re
import time
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Callable, Mapping

from .generator_schema import (
    PROBE_ATTEMPTS,
    SchemaProbe,
    probe_schema_enforcement,
    schema_error,
)


def generator_api_key() -> str:
    value = os.getenv("CASE_INTELLIGENCE_GENERATOR_API_KEY", "")
    if value:
        return value
    path = os.getenv("CASE_INTELLIGENCE_GENERATOR_API_KEY_FILE", "").strip()
    if not path:
        return ""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError("generator API key file is unreadable") from exc
    return text.strip()


def initialize_schema(client: object, mode: str) -> None:
    client._schema_results = {
        (client.model, True): (SchemaProbe(mode, 0, "", ""), 0.0),
    }
    client._schema_refreshing = set()
    client._schema_checks_started = False


def _show_answer_schema(client: object, result: SchemaProbe) -> None:
    client.schema_mode = result.status
    client.schema_detail = result.detail
    client.schema_fix = result.fix


def remember_schema(
    client: object, result: SchemaProbe, *, thinking: bool = True, model: str | None = None,
) -> None:
    model = client.model if model is None else model
    with _REFRESH_LOCK:
        client._schema_checks_started = True
        client._schema_results[(model, thinking)] = (result, time.monotonic())
        if thinking and model == client.model:
            _show_answer_schema(client, result)


def probe_generator(
    client: object, *, thinking: bool = True, model: str | None = None,
) -> None:
    if os.getenv("CASE_INTELLIGENCE_GENERATOR_SCHEMA_PROBE", "1") == "0":
        return
    model = client.model if model is None else model
    try:
        result = probe_schema_enforcement(
            client.endpoint,
            client.schema_server,
            model,
            thinking=thinking,
            timeout=min(float(client.timeout), 20.0),
            headers=getattr(client, "_headers", {}),
        )
    except (OSError, ValueError, TimeoutError):
        result = SchemaProbe(
            "unreachable",
            0,
            "The schema check could not run.",
            "Rerun doctor after the answer server is reachable.",
        )
    remember_schema(client, result, thinking=thinking, model=model)


def refresh_generator_schema(
    client: object, *, thinking: bool = True, now: float | None = None,
) -> None:
    """Refresh each model/state once a minute without blocking callers."""
    if not getattr(client, "_schema_checks_started", False) and getattr(client, "schema_mode", "unprobed") == "unprobed":
        return
    if os.getenv("CASE_INTELLIGENCE_GENERATOR_SCHEMA_PROBE", "1") == "0":
        return
    moment = time.monotonic() if now is None else now
    model = client.model
    key = (model, thinking)
    with _REFRESH_LOCK:
        cached = client._schema_results.get(key)
        if thinking and model == client.model:
            _show_answer_schema(client, cached[0] if cached else SchemaProbe("unprobed", 0, "", ""))
        if key in client._schema_refreshing or (cached and moment - cached[1] < 60):
            return
        client._schema_checks_started = True
        client._schema_refreshing.add(key)

    def refresh():
        try:
            probe_generator(client, thinking=thinking, model=model)
        finally:
            with _REFRESH_LOCK:
                client._schema_refreshing.discard(key)

    threading.Thread(target=refresh, daemon=True, name="generator-schema").start()


_REFRESH_LOCK = threading.Lock()


def schema_refused(client: object, *, thinking: bool = True) -> None:
    from .generation import GenerationUnavailable

    with _REFRESH_LOCK:
        cached = getattr(client, "_schema_results", {}).get((client.model, thinking))
    if cached is None or cached[0].status != "refused":
        return
    raise GenerationUnavailable(
        "This answer service did not return the required JSON schema. "
        "Answering stays off for this thinking state until a schema check passes. "
        "Search and source review still work."
    )


def generator_available(client: object, *, thinking: bool, use_client_availability: bool = False) -> bool:
    from .generation import (
        GenerationUnavailable, OllamaGenerator, OpenAICompatibleGenerator, _bounded_json_get,
    )

    if not isinstance(client, (OllamaGenerator, OpenAICompatibleGenerator)):
        return client.available
    try:
        schema_refused(client, thinking=thinking)
    except GenerationUnavailable:
        return False
    adapter = OllamaGenerator if isinstance(client, OllamaGenerator) else OpenAICompatibleGenerator
    if use_client_availability and type(client).available is not adapter.available:
        return client.available
    path, collection, identifier = (
        ("/api/tags", "models", "name") if isinstance(client, OllamaGenerator)
        else ("/v1/models", "data", "id")
    )
    payload = _bounded_json_get(
        f"{client.endpoint}{path}", timeout=2.0, headers=client._headers,
        **({"opener": client._opener} if client._opener is not None else {}),
    )
    return any(isinstance(item, dict) and item.get(identifier) == client.model
               for item in payload.get(collection, []))


_SINGLE_SCHEMA_ATTEMPT = ContextVar("single_schema_attempt", default=False)
_REQUEST_OBSERVER = ContextVar("generation_request_observer", default=None)


def observe_request(body: dict) -> dict:
    observer = _REQUEST_OBSERVER.get()
    if observer is not None:
        observer(body)
    return body


@contextmanager
def single_schema_attempt(request_observer=None):
    """Capture the first synthetic evaluation response without concealing repairs."""
    token = _SINGLE_SCHEMA_ATTEMPT.set(True)
    observer_token = _REQUEST_OBSERVER.set(request_observer)
    try:
        yield
    finally:
        _REQUEST_OBSERVER.reset(observer_token)
        _SINGLE_SCHEMA_ATTEMPT.reset(token)


def complete_structured(
    client: object,
    send: Callable[[int], Mapping[str, object]],
    parse: Callable[[Mapping[str, object]], Mapping[str, object]],
    schema: Mapping[str, object],
    *,
    thinking: bool = True,
) -> Mapping[str, object]:
    """Validate every reply locally, with bounded repair attempts for probed servers."""

    from .generation import GenerationUnavailable, GenerationSchemaRejected

    if getattr(client, "_schema_checks_started", False):
        refresh_generator_schema(client, thinking=thinking)
    schema_refused(client, thinking=thinking)
    attempts = 1 if _SINGLE_SCHEMA_ATTEMPT.get() else PROBE_ATTEMPTS
    for attempt in range(attempts):
        boundary = getattr(client, "structured_request_boundary", None)
        response = boundary(lambda: send(attempt)) if callable(boundary) else send(attempt)
        try:
            payload = parse(response)
        except GenerationUnavailable:
            if attempt + 1 == attempts:
                raise
            continue
        if schema_error(payload, schema) is None:
            return payload
        if attempt + 1 == attempts:
            raise GenerationSchemaRejected(payload)
    raise GenerationUnavailable("The answer service did not return the required JSON schema.")


def repair_message(messages: list[dict[str, str]], attempt: int) -> list[dict[str, str]]:
    if attempt <= 0:
        return messages
    return [
        *messages,
        {
            "role": "user",
            "content": (
                "The previous reply did not match the JSON schema. "
                "Reply again with only a valid object."
            ),
        },
    ]


def openai_message(response: Mapping[str, object]) -> Mapping[str, object]:
    from .generation import GenerationUnavailable

    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise GenerationUnavailable("The answer service returned an invalid response.")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise GenerationUnavailable("The answer service returned an invalid response.")
    return parse_model_content(message.get("content"))


def parse_model_content(content: object) -> Mapping[str, object]:
    from .generation import GenerationUnavailable, GenerationInvalidContent, MAX_RESPONSE_BYTES

    if not isinstance(content, str) or len(content) > MAX_RESPONSE_BYTES:
        raise GenerationUnavailable("The answer service returned invalid structured content.")
    value = content.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise GenerationInvalidContent("The answer service did not return structured content.") from exc
    if not isinstance(payload, dict):
        raise GenerationInvalidContent("The answer service returned invalid structured content.")
    return payload


def request_thinking(client: object, thinking: bool | None, *, default: bool) -> bool:
    if thinking is not None:
        return thinking
    explicit = client._explicit_thinking
    return default if explicit is None else explicit


def effective_thinking(client: object) -> bool:
    return request_thinking(client, None, default=True)


def expected_request_settings(thinking: bool) -> dict[str, bool | int]:
    from .generation import DEFAULT_REVIEW_BUDGET

    return {"thinking": thinking, "output_tokens": DEFAULT_REVIEW_BUDGET.output_tokens,
            "reasoning_tokens": DEFAULT_REVIEW_BUDGET.reasoning_tokens if thinking else 0}
