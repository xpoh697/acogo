"""WebRTC frame capture helper for acoGO! AWS Kinesis Video Streams."""
from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import hmac
import io
import json
import logging
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


async def async_capture_webrtc_snapshot(
    session: Any,
    aws: dict[str, Any],
    timeout: float = 7.0,
) -> bytes | None:
    """Connect as WebRTC viewer to AWS KVS, receive 1 video frame, and return JPEG bytes."""
    try:
        import aiohttp
        from aiortc import (
            RTCConfiguration,
            RTCIceCandidate,
            RTCIceServer,
            RTCPeerConnection,
            RTCSessionDescription,
        )
    except ImportError:
        _LOGGER.warning("aiortc or aiohttp not available for acoGO WebRTC capture")
        return None

    client_id = "HAViewer" + hashlib.md5(str(datetime.datetime.now().timestamp()).encode()).hexdigest()[:10]
    signed_url = generate_signed_wss_url(aws, client_id)
    region = aws.get("region", "eu-west-2")

    config = RTCConfiguration(
        iceServers=[RTCIceServer(urls=[f"stun:stun.kinesisvideo.{region}.amazonaws.com:443"])]
    )
    pc = RTCPeerConnection(configuration=config)
    pc.addTransceiver("video", direction="recvonly")

    frame_future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

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

    try:
        async with session.ws_connect(signed_url, timeout=5.0) as ws:
            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)

            offer_payload = {
                "type": pc.localDescription.type,
                "sdp": pc.localDescription.sdp,
            }
            msg = {
                "action": "SDP_OFFER",
                "messagePayload": base64.b64encode(json.dumps(offer_payload).encode()).decode(),
            }
            await ws.send_str(json.dumps(msg))

            @pc.on("icecandidate")
            async def on_ice_candidate(candidate: Any) -> None:
                if candidate:
                    cand_dict = {
                        "candidate": candidate.candidate,
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

            async def _read_signaling() -> None:
                async for ws_msg in ws:
                    if ws_msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            raw = json.loads(ws_msg.data)
                            mtype = raw.get("messageType")
                            if mtype == "SDP_ANSWER":
                                payload = json.loads(base64.b64decode(raw["messagePayload"]).decode())
                                await pc.setRemoteDescription(
                                    RTCSessionDescription(sdp=payload["sdp"], type=payload["type"])
                                )
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
        _LOGGER.info("Timeout waiting for WebRTC video frame from acoGO panel")
        return None
    except Exception as err:
        _LOGGER.warning("WebRTC snapshot capture error: %s", err)
        return None
    finally:
        try:
            await pc.close()
        except Exception:
            pass
