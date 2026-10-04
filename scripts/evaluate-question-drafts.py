#!/usr/bin/env python3
"""Evaluate question drafting on the fixed synthetic set with the configured generator.

Uses only CASE_INTELLIGENCE_GENERATOR_* settings, never selects or downloads a
model, and reads no matter data. Without a configured runtime the receipt
records the model gate as outstanding.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from case_intelligence.generation import GroundedGenerationService, generator_from_environment
from case_intelligence.question_draft_evaluation import DEFAULT_CASES, receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, help="Write the receipt here (a new file).")
    parser.add_argument("--require-model", action="store_true",
                        help="Exit non-zero unless the model gate passed.")
    args = parser.parse_args(argv)
    result = receipt(GroundedGenerationService(generator_from_environment()), args.cases)
    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        if args.output.exists():
            raise SystemExit("Choose a new output file; the existing receipt was preserved.")
        args.output.write_text(text)
    print(text, end="")
    summary = result.get("limitation") or (
        f"{result['relevant_questions']}/{result['shown_questions']} shown questions cite only relevant passages; "
        f"{result['shown_unsupported_quotes']} quote text outside their passages; gate {result['model_gate']}.")
    print(summary, file=sys.stderr)
    return 0 if result["model_gate"] == "passed" or not args.require_model else 1


if __name__ == "__main__":
    raise SystemExit(main())
