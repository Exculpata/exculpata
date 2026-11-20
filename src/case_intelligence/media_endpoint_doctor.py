"""Installed contract checks using fixed synthetic input and bounded status codes."""
import argparse
import json

CHECKS = {
    "alignment": ("ALIGNMENT", "word alignment", "recording and timed source text"),
    "translation": ("TRANSLATION", "translation", "source text"),
    "speaker_labeling": ("DIARIZATION", "speaker labeling", "recording and segment times"),
}
STATUS_MESSAGES = {
    "ok": "ok",
    "configuration": "invalid configuration. Check the HTTPS origin, credential and relay.",
    "unconfigured": "no endpoint configured in the running app.",
    "authentication": "authentication failed. Check the credential.",
    "unreachable": "service unreachable. Check the endpoint and relay.",
    "refused": "request refused. Check the model and API compatibility.",
    "incompatible": "invalid response. Check the API contract.",
    "fixture": "diagnostic recording unavailable. Check the installed app.",
    "limit": "synthetic request exceeded the service limit.",
}


def doctor(capability):
    from .media_enrichment import MediaContractError, probe_contract

    try:
        probe_contract(capability)
    except MediaContractError as exc:
        return exc.code
    return "ok"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capability", choices=CHECKS)
    capability = parser.parse_args().capability
    print(json.dumps({capability: doctor(capability)}))
