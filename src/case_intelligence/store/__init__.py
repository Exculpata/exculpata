"""Shared control-store validation constants."""

import re

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{15,80}$")
_SOURCE_DOCUMENT = re.compile(r"^(?:[0-9a-f]{32}|[a-z][a-z0-9-]{15,80})$")
_NOTEBOOK_ITEM = re.compile(r"^notebook-item-[0-9a-f]{32}$")
NOTEBOOK_TYPES = ("fact", "issue", "person", "place", "date", "event", "note")
NOTEBOOK_STATUSES = ("suggested", "confirmed", "disputed", "needs_review", "dismissed")
NOTEBOOK_ORIGINS = ("manual", "answer", "citation", "extraction")

_MEDIA_CLIP = re.compile(r"^media-clip-[0-9a-f]{32}$")
_REPORT = re.compile(r"^report-[0-9a-f]{32}$")
_REPORT_SECTION = re.compile(r"^report-section-[0-9a-f]{32}$")
MAX_REPORT_CITATION_EXCERPT_CHARS = 6_000
MAX_REPORT_SECTION_CITATIONS = 100
