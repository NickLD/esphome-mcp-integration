# Spec: `esphome_patch_yaml` tool

**Resolves:** [jrigling/esphome-mcp-integration#5](https://github.com/jrigling/esphome-mcp-integration/issues/5)

## Problem

`esphome_write_yaml` only supports a full-file overwrite. Every edit — even a
one-line tweak — requires the calling agent to regenerate the entire file's
contents as a literal tool-call parameter. For any config of non-trivial size
this becomes the dominant latency cost in an edit-validate-compile-upload
loop, and it scales the wrong way: larger, more complex configs (exactly the
ones a user most wants AI-agent help iterating on) pay the biggest tax per
edit.

## Goal

Add a new tool, `esphome_patch_yaml`, that edits an existing file in place via
one or more exact-match string replacements, so small edits cost O(edit size)
instead of O(file size).

## Design

### 1. Pure patch-application function

Add to `custom_components/esphome_mcp_bridge/llm.py`, near the other shared
helpers (`_insert_secret`, `_sanitize_filename`):

```python
class _PatchNotFound(Exception):
    """old_string did not match anywhere in the file."""

class _PatchAmbiguous(Exception):
    """old_string matched more than once; include the match count."""

def _apply_patches(content: str, patches: list[dict[str, str]]) -> str:
    """Apply each {old_string, new_string} patch in order, in memory.

    Raises ValueError for an empty old_string, _PatchNotFound if a patch's
    old_string has zero matches, _PatchAmbiguous if it has more than one.
    Patches apply sequentially against the running result, so a later patch
    may target text introduced by an earlier one. Returns the fully-patched
    string; never writes anything (that's the caller's job, once, after every
    patch has succeeded) — this is what makes the write atomic.
    """
```

Model the exception style directly on `_SecretKeyExists` (see
`_insert_secret`): specific, narrow exception types that a unit test can
assert on directly, not a generic `ValueError` with a string to grep.

### 2. The tool

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
        "same call have been applied) — zero matches or more than one is an "
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
        # 1. _guard(filename, require_yaml=True, allow_extra=_allow_extra_files(hass))
        #    — identical security posture to WriteYamlTool.
        # 2. Read the file (executor job); FileNotFoundError -> clear error
        #    ("... not found; use esphome_write_yaml to create it" — actually
        #    esphome_create_config, per the tools' own naming) same style as
        #    ReadYamlTool's not-found error.
        # 3. Run _apply_patches(content, patches) off the event loop
        #    (hass.async_add_executor_job) — pure CPU work, but keep the
        #    pattern consistent with the rest of the file's I/O calls.
        # 4. On _PatchNotFound / _PatchAmbiguous / ValueError: return
        #    {"error": ...} identifying which patch (index, and/or a snippet
        #    of old_string) failed and why. Nothing is written.
        # 5. On success: _write_file(path, patched_content) (reuse the
        #    existing helper), return
        #    {"success": True, "filename": filename, "patches_applied": len(patches)}.
```

Register `PatchYamlTool()` in `ESPHomeBuilderAPI.async_get_api_instance`'s
`tools` list, next to `WriteYamlTool()`.

### 3. Steer agents toward using it

Append to `_API_PROMPT` (in `llm.py`):

> Prefer esphome_patch_yaml over esphome_write_yaml when making small,
> targeted edits to a file that already exists — it avoids resending the
> entire file's contents. Use esphome_write_yaml only for a full rewrite, and
> esphome_create_config for a brand-new file.

Without this, the tool exists but agents will keep defaulting to the
familiar `esphome_write_yaml`, and the whole point is lost.

### 4. Tests

New file: `tests/ha/test_patch_yaml.py`, same shape as `tests/ha/test_secrets.py`
(`pytest.importorskip("homeassistant")`, then import the pure function
directly — no HA test harness or mocking needed):

- single patch applies correctly
- multiple patches apply in order, second patch targets text the first one introduced
- `old_string` not present anywhere → raises `_PatchNotFound`
- `old_string` present more than once → raises `_PatchAmbiguous`
- empty `old_string` → raises `ValueError`
- on a failing patch mid-list, the function's return value is never used to
  write anything (assert this at the `async_call` level or simply note in a
  test docstring that atomicity falls out of "compute in memory, write once"
  — whichever is more natural once the code exists)

### 5. Docs / bookkeeping

- Add a row to the README's tool table for `esphome_patch_yaml`.
- Bump `custom_components/esphome_mcp_bridge/manifest.json` version
  `0.5.2` → `0.6.0` (new capability, backward compatible).

## Out of scope

- No changes to `esphome_mcp_client` (the PyPI transport library) — this is
  pure local-filesystem logic, same as `write_yaml`/`create_config`/`add_secret`.
- No changes to the integration smoke-test workflow — that exercises the
  ESPHome dashboard's build/validate protocol, which this feature doesn't
  touch.
- No unified-diff / `git apply` format. Exact-match `{old_string, new_string}`
  pairs only (see the closed-out discussion on the issue for why).

## Acceptance criteria

- [ ] `_apply_patches` implemented with the exact semantics above, unit-tested per §4
- [ ] `PatchYamlTool` implemented, registered in `ESPHomeBuilderAPI`
- [ ] `_API_PROMPT` updated to steer agents toward it for small edits
- [ ] README tool table updated
- [ ] `manifest.json` version bumped
- [ ] `ruff check` and `pytest` (unit tests; integration tests untouched/unaffected) pass
- [ ] PR description references and closes issue #5

## Suggested branch / PR

- Branch: `feature/esphome-patch-yaml`
- PR title: `Add esphome_patch_yaml tool for targeted edits`
- PR body: reference `Closes #5`, summarize the design above
