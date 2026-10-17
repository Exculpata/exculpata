from pathlib import Path
from case_intelligence.review_wording import NOTE_TEMPLATES, REVIEW_NOTICE, prohibited_phrases
from case_intelligence.statement_comparison_exports import EXPORT_HEADERS


def test_authored_wording_is_neutral():
    template = (Path(__file__).resolve().parents[1] / 'src/case_intelligence/templates/workbench_inconsistencies.html').read_text()
    assert not prohibited_phrases('\n'.join([template, REVIEW_NOTICE, *NOTE_TEMPLATES.values(), *EXPORT_HEADERS]))


def test_wording_guard_rejects_verdicts_at_word_boundaries():
    assert prohibited_phrases('The witness lied; credibility destroyed.')
    assert not prohibited_phrases('Review the supplied passage.')
