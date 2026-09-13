"""Camera entity for ACO GO video preview and snapshots."""
from __future__ import annotations

import asyncio
import base64
import io
import logging
from datetime import datetime
from typing import Any

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, PREVIEW_AUTO_CLOSE_TIMEOUT
from .coordinator import AcoGoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Fallback 320x180 JPEG if Pillow is unavailable
_FALLBACK_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAA0JCgsKCA0LCgsODg0PEyAVExISEyccHhcgLikxMC4pLSwzOko+MzZGNywtQFdBRkxOUlNS"
    "Mj5aYVpQYEpRUk//2wBDAQ4ODhMREyYVFSZPNS01T09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT09PT0//wAAR"
    "CAC0AUADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1Fh"
    "ByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKT"
    "lJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAA"
    "AAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygp"
    "KjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK"
    "0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDz+iiitSQooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAo"
    "oooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAo"
    "oooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAo"
    "oooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAo"
    "oooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAo"
    "oooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKAP/9k="
)
FALLBACK_JPEG_BYTES = base64.b64decode(_FALLBACK_JPEG_B64)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ACO GO camera entities."""
    coordinator: AcoGoDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities: list[Camera] = []

    for dev_id in coordinator.data:
        entities.append(AcoGoCamera(coordinator, dev_id))

    async_add_entities(entities)


class AcoGoCamera(CoordinatorEntity[AcoGoDataUpdateCoordinator], Camera):
    """Representation of an ACO GO Intercom Camera."""

    _attr_has_entity_name = True
    _attr_name = "Camera"

    def __init__(self, coordinator: AcoGoDataUpdateCoordinator, dev_id: str) -> None:
        """Initialize camera entity."""
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self.dev_id = dev_id
        self._attr_unique_id = f"{dev_id}_camera"
        self._auto_close_task: asyncio.TimerHandle | None = None
        self._preview_active = False
        self._stream_params: dict[str, Any] = {}

    @property
    def is_on(self) -> bool:
        """Return true if camera is active."""
        return True

    @property
    def is_streaming(self) -> bool:
        """Return true if preview stream is currently requested."""
        return self._preview_active

    @property
    def device_info(self) -> dict[str, Any]:
        """Device information for Home Assistant."""
        info = self.coordinator.data.get(self.dev_id, {}).get("info", {})
        return {
            "identifiers": {(DOMAIN, self.dev_id)},
            "name": info.get("name", f"ACO Intercom {self.dev_id}"),
            "manufacturer": "ACO",
            "model": f"acoGO! (Model {info.get('model', 'Intercom')})",
            "sw_version": info.get("software"),
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Additional camera status attributes."""
        info = self.coordinator.data.get(self.dev_id, {}).get("info", {})
        status = self.coordinator.data.get(self.dev_id, {}).get("state", "unknown")
        is_ringing = self.coordinator.data.get(self.dev_id, {}).get("ringing", False)
        attrs: dict[str, Any] = {
            "intercom_status": status,
            "is_ringing": is_ringing,
            "preview_active": self._preview_active,
            "stream_type": "webrtc_kvs",
            "model": info.get("model"),
            "firmware": info.get("firmware"),
            "software": info.get("software"),
        }
        if self._stream_params:
            attrs["channel_arn"] = self._stream_params.get("channelARN")
            attrs["call_id"] = self._stream_params.get("callId")
            aws_info = self._stream_params.get("aws", {})
            if isinstance(aws_info, dict):
                attrs["aws_region"] = aws_info.get("region")
        return attrs

    def _generate_camera_card(
        self,
        name: str,
        status: str,
        is_ringing: bool,
    ) -> bytes:
        """Render intercom live status snapshot card synchronously."""
        try:
            from PIL import Image, ImageDraw

            width, height = 1280, 720
            img = Image.new("RGB", (width, height), color=(18, 24, 38))
            draw = ImageDraw.Draw(img)

            # Top header bar
            draw.rectangle([0, 0, width, 64], fill=(10, 14, 22))
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            draw.text((32, 22), f"ACO DOMOFON — {name.upper()}", fill=(255, 255, 255))
            draw.text((width - 240, 22), now_str, fill=(180, 190, 205))

            # Camera lens graphic
            cx, cy = width // 2, height // 2 - 30
            draw.ellipse([cx - 100, cy - 100, cx + 100, cy + 100], fill=(28, 38, 58), outline=(45, 156, 219), width=3)
            draw.ellipse([cx - 60, cy - 60, cx + 60, cy + 60], fill=(15, 20, 32), outline=(0, 210, 255), width=2)
            draw.ellipse([cx - 20, cy - 20, cx + 20, cy + 20], fill=(0, 210, 255))

            # Status banner
            if is_ringing:
                status_text = "ВХОДЯЩИЙ ВЫЗОВ (ЗВОНОК)"
                status_color = (255, 75, 75)
            elif status == "ready":
                status_text = "ЛИНИЯ ГОТОВА (ОНЛАЙН)"
                status_color = (46, 204, 113)
            else:
                status_text = "ПАНЕЛЬ ОЖИДАЕТ ВЫЗОВА / ОФФЛАЙН"
                status_color = (160, 170, 185)

            draw.text((cx - 130, cy + 130), status_text, fill=status_color)
            draw.text((cx - 150, cy + 165), "AWS Kinesis Video Streams WebRTC | acoGO! 2.0", fill=(140, 160, 185))

            # Bottom info bar
            draw.rectangle([0, height - 54, width, height], fill=(10, 14, 22))
            draw.text((32, height - 36), "acoGO! Home Assistant Integration", fill=(100, 120, 145))
            draw.text((width - 320, height - 36), "Электрозамок: Готов | Ворота: Готовы", fill=(140, 160, 185))

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            return buf.getvalue()
        except Exception as err:
            _LOGGER.debug("Pillow card rendering error: %s", err)
            return FALLBACK_JPEG_BYTES

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        """Return snapshot bytes and manage cloud preview session."""
        try:
            if not self._preview_active:
                resp = await self.coordinator.api.request_preview(self.dev_id)
                if isinstance(resp, dict):
                    params = resp.get("params", {})
                    if isinstance(params, dict):
                        self._stream_params = params
                self._preview_active = True

            # Reset auto-close timer to prevent keeping intercom busy
            if self._auto_close_task:
                self._auto_close_task.cancel()

            loop = asyncio.get_running_loop()
            self._auto_close_task = loop.call_later(
                PREVIEW_AUTO_CLOSE_TIMEOUT,
                lambda: asyncio.create_task(self._async_close_preview()),
            )
        except Exception as err:
            _LOGGER.debug("Preview request error for %s: %s", self.dev_id, err)

        # Generate status card in executor (non-blocking)
        info = self.coordinator.data.get(self.dev_id, {}).get("info", {})
        status = self.coordinator.data.get(self.dev_id, {}).get("state", "unknown")
        is_ringing = self.coordinator.data.get(self.dev_id, {}).get("ringing", False)
        panel_name = info.get("name", f"acoGO {self.dev_id}")

        try:
            image_bytes = await self.hass.async_add_executor_job(
                self._generate_camera_card,
                panel_name,
                status,
                is_ringing,
            )
            if image_bytes:
                return image_bytes
        except Exception as err:
            _LOGGER.warning("Error generating camera image: %s", err)

        return FALLBACK_JPEG_BYTES

    async def _async_close_preview(self) -> None:
        """Ensure preview session is closed."""
        if self._preview_active:
            try:
                await self.coordinator.api.end_preview()
            except Exception as err:
                _LOGGER.debug("Error ending preview: %s", err)
            finally:
                self._preview_active = False

    async def async_will_remove_from_hass(self) -> None:
        """Cleanup on entity removal."""
        if self._auto_close_task:
            self._auto_close_task.cancel()
        await self._async_close_preview()
        await super().async_will_remove_from_hass()
