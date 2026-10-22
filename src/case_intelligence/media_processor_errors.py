"""Staff-safe exceptions shared across the media processor boundary."""


class MediaProcessorError(RuntimeError):
    """Staff-safe failure at the transient processor boundary."""


class MediaProcessorNotFound(MediaProcessorError):
    """The processor verified that an ephemeral job is already absent."""
