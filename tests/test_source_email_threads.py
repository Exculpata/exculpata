from __future__ import annotations

import mailbox
from email.message import EmailMessage

from case_intelligence.extended_extract import extract_email
from case_intelligence.source_email_threads import (
    detect_quoted_history,
    group_threads,
    read_eml,
    read_mbox,
)


def _message(
    message_id: str | None,
    subject: str,
    body: str,
    *,
    reply_to: str | None = None,
    references: str | None = None,
    charset: str = "utf-8",
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = "sender@example.test"
    message["To"] = "reader@example.test"
    message["Subject"] = subject
    if message_id:
        message["Message-ID"] = message_id
    if reply_to:
        message["In-Reply-To"] = reply_to
    if references:
        message["References"] = references
    message.set_content(body, charset=charset)
    return message


def test_five_message_thread_reply_forward_and_exact_quoted_spans(tmp_path):
    messages = [
        _message("<m1@example.test>", "Generated plan", "Initial text."),
        _message(
            "<m2@example.test>",
            "Re: Generated plan",
            "Second note.\n\nOn Tue, sender wrote:\n> Initial text.",
            reply_to="<m1@example.test>",
            references="<m1@example.test>",
        ),
        _message(
            "<m3@example.test>",
            "Re: Generated plan",
            "Third note.\n\n> Second note.\n> Initial text.",
            reply_to="<m2@example.test>",
            references="<m1@example.test> <m2@example.test>",
        ),
        _message(
            "<m4@example.test>",
            "Fwd: Generated plan",
            "Forwarding.\n\n---------- Forwarded message ---------\nMessage-ID: <m2@example.test>\nFrom: sender@example.test\nSubject: Generated plan\n\nSecond note.",
            references="<m1@example.test> <m2@example.test>",
        ),
        _message(
            "<m5@example.test>",
            "Re: Generated plan",
            "Final note.\n\n-----Original Message-----\nMessage-ID: <m4@example.test>\nFrom: sender@example.test\n\nForwarding.",
            reply_to="<m4@example.test>",
        ),
    ]
    records = []
    for index, message in enumerate(messages):
        path = tmp_path / f"generated-{index}.eml"
        path.write_bytes(message.as_bytes())  # fixture-write: synthetic email fixture
        records.append(read_eml(path))

    threads = group_threads(records)

    assert len(threads) == 1
    assert [(node.message.message_id, node.parent_id) for node in threads[0].messages] == [
        ("m1@example.test", None),
        ("m2@example.test", "m1@example.test"),
        ("m3@example.test", "m2@example.test"),
        ("m4@example.test", "m2@example.test"),
        ("m5@example.test", "m4@example.test"),
    ]
    for record in records:
        for span in record.quoted_spans:
            assert record.body[span.start : span.end] == span.text
    assert records[1].quoted_spans[0].message_id is None
    assert records[3].quoted_spans[0].message_id == "m2@example.test"
    assert records[4].quoted_spans[0].message_id == "m4@example.test"


def test_broken_references_chain_and_subject_only_fallback(tmp_path):
    inputs = [
        _message("<root@example.test>", "Generated status", "Root."),
        _message(
            "<reply@example.test>",
            "Re: Generated status",
            "Reply.",
            references="<missing@example.test> <root@example.test>",
        ),
        _message(None, "RE: [External] Generated status", "Headerless."),
    ]
    records = []
    for index, message in enumerate(inputs):
        path = tmp_path / f"chain-{index}.eml"
        path.write_bytes(message.as_bytes())  # fixture-write: synthetic email fixture
        records.append(read_eml(path))

    thread = group_threads(records)[0]

    assert thread.messages[1].parent_id == "root@example.test"
    assert thread.messages[2].weak_link is True
    assert thread.messages[2].parent_id == "root@example.test"


def test_mbox_mixed_encodings_and_malformed_record(tmp_path):
    path = tmp_path / "generated.mbox"
    box = mailbox.mbox(path, create=True)
    try:
        box.add(_message("<utf@example.test>", "Generated encodings", "Snowman: ☃"))
        box.add(
            _message(
                "<latin@example.test>",
                "Re: Generated encodings",
                "Café reply.\n\n> Snowman",
                reply_to="<utf@example.test>",
                charset="iso-8859-1",
            )
        )
        box.add(b"Subject: broken\nContent-Type: multipart/mixed; boundary=absent\n\nbroken")
        box.flush()  # fixture-write: synthetic mailbox fixture
    finally:
        box.close()

    records = read_mbox(path)

    assert [record.body for record in records[:2]] == ["Snowman: ☃\n", "Café reply.\n\n> Snowman\n"]
    assert records[2].error and records[2].body == ""


def test_existing_email_extraction_is_unchanged_by_thread_read(tmp_path):
    path = tmp_path / "stable.eml"
    path.write_bytes(  # fixture-write: synthetic email fixture
        _message("<stable@example.test>", "Generated stable", "Stable body.").as_bytes()
    )
    before = extract_email(path)

    read_eml(path)

    assert extract_email(path) == before


def test_quote_detector_handles_leadin_without_prefixed_lines():
    body = "New text.\n\nOn Tuesday, sender wrote:\nQuoted text without prefix."
    spans = detect_quoted_history(body)
    assert len(spans) == 1
    assert spans[0].text == "On Tuesday, sender wrote:\nQuoted text without prefix."
