#!/usr/bin/env python3
"""Review inventory, not a Python sandbox. Runtime isolation is enforced by Compose."""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import sys
import shlex
import textwrap
import tomllib

import yaml


ROOT = Path(__file__).resolve().parents[1]
NETWORK_MODULES = ("httpx", "httpx2", "requests", "aiohttp", "urllib.request",
                   "socket", "http.client", "authlib.integrations", "service_endpoints.open_service_request",
                   "huggingface_hub", "faster_whisper", "ctranslate2", "av")
URL = re.compile(r"https?://([A-Za-z0-9._\[\]:-]+)")
HOSTNAME = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}\.?")


class UniqueLoader(yaml.SafeLoader):
    """Ambiguous YAML keys must not hide a policy change."""


def mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f"duplicate YAML key at line {key_node.start_mark.line + 1}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)


def load_compose(path):
    if path.is_symlink():
        raise ValueError("symlinked Compose file")
    value = yaml.load(path.read_text(), Loader=UniqueLoader)
    if not isinstance(value, dict):
        raise ValueError("Compose document must be a mapping")
    return value


def fingerprint(value):
    return hashlib.sha256(value.encode()).hexdigest()


def source_inventory(root):
    """Resolve ordinary imports, aliases, assignments and sync/async clients.

    This conservative inventory cannot resolve arbitrary reflection, generated
    code, third-party internals or every language. New networking imports and
    references are reviewed too, including an aliased function not yet called.
    """
    found = []
    paths = sorted(set((root / "src").rglob("*.py")) |
                   set(root.glob("services/*/src/**/*.py")) |
                   set(root.glob("deploy/**/*.py")) |
                   set(root.glob("scripts/**/*.py")))
    for path in paths:
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError(f"{relative}:1: symlinked source")
        found.extend(python_inventory(relative, path.read_text(), 0))
    for path in sorted(set(root.glob('.github/workflows/*.yml')) |
                       set(root.glob('.github/workflows/*.yaml'))):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError(f"{relative}:1: symlinked workflow")
        document = yaml.compose(path.read_text())
        def fields(node):
            return {key.value: value for key, value in node.value}
        for job_name, job in fields(fields(document)['jobs']).items():
            steps = fields(job).get('steps')
            for number, step in enumerate(steps.value if steps else [], 1):
                values = fields(step)
                name = values.get('name', values.get('id'))
                context = f'job {job_name}, step {number}' + (f' ({name.value})' if name else '')
                try:
                    uses = values.get('uses')
                    if uses and uses.value.lower().split('@', 1)[0] == 'actions/github-script':
                        raise SyntaxError('unsupported actions/github-script; use workflow_inventory() '
                                          'supported Python forms or a repository script',
                                          (relative, uses.start_mark.line + 1, 1, uses.value))
                    run = values.get('run')
                    if run is not None:
                        offset = run.start_mark.line + (run.style == '|')
                        found.extend(workflow_inventory(relative, run.value, offset))
                except SyntaxError as exc:
                    exc.msg = f'{exc.msg}; {context}'
                    raise
    return found


