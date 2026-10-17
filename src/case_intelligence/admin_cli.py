"""Content-free installation and identity administration commands."""

from __future__ import annotations

from case_intelligence.legacy_names import (
    LEGACY_STEM,
)

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from .identity import LocalAccountSettings
from .local_accounts import LocalAccountChange, LocalAccountRepository
from .managed_storage import ManagedMatterStorage, StoragePolicy


def _password_from_terminal(*, confirm: bool) -> str:
    if not sys.stdin.isatty():
        raise RuntimeError(
            "A terminal is required; use --password-stdin for unattended setup"
        )
    value = getpass.getpass("Password: ")
    if confirm and value != getpass.getpass("Confirm password: "):
        raise RuntimeError("Passwords did not match")
    return value


def _password_from_stdin() -> str:
    value = sys.stdin.readline(4_097)
    if not value or len(value) > 4_096:
        raise RuntimeError("Password input is missing or too large")
    return value.rstrip("\r\n")


def _read_password(args: argparse.Namespace) -> str:
    return _password_from_stdin() if args.password_stdin else _password_from_terminal(confirm=True)


def _actor() -> str:
    return f"uid:{os.geteuid()}"


def _report(change: LocalAccountChange) -> None:
    print(f"Local account change complete: {change.action}; account={change.username}; actor={change.actor}.")


def _accounts_init(args: argparse.Namespace) -> None:
    _report(LocalAccountRepository(args.file).initialize(
        args.username, args.display_name, _read_password(args), actor=_actor()))


def _accounts_add(args: argparse.Namespace) -> None:
    _report(LocalAccountRepository(args.file).create(
        args.username, args.display_name, _read_password(args),
        administrator=args.administrator, actor=_actor()))


def _accounts_password(args: argparse.Namespace) -> None:
    _report(LocalAccountRepository(args.file).change_password(
        args.username, _read_password(args), actor=_actor()))


def _accounts_migrate(args: argparse.Namespace) -> None:
    _report(LocalAccountRepository(args.file).migrate(args.backup_file, actor=_actor()))


def _accounts_rollback(args: argparse.Namespace) -> None:
    _report(LocalAccountRepository(args.file).rollback(
        args.backup_file, args.workspace_file, actor=_actor(), writers_stopped=args.confirm_stopped))


def _accounts_relocate(args: argparse.Namespace) -> None:
    _report(LocalAccountRepository(args.file).relocate(
        args.destination, args.backup_file, actor=_actor(), writers_stopped=args.confirm_stopped))


def _accounts_display_name(args: argparse.Namespace) -> None:
    _report(LocalAccountRepository(args.file).change_display_name(
        args.username, args.display_name, actor=_actor()))


def _accounts_enabled(args: argparse.Namespace) -> None:
    _report(LocalAccountRepository(args.file).set_enabled(
        args.username, args.accounts_command == "enable", actor=_actor()))


def _accounts_role(args: argparse.Namespace) -> None:
    _report(LocalAccountRepository(args.file).set_administrator(
        args.username, args.role == "administrator", actor=_actor()))


def _accounts_list(args: argparse.Namespace) -> None:
    settings = LocalAccountSettings(args.file)
    for account in sorted(settings.accounts.values(), key=lambda value: value.username):
        role = "administrator" if "administrator" in account.roles else "reviewer"
        state = "enabled" if account.enabled else "disabled"
        print(f"{account.username}\t{role}\t{state}\t{account.display_name}")


def _storage_init(args: argparse.Namespace) -> None:
    storage = ManagedMatterStorage.initialize(args.root, policy=StoragePolicy.from_environment())
    print(f"Initialized managed storage {storage.root}.")


def _storage_status(args: argparse.Namespace) -> None:
    ManagedMatterStorage(args.root, policy=StoragePolicy.from_environment(), require_marker=True)
    print("Existing managed storage boundary validated; sources and ownership marker retained.")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{LEGACY_STEM}")
    commands = parser.add_subparsers(dest="command", required=True)
    accounts = commands.add_parser("accounts", help="Manage local installation accounts")
    account_commands = accounts.add_subparsers(dest="accounts_command", required=True)
    for name, handler in (("init", _accounts_init), ("add", _accounts_add)):
        command = account_commands.add_parser(name)
        command.add_argument("--file", type=Path, required=True)
        command.add_argument("--username", required=True)
        command.add_argument("--display-name", required=True)
        command.add_argument("--password-stdin", action="store_true")
        if name == "add":
            command.add_argument("--administrator", action="store_true")
        command.set_defaults(handler=handler)
    password = account_commands.add_parser("password", help="Change a password and require a new sign-in")
    password.add_argument("--file", type=Path, required=True)
    password.add_argument("--username", required=True)
    password.add_argument("--password-stdin", action="store_true")
    password.set_defaults(handler=_accounts_password)
    listing = account_commands.add_parser("list", help="Show accounts, roles and enabled state")
    listing.add_argument("--file", type=Path, required=True)
    listing.set_defaults(handler=_accounts_list)

    migrate = account_commands.add_parser("migrate", help="Upgrade account changes after creating a recovery copy")
    migrate.add_argument("--file", type=Path, required=True)
    migrate.add_argument("--backup-file", type=Path, required=True)
    migrate.set_defaults(handler=_accounts_migrate)
    rollback = account_commands.add_parser("rollback", help="Invalidate local sessions and restore a version 1 migration recovery copy")
    rollback.add_argument("--file", type=Path, required=True)
    rollback.add_argument("--backup-file", type=Path, required=True)
    rollback.add_argument("--workspace-file", type=Path, required=True)
    rollback.add_argument("--confirm-stopped", action="store_true")
    rollback.set_defaults(handler=_accounts_rollback)
    relocate = account_commands.add_parser("relocate", help="Move accounts into a dedicated browser-management directory")
    relocate.add_argument("--file", type=Path, required=True)
    relocate.add_argument("--destination", type=Path, required=True)
    relocate.add_argument("--backup-file", type=Path, required=True)
    relocate.add_argument("--confirm-stopped", action="store_true")
    relocate.set_defaults(handler=_accounts_relocate)
    for name, handler in (("display-name", _accounts_display_name),
                          ("enable", _accounts_enabled), ("disable", _accounts_enabled),
                          ("role", _accounts_role)):
        description = {"display-name": "Change the name shown in the application",
                       "enable": "Allow an account to sign in again",
                       "disable": "Block sign-in and end existing sessions",
                       "role": "Change administrator access and require a new sign-in"}[name]
        command = account_commands.add_parser(name, help=description)
        command.add_argument("--file", type=Path, required=True)
        command.add_argument("--username", required=True)
        if name == "display-name":
            command.add_argument("--display-name", required=True)
        if name == "role":
            command.add_argument("--role", choices=("reviewer", "administrator"), required=True)
        command.set_defaults(handler=handler)

    storage = commands.add_parser("storage", help="Initialize managed matter storage")
    storage_commands = storage.add_subparsers(dest="storage_command", required=True)
    initialize = storage_commands.add_parser("init")
    initialize.add_argument("--root", type=Path, required=True)
    initialize.set_defaults(handler=_storage_init)
    status = storage_commands.add_parser("status", help="Validate existing managed storage without resetting it")
    status.add_argument("--root", type=Path, required=True)
    status.set_defaults(handler=_storage_status)
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    try:
        args.handler(args)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        parser.exit(1, f"{LEGACY_STEM}: {exc}\n")


if __name__ == "__main__":
    main()
