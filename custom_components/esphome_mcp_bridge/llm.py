"""ESPHome MCP Bridge: LLM tools and API.

Exposes a full ESPHome development cycle to AI agents connecting through Home
Assistant's MCP server:

  discover add-ons -> inventory devices -> read / create / write YAML ->
  validate -> compile -> upload (flash) / run -> stream logs -> clean

File operations (read/create/write) act directly on ``/config/esphome`` for
reliability. Build operations talk to the ESPHome dashboard / Device Builder
add-on (stable, beta, or dev) via the ``esphome_mcp_client`` library.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

import voluptuous as vol
import yaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from esphome_mcp_client import (
    DashboardClient,
    DashboardTarget,
    DeviceLogError,
    ESPHomeMCPError,
    SupervisorClient,
    WsClient,
    WsUnavailableError,
    async_stream_device_logs,
)

from .const import (
    ALLOWED_EXTENSIONS,
    API_ID,
    API_NAME,
    BLOCKED_FILES,
    DEFAULT_ALLOW_EXTRA_FILES,
    DOMAIN,
    ESPHOME_CONFIG_DIR,
    SECRET_KEY_PATTERN,
    SECRETS_FILE,
)
from .jobs import Job, JobRegistry

_LOGGER = logging.getLogger(__name__)

# Reusable schema fragment: every build/inventory tool accepts an optional
# add-on slug so the agent can target stable vs. beta vs. dev explicitly.
_ADDON_SLUG = vol.Optional("addon_slug")


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _sanitize_filename(filename: str, *, allow_subdirs: bool = False) -> str:
    """Reject path traversal and return the safe relative path.

    Any ``..`` sequence, absolute path, or backslash is treated as hostile
    rather than silently stripped, so a caller can never escape
    ``/config/esphome``. By default a directory separator is also rejected
    (bare filename only); when ``allow_subdirs`` is set, relative subpaths such
    as ``components/my_component/sensor.h`` are permitted but still cannot
    traverse out of the config directory.
    """
    if not filename or "\\" in filename or filename.startswith("/"):
        raise ValueError(f"Invalid filename: {filename!r}")
    parts = filename.split("/")
    if ".." in parts or "" in parts or "." in parts:
        raise ValueError(f"Invalid filename: {filename!r}")
    if not allow_subdirs and len(parts) > 1:
        raise ValueError(
            f"Invalid filename: {filename!r} (subdirectory paths require "
            "enabling extra file access in the integration options)."
        )
    return filename


def _guard(
    filename: str, *, require_yaml: bool = False, allow_extra: bool = False
) -> str:
    """Sanitize, then block secrets and (optionally) non-YAML files.

    ``allow_extra`` (driven by the integration option) relaxes two
    restrictions for the file tools: it permits relative subdirectory paths and
    lifts the ``.yaml``/``.yml`` requirement. Build tools never pass it - a
    configuration to validate/compile/flash is always a top-level YAML file.
    secrets.yaml is blocked (by basename, at any depth) regardless.
    """
    safe = _sanitize_filename(filename, allow_subdirs=allow_extra)
    if os.path.basename(safe) in BLOCKED_FILES:
        raise ValueError(f"Access to '{os.path.basename(safe)}' is not permitted.")
    if require_yaml and not allow_extra and not safe.endswith(ALLOWED_EXTENSIONS):
        raise ValueError("Only .yaml or .yml files are allowed.")
    return safe


def _allow_extra_files(hass: HomeAssistant) -> bool:
    """Read the current 'extra file access' option from integration state."""
    return bool(
        hass.data.get(DOMAIN, {}).get("allow_extra_files", DEFAULT_ALLOW_EXTRA_FILES)
    )


def _read_file(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _write_file(path: str, content: str) -> None:
    # Create parent directories so writes into a (new) subdirectory such as
    # components/my_component/ succeed; the dir is already inside the config dir.
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)


class _SecretKeyExists(Exception):
    """Raised when an insert-only secret write hits an existing key."""


def _insert_secret(path: str, key: str, value: str) -> None:
    """Insert ``key: value`` into secrets.yaml. Never overwrites; never reads
    values back out. Raises :class:`_SecretKeyExists` if the key is present."""
    content = ""
    existing: dict = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError as err:
            raise ValueError(f"secrets.yaml could not be parsed: {err}") from err
        if isinstance(parsed, dict):
            existing = parsed
        elif parsed is not None:
            raise ValueError("secrets.yaml is not a mapping of key: value")
    if key in existing:
        raise _SecretKeyExists(key)
    # json.dumps yields a YAML-safe double-quoted scalar (handles spaces/specials).
    prefix = "" if content == "" or content.endswith("\n") else "\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{prefix}{key}: {json.dumps(value)}\n")


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
    may target text introduced by an earlier one. "Exactly once" counts
    non-overlapping occurrences (str.count/str.replace semantics), so a
    self-overlapping old_string (e.g. "aa" within "aaa") can undercount.
    Returns the fully-patched string; never writes anything (that's the
    caller's job, once, after every patch has succeeded) - this is what
    makes the write atomic.
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


async def _async_connection(hass: HomeAssistant, slug: str | None):
    """Resolve the add-on and open a dashboard connection (base URL + auth).

    Shared by :func:`_async_dashboard` (legacy REST + WS spawn) and
    :func:`_async_build_client` (the multiplexed ``/ws`` API); both talk to the
    same add-on over the same Supervisor-ingress connection.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise ESPHomeMCPError(
            "SUPERVISOR_TOKEN is unavailable - is Home Assistant running under "
            "the Supervisor?"
        )
    session = async_get_clientsession(hass)
    supervisor = SupervisorClient(session, token)
    if not slug:
        slug = await supervisor.async_default_slug()

    # The target (base URL + ingress token) is stable, so cache it; the ingress
    # session it carries is not, so mint a fresh one per call in
    # open_dashboard_connection.
    cache: dict[str, DashboardTarget] = hass.data.setdefault(DOMAIN, {}).setdefault(
        "dashboard_target_cache", {}
    )
    target = cache.get(slug)
    if target is None:
        target = await supervisor.async_dashboard_target(slug)
        cache[slug] = target
    conn = await supervisor.open_dashboard_connection(target)
    return session, conn, slug


