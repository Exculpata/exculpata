"""Source registry startup with a strictly read-only archive path."""
from __future__ import annotations

import json
import sqlite3


def load_readonly_registry(store):
    """Read persisted metadata without repair, migration, cleanup or projection."""
    from .pilot_uploads import _SOURCE_REGISTRY_MARKER

    for directory in (store.root, store.files, store.staging, store.incoming,
                      store.derived, store.quarantine_root):
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError("Frozen source storage is unavailable or unsafe.")
    if store.registry_path.is_symlink() or not store.registry_path.is_file():
        raise RuntimeError("The source registry requires recovery before export.")
    if store.manifest_path.is_symlink() or not store.manifest_path.is_file():
        raise RuntimeError("The source registry marker is unavailable.")
    try:
        marker = json.loads(store.manifest_path.read_text(encoding="utf-8"))
        if marker != _SOURCE_REGISTRY_MARKER:
            raise RuntimeError("The source registry requires recovery before export.")
        connection = sqlite3.connect(store.registry_path.resolve().as_uri() + "?mode=ro",
                                     uri=True, check_same_thread=False, timeout=30)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        version = connection.execute(
            "SELECT value FROM source_registry_metadata WHERE key='schema_version'").fetchone()
        if version is None or version[0] != "1":
            raise RuntimeError("The source registry schema is unsupported.")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("The source registry integrity check failed.")
        store._registry = connection
        for document in store._registry_documents():
            if document.document_id in store.documents:
                raise RuntimeError("The source registry has duplicate identities.")
            document._units_loader = store._load_units
            document._units_iterator = store._iter_units
            store.documents[document.document_id] = document
    except Exception:
        if store._registry is not None:
            store._registry.close()
            store._registry = None
        elif 'connection' in locals():
            connection.close()
        raise


def load_source_registry(self) -> None:
    from .pilot_uploads import _SOURCE_REGISTRY_MARKER
    legacy = False
    loaded: list[PilotDocument]
    manifest_payload: object | None = None
    if self.manifest_path.exists() or self.manifest_path.is_symlink():
        if self.manifest_path.is_symlink() or not self.manifest_path.is_file():
            raise RuntimeError("pilot manifest is not a regular file")
        try:
            manifest_payload = json.loads(
                self.manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("pilot manifest could not be loaded") from exc

    if self.registry_path.exists() or self.registry_path.is_symlink():
        self._registry = self._open_registry(self.registry_path)
        loaded = self._registry_documents()
        if manifest_payload is not None:
            if not isinstance(manifest_payload, dict) or manifest_payload.get(
                "version"
            ) not in {2, 3, 4, 5}:
                raise RuntimeError("pilot manifest could not be loaded")
            if manifest_payload.get("version") == 5 and (
                manifest_payload != _SOURCE_REGISTRY_MARKER
            ):
                raise RuntimeError("pilot manifest could not be loaded")
    elif manifest_payload is not None:
        if not isinstance(manifest_payload, dict):
            raise RuntimeError("pilot manifest could not be loaded")
        if manifest_payload.get("version") == 5:
            raise RuntimeError("pilot source registry is unavailable")
        if manifest_payload.get("version") not in {2, 3, 4} or not isinstance(
            manifest_payload.get("documents"), list
        ):
            raise RuntimeError("pilot manifest could not be loaded")
        try:
            loaded = [
                self._decode_document(item)
                for item in manifest_payload["documents"]
            ]
        except (TypeError, ValueError) as exc:
            raise RuntimeError("pilot manifest could not be loaded") from exc
        legacy = True
    else:
        loaded = []

    changed: list[PilotDocument] = []
    for document in loaded:
        before = self._document_payload(document)
        document._units_loader = self._load_units
        document._units_iterator = self._iter_units
        if document.origin == "upload":
            path = self.files / document.stored_name
            if path.parent != self.files or path.is_symlink() or not path.is_file():
                document.state = "missing"
                document.message = "Stored file is unavailable. Ask the administrator to restore it, then restart the workbench."
                document.units = []
                document.units_file = ""
            elif not self._stored_file_matches(path, document):
                document.state = "changed"
                document.message = "Stored file changed after processing. Remove it and upload the intended file again."
                document.units = []
                document.units_file = ""
        elif document.origin != "registered":
            raise RuntimeError("pilot source registry contains an invalid source origin")
        if document.units_file and not self._units_file_is_safe(document.units_file):
            document.state = "failed"
            document.message = "Derived searchable text is unavailable. Choose Try again."
            document.units_file = ""
        if not document.has_video:
            document.playback_name = ""
            document.playback_media_type = document.media_type
            document.playback_size = document.size
            document.playback_state = "original"
            document.playback_message = "The original recording is ready to play."
        elif document.playback_state == "processing":
            document.playback_state = "queued"
            document.playback_message = "Browser playback preparation will resume."
        elif document.playback_state == "ready" and not self._playback_file_is_safe(
            document
        ):
            document.playback_name = ""
            document.playback_media_type = ""
            document.playback_size = 0
            document.playback_state = "queued"
            document.playback_message = "Browser playback preparation will resume."
        if document.document_id in self.documents:
            raise RuntimeError("pilot source registry contains duplicate source identities")
        self.documents[document.document_id] = document
        if before != self._document_payload(document):
            changed.append(document)
    self._reconcile_files()
    self._reconcile_derived()

    if legacy:
        for document in loaded:
            self._write_units(document)
        self._create_registry_atomic(tuple(loaded))
    elif self._registry is None:
        self._create_registry_atomic(())
    elif changed:
        self._persist_registry_delta(tuple(changed))
    self._write_manifest_marker()
