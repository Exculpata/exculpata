"""Remove only this module's derived render cache during matter deletion."""
from pathlib import Path

from case_intelligence.pilot_uploads import securely_delete_owned_tree


def purge_hook(matter_root: Path, matter_id: str) -> None:
    if matter_root.name != matter_id or matter_root.is_symlink():
        raise RuntimeError("review cache matter boundary is unsafe")
    cache = matter_root / "review-render-cache"
    if cache.exists() or cache.is_symlink():
        securely_delete_owned_tree(cache, allowed_parent=matter_root)
