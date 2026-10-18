"""Deterministically bounded coverage reads; no persisted or inferred totals."""
from dataclasses import dataclass
import time


@dataclass(frozen=True)
class CoverageLimits:
    documents: int = 1000
    units: int = 10000
    input_chars: int = 8 * 1024 * 1024
    text_chars: int = 8 * 1024 * 1024
    record_chars: int = 65536
    seconds: float | None = None


class _CoverageLimit(Exception):
    pass


def collect_timeline_units(documents, candidate, *, limits=CoverageLimits(), clock=time.monotonic,
                           coverage_records=None):
    """Return the bounded candidate prefix and an exact total only on exhaustion.

    Source/authorization guards remain the caller's responsibility. An optional
    UI deadline is cooperative; the default limits do not depend on clock speed.
    Reader chunks and individual records are bounded even before the next yield.
    """
    deadline = None if limits.seconds is None else clock() + limits.seconds
    input_chars = text_chars = total = 0
    units = []
    records = [] if coverage_records is None else coverage_records

    def gap(document, status):
        records.append(dict(source_id=getattr(document, 'document_id', ''),
                            source_name=getattr(document, 'display_name', 'Source'), status=status))

    def check():
        if deadline is not None and clock() >= deadline:
            raise _CoverageLimit()

    def charge_read(chars):
        nonlocal input_chars
        input_chars += chars
        if input_chars > limits.input_chars:
            raise _CoverageLimit()
        check()

    iterator = iter(documents)
    try:
        # One bounded lookahead distinguishes an exact ceiling from truncation.
        for index in range(limits.documents + 1):
            check()
            document = next(iterator, None)
            check()
            if document is None:
                return units, None if records else total, not records
            if index == limits.documents:
                gap(document, 'limit')
                break
            start = len(units)
            parsed = document.iter_parsed_units(budget_check=check, read_check=charge_read,
                                                max_record_chars=limits.record_chars)
            try:
                ordinal = 0
                while True:
                    check()
                    unit = next(parsed, None)
                    check()
                    if unit is None:
                        break
                    if total >= limits.units:
                        raise _CoverageLimit()
                    if not isinstance(unit.text, str):
                        raise ValueError('Invalid passage text')
                    text_chars += len(unit.text)
                    if text_chars > limits.text_chars:
                        raise _CoverageLimit()
                    if len(unit.text) > limits.record_chars:
                        raise ValueError('Passage exceeds the record limit')
                    ordinal += 1
                    total += 1
                    if len(units) < 2501:
                        units.append(candidate(document, unit, ordinal))
                    check()
            except _CoverageLimit:
                # The footer has not been validated when a budget interrupts
                # this iterator. Retain only earlier, fully read sources.
                del units[start:]
                gap(document, 'limit')
                raise
            except (OSError, ValueError, TypeError, RuntimeError):
                # A malformed container may have yielded a valid-looking prefix.
                # Do not offer entries from that unreadable source as usable.
                del units[start:]
                gap(document, 'unreadable')
                # Keep the global input/passage budgets charged, and continue
                # to later sources without claiming an exact population total.
            finally:
                parsed.close()
    except _CoverageLimit:
        pass
    # Hitting a budget is not proof that the population ended at that point.
    return units, None, False
