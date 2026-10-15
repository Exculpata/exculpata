"""Persist independent mode and accent choices, including the legacy theme upgrade."""
from importlib import resources

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from .compat_env import response_headers
from .identity import IdentityService
from .store_records import PrincipalPreferenceRecord

CHOICES = {"theme": ("dark", "light"), "accent": ("cyan", "green", "amber")}


def migrate_appearance(connection):
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(workbench_principal_preference)")}
        if "accent" not in columns:
            sql = resources.files("case_intelligence").joinpath("migrations/sqlite/0043_appearance_accents.sql").read_text()
            for statement in sql.split(";"):
                if statement.strip():
                    connection.execute(statement)


def read_preference(workspace, principal_id):
    principal = workspace.get_principal(principal_id)
    with workspace._lock:
        row = workspace.connection.execute(
            "SELECT principal_id,theme,accent,updated_at FROM workbench_principal_preference WHERE principal_id=?",
            (principal.principal_id,),
        ).fetchone()
    return (PrincipalPreferenceRecord(**dict(row)) if row else
            PrincipalPreferenceRecord(principal.principal_id, "dark", principal.created_at))


def set_preference(workspace, principal_id, field, value):
    from .workspace_store import WorkspaceProblem
    selected = (value or "").strip().casefold()
    if field not in CHOICES or selected not in CHOICES[field]:
        raise WorkspaceProblem(f"Choose an available appearance {field}.")
    principal = workspace.get_principal(principal_id)
    with workspace._lock, workspace.connection:
        # Update only the selected field: changing mode never resets the accent,
        # and concurrent requests cannot overwrite the other preference.
        workspace.connection.execute(
            f"INSERT INTO workbench_principal_preference(principal_id,{field},updated_at) "
            f"VALUES (?,?,?) ON CONFLICT(principal_id) DO UPDATE SET {field}=excluded.{field},updated_at=excluded.updated_at",
            (principal.principal_id, selected, workspace._now()),
        )
        return read_preference(workspace, principal_id)


def register_appearance_routes(app, *, workspace, require_csrf, auth_context, audit):
    def save(request, field, value, next_path):
        context = auth_context(request)
        if value not in CHOICES[field]:
            raise HTTPException(400, f"Choose an available appearance {field}.")
        preference = set_preference(workspace, context.principal_id, field, value)
        audit(request, f"preference.{field}", "success", context=context,
              object_type="principal", object_id=context.principal_id)
        return RedirectResponse(IdentityService.safe_next(next_path, default="/"), status_code=303,
                                headers=response_headers(field.title(), getattr(preference, field)))

    @app.post("/preferences/theme", dependencies=[Depends(require_csrf)], include_in_schema=False)
    def theme(request: Request, theme: str = Form(..., max_length=24),
              next_path: str = Form("/", alias="next", max_length=2_048)):
        return save(request, "theme", theme, next_path)

    @app.post("/preferences/accent", dependencies=[Depends(require_csrf)], include_in_schema=False)
    def accent(request: Request, accent: str = Form(..., max_length=24),
               next_path: str = Form("/", alias="next", max_length=2_048)):
        return save(request, "accent", accent, next_path)
