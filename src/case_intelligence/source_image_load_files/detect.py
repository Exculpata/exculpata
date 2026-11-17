"""Content recognition delegated to the frozen shared admission grammar."""
from case_intelligence.source_admission import classify_image_load_file


def detect_image_load_file(path, *, name=None):
    """Return the common recognition outcome without inferring from a filename.

    The caller already owns eligibility and source authority. A display name
    supplies no grammar evidence. Recognition does not certify a later tail and
    never authorizes fallback to another format after interpretation fails.
    """
    return classify_image_load_file(path)
