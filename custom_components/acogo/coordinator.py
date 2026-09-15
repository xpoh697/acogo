"""DataUpdateCoordinator for ACO GO with fast line monitoring for instant call detection."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AcoGoApiClient, AcoGoApiError
from .const import (
    APP_MODELS,
    CALL_LATCH_DURATION,
    DOMAIN,
    FAST_POLL_INTERVAL,
    PREVIEW_COOLDOWN_DELAY,
    PREVIEW_WATCHDOG_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)


class AcoGoDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching ACO GO data from cloud with fast line monitoring."""

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
        self._call_latch_until: dict[str, float] = {}
        self._preview_cooldown_until: dict[str, float] = {}
        self._bus_failure_start: dict[str, float] = {}
        self._line_monitor_task: asyncio.Task | None = None
        # Cache for last valid real camera frame (device_id -> (timestamp, jpeg_bytes))
        self.last_valid_snapshot: dict[str, tuple[float, bytes]] = {}

    def start_line_monitor(self) -> None:
        """Start the high-frequency line state monitor task if not already running."""
        if self._line_monitor_task is None or self._line_monitor_task.done():
            if hasattr(self.hass, "async_create_background_task"):
                self._line_monitor_task = self.hass.async_create_background_task(
                    self._async_line_monitor_loop(), "acogo_line_monitor"
                )
            else:
                self._line_monitor_task = self.hass.loop.create_task(self._async_line_monitor_loop())

    def stop_line_monitor(self) -> None:
        """Stop line state monitor task."""
        if self._line_monitor_task and not self._line_monitor_task.done():
            self._line_monitor_task.cancel()
            self._line_monitor_task = None

    async def _async_line_monitor_loop(self) -> None:
        """High-frequency line state monitor for instant doorbell call detection."""
        _LOGGER.debug("Starting high-frequency line monitor loop for acoGO")
        try:
            while True:
                await asyncio.sleep(FAST_POLL_INTERVAL)
                if not self.data:
                    continue

                now_ts = asyncio.get_running_loop().time()
                state_changed = False

                for dev_id, dev_entry in list(self.data.items()):
                    model = dev_entry.get("info", {}).get("model")
                    if model in APP_MODELS or self.api.is_device_busy(dev_id):
                        continue

                    try:
                        state_resp = await self.api.check_state(dev_id)
                    except Exception as err:
                        _LOGGER.debug("Line monitor error checking state for %s: %s", dev_id, err)
                        state_resp = "ready"

                    is_preview = self.is_preview_active(dev_id)
                    is_cooldown = (now_ts < self._preview_cooldown_until.get(dev_id, 0))

                    # Line is considered in active call when cloud signals busy
                    # and neither preview session nor post-preview cooldown is active
                    is_busy = (state_resp == "busy" and not is_preview and not is_cooldown)

                    # Offline watchdog & ringing latch
                    if is_busy:
                        if dev_id not in self._bus_failure_start:
                            self._bus_failure_start[dev_id] = now_ts
                        fail_duration = now_ts - self._bus_failure_start[dev_id]

                        # If bus has been unreachable for > 45 seconds continuously, it is OFFLINE, not ringing
                        if fail_duration > 45.0:
                            is_busy = False
                            is_ringing = False
                            is_online = False
                            state_resp = "offline"
                        else:
                            is_online = True
                            # Call confirmed: latch for at least CALL_LATCH_DURATION (25s)
                            if not dev_entry.get("is_ringing") and fail_duration <= CALL_LATCH_DURATION:
                                self._call_latch_until[dev_id] = now_ts + CALL_LATCH_DURATION
                                dev_name = dev_entry.get("info", {}).get("name", dev_id)
                                _LOGGER.info("Doorbell ringing on intercom %s (%s)!", dev_id, dev_name)
                                self.hass.bus.async_fire("acogo_incoming_call", {
                                    "device_id": dev_id,
                                    "name": dev_name,
                                })
                            is_ringing = (now_ts < self._call_latch_until.get(dev_id, 0))
                    else:
                        self._bus_failure_start.pop(dev_id, None)
                        is_online = (state_resp != "offline")
                        is_ringing = (now_ts < self._call_latch_until.get(dev_id, 0))

                    prev_ringing = dev_entry.get("is_ringing", False)
                    prev_state = dev_entry.get("state")

                    if is_ringing != prev_ringing or state_resp != prev_state or is_online != dev_entry.get("is_online"):
                        dev_entry["state"] = state_resp
                        dev_entry["is_online"] = is_online
                        dev_entry["is_ringing"] = is_ringing
                        state_changed = True

                if state_changed:
                    self.async_update_listeners()
        except asyncio.CancelledError:
            _LOGGER.debug("Line monitor loop cancelled cleanly")
        except Exception as err:
            _LOGGER.warning("Unexpected error in line monitor loop: %s", err)

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
        if dev_id in self._preview_watchdog_handles:
            self._preview_watchdog_handles[dev_id].cancel()

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
        if dev_id in self._preview_watchdog_handles:
            self._preview_watchdog_handles[dev_id].cancel()
            self._preview_watchdog_handles.pop(dev_id, None)

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

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from ACO GO Cloud with graceful degradation on transient glitches."""
        try:
            devices = await self.api.get_device_list()
            devices_data: dict[str, Any] = {}
            now_ts = asyncio.get_running_loop().time()

            for dev in devices:
                dev_id = dev["devId"]
                try:
                    state_resp = await self.api.check_state(dev_id)
                except Exception as state_err:
                    _LOGGER.debug("Temporary error checking state for %s: %s", dev_id, state_err)
                    prev_state = self.data.get(dev_id, {}).get("state", "ready") if self.data else "ready"
                    state_resp = prev_state

                is_preview = self.is_preview_active(dev_id)
                is_cooldown = (now_ts < self._preview_cooldown_until.get(dev_id, 0))
                is_busy = (state_resp == "busy" and not is_preview and not self.api.is_device_busy(dev_id) and not is_cooldown)
                is_latched = (now_ts < self._call_latch_until.get(dev_id, 0))
                is_ringing = is_latched or is_busy

                devices_data[dev_id] = {
                    "info": dev,
                    "state": state_resp,  # 'ready', 'busy', 'offline'
                    "is_online": (state_resp != "offline"),
                    "is_ringing": is_ringing,
                }

            return devices_data
        except AcoGoApiError as err:
            if self.data:
                _LOGGER.warning("Temporary error updating ACO GO data (maintaining cached state): %s", err)
                return self.data
            raise UpdateFailed(f"Error communicating with ACO API: {err}") from err