async def _async_dashboard(
    hass: HomeAssistant, slug: str | None
) -> tuple[DashboardClient, str]:
    """Legacy dashboard client (REST inventory/config + WS spawn) for an add-on."""
    session, conn, slug = await _async_connection(hass, slug)
    return DashboardClient(session, conn.base_url, headers=conn.headers), slug


class _BuildClient:
    """Runs a build/stream op on the Device Builder ``/ws`` API, falling back to
    the legacy WS spawn endpoints for a classic dashboard that lacks ``/ws``.

    Exposes the same method names/signatures as both underlying clients, so the
    build tools and background jobs can dispatch by name unchanged.
    """

    def __init__(self, ws: WsClient, legacy: DashboardClient) -> None:
        self._ws = ws
        self._legacy = legacy

    async def _dispatch(self, method: str, *args: Any, **kwargs: Any):
        try:
            return await getattr(self._ws, method)(*args, **kwargs)
        except WsUnavailableError:
            # Classic dashboard: /ws isn't there. The failure happens at connect
            # time (before any output), so retrying on legacy can't duplicate.
            return await getattr(self._legacy, method)(*args, **kwargs)

    async def compile(self, *args: Any, **kwargs: Any):
        return await self._dispatch("compile", *args, **kwargs)

    async def upload(self, *args: Any, **kwargs: Any):
        return await self._dispatch("upload", *args, **kwargs)

    async def clean(self, *args: Any, **kwargs: Any):
        return await self._dispatch("clean", *args, **kwargs)

    async def run(self, *args: Any, **kwargs: Any):
        return await self._dispatch("run", *args, **kwargs)

    async def validate(self, *args: Any, **kwargs: Any):
        return await self._dispatch("validate", *args, **kwargs)

    async def logs(self, *args: Any, **kwargs: Any):
        return await self._dispatch("logs", *args, **kwargs)


