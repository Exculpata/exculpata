"""Synthetic replacement waits exercise the shared navigation boundary."""

import importlib.util
from pathlib import Path

import pytest
from selenium.common.exceptions import WebDriverException

spec = importlib.util.spec_from_file_location(
    'browser_navigation', Path(__file__).resolve().parents[1] / 'scripts/browser_navigation.py')
navigation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(navigation)


class RetiringElement:
    def __init__(self, events, error):
        self.events, self.error = events, error

    def is_enabled(self):
        self.events.append('retiring element')
        raise self.error


class ReplacementDriver:
    def __init__(self, events):
        self.events = events
        self.states = iter(['loading', 'complete'])

    def execute_script(self, script):
        assert script == 'return document.readyState'
        state = next(self.states)
        self.events.append(state)
        return state


class SyntheticWait:
    def __init__(self, driver):
        self.driver = driver

    def until(self, condition):
        for _ in range(3):
            if condition(self.driver):
                return
        raise AssertionError('Synthetic wait did not settle')


def test_replacement_accepts_detached_node_then_waits_for_complete_document():
    events = []
    driver = ReplacementDriver(events)
    element = RetiringElement(
        events, WebDriverException('Node with given id does not belong to the document'))

    navigation.wait_for_replacement(driver, SyntheticWait(driver), element)

    assert events == ['retiring element', 'loading', 'complete']


def test_replacement_preserves_unexpected_driver_error():
    events = []
    driver = ReplacementDriver(events)
    error = WebDriverException('Unexpected inspector failure')
    element = RetiringElement(events, error)

    with pytest.raises(WebDriverException) as failure:
        navigation.wait_for_replacement(driver, SyntheticWait(driver), element)

    assert failure.value is error
    assert events == ['retiring element']
