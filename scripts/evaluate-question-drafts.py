#!/usr/bin/env python3
"""Evaluate question drafting on the fixed synthetic set with the configured generator.

Uses only CASE_INTELLIGENCE_GENERATOR_* settings, never selects or downloads a
model, and reads no matter data. Without a configured runtime the receipt
records the model gate as outstanding. The gate passes only for a clean checkout
at a recorded commit with one immutable model artifact observed before and after
the run, declared by a runtime profile that matches the selected pinned generator
in config/models.json; otherwise a passing score is recorded as unbound.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from case_intelligence.generation import GroundedGenerationService, generator_from_environment
from case_intelligence.question_draft_evaluation import DEFAULT_CASES, json_safe, receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, help="Write the receipt here (a new file).")
    parser.add_argument("--require-model", action="store_true",
                        help="Exit non-zero unless the model gate passed.")
    parser.add_argument("--profile", choices=("portable", "quality"),
                        help="The pinned generator profile in config/models.json that was run.")
    parser.add_argument("--runtime-profile", type=Path,
                        help="Runtime-profile JSON declaring the model artifact digest and its upstream pin "
                             "(the format docs/CLAIM_EVALUATION.md describes).")
    args = parser.parse_args(argv)
    if (args.profile is None) != (args.runtime_profile is None):
        parser.error("Give --profile and --runtime-profile together.")
    if args.output and args.output.exists():
        parser.error("Choose a new output file; the existing receipt was preserved.")
    try:
        runtime = json.loads(args.runtime_profile.read_text(encoding="utf-8")) if args.runtime_profile else None
        result = receipt(GroundedGenerationService(generator_from_environment()), args.cases,
                         profile=args.profile, runtime=runtime)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    # ASCII escapes keep a receipt writable even when raw model output holds text
    # that is not valid UTF-8 (such as a lone surrogate), and non-finite numbers are
    # kept as text, so the receipt is always strict JSON.
    text = json.dumps(json_safe(result), indent=2, ensure_ascii=True, allow_nan=False) + "\n"
    if args.output:
        if args.output.exists():
            raise SystemExit("Choose a new output file; the existing receipt was preserved.")
        args.output.write_text(text)
    print(text, end="")
    summary = result.get("limitation") or (
        f"{result['relevant_questions']}/{result['shown_questions']} shown questions cite only relevant passages; "
        f"{result['shown_unsupported_quotes']} quote text outside their passages; gate {result['model_gate']}."
        + "".join(f" {problem}" for problem in result.get("binding_problems", ())))
    print(summary, file=sys.stderr)
    return 0 if result["model_gate"] == "passed" or not args.require_model else 1


if __name__ == "__main__":
    raise SystemExit(main())
