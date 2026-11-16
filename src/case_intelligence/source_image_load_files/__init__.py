"""Public image-index parsers with lazy format imports to keep core acyclic."""
from .records import DiiDocument, Finding, LoadFileError, OptPage, RecordEvent


def parse_lfp(path, *, findings=None, on_event=None, cursor=None):
    from .lfp import parse_lfp as parse
    return parse(path, findings=findings, on_event=on_event, cursor=cursor)


def parse_dii(path, *, findings=None, on_event=None, cursor=None):
    from .dii import parse_dii as parse
    return parse(path, findings=findings, on_event=on_event, cursor=cursor)


def detect_image_load_file(path, *, name=None):
    from .detect import detect_image_load_file as detect
    return detect(path, name=name)


__all__ = ["DiiDocument", "Finding", "LoadFileError", "OptPage", "RecordEvent",
           "detect_image_load_file", "parse_dii", "parse_lfp"]
