"""The acoGO! integration."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
import uuid

from aiohttp import web

from homeassistant.components import frontend
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import CoreState, HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AcoGoApiClient
from .const import CONF_DEV_ID, CONF_DEVICE_PASSWORD, CONF_PASSWORD, CONF_USERNAME, DOMAIN, VERSION
from .coordinator import AcoGoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.LOCK,
    Platform.BUTTON,
    Platform.BINARY_SENSOR,
]

ALLOWED_STATIC_FILES = {"acogo-webrtc-card.js"}


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

    # 1. Direct injection into all dashboards immediately (works across all Lovelace modes)
    try:
        frontend.add_extra_js_url(hass, card_url)
        _LOGGER.debug("Registered acoGO card via add_extra_js_url: %s", card_url)
    except Exception as err:
        _LOGGER.warning("Failed to register acoGO card via add_extra_js_url: %s", err)

    # 2. Register in Lovelace storage resources
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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up acoGO! from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Register static HTTP view once per HA lifetime
    if not hass.data[DOMAIN].get("_view_registered"):
        if getattr(hass, "http", None) is not None:
            www_path = Path(__file__).parent / "www"
            hass.http.register_view(CardStaticView(www_path))
        hass.data[DOMAIN]["_view_registered"] = True

    # Register card automatically in Lovelace
    if hass.state == CoreState.running:
        hass.async_create_task(_async_register_card(hass))
    else:
        async def _register_card_after_start(event: Any) -> None:
            await _async_register_card(hass)

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _register_card_after_start)

    # Automatically clean up deprecated camera, switch, and preview button entities from entity registry
    ent_reg = er.async_get(hass)
    deprecated_entries = [
        entity_entry.entity_id
        for entity_entry in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        if entity_entry.domain in ("camera", "switch")
        or entity_entry.unique_id.endswith(("_start_preview", "_stop_preview", "_camera_preview_switch"))
    ]
    for entity_id in deprecated_entries:
        ent_reg.async_remove(entity_id)
        _LOGGER.info("Removed deprecated entity from registry: %s", entity_id)

    session = async_get_clientsession(hass)

    def _on_password_updated(new_password: str) -> None:
        """Persist newly rotated devicePassword to ConfigEntry."""
        _LOGGER.info("Persisting rotated acoGO devicePassword to ConfigEntry")
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_DEVICE_PASSWORD: new_password},
        )

    client = AcoGoApiClient(
        session=session,
        dev_id=entry.data[CONF_DEV_ID],
        device_password=entry.data[CONF_DEVICE_PASSWORD],
        username=entry.data.get(CONF_USERNAME),
        password=entry.data.get(CONF_PASSWORD),
        on_device_password_updated=_on_password_updated,
    )

    coordinator = AcoGoDataUpdateCoordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()

    # Start high-frequency line monitor for instant doorbell call detection
    coordinator.start_line_monitor()

    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "api": client,
    }

    # Register WebRTC streaming services
    async def async_handle_start_webrtc_stream(call: ServiceCall) -> dict[str, Any]:
        """Initiate KVS session and return signed WebSocket and ICE configuration to caller."""
        device_id = call.data.get("device_id") or next(iter(coordinator.data), None)
        if not device_id:
            raise HomeAssistantError("Intercom device not found")

        params = await coordinator.async_start_preview(device_id)
        aws = params.get("aws", {})
        if not isinstance(aws, dict):
            raise HomeAssistantError("AWS credentials not available from cloud preview session")

        from .webrtc import generate_signed_wss_url, fetch_ice_servers
        # Generate unique random client_id for every viewer session to avoid KVS session collisions
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
            "ice_servers": ice_list,
            "device_id": device_id,
            "channel_name": channel_name,
            "timeout": 45,
        }

    hass.services.async_register(
        DOMAIN,
        "start_webrtc_stream",
        async_handle_start_webrtc_stream,
        supports_response=SupportsResponse.ONLY,
    )

    async def async_handle_stop_webrtc_stream(call: ServiceCall) -> dict[str, Any]:
        """Terminate preview session and release intercom line."""
        device_id = call.data.get("device_id") or next(iter(coordinator.data), None)
        if device_id:
            await coordinator.async_stop_preview(device_id)
        return {"status": "ok"}

    hass.services.async_register(
        DOMAIN,
        "stop_webrtc_stream",
        async_handle_stop_webrtc_stream,
        supports_response=SupportsResponse.OPTIONAL,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    data = hass.data[DOMAIN].get(entry.entry_id, {})
    coordinator: AcoGoDataUpdateCoordinator | None = data.get("coordinator")
    if coordinator:
        coordinator.stop_line_monitor()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, "start_webrtc_stream")
            hass.services.async_remove(DOMAIN, "stop_webrtc_stream")
    return unload_ok
