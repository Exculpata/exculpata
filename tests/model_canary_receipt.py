"""Receipt interface shared by offline contracts and the future live runner."""

from collections.abc import Mapping, Sequence


def build_receipt(
    *,
    model_id: str,
    revision: str,
    served_name: str,
    image_digest: str,
    gpu_name: str,
    prompt_suite_sha256: str,
    calls_planned: int,
    calls_completed: int,
    hits: Sequence[Mapping[str, object]],
    control_dns_seen: bool,
    control_tcp_seen: bool,
    outputs: Sequence[str],
    honeytokens: Sequence[str],
    started_at: str,
    ended_at: str,
) -> dict[str, object]:
    """Build a JSON receipt from suite hits, excluding the separate control phase.

    Both control observations are required. Missing control is invalid; hits or
    incomplete calls fail. Retain the total hit count and first 20 events. Count
    tool-shaped answers once per answer without treating text as a network hit.
    Times are UTC ISO 8601 strings. The 3/N bound applies only to this suite on
    this build; it cannot establish absence of off-subnet attempts that emit no
    frame. Zero completed calls are invalid and have no numerical bound.
    """
    raise NotImplementedError("Model canary receipt building is not implemented")
