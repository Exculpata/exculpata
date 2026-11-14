#!/usr/bin/env python3
"""Preflight a spec against a read-only checkout of main before it goes to the owner or a reviewer.

Usage:
  python3 check_spec.py SPEC.md [--repo PATH] [--check WORKFLOW]...
  python3 check_spec.py NOTE.md --design-note [--repo PATH]
  python3 check_spec.py --selftest

Checks:
  (a)  allowed paths: every file the named failing check runs (workflow run: lines,
       the scripts they call, those scripts' local imports and path-loaded files,
       make recipes and shell scripts) must sit inside the spec's Files list or the
       paths in its APPROVED line.
  (b)  import boundary: any reuse or import the spec prescribes must pass main's
       import-boundary ratchet (layer map + _forbidden rule + baseline); a new module
       must be mappable and the layer map must be inside the fence.
  (c)  main's behavior: expected output lines are matched against string literals on
       main (static, never executed); calls the named check makes to Python scripts
       are matched against those scripts' argparse definitions on main.

Design-note mode (--design-note) checks a design note against the design gate instead
of the three checks above:
  (d)  acceptance: a numbered acceptance list (heading with "acceptance", at least 2
       numbered items; items with no number or measurable word are UNCHECKED).
  (e)  dry run: a "dry run" or "frozen tests" section that records a command and a
       result (N passed, N failed, exit N); a result of only NOT RUN is UNCHECKED.
  (f)  fence: a "fence" section that names its paths and shows the grep command it came
       from; the grep is re-run read-only with git grep on --repo and every file it
       matches must be named in the fence section (else FLAG).
--selftest runs built-in cases for (d), (e), (f) and exits 1 on any failure.

Output is plain text, one finding per line: FLAG, UNCHECKED, OK or NOTE, then a
summary line. Exit 1 when anything is flagged, 2 on usage error, else 0.
Standard library only.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_REPO = Path(__file__).resolve().parents[1]
MAX_LISTED = 25
MAX_FILES = 400
LAYER_RE = re.compile(r"\b(core\.[a-z_]+|modules\.[a-z_]+|app_shell|evals)\b")
EXT_RE = re.compile(r"\.(py|ya?ml|md|json|sh|toml|txt|lock|in|cfg|ini|c?js|ts|sql|html|css)$")
PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_.*\-]+(?:/[A-Za-z0-9_.*\-]*)+|[A-Za-z0-9_*\-]+\.[A-Za-z]{1,5}\b")
URL_RE = re.compile(r"https?://\S+")
NEGATION_RE = re.compile(
    r"\b(do not|don't|never|no new|must not|not import|without|avoid|instead of|rather than|forbid)\b", re.I)
REUSE_RE = re.compile(r"\b(reuse[sd]?|re-use|import(?:s|ed|ing)?|call(?:s|ing)?|delegate[sd]? to|use[sd]?|via)\b", re.I)
FAIL_RE = re.compile(r"\b(fail(?:s|ed|ing|ure)?|red|broken|errors?)\b", re.I)
OUTPUT_KW_RE = re.compile(
    r"\b(output|prints?|emits?|says|message|line|summary|logs?|reports?|expected|shows?|writes?|fail\w*|errors?)\b", re.I)
OUTPUT_NEG_RE = re.compile(r"\b(no|never|not|without|must not|absent|drop|suppress)\b", re.I)
SEARCH_DIRS = ("src", "scripts", "bin", ".github", "services", "deploy")
TEXT_SUFFIXES = {".py", ".sh", ".js", ".cjs", ".ts", ".yml", ".yaml", ".html", ".txt", ".md", ""}


# ---------------------------------------------------------------- findings

@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=lambda: {"FLAG": 0, "UNCHECKED": 0, "OK": 0})

    def add(self, level: str, check: str, message: str) -> None:
        if level in self.counts:
            self.counts[level] += 1
        self.lines.append(f"{level} ({check}) {message}")

    def render(self) -> str:
        c = self.counts
        tail = f"check_spec: {c['FLAG']} flagged, {c['UNCHECKED']} unchecked, {c['OK']} ok"
        return "\n".join([*self.lines, tail]) + "\n"


# ---------------------------------------------------------------- spec model

@dataclass
class Spec:
    path: Path
    lines: list[str]
    headings: list[str]  # heading in force for each line

    @classmethod
    def load(cls, path: Path) -> "Spec":
        lines = path.read_text(encoding="utf-8").splitlines()
        headings, current = [], ""
        for line in lines:
            if line.startswith("#"):
                current = line.lstrip("#").strip()
            headings.append(current)
        return cls(path, lines, headings)

    def approved_lines(self) -> list[str]:
        return [l for l in self.lines[:10] if "APPROVED" in l]

    def files_lines(self) -> list[str]:
        """Lines of every Files section: a '## Files' heading or a 'Files:' paragraph."""
        out: list[str] = []
        i = 0
        while i < len(self.lines):
            line = self.lines[i]
            if re.match(r"^#+\s*Files\b", line):
                i += 1
                while i < len(self.lines) and not self.lines[i].startswith("#"):
                    out.append(self.lines[i])
                    i += 1
                continue
            if re.match(r"^\s*(?:[-*]\s*)?\**Files\b[^:]*:", line):
                out.append(line)
                i += 1
                while i < len(self.lines) and self.lines[i].strip() and (
                        self.lines[i].lstrip().startswith(("-", "*")) or self.lines[i].startswith((" ", "\t"))):
                    out.append(self.lines[i])
                    i += 1
                continue
            i += 1
        return out


def path_tokens(text: str, repo: Path) -> list[str]:
    text = URL_RE.sub(" ", text)
    found = []
    for raw in PATH_TOKEN_RE.findall(text):
        tok = raw.lstrip(",;:()`'\"").rstrip(".,;:()`'\"")
        if raw.endswith("/") and not tok.endswith("/"):
            tok += "/"
        tok = tok.removeprefix("./")
        if not tok or tok.startswith("."*2):
            continue
        parts = [p for p in tok.split("/") if p]
        if not parts or any(p.isdigit() for p in parts):
            continue
        last = parts[-1]
        if tok.endswith("/") or EXT_RE.search(last) or (repo / tok).exists():
            if not any(ch.isalpha() for ch in tok):
                continue
            found.append(tok)
    return found


def covered(rel: str, allowed: list[str]) -> bool:
    for a in allowed:
        if a.endswith("/"):
            if rel.startswith(a):
                return True
        elif any(ch in a for ch in "*?["):
            if fnmatch.fnmatch(rel, a) or ("/" not in a and fnmatch.fnmatch(Path(rel).name, a)):
                return True
        elif "/" not in a:
            if Path(rel).name == a:
                return True
        elif rel == a.rstrip("/"):
            return True
    return False


def allowed_paths(spec: Spec, repo: Path, report: Report, workflows_named: list[str]) -> list[str]:
    files_text = "\n".join(spec.files_lines())
    from_files = path_tokens(files_text, repo)
    from_approved = path_tokens("\n".join(spec.approved_lines()), repo)
    allowed = list(dict.fromkeys(from_files + from_approved))
    if not from_files and re.search(r"workflow", files_text, re.I) and workflows_named:
        allowed.extend(w for w in workflows_named if w not in allowed)
        report.add("NOTE", "a", "Files list names no paths; read 'workflow files' as the workflows the spec names: "
                   + ", ".join(workflows_named))
    if not files_text.strip():
        report.add("NOTE", "a", "no Files section found in the spec")
    report.add("NOTE", "a", "allowed paths: " + (", ".join(allowed) if allowed else "(none)"))
    return allowed


# ---------------------------------------------------------------- workflows

@dataclass
class RunStep:
    workflow: str
    job: str
    step: str
    text: str

    def where(self) -> str:
        return f"{self.workflow} job '{self.job}' step '{self.step}'"


def parse_workflow(path: Path, rel: str) -> tuple[list[RunStep], list[str]]:
    """Line-based reader for GitHub Actions YAML: run: blocks and local uses:."""
    lines = path.read_text(encoding="utf-8").splitlines()
    runs: list[RunStep] = []
    local_uses: list[str] = []
    job, job_indent, in_jobs = "", None, False
    step: dict | None = None
    steps: list[dict] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if indent == 0:
            in_jobs = stripped.startswith("jobs:") or stripped == "runs:"
            job_indent = None
            if stripped == "runs:":
                job = "(composite)"
        elif in_jobs and job != "(composite)":
            m = re.match(r"^([\w.-]+):\s*$", stripped)
            if m and (job_indent is None or indent == job_indent):
                job_indent = indent
                job = m.group(1)
        if re.match(r"^-\s", stripped):
            step = {"job": job, "name": "", "runs": []}
            steps.append(step)
            stripped_item = stripped[1:].strip()
            key_indent = indent + 2
        else:
            stripped_item = stripped
            key_indent = indent
        m = re.match(r"^name:\s*(.*)$", stripped_item)
        if m and step is not None and step["job"] == job:
            step["name"] = m.group(1).strip().strip("'\"")
        m = re.match(r"^uses:\s*(\./\S+)", stripped_item)
        if m:
            local_uses.append(m.group(1).removeprefix("./").strip("'\""))
        m = re.match(r"^run:\s*(.*)$", stripped_item)
        if m:
            value = m.group(1).strip()
            body: list[str] = []
            if value[:1] in ("|", ">"):
                folded = value.startswith(">")
                i += 1
                while i < len(lines) and (not lines[i].strip() or
                                          len(lines[i]) - len(lines[i].lstrip(" ")) > key_indent):
                    body.append(lines[i].strip())
                    i += 1
                text = " ".join(body) if folded else "\n".join(body)
            else:
                text = value.strip("'\"")
                i += 1
            target = step if step is not None else {"job": job, "name": "", "runs": []}
            target["runs"].append(text)
            if step is None:
                steps.append(target)
            continue
        i += 1
    for s in steps:
        for text in s["runs"]:
            runs.append(RunStep(rel, s["job"], s["name"] or "(unnamed)", text))
    return runs, local_uses


def named_workflows(spec: Spec, repo: Path, extra: list[str]) -> list[str]:
    wf_dir = repo / ".github" / "workflows"
    if not wf_dir.is_dir():
        return []
    files = sorted(p for p in wf_dir.iterdir() if p.suffix in (".yml", ".yaml"))
    names = {}
    for p in files:
        m = re.search(r"(?m)^name:\s*(.+)$", p.read_text(encoding="utf-8"))
        if m:
            names[p] = m.group(1).strip().strip("'\"")
    found: list[str] = []
    text = "\n".join(spec.lines)
    for p in files:
        rel = p.relative_to(repo).as_posix()
        if re.search(r"(?<![\w-])" + re.escape(p.name) + r"\b", text):
            found.append(rel)
            continue
        name = names.get(p)
        if name and len(name) > 4:
            for line in spec.lines:
                if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", line, re.I) and FAIL_RE.search(line) \
                        and not re.search(r"\bgreen\b", line, re.I):
                    found.append(rel)
                    break
    for item in extra:
        cand = [p for p in files if item in (p.name, p.stem, p.relative_to(repo).as_posix(), names.get(p))]
        found.extend(p.relative_to(repo).as_posix() for p in cand)
        if not cand:
            found.append(item)
    return list(dict.fromkeys(found))


# ---------------------------------------------------------------- following run lines

def shell_lines(text: str) -> list[str]:
    joined = re.sub(r"\\\n\s*", " ", text)
    out = []
    for line in joined.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def split_tokens(line: str) -> list[str]:
    try:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return line.split()


def python_module_file(repo: Path, module: str, base: Path | None = None) -> Path | None:
    rel = Path(*module.split("."))
    roots = [r for r in (base, repo, repo / "src") if r is not None]
    for root in roots:
        for cand in (root / rel.with_suffix(".py"), root / rel / "__init__.py"):
            if cand.is_file() and repo in cand.resolve().parents:
                return cand
    return None


class Follower:
    """Collect every repo file a set of run lines executes, with provenance."""

    def __init__(self, repo: Path):
        self.repo = repo
        self.files: dict[str, str] = {}  # rel path -> why
        self.missing: dict[str, str] = {}
        self.invocations: list[tuple[str, list[str], str]] = []  # (script rel, tokens after script, where)
        self.queue: list[str] = []
        self.make_targets = self._read_make()

    def rel(self, path: Path) -> str | None:
        """Repo-relative path, or None when the file resolves outside the checkout (e.g. a symlink)."""
        try:
            path.resolve().relative_to(self.repo.resolve())
            return path.absolute().relative_to(self.repo.absolute()).as_posix()
        except ValueError:
            return None

    def add(self, rel: str | None, why: str) -> None:
        if rel and rel not in self.files and len(self.files) < MAX_FILES:
            self.files[rel] = why
            self.queue.append(rel)

    def _read_make(self) -> dict[str, tuple[list[str], list[str]]]:
        mk = self.repo / "Makefile"
        targets: dict[str, tuple[list[str], list[str]]] = {}
        if not mk.is_file():
            return targets
        current: list[str] = []
        vars_: dict[str, str] = {}
        for line in mk.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^([A-Za-z_][\w]*)\s*[:?+]?=\s*(.*)$", line)
            if m and not line.startswith("\t"):
                vars_.setdefault(m.group(1), m.group(2))
                continue
            m = re.match(r"^([^\t#=][^:=]*):(?!=)(.*)$", line)
            if m:
                names = m.group(1).split()
                prereq = m.group(2).split("#")[0].split()
                current = names
                for n in names:
                    targets.setdefault(n, ([], []))[0].extend(prereq)
                continue
            if line.startswith("\t") and current:
                recipe = re.sub(r"\$\(([A-Za-z_]\w*)\)", lambda mm: vars_.get(mm.group(1), mm.group(0)), line.strip())
                for n in current:
                    targets[n][1].append(recipe.lstrip("@-"))
            elif not line.startswith("\t"):
                current = []
        return targets

    def scan_run(self, text: str, where: str, depth: int = 0) -> None:
        for line in shell_lines(text):
            toks = split_tokens(line)
            for idx, tok in enumerate(toks):
                clean = tok.strip("'\"").removeprefix("./")
                if not clean or "$" in clean or "{" in clean:
                    continue
                if tok in ("make", "gmake") or tok.endswith("/make"):
                    self.add("Makefile", where)
                    for t in toks[idx + 1:]:
                        if t in ";&|()<>" or t.startswith("-") or "=" in t:
                            if t in ";&|()<>":
                                break
                            continue
                        self.scan_make(t, where, depth + 1)
                if tok == "-m" and idx + 1 < len(toks) and re.search(r"python", toks[idx - 1] if idx else ""):
                    mod = toks[idx + 1]
                    path = python_module_file(self.repo, mod)
                    if path is not None:
                        self.add(self.rel(path), f"{where} (python -m {mod})")
                    continue
                cand = self.repo / clean
                if "/" in clean or EXT_RE.search(clean):
                    if cand.is_file():
                        rel = self.rel(cand)
                        if rel is None:
                            continue
                        self.add(rel, where)
                        if rel.endswith(".py") and (idx == 0 or re.search(r"python[\d.]*$", toks[idx - 1])):
                            args = []
                            for t in toks[idx + 1:]:
                                if t in (";", "&&", "||", "|", "&", ")", ">", ">>", "<", "2>"):
                                    break
                                args.append(t)
                            self.invocations.append((rel, args, where))
                    elif "/" in clean and EXT_RE.search(clean) and not clean.startswith(("/", "~")):
                        self.missing.setdefault(clean, where)

    def scan_make(self, target: str, where: str, depth: int) -> None:
        if depth > 5 or target not in self.make_targets:
            return
        prereq, recipe = self.make_targets[target]
        for p in prereq:
            if (self.repo / p).is_file():
                self.add(p, f"{where} (make {target} prerequisite)")
            else:
                self.scan_make(p, where, depth + 1)
        self.scan_run("\n".join(recipe), f"{where} (make {target})", depth + 1)

    def expand(self) -> None:
        while self.queue:
            rel = self.queue.pop(0)
            path = self.repo / rel
            if rel.endswith(".py"):
                self.expand_python(path, rel)
            elif path.suffix in (".sh", "") and (rel.startswith(("bin/", "scripts/")) or path.suffix == ".sh"):
                try:
                    text = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                self.scan_run(text, f"called by {rel}")

    def expand_python(self, path: Path, rel: str) -> None:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            return
        base = path.parent
        for node in ast.walk(tree):
            mods: list[tuple[str, Path | None]] = []
            if isinstance(node, ast.Import):
                mods = [(a.name, base) for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    pkg = path.parent
                    for _ in range(node.level - 1):
                        pkg = pkg.parent
                    names = [node.module] if node.module else []
                    names += [f"{node.module}.{a.name}" if node.module else a.name for a in node.names]
                    for n in names:
                        p = python_module_file(self.repo, n, pkg)
                        if p is not None and pkg in p.parents:
                            self.add(self.rel(p), f"imported by {rel}")
                    continue
                mods = [(node.module or "", base)]
                mods += [(f"{node.module}.{a.name}", base) for a in node.names]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                s = node.value.strip()
                if re.fullmatch(r"[\w./-]+\.(py|sh)", s):
                    for cand in (self.repo / s, base / s):
                        if cand.is_file() and self.repo.resolve() in cand.resolve().parents:
                            self.add(self.rel(cand), f"loaded by path from {rel}")
                            break
            for name, b in mods:
                if not name or name.split(".")[0] in sys.stdlib_module_names:
                    continue
                p = python_module_file(self.repo, name, b)
                if p is not None:
                    self.add(self.rel(p), f"imported by {rel}")


# ---------------------------------------------------------------- argparse (call args)

@dataclass
class ArgSpec:
    options: set[str]
    required: set[str]
    allow_abbrev: bool
    unchecked: str = ""


def argparse_spec(path: Path) -> ArgSpec | None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, OSError):
        return None
    if "ArgumentParser" not in ast.dump(tree):
        return None
    spec = ArgSpec(set(), set(), True)
    loops: dict[int, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name) and isinstance(node.iter, (ast.Tuple, ast.List)):
            values = [e.value for e in node.iter.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if len(values) == len(node.iter.elts):
                for inner in ast.walk(node):
                    loops[id(inner)] = values if inner is not node else loops.get(id(inner), [])
                    if isinstance(inner, ast.Call):
                        inner._loop_var = node.target.id  # type: ignore[attr-defined]
                        inner._loop_vals = values  # type: ignore[attr-defined]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        attr = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if attr == "ArgumentParser":
            for kw in node.keywords:
                if kw.arg == "allow_abbrev" and isinstance(kw.value, ast.Constant):
                    spec.allow_abbrev = bool(kw.value.value)
        if attr in ("add_subparsers", "parse_known_args"):
            spec.unchecked = f"uses {attr}"
        if attr != "add_argument":
            continue
        names: list[str] = []
        for a in node.args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                names.append(a.value)
            elif isinstance(a, ast.BinOp) and isinstance(a.op, ast.Add) and isinstance(a.left, ast.Constant) \
                    and isinstance(a.right, ast.Name) and getattr(node, "_loop_var", None) == a.right.id:
                names.extend(a.left.value + v for v in node._loop_vals)  # type: ignore[attr-defined]
            else:
                spec.unchecked = f"add_argument with a computed name at line {node.lineno}"
        kws = {kw.arg: kw.value for kw in node.keywords}
        req = kws.get("required")
        required = isinstance(req, ast.Constant) and req.value is True
        opts = [n for n in names if n.startswith("-")]
        spec.options.update(opts)
        if required:
            spec.required.update(n for n in opts if n.startswith("--"))
    return spec


def check_call_args(follower: Follower, report: Report) -> None:
    seen = set()
    for rel, args, where in follower.invocations:
        key = (rel, tuple(args), where)
        if key in seen:
            continue
        seen.add(key)
        spec = argparse_spec(follower.repo / rel)
        if spec is None:
            continue
        if spec.unchecked:
            report.add("UNCHECKED", "c call-args", f"{rel} called from {where}: argparse not readable statically ({spec.unchecked})")
            continue
        if any(a in ('"$@"', "$@", "$*") or a.startswith("${") and a.endswith("[@]}") for a in args):
            report.add("UNCHECKED", "c call-args", f"{rel} called from {where}: arguments come from a shell expansion")
            continue
        passed = [a.split("=", 1)[0] for a in args if a.startswith("--") and len(a) > 2]
        longs = sorted(o for o in spec.options if o.startswith("--"))
        resolved, unknown = set(), []
        for p in passed:
            if p in spec.options:
                resolved.add(p)
                continue
            pref = [o for o in longs if o.startswith(p)] if spec.allow_abbrev else []
            if len(pref) == 1:
                resolved.add(pref[0])
            else:
                unknown.append(p)
        missing = sorted(spec.required - resolved)
        if missing or unknown:
            parts = []
            if missing:
                parts.append("missing required " + ", ".join(missing))
            if unknown:
                parts.append("unrecognized " + ", ".join(dict.fromkeys(unknown)))
            report.add("FLAG", "c call-args",
                       f"{rel} called from {where} would exit 2 on main (argparse): " + "; ".join(parts))
        else:
            report.add("OK", "c call-args", f"{rel} called from {where}: options match argparse on main (static)")


# ---------------------------------------------------------------- check (a)

def check_allowed_paths(spec: Spec, repo: Path, extra_checks: list[str], report: Report) -> None:
    workflows = named_workflows(spec, repo, extra_checks)
    allowed = allowed_paths(spec, repo, report, workflows)
    if not workflows:
        report.add("UNCHECKED", "a", "the spec names no workflow; nothing to follow. Name one with --check "
                   "if a CI check is the failing target")
        return
    follower = Follower(repo)
    for wf in workflows:
        path = repo / wf
        if not path.is_file():
            report.add("UNCHECKED", "a", f"named check {wf} not found under .github/workflows on main")
            continue
        follower.add(wf, "named failing check")
        runs, uses = parse_workflow(path, wf)
        jobs = sorted({r.job for r in runs})
        text = "\n".join(spec.lines)
        named_jobs = [j for j in jobs if len(jobs) > 1 and re.search(
            r"(`" + re.escape(j) + r"`|/\s*" + re.escape(j) + r"\b|\bjob\s+['\"`]?" + re.escape(j) + r"\b)", text)]
        if named_jobs:
            report.add("NOTE", "a", f"{wf}: following only the job(s) the spec names: {', '.join(named_jobs)}")
            runs = [r for r in runs if r.job in named_jobs]
        for step in runs:
            follower.scan_run(step.text, step.where())
        for u in uses:
            for name in ("action.yml", "action.yaml"):
                act = repo / u / name
                if act.is_file():
                    rel = act.relative_to(repo).as_posix()
                    follower.add(rel, f"{wf} uses ./{u}")
                    for step in parse_workflow(act, rel)[0]:
                        follower.scan_run(step.text, step.where())
    follower.expand()
    if len(follower.files) >= MAX_FILES:
        report.add("NOTE", "a", f"stopped following after {MAX_FILES} files; the list below is partial")
    outside_files = [(rel, why) for rel, why in follower.files.items() if not covered(rel, allowed)]
    outside = len(outside_files)
    for rel, why in outside_files[:MAX_LISTED]:
        report.add("FLAG", "a", f"{rel} is outside the allowed paths; reached from {why}")
    if outside > MAX_LISTED:
        rest = [rel for rel, _ in outside_files[MAX_LISTED:]]
        report.add("FLAG", "a", f"{len(rest)} more files outside the allowed paths: {', '.join(rest)}")
    for rel, why in follower.missing.items():
        report.add("NOTE", "a", f"{rel} is referenced by {why} but is missing on main")
    inside = len(follower.files) - outside
    report.add("OK" if not outside else "NOTE", "a",
               f"{inside} of {len(follower.files)} files run by {', '.join(workflows)} are inside the allowed paths")
    check_call_args(follower, report)


# ---------------------------------------------------------------- check (b)

def find_layer_map(repo: Path) -> Path | None:
    preferred = repo / "tests" / "snapshots" / "import_layers.json"
    if preferred.is_file():
        return preferred
    for p in sorted(repo.rglob("import_layers*.json")):
        if ".git" not in p.parts and "node_modules" not in p.parts:
            return p
    return None


class RuleNotInterpretable(ValueError):
    def __init__(self):
        super().__init__("rule not statically interpretable (unsupported syntax; use load_forbidden's bounded rule grammar)")


class StaticForbidden:
    """Validate the entire rule, then interpret only the bounded rule grammar."""

    def __init__(self, node: ast.FunctionDef):
        args = node.args
        if (node.decorator_list or args.posonlyargs or args.kwonlyargs or
                args.vararg or args.kwarg or args.defaults or args.kw_defaults or
                getattr(node, "type_params", []) or node.type_comment or
                [arg.arg for arg in args.args] != ["source", "target", "layers"] or
                len(list(ast.walk(node))) > 256):
            raise RuleNotInterpretable()
        annotations = [ast.parse(text, mode="eval").body for text in
                       ("str", "str", "dict[str, str]", "bool")]
        for actual, expected in zip([arg.annotation for arg in args.args] + [node.returns], annotations):
            if actual is not None and ast.dump(actual) != ast.dump(expected):
                raise RuleNotInterpretable()
        if any(arg.type_comment for arg in args.args):
            raise RuleNotInterpretable()
        self.names = {"source", "target"}
        self.bindings = []
        body = list(node.body)
        while body and isinstance(body[0], ast.Assign):
            assignment = body.pop(0)
            if (len(assignment.targets) != 1 or
                    not isinstance(assignment.targets[0], ast.Name) or
                    assignment.targets[0].id in self.names | {"layers", "AssertionError"} or
                    assignment.type_comment or not isinstance(assignment.value, ast.Subscript)):
                raise RuleNotInterpretable()
            value = self.expression(assignment.value)
            name = assignment.targets[0].id
            self.bindings.append((name, value))
            self.names.add(name)
        self.body = self.statements(body)

    def expression(self, node):
        if isinstance(node, ast.Constant) and type(node.value) in (str, bool):
            return ("literal", node.value)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in self.names:
            return ("name", node.id)
        if (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) and
                isinstance(node.value, ast.Name) and node.value.id == "layers" and
                isinstance(node.slice, ast.Name) and node.slice.id in {"source", "target"}):
            return ("layer", node.slice.id)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return ("not", self.expression(node.operand))
        if isinstance(node, ast.BoolOp) and type(node.op) in (ast.And, ast.Or):
            return ("and" if isinstance(node.op, ast.And) else "or",
                    [self.expression(value) for value in node.values])
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left = self.expression(node.left)
            right = node.comparators[0]
            if isinstance(node.ops[0], ast.Eq):
                return ("eq", left, self.expression(right))
            if (type(node.ops[0]) in (ast.In, ast.NotIn) and
                    type(right) in (ast.Set, ast.Tuple) and
                    all(isinstance(item, ast.Constant) and type(item.value) in (str, bool)
                        for item in right.elts)):
                return ("in" if isinstance(node.ops[0], ast.In) else "not_in",
                        left, tuple(item.value for item in right.elts))
        if (isinstance(node, ast.Call) and not node.keywords and len(node.args) == 1 and
                isinstance(node.func, ast.Attribute) and node.func.attr == "startswith" and
                isinstance(node.func.value, ast.Name) and node.func.value.id in self.names and
                isinstance(node.args[0], ast.Constant) and type(node.args[0].value) is str):
            return ("startswith", node.func.value.id, node.args[0].value)
        raise RuleNotInterpretable()

    def assertion_message(self, node):
        if isinstance(node, ast.Constant) and type(node.value) is str:
            return
        if isinstance(node, ast.Name) and node.id in self.names:
            return
        if isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.Constant) and type(part.value) is str:
                    continue
                if (isinstance(part, ast.FormattedValue) and part.conversion == -1 and
                        part.format_spec is None and isinstance(part.value, ast.Name) and
                        part.value.id in self.names):
                    continue
                raise RuleNotInterpretable()
            return
        raise RuleNotInterpretable()

    def statements(self, body):
        result = []
        for node in body:
            if isinstance(node, ast.If):
                result.append(("if", self.expression(node.test),
                               self.statements(node.body), self.statements(node.orelse)))
            elif isinstance(node, ast.Return):
                result.append(("return", self.expression(node.value)))
            elif isinstance(node, ast.Raise) and node.cause is None:
                exc = node.exc
                if isinstance(exc, ast.Name) and exc.id == "AssertionError":
                    result.append(("raise",))
                elif (isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name) and
                      exc.func.id == "AssertionError" and not exc.keywords and len(exc.args) <= 1):
                    for message in exc.args:
                        self.assertion_message(message)
                    result.append(("raise",))
                else:
                    raise RuleNotInterpretable()
            else:
                raise RuleNotInterpretable()
        return result

    def value(self, expression, names, layers):
        kind, *args = expression
        if kind == "literal":
            return args[0]
        if kind == "name":
            return names[args[0]]
        if kind == "layer":
            return layers[names[args[0]]]
        if kind == "not":
            return not self.value(args[0], names, layers)
        if kind in {"and", "or"}:
            for child in args[0]:
                value = self.value(child, names, layers)
                if (kind == "and" and not value) or (kind == "or" and value):
                    return value
            return value
        if kind == "eq":
            return self.value(args[0], names, layers) == self.value(args[1], names, layers)
        if kind in {"in", "not_in"}:
            found = self.value(args[0], names, layers) in args[1]
            return found if kind == "in" else not found
        if kind == "startswith":
            return names[args[0]].startswith(args[1])
        raise RuleNotInterpretable()

    def apply(self, body, names, layers):
        for statement in body:
            if statement[0] == "return":
                return True, self.value(statement[1], names, layers)
            if statement[0] == "raise":
                raise AssertionError("unknown import layer")
            branch = statement[2] if self.value(statement[1], names, layers) else statement[3]
            returned, value = self.apply(branch, names, layers)
            if returned:
                return True, value
        return False, None

    def __call__(self, source, target, layers):
        names = {"source": source, "target": target}
        for name, expression in self.bindings:
            names[name] = self.value(expression, names, layers)
        returned, value = self.apply(self.body, names, layers)
        if not returned or type(value) is not bool:
            raise RuleNotInterpretable()
        return value


def load_forbidden(repo: Path, layer_map: Path):
    """Read the rule as data; never import or execute the inspected test module."""
    tests = repo / "tests"
    for p in sorted(tests.rglob("*.py")) if tests.is_dir() else []:
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if layer_map.name not in text:
            continue
        try:
            tree = ast.parse(text)
        except (SyntaxError, RecursionError):
            return None, p.relative_to(repo).as_posix()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_forbidden":
                rel = p.relative_to(repo).as_posix()
                try:
                    if not isinstance(node, ast.FunctionDef):
                        raise RuleNotInterpretable()
                    return StaticForbidden(node), rel
                except (RuleNotInterpretable, RecursionError):
                    return None, rel
    return None, None


def module_index(layers: dict[str, str]) -> dict[str, list[str]]:
    idx: dict[str, list[str]] = {}
    for mod in layers:
        idx.setdefault(mod.rsplit(".", 1)[-1], []).append(mod)
        idx.setdefault(mod, []).append(mod)
    return idx


def refs_to_modules(text: str, layers: dict[str, str], pkg_roots: list[str]) -> list[str]:
    idx = module_index(layers)
    out: list[str] = []
    for tok in re.findall(r"`([^`]+)`|\b([\w./]+\.py)\b|\b((?:%s)\.[\w.]+)\b" % "|".join(map(re.escape, pkg_roots) or ["x^"]), text):
        raw = next(t for t in tok if t).strip()
        raw = raw.split("(")[0].strip()
        if raw.endswith(".py"):
            raw = raw[:-3]
        raw = raw.replace("/", ".")
        for root in ("src.",):
            raw = raw.removeprefix(root)
        cands = idx.get(raw) or idx.get(raw.rsplit(".", 1)[-1]) or []
        if not cands and "." in raw:
            # module.function form, e.g. bates_gaps.compute_bates_gaps
            head = raw.rsplit(".", 1)[0]
            cands = idx.get(head) or idx.get(head.rsplit(".", 1)[-1]) or []
        if len(cands) == 1:
            out.append(cands[0])
    return list(dict.fromkeys(out))


def check_import_boundary(spec: Spec, repo: Path, report: Report) -> None:
    layer_map = find_layer_map(repo)
    if layer_map is None:
        report.add("UNCHECKED", "b", "no import layer map found on main (looked for import_layers*.json)")
        return
    layers: dict[str, str] = json.loads(layer_map.read_text(encoding="utf-8"))
    map_rel = layer_map.relative_to(repo).as_posix()
    forbidden, rule_src = load_forbidden(repo, layer_map)
    if forbidden is None:
        if rule_src:
            report.add("UNCHECKED", "b", f"{rule_src}: {RuleNotInterpretable()}")
        else:
            report.add("UNCHECKED", "b", f"found {map_rel} but no _forbidden rule in a test that reads it")
        return
    baseline_path = layer_map.parent / "import_graph.json"
    baseline = set()
    if baseline_path.is_file():
        baseline = set(json.loads(baseline_path.read_text(encoding="utf-8")).get("forbidden_edges", []))
    report.add("NOTE", "b", f"ratchet on main: map {map_rel}, rule {rule_src}, baseline "
               f"{baseline_path.relative_to(repo).as_posix() if baseline_path.is_file() else '(none)'}")
    pkg_roots = sorted({m.split(".")[0] for m in layers})

    # Modules the spec creates or changes, with their layer.
    files_lines = spec.files_lines()
    allowed = path_tokens("\n".join(files_lines + spec.approved_lines()), repo)
    importers: dict[str, str | None] = {}
    for line in files_lines:
        for tok in path_tokens(line, repo):
            m = re.match(r"^src/(.+)\.py$", tok)
            if not m:
                continue
            mod = m.group(1).replace("/", ".").removesuffix(".__init__")
            if mod.split(".")[0] not in pkg_roots:
                continue
            if mod in layers:
                importers[mod] = layers[mod]
                continue
            if (repo / tok).is_file():
                report.add("NOTE", "b", f"{mod} exists on main but is not in {map_rel}; the ratchet on main "
                           "should already be failing")
                importers[mod] = infer_layer(mod, layers, report)
                continue
            lm = LAYER_RE.search(line)
            if lm:
                importers[mod] = lm.group(1)
                report.add("NOTE", "b", f"{mod} is new; layer {lm.group(1)} taken from the spec")
            else:
                importers[mod] = infer_layer(mod, layers, report)
            if not covered(map_rel, allowed):
                report.add("FLAG", "b", f"{mod} is a new module, and the ratchet on main requires every module "
                           f"in {map_rel}; that file is outside the allowed paths")
            else:
                report.add("OK", "b", f"{mod} is new and {map_rel} is inside the allowed paths")

    prescriptions = []
    for n, (line, heading) in enumerate(zip(spec.lines, spec.headings), 1):
        if line.startswith("#") or NEGATION_RE.search(line):
            continue
        if not REUSE_RE.search(line) and not re.search(r"reuse", heading, re.I):
            continue
        targets = refs_to_modules(line, layers, pkg_roots)
        if not targets:
            continue
        background = re.search(r"prior work|background|reference|outline|later", heading, re.I) is not None
        prescriptions.append((n, line, targets, background))

    if not prescriptions:
        report.add("NOTE", "b", "the spec prescribes no reuse or import of a mapped module")
        return
    if not importers:
        report.add("UNCHECKED", "b", "the spec prescribes reuse but its Files list names no package module to import from: "
                   + ", ".join(t for _, _, ts, _ in prescriptions for t in ts))
        return
    for n, line, targets, background in prescriptions:
        named_src = [m for m in importers if re.search(r"\b%s\b" % re.escape(m.rsplit(".", 1)[-1]), line)]
        sources = named_src or list(importers)
        for src in sources:
            for tgt in targets:
                if tgt == src:
                    continue
                edge = f"{src} -> {tgt}"
                src_layer = importers[src]
                if src_layer is None:
                    report.add("UNCHECKED", "b", f"line {n}: {edge}: layer of {src} unknown")
                    continue
                probe = dict(layers)
                probe[src] = src_layer
                try:
                    bad = forbidden(src, tgt, probe)
                except Exception as error:  # rule raised: unknown layer
                    report.add("UNCHECKED", "b", f"line {n}: {edge}: rule raised {error!r}")
                    continue
                desc = f"{edge} ({src_layer} -> {layers[tgt]})"
                if bad and edge in baseline:
                    report.add("OK", "b", f"line {n}: {desc} is forbidden but already in the baseline")
                elif bad and background:
                    report.add("NOTE", "b", f"line {n}: {desc} would break the ratchet if imported; listed under "
                               f"'{spec.headings[n - 1]}', not prescribed")
                elif bad:
                    report.add("FLAG", "b", f"line {n}: prescribed reuse {desc} breaks the import-boundary ratchet "
                               f"on main: ...{excerpt(line, tgt)}...")
                elif not background:
                    report.add("OK", "b", f"line {n}: {desc} is allowed")


def excerpt(line: str, module: str) -> str:
    short = module.rsplit(".", 1)[-1]
    pos = line.find(short)
    if pos < 0:
        return line.strip()[:120]
    return line[max(0, pos - 70):pos + len(short) + 30].strip()


def infer_layer(mod: str, layers: dict[str, str], report: Report) -> str | None:
    name = mod.rsplit(".", 1)[-1]
    prefix = name.split("_")[0]
    peers = [layers[m] for m in layers if m.rsplit(".", 1)[-1].startswith(prefix + "_") or m.rsplit(".", 1)[-1] == prefix]
    if not peers:
        report.add("UNCHECKED", "b", f"{mod} is new and its layer can't be inferred; state it in the Files line")
        return None
    best = max(set(peers), key=peers.count)
    if peers.count(best) / len(peers) >= 0.75:
        report.add("NOTE", "b", f"{mod} is new; layer {best} inferred from {len(peers)} '{prefix}_*' modules")
        return best
    report.add("UNCHECKED", "b", f"{mod} is new and '{prefix}_*' modules span layers {sorted(set(peers))}")
    return None


# ---------------------------------------------------------------- check (c) expected output

def search_literal(repo: Path, needle: str) -> list[str]:
    hits = []
    for d in SEARCH_DIRS:
        root = repo / d
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix not in TEXT_SUFFIXES or "node_modules" in p.parts:
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            pos = text.find(needle)
            if pos >= 0:
                hits.append(f"{p.relative_to(repo).as_posix()}:{text.count(chr(10), 0, pos) + 1}")
                if len(hits) >= 3:
                    return hits
    return hits


def check_expected_output(spec: Spec, repo: Path, report: Report) -> None:
    found_any = False
    for n, line in enumerate(spec.lines, 1):
        if line.startswith("#") or not OUTPUT_KW_RE.search(line):
            continue
        for m in re.finditer(r'"([^"\n]{6,})"|“([^”\n]{6,})”', line):
            quote = m.group(1) or m.group(2)
            if " " not in quote.strip() or quote.strip().endswith(" yes"):
                continue
            if re.fullmatch(r"\d+ (yes|no)\b.*", quote.strip()):
                continue
            found_any = True
            before = line[:m.start()]
            negative = bool(OUTPUT_NEG_RE.search(before[-40:]))
            needle = re.split(r"\{|<|\.\.\.|%s|%d", quote)[0].strip()
            if len(needle) < 6:
                report.add("UNCHECKED", "c", f'line {n}: "{quote}" has too little fixed text to search for')
                continue
            hits = search_literal(repo, needle)
            if negative and hits:
                report.add("FLAG", "c", f'line {n}: spec expects no "{quote}", but main emits that text at '
                           f"{', '.join(hits)} (static match; confirm the code path)")
            elif negative:
                report.add("OK", "c", f'line {n}: "{quote}" expected absent; no such literal on main (static, not executed)')
            elif hits:
                report.add("OK", "c", f'line {n}: "{quote}" exists on main at {", ".join(hits)} (static, not executed)')
            else:
                report.add("UNCHECKED", "c", f'line {n}: "{quote}" not found as a literal on main; new output, '
                           "external text or built at runtime, so not checked")
    if not found_any:
        report.add("NOTE", "c", "no quoted expected-output lines found in the spec")


# ---------------------------------------------------------------- main

# ---------------------------------------------------------------- design gate

NUM_ITEM_RE = re.compile(r"^\s*(\d+)[.)]\s+\S")
MEASURE_RE = re.compile(
    r"\d|\b(zero|exactly|every|none|at most|at least|identical|empty|unchanged)\b", re.I)
COUNT_RE = re.compile(
    r"\b\d+\s+(passed|failed|failures?|errors?|skipped|ok|green|red)\b|\bexit(?:\s+code)?\s*[=:]?\s*\d+\b", re.I)
NOT_RUN_RE = re.compile(r"\bNOT RUN\b")
CMD_RE = re.compile(r"`[^`\n]{4,}`|^\s*(?:\$|python3?|pytest|git|grep|rg|make|bash|\./)\s?\S", re.M)
GREP_ALIASES = {"grep": [], "egrep": ["-E"], "fgrep": ["-F"], "rg": ["-E"]}
GREP_FLAGS_KEPT = {"-E", "-i", "-F", "-w"}
GREP_PLAIN_SHORT = set("rRnlHIEiFwcshq")   # short options that take no value and do not change the file scope
GREP_PLAIN_LONG = {"--recursive", "--line-number", "--files-with-matches", "--no-color", "--color=never", "--ignore-case"}
MAX_FENCE_MATCHES = 40
MAX_GREP_COMMANDS = 20


def section_text(spec: Spec, heading_re: str) -> list[str]:
    """Lines under any heading matching heading_re, including its subheadings."""
    rx = re.compile(heading_re, re.I)
    out: list[str] = []
    stack: list[tuple[int, str]] = []
    for line in spec.lines:
        m = re.match(r"^(#+)\s*(.*)$", line)
        if m:
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, m.group(2)))
            continue
        if any(rx.search(h) for _, h in stack):
            out.append(line)
    return out


def design_acceptance(spec: Spec, report: Report) -> None:
    body = section_text(spec, r"acceptance")
    items = [l for l in body if NUM_ITEM_RE.match(l)]
    if len(items) < 2:
        report.add("FLAG", "d", f"no numbered acceptance list (found {len(items)} numbered items under an "
                   "'acceptance' heading; need at least 2)")
        return
    report.add("OK", "d", f"numbered acceptance list with {len(items)} items")
    vague = [l.strip() for l in items if not MEASURE_RE.search(re.sub(r"^\s*\d+[.)]\s+", "", l))]
    for l in vague[:5]:
        report.add("UNCHECKED", "d", "acceptance item has no number or measurable word: " + l[:100])


def design_dry_run(spec: Spec, report: Report) -> None:
    body = section_text(spec, r"dry.?run|frozen")
    text = "\n".join(body)
    if not text.strip():
        report.add("FLAG", "e", "no dry-run section (heading with 'dry run' or 'frozen tests')")
        return
    if not CMD_RE.search(text):
        report.add("FLAG", "e", "dry-run section records no command")
        return
    if COUNT_RE.search(text):
        report.add("OK", "e", "dry run records a command and a result")
    elif NOT_RUN_RE.search(text):
        report.add("UNCHECKED", "e", "dry run is marked NOT RUN; the frozen tests were not run")
    else:
        report.add("FLAG", "e", "dry-run section records a command but no result (need 'N passed', "
                   "'N failed' or 'exit N')")


def _split_command(span: str) -> list[str] | None:
    try:
        toks = shlex.split(span)
    except ValueError:
        return None
    for n, t in enumerate(toks):
        if t in ("|", "||", "&&", ";", ">", ">>", "2>&1"):
            toks = toks[:n]
            break
    if toks[:2] == ["git", "grep"]:
        toks = toks[1:]
    return toks if toks and toks[0] in GREP_ALIASES else None


def grep_commands(lines: list[str]) -> tuple[list[list[str]], list[str]]:
    """Grep commands found in the lines, and the lines with those commands removed."""
    cmds: list[list[str]] = []
    rest: list[str] = []
    for line in lines:
        spans = re.findall(r"`([^`\n]+)`", line)
        if spans:
            found = [t for t in map(_split_command, spans) if t]
            if found:
                cmds += found
                for sp in spans:
                    if _split_command(sp):
                        line = line.replace(f"`{sp}`", " ")
            rest.append(line)
            continue
        t = _split_command(line.strip().lstrip("-*$ ").strip())
        if t:
            cmds.append(t)
        else:
            rest.append(line)
    return cmds, rest


def grep_pattern_and_paths(toks: list[str]) -> tuple[str | None, list[str], list[str], str]:
    """(pattern, flags, paths, problem). problem is non-empty when the command cannot be re-run faithfully."""
    flags: list[str] = list(GREP_ALIASES[toks[0]])
    pats: list[str] = []
    paths: list[str] = []
    i = 1
    while i < len(toks):
        t = toks[i]
        if t == "--":
            paths += toks[i + 1:]
            break
        if t in ("-e", "--regexp") and i + 1 < len(toks):
            pats.append(toks[i + 1])
            i += 2
            continue
        if t.startswith("--"):
            if t not in GREP_PLAIN_LONG:
                return None, flags, paths, f"unsupported option {t}"
            i += 1
            continue
        if t.startswith("-") and len(t) > 1:
            for ch in t[1:]:
                if ch not in GREP_PLAIN_SHORT:
                    return None, flags, paths, f"unsupported option -{ch}"
                if f"-{ch}" in GREP_FLAGS_KEPT:
                    flags.append(f"-{ch}")
            i += 1
            continue
        if not pats:
            pats.append(t)
        else:
            paths.append(t)
        i += 1
    if len(pats) > 1:
        return None, flags, paths, "more than one -e pattern"
    return (pats[0] if pats else None), list(dict.fromkeys(flags)), paths, ""


def design_fence(spec: Spec, repo: Path, report: Report) -> None:
    import subprocess
    body = section_text(spec, r"fence")
    if not "\n".join(body).strip():
        report.add("FLAG", "f", "no fence section (heading with 'fence')")
        return
    cmds, rest = grep_commands(body)
    text = "\n".join(rest)   # the fence's own words with the grep commands removed, so a command cannot name its own matches
    fence_paths = path_tokens(text, repo)
    if not fence_paths:
        report.add("FLAG", "f", "fence section names no file paths")
    if not cmds:
        report.add("FLAG", "f", "fence section shows no grep command it was derived from")
        return
    if not (repo / ".git").exists():
        report.add("UNCHECKED", "f", f"{repo} is not a git checkout; grep was not re-run")
        return
    for toks in cmds[:MAX_GREP_COMMANDS]:
        pat, flags, paths, problem = grep_pattern_and_paths(toks)
        shown = " ".join(toks)[:90]
        if problem or pat is None:
            report.add("UNCHECKED", "f", f"grep not re-run ({problem or 'no pattern'}): {shown}")
            continue
        bad = [p for p in paths if p.startswith("/") or ".." in p.split("/") or p.startswith(":")]
        if bad:
            report.add("UNCHECKED", "f", f"grep not re-run (path outside the repo or pathspec magic: {bad[0]}): {shown}")
            continue
        scope = [p for p in paths if p != "|"] or ["."]
        cmd = ["git", "-C", str(repo), "grep", "-l", "-I", "--untracked", *flags, "-e", pat, "--", *scope]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            report.add("UNCHECKED", "f", f"grep could not run ({type(exc).__name__}): {shown}")
            continue
        if res.returncode == 1:
            report.add("UNCHECKED", "f", f"grep matches nothing on {repo.name}: {shown}")
            continue
        if res.returncode != 0:
            report.add("UNCHECKED", "f", f"grep could not run ({res.stderr.strip()[:80]}): {shown}")
            continue
        matched = [m for m in res.stdout.splitlines() if m]
        by_name: dict[str, int] = {}
        for m in matched:
            by_name[Path(m).name] = by_name.get(Path(m).name, 0) + 1

        def named(m: str) -> bool:
            if covered(m, fence_paths):
                return True
            name = Path(m).name   # a bare file name counts only as a whole token and only when it is unambiguous
            return by_name[name] == 1 and re.search(r"(?<![\w/.\-])" + re.escape(name) + r"(?![\w\-])", text) is not None

        missing = [m for m in matched if not named(m)]
        if not missing:
            report.add("OK", "f", f"every file matched by `{shown}` ({len(matched)}) is named in the fence")
        else:
            listed = ", ".join(missing[:MAX_LISTED]) + (" ..." if len(missing) > MAX_LISTED else "")
            report.add("FLAG", "f", f"fence omits {len(missing)} file(s) matched by `{shown}`: {listed}")
    if len(cmds) > MAX_GREP_COMMANDS:
        report.add("UNCHECKED", "f", f"{len(cmds) - MAX_GREP_COMMANDS} further grep commands were not re-run (limit {MAX_GREP_COMMANDS})")


def check_design_gate(spec: Spec, repo: Path, report: Report) -> None:
    design_acceptance(spec, report)
    design_dry_run(spec, report)
    design_fence(spec, repo, report)


def selftest() -> int:
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        (repo / "tests").mkdir(parents=True)
        (repo / "src").mkdir()
        (repo / "src" / "a.py").write_text("conn.executescript('x')\n")
        (repo / "tests" / "b.py").write_text("conn.executescript('y')\n")
        (repo / "tests" / "c.py").write_text("print('clean')\n")
        try:
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, timeout=60)
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"FAIL selftest setup ({type(exc).__name__})")
            print("selftest: 0 passed, 1 failed")
            return 1
        good = (
            "# Note\n\n## Acceptance\n1. 0 failures across 8 runs\n2. tests/b.py has 0 raw calls\n\n"
            "## Dry run, recorded\n`pytest -q tests/c.py` -> 3 passed in 1.2s\n\n"
            "## Fence\nDerived with `grep -rl executescript src tests`:\n- src/a.py\n- tests/b.py\n")
        nofence_b = good.replace("- tests/b.py\n", "")
        # (name, note text, wanted level, substring of the wanted line or None for "no FLAG")
        cases = [
            ("good note has no flags", good, None, None),
            ("missing acceptance list", good.replace("1. 0 failures across 8 runs\n2. tests/b.py has 0 raw calls\n", "prose only\n"), "FLAG", "(d)"),
            ("single acceptance item", good.replace("2. tests/b.py has 0 raw calls\n", ""), "FLAG", "(d)"),
            ("vague acceptance item", good.replace("2. tests/b.py has 0 raw calls", "2. make it better"), "UNCHECKED", "no number or measurable"),
            ("acceptance items under a subheading", good.replace("## Acceptance\n", "## Acceptance\n### Bars\n"), None, None),
            ("missing dry run", good.replace("## Dry run, recorded\n`pytest -q tests/c.py` -> 3 passed in 1.2s\n", ""), "FLAG", "(e)"),
            ("dry run without a result", good.replace("-> 3 passed in 1.2s", "(ran it)"), "FLAG", "(e)"),
            ("dry run marked NOT RUN", good.replace("-> 3 passed in 1.2s", "-> NOT RUN, no pytest"), "UNCHECKED", "NOT RUN"),
            ("dry run without a command", good.replace("`pytest -q tests/c.py` -> 3 passed in 1.2s", "3 passed"), "FLAG", "(e)"),
            ("fence omits a matched file", nofence_b, "FLAG", "fence omits"),
            ("fence omits a file when the grep is piped", nofence_b.replace("`grep -rl executescript src tests`", "`grep -rl executescript src tests | wc -l`"), "FLAG", "fence omits"),
            ("grep with directory slashes cannot name its own matches", nofence_b.replace("src tests`", "src/ tests/`"), "FLAG", "fence omits"),
            ("bare basename of another directory does not cover a file", nofence_b.replace("- src/a.py\n", "- src/a.py\n- docs/b.py\n"), "FLAG", "fence omits"),
            ("fence without a grep command", good.replace("`grep -rl executescript src tests`", "reading"), "FLAG", "(f)"),
            ("fence naming no paths", good.replace("- src/a.py\n- tests/b.py\n", ""), "FLAG", "names no file paths"),
            ("unsupported grep option is not re-run", good.replace("-rl executescript", "-rl -A 3 executescript"), "UNCHECKED", "unsupported option"),
            ("path outside the repo is not re-run", good.replace("src tests`", "src ../x`"), "UNCHECKED", "outside the repo"),
            ("pattern starting with a dash is inert", good.replace("-rl executescript src tests", "-rl -e -x -- src tests"), "UNCHECKED", "matches nothing"),
            ("grep that matches nothing", good.replace("executescript src", "nosuchsymbol src"), "UNCHECKED", "matches nothing"),
        ]
        failed = 0
        for name, text, level, needle in cases:
            note = Path(tmp) / "note.md"
            note.write_text(text)
            rep = Report()
            check_design_gate(Spec.load(note), repo, rep)
            if level is None:
                ok = rep.counts["FLAG"] == 0
            else:
                ok = any(l.startswith(level) and needle in l for l in rep.lines)
            print(("PASS " if ok else "FAIL ") + name)
            if not ok:
                failed += 1
                print("\n".join("    " + l for l in rep.lines))
        non_git = Path(tmp) / "plain"
        non_git.mkdir()
        note = Path(tmp) / "note.md"
        note.write_text(good)
        rep = Report()
        check_design_gate(Spec.load(note), non_git, rep)
        ok = any(l.startswith("UNCHECKED") and "not a git checkout" in l for l in rep.lines)
        print(("PASS " if ok else "FAIL ") + "non-git repo is UNCHECKED")
        failed += 0 if ok else 1
        print(f"selftest: {len(cases) + 1 - failed} passed, {failed} failed")
        return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("spec", type=Path, nargs="?")
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO,
                        help=f"read-only checkout of current main (default {DEFAULT_REPO})")
    parser.add_argument("--check", action="append", default=[],
                        help="workflow file, stem or name of the failing check (repeatable)")
    parser.add_argument("--design-note", action="store_true",
                        help="check the file as a design note against the design gate (acceptance, dry run, fence)")
    parser.add_argument("--selftest", action="store_true", help="run the design-gate self-test and exit")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.spec is None:
        parser.error("spec is required unless --selftest is given")
    if not args.spec.is_file():
        print(f"check_spec: spec not found: {args.spec}", file=sys.stderr)
        return 2
    if not args.repo.is_dir():
        print(f"check_spec: repo not found: {args.repo}", file=sys.stderr)
        return 2
    repo = args.repo.resolve()
    spec = Spec.load(args.spec)
    report = Report()
    head = (repo / ".git" / "HEAD")
    report.add("NOTE", "-", f"spec {args.spec} against {repo}"
               + (f" (HEAD {git_head(repo)})" if head.exists() else ""))
    if args.design_note:
        check_design_gate(spec, repo, report)
    else:
        check_allowed_paths(spec, repo, args.check, report)
        check_import_boundary(spec, repo, report)
        check_expected_output(spec, repo, report)
    sys.stdout.write(report.render())
    return 1 if report.counts["FLAG"] else 0


def git_head(repo: Path) -> str:
    head = (repo / ".git" / "HEAD").read_text().strip()
    if head.startswith("ref: "):
        ref = repo / ".git" / head[5:]
        if ref.is_file():
            return ref.read_text().strip()[:12]
        packed = repo / ".git" / "packed-refs"
        if packed.is_file():
            for line in packed.read_text().splitlines():
                if line.endswith(head[5:]):
                    return line.split()[0][:12]
        return head[5:]
    return head[:12]


if __name__ == "__main__":
    sys.exit(main())
