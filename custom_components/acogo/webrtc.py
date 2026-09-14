"""WebRTC frame capture helper for acoGO! AWS Kinesis Video Streams."""
from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import hmac
import io
import ipaddress
import json
import logging
import os
import re
import unicodedata
import urllib.parse
from typing import Any

_LOGGER = logging.getLogger(__name__)


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _get_signature_key(key: str, date_stamp: str, region_name: str, service_name: str) -> bytes:
    k_date = _sign(("AWS4" + key).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region_name)
    k_service = _sign(k_region, service_name)
    k_signing = _sign(k_service, "aws4_request")
    return k_signing


def generate_signed_wss_url(aws: dict[str, Any], client_id: str) -> str:
    """Generate an AWS SigV4 signed WebSocket URL for KVS signaling."""
    access_key = aws.get("access key ID", "")
    secret_key = aws.get("secret access key ID", "")
    session_token = aws.get("session token")
    region = aws.get("region", "eu-west-2")
    channel_arn = aws.get("channel arn") or aws.get("channelARN", "")
    wss_endpoint = aws.get("wss endpoint", "")

    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    service = "kinesisvideo"

    host = urllib.parse.urlparse(wss_endpoint).netloc

    query_params: dict[str, str] = {
        "X-Amz-ChannelARN": channel_arn,
        "X-Amz-ClientId": client_id,
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{access_key}/{date_stamp}/{region}/{service}/aws4_request",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": "299",
        "X-Amz-SignedHeaders": "host",
    }
    if session_token:
        query_params["X-Amz-Security-Token"] = session_token

    canonical_querystr = "&".join(
        f"{k}={urllib.parse.quote(str(query_params[k]), safe='')}"
        for k in sorted(query_params.keys())
    )
    canonical_headers = f"host:{host}\n"
    signed_headers = "host"
    payload_hash = hashlib.sha256(b"").hexdigest()

    canonical_request = f"GET\n/\n{canonical_querystr}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"

    signing_key = _get_signature_key(secret_key, date_stamp, region, service)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    return f"{wss_endpoint}/?{canonical_querystr}&X-Amz-Signature={signature}"


async def fetch_ice_servers(session: Any, aws: dict[str, Any]) -> list[Any]:
    """Fetch dynamic AWS KVS STUN/TURN server configurations via SigV4."""
    from aiortc import RTCIceServer

    access_key = aws.get("access key ID", "")
    secret_key = aws.get("secret access key ID", "")
    session_token = aws.get("session token")
    region = aws.get("region", "eu-west-2")
    channel_arn = aws.get("channel arn") or aws.get("channelARN", "")
    https_endpoint = aws.get("https endpoint", "")

    fallback_servers = [
        RTCIceServer(urls=[f"stun:stun.kinesisvideo.{region}.amazonaws.com:443"])
    ]

    if not https_endpoint or not channel_arn or not access_key or not secret_key:
        return fallback_servers

    try:
        now = datetime.datetime.now(datetime.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        service = "kinesisvideo"

        host = urllib.parse.urlparse(https_endpoint).netloc
        path = "/v1/get-ice-server-config"
        payload = json.dumps({"ChannelARN": channel_arn}).encode("utf-8")
        payload_hash = hashlib.sha256(payload).hexdigest()

        headers: dict[str, str] = {
            "content-type": "application/json",
            "host": host,
            "x-amz-date": amz_date,
        }
        if session_token:
            headers["x-amz-security-token"] = session_token

        signed_headers = ";".join(sorted(headers.keys()))
        canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers.keys()))
        canonical_request = f"POST\n{path}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
        credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
        string_to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"

        signing_key = _get_signature_key(secret_key, date_stamp, region, service)
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        headers["Authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, SignedHeaders={signed_headers}, Signature={signature}"

        import aiohttp
        async with session.post(
            f"{https_endpoint}{path}",
            data=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=3.0),
        ) as r:
            if r.status == 200:
                data = await r.json()
                ice_servers = list(fallback_servers)
                for s in data.get("IceServerList", []):
                    valid_uris = [u for u in s.get("Uris", []) if not u.startswith("turns:")]
                    if valid_uris:
                        ice_servers.append(
                            RTCIceServer(
                                urls=valid_uris,
                                username=s.get("Username"),
                                credential=s.get("Password"),
                            )
                        )
                return ice_servers
    except Exception as err:
        _LOGGER.debug("Could not fetch AWS ICE servers dynamically (%s), using default STUN", err)

    return fallback_servers


def is_private_ip(ip_str: str) -> bool:
    """Return True if IP is RFC1918 private, loopback, or link-local."""
    try:
        ip = ipaddress.ip_address(ip_str)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        return False


def filter_private_candidates(sdp: str) -> str:
    """Filter out RFC1918 private host candidates to avoid AWS TURN 403 Forbidden IP errors."""
    clean_lines = []
    for line in sdp.splitlines():
        if line.startswith("a=candidate:"):
            parts = line.split()
            if len(parts) >= 8 and parts[7] == "host":
                if is_private_ip(parts[4]):
                    continue
        clean_lines.append(line)
    return "\r\n".join(clean_lines) + "\r\n"


