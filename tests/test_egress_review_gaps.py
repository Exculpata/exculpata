"""Synthetic regressions for routing, issuer setup and isolation review gaps."""
import ipaddress
import json
import runpy
import socket
import sys

import pytest
import yaml

from tests.test_oss_installer import installer, ready_host, preflight_args, checks_by_name
from tests.test_reference_network_boundary import ROOT, contract_tree, run_contract


@pytest.mark.parametrize("service,key,value", [
    ("egress-proxy", "OIDC_ISSUER", "https://other.example.test/realm"),
    ("egress-proxy", "OIDC_ISSUER_PINS", "${UNREVIEWED_PINS}"),
    ("app", "CASE_INTELLIGENCE_OIDC_ISSUER", "https://other.example.test/realm"),
    ("app", "CASE_INTELLIGENCE_OIDC_EGRESS_PINS", "${UNREVIEWED_PINS}"),
])
def test_contract_rejects_changed_issuer_destinations(contract_tree, service, key, value):
    path = contract_tree / "compose.yaml"
    data = yaml.safe_load(path.read_text())
    data["services"][service].setdefault("environment", {})[key] = value
    path.write_text(yaml.safe_dump(data))
    assert run_contract(contract_tree).returncode == 1


@pytest.mark.parametrize("key", ["OIDC_ISSUER", "OIDC_ISSUER_PINS"])
def test_contract_rejects_removed_relay_destination(contract_tree, key):
    path = contract_tree / "compose.yaml"
    data = yaml.safe_load(path.read_text())
    del data["services"]["egress-proxy"]["environment"][key]
    path.write_text(yaml.safe_dump(data))
    assert run_contract(contract_tree).returncode == 1


@pytest.mark.parametrize("key,value", [
    ("pid", "host"), ("ipc", "host"), ("userns_mode", "host"),
    ("pid", "service:gateway"), ("ipc", "service:gateway"),
    ("uts", "host"), ("cgroup", "host"), ("isolation", "default"),
    ("env_file", ["./config/unreviewed.env"]),
])
def test_contract_rejects_namespace_and_environment_source_changes(contract_tree, key, value):
    path = contract_tree / "compose.local-accounts.yaml"
    data = yaml.safe_load(path.read_text())
    data["services"]["app"][key] = value
    path.write_text(yaml.safe_dump(data))
    assert run_contract(contract_tree).returncode == 1


PRIVATE = str(ipaddress.IPv4Address(bytes((10, 0, 0, 5))))
ISSUER = "https://" + PRIVATE + "/realm"


@pytest.mark.parametrize("pins,accepted", [
    ("", False),
    (json.dumps({"issuer": ISSUER, "addresses": [PRIVATE]}), True),
    (json.dumps({"issuer": "https://other.example.test", "addresses": [PRIVATE]}), False),
    (json.dumps({"issuer": ISSUER, "addresses": []}), False),
    (json.dumps({"issuer": ISSUER, "addresses": ["127.0.0.1"]}), False),
    ("{invalid", False),
    (json.dumps({"issuer": ISSUER, "addresses": [PRIVATE]}) + "\n", False),
], ids=["missing", "explicit", "mismatch", "empty", "unsafe", "malformed", "line-control"])
def test_fresh_private_issuer_preflight_checks_pins_before_writes(tmp_path, ready_host, pins, accepted):
    args = preflight_args(tmp_path, "--auth", "oidc", "--dry-run", "--oidc-issuer", ISSUER)
    args.oidc_egress_pins = pins
    result = installer._collect_preflight("none", args)
    assert (checks_by_name(result)["identity-options"].state == "pass") is accepted
    assert not args.root.exists()


def test_fresh_private_issuer_pin_option_is_exposed(tmp_path):
    value = json.dumps({"issuer": ISSUER, "addresses": [PRIVATE]})
    args = preflight_args(tmp_path, "--oidc-egress-pins", value)
    assert args.oidc_egress_pins == value


