"""Camera entity for ACO GO video preview and snapshots."""
from __future__ import annotations

import asyncio
import io
import logging
import unicodedata
from datetime import datetime
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AcoGoDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

# Valid minimal 16x16 dark JPEG raw bytes fallback
FALLBACK_JPEG_BYTES = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00\x10\x0b\x0c"
    b"\x0e\x0c\n\x10\x0e\r\x0e\x12\x11\x10\x13\x18(\x1a\x18\x16\x16\x181#%\x1d(:3=<9387@H\\N@DWE78PmQW_bghg"
    b">Mqypdx\\egc\xff\xdb\x00C\x01\x11\x12\x12\x18\x15\x18/\x1a\x1a/cB8Bcccccccccccccccccccccccccccccccccc"
    b"cccccccccccccccc\xff\xc0\x00\x11\x08\x00\x10\x00\x10\x03\x01\"\x00\x02\x11\x01\x03\x11\x01\xff\xc4\x00"
    b"\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07"
    b"\x08\t\n\x0b\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04\x04\x00\x00\x01}\x01\x02"
    b"\x03\x00\x04\x11\x05\x12!1A\x06\x13Qa\x07\"q\x142\x81\x91\xa1\x08#B\xb1\xc1\x15R\xd1\xf0$3br\x82\t\n"
    b"\x16\x17\x18\x19\x1a%&'()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x83\x84\x85\x86\x87\x88\x89\x8a\x92"
    b"\x93\x94\x95\x96\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9"
    b"\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6"
    b"\xe7\xe8\xe9\xea\xf1\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xc4\x00\x1f\x01\x00\x03\x01\x01\x01\x01"
    b"\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xc4\x00\xb5"
    b"\x11\x00\x02\x01\x02\x04\x04\x03\x04\x07\x05\x04\x04\x00\x01\x02w\x00\x01\x02\x03\x11\x04\x05!1\x06"
    b"\x12AQ\x07aq\x13\"2\x81\x08\x14B\x91\xa1\xb1\xc1\t#3R\xf0\x15br\xd1\n\x16$4\xe1%\xf1\x17\x18\x19\x1a"
    b"&'()*56789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz\x82\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96"
    b"\x97\x98\x99\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7\xb8\xb9\xba\xc2\xc3\xc4"
    b"\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5\xd6\xd7\xd8\xd9\xda\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf2"
    b"\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00?\x00\xe2\xe8\xa2\x8a"
    b"\xb1\x1f\xff\xd9"
)


