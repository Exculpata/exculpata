"""Inject a blocked socket probe for the demo's pytest integration test."""

from tests import conftest as hooks


BLOCKED_PROBE = "Synthetic demo socket probe failure"


def pytest_collection_finish(session):
    session.config.stash[hooks._probe_result] = BLOCKED_PROBE
