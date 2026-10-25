"""Run the content-free schema diagnostic inside the existing app namespace."""
from dataclasses import asdict
import json
import os

from .generator_completion import generator_api_key
from .generator_config import validate_generator_environment
from .generator_schema import SchemaProbe, probe_schema_enforcement
from .service_endpoints import validate_service_endpoint


def probe_configured_generator():
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
    print(json.dumps(asdict(probe_configured_generator())))
