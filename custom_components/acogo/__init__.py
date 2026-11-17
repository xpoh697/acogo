"""The acoGO integration."""
from __future__ import annotations

from datetime import timedelta
import importlib.util
import io
import json
import logging
from pathlib import Path
import time
from typing import Any
import uuid

from aiohttp import web

from homeassistant.components import frontend, webhook
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_STATE_CHANGED,
    Platform,
)
from homeassistant.core import CoreState, Event, HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.event import async_track_time_interval

from .api import AcoGoApiClient, AcoGoAuthError
from .const import (
    APP_MODELS,
    CONF_DEV_ID,
    CONF_DEVICE_PASSWORD,
    DOMAIN,
    ORDER_END_CALL,
    ORDER_REJECT_CALL,
    SERVICE_TRIGGER_INCOMING_CALL,
    VERSION,
    WEBHOOK_ID,
)
from .coordinator import AcoGoDataUpdateCoordinator
from .webrtc import fetch_ice_servers, generate_signed_wss_url, setup_acogo_file_logger

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.LOCK,
    Platform.BUTTON,
    Platform.BINARY_SENSOR,
]

ALLOWED_STATIC_FILES = {"acogo-webrtc-card.js"}


def _capture_adb_screenshot_sync(host: str, key_path: str) -> bytes | None:
    """Capture live tablet screen via ADB TCP (port 5555) using authorized RSA key."""
    from adb_shell.adb_device import AdbDeviceTcp
    from adb_shell.auth.sign_pythonrsa import PythonRSASigner

    priv_file = Path(key_path)
    pub_file = Path(f"{key_path}.pub")
    if not priv_file.is_file() or not pub_file.is_file():
        _LOGGER.debug("ADB key files not found at %s", key_path)
        return None

    priv = priv_file.read_text(encoding="utf-8")
    pub = pub_file.read_text(encoding="utf-8")
    signer = PythonRSASigner(pub, priv)
    device = AdbDeviceTcp(host, 5555, default_transport_timeout_s=5.0)
    try:
        device.connect(rsa_keys=[signer], auth_timeout_s=3.0)
        remote_tmp = "/data/local/tmp/acogo_call.png"
        device.shell(f"screencap -p {remote_tmp}", read_timeout_s=5.0)
        buf = io.BytesIO()
        device.pull(remote_tmp, buf)
        device.shell(f"rm -f {remote_tmp}", read_timeout_s=3.0)
        data = buf.getvalue()
        return data if len(data) > 10240 else None
    finally:
        try:
            device.close()
        except Exception:
            pass


class CardStaticView(HomeAssistantView):
    """View to serve static Lovelace card files with no-cache and CORS headers."""

    url = f"/api/{DOMAIN}/static/{{filename}}"
    name = f"api:{DOMAIN}:static"
    requires_auth = False
    cors_allowed = True

    def __init__(self, www_path: Path) -> None:
        """Initialize view with www path."""
        self._www_path = www_path

    async def get(self, request: web.Request, filename: str) -> web.Response:
        """Handle GET request for static files."""
        if filename not in ALLOWED_STATIC_FILES:
            return web.Response(status=404)

        file_path = self._www_path / filename
        if not file_path.is_file():
            return web.Response(status=404)

        try:
            return web.FileResponse(
                file_path,
                headers={"Cache-Control": "no-cache, no-store, must-revalidate, max-age=0"},
            )
        except Exception:
            return web.Response(status=500)


async def _async_register_card(hass: HomeAssistant) -> None:
    """Register the Lovelace card with Lovelace resources and frontend."""
    card_url = f"/api/{DOMAIN}/static/acogo-webrtc-card.js?v={VERSION}"

    try:
        frontend.add_extra_js_url(hass, card_url)
        _LOGGER.debug("Registered acoGO card via add_extra_js_url: %s", card_url)
    except Exception as err:
        _LOGGER.warning("Failed to register acoGO card via add_extra_js_url: %s", err)

    registered = await _async_register_lovelace_resource(hass, card_url)
    if registered:
        _LOGGER.info("Registered acoGO Lovelace resource: %s", card_url)


