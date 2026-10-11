"""Draft witness questions and discovery requests from an answer's passages.

The workbench still owns membership, source guards, citation checks and notebook
storage. These methods and routes only coordinate that boundary: passages are
read under the guards, the model runs after the guards are released, and a draft
is shown or saved only when the cited passages still match.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Mapping, Sequence

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse

from .generation import EvidenceItem, GenerationRejected, GenerationUnavailable
from .store_records import MatterRecord, NotebookItemRecord, WorkspaceProblem


class AnswerQuestionDrafts:
    """Answer-passage question drafts for CaseIntelligenceWorkbench."""

    def _answer_passages(self, matter: MatterRecord, conversation_id: str, message_id: str,
                         ) -> tuple[str, list[tuple[EvidenceItem, dict[str, object]]]]:
        """The answer's distinct cited passages, in order, verified together; all or nothing.

        Returns the reviewer's question that the answer replied to, and each
        passage as model evidence (S1, S2, ...) with its canonical reference.
        The order is deterministic, so passage identifiers in a draft name the
        same passages when it is saved. Call under the source mutation guard
        and the workspace lock.
        """
        from .generation import MAX_EVIDENCE_CHARS, MAX_EVIDENCE_ITEMS

        messages = self.workspace.messages(matter.matter_id, conversation_id)
        position = next((index for index, item in enumerate(messages)
                         if item.message_id == message_id and item.role == "assistant"), None)
        if position is None or messages[position].payload.get("kind") != "generated":
            raise KeyError(message_id)
        topic = next((item.content for item in reversed(messages[:position]) if item.role == "user"), "")
        payload = messages[position].payload
        claims = payload.get("claims")
        # A source-backed limitation is shown with the answer, so its passages are
        # drafted from too, as Report copy includes them.
        limitation = (payload.get("source_limitation") if "source_limitation" in payload else
                      payload.get("limitation") if not payload.get("verification_notice") else None)
        groups = [*(claims if isinstance(claims, list) else ()),
                  *((limitation,) if isinstance(limitation, Mapping) else ())]
        citations, tokens = [], set()
        for claim in groups:
            values = claim.get("citations") if isinstance(claim, Mapping) else None
            for citation in values if isinstance(values, list) else ():
                if not isinstance(citation, Mapping):
                    continue
                token = self._citation_support_token(citation)
                if token and token not in tokens and len(citations) < MAX_EVIDENCE_ITEMS:
                    tokens.add(token)
                    citations.append(citation)
        if not citations:
            raise WorkspaceProblem("That answer no longer has source support.")
        try:
            units = self._verified_answer_units(matter, citations)
        except WorkspaceProblem as exc:
            raise WorkspaceProblem("The cited passages changed or are unavailable, so nothing was drafted.") from exc
        passages: list[tuple[EvidenceItem, dict[str, object]]] = []
        total = 0
        for number, (reference, _document, _unit, _ordinal) in enumerate(units, 1):
            # The display prefix (up to 6,000 characters) is what the model reads.
            excerpt = str(reference["excerpt"])
            if total + len(excerpt) > MAX_EVIDENCE_CHARS:
                break
            total += len(excerpt)
            passages.append((EvidenceItem(f"S{number}", str(reference["source_name"]), str(reference["location"]),
                                          excerpt, "transcript" if reference.get("kind") == "transcript" else "document",
                                          str(reference["document_id"])), dict(reference)))
        return str(topic or ""), passages

    def draft_answer_questions(self, matter: MatterRecord, actor_id: str, conversation_id: str,
                               message_id: str, purpose: str) -> dict[str, object]:
        """Draft witness questions or discovery requests from an answer's cited passages.

        Nothing is saved. Passages are read and verified under the guards, which
        are released before the model is called, then read again under the guards
        afterwards; if access or any passage changed, nothing is returned.
        """
        from .question_drafting import PURPOSES, draft_questions, passage_basis

        if purpose not in PURPOSES:
            raise WorkspaceProblem("Choose witness questions or discovery requests.")
        self.workspace.membership(matter.matter_id, actor_id)
        with self.source_store(matter).mutation_guard(), self.workspace._lock:
            self.workspace.membership(matter.matter_id, actor_id)
            topic, passages = self._answer_passages(matter, conversation_id, message_id)
        evidence = [passage[0] for passage in passages]
        draft = draft_questions(self.generator, purpose, topic, evidence)
        # Access may have been revoked, or a cited passage changed, during the model
        # call: nothing is returned then, so a draft is only shown against current passages.
        with self.source_store(matter).mutation_guard(), self.workspace._lock:
            self.workspace.membership(matter.matter_id, actor_id)
            _topic, current = self._answer_passages(matter, conversation_id, message_id)
            if [passage[0] for passage in current] != evidence:
                raise WorkspaceProblem("The cited passages changed while drafting, so nothing was drafted.")
        labels = {item.evidence_id: f"{item.source_name} · {item.location}" for item in evidence}
        return {
            "purpose": purpose, "label": PURPOSES[purpose][0], "notice": draft.notice,
            "basis": passage_basis(evidence),
            "questions": [{"text": question.text,
                           "passages": [int(identifier[1:]) for identifier in question.evidence_ids],
                           "sources": [labels[identifier] for identifier in question.evidence_ids]}
                          for question in draft.questions],
        }

    def save_answer_questions(self, matter: MatterRecord, actor_id: str, conversation_id: str,
                              message_id: str, purpose: str,
                              questions: Sequence[tuple[str, Sequence[int]]],
                              basis: str = "") -> tuple[NotebookItemRecord, bool]:
        """Save a reviewed draft as one Suggested note, after checking it again.

        The draft's basis must match the answer's current passages exactly, and every
        question is re-verified against them; if a passage changed (even a corrected
        transcript that keeps its identity), or a question no longer passes, nothing
        is saved.
        """
        from .question_drafting import MAX_DRAFTED_QUESTIONS, PURPOSES, passage_basis, verify_question

        if purpose not in PURPOSES:
            raise WorkspaceProblem("Choose witness questions or discovery requests.")
        if not 1 <= len(questions) <= MAX_DRAFTED_QUESTIONS:
            raise WorkspaceProblem(f"Save between 1 and {MAX_DRAFTED_QUESTIONS} questions.")
        self.workspace.membership(matter.matter_id, actor_id)
        with self.source_store(matter).mutation_guard(), self.workspace._lock:
            self.workspace.membership(matter.matter_id, actor_id)
            topic, passages = self._answer_passages(matter, conversation_id, message_id)
            if not hmac.compare_digest(basis.encode(), passage_basis([passage[0] for passage in passages]).encode()):
                raise WorkspaceProblem("The cited passages changed since this draft. Draft the questions again.")
            evidence = {passage[0].evidence_id: passage for passage in passages}
            lines, used = [], []
            for number, (text, passages) in enumerate(questions, 1):
                identifiers = [f"S{passage}" for passage in passages]
                if any(identifier not in evidence for identifier in identifiers):
                    raise WorkspaceProblem("A cited passage changed or is unavailable. Draft the questions again.")
                question = verify_question(text, identifiers, {key: value[0] for key, value in evidence.items()})
                if question is None:
                    raise WorkspaceProblem("A question no longer passes the citation and text checks. Draft the questions again.")
                for identifier in question.evidence_ids:
                    if identifier not in used:
                        used.append(identifier)
                sources = "; ".join(f"{evidence[i][0].source_name} · {evidence[i][0].location}" for i in question.evidence_ids)
                lines.append(f"{number}. {question.text}\n   Sources: {sources}")
            body = "\n".join(lines)
            title = self._notebook_title(f"{PURPOSES[purpose][1]}: {topic}" if topic.strip() else PURPOSES[purpose][1])
            # Bind the dedupe key to the cited passages' identities, not only the text:
            # two passages can share a display name and location.
            basis = json.dumps([body, [evidence[identifier][1] for identifier in used]],
                               sort_keys=True, ensure_ascii=False, default=str)
            digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]
            return self.workspace.create_notebook_item(
                matter.matter_id, actor_id, item_type="note", status="suggested", title=title, body=body,
                origin="answer", source_conversation_id=conversation_id, source_message_id=message_id,
                dedupe_key=f"questions:{message_id}:{purpose}:{digest}",
                references=tuple(evidence[identifier][1] for identifier in used),
            )


def register_answer_question_routes(
    app, *, bench, templates, auth_context, base_context, require_csrf,
    authorized_matter, audit, answer_feedback, query_url, review_return_href,
):
    """POST routes that draft questions from an answer and save the reviewed set."""
    _answer_feedback = answer_feedback
    _query_url = query_url
    _source_review_return_href = review_return_href

    @app.post(
        "/matters/{slug}/conversations/{conversation_id}/messages/{message_id}/questions",
        dependencies=[Depends(require_csrf)],
    )
    def draft_answer_questions(
        request: Request,
        slug: str,
        conversation_id: str,
        message_id: str,
        purpose: str = Form("", max_length=20),
        return_to: str = Form("", max_length=4000),
    ):
        context = auth_context(request)
        wants_json = "application/json" in request.headers.get("accept", "")
        return_path = _source_review_return_href(slug, return_to)

        def refuse(message: str, status: int):
            if wants_json:
                return JSONResponse({"message": message}, status_code=status, headers={"Cache-Control": "no-store"})
            return _answer_feedback(slug, conversation_id, return_path, message, error=True)
        try:
            matter = authorized_matter(request, slug)
            draft = bench.draft_answer_questions(matter, context.principal_id, conversation_id, message_id, purpose)
        except KeyError as exc:
            raise HTTPException(404, "Answer not found") from exc
        except WorkspaceProblem as exc:
            return refuse(str(exc), 409)
        except GenerationUnavailable as exc:
            return refuse(str(exc), 503)
        except GenerationRejected:
            return refuse("No drafted question passed the citation and text checks. Try again.", 409)
        audit(request, "answer.draft_questions", "success", context=context, matter=matter,
              object_type="message", object_id=message_id,
              details={"count": len(draft["questions"]), "type": "answer"})
        save_url = f"/matters/{slug}/conversations/{conversation_id}/messages/{message_id}/questions/save"
        if wants_json:
            return JSONResponse({**draft, "save_url": save_url}, headers={"Cache-Control": "no-store"})
        response = templates.TemplateResponse(
            request=request,
            name="workbench_question_draft.html",
            context={**base_context(request, matter), "matter": matter, "draft": draft, "save_url": save_url,
                     "back_url": return_path or _query_url(f"/matters/{slug}", conversation=conversation_id) + "#latest",
                     "return_to": return_path or ""},
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post(
        "/matters/{slug}/conversations/{conversation_id}/messages/{message_id}/questions/save",
        dependencies=[Depends(require_csrf)],
    )
    def save_answer_questions(
        request: Request,
        slug: str,
        conversation_id: str,
        message_id: str,
        purpose: str = Form("", max_length=20),
        question: list[str] = Form(default=[]),
        passages: list[str] = Form(default=[]),
        basis: str = Form("", max_length=64),
        return_to: str = Form("", max_length=4000),
    ):
        context = auth_context(request)
        wants_json = "application/json" in request.headers.get("accept", "")
        return_path = _source_review_return_href(slug, return_to)
        try:
            if len(question) != len(passages) or len(question) > 8:
                raise WorkspaceProblem("The drafted questions are incomplete. Draft them again.")
            parsed = []
            for text, cited in zip(question, passages):
                numbers = [part.strip() for part in cited.split(",") if part.strip()]
                if len(text) > 400 or not 1 <= len(numbers) <= 4 or not all(re.fullmatch(r"[1-9]|1[0-2]", n) for n in numbers):
                    raise WorkspaceProblem("The drafted questions are incomplete. Draft them again.")
                parsed.append((text, [int(n) for n in numbers]))
            matter = authorized_matter(request, slug)
            item, created = bench.save_answer_questions(
                matter, context.principal_id, conversation_id, message_id, purpose, parsed, basis)
        except KeyError as exc:
            raise HTTPException(404, "Answer not found") from exc
        except WorkspaceProblem as exc:
            if wants_json:
                return JSONResponse({"message": str(exc)}, status_code=409, headers={"Cache-Control": "no-store"})
            return _answer_feedback(slug, conversation_id, return_path, str(exc), error=True)
        audit(request, "notebook.capture_questions", "success", context=context, matter=matter,
              object_type="notebook_item", object_id=item.item_id,
              details={"created": created, "state": item.status, "count": len(parsed)})
        notice = ("Questions saved to case notes for review." if created
                  else "Those questions were already saved to case notes.")
        notebook_url = f"/matters/{slug}/notebook"
        if wants_json:
            return JSONResponse({"message": notice, "created": created, "item_id": item.item_id,
                                 "notebook_url": notebook_url}, headers={"Cache-Control": "no-store"})
        return _answer_feedback(slug, conversation_id, return_path, notice)
