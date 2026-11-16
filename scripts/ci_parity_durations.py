"""Check reported pytest phase totals against the command's timeout.

Only the slowest-duration sections are evidence; unreported phases are unknown.
An absent section is reported explicitly without inventing a test failure.
"""

import math
import re
import shlex


# Hosted runners are about 2.7 to 3.0 times slower, so more than 30% locally
# can time out hosted; never raise this to make a test pass.
NEAR_LIMIT_FRACTION = 0.30
ACCEPTED_EXCEPTIONS = {
    'tests/test_discovery_review_index.py::test_rebuild_step_row_budget[10000]':
        (135, '2026-11-15T15:24'),
    'tests/test_discovery_review_index_lifecycle.py::'
    'test_single_heavily_coded_item_stays_pending_with_complete_values_on_resume':
        (125, '2026-11-15T17:17'),
}
_ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
_HEADER = re.compile(r'^=+\s+slowest (?:\d+ )?durations\s+=+$')
_DURATION = re.compile(r'^(\d+(?:\.\d+)?)s\s+(?:setup|call|teardown)\s+(.+)$')


def near_limit(command, log_text, *, step_tag='pytest', emit=print):
    """Return offending (test ID, seconds) pairs and emit their fixed-limit notes.

    Both timeout argument forms and shell quoting are supported. If a step has
    multiple pytest commands, use its smallest positive timeout conservatively.
    Unparsable timeout text is reported explicitly as unchecked evidence.
    A zero timeout disables pytest's timer and supplies no duration limit.
    There is deliberately no caller override for the fraction.
    """
    command = command.replace('\\\n', '')
    if '--timeout' not in command:
        return []
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = iter(token.strip() for token in list(lexer))
    except ValueError:
        emit(f'DURATIONS UNCHECKED {step_tag}: cannot split pytest --timeout command')
        return []
    timeouts = []
    parsed = invalid = False
    for token in tokens:
        if token == '--timeout':
            value = next(tokens, '')
        elif token.startswith('--timeout='):
            value = token.partition('=')[2]
        else:
            continue
        try:
            timeout = float(value)
            if not math.isfinite(timeout) or timeout < 0:
                raise ValueError
        except ValueError:
            invalid = True
            continue
        parsed = True
        if timeout:
            timeouts.append(timeout)
    if invalid or not parsed:
        emit(f'DURATIONS UNCHECKED {step_tag}: use a finite nonnegative pytest --timeout value')
    if not timeouts:
        return []
    timeout = min(timeouts)
    totals = {}
    found = active = False
    for raw in log_text.splitlines():
        line = _ANSI.sub('', raw).strip()
        if _HEADER.fullmatch(line):
            found = active = True
        elif line.startswith('==='):
            active = False
        elif active and (match := _DURATION.fullmatch(line)):
            seconds, test_id = match.groups()
            totals[test_id] = totals.get(test_id, 0.0) + float(seconds)
    if not found:
        emit(f'DURATIONS UNCHECKED {step_tag}: no durations section for pytest timeout {timeout:g}s')
    offenders = []
    for test_id, seconds in totals.items():
        ceiling_note = ''
        if test_id in ACCEPTED_EXCEPTIONS:
            ceiling, approval = ACCEPTED_EXCEPTIONS[test_id]
            if seconds <= ceiling:
                emit(f'ACCEPTED EXCEPTION {step_tag}: {test_id} took {seconds:.2f}s '
                     f'of {timeout:g}s (ceiling {ceiling}s, approval {approval})')
                continue
            ceiling_note = f', ceiling {ceiling}s'
        elif seconds <= NEAR_LIMIT_FRACTION * timeout:
            continue
        offenders.append((test_id, seconds))
        emit(f'NEAR TIMEOUT {step_tag}: {test_id} took {seconds:.2f}s of {timeout:g}s '
             f'(limit {NEAR_LIMIT_FRACTION:.2f}{ceiling_note})')
    return offenders