def workflow_inventory(relative, source, line_offset):
    """Inspect bounded inline Python; reject unmodeled interpreter launches.

    File and module commands are recognized separately from inline payloads.
    Repository Python files are already included by source_inventory().
    """
    found = []
    lines = source.splitlines()
    index = 0

    def unsupported(command):
        raise SyntaxError('unsupported Python launch; use workflow_inventory() supported '
                          'python - heredocs, python -c, or repository script/module commands',
                          (relative, line_offset + index + 1, 1, command))

    while index < len(lines):
        command = lines[index].strip()
        while command.endswith('\\') and index + 1 < len(lines):
            index += 1
            command = command[:-1] + lines[index].strip()
        heredoc = re.fullmatch(r"python3?\s+-\s*[^<]*<<\s*(['\"]?)(\w+)\1", command)
        if heredoc:
            if any(token in command.split('<<', 1)[0] for token in '|;&<>()`'):
                unsupported(command)
            start = index + 1
            index = start
            while index < len(lines) and lines[index].strip() != heredoc[2]:
                index += 1
            if index == len(lines):
                raise SyntaxError('unterminated Python heredoc', (relative, line_offset + start, 1, command))
            found.extend(python_inventory(relative, textwrap.dedent('\n'.join(lines[start:index])),
                                          line_offset + start))
        elif re.match(r'python3?\s+-c\b', command):
            try:
                arguments = shlex.split(command)
            except ValueError as exc:
                unsupported(command)
            if len(arguments) != 3:
                unsupported(command)
            found.extend(python_inventory(relative, arguments[2], line_offset + index))
        else:
            try:
                lexer = shlex.shlex(command, posix=True, punctuation_chars='|&;<>()')
                lexer.whitespace_split = True
                arguments = list(lexer)
            except ValueError:
                if re.search(r'\bpython(?:3(?:\.\d+)?)?\b', command):
                    unsupported(command)
                index += 1
                continue
            launches = [i for i, argument in enumerate(arguments)
                        if re.fullmatch(r'(?:[^\s]*/)?python(?:3(?:\.\d+)?)?', argument)]
            if launches:
                position = launches[0]
                prefix, payload = arguments[:position], arguments[position + 1:]
                if prefix[:1] == ['env']:
                    prefix = prefix[1:]
                if (not all(re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*=[^\n]*', item) for item in prefix)
                        or any(re.fullmatch(r'[|&;<>()]+', item) for item in arguments)
                        or any(token in command for token in ('`', '$('))):
                    unsupported(command)
                while payload and payload[0] in ('-u', '-B', '-I', '-E', '-s', '-S'):
                    payload = payload[1:]
                module = (len(payload) >= 2 and payload[0] == '-m'
                          and re.fullmatch(r'[A-Za-z_]\w*(?:\.\w+)*', payload[1]))
                script = payload and not payload[0].startswith('-') and payload[0].endswith('.py')
                if not (module or script):
                    unsupported(command)
        index += 1
    return found


def python_inventory(relative, source_text, line_offset):
    found = []
    try:
        tree = ast.parse(source_text, filename=relative)
        ast.increment_lineno(tree, line_offset)
        aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for item in node.names:
                    aliases[item.asname or item.name.split('.')[0]] = item.name if item.asname else item.name.split('.')[0]
            elif isinstance(node, ast.ImportFrom):
                for item in node.names:
                    aliases[item.asname or item.name] = f"{node.module}.{item.name}"

        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        def scope_for(node):
            while node in parents:
                node = parents[node]
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return node
            return tree

        local_aliases = {}
        instance_aliases = {}

        def class_for(node):
            while node in parents:
                node = parents[node]
                if isinstance(node, ast.ClassDef):
                    return node
            return None

        def resolve(node, scope=None):
            scope = scope or scope_for(node)
            if isinstance(node, ast.Name):
                search = scope
                while True:
                    if (search, node.id) in local_aliases:
                        return local_aliases[search, node.id]
                    if search is tree:
                        break
                    search = scope_for(search)
                return aliases.get(node.id, node.id)
            if isinstance(node, ast.Attribute):
                instance = instance_aliases.get((class_for(scope), ast.unparse(node)))
                if instance:
                    return instance
                return resolve(node.value, scope) + "." + node.attr
            if isinstance(node, ast.Call):
                name = resolve(node.func, scope)
                module = network_import(node, name)
                if module:
                    return module
                if name.rsplit('.', 1)[-1] in {"Client", "AsyncClient", "Session", "ClientSession", "OAuth", "build_opener", "OpenerDirector", "create_connection"}:
                    return name
                return ""
            if isinstance(node, ast.Await):
                return resolve(node.value, scope)
            if isinstance(node, ast.IfExp):
                for option in (node.body, node.orelse):
                    name = resolve(option, scope)
                    if networking(name):
                        return name
            return ""

        def networking(name):
            return any(name == module or name.startswith(module + ".") for module in NETWORK_MODULES)

        def network_import(node, callee):
            if callee in {"importlib.import_module", "__import__"} and node.args:
                argument = node.args[0]
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str) and networking(argument.value):
                    return argument.value
            return ""

        # A bounded fixed point also follows aliases assigned after their use.
        for _ in range(12):
            before = dict(local_aliases), dict(instance_aliases)
            for node in ast.walk(tree):
                pairs = []
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                        if arg.annotation is not None:
                            name = resolve(arg.annotation, node)
                            if networking(name):
                                local_aliases[node, arg.arg] = name
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    pairs = [(target, node.value) for target in targets]
                elif isinstance(node, (ast.With, ast.AsyncWith)):
                    pairs = [(item.optional_vars, item.context_expr) for item in node.items]
                for target, value in pairs:
                    name = resolve(value, scope_for(node)) if value is not None else ""
                    if isinstance(target, ast.Name) and networking(name):
                        local_aliases[scope_for(node), target.id] = name
                    elif isinstance(target, ast.Attribute) and networking(name):
                        instance_aliases[class_for(node), ast.unparse(target)] = name
            if (local_aliases, instance_aliases) == before:
                break
        for node in ast.walk(tree):
            kind = None
            detail = ""
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = ([item.name for item in node.names] if isinstance(node, ast.Import)
                         else [str(node.module)])
                if any(networking(name) for name in names):
                    kind, detail = "client", "network import"
            elif isinstance(node, ast.Call):
                name = resolve(node.func)
                if network_import(node, name):
                    kind, detail = "client", "network import"
                elif networking(name):
                    kind, detail = "client", name
            elif isinstance(node, (ast.Attribute, ast.Name)) and isinstance(node.ctx, ast.Load) and networking(resolve(node)):
                # Outermost reference only; calls have their own full argument fingerprint.
                parent = parents.get(node)
                if not isinstance(parent, (ast.Attribute, ast.Call)):
                    kind, detail = "client", resolve(node)
            if kind:
                scope = []
                parent = node
                while parent in parents:
                    parent = parents[parent]
                    if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        scope.append(parent.name)
                # Python 3.13 changed dump's default to omit empty lists. Keep
                # the reviewed 3.12 representation, including argument lists.
                dump_options = {"show_empty": True} if sys.version_info >= (3, 13) else {}
                signature = ".".join(reversed(scope)) + ":" + ast.dump(node, include_attributes=False, **dump_options)
                found.append((relative, node.lineno, kind, fingerprint(signature), detail))
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                hosts = set(URL.findall(node.value))
                if HOSTNAME.fullmatch(node.value):
                    hosts.add(node.value)
                if len(node.value) <= 45:
                    try:
                        hosts.add(str(ipaddress.ip_address(node.value)))
                    except ValueError:
                        pass
                for host in sorted(hosts):
                    found.append((relative, node.lineno, "host", fingerprint(node.value), host.lower()))
    except SyntaxError as exc:
        exc.lineno += line_offset
        raise

    return found


