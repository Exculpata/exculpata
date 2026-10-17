"""Cooperatively bounded live coverage reads; no persisted or inferred totals."""
from dataclasses import dataclass
import time


@dataclass(frozen=True)
class CoverageLimits:
    documents: int = 1000
    units: int = 10000
    input_chars: int = 8 * 1024 * 1024
    text_chars: int = 8 * 1024 * 1024
    record_chars: int = 65536
    seconds: float = 2.0


class _CoverageLimit(Exception):
    pass


def collect_timeline_units(documents, candidate, *, limits=CoverageLimits(), clock=time.monotonic):
    """Return the bounded candidate prefix and an exact total only on exhaustion.

    Source/authorization guards remain the caller's responsibility. The deadline
    is cooperative: check before and after each read/decode and candidate build.
    Reader chunks and individual records are bounded even before the next yield.
    """
    deadline = clock() + limits.seconds
    input_chars = text_chars = total = 0
    units = []

    def check():
        if clock() >= deadline:
            raise _CoverageLimit()

    def charge_read(chars):
        nonlocal input_chars
        input_chars += chars
        if input_chars > limits.input_chars:
            raise _CoverageLimit()
        check()

    iterator = iter(documents)
    try:
        for _ in range(limits.documents):
            check()
            document = next(iterator, None)
            check()
            if document is None:
                return units, total, True
            start = len(units)
            parsed = document.iter_parsed_units(budget_check=check, read_check=charge_read,
                                                max_record_chars=limits.record_chars)
            try:
                ordinal = 0
                while True:
                    check()
                    if total >= limits.units:
                        raise _CoverageLimit()
                    unit = next(parsed, None)
                    check()
                    if unit is None:
                        break
                    if not isinstance(unit.text, str):
                        raise ValueError('Invalid passage text')
                    text_chars += len(unit.text)
                    if len(unit.text) > limits.record_chars or text_chars > limits.text_chars:
                        raise _CoverageLimit()
                    ordinal += 1
                    total += 1
                    if len(units) < 2501:
                        units.append(candidate(document, unit, ordinal))
                    check()
            except _CoverageLimit:
                # The footer has not been validated when a budget interrupts
                # this iterator. Retain only earlier, fully read sources.
                del units[start:]
                raise
            except (OSError, ValueError, TypeError, RuntimeError):
                # A malformed container may have yielded a valid-looking prefix.
                # Do not offer entries from that unreadable source as usable.
                del units[start:]
                return units, None, False
            finally:
                parsed.close()
    except _CoverageLimit:
        pass
    # Hitting a budget is not proof that the population ended at that point.
    return units, None, False