async def _async_register_lovelace_resource(hass: HomeAssistant, url: str) -> bool:
    """Create or update the Lovelace resource entry, cleaning up duplicates and legacy URLs."""
    lovelace_data = hass.data.get("lovelace")
    if lovelace_data is None:
        return False

    resources = getattr(lovelace_data, "resources", None)
    if resources is None:
        return False

    if not hasattr(resources, "async_create_item") or not hasattr(resources, "async_update_item"):
        return False

    base_url = url.split("?")[0]
    legacy_url = "/local/acogo-webrtc-card.js"
    matched_items = []

    try:
        for item in resources.async_items():
            existing_url = item.get("url") or ""
            existing_base = existing_url.split("?")[0]
            if existing_base in (base_url, legacy_url):
                matched_items.append(item)
    except Exception:
        return False

    try:
        if matched_items:
            first_item = matched_items[0]
            if first_item.get("url") != url:
                await resources.async_update_item(first_item["id"], {"res_type": "module", "url": url})
                _LOGGER.info("Updated Lovelace resource to: %s", url)

            if len(matched_items) > 1 and hasattr(resources, "async_delete_item"):
                for dup in matched_items[1:]:
                    await resources.async_delete_item(dup["id"])
                    _LOGGER.info("Removed duplicate/legacy Lovelace resource: %s", dup.get("url"))
        else:
            await resources.async_create_item({"res_type": "module", "url": url})
            _LOGGER.info("Created Lovelace resource: %s", url)
        return True
    except Exception as err:
        _LOGGER.warning("Failed to register/update Lovelace resource: %s", err)
        return False


def _get_active_entry_runtime(
    hass: HomeAssistant,
) -> tuple[AcoGoApiClient, AcoGoDataUpdateCoordinator] | None:
    """Return the active (api, coordinator) pair for acoGO from hass.data."""
    for value in hass.data.get(DOMAIN, {}).values():
        if isinstance(value, dict) and "coordinator" in value and "api" in value:
            return value["api"], value["coordinator"]
    return None


def _resolve_device_id(
    coord: AcoGoDataUpdateCoordinator, raw_device_id: str | None
) -> str | None:
    """Resolve an intercom device_id, falling back to the active panel if stale/omitted."""
    data = coord.data or {}
    if raw_device_id and raw_device_id in data:
        return raw_device_id

    for dev_id, dev_data in data.items():
        if dev_data.get("info", {}).get("model") not in APP_MODELS:
            if raw_device_id:
                _LOGGER.warning(
                    "Requested device_id '%s' not found in active devices; using '%s'",
                    raw_device_id,
                    dev_id,
                )
            return dev_id
    return next(iter(data), None)


def _cancel_orphaned_snapshot_timers(hass: HomeAssistant) -> None:
    """Cancel any orphaned interval timers left over from pre-v1.1.8 reloads."""
    try:
        for handle in list(getattr(hass.loop, "_scheduled", ())):
            if not handle.cancelled() and "_async_background_snapshot_refresh" in repr(handle):
                handle.cancel()
                _LOGGER.info("Cancelled orphaned legacy acoGO background refresh timer")
    except Exception as err:
        _LOGGER.debug("Timer sweep skipped: %s", err)


