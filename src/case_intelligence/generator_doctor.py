"""Run the content-free schema diagnostic inside the existing app namespace."""
from dataclasses import asdict
import json
import math
import os

from .generation import DEFAULT_TIMEOUT_SECONDS
from .generator_completion import generator_api_key
from .generator_config import validate_generator_environment
from .generator_schema import SchemaProbe, probe_schema_enforcement
from .oidc_network_policy import GENERATOR_RELAY_LIMITS
from .service_endpoints import validate_service_endpoint


def probe_configured_generator():
    try:
        timeout = float(os.getenv("CASE_INTELLIGENCE_GENERATOR_TIMEOUT", str(DEFAULT_TIMEOUT_SECONDS)))
        if not math.isfinite(timeout) or not 0 < timeout <= GENERATOR_RELAY_LIMITS.seconds:
            raise ValueError()
    except ValueError:
        return SchemaProbe(
            "unreachable", 0,
            "CASE_INTELLIGENCE_GENERATOR_TIMEOUT must be a positive finite number no greater "
            f"than the relay generator tunnel limit ({GENERATOR_RELAY_LIMITS.seconds} seconds).",
            "Lower the configured timeout or update the shared relay limit and recreate both images.",
        )
    try:
        endpoint = validate_service_endpoint(
            os.getenv("CASE_INTELLIGENCE_GENERATOR_URL", ""),
            environment_name="CASE_INTELLIGENCE_GENERATOR_ALLOWED_HOSTS", label="generator endpoint",
        )
        validate_generator_environment(os.environ)
        token = generator_api_key()
        backend = os.getenv("CASE_INTELLIGENCE_GENERATOR_BACKEND", "")
        server = os.getenv("CASE_INTELLIGENCE_GENERATOR_SERVER", "") or ("ollama" if backend == "ollama" else "vllm")
        return probe_schema_enforcement(endpoint, server, os.getenv("CASE_INTELLIGENCE_GENERATOR_MODEL", ""),
                                        headers={"Authorization": f"Bearer {token}"} if token else {}, timeout=15)
    except (ValueError, OSError):
        return SchemaProbe("unreachable", 0, "The configured schema check is unavailable.",
                           "Check the answer server configuration and credential file.")


if __name__ == "__main__":
    result = probe_configured_generator()
    print(json.dumps(asdict(result)))
    raise SystemExit(1 if result.status == "unreachable" else 0)
