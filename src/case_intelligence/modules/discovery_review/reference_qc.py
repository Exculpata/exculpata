"""Compare inert index references with the selected production's directory entries."""
import json
from pathlib import PurePosixPath
import re
import unicodedata

from case_intelligence.pilot_uploads import MAX_UPLOAD_INTAKE_ITEMS, PilotStore, UploadProblem
from case_intelligence.source_load_files import DAT_INTAKE_ROLES

from .fields import MAX_HEADER_CHARS
from .production.matching import path_key


MAX_INVENTORY_BYTES = 6 * 1024 * 1024
MAX_PATH_BYTES = 2048
EXAMPLES_PER_STATUS = 5
EXAMPLE_PATH_CHARS = 200
STATUSES = ("matched", "name_only", "missing", "ambiguous", "ineligible", "unsafe")


class InvalidInventory(ValueError):
    """The supplied selection cannot establish a valid index scope."""


def _dat_profile(value):
    if (not isinstance(value, dict) or set(value) != {"sha256", "fields"}
            or not isinstance(value["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])
            or not isinstance(value["fields"], dict) or not 1 <= len(value["fields"]) <= len(DAT_INTAKE_ROLES)):
        raise InvalidInventory("The proposed DAT field mapping is invalid.")
    fields = value["fields"]
    for header, role in fields.items():
        if (not isinstance(header, str) or len(header) > MAX_HEADER_CHARS or not header.strip()
                or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in header)
                or not isinstance(role, str) or role not in DAT_INTAKE_ROLES):
            raise InvalidInventory("The proposed DAT field mapping is invalid.")
    if len(set(fields.values())) != len(fields):
        raise InvalidInventory("A DAT role was assigned more than once.")
    return {"sha256": value["sha256"], "fields": dict(fields)}


def _descriptor(pair):
    if (not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str)
            or not pair[0] or type(pair[1]) is not bool or len(pair[0]) > MAX_PATH_BYTES):
        raise InvalidInventory("The selection contains an invalid descriptor.")
    raw, eligible = pair
    try:
        if len(raw.encode("utf-8")) > MAX_PATH_BYTES:
            raise InvalidInventory("The selection contains a path beyond the upload limit.")
    except UnicodeEncodeError as error:
        raise InvalidInventory("The selection contains invalid text.") from error
    try:
        selected, _, _, _ = PilotStore.validate_relative_upload_path(raw)
    except UploadProblem as error:
        # Preflight distinguishes an unsupported extension after all path/name
        # safety checks. It remains a selected file that cannot be uploaded.
        if not str(error).startswith("Supported sources are"):
            raise InvalidInventory("The selection contains an unsafe path or filename.") from error
        return str(PurePosixPath(unicodedata.normalize("NFC", raw))), False
    return selected, eligible


class SelectionInventory:
    """Index selected names once; duplicate candidates preserve ambiguity."""

    def __init__(self, data):
        if (not isinstance(data, dict) or set(data) not in ({"index", "files"}, {"index", "files", "dat_profile"})
                or not isinstance(data["files"], list) or not 1 <= len(data["files"]) <= MAX_UPLOAD_INTAKE_ITEMS
                or type(data["index"]) is not int or not 0 <= data["index"] < len(data["files"])):
            raise InvalidInventory("The selected index or file list is invalid.")
        index = data["index"]
        self.index_path, eligible = _descriptor(data["files"][index])
        if not eligible:
            raise InvalidInventory("The chosen index must have a safe, eligible selection entry.")
        self.dat_profile = _dat_profile(data["dat_profile"]) if "dat_profile" in data else None
        if self.dat_profile and not self.index_path.casefold().endswith(".dat"):
            raise InvalidInventory("A field mapping requires a selected DAT index.")
        self.root = self.index_path.partition("/")[0] if "/" in self.index_path else ""
        self.paths, self.names = {}, {}
        size = len('{"files":[],"index":}') + len(str(index))
        if self.dat_profile:
            size += len(',"dat_profile":') + len(json.dumps(self.dat_profile, ensure_ascii=False, separators=(",", ":")).encode())
        for position, pair in enumerate(data["files"]):
            selected, eligible = _descriptor(pair)
            size += len(json.dumps(pair, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + (position > 0)
            if size > MAX_INVENTORY_BYTES:
                raise InvalidInventory("The selected-file metadata exceeds its size limit.")
            if position == index:
                continue
            root, slash, relative = selected.partition("/")
            if (root if slash else "") != self.root:
                continue
            if selected.casefold() == self.index_path.casefold():
                raise InvalidInventory("The selected index path occurs more than once.")
            key = (relative if slash else selected).casefold()
            for entries, name in ((self.paths, key), (self.names, key.rsplit("/", 1)[-1])):
                entries[name] = "ambiguous" if name in entries else "matched" if eligible else "ineligible"

    def compare(self, value):
        if not value:
            return "missing"
        key = path_key(value)
        if not key or value.startswith("~") or not key.strip():
            return "unsafe"
        if self.root:
            direct = self.paths.get(key)
            folder, slash, relative = key.partition("/")
            anchored = self.paths.get(relative) if slash and folder == self.root.casefold() else None
            if direct and anchored:
                return "ambiguous"
            if direct or anchored:
                return direct or anchored
            if slash:
                return "missing"
        by_name = self.names.get(key.rsplit("/", 1)[-1], "missing")
        return "name_only" if by_name == "matched" else by_name


class ReferenceQC:
    def __init__(self, inventory):
        self.inventory = inventory
        self.counts = dict.fromkeys(STATUSES, 0)
        self.examples = []
        self.uncited = []

    def observe(self, role, directory, value, ordinal, line):
        length = len(directory) + (1 if directory else 0) + len(value)
        if length > MAX_PATH_BYTES:
            status = "unsafe"
        else:
            path = directory + "/" + value if directory else value
            status = self.inventory.compare(path) if len(path.encode("utf-8")) <= MAX_PATH_BYTES else "unsafe"
        self.counts[status] += 1
        if status != "matched" and self.counts[status] <= EXAMPLES_PER_STATUS:
            prefix = directory[:EXAMPLE_PATH_CHARS] + "/" if directory else ""
            example = {"role": role, "path": (prefix + value[:EXAMPLE_PATH_CHARS])[:EXAMPLE_PATH_CHARS],
                       "path_truncated": length > EXAMPLE_PATH_CHARS, "ordinal": ordinal, "line": line, "status": status}
            self.examples.append(example)
            if line is None:
                self.uncited.append(example)
        return status

    def cite_dii_block(self, line):
        for example in self.uncited:
            example["line"] = line
        self.uncited.clear()

    def report(self):
        total = sum(self.counts.values())
        return {"total": total, "counts": self.counts, "examples": self.examples,
                "examples_truncated": total - self.counts["matched"] > len(self.examples)}
