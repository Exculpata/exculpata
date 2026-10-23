"""Generator failures distinguish runtime availability from rejected model output."""
from __future__ import annotations


class GenerationUnavailable(RuntimeError):
    """The configured local generator cannot currently answer."""


class GenerationSchemaRejected(GenerationUnavailable):
    """A parsed object failed local validation; only explicit evaluation retains it."""

    def __init__(self, payload):
        super().__init__("The reply did not match the JSON schema. Answering is unavailable for this request.")
        self.payload = payload


class GenerationInvalidContent(GenerationUnavailable):
    """The service returned text that is not a structured model answer."""


class GenerationRejected(RuntimeError):
    """The generated response could not pass independent support checks."""


class GenerationGroundingRejected(GenerationRejected):
    """The response was well formed, but no generated claim was supportable."""
