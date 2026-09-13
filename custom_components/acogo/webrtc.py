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
import re
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
            timeout=aiohttp.ClientTimeout(total=2.5),
        ) as r:
            if r.status == 200:
                data = await r.json()
                ice_servers = list(fallback_servers)
                for s in data.get("IceServerList", []):
                    # aiortc handles standard turn: URIs, skip turns:
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


def filter_private_candidates(sdp: str) -> str:
    """Filter out RFC1918 private host candidates to avoid AWS TURN 403 Forbidden IP errors."""
    clean_lines = []
    for line in sdp.splitlines():
        if line.startswith("a=candidate:"):
            parts = line.split()
            if len(parts) >= 8 and parts[7] == "host":
                ip_str = parts[4]
                try:
                    ip = ipaddress.ip_address(ip_str)
                    if ip.is_private or ip.is_loopback or ip.is_link_local:
                        continue
                except ValueError:
                    pass
        clean_lines.append(line)
    return "\r\n".join(clean_lines) + "\r\n"


async def async_capture_webrtc_snapshot(
    session: Any,
    aws: dict[str, Any],
    timeout: float = 12.0,
) -> bytes | None:
    """Connect as WebRTC viewer to AWS KVS, receive 1 video frame, and return JPEG bytes."""
    try:
        import aiohttp
        from aiortc import (
            RTCConfiguration,
            RTCPeerConnection,
            RTCSessionDescription,
        )
        from aiortc.sdp import candidate_from_sdp
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
            # Send periodic RTCP Picture Loss Indication (PLI) to trigger an immediate keyframe
            for _ in range(4):
                if frame_future.done():
                    break
                try:
                    if remote_ssrc and hasattr(transceiver, "receiver") and transceiver.receiver:
                        await transceiver.receiver._send_rtcp_pli(remote_ssrc)
                except Exception:
                    pass
                await asyncio.sleep(0.8)

    try:
        async with session.ws_connect(signed_url, timeout=aiohttp.ClientTimeout(total=8.0)) as ws:
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
                nonlocal remote_ssrc
                async for ws_msg in ws:
                    if ws_msg.type == aiohttp.WSMsgType.TEXT:
                        # AWS Kinesis sends empty text messages as heartbeats; skip them safely
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
                                cand_str = payload.get("candidate")
                                if cand_str:
                                    try:
                                        c = candidate_from_sdp(cand_str)
                                        c.sdpMid = payload.get("sdpMid")
                                        c.sdpMLineIndex = payload.get("sdpMLineIndex")
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
