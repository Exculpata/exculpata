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

    def trusted_payloads(data, filename, optimize):
        compiled = compile(data, filename, "exec", dont_inherit=True, optimize=optimize)
        # Preserve the cache writer's reference ownership for marshal encoding.
        cache_writer_references = (compiled, compiled)
        payload = marshal.dumps(cache_writer_references[0])
        del cache_writer_references, compiled
        # Python runtimes may release the writer's caller reference before the
        # marshal call. Both encodings below contain only freshly compiled source.
        unshared = marshal.dumps(compile(data, filename, "exec", dont_inherit=True,
                                         optimize=optimize))
        return payload, unshared

    def filename_candidates(payload):
        # Read bounded string *candidates*, never deserialize untrusted objects.
        # Candidates authorize nothing: a complete payload hash must still match
        # trusted source compiled with that traceback-only filename.
        found = set()
        for offset, tag in enumerate(payload):
            tag &= 0x7f
            width = 1 if tag in b"zZ" else 4 if tag in b"aAut" else 0
            if not width or offset + 1 + width > len(payload):
                continue
            start = offset + 1 + width
            size = int.from_bytes(payload[offset + 1:start], "little")
            if not 0 < size <= 16384 or start + size > len(payload):
                continue
            try:
                name = payload[start:start + size].decode("utf-8", "surrogatepass")
            except UnicodeDecodeError:
                continue
            if "\0" in name or name in found:
                continue
            found.add(name)
            if len(found) > 128:
                raise RuntimeError("Too many IDNA bytecode filename candidates")
            yield name
            # Both compiler-owned and caller-interned filename strings occur in
            # genuine caches. Their marshal string tags differ, not their code.
            yield sys.intern(name)

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
                    optimize = expected[artifact.name]
                    canonical = trusted_payloads(data, str(source), optimize)
                    maximum = max(map(len, canonical)) + 16384
                    with artifact.open("rb") as stream:
                        stored = stream.read(16 + maximum + 1)
                    if (len(stored) < 16 or len(stored) > 16 + maximum
                            or stored[:4] != importlib.util.MAGIC_NUMBER
                            or int.from_bytes(stored[4:8], "little") not in (0, 1, 3)):
                        raise RuntimeError("IDNA bytecode failed source integrity validation")
                    digest = hashlib.sha256(stored[16:]).digest()
                    if digest not in {hashlib.sha256(item).digest() for item in canonical} and not any(
                        digest == hashlib.sha256(item).digest()
                        for filename in filename_candidates(stored[16:])
                        for item in trusted_payloads(data, filename, optimize)
                    ):
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
