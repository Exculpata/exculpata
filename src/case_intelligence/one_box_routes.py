"""Opt-in dispatch from one question box into existing review workflows."""
from __future__ import annotations

import os
import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import ask_router
from .exact_search import MAX_QUERY_CHARS

MAX_ONE_BOX_CHARS = 2_000
# Leave room below the gateway's default 8 KiB request line for HTTP syntax.
MAX_REDIRECT_BYTES = 7_000
ROUTE_LABELS = {
    "exact": "Exact search",
    "question": "Cited answer",
    "every_source": "Every-source review",
}


def _destination(slug, path, text, kind, **query):
    return f"/matters/{slug}{path}?" + urlencode({
        **query, "one_box_text": text, "one_box_kind": kind,
    })


def _answer_redirect(response, slug, text, kind):
    """Preserve the canonical answer response and its same-matter destination."""
    if response.status_code != 303:
        return response
    location = urlsplit(response.headers.get("location", ""))
    if location.scheme or location.netloc or location.path != f"/matters/{slug}":
        return response
    query = [(key, value) for key, value in parse_qsl(location.query, keep_blank_values=True)
             if key not in {"one_box_text", "one_box_kind"}]
    query.extend((("one_box_text", text), ("one_box_kind", kind)))
    response.headers["location"] = urlunsplit(("", "", location.path, urlencode(query), location.fragment))
    return response


def install_one_box_routes(app, *, authorized_matter, require_csrf, templates, ask, readiness_for, home):
    """Install one adapter; all answering remains in the supplied Ask handler.

    The feature is read once at application creation, using the existing
    automatic-discovery truthy values. The registered route remains a 404 when
    disabled, including requests that do not carry a valid CSRF token.
    """
    enabled = os.getenv("CASE_INTELLIGENCE_ONE_BOX", "").strip().casefold() in {
        "1", "true", "yes", "on",
    }

    def route_context(request, matter):
        if not enabled or matter is None:
            return None
        draft = getattr(request.state, "one_box_draft", None)
        text, kind = draft if draft is not None else (
            request.query_params.get("one_box_text", ""), request.query_params.get("one_box_kind", ""))
        if kind not in ROUTE_LABELS or not text.strip() or len(text) > MAX_ONE_BOX_CHARS:
            return None
        root = f"/matters/{matter.slug}"
        allowed = {root, root + "/exact-search", root + "/full-review", root + "/home"}
        if draft is not None:
            allowed.add(root + "/one-box")
        if request.url.path not in allowed:
            return None
        decision = ask_router.classify(text)
        reason = decision.reason
        if kind != decision.kind:
            reason += f" You selected {ROUTE_LABELS[kind].lower()}."
        return {
            "kind": kind,
            "text": text,
            "reason": reason,
            "alternatives": tuple({"kind": other, "label": label}
                                  for other, label in ROUTE_LABELS.items() if other != kind),
            "prefill": text if kind == "every_source" and request.url.path == root + "/full-review" else "",
        }

    templates.env.globals.update(one_box_enabled=enabled, one_box_context=route_context,
        one_box_request_key=lambda: "answer-request-" + uuid.uuid4().hex)

    def require_enabled():
        if not enabled:
            raise HTTPException(404, "Not found")

    @app.post("/matters/{slug}/one-box", dependencies=[Depends(require_enabled), Depends(require_csrf)])
    def one_box(
        request: Request,
        slug: str,
        question: str = Form(..., max_length=MAX_ONE_BOX_CHARS),
        route: str = Form("", pattern="^(|exact|question|every_source)$"),
        request_key: str = Form("", max_length=47),
    ):
        matter = authorized_matter(request, slug)
        decision = ask_router.classify(question)
        kind = route or decision.kind

        def recover(message):
            destination = _destination(slug, "/home", question, kind, error=message)
            if len(destination.encode("ascii")) > MAX_REDIRECT_BYTES:
                return render_recovery(message)
            return RedirectResponse(destination,
                                    status_code=303, headers={"Cache-Control": "no-store"})

        def render_recovery(message):
            request.state.one_box_draft = (question, kind)
            response = home(request=request, slug=slug, notice="", error=message)
            response.status_code = 422
            return response

        if not question.strip():
            return recover("Enter a question, search words or a description of the records you want to find.")
        # Check the actual encoded text, including its duplicate exact-search q.
        # Reserve space for canonical answer IDs and recovery messages. Refuse
        # before queueing work and render the unchanged draft without a redirect.
        candidate = _destination(slug, "/exact-search" if kind == "exact" else "/full-review",
                                 question, kind, **({"q": question} if kind == "exact" else {}))
        if len(candidate.encode("ascii")) + 512 > MAX_REDIRECT_BYTES:
            return render_recovery("This text is too long for a safe destination URL after encoding. Shorten it and try again; your text is unchanged.")
        readiness = readiness_for(matter.matter_id)
        if not readiness.can_query:
            return recover("Sources are not ready to query. Review source preparation, then try again.")
        if kind == "exact":
            if len(question) > MAX_QUERY_CHARS:
                return recover(f"Exact search accepts up to {MAX_QUERY_CHARS} characters. Shorten the text or choose another route; your text is unchanged.")
            return RedirectResponse(_destination(slug, "/exact-search", question, kind, q=question),
                                    status_code=303, headers={"Cache-Control": "no-store"})
        if kind == "every_source":
            # GET only prefills a fresh criterion form. The existing criterion
            # save and scope-confirmed run controls remain the only write paths.
            return RedirectResponse(_destination(slug, "/full-review", question, kind),
                                    status_code=303, headers={"Cache-Control": "no-store"})
        # FastAPI Form defaults are descriptor objects when called directly.
        # Pass every argument explicitly so this is exactly the ordinary fresh
        # focused-answer path, with no implicit notebook, source or research mode.
        response = ask(request=request, slug=slug, question=question, conversation="",
            request_key=request_key, source_set="", notebook_mode="", notebook_item=[],
            use_saved_context=False, expected_selection_revision=None, review_task="answer")
        return _answer_redirect(response, slug, question, kind)
