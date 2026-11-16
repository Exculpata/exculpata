"""Invented LFP/DII bytes built from code; no external export or source files."""
from __future__ import annotations

import codecs


def _encoded(text, encoding):
    normalized = encoding.lower().replace("_", "-")
    if normalized == "utf-16-le":
        return codecs.BOM_UTF16_LE + text.encode(normalized)
    if normalized == "utf-16-be":
        return codecs.BOM_UTF16_BE + text.encode(normalized)
    return text.encode(encoding)


def lfp_bytes(records=None, *, encoding="utf-8", newline="\n"):
    """Build IM lines from explicit key/boundary/location/type/rotation mappings."""
    if records is None:
        records = (
            {"key": "SYNGR0007", "boundary": "D", "volume": "SYNVOL",
             "directory": "Generated/Images", "filename": "café.png", "image_type": "4"},
            {"key": "SYNGR0099", "boundary": "", "volume": "SYNVOL",
             "directory": "Generated/Images", "filename": "blue.png", "image_type": "4", "rotation": "0"},
            {"key": "SYNGR0042", "boundary": "D", "filename": "green.tif", "image_type": "2", "rotation": "2"},
        )
    lines = []
    for record in records:
        filename, image_type = record["filename"], record.get("image_type", "4")
        if "volume" in record:
            location = ";".join(("@" + record["volume"], record.get("directory", ""), filename, image_type))
        else:
            location = ";".join((filename, image_type))
        values = ["IM", record["key"], record.get("boundary", "D"), record.get("offset", "0"), location]
        if "rotation" in record:
            values.append(record["rotation"])
        lines.append(",".join(values))
    return _encoded(newline.join(lines) + (newline if lines else ""), encoding)


def dii_bytes(documents=None, *, encoding="utf-8", newline="\n"):
    """Build document-mode blocks from literal tags and explicit image entries."""
    if documents is None:
        documents = (
            {"record_id": "synthetic-alpha", "begin_bates": "SYNGR0007", "page_count": "0002",
             "docid": "separate-alpha", "directory": "Generated\\Images", "images": ("café.png", "blue.png")},
            {"record_id": "synthetic-beta", "begin_bates": "SYNGR0042",
             "directory": "", "images": ("green.tif",)},
        )
    lines = ["@FULLTEXT DOC"]
    for document in documents:
        lines.extend(("@T " + document["record_id"], "@BATESBEG " + document["begin_bates"]))
        for key, tag in (("docid", "@DOCID"), ("page_count", "@C PGCOUNT"), ("parent_id", "@PARENTID")):
            if key in document:
                lines.append(tag + " " + str(document[key]))
        directory = document.get("directory", "")
        lines.append("@D @I\\" + (directory + "\\" if directory else ""))
        lines.extend(document.get("images", ()))
    return _encoded(newline.join(lines) + newline, encoding)


def minimal_image_index(suffix):
    """A valid generated index smaller than the browser harness's 128-byte limit."""
    suffix = suffix.lower().lstrip(".")
    if suffix == "lfp":
        return lfp_bytes(({"key": "SYN1", "boundary": "D", "filename": "red.png", "image_type": "4"},))
    if suffix == "dii":
        return dii_bytes(({"record_id": "SYN1", "begin_bates": "SYN1", "directory": "", "images": ("red.png",)},))
    raise ValueError("The generated format must be LFP or DII.")