def _safe_unregister_webhook(hass: HomeAssistant, exclude_entry_id: str | None = None) -> None:
    """Unregister the incoming call webhook if no other acoGO entries remain."""
    domain_data = hass.data.get(DOMAIN, {})
    has_other_entries = any(
        isinstance(val, dict) and key != exclude_entry_id and "coordinator" in val
        for key, val in domain_data.items()
    )
    if has_other_entries:
        return
    try:
        webhook.async_unregister(hass, WEBHOOK_ID)
    except Exception:
        pass


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up acoGO from a config entry."""
    await hass.async_add_executor_job(setup_acogo_file_logger)
    _cancel_orphaned_snapshot_timers(hass)

    hass.data.setdefault(DOMAIN, {})

    # Register static HTTP view once per HA lifetime
    if not hass.data[DOMAIN].get("_view_registered"):
        if getattr(hass, "http", None) is not None:
            www_path = Path(__file__).parent / "www"
            hass.http.register_view(CardStaticView(www_path))
        hass.data[DOMAIN]["_view_registered"] = True

    # Register card automatically in Lovelace (deferred if HA is still starting)
    if hass.state == CoreState.running:
        hass.async_create_task(_async_register_card(hass))
    else:
        async def _register_card_after_start(event: Any) -> None:
            await _async_register_card(hass)

        entry.async_on_unload(
            hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _register_card_after_start)
        )

    dev_id: str = entry.data[CONF_DEV_ID]
    device_password: str | None = entry.data.get(CONF_DEVICE_PASSWORD)
    username: str = entry.data[CONF_USERNAME]
    password: str = entry.data[CONF_PASSWORD]

    session = aiohttp_client.async_get_clientsession(hass)

    def _on_password_updated(new_pwd: str) -> None:
        """Update entry data if cloud rotated or newly generated devicePassword."""
        new_data = {**entry.data, CONF_DEVICE_PASSWORD: new_pwd}
        hass.config_entries.async_update_entry(entry, data=new_data)
        _LOGGER.debug("Updated config entry with new devicePassword")

    api = AcoGoApiClient(
        session=session,
        dev_id=dev_id,
        device_password=device_password,
        username=username,
        password=password,
        on_device_password_updated=_on_password_updated,
    )

    if not device_password:
        try:
            await api.register_device()
        except AcoGoAuthError as err:
            raise ConfigEntryNotReady(f"Failed to authenticate with acoGO Cloud: {err}") from err

    coordinator = AcoGoDataUpdateCoordinator(hass, api)
    await coordinator.async_config_entry_first_refresh()
    coordinator.start_kiosk_monitor()

    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
    }

    # Built-in state listener for tablet notification & Fully Kiosk foreground_app call triggers
    @callback
    def _async_on_tablet_state_changed(event: Event[Any]) -> None:
        """Detect incoming acoGO call from notification or Fully Kiosk foreground_app sensors."""
        entity_id: str = event.data.get("entity_id") or ""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if new_state is None or new_state.state in ("unknown", "unavailable"):
            return

        if entity_id.endswith("_last_notification"):
            if old_state is not None and new_state.attributes.get("package") == "pl.aco.acogo":
                hass.async_create_task(
                    coordinator.async_handle_call_trigger(source=entity_id)
                )
        elif entity_id.endswith("_foreground_app"):
            if old_state is not None:
                if (
                    old_state.state not in ("unknown", "unavailable", "pl.aco.acogo")
                    and new_state.state == "pl.aco.acogo"
                ):
                    hass.async_create_task(
                        coordinator.async_handle_call_trigger(source=entity_id)
                    )
                elif (
                    old_state.state == "pl.aco.acogo"
                    and new_state.state != "pl.aco.acogo"
                ):
                    for active_dev_id in list(coordinator._call_reset_handles):
                        coordinator.reset_call_state(active_dev_id)

    entry.async_on_unload(
        hass.bus.async_listen(EVENT_STATE_CHANGED, _async_on_tablet_state_changed)
    )

    # Register custom service: send_order
    async def async_handle_send_order(call: ServiceCall) -> None:
        runtime = _get_active_entry_runtime(hass)
        if not runtime:
            raise HomeAssistantError("acoGO integration is not ready")
        active_api, active_coord = runtime
        device_id = _resolve_device_id(active_coord, call.data.get("device_id"))
        if not device_id:
            raise HomeAssistantError("Intercom device not found")
        order_id: str = call.data["order_id"]
        _LOGGER.info("Executing custom acoGO order '%s' for device %s", order_id, device_id)
        try:
            await active_api.send_order(device_id, order_id)
            if order_id in (ORDER_END_CALL, ORDER_REJECT_CALL):
                active_coord.reset_call_state(device_id)
        except Exception as err:
            raise HomeAssistantError(f"Failed to execute order '{order_id}': {err}") from err

    hass.services.async_register(DOMAIN, "send_order", async_handle_send_order)

    # Register custom service: switch_camera
    async def async_handle_switch_camera(call: ServiceCall) -> None:
        runtime = _get_active_entry_runtime(hass)
        if not runtime:
            raise HomeAssistantError("acoGO integration is not ready")
        active_api, active_coord = runtime
        device_id = _resolve_device_id(active_coord, call.data.get("device_id"))
        if not device_id:
            raise HomeAssistantError("Intercom device not found")
        _LOGGER.info("Switching camera for device %s", device_id)
        try:
            await active_api.switch_video(device_id)
        except Exception as err:
            raise HomeAssistantError(f"Failed to switch camera: {err}") from err

    hass.services.async_register(DOMAIN, "switch_camera", async_handle_switch_camera)

    # Register custom service: trigger_incoming_call
    async def async_handle_trigger_call(call: ServiceCall) -> None:
        runtime = _get_active_entry_runtime(hass)
        if not runtime:
            raise HomeAssistantError("acoGO integration is not ready")
        _, active_coord = runtime
        device_id = _resolve_device_id(active_coord, call.data.get("device_id"))
        await active_coord.async_handle_call_trigger(device_id=device_id, source="service")

    hass.services.async_register(DOMAIN, SERVICE_TRIGGER_INCOMING_CALL, async_handle_trigger_call)

    # Register native webhook for wall tablet (always binds dynamically to active coordinator)
    async def async_handle_webhook(hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
        runtime = _get_active_entry_runtime(hass)
        if not runtime:
            return web.Response(status=503, text="Coordinator not ready")
        _, active_coord = runtime

        raw_device_id = None
        action = "ring"
        try:
            if request.can_read_body:
                text = await request.text()
                if text and text.strip():
                    body = json.loads(text)
                    if isinstance(body, dict):
                        raw_device_id = body.get("device_id")
                        action = body.get("action", "ring")
        except Exception:
            pass

        device_id = _resolve_device_id(active_coord, raw_device_id)
        if action == "cancel" and device_id:
            active_coord.reset_call_state(device_id)
            return web.json_response({"status": "cancelled"})

        success = await active_coord.async_handle_call_trigger(device_id=device_id, source="webhook")
        return web.json_response({"status": "ok" if success else "ignored"})

    try:
        webhook.async_unregister(hass, WEBHOOK_ID)
    except Exception:
        pass
    webhook.async_register(
        hass,
        DOMAIN,
        "acoGO Incoming Call",
        WEBHOOK_ID,
        async_handle_webhook,
        local_only=True,
    )
    entry.async_on_unload(lambda: _safe_unregister_webhook(hass, exclude_entry_id=entry.entry_id))
    _LOGGER.debug("Registered acoGO call webhook: %s", WEBHOOK_ID)

    # Register custom service: start_webrtc_stream
    async def async_handle_start_stream(call: ServiceCall) -> dict[str, Any]:
        runtime = _get_active_entry_runtime(hass)
        if not runtime:
            raise HomeAssistantError("acoGO integration is not ready")
        _, active_coord = runtime

        device_id = _resolve_device_id(active_coord, call.data.get("device_id"))
        if not device_id:
            raise HomeAssistantError("Intercom device not found")

        params = await active_coord.async_start_preview(device_id)
        aws = params.get("aws", {})
        if not aws or not aws.get("channel arn"):
            raise HomeAssistantError("Failed to obtain valid AWS streaming credentials from acoGO Cloud")
        if aws.get("access key ID"):
            active_coord.last_valid_aws[device_id] = (time.time(), aws)

        client_id = f"HABrowser_{uuid.uuid4().hex[:8]}"
        wss_url = generate_signed_wss_url(aws, client_id)
        ice_servers = await fetch_ice_servers(session, aws)
        ice_list: list[dict[str, Any]] = []
        for s in ice_servers:
            item: dict[str, Any] = {"urls": s.urls}
            if s.username:
                item["username"] = s.username
            if s.credential:
                item["credential"] = s.credential
            ice_list.append(item)

        channel_name = aws.get("channel name") or aws.get("channelName", "") or aws.get("channel_name", "")

        return {
            "wss_url": wss_url,
            "signed_wss_url": wss_url,
            "ice_servers": ice_list,
            "device_id": device_id,
            "channel_name": channel_name,
            "channel_arn": aws.get("channel arn"),
            "region": aws.get("region", "eu-west-2"),
            "timeout": 45,
            "call_id": params.get("callId"),
        }

    hass.services.async_register(
        DOMAIN,
        "start_webrtc_stream",
        async_handle_start_stream,
        supports_response=SupportsResponse.ONLY,
    )

    # Register custom service: stop_webrtc_stream
    async def async_handle_stop_stream(call: ServiceCall) -> None:
        runtime = _get_active_entry_runtime(hass)
        if not runtime:
            return
        _, active_coord = runtime
        device_id = _resolve_device_id(active_coord, call.data.get("device_id"))
        if device_id:
            await active_coord.async_stop_preview(device_id)

    hass.services.async_register(DOMAIN, "stop_webrtc_stream", async_handle_stop_stream)

    # Register custom service: capture_snapshot
    async def async_handle_capture_snapshot(call: ServiceCall) -> dict[str, Any]:
        """Capture live call screenshot via ADB if ringing, else WebRTC snapshot."""
        runtime = _get_active_entry_runtime(hass)
        if not runtime:
            raise HomeAssistantError("acoGO integration is not ready")
        _, active_coord = runtime

        device_id = _resolve_device_id(active_coord, call.data.get("device_id"))
        if not device_id:
            raise HomeAssistantError("Intercom device not found")

        raw_filename = call.data.get("filename", "/config/www/doorbell_latest.jpg")
        timeout = float(call.data.get("timeout", 20.0))

        if hass.config.is_allowed_path(raw_filename):
            target_path = Path(raw_filename)
        else:
            safe_name = Path(raw_filename).name or "doorbell_latest.jpg"
            target_path = Path(hass.config.path("www", safe_name))

        await hass.async_add_executor_job(target_path.parent.mkdir, 0o755, True, True)

        jpeg_bytes: bytes | None = None
        has_aiortc = importlib.util.find_spec("aiortc") is not None
        has_adb = importlib.util.find_spec("adb_shell") is not None
        is_ringing = bool((active_coord.data or {}).get(device_id, {}).get("is_ringing", False))

        # During an active call, capture the tablet screen directly via ADB (shows live acoGO video)
        if is_ringing and has_adb:
            key_path = hass.config.path(".storage", "acogo_adbkey")
            for k_entry in hass.config_entries.async_entries("fully_kiosk"):
                host = k_entry.data.get("host")
                if not host:
                    continue
                try:
                    adb_bytes = await hass.async_add_executor_job(
                        _capture_adb_screenshot_sync, host, key_path
                    )
                    if adb_bytes:
                        jpeg_bytes = adb_bytes
                        active_coord.last_valid_snapshot[device_id] = (time.time(), adb_bytes)
                        _LOGGER.info("Captured live tablet call screenshot via ADB (%s, %d bytes)", host, len(adb_bytes))
                        break
                except Exception as adb_err:
                    _LOGGER.warning("ADB screenshot failed for %s: %s", host, adb_err)

        if not jpeg_bytes and has_aiortc:
            params = await active_coord.async_start_preview(
                device_id, retry_count=1 if is_ringing else 3
            )
            aws = params.get("aws") if isinstance(params, dict) else None

            if isinstance(aws, dict) and aws.get("access key ID") and aws.get("channel arn"):
                active_coord.last_valid_aws[device_id] = (time.time(), aws)
                from .webrtc import async_capture_webrtc_snapshot
                jpeg_bytes = await async_capture_webrtc_snapshot(session, aws, timeout=timeout)
                if jpeg_bytes:
                    active_coord.last_valid_snapshot[device_id] = (time.time(), jpeg_bytes)

            if params and not is_ringing:
                await active_coord.async_stop_preview(device_id)

        # Fallback 1: Use cached real frame (max 60s during active call to avoid stale empty photos)
        if not jpeg_bytes:
            cached_entry = active_coord.last_valid_snapshot.get(device_id)
            if cached_entry:
                cached_time, cached_bytes = cached_entry
                max_age = 60.0 if is_ringing else 900.0
                if time.time() - cached_time < max_age:
                    _LOGGER.info("Using cached real camera snapshot from %s ago", int(time.time() - cached_time))
                    jpeg_bytes = cached_bytes

        # Fallback 2: Generate status graphic card if no real frame available
        if not jpeg_bytes:
            from .webrtc import generate_snapshot_fallback_card
            info = (active_coord.data or {}).get(device_id, {}).get("info", {})
            panel_name = info.get("name", f"acoGO {device_id}")
            jpeg_bytes = await hass.async_add_executor_job(generate_snapshot_fallback_card, panel_name)

        def _write_file(p: Path, data: bytes) -> None:
            with open(p, "wb") as f:
                f.write(data)

        await hass.async_add_executor_job(_write_file, target_path, jpeg_bytes)

        _LOGGER.info("Saved acoGO snapshot to %s (%d bytes)", target_path, len(jpeg_bytes))
        return {
            "success": True,
            "filename": str(target_path),
            "bytes": len(jpeg_bytes),
        }

    hass.services.async_register(
        DOMAIN,
        "capture_snapshot",
        async_handle_capture_snapshot,
        supports_response=SupportsResponse.OPTIONAL,
    )

    # Periodic background snapshot refresher (every 10 minutes), only if aiortc is available
    async def _async_background_snapshot_refresh(*_: Any) -> None:
        if not coordinator.data:
            return
        for dev_id, dev_data in list(coordinator.data.items()):
            if (
                dev_data.get("is_online")
                and not coordinator.is_preview_active(dev_id)
                and not dev_data.get("is_ringing")
            ):
                try:
                    p = await coordinator.async_start_preview(dev_id, retry_count=1)
                    aws_p = p.get("aws")
                    if isinstance(aws_p, dict) and aws_p.get("access key ID") and aws_p.get("channel arn"):
                        coordinator.last_valid_aws[dev_id] = (time.time(), aws_p)
                        from .webrtc import async_capture_webrtc_snapshot
                        frame = await async_capture_webrtc_snapshot(session, aws_p, timeout=12.0)
                        if frame:
                            coordinator.last_valid_snapshot[dev_id] = (time.time(), frame)
                            _LOGGER.debug("Background camera frame cache refreshed for %s", dev_id)
                except Exception as err:
                    _LOGGER.debug("Background snapshot refresh error for %s: %s", dev_id, err)
                finally:
                    await coordinator.async_stop_preview(dev_id)

    if importlib.util.find_spec("aiortc") is not None:
        entry.async_on_unload(
            async_track_time_interval(hass, _async_background_snapshot_refresh, timedelta(minutes=10))
        )
    else:
        _LOGGER.debug("Skipping background WebRTC snapshot refresh: 'aiortc' is not installed")

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator: AcoGoDataUpdateCoordinator | None = data.get("coordinator")
    if coordinator:
        coordinator.async_shutdown()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        has_active_entries = any(
            isinstance(val, dict) and "coordinator" in val
            for val in hass.data.get(DOMAIN, {}).values()
        )
        if not has_active_entries:
            hass.services.async_remove(DOMAIN, "start_webrtc_stream")
            hass.services.async_remove(DOMAIN, "stop_webrtc_stream")
            hass.services.async_remove(DOMAIN, "capture_snapshot")
            hass.services.async_remove(DOMAIN, "send_order")
            hass.services.async_remove(DOMAIN, "switch_camera")
            hass.services.async_remove(DOMAIN, SERVICE_TRIGGER_INCOMING_CALL)
            _safe_unregister_webhook(hass)

    return unload_ok
