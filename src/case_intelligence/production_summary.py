"""Public production-summary API; naive stubs for the red-first 378c commit."""
from .bates_gaps import compute_bates_gaps


def parse_load_file(raw: bytes) -> list[dict[str, str]]:
    return []


def map_documents(documents, rows, load_file_id):
    return ()


def build_summary(name, slug, store, load_file_token=''):
    return {'name': name, 'slug': slug, 'facts': [], 'load_rows': []}


def render_markdown(payload):
    return ''


def render_json(payload):
    return '{}'
