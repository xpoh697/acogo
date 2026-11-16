"""DataUpdateCoordinator for ACO GO with webhook-assisted call state management."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AcoGoApiClient, AcoGoApiError
from .const import (
    APP_MODELS,
    DOMAIN,
    EVENT_INCOMING_CALL,
    PREVIEW_COOLDOWN_DELAY,
    PREVIEW_WATCHDOG_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)


class AcoGoDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching ACO GO data from cloud with call state tracking."""

    def __init__(self, hass: HomeAssistant, api: AcoGoApiClient) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=30),
        )
        self.api = api
        self.preview_active_devices: dict[str, dict[str, Any]] = {}
        self._preview_watchdog_handles: dict[str, asyncio.TimerHandle] = {}
        self._call_reset_handles: dict[str, asyncio.TimerHandle] = {}
        self._call_latch_until: dict[str, float] = {}
        self._call_cooldown_until: dict[str, float] = {}
        self._preview_cooldown_until: dict[str, float] = {}
        self._kiosk_monitor_task: asyncio.Task[None] | None = None
        self._kiosk_prev_apps: dict[str, str] = {}
        self._kiosk_configured_hosts: set[str] = set()
        self._kiosk_usage_warned: bool = False
        # Cache for last valid real camera frame (device_id -> (timestamp, jpeg_bytes))
        self.last_valid_snapshot: dict[str, tuple[float, bytes]] = {}

    def start_kiosk_monitor(self) -> None:
        """Start fast local LAN polling of Fully Kiosk instances configured in HA."""
        if self._kiosk_monitor_task is None or self._kiosk_monitor_task.done():
            self._kiosk_monitor_task = self.hass.async_create_background_task(
                self._async_kiosk_monitor_loop(),
                "acogo_fully_kiosk_monitor",
            )

    async def _async_ensure_kiosk_settings(
        self,
        session: aiohttp.ClientSession,
        url: str,
        password: str,
        timeout: aiohttp.ClientTimeout,
        verify_ssl: bool,
    ) -> None:
        """Enable usageStatistics and setWifiWakelock when Fully Kiosk wakes up."""
        for setting_key in ("usageStatistics", "setWifiWakelock"):
            params = {
                "cmd": "setBooleanSetting",
                "key": setting_key,
                "value": "true",
                "type": "json",
                "password": password,
            }
            try:
                async with session.get(
                    url,
                    params=params,
                    timeout=timeout,
                    ssl=verify_ssl,
                ) as resp:
                    _LOGGER.debug("Set Fully Kiosk %s=true (HTTP %s)", setting_key, resp.status)
            except (aiohttp.ClientError, TimeoutError):
                pass

    async def _async_kiosk_monitor_loop(self) -> None:
        """Poll Fully Kiosk deviceInfo every 1.5s for exact pl.aco.acogo call detection."""
        session = async_get_clientsession(self.hass)
        timeout = aiohttp.ClientTimeout(total=1.5)

        while True:
            try:
                for k_entry in self.hass.config_entries.async_entries("fully_kiosk"):
                    host = k_entry.data.get("host")
                    password = k_entry.data.get("password", "")
                    if not host:
                        continue
                    scheme = "https" if k_entry.data.get("ssl") else "http"
                    verify_ssl = bool(k_entry.data.get("verify_ssl", False))
                    url = f"{scheme}://{host}:2323/"
                    params = {"cmd": "deviceInfo", "type": "json", "password": password}
                    try:
                        async with session.get(
                            url,
                            params=params,
                            timeout=timeout,
                            ssl=verify_ssl,
                        ) as resp:
                            if resp.status != 200:
                                continue
                            info = await resp.json(content_type=None)
                    except (aiohttp.ClientError, TimeoutError):
                        continue

                    if host not in self._kiosk_configured_hosts:
                        self._kiosk_configured_hosts.add(host)
                        await self._async_ensure_kiosk_settings(
                            session, url, password, timeout, verify_ssl
                        )

                    if not isinstance(info, dict):
                        continue
                    curr_app = info.get("foregroundApp") or ""
                    if not curr_app:
                        continue
                    prev_app = self._kiosk_prev_apps.get(host)
                    self._kiosk_prev_apps[host] = curr_app

                    if prev_app != "pl.aco.acogo" and curr_app == "pl.aco.acogo":
                        await self.async_handle_call_trigger(source="fully_kiosk_fast")
                    elif prev_app == "pl.aco.acogo" and curr_app != "pl.aco.acogo":
                        for dev_id in list(self._call_reset_handles):
                            self.reset_call_state(dev_id)
                    elif (
                        prev_app == "de.ozerov.fully"
                        and curr_app == "com.sec.android.app.launcher"
                        and not self._kiosk_usage_warned
                    ):
                        self._kiosk_usage_warned = True
                        _LOGGER.warning(
                            "Fully Kiosk (%s) reported com.sec.android.app.launcher; verify Android Usage Access permission is granted if acoGO! popped up",
                            host,
                        )

                await asyncio.sleep(1.5)
            except asyncio.CancelledError:
                break
            except Exception as err:
                _LOGGER.debug("Fully Kiosk fast monitor error: %s", err)
                await asyncio.sleep(3.0)

    def stop_line_monitor(self) -> None:
        """Kept for backwards compatibility; delegates to async_shutdown."""
        self.async_shutdown()

    def async_shutdown(self) -> None:
        """Cancel all active watchdog, call reset timers, and kiosk monitor task on unload."""
        if self._kiosk_monitor_task is not None and not self._kiosk_monitor_task.done():
            self._kiosk_monitor_task.cancel()
        self._kiosk_monitor_task = None

        for handle in self._preview_watchdog_handles.values():
            handle.cancel()
        self._preview_watchdog_handles.clear()

        for handle in self._call_reset_handles.values():
            handle.cancel()
        self._call_reset_handles.clear()

    async def async_start_preview(self, dev_id: str, retry_count: int = 3) -> dict[str, Any]:
        """Start live preview session for intercom device with watchdog protection and retry logic."""
        if self.is_preview_active(dev_id):
            _LOGGER.debug("Terminating previous preview session for %s before starting new", dev_id)
            await self.async_stop_preview(dev_id)

        params: dict[str, Any] = {}
        for attempt in range(retry_count):
            try:
                resp = await self.api.request_preview(dev_id, preview_type="video-only")
            except AcoGoApiError as err:
                if attempt == retry_count - 1:
                    raise HomeAssistantError(f"Ошибка включения камеры {dev_id}: {err}") from err
                await asyncio.sleep(1.0)
                continue

            candidate_params = resp.get("params") if isinstance(resp, dict) else None
            if isinstance(candidate_params, dict) and candidate_params.get("aws"):
                params = candidate_params
                break

            # If line is busy (e.g. intercom currently ringing), wait a moment and retry
            if isinstance(resp, dict) and resp.get("response") == "busy":
                _LOGGER.debug("Intercom %s busy, retrying preview request (attempt %d/%d)...", dev_id, attempt + 1, retry_count)
                if attempt < retry_count - 1:
                    await asyncio.sleep(1.0)
                    continue

        if not params:
            _LOGGER.debug("Could not obtain active preview AWS parameters for %s (intercom busy in call)", dev_id)
            return {}

        self.preview_active_devices[dev_id] = params

        # Cancel previous watchdog timer if running
        if old_handle := self._preview_watchdog_handles.pop(dev_id, None):
            old_handle.cancel()

        # Schedule automatic shutdown watchdog (45s)
        loop = self.hass.loop
        self._preview_watchdog_handles[dev_id] = loop.call_later(
            PREVIEW_WATCHDOG_TIMEOUT,
            lambda: self.hass.async_create_task(self.async_stop_preview(dev_id)),
        )

        self.async_update_listeners()
        return params

    async def async_stop_preview(self, dev_id: str) -> None:
        """End live preview session and release intercom line with cooldown protection."""
        if handle := self._preview_watchdog_handles.pop(dev_id, None):
            handle.cancel()

        # Set cooldown so monitor does not misinterpret preview teardown as incoming call
        now_ts = asyncio.get_running_loop().time()
        self._preview_cooldown_until[dev_id] = now_ts + PREVIEW_COOLDOWN_DELAY

        try:
            await self.api.end_preview()
        except Exception as err:
            _LOGGER.debug("Error ending preview for %s: %s", dev_id, err)
        finally:
            self.preview_active_devices.pop(dev_id, None)
            self.async_update_listeners()

    def is_preview_active(self, dev_id: str) -> bool:
        """Check if preview is active for device."""
        return dev_id in self.preview_active_devices

    def get_preview_params(self, dev_id: str) -> dict[str, Any]:
        """Get current preview parameters for device."""
        return self.preview_active_devices.get(dev_id, {})

    async def async_handle_call_trigger(self, device_id: str | None = None, source: str = "webhook") -> bool:
        """Handle incoming call trigger from webhook or automation with cooldown and auto-reset."""
        now_ts = asyncio.get_running_loop().time()
        data = self.data or {}
        target_dev_id = device_id if (device_id and device_id in data) else None
        if not target_dev_id:
            for dev_id, dev_data in data.items():
                model = dev_data.get("info", {}).get("model")
                if model not in APP_MODELS:
                    target_dev_id = dev_id
                    break
            if not target_dev_id:
                target_dev_id = next(iter(data), None)

        if not target_dev_id or target_dev_id not in data:
            _LOGGER.warning("Call trigger received but no intercom device found in coordinator data")
            return False

        # 10s cooldown to prevent notification chattering / duplicate push packets
        last_cooldown = self._call_cooldown_until.get(target_dev_id, 0)
        if now_ts < last_cooldown:
            _LOGGER.debug("Call trigger for %s ignored due to active cooldown", target_dev_id)
            return False

        ring_duration = 35.0
        self._call_latch_until[target_dev_id] = now_ts + ring_duration
        self._call_cooldown_until[target_dev_id] = now_ts + 10.0

        dev_name = data[target_dev_id].get("info", {}).get("name", target_dev_id)
        _LOGGER.info("Doorbell incoming call triggered via %s for %s (%s)!", source, target_dev_id, dev_name)

        data[target_dev_id]["is_ringing"] = True
        self.async_update_listeners()

        self.hass.bus.async_fire(EVENT_INCOMING_CALL, {
            "device_id": target_dev_id,
            "name": dev_name,
            "source": source,
        })

        # Automatic watchdog reset after ring_duration
        def _reset_call() -> None:
            self._call_reset_handles.pop(target_dev_id, None)
            if self.data and target_dev_id in self.data and self.data[target_dev_id].get("is_ringing"):
                if asyncio.get_running_loop().time() >= self._call_latch_until.get(target_dev_id, 0):
                    self.data[target_dev_id]["is_ringing"] = False
                    self.async_update_listeners()
                    _LOGGER.debug("Call latch expired for %s", target_dev_id)

        if old_handle := self._call_reset_handles.pop(target_dev_id, None):
            old_handle.cancel()
        self._call_reset_handles[target_dev_id] = self.hass.loop.call_later(ring_duration, _reset_call)
        return True

    def reset_call_state(self, device_id: str) -> None:
        """Immediately reset ringing state (e.g. after door opened or call answered/ended)."""
        if handle := self._call_reset_handles.pop(device_id, None):
            handle.cancel()
        self._call_latch_until[device_id] = 0.0
        if self.data and device_id in self.data and self.data[device_id].get("is_ringing"):
            self.data[device_id]["is_ringing"] = False
            self.async_update_listeners()
            _LOGGER.debug("Call state reset for %s", device_id)

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from ACO GO Cloud with graceful degradation on transient glitches."""
        try:
            devices = await self.api.get_device_list()
            devices_data: dict[str, Any] = {}
            now_ts = asyncio.get_running_loop().time()

            for dev in devices:
                dev_id = dev.get("devId")
                if not dev_id:
                    continue

                if dev.get("status") == "offline":
                    state_resp = "offline"
                else:
                    try:
                        state_resp = await self.api.check_state(dev_id)
                    except Exception as state_err:
                        _LOGGER.debug("Temporary error checking state for %s: %s", dev_id, state_err)
                        prev_state = self.data.get(dev_id, {}).get("state", "ready") if self.data else "ready"
                        state_resp = prev_state

                is_preview = self.is_preview_active(dev_id)
                is_cooldown = (
                    (now_ts < self._preview_cooldown_until.get(dev_id, 0))
                    or (now_ts < self._call_cooldown_until.get(dev_id, 0))
                )
                is_busy = (state_resp == "busy" and not is_preview and not self.api.is_device_busy(dev_id) and not is_cooldown)
                is_latched = (now_ts < self._call_latch_until.get(dev_id, 0))
                is_ringing = is_latched or is_busy

                is_offline = (dev.get("status") == "offline" or state_resp == "offline")
                devices_data[dev_id] = {
                    "info": dev,
                    "state": "offline" if is_offline else state_resp,  # 'ready', 'busy', 'offline'
                    "is_online": not is_offline,
                    "is_ringing": False if is_offline else is_ringing,
                }

            return devices_data
        except AcoGoApiError as err:
            if self.data:
                _LOGGER.warning("Temporary error updating ACO GO data (maintaining cached state): %s", err)
                return self.data
            raise UpdateFailed(f"Error communicating with ACO API: {err}") from err
