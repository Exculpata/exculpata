"""Optional human-authored matter orientation; no downstream consumption."""
import unicodedata

from .workspace_store import WorkspaceProblem

OBJECTIVE_CHARS, OBJECTIVE_BYTES = 500, 1024
BACKGROUND_CHARS, BACKGROUND_BYTES = 10000, 24000


class MatterPurposeConflict(WorkspaceProblem):
    pass


def validate_text(value, label, characters, byte_limit):
    if not isinstance(value, str):
        raise WorkspaceProblem(f'{label} must be plain text; use an empty string to clear it.')
    if len(value) > characters:
        raise WorkspaceProblem(f'{label} must be at most {characters:,} characters and {byte_limit:,} UTF-8 bytes.')
    try:
        encoded = value.encode('utf-8')
    except UnicodeEncodeError as exc:
        raise WorkspaceProblem(f'{label} must be valid UTF-8 text.') from exc
    if len(encoded) > byte_limit:
        raise WorkspaceProblem(f'{label} must be at most {characters:,} characters and {byte_limit:,} UTF-8 bytes.')
    if any(unicodedata.category(char) == 'Cc' and char not in '\r\n\t' for char in value):
        raise WorkspaceProblem(f'{label} contains unsupported control characters.')
    return value  # Preserve whitespace, Unicode and line endings exactly.


class MatterPurposeService:
    def __init__(self, repository):
        self.repository = repository

    def read(self, matter_id, actor_id, *, check_authority=lambda: None):
        repo = self.repository
        with repo.unit.reading(matter_id, actor_id):
            check_authority()
            repo.require_active(matter_id)
            result = repo.get(matter_id)
            repo.unit.read_authorize(matter_id, actor_id)
            check_authority()
            return result

    def update(self, matter_id, actor_id, *, expected_revision, objective, background,
               request_id, session_id=None, check_authority=lambda: None):
        repo = self.repository
        with repo.unit.transaction(matter_id, actor_id):
            check_authority()
            repo.require_active(matter_id)
            if type(expected_revision) is not int or expected_revision < 0:
                raise WorkspaceProblem('A displayed revision is required.')
            current = repo.get(matter_id)
            if current['revision'] != expected_revision:
                raise MatterPurposeConflict('The objective or background changed in another tab. '
                    'Your draft was not saved. Compare it with the current saved version before trying again.')
            objective = validate_text(objective, 'Objective', OBJECTIVE_CHARS, OBJECTIVE_BYTES)
            background = validate_text(background, 'Background', BACKGROUND_CHARS, BACKGROUND_BYTES)
            if (objective, background) != (current['objective'], current['background']):
                current = repo.save(matter_id, actor_id, objective, background,
                                    expected_revision + 1, request_id, session_id)
            # Late failures roll back both the content and its audit event.
            repo.unit.authorize(matter_id, actor_id)
            check_authority()
            return current
