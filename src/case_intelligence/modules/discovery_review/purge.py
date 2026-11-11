"""Remove only this module's derived review state during matter deletion."""
import re
from pathlib import Path

from case_intelligence.modules.discovery_review.reader import forget
from case_intelligence.pilot_uploads import securely_delete_owned_tree


def purge_hook(matter_root: Path, matter_id: str) -> None:
    if (re.fullmatch(r"ci-matter-[0-9a-f]{32}", matter_id) is None
            or matter_root.name != matter_id or matter_root.is_symlink()):
        raise RuntimeError("review cache matter boundary is unsafe")
    forget(matter_id)
    cache = matter_root / "review-render-cache"
    if cache.exists() or cache.is_symlink():
        securely_delete_owned_tree(cache, allowed_parent=matter_root)