def _sanitize_ascii(text: str) -> str:
    """Normalize unicode and filter strictly to printable ASCII."""
    replacements = {"ł": "l", "Ł": "L", "—": "-", "–": "-"}
    for k, v in replacements.items():
        text = text.replace(k, v)
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_chars = [c for c in nfkd if not unicodedata.combining(c)]
    clean_str = "".join(ascii_chars)
    return "".join(c for c in clean_str if 32 <= ord(c) < 127)


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
    """Representation of an ACO GO Intercom Camera with native snapshot and preview controls."""

    _attr_has_entity_name = True
    _attr_name = "Camera"
    _attr_supported_features = CameraEntityFeature.ON_OFF

    def __init__(self, coordinator: AcoGoDataUpdateCoordinator, dev_id: str) -> None:
        """Initialize camera entity."""
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self.dev_id = dev_id
        self._attr_unique_id = f"{dev_id}_camera"
        self._last_image: bytes | None = None
        self._bg_task: asyncio.Task | None = None

    @property
    def is_on(self) -> bool:
        """Return true if camera entity is available."""
        return True

    @property
    def is_streaming(self) -> bool:
        """Return False so Home Assistant more-info modal always renders the full image view container."""
        return False

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
        """Additional camera status attributes (excluding sensitive AWS keys)."""
        info = self.coordinator.data.get(self.dev_id, {}).get("info", {})
        status = self.coordinator.data.get(self.dev_id, {}).get("state", "unknown")
        is_ringing = self.coordinator.data.get(self.dev_id, {}).get("ringing", False)
        is_streaming = self.coordinator.is_preview_active(self.dev_id)

        attrs: dict[str, Any] = {
            "intercom_status": status,
            "is_ringing": is_ringing,
            "is_streaming": is_streaming,
            "preview_active": is_streaming,
            "stream_type": "webrtc_kvs",
            "has_live_image": self._last_image is not None,
            "model": info.get("model"),
            "firmware": info.get("firmware"),
            "software": info.get("software"),
        }
        params = self.coordinator.get_preview_params(self.dev_id)
        if params:
            attrs["call_id"] = params.get("callId")
            aws_info = params.get("aws", {})
            if isinstance(aws_info, dict):
                attrs["channel_arn"] = aws_info.get("channel arn") or aws_info.get("channelARN")
                attrs["channel_name"] = aws_info.get("channel name")
                attrs["aws_region"] = aws_info.get("region")
                attrs["wss_endpoint"] = aws_info.get("wss endpoint")
        return attrs

    def _handle_coordinator_update(self) -> None:
        """Respond to coordinator state changes and launch background capture if stream is active."""
        super()._handle_coordinator_update()
        is_streaming = self.coordinator.is_preview_active(self.dev_id)
        is_ringing = self.coordinator.data.get(self.dev_id, {}).get("ringing", False)
        if (is_streaming or is_ringing) and (self._bg_task is None or self._bg_task.done()):
            self._bg_task = self.hass.async_create_task(self._async_background_capture_loop())
        elif not is_streaming and not is_ringing and self._bg_task and not self._bg_task.done():
            self._bg_task.cancel()
            self._bg_task = None

    async def _async_background_capture_loop(self) -> None:
        """Background worker that captures real frames over WebRTC with fail-fast timeout protection."""
        max_attempts = 2
        attempt = 0
        try:
            while self.coordinator.is_preview_active(self.dev_id) or self.coordinator.data.get(self.dev_id, {}).get("ringing", False):
                attempt += 1
                if attempt > max_attempts:
                    _LOGGER.info(
                        "WebRTC capture reached limit (%s attempts) without video feed for %s. Auto-releasing session.",
                        max_attempts,
                        self.dev_id,
                    )
                    info = self.coordinator.data.get(self.dev_id, {}).get("info", {})
                    panel_name = info.get("name", f"acoGO {self.dev_id}")
                    timeout_card = await self.hass.async_add_executor_job(
                        self._generate_camera_card,
                        panel_name,
                        "timeout",
                        False,
                        False,
                        "TIMEOUT (NO VIDEO FEED)",
                        "Intercom did not send video feed within 25s | Session auto-closed",
                    )
                    self._last_image = timeout_card
                    self.async_write_ha_state()
                    # Automatically turn off preview to reset switch and release intercom line
                    await self.coordinator.async_stop_preview(self.dev_id)
                    break

                params = self.coordinator.get_preview_params(self.dev_id)
                if params and isinstance(params.get("aws"), dict):
                    from .webrtc import async_capture_webrtc_snapshot
                    session = async_get_clientsession(self.hass)
                    frame_bytes = await async_capture_webrtc_snapshot(
                        session=session,
                        aws=params["aws"],
                        timeout=11.0,
                    )
                    if frame_bytes:
                        _LOGGER.info("Successfully received camera frame from acoGO WebRTC")
                        self._last_image = frame_bytes
                        self.async_write_ha_state()
                        await asyncio.sleep(1.0)
                        continue
                await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            pass
        except Exception as err:
            _LOGGER.debug("Background capture loop error: %s", err)
        finally:
            self._bg_task = None

    async def async_turn_on(self) -> None:
        """Turn on camera live preview stream."""
        await self.coordinator.async_start_preview(self.dev_id)
        if self._bg_task is None or self._bg_task.done():
            self._bg_task = self.hass.async_create_task(self._async_background_capture_loop())

    async def async_turn_off(self) -> None:
        """Turn off camera live preview stream."""
        if self._bg_task and not self._bg_task.done():
            self._bg_task.cancel()
            self._bg_task = None
        await self.coordinator.async_stop_preview(self.dev_id)

    def _get_font(self, size: int) -> Any:
        """Safely load font with size fallback for all Pillow versions."""
        from PIL import ImageFont
        try:
            return ImageFont.load_default(size=size)
        except Exception:
            return ImageFont.load_default()

    def _get_text_width(self, draw: Any, text: str, font: Any) -> int:
        """Safely calculate text width across Pillow versions."""
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            return int(bbox[2] - bbox[0])
        except Exception:
            return len(text) * 12

    def _generate_camera_card(
        self,
        name: str,
        status: str,
        is_ringing: bool,
        is_streaming: bool,
        custom_status: str | None = None,
        custom_sub: str | None = None,
    ) -> bytes:
        """Render informative camera status card synchronously with clean typography."""
        try:
            from PIL import Image, ImageDraw

            width, height = 1280, 720
            img = Image.new("RGB", (width, height), color=(18, 24, 38))
            draw = ImageDraw.Draw(img)

            font_title = self._get_font(24)
            font_status = self._get_font(34)
            font_sub = self._get_font(20)
            font_small = self._get_font(18)

            draw.rectangle([0, 0, width, 64], fill=(10, 14, 22))
            clean_name = _sanitize_ascii(name.upper())
            title_text = f"ACO INTERCOM - {clean_name}"
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            draw.text((32, 20), title_text, fill=(255, 255, 255), font=font_title)
            draw.text((width - 270, 20), now_str, fill=(180, 190, 205), font=font_title)

            cx, cy = width // 2, height // 2 - 30
            if custom_status:
                lens_outer = (58, 30, 20)
                lens_ring = (255, 120, 50)
                lens_center = (255, 130, 60)
            elif is_ringing:
                lens_outer = (58, 24, 24)
                lens_ring = (255, 75, 75)
                lens_center = (255, 80, 80)
            elif is_streaming:
                lens_outer = (58, 48, 18)
                lens_ring = (255, 170, 0)
                lens_center = (255, 190, 20)
            else:
                lens_outer = (28, 38, 58)
                lens_ring = (0, 210, 255)
                lens_center = (0, 210, 255)

            draw.ellipse([cx - 110, cy - 110, cx + 110, cy + 110], fill=lens_outer, outline=(45, 156, 219), width=3)
            draw.ellipse([cx - 70, cy - 70, cx + 70, cy + 70], fill=(15, 20, 32), outline=lens_ring, width=2)
            draw.ellipse([cx - 24, cy - 24, cx + 24, cy + 24], fill=lens_center)

            if custom_status:
                status_text = custom_status
                status_color = (255, 120, 50)
                sub_text = custom_sub or "WebRTC session auto-released"
            elif is_ringing:
                status_text = "INCOMING CALL (RINGING)"
                status_color = (255, 75, 75)
                sub_text = "Call in Progress | Doorbell is Ringing"
            elif is_streaming:
                status_text = "CONNECTING (STARTING PREVIEW)"
                status_color = (255, 170, 0)
                sub_text = "Negotiating WebRTC stream with intercom | Please wait..."
            elif status == "ready":
                status_text = "LINE READY (STANDBY)"
                status_color = (46, 204, 113)
                sub_text = "Standby | Use 'Camera Stream' switch to start preview"
            else:
                status_text = "STANDBY / IDLE"
                status_color = (160, 170, 185)
                sub_text = "acoGO! 2.0 Cloud Connected"

            st_width = self._get_text_width(draw, status_text, font_status)
            draw.text((cx - st_width // 2, cy + 140), status_text, fill=status_color, font=font_status)

            sub_width = self._get_text_width(draw, sub_text, font_sub)
            draw.text((cx - sub_width // 2, cy + 190), sub_text, fill=(140, 160, 185), font=font_sub)

            draw.rectangle([0, height - 54, width, height], fill=(10, 14, 22))
            draw.text((32, height - 38), "acoGO! Home Assistant Integration", fill=(100, 120, 145), font=font_small)
            if custom_status:
                footer_right = "Stream: TIMEOUT"
                footer_color = (255, 120, 50)
            elif is_streaming:
                footer_right = "Stream: CONNECTING"
                footer_color = (255, 170, 0)
            else:
                footer_right = "Door Lock: Ready | Gate: Ready"
                footer_color = (140, 160, 185)
            draw.text((width - 320, height - 38), footer_right, fill=footer_color, font=font_small)

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
        """Return real WebRTC video frame or informative status card immediately."""
        info = self.coordinator.data.get(self.dev_id, {}).get("info", {})
        status = self.coordinator.data.get(self.dev_id, {}).get("state", "unknown")
        is_ringing = self.coordinator.data.get(self.dev_id, {}).get("ringing", False)
        is_streaming = self.coordinator.is_preview_active(self.dev_id)
        panel_name = info.get("name", f"acoGO {self.dev_id}")

        # If we have captured real camera frame and preview or ringing is active, return it immediately!
        if self._last_image and (is_streaming or is_ringing):
            return self._last_image

        # Start background capture worker if needed
        if (is_streaming or is_ringing) and (self._bg_task is None or self._bg_task.done()):
            self._bg_task = self.hass.async_create_task(self._async_background_capture_loop())

        # Return generated informative status card
        try:
            image_bytes = await self.hass.async_add_executor_job(
                self._generate_camera_card,
                panel_name,
                status,
                is_ringing,
                is_streaming,
            )
            if image_bytes:
                return image_bytes
        except Exception as err:
            _LOGGER.warning("Error generating camera image: %s", err)

        return self._last_image or FALLBACK_JPEG_BYTES