async def _async_build_client(
    hass: HomeAssistant, slug: str | None
) -> tuple[_BuildClient, str]:
    """Build/stream client for an add-on: /ws primary, legacy spawn fallback."""
    session, conn, slug = await _async_connection(hass, slug)
    ws = WsClient(session, conn.base_url, headers=conn.headers)
    legacy = DashboardClient(session, conn.base_url, headers=conn.headers)
    return _BuildClient(ws, legacy), slug


def _result_to_dict(slug: str, configuration: str, result: Any) -> dict[str, Any]:
    """Normalize a CommandResult into a JSON-serializable tool response."""
    return {
        "addon_slug": slug,
        "configuration": configuration,
        "success": result.success,
        "exit_code": result.exit_code,
        "truncated": result.truncated,
        "output": result.output,
    }


def _esphome_credentials(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Connection params for devices adopted by HA's ESPHome integration.

    Used to stream logs straight from the device's native API, which works
    regardless of whether the add-on runs the classic dashboard or the Device
    Builder. Encryption keys/passwords are used in-memory only and never
    returned to the caller.
    """
    creds: list[dict[str, Any]] = []
    for entry in hass.config_entries.async_entries("esphome"):
        data = entry.data
        creds.append(
            {
                "host": data.get("host"),
                "port": data.get("port") or 6053,
                "noise_psk": data.get("noise_psk"),
                "password": data.get("password"),
                "device_name": data.get("device_name"),
            }
        )
    return creds


def _match_device(
    creds: list[dict[str, Any]], *, name: str | None, address: str | None
) -> dict[str, Any] | None:
    """Pick the adopted-device entry matching a host (preferred) or device name."""
    if address:
        for cred in creds:
            if cred["host"] == address:
                return cred
    if name:
        for cred in creds:
            if cred["device_name"] == name:
                return cred
    return None


def _job_registry(hass: HomeAssistant) -> JobRegistry:
    return hass.data.setdefault(DOMAIN, {}).setdefault("jobs", JobRegistry())


async def _run_job(
    hass: HomeAssistant,
    job: Job,
    method: str,
    configuration: str,
    addon_slug: str | None,
    extra: dict[str, Any],
) -> None:
    """Background driver: run a build command, streaming lines into the job."""
    try:
        client, _ = await _async_build_client(hass, addon_slug)
        result = await getattr(client, method)(
            configuration, on_line=job.lines.append, **extra
        )
        job.exit_code = result.exit_code
        job.truncated = result.truncated
        job.status = "done"
    except ESPHomeMCPError as err:
        job.status = "error"
        job.error = str(err)
    finally:
        job.finished_at = time.time()


def _start_background_job(
    hass: HomeAssistant,
    kind: str,
    method: str,
    configuration: str,
    addon_slug: str | None,
    extra: dict[str, Any],
) -> dict[str, Any]:
    """Create a job, launch it as an HA background task, return its initial state."""
    job = _job_registry(hass).create(kind, configuration, addon_slug)
    hass.async_create_background_task(
        _run_job(hass, job, method, configuration, addon_slug, extra),
        name=f"esphome_mcp_bridge_{kind}_{job.id}",
    )
    response = job.to_dict()
    response["note"] = (
        "Started in the background. Poll esphome_job_status with this job_id "
        "(optionally with wait_seconds) until status is 'done' or 'error'."
    )
    return response


# --------------------------------------------------------------------------- #
# Discovery / inventory tools
# --------------------------------------------------------------------------- #
class ListAddonsTool(llm.Tool):
    """Discover which ESPHome add-ons are installed (stable/beta/dev)."""

    name = "esphome_list_addons"
    description = (
        "List the installed ESPHome dashboard add-ons (stable, beta, dev), with "
        "their slug, version, and running state, plus which one is used by "
        "default. Use the returned slug as 'addon_slug' on other tools to target "
        "a specific channel."
    )
    parameters = vol.Schema({})

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict[str, Any]:
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            return {"error": "SUPERVISOR_TOKEN unavailable; not running under Supervisor."}
        session = async_get_clientsession(hass)
        supervisor = SupervisorClient(session, token)
        try:
            addons = await supervisor.list_esphome_addons()
            default = await supervisor.async_default_slug() if addons else None
        except ESPHomeMCPError as err:
            return {"error": str(err)}
        return {
            "default_slug": default,
            "addons": [
                {
                    "slug": a.slug,
                    "name": a.name,
                    "version": a.version,
                    "version_latest": a.version_latest,
                    "state": a.state,
                    "update_available": a.update_available,
                }
                for a in addons
            ],
        }


class ListDevicesTool(llm.Tool):
    """Inventory configured ESPHome devices with online status and versions."""

    name = "esphome_list_devices"
    description = (
        "List configured ESPHome devices: name, configuration filename, ESPHome "
        "version, target platform, loaded integrations, network address, and "
        "whether the device is currently online. Use this to take inventory "
        "before editing or debugging."
    )
    parameters = vol.Schema({_ADDON_SLUG: str})

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict[str, Any]:
        try:
            client, slug = await _async_dashboard(hass, tool_input.tool_args.get("addon_slug"))
            devices = await client.inventory()
        except ESPHomeMCPError as err:
            return {"error": str(err)}
        return {"addon_slug": slug, "devices": devices}


# --------------------------------------------------------------------------- #
# Configuration file tools (filesystem)
# --------------------------------------------------------------------------- #
class ReadYamlTool(llm.Tool):
    """Read an ESPHome YAML configuration file."""

    name = "esphome_read_yaml"
    description = (
        "Read a file from /config/esphome (typically an ESPHome YAML config) to "
        "inspect it. With extra file access enabled, non-YAML files and "
        "subdirectory paths (e.g. components/my_component/sensor.h) also work."
    )
    parameters = vol.Schema({vol.Required("filename"): str})

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict[str, Any]:
        try:
            filename = _guard(
                tool_input.tool_args["filename"],
                allow_extra=_allow_extra_files(hass),
            )
        except ValueError as err:
            return {"error": str(err)}
        path = os.path.join(ESPHOME_CONFIG_DIR, filename)
        try:
            content = await hass.async_add_executor_job(_read_file, path)
        except FileNotFoundError:
            return {"error": f"File '{filename}' not found in {ESPHOME_CONFIG_DIR}."}
        except OSError as err:
            return {"error": str(err)}
        return {"filename": filename, "content": content}


class CreateConfigTool(llm.Tool):
    """Create a NEW ESPHome configuration file (fails if it already exists)."""

    name = "esphome_create_config"
    description = (
        "Create a new ESPHome YAML config in /config/esphome; fails if it already "
        "exists (use esphome_write_yaml to modify). With extra file access "
        "enabled, also creates non-YAML files and subdirectory paths (e.g. C++ in "
        "components/my_component/); missing parent dirs are created."
    )
    parameters = vol.Schema(
        {
            vol.Required("filename"): str,
            vol.Required("content"): str,
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
        if await hass.async_add_executor_job(os.path.exists, path):
            return {"error": f"'{filename}' already exists; use esphome_write_yaml."}
        try:
            await hass.async_add_executor_job(
                _write_file, path, tool_input.tool_args["content"]
            )
        except OSError as err:
            return {"error": str(err)}
        return {"success": True, "filename": filename, "created": True}


class WriteYamlTool(llm.Tool):
    """Write (overwrite) an ESPHome YAML configuration file."""

    name = "esphome_write_yaml"
    description = (
        "Write or overwrite an ESPHome YAML config in /config/esphome; validate "
        "or compile afterward to confirm. With extra file access enabled, also "
        "writes non-YAML files and subdirectory paths (e.g. C++ in "
        "components/my_component/); missing parent dirs are created."
    )
    parameters = vol.Schema(
        {
            vol.Required("filename"): str,
            vol.Required("content"): str,
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
            await hass.async_add_executor_job(
                _write_file, path, tool_input.tool_args["content"]
            )
        except OSError as err:
            return {"error": str(err)}
        return {"success": True, "filename": filename}


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
            vol.Required("patches"): vol.All(
                [
                    vol.Schema(
                        {
                            vol.Required("old_string"): str,
                            vol.Required("new_string"): str,
                        }
                    )
                ],
                vol.Length(min=1),
            ),
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


class AddSecretTool(llm.Tool):
    """Insert a key into secrets.yaml (insert-only, write-only)."""

    name = "esphome_add_secret"
    description = (
        "Add a secret to /config/esphome/secrets.yaml so a configuration's "
        "!secret references resolve. INSERT-ONLY and WRITE-ONLY: it never reads "
        "or returns existing secret values, and it returns an error if the key "
        "already exists rather than overwriting it. Use this when scaffolding a "
        "new device configuration to ensure every referenced secret exists "
        "(e.g. wifi_password, api_encryption_key, ota_password)."
    )
    parameters = vol.Schema(
        {
            vol.Required("key"): str,
            vol.Required("value"): str,
        }
    )

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict[str, Any]:
        key = tool_input.tool_args["key"]
        value = tool_input.tool_args["value"]
        if not re.match(SECRET_KEY_PATTERN, key):
            return {
                "error": (
                    f"Invalid secret key {key!r}; use a snake_case identifier "
                    "(letters, digits, underscore, hyphen)."
                )
            }
        path = os.path.join(ESPHOME_CONFIG_DIR, SECRETS_FILE)
        try:
            await hass.async_add_executor_job(_insert_secret, path, key, value)
        except _SecretKeyExists:
            return {
                "error": (
                    f"Secret {key!r} already exists; refusing to overwrite "
                    "(insert-only). Choose a different key or update it manually."
                )
            }
        except (OSError, ValueError) as err:
            return {"error": str(err)}
        # Deliberately never echo the value back.
        _LOGGER.info("esphome_add_secret: inserted secret key '%s'", key)
        return {"success": True, "key": key, "created": True}


# --------------------------------------------------------------------------- #
# Build-cycle tools (dashboard WebSocket spawn protocol)
# --------------------------------------------------------------------------- #
class _BuildTool(llm.Tool):
    """Base for tools that run a spawn command against one configuration."""

    # Subclasses set: name, description, _method (DashboardClient coroutine name).
    _method: str = ""
    _supports_background: bool = False
    parameters = vol.Schema(
        {
            vol.Required("configuration"): str,
            _ADDON_SLUG: str,
        }
    )

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict[str, Any]:
        args = tool_input.tool_args
        try:
            configuration = _guard(args["configuration"], require_yaml=True)
        except ValueError as err:
            return {"error": str(err)}
        if self._supports_background and args.get("background"):
            return _start_background_job(
                hass, self._method, self._method, configuration,
                args.get("addon_slug"), {},
            )
        try:
            client, slug = await _async_build_client(hass, args.get("addon_slug"))
            result = await getattr(client, self._method)(configuration)
        except ESPHomeMCPError as err:
            return {"error": str(err)}
        return _result_to_dict(slug, configuration, result)


class ValidateTool(_BuildTool):
    """Validate an ESPHome configuration without building it."""

    name = "esphome_validate"
    description = (
        "Validate an ESPHome configuration file without building it. Returns the "
        "validator output and exit code (0 = valid). Run this after editing, "
        "before compiling — it's much faster than a full compile."
    )
    _method = "validate"


class CompileTool(_BuildTool):
    """Compile an ESPHome configuration into firmware."""

    name = "esphome_compile"
    description = (
        "Compile an ESPHome configuration into firmware. Returns the build log "
        "and exit code (0 = success). Compilation runs to completion before the "
        "result is returned. For a slow first build (which downloads a toolchain "
        "and can exceed the client timeout), pass background=true to get a job_id "
        "immediately, then poll esphome_job_status."
    )
    _method = "compile"
    _supports_background = True
    parameters = vol.Schema(
        {
            vol.Required("configuration"): str,
            vol.Optional("background", default=False): bool,
            _ADDON_SLUG: str,
        }
    )


class CleanTool(_BuildTool):
    """Clean cached build files for a configuration."""

    name = "esphome_clean"
    description = (
        "Delete cached build artifacts for an ESPHome configuration. Use this "
        "when a build behaves unexpectedly and you want a clean rebuild."
    )
    _method = "clean"


class _FlashTool(llm.Tool):
    """Base for tools that take an optional target port (upload/run/logs)."""

    _method: str = ""
    parameters = vol.Schema(
        {
            vol.Required("configuration"): str,
            vol.Optional("port", default="OTA"): str,
            vol.Optional("background", default=False): bool,
            _ADDON_SLUG: str,
        }
    )

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict[str, Any]:
        args = tool_input.tool_args
        try:
            configuration = _guard(args["configuration"], require_yaml=True)
        except ValueError as err:
            return {"error": str(err)}
        port = args.get("port", "OTA")
        if args.get("background"):
            return _start_background_job(
                hass, self._method, self._method, configuration,
                args.get("addon_slug"), {"port": port},
            )
        try:
            client, slug = await _async_build_client(hass, args.get("addon_slug"))
            result = await getattr(client, self._method)(configuration, port)
        except ESPHomeMCPError as err:
            return {"error": str(err)}
        response = _result_to_dict(slug, configuration, result)
        response["port"] = port
        return response


class UploadTool(_FlashTool):
    """Flash already-built (or freshly built) firmware to a device."""

    name = "esphome_upload"
    description = (
        "Upload (flash) firmware for an ESPHome configuration to a device. "
        "'port' defaults to 'OTA' (over-the-air); pass a device IP/hostname or "
        "serial port to target it explicitly. Returns the upload log and exit "
        "code. Pass background=true to run as a job and poll esphome_job_status."
    )
    _method = "upload"


class RunTool(_FlashTool):
    """Compile and upload in a single step."""

    name = "esphome_run"
    description = (
        "Compile an ESPHome configuration AND upload it to the device in one "
        "step (the equivalent of the dashboard 'Install' action). 'port' "
        "defaults to 'OTA'. Returns the combined build + upload log. Pass "
        "background=true to run as a job and poll esphome_job_status."
    )
    _method = "run"


class LogsTool(llm.Tool):
    """Capture a bounded window of live device logs for debugging.

    Prefers streaming directly from the device's native API (works on both the
    classic dashboard and the new Device Builder); falls back to the dashboard's
    /logs endpoint (classic dashboard only).
    """

    name = "esphome_logs"
    description = (
        "Stream live logs from a running ESPHome device for debugging, capturing "
        "a bounded window (default ~30s or 500 lines). If the device is adopted "
        "in Home Assistant, logs are read directly from the device (works on both "
        "the classic dashboard and the new Device Builder); otherwise it falls "
        "back to the dashboard log endpoint. Optionally pass 'address' to target a "
        "device IP/hostname. Returns captured log lines; 'truncated' is true if "
        "the window limit was hit."
    )
    parameters = vol.Schema(
        {
            vol.Required("configuration"): str,
            vol.Optional("address"): str,
            vol.Optional("max_seconds", default=30): vol.All(
                vol.Coerce(float), vol.Range(min=1, max=120)
            ),
            _ADDON_SLUG: str,
        }
    )

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict[str, Any]:
        args = tool_input.tool_args
        try:
            configuration = _guard(args["configuration"], require_yaml=True)
        except ValueError as err:
            return {"error": str(err)}
        max_seconds = args.get("max_seconds", 30)
        address = args.get("address")
        addon_slug = args.get("addon_slug")

        # Identify the device (name + address) from dashboard inventory.
        name: str | None = None
        dash_client: DashboardClient | None = None
        slug: str | None = None
        try:
            dash_client, slug = await _async_dashboard(hass, addon_slug)
            for dev in await dash_client.inventory():
                if dev.get("configuration") == configuration:
                    name = dev.get("name")
                    address = address or dev.get("address")
                    break
        except ESPHomeMCPError:
            pass  # dashboard may be unreachable; device-direct can still work

        # 1. Direct from the device (backend-independent) if it's adopted in HA.
        device_error: str | None = None
        creds = _match_device(_esphome_credentials(hass), name=name, address=address)
        if creds:
            host = address or creds["host"]
            try:
                result = await async_stream_device_logs(
                    host,
                    port=creds["port"],
                    password=creds["password"],
                    noise_psk=creds["noise_psk"],
                    max_seconds=max_seconds,
                )
            except DeviceLogError as err:
                device_error = str(err)
            else:
                response = _result_to_dict(slug or "", configuration, result)
                response["mode"] = "device"
                response["address"] = host
                return response

        # 2. Dashboard fallback: /ws devices/logs on the Device Builder, or the
        #    legacy /logs spawn on a classic dashboard (handled by _BuildClient).
        if dash_client is None:
            return {
                "error": device_error
                or "No reachable ESPHome dashboard, and the device is not adopted "
                "in Home Assistant for direct log streaming."
            }
        try:
            build_client, _ = await _async_build_client(hass, addon_slug)
            result = await build_client.logs(configuration, "OTA", max_seconds=max_seconds)
        except ESPHomeMCPError as err:
            msg = str(err)
            if device_error:
                msg += f" | device-direct also failed: {device_error}"
            elif not creds:
                msg += (
                    " | tip: adopt this device in Home Assistant's ESPHome "
                    "integration to enable direct log streaming."
                )
            return {"error": msg}
        response = _result_to_dict(slug, configuration, result)
        response["mode"] = "dashboard"
        return response


class JobStatusTool(llm.Tool):
    """Poll the status and output of a background build job."""

    name = "esphome_job_status"
    description = (
        "Check a background ESPHome job (compile/upload/run started with "
        "background=true). Returns status ('running'/'done'/'error'), exit code, "
        "and the build output captured so far. Optionally set 'wait_seconds' to "
        "block until the job finishes (up to that many seconds) before returning."
    )
    parameters = vol.Schema(
        {
            vol.Required("job_id"): str,
            vol.Optional("wait_seconds", default=0): vol.All(
                vol.Coerce(float), vol.Range(min=0, max=120)
            ),
        }
    )

    async def async_call(
        self, hass: HomeAssistant, tool_input: llm.ToolInput, llm_context: llm.LLMContext
    ) -> dict[str, Any]:
        job_id = tool_input.tool_args["job_id"]
        job = _job_registry(hass).get(job_id)
        if job is None:
            return {"error": f"No job with id {job_id!r} (it may have expired)."}
        wait = tool_input.tool_args.get("wait_seconds", 0)
        deadline = time.monotonic() + wait
        while not job.is_finished and time.monotonic() < deadline:
            await asyncio.sleep(min(2.0, max(0.1, deadline - time.monotonic())))
        return job.to_dict()


# --------------------------------------------------------------------------- #
# The API
# --------------------------------------------------------------------------- #
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

# Appended to the prompt only when the 'extra file access' option is enabled.
_EXTRA_FILES_PROMPT = (
    " Extra file access is enabled: the read/create/write tools may also operate "
    "on non-YAML files (e.g. C++ in a custom components/ directory) and accept "
    "relative subdirectory paths such as components/my_component/sensor.h. "
    "secrets.yaml remains off-limits, and paths can never escape /config/esphome."
)


class ESPHomeBuilderAPI(llm.API):
    """LLM API exposing the ESPHome development cycle."""

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(hass=hass, id=API_ID, name=API_NAME)

    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
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
        api_prompt = _API_PROMPT
        if _allow_extra_files(self.hass):
            api_prompt += _EXTRA_FILES_PROMPT
        return llm.APIInstance(
            api=self,
            api_prompt=api_prompt,
            llm_context=llm_context,
            tools=tools,
        )
