"""The matter-scoped identity of an external model service."""
from dataclasses import dataclass
import os
from urllib.parse import urlsplit

from .generator_config import bundled_generator, generator_server, validate_generator_environment
from .oidc_network_policy import issuer_authority, validated_https_url
from .retrieval_endpoint_config import configured_endpoint


ENDPOINT_ROLES = {"embedding": "EMBEDDING", "reranking": "RERANK", "transcription": "ASR"}
CAPABILITIES = ("generation", *ENDPOINT_ROLES)


@dataclass(frozen=True)
class ModelDestination:
    capability: str
    endpoint: str
    model: str

    def __post_init__(self):
        if self.capability not in CAPABILITIES:
            raise ValueError("Unknown model capability.")
        if (not isinstance(self.model, str) or not 1 <= len(self.model) <= 200
                or not self.model.isprintable() or self.model.strip() != self.model or "$" in self.model):
            raise ValueError("Invalid external model name.")
        if not isinstance(self.endpoint, str) or "$" in self.endpoint:
            raise ValueError("Invalid external model endpoint.")
        url = validated_https_url(self.endpoint, origin_only=self.capability != "generation")
        host, port = issuer_authority(url)
        authority = f"[{host}]" if ":" in host else host
        port_part = "" if port == 443 else f":{port}"
        path = urlsplit(url).path.rstrip("/")
        object.__setattr__(self, "endpoint", f"https://{authority}{port_part}{path}")


def model_destination(capability: str, url: str, model: str) -> ModelDestination:
    return ModelDestination(capability, url, model)


def configured_model_destination(capability: str) -> ModelDestination | None:
    if capability == "generation":
        backend = os.getenv("CASE_INTELLIGENCE_GENERATOR_BACKEND", "").strip().casefold()
        if not backend:
            return None
        if backend not in {"ollama", "openai"}:
            raise ValueError("Unsupported answer server.")
        validate_generator_environment(os.environ)
        generator_server(backend)
        url = os.getenv("CASE_INTELLIGENCE_GENERATOR_URL", "").strip()
        if bundled_generator(url):
            return None
        model = os.getenv("CASE_INTELLIGENCE_GENERATOR_MODEL", "").strip()
        return model_destination(capability, url, model)
    if capability not in ENDPOINT_ROLES:
        raise ValueError("Unknown model capability.")
    endpoint = configured_endpoint(ENDPOINT_ROLES[capability])
    return model_destination(capability, endpoint.url, endpoint.model) if endpoint else None
