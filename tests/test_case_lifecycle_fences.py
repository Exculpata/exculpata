"""Frozen content is protected even when a stale writer skips route checks."""

from tests.fixture_write import fixture_write

from tests.case_lifecycle_helpers import lifecycle_session
import sqlite3

import pytest

from case_intelligence.workspace_store import WorkspaceStore
from tests.test_case_lifecycle import OWNER, MEMBER, setup


def test_content_fence_blocks_direct_and_indirect_mutations_and_survives_restart(setup):
    store, matter, clock = setup
    conversation = store.get_conversation(matter.matter_id)
    message = store.append_message(matter.matter_id, conversation.conversation_id, 'user',
                                   'Generated pre-exit message')
    store.case_lifecycle.begin_exit(matter.slug, OWNER, session_id=lifecycle_session(store, OWNER, administrator_override=False).session_id)
    original = store.get_conversation_any(matter.matter_id, conversation.conversation_id)
    with pytest.raises(sqlite3.IntegrityError, match='frozen'):
        store.rename_conversation(matter.matter_id, conversation.conversation_id, 'Generated stale rename')
    assert store.get_conversation_any(matter.matter_id, conversation.conversation_id) == original
    for statement, parameters in [
        ('UPDATE workbench_message SET content=? WHERE message_id=?', ('Generated late edit', message.message_id)),
        ('DELETE FROM workbench_message WHERE message_id=?', (message.message_id,)),
        ('INSERT INTO workbench_message VALUES (?,?,?,?,?,?,?)',
         ('message-' + 'f' * 32, conversation.conversation_id, 2, 'user', 'Generated late message', '{}', store._now())),
    ]:
        with pytest.raises(sqlite3.IntegrityError, match='frozen'):
            with fixture_write(store) as connection:
                connection.execute(statement, parameters)
    reopened = WorkspaceStore(store.path, clock=clock)
    try:
        assert reopened.get_conversation_any(matter.matter_id, conversation.conversation_id) == original
        assert reopened.matter_lifecycle(matter.matter_id).state == 'closing'
        with pytest.raises(sqlite3.IntegrityError, match='frozen'):
            reopened.rename_conversation(matter.matter_id, conversation.conversation_id, 'Generated restart edit')
    finally:
        reopened.close()


def test_migration_restores_deleted_media_cleanup_trigger_immediately(setup):
    store, _, _ = setup
    with store._lock:
        trigger = store.connection.execute("SELECT sql FROM sqlite_master WHERE type='trigger' "
                                           "AND name='workbench_deleted_matter_media_cleanup'").fetchone()
    assert trigger is not None
    assert 'DELETE FROM workbench_media_clip' in trigger[0]
    assert 'DELETE FROM workbench_media_job' in trigger[0]


def test_recovery_reads_are_owner_only_for_failed_legacy_state(setup):
    store, matter, _ = setup
    with fixture_write(store):
        store.connection.execute("UPDATE workbench_matter_lifecycle SET state='purge_failed' WHERE matter_id=?",
                                 (matter.matter_id,))
    with pytest.raises(KeyError):
        store.case_lifecycle.read_matter(matter.slug, MEMBER)
    assert store.case_lifecycle.read_matter(matter.slug, OWNER) == matter
