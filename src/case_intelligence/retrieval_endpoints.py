"""Explicit operator retrieval endpoints, with bounded, content-free failures."""
from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
import urllib.error
import urllib.request

from .generator_transport import open_generator_request
from .generator_config import generator_pins
from .oidc_network_policy import validated_https_url
from .retrieval_limits import MAX_BODY_BYTES

REBUILD = ("The embedding index must be rebuilt. Select a 768-dimensional model, stop app writers, "
           "then run: python -m case_intelligence.retrieval_rebuild --confirm-stopped")


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
        if (not model or len(model) > 200 or any(ord(c) < 32 for c in model)
                or "$" in url + model or not math.isfinite(timeout) or not 0 < timeout <= 300):
            raise ValueError
        generator_pins(environment.get(prefix + "EGRESS_PINS", ""), url)
    except ValueError:
        raise ValueError("Invalid retrieval endpoint configuration; check HTTPS origin, model, pins and timeout.") from None
    return Endpoint(role, url, model, timeout)


def relay_environment(environment):
    result = {}
    for role in ("EMBEDDING", "RERANK"):
        endpoint = configured_endpoint(role, environment)
        result[role + "_EGRESS_URL"] = endpoint.url if endpoint else ""
        result[role + "_EGRESS_PINS"] = environment.get(f"CASE_INTELLIGENCE_{role}_EGRESS_PINS", "")
    return result


def retrieval_ready(worker_url, local_ready):
    configured = [configured_endpoint(role) for role in ("EMBEDDING", "RERANK")]
    return any(configured) or bool(worker_url and local_ready(worker_url))


def require_index_identity():
    from .embedding_index import configured_embedding, embedding_index_path, load_embedding_index
    path = embedding_index_path()
    record = load_embedding_index(path) if path else None
    model, revision = configured_embedding()
    if (record is None and (path is not None or configured_endpoint("EMBEDDING") is not None) or record is not None and
            (type(record.get("populated")) is not bool or
             record.get("model") != model or record.get("revision") != revision)):
        raise RuntimeError(REBUILD)


def request_json(config, path, body=None):
    prefix = f"CASE_INTELLIGENCE_{config.role}"
    try:
        key = os.getenv(prefix + "_API_KEY", "")
        key_file = os.getenv(prefix + "_API_KEY_FILE", "")
        if not key and key_file:
            key = Path(key_file).read_text(encoding="utf-8").strip()
        data = json.dumps(body).encode() if body is not None else None
        if data is not None and len(data) > MAX_BODY_BYTES:
            raise ValueError
        request = urllib.request.Request(config.url + path, data=data,
            headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {key}"} if key else {})})
        with open_generator_request(request, timeout=config.timeout, environment_prefix=prefix) as response:
            raw = response.read(MAX_BODY_BYTES + 1)
        if len(raw) > MAX_BODY_BYTES:
            raise ValueError
        return json.loads(raw)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise RuntimeError("Endpoint authentication failed; check the credential." if code in {401, 403}
                           else "Endpoint refused the request; check API compatibility.") from None
    except (OSError, ValueError):
        raise RuntimeError("Endpoint unreachable or invalid response; check configuration and relay.") from None


def endpoint_batch(config, operation, body):
    from .review_bench_v2 import _model_numbers
    embedding = operation == "embed"
    payload = {"model": config.model, "input" if embedding else "documents": body["texts"]}
    if not embedding:
        payload["query"] = body["query"]
    result = request_json(config, "/v1/embeddings" if embedding else "/v1/rerank", payload)
    try:
        rows = result["data" if embedding else "results"]
        if (not isinstance(rows, list) or len(rows) != len(body["texts"])
                or any(type(row["index"]) is not int for row in rows)
                or sorted(row["index"] for row in rows) != list(range(len(rows)))):
            raise ValueError
        values = [row["embedding" if embedding else "relevance_score"]
                  for row in sorted(rows, key=lambda row: row["index"])]
        key = "vectors" if embedding else "scores"
        checked = _model_numbers({key: values}, key, len(rows), vectors=embedding)
        if embedding and any(len(vector) != 768 for vector in checked):
            raise RuntimeError(REBUILD)
        return {key: values}
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("Invalid retrieval response; check API compatibility.") from None


def doctor(role):
    try:
        config = configured_endpoint(role)
        if config is None:
            return "local worker (no external endpoint configured)"
        if role == "EMBEDDING":
            require_index_identity()
        config = replace(config, timeout=min(config.timeout, 15))
        models = request_json(config, "/v1/models")
        if not any(row.get("id") == config.model for row in models["data"]):
            return "Configured model is not listed; check the model setting."
        endpoint_batch(config, "embed" if role == "EMBEDDING" else "rerank",
                       {"texts": ["Synthetic diagnostic text"], "query": "Synthetic query"})
        return "ok"
    except (ValueError, KeyError, TypeError, AttributeError):
        return "Invalid retrieval configuration or model list; check settings and API compatibility."
    except RuntimeError as exc:
        return str(exc)


if __name__ == "__main__":
    print(json.dumps({role: doctor(role) for role in ("EMBEDDING", "RERANK")}))
