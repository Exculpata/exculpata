"""Private transport binding for the two reviewed standalone model evaluations."""

from contextlib import contextmanager

from .external_model_grants import model_destination
from .generation_matter import _reviewed_evaluation


@contextmanager
def _reviewed_endpoint_scope(client):
    """Bind a verified in-memory corpus to its selected generator for the scoring call."""
    endpoint = getattr(client, "endpoint", "")
    if not endpoint.startswith("https://"):
        yield
        return
    reviewed = model_destination("generation", endpoint, client.model)
    token = _reviewed_evaluation.set((client, reviewed))
    try:
        yield
    finally:
        _reviewed_evaluation.reset(token)
