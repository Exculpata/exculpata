"""Read-only, source-cited volume QC; ranges retain their produced labels."""
import hashlib
import os
import re
from collections import Counter, defaultdict, namedtuple
from datetime import datetime
from pathlib import Path
from .source_load_files import Finding, LoadFileError, parse_dat, parse_opt, resolve_volume_path

BatesRange = namedtuple("BatesRange", "begin end file line page_count")
QCReport = namedtuple("QCReport", "document_count page_count documents pages ranges joined_pages families findings")

def parse_plain_label(label):
    match = re.fullmatch(r"([A-Za-z]+)([0-9]{1,18})", label)
    return (match[1], int(match[2]), len(match[2])) if match else None

def _valid_date(value):
    if not value.strip():
        return True
    value = value.replace("T", " ")
    for pattern, form in ((r"[0-9]{4}-[0-9]{2}-[0-9]{2}", "%Y-%m-%d"),
                          (r"[0-9]{8}", "%Y%m%d"), (r"[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}", "%m/%d/%Y")):
        for suffix, time_format in (("", ""), (r" [0-9]{2}:[0-9]{2}", " %H:%M"),
                                    (r" [0-9]{2}:[0-9]{2}:[0-9]{2}", " %H:%M:%S")):
            if re.fullmatch(pattern + suffix, value):
                try:
                    datetime.strptime(value, form + time_format)
                    return True
                except ValueError:
                    return False
    return False

def qc_volume(volume_root, *, dat_path, opt_path, profile):
    """Inspect a stable volume; reject load files outside its explicit root."""
    root = Path(volume_root).resolve()
    for path in (dat_path, opt_path):
        if Path(path).resolve() == root or not Path(path).resolve().is_relative_to(root):
            raise LoadFileError("unsafe_path", path, 1)
    findings, counts, truncated = [], Counter(), {}
    def add(code, file, line, count=1):
        kept = min(count, max(0, 1000 - counts[code]))
        findings.extend(Finding(code, str(file), line) for _ in range(kept))
        counts[code] += count
        if count > kept:
            if code not in truncated:
                truncated[code] = Finding("findings_truncated", str(file), line, code)
                findings.append(truncated[code])
            truncated[code].count += count - kept
    parser_findings, headers = [], []
    documents = tuple(parse_dat(dat_path, findings=parser_findings, headers_out=headers))
    pages = tuple(parse_opt(opt_path, findings=parser_findings))
    for finding in parser_findings:
        add(finding.truncated_code or finding.code, finding.file, finding.line,
            finding.count if finding.truncated_code else 1)
    positions = {target: headers.index(header) for header, target in profile.fields.items()
                 if headers.count(header) == 1}
    ambiguous = any(headers.count(header) > 1 for header in profile.fields)
    mapped = "begin_bates" in positions and not ambiguous
    if headers and (ambiguous or not mapped):
        add("ambiguous_mapping" if ambiguous else "missing_bates_mapping", dat_path, 1)
    referenced = {Path(dat_path).resolve(), Path(opt_path).resolve()}
    def file_path(value, record):
        try:
            path = resolve_volume_path(root, value, record.file, record.line)
            referenced.add(path)
            if path.is_file():
                return path
            add("missing_file", record.file, record.line)
        except LoadFileError as error:
            add(error.code, error.file, error.line)
        except OSError:
            add("read_error", record.file, record.line)
    values, begins, ends, groups, page_keys = {}, Counter(), set(), defaultdict(list), Counter(p.values[0] for p in pages)
    group, first_line, group_sizes = None, None, Counter()
    for page in pages:
        file_path(page.values[2], page)
        if page_keys[page.values[0]] > 1:
            add("duplicate_opt_key", page.file, page.line)
        if page.values[3] == "Y":
            group, first_line = page.values[0], page.line
        groups[group].append(page)
        group_sizes[first_line] += 1
    groups = {key: tuple(group_pages) for key, group_pages in groups.items()}
    ranges, joined, families, intervals = [], {}, defaultdict(list), defaultdict(list)
    for row in documents:
        fields = {target: row.values[index] for target, index in positions.items()}
        values[row.line] = fields
        paths = {target: file_path(fields[target], row) for target in ("text_path", "native_path") if target in fields}
        if "date" in fields and not _valid_date(fields["date"]):
            add("unparseable_date", row.file, row.line)
        for algorithm in ("md5", "sha1", "sha256"):
            if algorithm in fields and paths.get("native_path"):
                try:
                    with paths["native_path"].open("rb") as stream:
                        actual = hashlib.file_digest(stream, algorithm).hexdigest()
                    if actual.lower() != fields[algorithm].strip().lower():
                        add("hash_mismatch", row.file, row.line)
                except OSError:
                    add("read_error", row.file, row.line)
        if not mapped:
            continue
        begin, end = fields["begin_bates"], fields.get("end_bates", fields["begin_bates"])
        begins[begin] += 1
        ends.add(end)
        joined[row.line] = groups.get(begin, ())
        ranges.append(BatesRange(begin, end, row.file, row.line, len(joined[row.line])))
    by_begin = {r.begin: r for r in ranges if begins[r.begin] == 1}
    for r in ranges:
        start, end = parse_plain_label(r.begin), parse_plain_label(r.end)
        if begins[r.begin] > 1:
            add("duplicate_begin", r.file, r.line)
        if not r.page_count:
            add("dat_only", r.file, r.line)
        if not start or not end:
            add("unparseable_label", r.file, r.line)
        elif start[0] != end[0]:
            add("prefix_mismatch", r.file, r.line)
        elif start[1] > end[1]:
            add("reversed_range", r.file, r.line)
        else:
            intervals[start[0]].append((start[1], end[1], r.line))
            if r.page_count and r.page_count != end[1] - start[1] + 1:
                add("page_count_mismatch", r.file, r.line)
        fields = values[r.line]
        attachment = (fields.get("begin_attachment", ""), fields.get("end_attachment", ""))
        if any(attachment):
            if attachment[0] not in begins or attachment[1] not in ends:
                add("orphan_attachment", r.file, r.line)
            else:
                families[attachment].append(r.begin)
    for entries in intervals.values():
        maximum = -1
        for start, end, line in sorted(entries):
            if start <= maximum:
                add("overlap", dat_path, line)
            maximum = max(maximum, end)
    for key, group_pages in groups.items():
        r = by_begin.get(key)
        start, end = (parse_plain_label(r.begin), parse_plain_label(r.end)) if r else (None, None)
        for page in group_pages:
            if mapped and key not in begins:
                add("opt_only", page.file, page.line)
            if page.values[3] == "Y" and not re.fullmatch("0*" + str(group_sizes[page.line]), page.values[6]):
                add("page_count_mismatch", page.file, page.line)
            label = parse_plain_label(page.values[0])
            if start and end and start[0] == end[0] and start[1] <= end[1]:
                if not label or label[0] != start[0] or not start[1] <= label[1] <= end[1]:
                    add("page_outside_range", page.file, page.line)
    for folder, _, names in os.walk(root, onerror=lambda error: add("read_error", error.filename, 0)):
        for name in names:
            path = Path(folder) / name
            try:
                resolved = path.resolve()
                if resolved == root or not resolved.is_relative_to(root):
                    raise ValueError
                if resolved not in referenced:
                    add("orphan_file", path, 0)
            except (ValueError, OSError, RuntimeError):
                add("unsafe_path", path, 0)
    return QCReport(len(documents), len(pages), documents, pages, tuple(ranges), joined,
                    tuple(tuple(members) for members in families.values()), tuple(findings))
