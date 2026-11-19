"""Bind case-bearing generator submissions to the matter that initiated the work."""

from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

from .external_model_grants import model_destination
from .generation_errors import GenerationApprovalRequired


_matter_scope = ContextVar("generation_matter_scope", default=None)
_reviewed_evaluation = ContextVar("reviewed_generation_evaluation", default=None)


@contextmanager
def generation_matter_scope(workspace, matter_id):
    token = _matter_scope.set((workspace, matter_id))
    try:
        yield
    finally:
        _matter_scope.reset(token)


def _evaluation_request_options(client):
    evaluation = _reviewed_evaluation.get()
    return {"evaluation_client": client} if evaluation is not None and evaluation[0] is client else {}


def require_generation_grant(url, model, evaluation_client=None):
    scope = _matter_scope.get()
    evaluation = _reviewed_evaluation.get()
    parsed = urlsplit(url)
    if not parsed.query and not parsed.fragment:
        for route in ("/api/chat", "/v1/chat/completions", "/tokenize"):
            if parsed.path.endswith(route):
                base_url = parsed._replace(path=parsed.path.removesuffix(route)).geturl()
                destination = model_destination("generation", base_url, model)
                if scope is not None:
                    workspace, matter_id = scope
                    if workspace.model_grant_allows(matter_id, destination):
                        return
                elif evaluation is not None and evaluation_client is evaluation[0] and destination == evaluation[1]:
                    return
                break
    raise GenerationApprovalRequired("External generation requires matter approval for this endpoint and model.")
