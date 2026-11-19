"""Explicit operator retrieval endpoints, with bounded, content-free failures."""
from dataclasses import replace
import http.client
import json
import math
import os
from pathlib import Path
import urllib.error
import urllib.request

from .generator_transport import open_generator_request
from .retrieval_endpoint_config import Endpoint, configured_endpoint
from .retrieval_limits import MAX_BODY_BYTES

REBUILD = ("The embedding index must be rebuilt. Select a 768-dimensional model and stop app writers. "
           "Run python -m case_intelligence.retrieval_rebuild --confirm-stopped. For an external "
           "endpoint, first save and review the output of --plan, then add --approved-plan FILE.")
WRONG_DIMENSION = ("The endpoint returned vectors with the wrong dimension. Select a 768-dimensional "
                   "model. If you change models, the index must be rebuilt with the rebuild command.")
INVALID_CONFIGURATION = "The retrieval endpoint configuration is invalid. Check the destination and model."


class EndpointFailure(RuntimeError):
    """An actionable diagnostic containing no endpoint-supplied text."""


class RetrievalGrantRequired(EndpointFailure):
    """This matter has no approval for the dispatched external destination."""


def relay_environment(environment):
    result = {}
    for role in ("EMBEDDING", "RERANK"):
        endpoint = configured_endpoint(role, environment)
        result[role + "_EGRESS_URL"] = endpoint.url if endpoint else ""
        result[role + "_EGRESS_PINS"] = environment.get(f"CASE_INTELLIGENCE_{role}_EGRESS_PINS", "") if endpoint else ""
    return result


def retrieval_ready(worker_url, local_ready):
    configured = [configured_endpoint(role) for role in ("EMBEDDING", "RERANK")]
    return any(configured) or bool(worker_url and local_ready(worker_url))


def require_index_identity(*, require_record=False):
    from .embedding_index import configured_embedding, embedding_change_warning, embedding_index_path, load_embedding_index
    path = embedding_index_path()
    record = load_embedding_index(path) if path else None
    model, revision = configured_embedding()
    if record is None:
        if (configured_endpoint("EMBEDDING") is not None
                or path is not None and (require_record or path.exists())):
            raise RuntimeError(REBUILD)
        return
    if embedding_change_warning(record, model=model, revision=revision, allow_empty=True):
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
        raise EndpointFailure("Endpoint authentication failed; check the credential." if code in {401, 403}
                              else "Endpoint refused the request; check API compatibility.") from None
    except (http.client.HTTPException, OSError, ValueError):
        raise EndpointFailure("Endpoint unreachable or invalid response; check configuration and relay.") from None


def endpoint_batch(config, operation, body):
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
        if embedding and any(not isinstance(vector, list) or not vector for vector in values):
            raise ValueError
        numbers = [value for vector in values for value in vector] if embedding else values
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in numbers):
            raise ValueError
        if embedding and any(len(vector) != 768 for vector in values):
            raise EndpointFailure(WRONG_DIMENSION)
        return {key: values}
    except (KeyError, TypeError, ValueError, OverflowError):
        raise EndpointFailure("Invalid retrieval response; check API compatibility.") from None


def probe_endpoint(config: Endpoint) -> str:
    """Check a configured API with synthetic inputs independently of the stored index."""
    if config.role not in {"EMBEDDING", "RERANK"}:
        return "Unknown retrieval role. Select embeddings or reranking."
    try:
        config = replace(config, timeout=min(config.timeout, 15))
        models = request_json(config, "/v1/models")
        if not isinstance(models["data"], list):
            return "Invalid model list. Check the endpoint's /v1/models response."
        if not any(row.get("id") == config.model for row in models["data"]):
            return "Configured model is not listed; check the model setting."
        if config.role == "EMBEDDING":
            endpoint_batch(config, "embed", {"texts": ["Synthetic diagnostic text"]})
        else:
            endpoint_batch(config, "rerank", {"texts": ["Synthetic first passage", "Synthetic second passage"],
                                              "query": "Synthetic query"})
        return "ok"
    except http.client.HTTPException:
        return "Endpoint unreachable or invalid response; check configuration and relay."
    except EndpointFailure as exc:
        return str(exc)
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        return "Invalid retrieval configuration or model list; check settings and API compatibility."


def doctor(role):
    try:
        config = configured_endpoint(role)
    except ValueError as exc:
        return str(exc)
    if config is None:
        return "No external endpoint configured. Local worker availability was not checked."
    result = probe_endpoint(config)
    if role == "EMBEDDING":
        try:
            require_index_identity()
        except RuntimeError:
            endpoint_status = "Endpoint passed." if result == "ok" else result
            return f"{endpoint_status} {REBUILD}"
    return result


if __name__ == "__main__":
    print(json.dumps({role: doctor(role) for role in ("EMBEDDING", "RERANK")}))