def check(root):
    policy = tomllib.loads((root / "config/egress-allowlist.toml").read_text())
    errors = []

    def reject(path, reason, token=None):
        line = 1
        if token:
            line = next((n for n, value in enumerate(path.read_text().splitlines(), 1)
                         if token in value), 1)
        errors.append(f"{path.relative_to(root)}:{line}: {reason}")

    files = sorted(set(root.rglob("compose*.yaml")) | set(root.rglob("compose*.yml")))
    files = [p for p in files if not set(p.relative_to(root).parts) & {".git", ".venv", "node_modules"}]
    expected_files = set(policy["compose"])
    if {p.relative_to(root).as_posix() for p in files} != expected_files:
        errors.append("config/egress-allowlist.toml:1: Compose file inventory changed; review every new/removed graph")
    base = load_compose(root / "compose.yaml")
    for path in files:
        relative = path.relative_to(root).as_posix()
        data = load_compose(path)
        if relative not in expected_files:
            reject(path, "unreviewed Compose file")
            continue
        approved = json.loads(policy["compose"][relative])
        networks = data.get("networks", {})
        if networks != approved["networks"]:
            reject(path, "network definitions changed (internal, external, driver or options)", "networks:")
        # A named volume can conceal a host bind through driver_opts. Compare
        # definitions as well as mounts; a socket need not be named docker.sock.
        if data.get("volumes", {}) != approved["volumes"]:
            reject(path, "volume definitions changed (external, driver or host bind options)", "volumes:")
        if any(key in data for key in ("include", "extends")):
            reject(path, "unreviewed Compose include/extends")
        for service, definition in data.get("services", {}).items():
            if not isinstance(definition, dict):
                reject(path, f"invalid service {service}", service + ":")
                continue
            network_fields = {key: definition[key] for key in (
                "networks", "network_mode", "ports", "privileged", "cap_add", "devices",
                "sysctls", "extra_hosts", "dns", "dns_search", "dns_opt", "extends", "cap_drop",
                "security_opt", "volumes", "volumes_from", "use_api_socket", "env_file",
                "pid", "ipc", "userns_mode", "uts", "cgroup", "isolation") if key in definition}
            environment = definition.get("environment", {})
            if not isinstance(environment, dict):
                raise ValueError("transport environment must be a mapping")
            routing = {key: value for key, value in environment.items()
                       if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
                       or key in {"CASE_INTELLIGENCE_OIDC_PROXY", "OIDC_ISSUER", "OIDC_ISSUER_PINS",
                                  "CASE_INTELLIGENCE_OIDC_ISSUER", "CASE_INTELLIGENCE_OIDC_EGRESS_PINS"}}
            if routing:
                network_fields["proxy_environment"] = routing
            expected = approved["services"].get(service)
            if expected is None or network_fields != expected:
                reject(path, f"{service}: network membership, published ports, mounts or escape capability changed", service + ":")
            effective = dict(base.get("services", {}).get(service, {}))
            effective.update(definition)
            if "networks" not in effective and effective.get("network_mode") != "none":
                reject(path, f"{service}: implicit/default or escaped network", service + ":")
        if set(approved["services"]) - set(data.get("services", {})):
            reject(path, "reviewed service removed")
    observed = source_inventory(root)
    remaining = Counter((r["path"], r["kind"], r["digest"], r["detail"])
                        for r in policy.get("source", []))
    for path, line, kind, digest, detail in observed:
        key = path, kind, digest, detail
        if remaining[key]:
            remaining[key] -= 1
        else:
            errors.append(f"{path}:{line}: unreviewed outbound {kind}: {detail}")
    for (path, kind, _, detail), count in remaining.items():
        if count:
            errors.append(f"{path}:1: stale {kind} allowance: {detail}; remove or review changed call")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        errors = check(args.root.resolve())
    except SyntaxError as exc:
        print(f"{exc.filename}:{exc.lineno}: invalid Python source: {exc.msg}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as exc:
        print(f"network-contract: invalid or unreadable policy/source: {type(exc).__name__}", file=sys.stderr)
        return 1
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Network contract passed (static repository inventory).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
