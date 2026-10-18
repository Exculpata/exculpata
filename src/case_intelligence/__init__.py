__version__ = "0.1.0b1"


def _protect_offline_idna_import():
    """Keep the trust anchor outside the cache directory it validates."""
    import hashlib
    import importlib.abc
    import importlib.machinery
    import importlib.util
    import marshal
    import re
    from pathlib import Path
    import sys

    name = __name__ + "._idna"
    source = Path(__file__).with_name("_idna") / "__init__.py"

    class SourceOnlyLoader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            data = source.read_bytes()
            cache = source.parent / "__pycache__"
            if cache.is_symlink():
                raise RuntimeError("IDNA bytecode cache must not be a symlink")
            if cache.exists():
                expected = {
                    Path(importlib.util.cache_from_source(str(source), optimization=level)).name: optimize
                    for level, optimize in (("", 0), ("1", 1), ("2", 2))
                }
                for artifact in cache.iterdir():
                    if (artifact.is_symlink() or not artifact.is_file()
                            or (artifact.name not in expected and not re.fullmatch(
                                r"__init__\.cpython-[0-9]{2,3}(?:\.opt-[12])?\.pyc", artifact.name))):
                        raise RuntimeError("Unexpected IDNA bytecode artifact")
                    if artifact.name not in expected:
                        # A different interpreter tag cannot supply this import.
                        # It is never read or executed; its owning runtime checks
                        # its payload if the source tree is used there later.
                        continue
                    # compileall permits source-relative filenames; these alter
                    # traceback metadata, never the source being compiled.
                    filenames = {str(source), *(str(Path(*source.parts[i:]))
                                                for i in range(1, len(source.parts)))}
                    trusted_payloads = []
                    for filename in filenames:
                        compiled = compile(data, filename, "exec", dont_inherit=True,
                                           optimize=expected[artifact.name])
                        # Match the cache writer's caller/callee references:
                        # marshal also encodes reference ownership.
                        cache_writer_references = (compiled, compiled)
                        trusted_payloads.append(marshal.dumps(cache_writer_references[0]))
                    maximum = max(map(len, trusted_payloads))
                    hashes = {hashlib.sha256(payload).digest() for payload in trusted_payloads}
                    with artifact.open("rb") as stream:
                        stored = stream.read(16 + maximum + 1)
                    if (len(stored) < 16 or len(stored) > 16 + maximum
                            or stored[:4] != importlib.util.MAGIC_NUMBER
                            or int.from_bytes(stored[4:8], "little") not in (0, 1, 3)
                            or hashlib.sha256(stored[16:]).digest() not in hashes):
                        raise RuntimeError("IDNA bytecode failed source integrity validation")
            # Neither timestamp nor source-hash headers authorize execution of a
            # cached payload. Compile the trusted source bytes on every load/reload.
            return compile(data, str(source), "exec", dont_inherit=True, optimize=sys.flags.optimize)

    class IdnaFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == name:
                loader = SourceOnlyLoader(fullname, str(source))
                return importlib.util.spec_from_file_location(fullname, source, loader=loader)
            return None

    # Only this exact private package is intercepted; all other imports retain
    # their existing resolution, including HTTPX's separately locked IDNA.
    sys.meta_path.insert(0, IdnaFinder())


_protect_offline_idna_import()
del _protect_offline_idna_import
