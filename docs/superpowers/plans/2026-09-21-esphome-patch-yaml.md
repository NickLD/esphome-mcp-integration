# esphome_patch_yaml Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new `esphome_patch_yaml` LLM tool that edits an existing ESPHome
YAML file via one or more exact-match `{old_string, new_string}` replacements,
so a small edit costs O(edit size) instead of O(file size) like
`esphome_write_yaml` requires today.

**Architecture:** A pure, HA-free helper `_apply_patches(content, patches)`
applies each patch in order against an in-memory string, raising a narrow
exception the moment a patch doesn't match exactly once. `PatchYamlTool`
(an `llm.Tool`) wraps it: read file -> apply patches off the event loop ->
write once, only if every patch succeeded. This mirrors the existing
`WriteYamlTool` / `_insert_secret` patterns already in
`custom_components/esphome_mcp_bridge/llm.py`.

**Tech Stack:** Python 3.11+, Home Assistant `llm.Tool` / `voluptuous`,
pytest (HA-gated via `pytest.importorskip`).

**Spec:** `docs/specs/esphome-patch-yaml-spec.md`

## Global Constraints

- No changes to `esphome_mcp_client` (the PyPI transport library) — this is
  pure local-filesystem logic.
- No changes to the integration smoke-test workflow.
- Exact-match `{old_string, new_string}` pairs only — no unified-diff /
  `git apply` format.
- Same security posture as `WriteYamlTool`: `_guard(filename, require_yaml=True,
  allow_extra=_allow_extra_files(hass))`. `secrets.yaml` stays blocked; paths
  can never escape `ESPHOME_CONFIG_DIR`.
- All-or-nothing write: nothing is written to disk unless every patch in the
  call succeeds.
- If `pytest`/`ruff` are not installed in the environment running these
  steps, run `pip install -e ".[dev]"` first (see `CLAUDE.md` Commands). The
  `tests/ha/*` tests additionally require `homeassistant` to be installed to
  run for real; without it they auto-skip via `pytest.importorskip`, which is
  expected in a plain dev environment and not a failure.

---

## File Structure

- Modify: `custom_components/esphome_mcp_bridge/llm.py`
  - Add `_PatchNotFound`, `_PatchAmbiguous` exceptions and `_apply_patches()`
    near `_insert_secret` (Shared helpers section).
  - Add `PatchYamlTool` class near `WriteYamlTool` (Configuration file tools
    section).
  - Register `PatchYamlTool()` in `ESPHomeBuilderAPI.async_get_api_instance`.
  - Extend `_API_PROMPT` with a steering sentence.
- Create: `tests/ha/test_patch_yaml.py` — unit tests for `_apply_patches`,
  same shape as `tests/ha/test_secrets.py`.
- Modify: `README.md` — add `esphome_patch_yaml` row to the tool table.
- Modify: `custom_components/esphome_mcp_bridge/manifest.json` — bump
  `version` `0.5.2` -> `0.6.0`.

---

### Task 1: `_apply_patches` core logic

**Files:**
- Modify: `custom_components/esphome_mcp_bridge/llm.py` (add after
  `_insert_secret`, i.e. after line 152, before the blank line preceding
  `async def _async_connection`)
- Test: `tests/ha/test_patch_yaml.py` (new file)

**Interfaces:**
- Produces:
  - `class _PatchNotFound(Exception)` with attributes `.index: int`,
    `.old_string: str`
  - `class _PatchAmbiguous(Exception)` with attributes `.index: int`,
    `.old_string: str`, `.count: int`
  - `def _apply_patches(content: str, patches: list[dict[str, str]]) -> str`
    — each `patches[i]` is a plain dict with string keys `"old_string"` /
    `"new_string"` (matches what `voluptuous` hands back from the tool's
    schema, and what a test can pass directly without constructing anything
    HA-specific).

- [ ] **Step 1: Write the failing tests**

