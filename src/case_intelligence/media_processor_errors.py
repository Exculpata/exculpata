"""Staff-safe exceptions shared across the media processor boundary."""


class MediaProcessorError(RuntimeError):
    """Staff-safe failure at the transient processor boundary."""


class MediaProcessorNotFound(MediaProcessorError):
    """The processor verified that an ephemeral job is already absent."""


class TranscriptionApprovalRequired(MediaProcessorError):
    def __init__(self):
        super().__init__("Transcription requires current matter approval for the configured endpoint and model.")
