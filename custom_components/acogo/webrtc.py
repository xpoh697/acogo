"""WebRTC frame capture helper for acoGO! AWS Kinesis Video Streams."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import datetime
import hashlib
import hmac
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import struct
import unicodedata
import urllib.parse
from typing import Any

_LOGGER = logging.getLogger(__name__)

_FILE_LOGGER_INSTALLED = False


def setup_acogo_file_logger() -> None:
    """Configure a dedicated RotatingFileHandler writing to /config/acogo.log.

    This ensures full debug logs (including aiortc, aioice, and acogo) are accessible
    directly via SMB share \\\\<ha_ip>\\config\\acogo.log.
    """
    global _FILE_LOGGER_INSTALLED
    if _FILE_LOGGER_INSTALLED:
        return

    try:
        log_path = "/config/acogo.log"
        if not os.path.isdir("/config"):
            log_path = os.path.join(os.getcwd(), "acogo.log")

        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] (%(name)s) %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        handler = RotatingFileHandler(
            log_path,
            maxBytes=2 * 1024 * 1024,  # 2 MB
            backupCount=1,
            encoding="utf-8",
        )
        handler.setFormatter(formatter)
        handler.setLevel(logging.DEBUG)

        target_loggers = [
            "custom_components.acogo",
            "aiortc",
            "aioice",
        ]

        for name in target_loggers:
            lg = logging.getLogger(name)
            lg.setLevel(logging.DEBUG)
            for h in list(lg.handlers):
                if isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", "") == handler.baseFilename:
                    lg.removeHandler(h)
            lg.addHandler(handler)

        _FILE_LOGGER_INSTALLED = True
        _LOGGER.info("Dedicated acoGO file logger initialized at %s", log_path)
    except Exception as err:
        _LOGGER.warning("Could not set up dedicated acoGO file logger: %s", err)


@dataclass(slots=True)
class RTCIceServer:
    """Lightweight ICE server configuration container (zero external dependencies)."""

    urls: list[str] = field(default_factory=list)
    username: str | None = None
    credential: str | None = None


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


async def fetch_ice_servers(session: Any, aws: dict[str, Any]) -> list[RTCIceServer]:
    """Fetch dynamic AWS KVS STUN/TURN server configurations via SigV4."""
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

        signed_headers_str = ";".join(sorted(headers.keys()))
        canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers.keys()))
        canonical_request = f"POST\n{path}\n\n{canonical_headers}\n{signed_headers_str}\n{payload_hash}"
        credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
        string_to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"

        signing_key = _get_signature_key(secret_key, date_stamp, region, service)
        signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        headers["Authorization"] = f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, SignedHeaders={signed_headers_str}, Signature={signature}"

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


def strip_candidates_from_sdp(sdp: str) -> str:
    """Strip candidate and end-of-candidates lines to make pure trickle-ICE SDP offer (<1.5KB)."""
    clean_lines = [
        line for line in sdp.splitlines()
        if not line.startswith("a=candidate:") and not line.startswith("a=end-of-candidates")
    ]
    return "\r\n".join(clean_lines) + "\r\n"


def compact_h264_sdp(sdp: str) -> str:
    """Compact SDP offer to keep only H.264 codecs and required attributes matching JS card."""
    lines = sdp.splitlines()
    in_video = False
    in_app = False
    h264_pts: set[str] = set()
    rtx_pts: dict[str, str] = {}
    app_has_sctp_port = False

    # Pass 1: find H.264 payload types
    for line in lines:
        if line.startswith("m=video "):
            in_video = True
            in_app = False
            continue
        elif line.startswith("m=application ") or line.startswith("m=audio "):
            in_video = False

        if in_video:
            m = re.match(r"^a=rtpmap:(\d+)\s+([A-Za-z0-9\-_]+)/", line, re.IGNORECASE)
            if m:
                pt, codec = m.group(1), m.group(2).upper()
                if codec == "H264":
                    h264_pts.add(pt)
            m_fmtp = re.match(r"^a=fmtp:(\d+)\s+apt=(\d+)", line, re.IGNORECASE)
            if m_fmtp:
                rtx_pts[m_fmtp.group(1)] = m_fmtp.group(2)

    allowed_pts = set(h264_pts)
    for rtx_pt, apt_pt in rtx_pts.items():
        if apt_pt in h264_pts:
            allowed_pts.add(rtx_pt)

    # Pass 2: filter SDP lines
    in_video = False
    in_app = False
    result: list[str] = []

    for line in lines:
        # Drop redundant large fingerprints (keep sha-256 only)
        if line.startswith("a=fingerprint:sha-384") or line.startswith("a=fingerprint:sha-512"):
            continue

        if line.startswith("m=video "):
            in_video = True
            in_app = False
            parts = line.split(" ")
            if len(parts) > 3 and allowed_pts:
                header = parts[:3]
                pts = [pt for pt in parts[3:] if pt in allowed_pts]
                result.append(" ".join(header + pts))
            else:
                result.append(line)
            continue

        if line.startswith("m=application "):
            in_video = False
            in_app = True
            result.append(line)
            continue

        if line.startswith("m=audio ") or (line.startswith("m=") and not line.startswith("m=video ") and not line.startswith("m=application ")):
            in_video = False
            in_app = False
            result.append(line)
            continue

        if in_video:
            pt_attr = re.match(r"^a=(?:rtpmap|fmtp|rtcp-fb):(\d+)", line, re.IGNORECASE)
            if pt_attr:
                pt = pt_attr.group(1)
                if pt not in allowed_pts:
                    continue
            result.append(line)
            continue

        if in_app:
            if line.startswith("a=sctp-port:"):
                app_has_sctp_port = True
            result.append(line)
            continue

        result.append(line)

    final_sdp = "\r\n".join(result)
    if not app_has_sctp_port and "m=application" in final_sdp:
        final_sdp = re.sub(r"(m=application[^\r\n]*)", r"\1\r\na=sctp-port:5000", final_sdp)

    return final_sdp.strip() + "\r\n"


def extract_ssrcs_from_sdp(sdp: str) -> dict[str, int]:
    """Extract audio and video remote SSRCs from SDP answer by media line."""
    ssrcs: dict[str, int] = {}
    current_media: str | None = None
    for line in sdp.splitlines():
        if line.startswith("m="):
            parts = line.split()
            current_media = parts[0][2:]  # "audio" or "video"
        elif line.startswith("a=ssrc:") and current_media:
            m = re.search(r"a=ssrc:(\d+)", line)
            if m and current_media not in ssrcs:
                ssrcs[current_media] = int(m.group(1))
    return ssrcs


def _clean_ascii(text: str) -> str:
    """Normalize and convert text to clean printable ASCII."""
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
    """Generate an informative JPEG card when direct WebRTC frame capture fails."""
    try:
        from PIL import Image, ImageDraw, ImageFont

        width, height = 1280, 720
        img = Image.new("RGB", (width, height), color=(15, 23, 42))
        draw = ImageDraw.Draw(img)

        candidate_font_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
        ]

        has_font = False
        font_title = font_big = font_sub = font_small = None

        for p in candidate_font_paths:
            if os.path.exists(p):
                try:
                    font_title = ImageFont.truetype(p, 26)
                    font_big = ImageFont.truetype(p, 36)
                    font_sub = ImageFont.truetype(p, 22)
                    font_small = ImageFont.truetype(p, 18)
                    has_font = True
                    break
                except Exception:
                    continue

        if not has_font:
            try:
                font_title = ImageFont.load_default(size=26)
                font_big = ImageFont.load_default(size=36)
                font_sub = ImageFont.load_default(size=22)
                font_small = ImageFont.load_default(size=18)
            except Exception:
                font_title = font_big = font_sub = font_small = ImageFont.load_default()

        clean_panel = _clean_ascii(panel_name).upper() or "ACOGO"

        draw.rectangle([0, 0, width, 68], fill=(10, 15, 28))
        draw.text((32, 22), f"ACO INTERCOM - {clean_panel}", fill=(255, 255, 255), font=font_title)
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        draw.text((width - 260, 22), now_str, fill=(148, 163, 184), font=font_title)

        cx, cy = width // 2, height // 2 - 20
        draw.ellipse([cx - 100, cy - 100, cx + 100, cy + 100], fill=(30, 41, 59), outline=(2, 132, 199), width=3)
        draw.ellipse([cx - 60, cy - 60, cx + 60, cy + 60], fill=(15, 23, 42), outline=(245, 158, 11), width=2)
        draw.ellipse([cx - 20, cy - 20, cx + 20, cy + 20], fill=(245, 158, 11))

        status_text = "INCOMING DOORBELL CALL"
        sub_text = "Open Home Assistant dashboard for live 30 FPS WebRTC video"

        tw = _get_text_width(draw, status_text, font_big)
        draw.text((cx - tw // 2, cy + 120), status_text, fill=(245, 158, 11), font=font_big)

        tw_sub = _get_text_width(draw, sub_text, font_sub)
        draw.text((cx - tw_sub // 2, cy + 175), sub_text, fill=(148, 163, 184), font=font_sub)

        draw.rectangle([0, height - 50, width, height], fill=(10, 15, 28))
        draw.text((32, height - 35), "acoGO! Home Assistant Integration", fill=(100, 116, 139), font=font_small)
        draw.text((width - 320, height - 35), "WebRTC Kinesis Live Stream", fill=(16, 185, 129), font=font_small)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception as err:
        _LOGGER.debug("Error creating snapshot fallback card: %s", err)
        return b""


async def async_capture_webrtc_snapshot(
    session: Any,
    aws: dict[str, Any],
    timeout: float = 25.0,
) -> bytes | None:
    """Connect as WebRTC viewer to AWS KVS and capture one video frame as JPEG."""
    setup_acogo_file_logger()

    try:
        import aiohttp
        from aiortc import (
            RTCConfiguration,
            RTCIceServer as AiortcIceServer,
            RTCPeerConnection,
            RTCSessionDescription,
        )
        from aiortc.sdp import candidate_from_sdp
        from aiortc.rtp import (
            RTCP_PSFB_APP,
            RTCP_PSFB_FIR,
            RTCP_PSFB_PLI,
            RtcpPsfbPacket,
            RtcpRrPacket,
        )
        from aiortc.rtcrtpreceiver import pack_remb_fci
    except ImportError as err:
        _LOGGER.warning("aiortc, av or aiohttp not available: %s", err)
        return None

    client_id = "HAViewer" + hashlib.md5(
        str(datetime.datetime.now().timestamp()).encode()
    ).hexdigest()[:10]
    signed_url = generate_signed_wss_url(aws, client_id)

    ice_servers_raw = await fetch_ice_servers(session, aws)
    _LOGGER.info(
        "ICE servers fetched: %d total (%s)",
        len(ice_servers_raw),
        ", ".join(s.urls[0] if s.urls else "?" for s in ice_servers_raw),
    )

    rtc_ice_servers = [
        AiortcIceServer(urls=s.urls, username=s.username, credential=s.credential)
        for s in ice_servers_raw
    ]
    config = RTCConfiguration(iceServers=rtc_ice_servers)
    pc = RTCPeerConnection(configuration=config)

    # Add video transceiver (recvonly) - matches frontend Lovelace card
    video_transceiver = pc.addTransceiver("video", direction="recvonly")

    # Create DataChannel matching official acoGO app and web card
    ch_name = (
        aws.get("channel name")
        or aws.get("channelName")
        or aws.get("channel_name")
        or "data"
    )
    dc = pc.createDataChannel(str(ch_name))
    _LOGGER.info("DataChannel created (label: %s)", dc.label)

    frame_future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
    remote_video_ssrc = 0
    pending_ice_candidates: list[str] = []
    has_remote_description = False

    def _send_network_handshake(channel: Any) -> None:
        try:
            msg = json.dumps({"network": {"type": "wifi"}})
            channel.send(msg)
            _LOGGER.info("Sent DataChannel network handshake: %s", msg)
        except Exception as he:
            _LOGGER.debug("Handshake send error: %s", he)

    @dc.on("open")
    def on_dc_open() -> None:
        _LOGGER.info("DataChannel '%s' is OPEN! Sending network handshake", dc.label)
        _send_network_handshake(dc)

    @dc.on("message")
    def on_dc_message(msg: Any) -> None:
        _LOGGER.info("DataChannel message from intercom: %s", msg)

    @pc.on("datachannel")
    def on_datachannel(channel: Any) -> None:
        _LOGGER.info("Intercom initiated DataChannel: %s", channel.label)

        @channel.on("open")
        def on_ch_open() -> None:
            _send_network_handshake(channel)

        @channel.on("message")
        def on_ch_msg(msg: Any) -> None:
            _LOGGER.info("Master channel message: %s", msg)

    @pc.on("track")
    def on_track(track: Any) -> None:
        _LOGGER.info("Track received from intercom: kind=%s id=%s", track.kind, track.id)
        if track.kind == "video":
            async def _recv_native() -> None:
                try:
                    frame = await asyncio.wait_for(track.recv(), timeout=timeout)
                    _LOGGER.info(
                        "Native aiortc video frame decoded successfully: %dx%d!",
                        frame.width, frame.height,
                    )
                    img = frame.to_image()
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=85)
                    if not frame_future.done():
                        frame_future.set_result(buf.getvalue())
                except asyncio.CancelledError:
                    pass
                except Exception as err:
                    _LOGGER.info("Native track.recv() exception: %s (%s)", err, type(err).__name__)

            asyncio.create_task(_recv_native())

    @pc.on("connectionstatechange")
    async def on_connection_state_change() -> None:
        _LOGGER.info("PC connection state: %s", pc.connectionState)

        if pc.connectionState == "connected":
            try:
                ice_transport = video_transceiver.receiver.transport.transport
                ice_conn = ice_transport._connection
                for comp, pair in ice_conn._nominated.items():
                    local_c = pair.protocol.local_candidate
                    remote_c = pair.remote_candidate
                    _LOGGER.info(
                        "Nominated ICE pair [comp=%d]: local=%s:%d (%s) -> remote=%s:%d (%s)",
                        comp,
                        local_c.host, local_c.port, local_c.type,
                        remote_c.host, remote_c.port, remote_c.type,
                    )
            except Exception as err:
                _LOGGER.debug("Could not log nominated ICE pair: %s", err)

            try:
                dtls_state = video_transceiver.receiver.transport.state
                _LOGGER.info("DTLS transport state: %s", dtls_state)
            except Exception:
                pass

            # Send network handshake if DataChannel is already open
            if dc.readyState == "open":
                _LOGGER.info("DataChannel is already open upon connected, sending handshake")
                _send_network_handshake(dc)

            # Continuous DataChannel handshake retries until first frame
            async def _dc_handshake_loop() -> None:
                for _ in range(int(timeout)):
                    if frame_future.done():
                        break
                    if dc.readyState == "open":
                        _send_network_handshake(dc)
                    await asyncio.sleep(1.0)

            asyncio.create_task(_dc_handshake_loop())

            # Send Compound RTCP feedback (Receiver Report + REMB + PLI/FIR) per RFC 3550
            receiver = video_transceiver.receiver
            local_ssrc = getattr(receiver, "_RTCRtpReceiver__rtcp_ssrc", 1) or 1
            v_ssrc = remote_video_ssrc

            _LOGGER.info(
                "Initiating Compound RTCP feedback: local_ssrc=%d remote_video_ssrc=%d",
                local_ssrc,
                v_ssrc,
            )

            # 1. Send Compound REMB declaring 2.5 Mbps bandwidth for video
            if v_ssrc:
                try:
                    rr = RtcpRrPacket(ssrc=local_ssrc)
                    remb_fci = pack_remb_fci(2_500_000, [v_ssrc])
                    remb_pkt = RtcpPsfbPacket(
                        fmt=RTCP_PSFB_APP,
                        ssrc=local_ssrc,
                        media_ssrc=0,
                        fci=remb_fci,
                    )
                    compound_remb = bytes(rr) + bytes(remb_pkt)
                    await receiver.transport._send_rtp(compound_remb)
                    _LOGGER.info("Sent Compound RTCP REMB (2.5 Mbps) for video SSRC %d", v_ssrc)
                except Exception as re_err:
                    _LOGGER.debug("REMB send error: %s", re_err)

            # 2. Keyframe request loop: Compound PLI + FIR for video SSRC
            async def _keyframe_loop() -> None:
                fir_seq = 0
                for i in range(15):
                    if frame_future.done():
                        break
                    try:
                        if v_ssrc:
                            rr = RtcpRrPacket(ssrc=local_ssrc)
                            pli = RtcpPsfbPacket(
                                fmt=RTCP_PSFB_PLI,
                                ssrc=local_ssrc,
                                media_ssrc=v_ssrc,
                                fci=b"",
                            )
                            compound_pli = bytes(rr) + bytes(pli)
                            await receiver.transport._send_rtp(compound_pli)
                            _LOGGER.info("Compound PLI packet #%d sent for video SSRC %d", i + 1, v_ssrc)

                            if i % 2 == 0:
                                fir_fci = struct.pack("!IBxxx", v_ssrc, fir_seq & 0xFF)
                                fir_seq += 1
                                fir = RtcpPsfbPacket(
                                    fmt=RTCP_PSFB_FIR,
                                    ssrc=local_ssrc,
                                    media_ssrc=0,
                                    fci=fir_fci,
                                )
                                compound_fir = bytes(rr) + bytes(fir)
                                await receiver.transport._send_rtp(compound_fir)
                                _LOGGER.info("Compound FIR packet #%d sent for video SSRC %d", fir_seq, v_ssrc)
                    except Exception as pe:
                        _LOGGER.debug("Keyframe send error: %s", pe)
                    await asyncio.sleep(0.4)

            asyncio.create_task(_keyframe_loop())

            # 3. Monitor transport RX packets periodically
            async def _monitor_stats() -> None:
                dtls_tr = video_transceiver.receiver.transport
                for _ in range(int(timeout * 2)):
                    if frame_future.done():
                        break
                    rx_p = getattr(dtls_tr, "_RTCDtlsTransport__rx_packets", -1)
                    rx_b = getattr(dtls_tr, "_RTCDtlsTransport__rx_bytes", -1)
                    _LOGGER.info("DTLS datagram stats: rx_packets=%d rx_bytes=%d", rx_p, rx_b)
                    await asyncio.sleep(1.0)

            asyncio.create_task(_monitor_stats())

        elif pc.connectionState == "failed":
            _LOGGER.warning("ICE connection state changed to FAILED")
            if not frame_future.done():
                frame_future.set_exception(ConnectionError("ICE failed"))

    try:
        async with session.ws_connect(
            signed_url, timeout=aiohttp.ClientTimeout(total=8.0)
        ) as ws:

            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)

            # Compact SDP to H.264 and strip initial candidate lines for Pure Trickle ICE (< 1.5KB)
            compact_offer = compact_h264_sdp(pc.localDescription.sdp)
            pure_trickle_offer = strip_candidates_from_sdp(compact_offer)

            await ws.send_str(json.dumps({
                "action": "SDP_OFFER",
                "messagePayload": base64.b64encode(json.dumps({
                    "type": pc.localDescription.type,
                    "sdp": pure_trickle_offer,
                }).encode()).decode(),
            }))
            _LOGGER.info("Pure Trickle ICE SDP offer sent to KVS signaling (%d bytes)", len(pure_trickle_offer))

            # Active Trickle ICE sender: stream public srflx & relay candidates from aioice directly to KVS WebSocket
            async def _send_trickle_candidates() -> None:
                sent_cand_ids: set[tuple[str, str, int]] = set()
                while not frame_future.done():
                    try:
                        ice_transports = getattr(pc, "_RTCPeerConnection__iceTransports", [])
                        for it in ice_transports:
                            conn = getattr(it, "_connection", None)
                            if conn and hasattr(conn, "_local_candidates"):
                                for c in list(conn._local_candidates):
                                    cand_id = (c.type, c.host, c.port)
                                    if cand_id not in sent_cand_ids:
                                        sent_cand_ids.add(cand_id)
                                        # Only send public IPv4 srflx and relay candidates
                                        if c.type in ("srflx", "relay") and ":" not in c.host:
                                            cand_str = f"candidate:{c.to_sdp()}"
                                            _LOGGER.info(
                                                "Sending Trickle ICE candidate to intercom: %s %s:%d",
                                                c.type, c.host, c.port,
                                            )
                                            cand_dict = {
                                                "candidate": cand_str,
                                                "sdpMid": "0",
                                                "sdpMLineIndex": 0,
                                            }
                                            await ws.send_str(json.dumps({
                                                "action": "ICE_CANDIDATE",
                                                "messagePayload": base64.b64encode(
                                                    json.dumps(cand_dict).encode()
                                                ).decode(),
                                            }))
                    except Exception as ex:
                        _LOGGER.debug("Trickle candidate scan error: %s", ex)
                    await asyncio.sleep(0.1)

            trickle_task = asyncio.create_task(_send_trickle_candidates())

            async def _read_signaling() -> None:
                nonlocal remote_video_ssrc, has_remote_description
                async for ws_msg in ws:
                    if ws_msg.type == aiohttp.WSMsgType.TEXT:
                        if not ws_msg.data or not ws_msg.data.strip():
                            continue
                        try:
                            raw = json.loads(ws_msg.data)
                            mtype = raw.get("messageType")
                            if mtype == "SDP_ANSWER":
                                payload = json.loads(
                                    base64.b64decode(raw["messagePayload"]).decode()
                                )
                                sdp_text = payload.get("sdp", "")

                                ssrc_map = extract_ssrcs_from_sdp(sdp_text)
                                remote_video_ssrc = ssrc_map.get("video", 0)
                                _LOGGER.info(
                                    "SSRCs extracted from SDP answer: video=%d (all=%s)",
                                    remote_video_ssrc,
                                    ssrc_map,
                                )

                                await pc.setRemoteDescription(
                                    RTCSessionDescription(sdp=sdp_text, type=payload["type"])
                                )
                                has_remote_description = True
                                _LOGGER.info(
                                    "Set remote description (SDP answer applied, flushing %d pending candidates)",
                                    len(pending_ice_candidates),
                                )

                                # Flush queued candidates received before SDP_ANSWER
                                for c_str in pending_ice_candidates:
                                    try:
                                        c = candidate_from_sdp(c_str)
                                        c.sdpMid = "0"
                                        c.sdpMLineIndex = 0
                                        await pc.addIceCandidate(c)
                                        _LOGGER.info("Added flushed remote candidate: %s", c_str[:70])
                                    except Exception as ce:
                                        _LOGGER.debug("Error adding queued remote candidate: %s", ce)
                                pending_ice_candidates.clear()

                            elif mtype == "ICE_CANDIDATE":
                                payload = json.loads(
                                    base64.b64decode(raw["messagePayload"]).decode()
                                )
                                cand_str = payload.get("candidate", "")
                                if cand_str:
                                    clean = re.sub(r"^candidate:\s*", "", cand_str)
                                    if not has_remote_description:
                                        pending_ice_candidates.append(clean)
                                    else:
                                        try:
                                            c = candidate_from_sdp(clean)
                                            c.sdpMid = payload.get("sdpMid") or "0"
                                            c.sdpMLineIndex = payload.get("sdpMLineIndex") or 0
                                            await pc.addIceCandidate(c)
                                            _LOGGER.info("Added remote ICE candidate: %s", clean[:70])
                                        except Exception as ce:
                                            _LOGGER.debug("Error adding remote candidate: %s", ce)

                            elif mtype == "STATUS_RESPONSE" or raw.get("statusResponse"):
                                st = raw.get("statusResponse") or raw
                                _LOGGER.warning("KVS status response: %s", st)
                        except Exception as e:
                            _LOGGER.debug("Signaling parse error: %s", e)
                    elif ws_msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break

            signaling_task = asyncio.create_task(_read_signaling())

            try:
                jpeg_data = await asyncio.wait_for(
                    asyncio.shield(frame_future), timeout=timeout
                )
                _LOGGER.info("WebRTC snapshot captured successfully: %d bytes JPEG", len(jpeg_data))
                return jpeg_data
            except asyncio.TimeoutError:
                _LOGGER.warning("Timeout waiting for WebRTC video frame (timeout=%ds)", int(timeout))
                return None
            except Exception as err:
                _LOGGER.warning("Frame capture error: %s (%s)", err, type(err).__name__)
                return None
            finally:
                trickle_task.cancel()
                signaling_task.cancel()

    except asyncio.TimeoutError:
        _LOGGER.warning("KVS WebSocket signaling connection timeout")
        return None
    except Exception as err:
        _LOGGER.warning("WebRTC snapshot capture exception: %s", err)
        return None
    finally:
        try:
            await pc.close()
        except Exception:
            pass
