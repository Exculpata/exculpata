"""Generate invented 50-document, 180-page volumes in a caller-owned directory."""
from pathlib import Path
import shutil


def generate(destination):
    root = Path(destination) / "SYNTH001"
    for folder in ("TEXT", "IMAGES", "NATIVES", "encodings"):
        (root / folder).mkdir(parents=True, exist_ok=True)
    rows = [("BEGDOC", "ENDDOC", "BEGATTACH", "ENDATTACH", "TEXT", "NATIVE")]
    pages, begin = [], 1
    for document in range(50):
        count = 4 if document < 30 else 3
        end = begin + count - 1
        text = f"TEXT/DOC{document + 1}.txt"
        native = f"NATIVES/DOC{document + 1}.txt" if document < 3 else ""
        rows.append((f"SYN{begin:06}", f"SYN{end:06}", "SYN000001" if document < 2 else "",
                     "SYN000008" if document < 2 else "", text, native))
        (root / text).write_text("Synthetic extracted text.\n")  # fixture-write: Generated synthetic text.
        if native:
            (root / native).write_text("Synthetic native text.\n")  # fixture-write: Generated synthetic native.
        for page in range(begin, end + 1):
            path = f"IMAGES/PAGE{page}.txt"
            (root / path).write_text("Synthetic page.\n")  # fixture-write: Generated synthetic page stand-in.
            pages.append(f"SYN{page:06},SYNTH001,{path},{'Y' if page == begin else ''},,,{count if page == begin else ''}\n")
        begin = end + 1
    dat = "".join("\x14".join(f"þ{value}þ" for value in row) + "\n" for row in rows)
    (root / "SYNTH001.dat").write_bytes(dat.encode("cp1252"))  # fixture-write: Generated synthetic DAT.
    (root / "SYNTH001.opt").write_text("".join(pages))  # fixture-write: Generated synthetic OPT.
    for encoding in ("cp1252", "utf-8", "utf-16", "utf-16-be"):
        data = dat.encode(encoding)
        if encoding == "utf-16-be":
            data = b"\xfe\xff" + data
        (Path(destination) / f"{encoding}.dat").write_bytes(data)  # fixture-write: Alternate encoding outside the volume.
    shutil.copytree(Path(__file__).parent.parent / "SYNTH002", Path(destination) / "SYNTH002")  # fixture-write: Copy invented QC volume.
