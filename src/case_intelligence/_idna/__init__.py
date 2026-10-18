"""Pinned, offline IDNA2008 source; import only the bytes verified in memory."""
import hashlib
import io
from pathlib import Path
import sys
from types import ModuleType
import zipfile

ARCHIVE_SHA256 = "390aa8fb9c672c2f89f4c02d12d89671bde3dbcabba5aee9226356b4fce4ae13"
archive = Path(__file__).with_name("idna-3.20.pyz")
try:
    data = archive.read_bytes()
    if hashlib.sha256(data).hexdigest() != ARCHIVE_SHA256:
        raise RuntimeError("Bundled IDNA source failed integrity validation")
    allowed = {"__init__.py", archive.name, "manifest.json", "LICENSE.md", "README.md", "__pycache__"}
    if any(path.name not in allowed for path in archive.parent.iterdir()):
        raise RuntimeError("Unexpected bundled IDNA sibling")
except OSError as exc:
    raise RuntimeError("Bundled IDNA source is unavailable") from exc

# No directory or archive pathname can supply relative imports after validation.
# Load dependencies first, including core's deferred UTS46 import, from these bytes.
__path__ = []
with zipfile.ZipFile(io.BytesIO(data)) as source:
    for name in ("idnadata", "intranges", "package_data", "uts46data", "core"):
        fullname = __name__ + "." + name
        module = ModuleType(fullname)
        module.__package__ = __name__
        module.__file__ = str(archive) + "/idna/" + name + ".py"
        sys.modules[fullname] = module
        exec(compile(source.read("idna/" + name + ".py"), module.__file__, "exec"), module.__dict__)
        globals()[name] = module
IDNAError, encode = core.IDNAError, core.encode
__version__ = package_data.__version__
del data, source, module, name, fullname
