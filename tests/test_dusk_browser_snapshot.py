"""Synthetic theme transitions must settle to the entire original snapshot."""
import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest
from selenium.webdriver.support.ui import WebDriverWait


@pytest.fixture
def dusk_script():
    path = Path(__file__).resolve().parents[1] / 'scripts/browser-accept-dusk.py'
    spec = importlib.util.spec_from_file_location('browser_dusk_snapshot', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LIGHT = [['rgb(0, 0, 0)', 'rgb(255, 255, 255)', '1', 'rgb(0, 0, 0)']]


def test_light_snapshot_waits_for_styles_after_theme_attribute_changes(dusk_script):
    snapshot = Mock(side_effect=[
        [['rgb(255, 255, 255)', 'rgb(0, 0, 0)', '1', 'rgb(255, 255, 255)']],
        LIGHT,
    ])
    dusk_script.wait_for_light_snapshot(WebDriverWait(None, 1, poll_frequency=.001), snapshot, LIGHT)
    assert snapshot.call_count == 2


@pytest.mark.parametrize('index,property', list(enumerate(('color', 'backgroundColor', 'opacity', 'borderColor'))))
def test_persistent_difference_fails_with_the_first_property(dusk_script, index, property):
    actual = [LIGHT[0][:], LIGHT[0][:]]
    for position in range(index, 4):
        actual[1][position] = 'synthetic differing value'
    snapshot = Mock(return_value=actual)
    with pytest.raises(AssertionError, match=f'element 1 {property}: expected'):
        dusk_script.wait_for_light_snapshot(
            WebDriverWait(None, .01, poll_frequency=.001), snapshot, [LIGHT[0], LIGHT[0]])
    assert snapshot.call_count > 1


def test_changed_element_count_cannot_pass(dusk_script):
    with pytest.raises(AssertionError, match='element count: expected 1, got 0'):
        dusk_script.wait_for_light_snapshot(
            WebDriverWait(None, .01, poll_frequency=.001), lambda: [], LIGHT)


def test_matching_light_snapshot_passes_immediately(dusk_script):
    snapshot = Mock(return_value=LIGHT)
    dusk_script.wait_for_light_snapshot(WebDriverWait(None, 1), snapshot, LIGHT)
    snapshot.assert_called_once_with()
