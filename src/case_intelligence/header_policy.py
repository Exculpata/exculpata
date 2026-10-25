"""Request header-family admission before identity or session processing."""

from collections.abc import Mapping

from .legacy_names import LEGACY_STEM, name_value


def header_family_allowed(headers: Mapping[str, str], family: str) -> bool:
    if family == "both":
        return True
    if family == "current":
        rejected_prefix = name_value("header_prefix_lower", LEGACY_STEM)
    elif family == "legacy":
        rejected_prefix = name_value("header_prefix_lower")
    else:
        return False
    for name in headers:
        if name.lower().startswith(rejected_prefix):
            return False
    return True
