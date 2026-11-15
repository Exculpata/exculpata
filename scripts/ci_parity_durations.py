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
_ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
_HEADER = re.compile(r'^=+\s+slowest (?:\d+ )?durations\s+=+$')
_DURATION = re.compile(r'^(\d+(?:\.\d+)?)s\s+(?:setup|call|teardown)\s+(.+)$')


def near_limit(command, log_text, *, step_tag='pytest', emit=print):
    """Return offending (test ID, seconds) pairs and emit their fixed-limit notes.

    Both timeout argument forms and shell quoting are supported. If a step has
    multiple pytest commands, use its smallest positive timeout conservatively.
    A zero timeout disables pytest's timer and supplies no duration limit.
    There is deliberately no caller override for the fraction.
    """
    if '--timeout' not in command:
        return []
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    tokens = iter(lexer)
    timeouts = []
    for token in tokens:
        if token == '--timeout':
            value = next(tokens, '')
        elif token.startswith('--timeout='):
            value = token.partition('=')[2]
        else:
            continue
        timeout = float(value)
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError('Use a finite nonnegative pytest --timeout value.')
        if timeout:
            timeouts.append(timeout)
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
    offenders = [(test_id, seconds) for test_id, seconds in totals.items()
                 if seconds > NEAR_LIMIT_FRACTION * timeout]
    for test_id, seconds in offenders:
        emit(f'NEAR TIMEOUT {step_tag}: {test_id} took {seconds:.2f}s of {timeout:g}s '
             f'(limit {NEAR_LIMIT_FRACTION:.2f})')
    return offenders
