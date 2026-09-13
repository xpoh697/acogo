"""Async API Client for ACO GO Cloud."""
from __future__ import annotations

import asyncio
import logging
from typing import Any
import uuid

import aiohttp

from .const import (
    BASE_URL,
    DOOR_CALL_DELAY,
    DOOR_HOLD_DELAY,
    ORDER_END_CALL,
    ORDER_EZ_OPEN,
    ORDER_F2_OPEN,
    ORDER_RECEIVE_CALL,
)

_LOGGER = logging.getLogger(__name__)


class AcoGoAuthError(Exception):
    """Authentication failure."""


class AcoGoApiError(Exception):
    """General API communication error."""


class AcoGoApiClient:
    """Client for https://api.aco.com.pl/listener/v1."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        dev_id: str,
        device_password: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self.session = session
        self.dev_id = dev_id
        self.device_password = device_password
        self.username = username
        self.password = password
        self._device_locks: dict[str, asyncio.Lock] = {}

    def get_device_lock(self, device_id: str) -> asyncio.Lock:
        """Get or create an asyncio lock for a specific intercom device."""
        if device_id not in self._device_locks:
            self._device_locks[device_id] = asyncio.Lock()
        return self._device_locks[device_id]

    async def register_device(self, username: str | None = None, password: str | None = None) -> str:
        """Register client device and obtain devicePassword."""
        user = username or self.username
        pwd = password or self.password
        if not user or not pwd:
            raise AcoGoAuthError("Username and password are required for registration")

        url = f"{BASE_URL}/device"
        headers = {
            "Accept": "text/plain, */*",
            "Content-Type": "application/json",
            "devId": self.dev_id,
            "userName": user,
            "userPassword": pwd,
        }
        payload = {
            "language": "en",
            "name": "Home Assistant acoGO",
            "localization": "",
            "firmware": "HA",
            "software": "1.0.0",
            "hardware": "HomeAssistant",
            "fcmToken": "",
            "model": 62,
        }

        try:
            async with self.session.post(url, json=payload, headers=headers, timeout=15) as resp:
                if resp.status == 401:
                    raise AcoGoAuthError("Invalid credentials")
                if resp.status not in (200, 201):
                    text = await resp.text()
                    raise AcoGoApiError(f"Registration failed ({resp.status}): {text}")
                data = await resp.json()
        except aiohttp.ClientError as err:
            raise AcoGoApiError(f"Network error during registration: {err}") from err

        add_info = data.get("additionalInfo") or {}
        dev_pwd = add_info.get("devicePassword")
        if not dev_pwd:
            raise AcoGoAuthError("No devicePassword returned by server")

        self.device_password = dev_pwd
        self.username = user
        self.password = pwd
        return dev_pwd

    def _get_headers(self) -> dict[str, str]:
        if not self.device_password:
            raise AcoGoAuthError("Missing devicePassword. Must authenticate first.")
        return {
            "Accept": "text/plain, */*",
            "Content-Type": "application/json",
            "devId": self.dev_id,
            "devicePassword": self.device_password,
        }

    async def _request(self, method: str, path: str, json: Any = None) -> Any:
        url = f"{BASE_URL}{path}"
        try:
            async with self.session.request(method, url, json=json, headers=self._get_headers(), timeout=15) as resp:
                if resp.status == 401 and self.username and self.password:
                    _LOGGER.warning("ACO GO token expired (401). Attempting re-authentication...")
                    await self.register_device()
                    async with self.session.request(method, url, json=json, headers=self._get_headers(), timeout=15) as retry_resp:
                        if retry_resp.status != 200:
                            raise AcoGoApiError(f"Request failed after re-auth: {retry_resp.status}")
                        return await retry_resp.json()

                if resp.status != 200:
                    text = await resp.text()
                    raise AcoGoApiError(f"API error {resp.status} on {path}: {text}")
                return await resp.json()
        except aiohttp.ClientError as err:
            raise AcoGoApiError(f"Connection error to {url}: {err}") from err

    async def get_device_list(self) -> list[dict[str, Any]]:
        """Fetch list of user devices."""
        data = await self._request("GET", "/device-by-app")
        if isinstance(data, list):
            # Filter out mobile apps (model 62 and 63)
            return [d for d in data if d.get("model") not in (62, 63)]
        return []

    async def check_state(self, device_id: str) -> str:
        """Check status of intercom line ('ready', 'busy', 'offline')."""
        res = await self._request("POST", "/device/check-state", json={"devId": device_id})
        return res.get("response", "offline")

    async def send_order(self, target_id: str, order_id: str) -> bool:
        """Send command to intercom."""
        res = await self._request(
            "POST",
            f"/order?orderId={order_id}",
            json={"address": None, "targetId": target_id},
        )
        return bool(res)

    async def open_door_sequence(self, target_id: str, is_gate: bool = False) -> bool:
        """Safely execute door/gate unlock with device locking and guaranteed endCall."""
        lock = self.get_device_lock(target_id)
        order_cmd = ORDER_F2_OPEN if is_gate else ORDER_EZ_OPEN

        async with lock:
            state = await self.check_state(target_id)
            is_active_call = (state == "busy")

            if is_active_call:
                # Direct unlock during active call
                return await self.send_order(target_id, order_cmd)

            # Idle sequence: receiveCall -> wait 3s -> open -> wait 5s -> endCall
            try:
                await self.send_order(target_id, ORDER_RECEIVE_CALL)
                await asyncio.sleep(DOOR_CALL_DELAY)
                await self.send_order(target_id, order_cmd)
                await asyncio.sleep(DOOR_HOLD_DELAY)
                return True
            finally:
                # Guaranteed line release even on cancellation or error
                try:
                    await self.send_order(target_id, ORDER_END_CALL)
                except Exception as err:
                    _LOGGER.error("Failed to send endCall for %s: %s", target_id, err)

    async def switch_video(self, target_id: str) -> bool:
        """Switch camera video input on intercom."""
        res = await self._request("POST", "/order/video-sw", json={"targetId": target_id})
        return bool(res)

    async def request_preview(self, device_id: str) -> dict[str, Any]:
        """Request live WebRTC/Kinesis video preview session."""
        return await self._request("POST", "/preview/request", json={"devId": device_id, "previewType": "video-only"})

    async def end_preview(self) -> bool:
        """Terminate video preview session."""
        res = await self._request("POST", "/preview/end", json=None)
        return bool(res)
