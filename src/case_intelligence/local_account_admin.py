"""Authenticated browser account management over the shared local repository."""
from __future__ import annotations

import hashlib
import hmac
import re
import sqlite3
import threading
from typing import Mapping
from urllib.parse import parse_qs, quote, urlencode

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from .identity import SESSION_COOKIE
from .local_accounts import LocalAccount, LocalAccountRepository, normalized_account


class _AccountConflict(RuntimeError):
    pass


class _AccountDenied(RuntimeError):
    pass


async def _read_form(request: Request) -> dict[str, str]:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
        raise HTTPException(415, "Use the account form to submit this change.")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 32 * 1024:
            raise HTTPException(413, "The account form is too large. Shorten its fields and try again.")
        body.extend(chunk)
    try:
        values = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True,
                          encoding="utf-8", errors="strict", max_num_fields=12)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "The account form could not be read. Reload it and try again.") from None
    if any(len(value) != 1 for value in values.values()):
        raise HTTPException(400, "The account form has duplicate fields. Reload it and try again.")
    return {key: value[0] for key, value in values.items()}


def register_local_account_routes(app, *, identity, bench, templates, auth_context, base_context, audit):
    mutations = threading.BoundedSemaphore(2)
    def administrator(request: Request, *, mutate: bool = False):
        context = auth_context(request)
        if not context.is_administrator:
            audit(request, "admin.people", "denied", context=context)
            raise HTTPException(403, "Administrator access is required.")
        settings = identity.local_settings
        if mutate and (identity.auth_mode != "local" or settings is None or not settings.management_enabled):
            raise HTTPException(403, "Browser account changes are not enabled. Open People for the next step.")
        return context

    def projection(account: LocalAccount):
        return {"username": account.username, "display_name": account.display_name,
                "enabled": account.enabled, "administrator": "administrator" in account.roles,
                "edit_token": identity.local_account_edit_token(account),
                "url": "/admin/people/accounts/" + quote(account.username, safe="")}

    def people(request: Request, *, error: str = "", notice: str = "", status_code: int = 200,
               account: str | None = None, form_values: Mapping[str, str] | None = None):
        context = administrator(request)
        local = identity.auth_mode == "local" and identity.local_settings is not None
        accounts = identity.local_settings.accounts if local else {}
        selected = accounts.get(account) if account is not None else None
        if account is not None and selected is None:
            raise HTTPException(404, "This account is no longer available.")
        return templates.TemplateResponse(
            request=request, name="workbench_people.html", status_code=status_code,
            context={**base_context(request), "people": tuple(projection(value) for value in sorted(accounts.values(), key=lambda item: item.display_name.casefold())),
                     "selected_account": projection(selected) if selected is not None else None,
                     "management_enabled": local and identity.local_settings.management_enabled,
                     "has_teammate": sum(value.enabled for value in accounts.values()) > 1,
                     "has_matter": bool(bench.workspace.all_matters()),
                     "current_username": context.principal.provider_subject,
                     "provider_people": identity.membership_candidates() if not local else (),
                     "form_values": dict(form_values or {}), "error": error, "notice": notice},
        )

    def writer(request: Request, context, *, username: str, edit_token: str | None):
        settings = identity.local_settings
        raw_token = request.cookies.get(SESSION_COOKIE)
        target_id = "local-account-" + hashlib.sha256(username.casefold().encode()).hexdigest()[:32]

        def guard(accounts: Mapping[str, LocalAccount]) -> None:
            # This runs under the same process writer lock as the target update.
            # A concurrent CLI/browser demotion or session revocation cannot rely
            # on the administrator status observed when the form was loaded.
            settings._validate_management_boundary()
            fresh = identity.resolve(raw_token)
            actor = accounts.get(context.principal.provider_subject)
            if (fresh is None or fresh.principal_id != context.principal_id or not fresh.is_administrator
                    or fresh.auth_method != "local" or actor is None or not actor.enabled
                    or "administrator" not in actor.roles):
                raise _AccountDenied("Your administrator session changed. Sign in again before managing people.")
            if edit_token is not None:
                current = accounts.get(username)
                if (not re.fullmatch(r"[0-9a-f]{64}", edit_token) or current is None
                        or not hmac.compare_digest(edit_token, identity.local_account_edit_token(current))):
                    raise _AccountConflict("This account changed in another window. Review its current settings and try again.")

        completion = {"audit_problem": False, "principal_problem": False}

        def record_request(change):
            audit(request, f"account.{change.action}.requested", "success", context=context,
                  object_type="local_account", object_id=target_id)

        def record_completion(change, accounts):
            # Keep the projection refresh under the account writer lock so a
            # later rename cannot be followed by an older display-name update.
            if change.action == "display-name":
                try:
                    account = accounts[username]
                    bench.workspace.refresh_principal_display_name(
                        settings.provider_key, username, account.display_name,
                    )
                except (OSError, sqlite3.Error, RuntimeError):
                    completion["principal_problem"] = True
            try:
                audit(request, f"account.{change.action}.completed", "success", context=context,
                      object_type="local_account", object_id=target_id)
            except (OSError, sqlite3.Error, RuntimeError):
                completion["audit_problem"] = True

        repository = LocalAccountRepository(settings.accounts_file, guard=guard,
                                            before_write=record_request, after_write=record_completion)
        return repository, completion

    def apply(request: Request, context, form: dict[str, str], *, username: str | None, action: str):
        permitted = {"csrf_token", "edit_token", "username", "display_name", "password", "password_confirm", "role", "enabled", "confirm_self"}
        if set(form) - permitted:
            raise RuntimeError("The account form contains unexpected fields. Reload it and try again.")
        if action == "create":
            username, _ = normalized_account(form.get("username", ""), form.get("display_name", ""))
            edit_token = None
        else:
            if not username or len(username) > 128:
                raise RuntimeError("This account is no longer available.")
            edit_token = form.get("edit_token", "")
        repository, completion = writer(request, context, username=username, edit_token=edit_token)
        is_self = username == context.principal.provider_subject
        role = form.get("role", "reviewer")
        if action in {"create", "role"} and role not in {"reviewer", "administrator"}:
            raise RuntimeError("Choose reviewer or administrator access.")
        if action in {"create", "password"}:
            password = form.get("password", "")
            if password != form.get("password_confirm", ""):
                raise RuntimeError("The passwords do not match. Enter them again.")
        if action == "state" and form.get("enabled") not in {"yes", "no"}:
            raise RuntimeError("Choose whether this account can sign in.")
        self_removal = is_self and ((action == "state" and form["enabled"] == "no") or
                                   (action == "role" and role == "reviewer"))
        if self_removal and form.get("confirm_self") != "yes":
            raise RuntimeError("Confirm that this change will sign you out before continuing.")
        actor = context.principal_id
        if action == "create":
            repository.create(username, form.get("display_name", ""), password,
                              administrator=role == "administrator", actor=actor)
        elif action == "name":
            repository.change_display_name(username, form.get("display_name", ""), actor=actor)
        elif action == "password":
            repository.change_password(username, password, actor=actor)
        elif action == "state":
            repository.set_enabled(username, form["enabled"] == "yes", actor=actor)
        elif action == "role":
            repository.set_administrator(username, role == "administrator", actor=actor)
        else:
            raise RuntimeError("That account action is unavailable.")
        if is_self and action in {"password", "state", "role"}:
            # Do not render another privileged page using the pre-change context.
            code = "account_changed_audit" if completion["audit_problem"] else "account_changed"
            response = RedirectResponse("/auth/login?" + urlencode({"error": code}), status_code=303)
            response.delete_cookie(SESSION_COOKIE, path="/")
            return response
        notices = {"create": "Account created. Share the sign-in address and password privately. Have this person sign in once, then add them from the matter’s Case team page.",
                   "name": "Name updated.", "password": "Password changed. Previous sessions have ended; share the new password privately.",
                   "state": "Sign-in access updated. Previous sessions have ended.",
                   "role": "Application role updated. This person must sign in again; matter-team membership is unchanged."}
        message = notices[action]
        if completion["audit_problem"]:
            message += " Audit completion needs operator attention."
        if completion["principal_problem"]:
            message += " Saved team names could not refresh; ask the installation operator to repair the account display name."
        destination = "/admin/people/accounts/" + quote(username, safe="")
        return RedirectResponse(destination + "?" + urlencode({"notice": message}), status_code=303)

    async def require_csrf(request: Request) -> dict[str, str]:
        context = administrator(request, mutate=True)
        form = await _read_form(request)
        if not identity.csrf_valid(context, form.get("csrf_token")):
            audit(request, "security.csrf", "denied", context=context)
            raise HTTPException(403, "This form expired. Refresh the page and try again.")
        return form

    async def mutate(request: Request, form: dict[str, str], *, username: str | None, action: str):
        context = administrator(request, mutate=True)
        if not mutations.acquire(blocking=False):
            raise HTTPException(429, "Account changes are busy. Wait a moment and try again.")
        try:
            return await run_in_threadpool(apply, request, context, form, username=username, action=action)
        except _AccountDenied as exc:
            raise HTTPException(403, str(exc)) from None
        except _AccountConflict as exc:
            return people(request, account=username, error=str(exc), status_code=409)
        except (OSError, sqlite3.Error):
            if identity.resolve(request.cookies.get(SESSION_COOKIE)) is None:
                response = RedirectResponse("/auth/login", status_code=303)
                response.delete_cookie(SESSION_COOKIE, path="/")
                return response
            return people(request, account=username, status_code=503,
                          error="The change could not be confirmed. Reload the account before retrying, or ask the installation operator to check account storage.")
        except RuntimeError as exc:
            if identity.resolve(request.cookies.get(SESSION_COOKIE)) is None:
                response = RedirectResponse("/auth/login", status_code=303)
                response.delete_cookie(SESSION_COOKIE, path="/")
                return response
            # Repository/input errors are fixed messages and never interpolate
            # passwords, hashes or supplied form values.
            message = str(exc)
            if message == "At least one enabled local administrator is required":
                message = "Keep one enabled administrator. Add or promote another administrator before changing this account."
            return people(request, account=username, error=message, status_code=400,
                          form_values={key: form.get(key, "")[:160] for key in ("username", "display_name")})
        finally:
            mutations.release()

    @app.get("/admin/people", include_in_schema=False)
    def list_people(request: Request, notice: str = Query("", max_length=240)):
        return people(request, notice=notice)

    @app.get("/admin/people/setup", include_in_schema=False)
    def account_setup(request: Request):
        administrator(request)
        return templates.TemplateResponse(request=request, name="workbench_people_setup.html", context=base_context(request))

    @app.get("/admin/people/accounts/{username}", include_in_schema=False)
    def account_details(request: Request, username: str, notice: str = Query("", max_length=240)):
        return people(request, account=username, notice=notice)

    @app.post("/admin/people/create", include_in_schema=False)
    async def create_person(request: Request, form: dict[str, str] = Depends(require_csrf)):
        return await mutate(request, form, username=None, action="create")

    @app.post("/admin/people/accounts/{username}/{action}", include_in_schema=False)
    async def change_person(request: Request, username: str, action: str, form: dict[str, str] = Depends(require_csrf)):
        if action not in {"name", "password", "state", "role"}:
            raise HTTPException(404, "That account action is unavailable.")
        return await mutate(request, form, username=username, action=action)
