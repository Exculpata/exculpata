"""Fixed compatibility sample for single-precision embedding vectors."""
import hashlib
import math
import struct


PROBES = (
    "A synthetic bicycle has a silver bell.",
    "Rain filled the synthetic garden pond.",
    "The synthetic ledger records seven boxes.",
)
MISMATCH = "The synthetic embedding fingerprint changed. Rebuild the embedding index to resume meaning search."


def vector_fingerprint(vectors):
    """Hash the ordered IEEE binary32 components used by pgvector."""
    if not isinstance(vectors, (list, tuple)) or len(vectors) != len(PROBES):
        raise ValueError("Incomplete synthetic embedding response.")
    packed = []
    for row in vectors:
        if (not isinstance(row, (list, tuple)) or len(row) != 768
                or any(type(x) not in (float, int) or not math.isfinite(x) for x in row)):
            raise ValueError("Invalid synthetic embedding vector.")
        try:
            data = struct.pack("<768f", *row)
        except (OverflowError, struct.error):
            raise ValueError("Invalid synthetic embedding component.") from None
        if not any(struct.unpack("<768f", data)) or data in packed:
            raise ValueError("Synthetic inputs require distinct, nonzero embeddings.")
        packed.append(data)
    return hashlib.sha256(b"".join(packed)).hexdigest()
