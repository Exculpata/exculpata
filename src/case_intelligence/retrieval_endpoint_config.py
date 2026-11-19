"""Offline configuration shared by retrieval clients and matter approvals."""
from dataclasses import dataclass
import hashlib
import math
import os

from .generator_config import generator_pins
from .oidc_network_policy import validated_https_url


@dataclass(frozen=True)
class Endpoint:
    role: str
    url: str
    model: str
    timeout: float

    @property
    def identity(self):
        return "endpoint:" + hashlib.sha256(self.url.encode()).hexdigest()


def configured_endpoint(role, environment=None):
    if role not in {"EMBEDDING", "RERANK"}:
        raise ValueError("Unknown retrieval role")
    environment = os.environ if environment is None else environment
    prefix = f"CASE_INTELLIGENCE_{role}_"
    url = environment.get(prefix + "URL", "").strip()
    if not url:
        return None
    try:
        url = validated_https_url(url, origin_only=True)
        model = environment.get(prefix + "MODEL", "").strip()
        timeout = float(environment.get(prefix + "TIMEOUT", "120"))
        if (not model or len(model) > 200 or not model.isprintable()
                or "$" in url + model or not math.isfinite(timeout) or not 0 < timeout <= 300):
            raise ValueError
        generator_pins(environment.get(prefix + "EGRESS_PINS", ""), url)
    except ValueError:
        raise ValueError(f"Invalid CASE_INTELLIGENCE_{role}_ configuration; check URL (HTTPS origin), MODEL, EGRESS_PINS and TIMEOUT.") from None
    return Endpoint(role, url, model, timeout)
