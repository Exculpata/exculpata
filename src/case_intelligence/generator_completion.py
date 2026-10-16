"""Local schema checks for an answer server that does not enforce JSON schema."""
from __future__ import annotations

import os
import time
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


def remember_schema(client: object, result: SchemaProbe) -> None:
    client.schema_mode = result.status  # type: ignore[attr-defined]
    client.schema_detail = result.detail  # type: ignore[attr-defined]
    client.schema_fix = result.fix  # type: ignore[attr-defined]
    client._schema_checked_at = time.monotonic()  # type: ignore[attr-defined]


def probe_generator(client: object) -> None:
    if os.getenv("CASE_INTELLIGENCE_GENERATOR_SCHEMA_PROBE", "1") == "0":
        return
    try:
        result = probe_schema_enforcement(
            client.endpoint,  # type: ignore[attr-defined]
            client.schema_server,  # type: ignore[attr-defined]
            client.model,  # type: ignore[attr-defined]
            timeout=min(float(client.timeout), 20.0),  # type: ignore[attr-defined]
            headers=getattr(client, "_headers", {}),
        )
    except (OSError, ValueError, TimeoutError):
        result = SchemaProbe(
            "unreachable",
            0,
            "The schema check could not run.",
            "Rerun doctor after the answer server is reachable.",
        )
    remember_schema(client, result)


def refresh_generator_schema(client: object, *, now: float | None = None) -> None:
    """Re-check a probed server at most once a minute. Unprobed clients do not call out."""

    if getattr(client, "schema_mode", "unprobed") == "unprobed":
        return
    moment = time.monotonic() if now is None else now
    if moment - float(getattr(client, "_schema_checked_at", 0.0)) < 60:
        return
    probe_generator(client)


def schema_refused(client: object) -> None:
    from .generation import GenerationUnavailable

    if getattr(client, "schema_mode", "unprobed") != "refused":
        return
    raise GenerationUnavailable(
        "This answer service did not return the required JSON schema. "
        "Answering stays off until a schema check passes. Search and source review still work."
    )


def complete_structured(
    client: object,
    send: Callable[[int], Mapping[str, object]],
    parse: Callable[[Mapping[str, object]], Mapping[str, object]],
    schema: Mapping[str, object],
) -> Mapping[str, object]:
    """Trust a verified server. Otherwise validate locally and retry, then refuse."""

    from .generation import GenerationUnavailable

    schema_refused(client)
    checking = getattr(client, "schema_mode", "unprobed") in {"validate_and_retry", "unreachable"}
    attempts = PROBE_ATTEMPTS if checking else 1
    last = "The answer service did not return the required JSON schema."
    for attempt in range(attempts):
        response = send(attempt)
        try:
            payload = parse(response)
        except GenerationUnavailable as exc:
            last = str(exc)
            if not checking or attempt + 1 == attempts:
                raise
            continue
        if not checking or schema_error(payload, schema) is None:
            return payload
        last = "The reply did not match the JSON schema."
    raise GenerationUnavailable(last + " Answering is unavailable for this request.")


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
