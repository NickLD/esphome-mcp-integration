"""Tests for the patch-apply logic used by esphome_patch_yaml.

Lives under tests/ha because the helper is defined in the integration's llm
module, which imports Home Assistant. Auto-skips without HA installed.
"""
from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")

from custom_components.esphome_mcp_bridge.llm import (  # noqa: E402
    _apply_patches,
    _PatchAmbiguous,
    _PatchNotFound,
)


def test_single_patch_applies():
    content = "esphome:\n  name: kitchen\n"
    result = _apply_patches(
        content, [{"old_string": "kitchen", "new_string": "garage"}]
    )
    assert result == "esphome:\n  name: garage\n"


def test_multiple_patches_apply_in_order_second_targets_first_output():
    content = "value: old\n"
    patches = [
        {"old_string": "old", "new_string": "TEMP"},
        {"old_string": "TEMP", "new_string": "new"},
    ]
    assert _apply_patches(content, patches) == "value: new\n"


def test_old_string_not_found_raises():
    with pytest.raises(_PatchNotFound) as exc_info:
        _apply_patches("value: old\n", [{"old_string": "missing", "new_string": "x"}])
    assert exc_info.value.index == 0
    assert exc_info.value.old_string == "missing"


def test_old_string_ambiguous_raises():
    content = "dup\ndup\n"
    with pytest.raises(_PatchAmbiguous) as exc_info:
        _apply_patches(content, [{"old_string": "dup", "new_string": "x"}])
    assert exc_info.value.count == 2


def test_empty_old_string_raises_value_error():
    with pytest.raises(ValueError):
        _apply_patches("value: old\n", [{"old_string": "", "new_string": "x"}])


def test_failing_patch_never_produces_a_partial_result():
    """Atomicity falls out of 'compute in memory, write once': a failing
    patch raises out of _apply_patches before returning anything, so the
    caller (PatchYamlTool.async_call) never has a partially-patched string
    it could mistakenly write to disk."""
    content = "a: 1\nb: 2\n"
    patches = [
        {"old_string": "a: 1", "new_string": "a: 9"},
        {"old_string": "missing", "new_string": "x"},
    ]
    with pytest.raises(_PatchNotFound):
        _apply_patches(content, patches)