@pytest.mark.parametrize('address,pinned,accepted', [
    (ipaddress.IPv6Address((0xfd12 << 112) | 5), True, True),
    (ipaddress.IPv6Address((0xfd12 << 112) | 5), False, False),
    (ipaddress.IPv4Address(bytes((169, 254, 169, 254))), False, False),
    (ipaddress.IPv4Address(bytes((169, 254, 169, 254))), True, False),
    (ipaddress.IPv6Address((0xfd00 << 112) | (0xec2 << 96) | 0x254), True, False),
    (ipaddress.IPv4Address('8.8.8.8'), False, True),
    (ipaddress.IPv4Address('8.8.8.8'), True, True),
], ids=['ula-pinned', 'ula-unpinned', 'metadata', 'metadata-pinned', 'metadata-v6-pinned', 'public', 'public-pinned'])
def test_fresh_literal_access_policy_is_offline(tmp_path, monkeypatch, address, pinned, accepted):
    host = f'[{address}]' if address.version == 6 else str(address)
    issuer = 'https://' + host + '/realm'
    args = preflight_args(tmp_path, '--auth', 'oidc', '--oidc-issuer', issuer)
    args.oidc_egress_pins = json.dumps({'issuer': issuer, 'addresses': [str(address)]}) if pinned else ''
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: pytest.fail('offline preflight performed DNS'))
    assert installer._identity_options_valid(args) is accepted
    assert not args.root.exists()


def test_unpinned_private_install_stops_before_mutation(tmp_path, ready_host, monkeypatch):
    root = tmp_path / 'new-node'
    secret = tmp_path / 'synthetic-secret'
    secret.write_text('synthetic-oidc-secret-value')
    secret.chmod(0o600)
    monkeypatch.setattr(installer, '_prepare_directories', lambda *a, **k: pytest.fail('prepared node before rejecting issuer'))
    monkeypatch.setattr(installer, '_stage_release', lambda *a, **k: pytest.fail('staged release before rejecting issuer'))
    monkeypatch.setattr(installer, '_run', lambda *a, **k: pytest.fail('ran external command before rejecting issuer'))
    monkeypatch.setattr(sys, 'argv', ['install', '--root', str(root), '--models', 'none', '--auth', 'oidc',
        '--oidc-issuer', ISSUER, '--oidc-client-secret-file', str(secret), '--non-interactive', '--plain'])
    assert installer.main() == 1
    assert not root.exists()


def test_fresh_configuration_retains_validated_issuer_pins(tmp_path):
    args = installer._parser().parse_args(["install", "--auth", "oidc", "--models", "none", "--non-interactive"])
    installer._collect_identity_choices(args)
    args.oidc_issuer = ISSUER
    args.oidc_egress_pins = json.dumps({"issuer": ISSUER, "addresses": [PRIVATE]})
    console = installer.Console(color=False, quiet=True)
    root = tmp_path / "node"
    paths = installer._prepare_directories(console, root, storage_root=None, resume=False, dry_run=False)
    cert, key = paths["tls"] / "synthetic.crt", paths["tls"] / "synthetic.key"
    cert.write_text("synthetic certificate")
    key.write_text("synthetic key")
    args.tls_cert, args.tls_key = cert, key
    installer._private_write(paths["secrets"] / "oidc-client-secret", "synthetic-oidc-secret-value")
    installer._configure(console, args, root, paths, "synthetic-release", ROOT, ())
    saved = installer._dotenv(installer._app_env(root), literal_keys={"CASE_INTELLIGENCE_OIDC_EGRESS_PINS"})
    assert saved.get("CASE_INTELLIGENCE_OIDC_EGRESS_PINS") == args.oidc_egress_pins
    assert installer._compose_environment(installer._compose(root, ()))['OIDC_EGRESS_PINS'] == args.oidc_egress_pins


@pytest.mark.parametrize("routes,accepted", [
    ("eth1 00000000 010200C0 0003 0 0 0 00000000", True),
    ("eth1 00000000 020200C0 0003 0 0 0 00000000", False),
    ("eth1 00000000 010200C0 0001 0 0 0 00000000", False),
    ("eth1 000200C0 00000000 0001 0 0 0 00FFFFFF", False),
    ("", False),
])
def test_route_observation_requires_the_actual_front_default_gateway(routes, accepted):
    check = runpy.run_path(str(ROOT / 'tests/network_smoke/front_control.py'))['assert_front_default_route']
    table = "Iface Destination Gateway Flags RefCnt Use Metric Mask\n" + routes
    if accepted:
        check('192.0.2.1', table)
    else:
        with pytest.raises(AssertionError, match='default route'):
            check('192.0.2.1', table)