def _clean_ascii(text: str) -> str:
    """Normalize and convert text to clean printable ASCII (e.g. Julianów -> Julianow)."""
    norm = unicodedata.normalize("NFKD", text)
    clean = norm.encode("ascii", "ignore").decode("ascii")
    return "".join(c for c in clean if 32 <= ord(c) < 127).strip()


def _get_text_width(draw: Any, text: str, font: Any) -> int:
    """Safely calculate text width in pixels across different Pillow versions."""
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]
    except Exception:
        return len(text) * 14


def generate_snapshot_fallback_card(panel_name: str) -> bytes:
    """Generate an informative JPEG card when direct WebRTC frame capture is buffering."""
    try:
        from PIL import Image, ImageDraw, ImageFont

        width, height = 1280, 720
        img = Image.new("RGB", (width, height), color=(15, 23, 42))
        draw = ImageDraw.Draw(img)

        # Multi-level font discovery with Unicode support
        candidate_font_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/arial.ttf",
            "arial.ttf",
        ]

        has_unicode_font = False
        font_title = font_big = font_sub = font_small = None

        for p in candidate_font_paths:
            if os.path.exists(p):
                try:
                    font_title = ImageFont.truetype(p, 26)
                    font_big = ImageFont.truetype(p, 36)
                    font_sub = ImageFont.truetype(p, 22)
                    font_small = ImageFont.truetype(p, 18)
                    has_unicode_font = True
                    break
                except Exception:
                    continue

        if not has_unicode_font:
            try:
                font_title = ImageFont.load_default(size=26)
                font_big = ImageFont.load_default(size=36)
                font_sub = ImageFont.load_default(size=22)
                font_small = ImageFont.load_default(size=18)
            except Exception:
                font_title = font_big = font_sub = font_small = ImageFont.load_default()

        # Clean panel name to prevent tofu in header
        clean_panel = _clean_ascii(panel_name).upper() or "ACOGO"

        # Header bar
        draw.rectangle([0, 0, width, 68], fill=(10, 15, 28))
        draw.text((32, 22), f"ACO INTERCOM - {clean_panel}", fill=(255, 255, 255), font=font_title)
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        draw.text((width - 260, 22), now_str, fill=(148, 163, 184), font=font_title)

        # Central lens graphic
        cx, cy = width // 2, height // 2 - 20
        draw.ellipse([cx - 100, cy - 100, cx + 100, cy + 100], fill=(30, 41, 59), outline=(2, 132, 199), width=3)
        draw.ellipse([cx - 60, cy - 60, cx + 60, cy + 60], fill=(15, 23, 42), outline=(245, 158, 11), width=2)
        draw.ellipse([cx - 20, cy - 20, cx + 20, cy + 20], fill=(245, 158, 11))

        # Status text (Cyrillic if TTF exists, crystal-clear Latin if default font)
        if has_unicode_font:
            status_text = "ВХОДЯЩИЙ ЗВОНОК В ДОМОФОН"
            sub_text = "Откройте карточку в Home Assistant для просмотра видео 30 FPS"
        else:
            status_text = "INCOMING DOORBELL CALL"
            sub_text = "Open Home Assistant dashboard for live 30 FPS WebRTC video"

        tw = _get_text_width(draw, status_text, font_big)
        draw.text((cx - tw // 2, cy + 120), status_text, fill=(245, 158, 11), font=font_big)

        tw_sub = _get_text_width(draw, sub_text, font_sub)
        draw.text((cx - tw_sub // 2, cy + 175), sub_text, fill=(148, 163, 184), font=font_sub)

        # Footer bar
        draw.rectangle([0, height - 50, width, height], fill=(10, 15, 28))
        draw.text((32, height - 35), "acoGO! Home Assistant Integration", fill=(100, 116, 139), font=font_small)
        draw.text((width - 320, height - 35), "WebRTC Kinesis Live Stream", fill=(16, 185, 129), font=font_small)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception as err:
        _LOGGER.debug("Error creating snapshot fallback card: %s", err)
        # Minimal 1x1 black JPEG fallback
        return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9"


async def async_capture_webrtc_snapshot(
    session: Any,
    aws: dict[str, Any],
    timeout: float = 10.0,
) -> bytes | None:
    """Connect as WebRTC viewer to AWS KVS, receive 1 video frame, and return JPEG bytes."""
    try:
        import aiohttp
        from aiortc import (
            RTCConfiguration,
            RTCPeerConnection,
            RTCSessionDescription,
        )
        from aiortc.sdp import candidate_from_sdp, candidate_to_sdp
        from aiortc.rtp import RtcpPsfbPacket
        from aiortc.rtcrtpreceiver import pack_remb_fci
    except ImportError:
        _LOGGER.warning("aiortc or aiohttp not available for acoGO WebRTC capture")
        return None

    client_id = "HAViewer" + hashlib.md5(str(datetime.datetime.now().timestamp()).encode()).hexdigest()[:10]
    signed_url = generate_signed_wss_url(aws, client_id)

    ice_servers = await fetch_ice_servers(session, aws)
    config = RTCConfiguration(iceServers=ice_servers)
    pc = RTCPeerConnection(configuration=config)
    transceiver = pc.addTransceiver("video", direction="recvonly")

    frame_future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
    remote_ssrc = 0

    @pc.on("track")
    def on_track(track: Any) -> None:
        if track.kind == "video":
            async def _recv_frame() -> None:
                try:
                    frame = await asyncio.wait_for(track.recv(), timeout=timeout)
                    img = frame.to_image()
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=85)
                    jpeg_bytes = buf.getvalue()
                    if not frame_future.done():
                        frame_future.set_result(jpeg_bytes)
                except Exception as err:
                    if not frame_future.done():
                        frame_future.set_exception(err)

            asyncio.create_task(_recv_frame())

    @pc.on("connectionstatechange")
    async def on_connection_state_change() -> None:
        if pc.connectionState == "connected":
            for _ in range(4):
                if frame_future.done():
                    break
                try:
                    r = transceiver.receiver
                    local_ssrc = getattr(r, "_RTCRtpReceiver__rtcp_ssrc", None)
                    if remote_ssrc and local_ssrc and hasattr(r, "_send_rtcp"):
                        remb_fci = pack_remb_fci(2_500_000, [remote_ssrc])
                        remb_pkt = RtcpPsfbPacket(fmt=15, ssrc=local_ssrc, media_ssrc=0, fci=remb_fci)
                        await r._send_rtcp(remb_pkt)
                        await r._send_rtcp_pli(remote_ssrc)
                except Exception:
                    pass
                await asyncio.sleep(0.5)

    try:
        async with session.ws_connect(signed_url, timeout=aiohttp.ClientTimeout(total=6.0)) as ws:
            @pc.on("icecandidate")
            async def on_ice_candidate(candidate: Any) -> None:
                if candidate:
                    if is_private_ip(candidate.ip):
                        return
                    cand_str = f"candidate:{candidate_to_sdp(candidate)}"
                    cand_dict = {
                        "candidate": cand_str,
                        "sdpMid": candidate.sdpMid,
                        "sdpMLineIndex": candidate.sdpMLineIndex,
                    }
                    c_msg = {
                        "action": "ICE_CANDIDATE",
                        "messagePayload": base64.b64encode(json.dumps(cand_dict).encode()).decode(),
                    }
                    try:
                        await ws.send_str(json.dumps(c_msg))
                    except Exception:
                        pass

            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)

            filtered_offer_sdp = filter_private_candidates(pc.localDescription.sdp)
            offer_payload = {
                "type": pc.localDescription.type,
                "sdp": filtered_offer_sdp,
            }
            msg = {
                "action": "SDP_OFFER",
                "messagePayload": base64.b64encode(json.dumps(offer_payload).encode()).decode(),
            }
            await ws.send_str(json.dumps(msg))

            async def _read_signaling() -> None:
                nonlocal remote_ssrc
                async for ws_msg in ws:
                    if ws_msg.type == aiohttp.WSMsgType.TEXT:
                        if not ws_msg.data or not ws_msg.data.strip():
                            continue
                        try:
                            raw = json.loads(ws_msg.data)
                            mtype = raw.get("messageType")
                            if mtype == "SDP_ANSWER":
                                payload = json.loads(base64.b64decode(raw["messagePayload"]).decode())
                                sdp_text = payload.get("sdp", "")
                                ssrc_match = re.search(r"a=ssrc:(\d+)", sdp_text)
                                if ssrc_match:
                                    remote_ssrc = int(ssrc_match.group(1))

                                sanitized_sdp = filter_private_candidates(sdp_text)
                                await pc.setRemoteDescription(
                                    RTCSessionDescription(sdp=sanitized_sdp, type=payload["type"])
                                )
                            elif mtype == "ICE_CANDIDATE":
                                payload = json.loads(base64.b64decode(raw["messagePayload"]).decode())
                                cand_str = payload.get("candidate", "")
                                if cand_str:
                                    clean_c_str = re.sub(r"^candidate:\s*", "", cand_str)
                                    try:
                                        c = candidate_from_sdp(clean_c_str)
                                        c.sdpMid = payload.get("sdpMid")
                                        c.sdpMLineIndex = payload.get("sdpMLineIndex")
                                        if not is_private_ip(c.ip):
                                            await pc.addIceCandidate(c)
                                    except Exception:
                                        pass
                        except Exception as e:
                            _LOGGER.debug("Error processing signaling message: %s", e)
                    elif ws_msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break

            signaling_task = asyncio.create_task(_read_signaling())

            try:
                jpeg_data = await asyncio.wait_for(frame_future, timeout=timeout)
                return jpeg_data
            finally:
                signaling_task.cancel()

    except asyncio.TimeoutError:
        _LOGGER.debug("Timeout waiting for WebRTC video frame from acoGO panel")
        return None
    except Exception as err:
        _LOGGER.debug("WebRTC snapshot capture error: %s", err)
        return None
    finally:
        try:
            await pc.close()
        except Exception:
            pass