Create `tests/ha/test_patch_yaml.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/ha/test_patch_yaml.py -v`
Expected: if `homeassistant` is installed, every test `FAIL`s with
`ImportError` (`_apply_patches` doesn't exist yet). If `homeassistant` is
*not* installed, every test is reported `SKIPPED` — in that case, just
confirm the skip reason is the `pytest.importorskip("homeassistant")` line
(not a different, unrelated import error) before moving on; you'll validate
the real behavior in Step 4 the same way.

- [ ] **Step 3: Implement `_apply_patches` and its exceptions**

In `custom_components/esphome_mcp_bridge/llm.py`, insert directly after the
`_insert_secret` function (after line 152) and before the blank lines
leading into `async def _async_connection`:

```python
class _PatchNotFound(Exception):
    """A patch's old_string did not match anywhere in the file."""

    def __init__(self, index: int, old_string: str) -> None:
        self.index = index
        self.old_string = old_string
        super().__init__(f"patch {index}: old_string not found")


class _PatchAmbiguous(Exception):
    """A patch's old_string matched more than once."""

    def __init__(self, index: int, old_string: str, count: int) -> None:
        self.index = index
        self.old_string = old_string
        self.count = count
        super().__init__(f"patch {index}: old_string matched {count} times")


def _apply_patches(content: str, patches: list[dict[str, str]]) -> str:
    """Apply each {old_string, new_string} patch in order, in memory.

    Raises ValueError for an empty old_string, _PatchNotFound if a patch's
    old_string has zero matches, _PatchAmbiguous if it has more than one.
    Patches apply sequentially against the running result, so a later patch
    may target text introduced by an earlier one. Returns the fully-patched
    string; never writes anything (that's the caller's job, once, after every
    patch has succeeded) - this is what makes the write atomic.
    """
    result = content
    for index, patch in enumerate(patches):
        old = patch["old_string"]
        new = patch["new_string"]
        if old == "":
            raise ValueError(f"patch {index}: old_string must not be empty")
        count = result.count(old)
        if count == 0:
            raise _PatchNotFound(index, old)
        if count > 1:
            raise _PatchAmbiguous(index, old, count)
        result = result.replace(old, new, 1)
    return result
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/ha/test_patch_yaml.py -v`
Expected: all `PASS` (or all `SKIPPED` if `homeassistant` isn't installed in
this environment — that's the pre-existing, expected state for `tests/ha/*`;
if so, additionally run
`python -c "from custom_components.esphome_mcp_bridge import llm"` — this
still imports `homeassistant.helpers.llm` and will fail loudly with a syntax
or `NameError` if the new code is broken, giving you a signal even without a
full HA install).

- [ ] **Step 5: Commit**

```bash
git add custom_components/esphome_mcp_bridge/llm.py tests/ha/test_patch_yaml.py
git commit -m "Add _apply_patches core logic for esphome_patch_yaml

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 2: `PatchYamlTool` and API wiring

**Files:**
- Modify: `custom_components/esphome_mcp_bridge/llm.py`
  - Add `PatchYamlTool` class after `WriteYamlTool` (after line 520, before
    `class AddSecretTool`)
  - Modify `ESPHomeBuilderAPI.async_get_api_instance`'s `tools` list
    (around line 887-901)
  - Modify `_API_PROMPT` (around line 854-867)

**Interfaces:**
- Consumes (from Task 1): `_apply_patches(content, patches) -> str`,
  `_PatchNotFound` (`.index`, `.old_string`), `_PatchAmbiguous` (`.index`,
  `.old_string`, `.count`)
- Consumes (pre-existing in `llm.py`): `_guard(filename, *, require_yaml,
  allow_extra) -> str`, `_allow_extra_files(hass) -> bool`, `_read_file(path)
  -> str`, `_write_file(path, content) -> None`, `ESPHOME_CONFIG_DIR`
- Produces: `class PatchYamlTool(llm.Tool)` with `name =
  "esphome_patch_yaml"`, registered as one of the tools returned by
  `ESPHomeBuilderAPI.async_get_api_instance`.

- [ ] **Step 1: Add `PatchYamlTool`**

In `custom_components/esphome_mcp_bridge/llm.py`, insert after the
`WriteYamlTool` class (after line 520) and before `class AddSecretTool`:

```python
class PatchYamlTool(llm.Tool):
    """Edit an existing ESPHome YAML configuration file via exact-match
    string replacements, without resending the whole file."""

    name = "esphome_patch_yaml"
    description = (
        "Edit an existing file in /config/esphome by replacing one or more "
        "exact text snippets, instead of resending the whole file's content "
        "like esphome_write_yaml requires. Each patch's 'old_string' must "
        "match exactly once in the file (after any earlier patches in this "
        "same call have been applied) - zero matches or more than one is an "
        "error, so there's never ambiguity about what changed. Patches apply "
        "in the order given, and nothing is written to disk unless every "
        "patch succeeds (all-or-nothing). The file must already exist; use "
        "esphome_create_config for a new file. With extra file access "
        "enabled, also works on non-YAML files and subdirectory paths."
    )
    parameters = vol.Schema(
        {
            vol.Required("filename"): str,
            vol.Required("patches"): [
                vol.Schema(
                    {
                        vol.Required("old_string"): str,
                        vol.Required("new_string"): str,
                    }
                )
            ],
        }
    )

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict[str, Any]:
        try:
            filename = _guard(
                tool_input.tool_args["filename"],
                require_yaml=True,
                allow_extra=_allow_extra_files(hass),
            )
        except ValueError as err:
            return {"error": str(err)}
        path = os.path.join(ESPHOME_CONFIG_DIR, filename)
        try:
            content = await hass.async_add_executor_job(_read_file, path)
        except FileNotFoundError:
            return {
                "error": (
                    f"File '{filename}' not found in {ESPHOME_CONFIG_DIR}; "
                    "use esphome_create_config to create it."
                )
            }
        except OSError as err:
            return {"error": str(err)}

        patches = tool_input.tool_args["patches"]
        try:
            patched = await hass.async_add_executor_job(
                _apply_patches, content, patches
            )
        except _PatchNotFound as err:
            return {
                "error": (
                    f"Patch {err.index}: old_string not found "
                    f"({err.old_string[:80]!r}). No changes were written."
                )
            }
        except _PatchAmbiguous as err:
            return {
                "error": (
                    f"Patch {err.index}: old_string matched {err.count} times "
                    f"({err.old_string[:80]!r}); it must match exactly once. "
                    "No changes were written."
                )
            }
        except ValueError as err:
            return {"error": f"{err}. No changes were written."}

        try:
            await hass.async_add_executor_job(_write_file, path, patched)
        except OSError as err:
            return {"error": str(err)}
        return {
            "success": True,
            "filename": filename,
            "patches_applied": len(patches),
        }
```

- [ ] **Step 2: Register the tool**

In `ESPHomeBuilderAPI.async_get_api_instance`, add `PatchYamlTool()` to the
`tools` list right after `WriteYamlTool()`:

```python
        tools: list[llm.Tool] = [
            ListAddonsTool(),
            ListDevicesTool(),
            ReadYamlTool(),
            CreateConfigTool(),
            WriteYamlTool(),
            PatchYamlTool(),
            AddSecretTool(),
            ValidateTool(),
            CompileTool(),
            CleanTool(),
            UploadTool(),
            RunTool(),
            LogsTool(),
            JobStatusTool(),
        ]
```

- [ ] **Step 3: Steer agents toward it in `_API_PROMPT`**

Change the `_API_PROMPT` assignment so the sentence "A typical flow is..."
paragraph gains a new sentence about preferring the patch tool. Replace:

```python
_API_PROMPT = (
    "You can manage ESPHome devices running inside Home Assistant across a full "
    "development cycle: discover the installed ESPHome add-on channels, take "
    "inventory of devices, read/create/write YAML configurations in "
    "/config/esphome, then validate, compile, upload (flash), run, and stream "
    "logs via the ESPHome dashboard. A typical flow is: list devices -> read or "
    "create a config -> write changes -> validate -> compile -> run (flash) -> "
    "check logs. When scaffolding a new config, use esphome_add_secret to insert "
    "any referenced secrets (it never overwrites existing ones). Never read, "
    "write, or build secrets.yaml directly. Compilation and "
    "uploads run to completion before returning; log streaming returns a bounded "
    "window. Prefer validating before compiling, and report exit codes and "
    "relevant log lines back to the user."
)
```

with:

```python
_API_PROMPT = (
    "You can manage ESPHome devices running inside Home Assistant across a full "
    "development cycle: discover the installed ESPHome add-on channels, take "
    "inventory of devices, read/create/write YAML configurations in "
    "/config/esphome, then validate, compile, upload (flash), run, and stream "
    "logs via the ESPHome dashboard. A typical flow is: list devices -> read or "
    "create a config -> write changes -> validate -> compile -> run (flash) -> "
    "check logs. When scaffolding a new config, use esphome_add_secret to insert "
    "any referenced secrets (it never overwrites existing ones). Never read, "
    "write, or build secrets.yaml directly. Compilation and "
    "uploads run to completion before returning; log streaming returns a bounded "
    "window. Prefer validating before compiling, and report exit codes and "
    "relevant log lines back to the user. Prefer esphome_patch_yaml over "
    "esphome_write_yaml when making small, targeted edits to a file that "
    "already exists - it avoids resending the entire file's contents. Use "
    "esphome_write_yaml only for a full rewrite, and esphome_create_config "
    "for a brand-new file."
)
```

- [ ] **Step 4: Verify nothing is broken**

Run: `pytest -v` (from the repo root)
Expected: all previously-passing tests still `PASS` (or `SKIP` for the
`tests/ha/*` files if `homeassistant` isn't installed), and there are no
new failures or collection errors.

Also sanity-check the file still parses and imports cleanly:
Run: `python -m py_compile custom_components/esphome_mcp_bridge/llm.py`
Expected: exits with no output (success).

- [ ] **Step 5: Commit**

```bash
git add custom_components/esphome_mcp_bridge/llm.py
git commit -m "Add esphome_patch_yaml tool and register it in the LLM API

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

### Task 3: Docs, version bump, and full verification

**Files:**
- Modify: `README.md`
- Modify: `custom_components/esphome_mcp_bridge/manifest.json`

**Interfaces:**
- Consumes: none (documentation/metadata only; no code interfaces).

- [ ] **Step 1: Add the tool to the README table**

In `README.md`, in the tool table (see lines 37-49), add a row for
`esphome_patch_yaml` right after the `esphome_write_yaml` row (line 43):

```markdown
| `esphome_write_yaml` | Overwrite an existing config |
| `esphome_patch_yaml` | Edit an existing config via exact-match `{old_string, new_string}` replacements, without resending the whole file |
| `esphome_add_secret` | Insert a key into `secrets.yaml` — **insert-only, write-only** (never reads/returns values; errors if the key exists) |
```

(i.e. insert the new `esphome_patch_yaml` line between the existing
`esphome_write_yaml` and `esphome_add_secret` lines — don't duplicate
those two.)

- [ ] **Step 2: Bump the integration version**

In `custom_components/esphome_mcp_bridge/manifest.json`, change:

```json
  "version": "0.5.2"
```

to:

```json
  "version": "0.6.0"
```

- [ ] **Step 3: Run the full verification suite**

Run, from the repo root:

```bash
pip install -e ".[dev]"   # only if ruff/pytest aren't already available
ruff check custom_components esphome_mcp_client tests
pytest
```

Expected: `ruff check` reports no issues; `pytest` shows all tests `PASS`
(the `tests/ha/*` tests `PASS` if `homeassistant` is installed, or `SKIP`
with the `pytest.importorskip("homeassistant")` reason if not — either is
acceptable, a new failure is not).

- [ ] **Step 4: Commit**

```bash
git add README.md custom_components/esphome_mcp_bridge/manifest.json
git commit -m "Document esphome_patch_yaml and bump integration to 0.6.0

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
```

---

## Acceptance Criteria (from the spec)

- [ ] `_apply_patches` implemented with the exact semantics above, unit-tested per Task 1
- [ ] `PatchYamlTool` implemented, registered in `ESPHomeBuilderAPI`
- [ ] `_API_PROMPT` updated to steer agents toward it for small edits
- [ ] README tool table updated
- [ ] `manifest.json` version bumped
- [ ] `ruff check` and `pytest` (unit tests; integration tests untouched/unaffected) pass
- [ ] PR description references and closes issue #5 (handled at PR-creation time, after this plan's tasks are complete — see `docs/specs/esphome-patch-yaml-spec.md`'s suggested branch/PR section: branch `feature/esphome-patch-yaml`, title `Add esphome_patch_yaml tool for targeted edits`, body referencing `Closes #5`)
