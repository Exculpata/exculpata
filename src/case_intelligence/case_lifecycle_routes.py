"""Owner/admin check-ins, frozen full exports and explicit final confirmation."""
from __future__ import annotations

from urllib.parse import urlencode
import sqlite3
import os

from fastapi import Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from .case_lifecycle_support import (archive_path, cleanup_archives, idle,
    install_archive, source_lock, verified_archive)
from .matter_archive import build_matter_archive
from .workspace_store import WorkspaceProblem
from .identity import SESSION_COOKIE, OIDC_STATE_COOKIE
from . import case_oidc
from .compat_env import kerberos_headers


def register_case_lifecycle_routes(app, *, bench, identity, templates, auth_context,
        base_context, require_csrf, require_response_lease, transfer_response_lease):
    @app.exception_handler(sqlite3.IntegrityError)
    async def frozen_content_error(request: Request, exc: sqlite3.IntegrityError):
        if str(exc) == 'matter content is frozen for explicit exit':
            return JSONResponse({'detail': 'This matter is frozen for closure. Reopen it before making changes.'},
                                status_code=409, headers={'Cache-Control': 'no-store'})
        raise exc

    def redirect(slug, error='', notice=''):
        values = {key: value for key, value in (('error', error), ('notice', notice)) if value}
        return RedirectResponse(f'/matters/{slug}/close' + ('?' + urlencode(values) if values else ''),
                                status_code=303, headers={'Cache-Control': 'no-store'})

    def owner(request, slug):
        context = auth_context(request)
        try:
            matter, lifecycle = bench.matter_for_closure(slug, context.principal_id,
                administrator_override=context.is_administrator)
        except KeyError as exc:
            raise HTTPException(404, 'Matter not found') from exc
        return context, matter, lifecycle

    def refresh_owner(request, slug, original):
        current = identity.resolve(request.cookies.get(SESSION_COOKIE), read_only=True)
        if current is not None and identity.auth_mode == 'kerberos':
            current = identity.bind_kerberos_request(current, *kerberos_headers(request.headers))
        try:
            if current is None or current.principal_id != original.principal_id:
                raise KeyError(slug)
            bench.matter_for_closure(slug, current.principal_id,
                administrator_override=current.is_administrator)
        except KeyError as exc:
            raise HTTPException(404, 'Matter is no longer available') from exc
        return current

    async def reauthorize(context, matter, *, action, mode):
        if case_oidc.available(identity, context, matter, action=action, mode=mode):
            return None
        start = await case_oidc.start(identity, context, matter, action=action, mode=mode)
        response = RedirectResponse(start.authorization_url, status_code=302,
                                    headers={'Cache-Control': 'no-store'})
        response.set_cookie(OIDC_STATE_COOKIE, start.state, **identity.oidc_state_cookie_options)
        return response

    def session_id(context):
        if context.session is None:
            raise WorkspaceProblem('Sign in again before confirming deletion.')
        return context.session.session_id

    def page(request, slug, *, error='', notice='', confirmation=None):
        context, matter, lifecycle = owner(request, slug)
        exit_record = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
        now = bench.workspace._now()
        # Do not open/reconcile sources to render the decision page. The certified
        # archive contains the authoritative inventory; counts here are context.
        counts = bench.workspace.matter_content_counts(matter.matter_id)
        return templates.TemplateResponse(request=request, name='workbench_close_matter.html',
            context={**base_context(request, matter, include_assistant=False),
                'matter': matter, 'lifecycle': lifecycle, 'exit_record': exit_record,
                'policy': bench.workspace.case_lifecycle.policy(matter.matter_id),
                'eligible': bool(exit_record and bench._parse_utc(now) >= bench._parse_utc(exit_record.eligible_after)),
                'confirmation': confirmation,
                'download_complete': bool(exit_record and bench.workspace.case_lifecycle.downloads.receipt(
                    exit_record, context.principal_id)),
                'active_work': bench.matter_active_work_counts(matter.matter_id),
                'busy': any(bench.matter_active_work_counts(matter.matter_id).values()),
                'conversation_count': max(counts['conversations'], lifecycle.conversation_count),
                'message_count': max(counts['messages'], lifecycle.message_count),
                'notebook_count': counts['notebook_items'],
                'administrator_override': context.is_administrator and matter.owner_id != context.principal_id,
                'oidc_administrator': case_oidc.required(identity, context, matter),
                'selected_mode': case_oidc.selected_mode(identity, context, matter),
                'owner_name': bench.workspace.get_principal(matter.owner_id).display_name,
                'error': error, 'notice': notice}, headers={'Cache-Control': 'no-store'})

    @app.post('/matters/{slug}/check-in', dependencies=[Depends(require_csrf)])
    def check_in(request: Request, slug: str):
        context, matter, _ = owner(request, slug)
        try:
            bench.workspace.case_lifecycle.check_in(matter.matter_id, context.principal_id,
                session_id=context.session.session_id if context.session else None, administrator_override=(context.is_administrator and matter.owner_id != context.principal_id))
        except WorkspaceProblem as exc:
            return redirect(slug, error=str(exc))
        return RedirectResponse(f'/matters/{slug}/settings', status_code=303)

    @app.get('/matters/{slug}/close', response_class=HTMLResponse)
    def close_page(request: Request, slug: str, error: str = Query('', max_length=240),
                   notice: str = Query('', max_length=240)):
        return page(request, slug, error=error, notice=notice)

    @app.post('/matters/{slug}/close/begin', dependencies=[Depends(require_csrf)])
    async def begin_exit(request: Request, slug: str, mode: str = Form('export_exit', max_length=32)):
        context, matter, lifecycle = owner(request, slug)
        try:
            if lifecycle.state == 'active':
                challenge = await reauthorize(context, matter, action='begin', mode=mode)
                if challenge is not None:
                    return challenge
            with source_lock(bench, matter.matter_id):
                idle(bench, matter.matter_id)
                current_authority = refresh_owner(request, slug, context)
                bench.workspace.case_lifecycle.begin_exit(slug, context.principal_id, mode=mode,
                    session_id=session_id(current_authority), administrator_override=(current_authority.is_administrator and matter.owner_id != current_authority.principal_id))
        except WorkspaceProblem as exc:
            return redirect(slug, error=str(exc))
        return redirect(slug, notice='Closure started. The matter is frozen and can be reopened before final deletion.')

    @app.post('/matters/{slug}/close/cancel', dependencies=[Depends(require_csrf)])
    def cancel_exit(request: Request, slug: str, exit_id: str = Form(..., max_length=100)):
        context, matter, _ = owner(request, slug)
        try:
            with source_lock(bench, matter.matter_id):
                idle(bench, matter.matter_id)
                # Clean only after repository verifies the submitted exit. A
                # failed cleanup leaves a harmless stale archive, not a new gate.
                current_authority = refresh_owner(request, slug, context)
                bench.workspace.case_lifecycle.cancel_exit(slug, context.principal_id, exit_id,
                    session_id=session_id(current_authority), administrator_override=(current_authority.is_administrator and matter.owner_id != current_authority.principal_id))
                cleanup_archives(bench, matter.matter_id)
        except WorkspaceProblem as exc:
            return redirect(slug, error=str(exc))
        return RedirectResponse(f'/matters/{slug}/settings', status_code=303)

    @app.post('/matters/{slug}/close/export', dependencies=[Depends(require_csrf)])
    def prepare_export(request: Request, slug: str):
        context, matter, _ = owner(request, slug)
        artifact = None
        installed = False
        registered = False
        try:
            with source_lock(bench, matter.matter_id):
                idle(bench, matter.matter_id)
                artifact = build_matter_archive(bench, matter, context.principal_id,
                    administrator_override=context.is_administrator)
                current_authority = refresh_owner(request, slug, context)
                install_archive(bench, matter, artifact)
                installed = True
                bench.workspace.case_lifecycle.register_export(matter.matter_id, context.principal_id,
                    artifact.exit_id, artifact.export_id, artifact.sha256, artifact.snapshot_digest,
                    session_id=session_id(current_authority), administrator_override=(current_authority.is_administrator and matter.owner_id != current_authority.principal_id))
                registered = True
                cleanup_archives(bench, matter.matter_id, keep=artifact.export_id)
        except (WorkspaceProblem, OSError, RuntimeError) as exc:
            message = str(exc) if isinstance(exc, WorkspaceProblem) else 'The full export could not be prepared safely. Try again after checking storage.'
            return redirect(slug, error=message)
        finally:
            if artifact is not None and not registered:
                if installed:
                    from .pilot_uploads import securely_delete_owned_tree
                    from .case_lifecycle_support import archive_directory
                    parent = archive_directory(bench, matter.matter_id)
                    securely_delete_owned_tree(parent / artifact.export_id, allowed_parent=parent)
                else:
                    artifact.cleanup()
        return redirect(slug, notice='Full export verified. Download it and check it outside this workbench before final confirmation.')

    @app.get('/matters/{slug}/close/export', dependencies=[Depends(require_response_lease)])
    def download_export(request: Request, slug: str):
        context, matter, _ = owner(request, slug)
        current = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
        if current is None or not current.export_id:
            raise HTTPException(409, 'Prepare a full export first.')
        from .case_archive_response import open_verified_archive, archive_response
        descriptor = None
        try:
            descriptor, size = open_verified_archive(bench, matter, current)
            current_authority = refresh_owner(request, slug, context)
            response = archive_response(request, matter, current, descriptor, size)
            from .case_archive_delivery import ArchiveDelivery
            downloads = bench.workspace.case_lifecycle.downloads
            attempt = downloads.begin(matter.matter_id, context.principal_id,
                session_id(current_authority), current.export_id, current.archive_digest,
                size, response.start, response.start + response.length,
                administrator_override=(current_authority.is_administrator and matter.owner_id != current_authority.principal_id))
            def completed():
                refresh_owner(request, slug, context)
                return downloads.complete(attempt)
            request.scope['state']['case_archive_delivery'] = ArchiveDelivery(
                response.delivery_proof, complete=completed, discard=lambda: downloads.discard(attempt))
            transferred = transfer_response_lease(request, response, through_send=True)
            descriptor = None
            return transferred
        except WorkspaceProblem as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @app.post('/matters/{slug}/close/confirm', dependencies=[Depends(require_csrf)])
    async def confirm_page(request: Request, slug: str, export_id: str = Form('', max_length=100),
                     mode: str = Form('export_exit', max_length=32)):
        context, matter, lifecycle = owner(request, slug)
        try:
            with bench.workspace._lock:
                bench.workspace.case_lifecycle._ready(matter.matter_id, export_id or None, context.principal_id, mode)
            challenge = await reauthorize(context, matter, action='purge', mode=mode)
            if challenge is not None:
                return challenge
            with source_lock(bench, matter.matter_id):
                idle(bench, matter.matter_id)
                current = bench.workspace.case_lifecycle.current_exit(matter.matter_id)
                if current is None:
                    raise WorkspaceProblem('Start an explicit exit first.')
                if lifecycle.state != 'purge_failed' and current.mode == 'export_exit':
                    verified_archive(bench, matter, current)
                current_authority = refresh_owner(request, slug, context)
                confirmation = bench.workspace.case_lifecycle.issue_confirmation(slug, context.principal_id,
                    session_id(current_authority), export_id, mode=mode, administrator_override=(current_authority.is_administrator and matter.owner_id != current_authority.principal_id))
        except WorkspaceProblem as exc:
            return redirect(slug, error=str(exc))
        return page(request, slug, confirmation=confirmation)

    @app.post('/matters/{slug}/close', dependencies=[Depends(require_csrf)])
    def purge(request: Request, slug: str, confirmed_name: str = Form('', max_length=140),
              acknowledge: str = Form('', max_length=8), export_acknowledged: str = Form('', max_length=8),
              token: str = Form('', max_length=128), export_id: str = Form('', max_length=100),
              mode: str = Form('export_exit', max_length=32),
              no_backup_acknowledged: str = Form('', max_length=8)):
        context, matter, _ = owner(request, slug)
        try:
            if acknowledge != 'yes':
                raise WorkspaceProblem('Confirm that you understand deletion cannot be undone.')
            matter, lifecycle = bench.begin_matter_purge(slug, context.principal_id, confirmed_name,
                administrator_override=(context.is_administrator and matter.owner_id != context.principal_id), token=token, session_id=session_id(context),
                export_id=export_id, export_acknowledged=export_acknowledged == 'yes',
                mode=mode, no_backup_acknowledged=no_backup_acknowledged == 'yes',
                refresh_authority=lambda: refresh_owner(request, slug, context))
            bench.execute_matter_purge(matter, lifecycle,
                refresh_authority=lambda: refresh_owner(request, slug, context))
        except WorkspaceProblem as exc:
            return redirect(slug, error=str(exc))
        except (OSError, RuntimeError):
            return redirect(slug, error='Deletion stopped safely. Review the current status before retrying.')
        return RedirectResponse('/matters/new?' + urlencode({'notice': 'Matter deleted. Its workbench data and processing copies were removed.'}),
                                status_code=303)
