"""Shared neutral language for machine-suggested review candidates."""
import re

PROHIBITED_PHRASES = (
    'lied', 'false', 'fabricated', 'perjury', 'impeach',
    'inconsistent testimony proves', 'credibility destroyed',
)
REVIEW_NOTICE = ('Possible inconsistencies for review. These are differences in wording or detail '
                 'between records, not findings about any witness.')
NOTE_TEMPLATES = {
    'time_date': 'Time/date differs: {left} in one account; {right} in the other.',
    'location': 'Location differs: {left} in one account; {right} in the other.',
    'people_present': 'Presence wording differs: {left} in one account; {right} in the other.',
    'quantity_count': 'Count differs: {left} in one account; {right} in the other.',
    'observed_action': 'Action wording differs: {left} in one account; {right} in the other.',
    'object_description': 'Object description differs: {left} in one account; {right} in the other. Check whether both refer to the same object.',
}


def prohibited_phrases(text):
    """Scan authored wording, never rewrite or censor quoted source passages."""
    return [phrase for phrase in PROHIBITED_PHRASES
            if re.search(r'\b' + re.escape(phrase) + r'\b', text, re.I)]
