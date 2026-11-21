"""Original image references, separate from document or page associations."""
from case_intelligence.pilot_uploads import IMAGE_MEDIA_TYPES, UploadProblem
from case_intelligence.source_image_load_files import parse_dii

from ..load_files import dii_review_event
from .matching import path_key
from .repository import ProductionStale

IMAGE_WINDOW = 10


def image_reference(repository, store, matter_id, attempt, path, line, ordinal):
    result = {"path": path, "line": line, "ordinal": ordinal, "status": "unavailable", "source": None}
    if not path_key(path):
        return {**result, "status": "unsafe_path"}
    candidates = repository.exact_path_candidates(matter_id, attempt["root_key"], path,
        excluded_document_id=attempt["document_id"])
    if len(candidates) > 1:
        return {**result, "status": "ambiguous"}
    if not candidates or candidates[0]["media_type"] not in IMAGE_MEDIA_TYPES:
        return result
    candidate = candidates[0]
    try:
        document = store.get(candidate["document_id"])
        if (document.version_id != candidate["version_id"] or document.state != "ready"
                or document.media_type != candidate["media_type"] or document.relative_path != candidate["relative_path"]):
            return result
        store.source_path(document.document_id)
    except (KeyError, UploadProblem, OSError):
        return result
    return {**result, "status": "matched", "source": {"document_id": document.document_id,
            "version_id": document.version_id, "media_type": document.media_type}}


def dii_paths(path, record, start, fingerprint):
    """Release names only after the parsed block equals its preserved record."""
    names, count, verified = [], 0, False

    def observe(block, directory, name):
        nonlocal count
        if block == record["ordinal"]:
            count += 1
            if start <= count < start + IMAGE_WINDOW:
                names.append(directory + "/" + name if directory else name)

    def check(event):
        nonlocal verified
        if event.kind == "file_failure":
            raise ProductionStale("The DII source could not be verified.")
        if event.ordinal != record["ordinal"]:
            return
        projected = dii_review_event(event)
        if (projected.kind != "accepted" or verified or event.line != record["line"]
                or not event.next_cursor or tuple(event.next_cursor["source"]) != fingerprint
                or tuple(record["headers"]) != projected.headers or tuple(record["cells"]) != projected.cells
                or tuple(tuple(pair) for pair in record["optional_values"]) != projected.optional_values
                or tuple(record["diagnostics"]) != projected.diagnostics
                or dict(event.optional_values).get("observed_image_entries") != str(count)):
            raise ProductionStale("The DII document no longer matches its preserved record.")
        verified = True

    for _ in parse_dii(path, on_event=check, on_image_name=observe):
        pass
    if not verified:
        raise ProductionStale("The selected DII document is no longer available.")
    if start > count:
        raise KeyError("The image position is outside the selected DII document.")
    return names, count
