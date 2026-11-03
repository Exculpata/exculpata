"""Synthetic DAT/OPT volumes; all writes stay below the supplied directory."""

from pathlib import Path


def write_file(root, relative, data):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError("fixture path must stay below its root")
    path.parent.mkdir(parents=True, exist_ok=True)  # fixture-write: Synthetic fixture directories.
    path.write_bytes(data)  # fixture-write: Synthetic bytes within the supplied root.
    return path


def dat_record(values):
    return "\x14".join(f"þ{value}þ" for value in values) + "\n"


def opt_page(label, *, first=True, count=1):
    return (label, "SYNTH001", f"IMAGES/{label}.tif", "Y" if first else "",
            "", "", str(count) if first else "")


def volume(root, headers, rows, pages=(), files=None):
    root = Path(root).resolve()
    dat = dat_record(headers) + "".join(
        row if isinstance(row, str) else dat_record(row) for row in rows)
    write_file(root, "DATA/volume.dat", dat.encode("utf-8"))
    write_file(root, "DATA/volume.opt", "".join(
        ",".join(page) + "\n" for page in pages).encode("utf-8"))
    for page in pages:
        write_file(root, page[2].replace("\\", "/"), b"synthetic image placeholder")
    for name, data in (files or {}).items():
        write_file(root, name, data)
    return root


def exact_volume(root):
    rows, pages, files = [], [], {}
    start = 1
    for index in range(50):
        count = 4 if index < 30 else 3
        begin, end = f"SYN{start:04}", f"SYN{start + count - 1:04}"
        text = f"TEXT/{index:03}.txt"
        files[text] = f"synthetic document {index}\n".encode()
        rows.append((begin, end, "SYN0001" if index < 3 else "",
                     "SYN0012" if index < 3 else "", text))
        pages.extend(opt_page(f"SYN{number:04}", first=number == start, count=count)
                     for number in range(start, start + count))
        start += count
    return volume(root, ("BEGDOC", "ENDDOC", "BEGATTACH", "ENDATTACH", "TEXT"),
                  rows, pages, files)


def hostile_volume(root, count):
    rows = []
    for index in range(count):
        number = index // 3
        prefix_id = number % 1000
        prefix = "P" + "".join(chr(65 + prefix_id // divisor % 26)
                               for divisor in (676, 26, 1))
        label = (f"A{2 * number + 1}", f"A-{2 * number + 1}",
                 f"{prefix}{2 * (number // 1000) + 1}")[index % 3]
        rows.append((label, label))
    return volume(root, ("BEGDOC", "ENDDOC"), rows)
