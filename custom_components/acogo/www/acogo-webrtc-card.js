/**
 * acoGO! Live WebRTC Lovelace Card
 * Directly connects to AWS Kinesis Video Streams WebRTC for ultra-low latency intercom video feed.
 * Version: 1.0.9
 */

function safeBtoa(obj) {
  const str = typeof obj === 'string' ? obj : JSON.stringify(obj);
  try {
    return btoa(str);
  } catch (e) {
    return btoa(encodeURIComponent(str).replace(/%([0-9A-F]{2})/g, (_, p1) => String.fromCharCode('0x' + p1)));
  }
}

function filterPrivateCandidates(sdp) {
  if (!sdp) return '';
  const lines = sdp.split(/\r?\n/);
  const clean = lines.filter(line => {
    if (line.startsWith('a=candidate:')) {
      const parts = line.split(' ');
      if (parts.length >= 8 && parts[7] === 'host') {
        const ip = parts[4];
        if (ip.endsWith('.local') ||
            ip.startsWith('10.') ||
            ip.startsWith('192.168.') ||
            ip.startsWith('127.') ||
            ip.startsWith('169.254.') ||
            /^172\.(1[6-9]|2[0-9]|3[0-1])\./.test(ip)) {
          return false;
        }
      }
    }
    return true;
  });
  return clean.join('\r\n') + '\r\n';
}

function compactH264Sdp(sdp) {
  if (!sdp) return '';
  const lines = sdp.split(/\r?\n/);
  let inVideo = false;
  let inApp = false;
  const h264Pts = new Set();
  const rtxPts = new Map();
  let appHasSctpPort = false;

  // Pass 1: identify H.264 payload types and associated RTX
  for (const line of lines) {
    if (line.startsWith('m=video ')) {
      inVideo = true;
      inApp = false;
      continue;
    } else if (line.startsWith('m=application ') || line.startsWith('m=audio ')) {
      inVideo = false;
    }
    if (inVideo) {
      const rtpmapMatch = line.match(/^a=rtpmap:(\d+)\s+([A-Za-z0-9\-_]+)\//i);
      if (rtpmapMatch) {
        const pt = rtpmapMatch[1];
        const codec = rtpmapMatch[2].toUpperCase();
        if (codec === 'H264') {
          h264Pts.add(pt);
        }
      }
      const fmtpMatch = line.match(/^a=fmtp:(\d+)\s+apt=(\d+)/i);
      if (fmtpMatch) {
        rtxPts.set(fmtpMatch[1], fmtpMatch[2]);
      }
    }
  }

  const allowedPts = new Set(h264Pts);
  for (const [rtxPt, aptPt] of rtxPts.entries()) {
    if (h264Pts.has(aptPt)) {
      allowedPts.add(rtxPt);
    }
  }

  // Pass 2: filter SDP lines
  inVideo = false;
  inApp = false;
  const result = [];

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    if (line.startsWith('m=video ')) {
      inVideo = true;
      inApp = false;
      const parts = line.split(' ');
      if (parts.length > 3 && allowedPts.size > 0) {
        const header = parts.slice(0, 3);
        const pts = parts.slice(3).filter(pt => allowedPts.has(pt));
        result.push([...header, ...pts].join(' '));
      } else {
        result.push(line);
      }
      continue;
    }

    if (line.startsWith('m=application ')) {
      inVideo = false;
      inApp = true;
      result.push(line);
      continue;
    }

    if (line.startsWith('m=audio ') || (line.startsWith('m=') && !line.startsWith('m=video ') && !line.startsWith('m=application '))) {
      inVideo = false;
      inApp = false;
      result.push(line);
      continue;
    }

    if (inVideo) {
      const ptAttr = line.match(/^a=(?:rtpmap|fmtp|rtcp-fb):(\d+)/i);
      if (ptAttr) {
        const pt = ptAttr[1];
        if (!allowedPts.has(pt)) {
          continue;
        }
      }
      result.push(line);
      continue;
    }

    if (inApp) {
      if (line.startsWith('a=sctp-port:')) {
        appHasSctpPort = true;
      }
      result.push(line);
      continue;
    }

    result.push(line);
  }

  let finalSdp = result.join('\r\n');
  if (!appHasSctpPort && finalSdp.includes('m=application')) {
    finalSdp = finalSdp.replace(/(m=application[^\r\n]*)/, '$1\r\na=sctp-port:5000');
  }

  return finalSdp.trim() + '\r\n';
}

class AcoGoWebRtcCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._pc = null;
    this._ws = null;
    this._dataChannel = null;
    this._streamActive = false;
    this._countdown = 0;
    this._countdownInterval = null;
    this._connectTimeout = null;
    this._waitInterval = null;
    this._waitingSeconds = 0;
    this._pendingIceCandidates = [];
    this._hasRemoteDescription = false;
    this._status = 'idle'; // idle | connecting | streaming | error
    this._statusText = 'ГОТОВ К ТРАНСЛЯЦИИ';
  }

  static getStubConfig() {
    return {
      title: 'acoGO! Julianów',
      device_id: '01:01:26:79'
    };
  }

  getCardSize() {
    return 6;
  }

  setConfig(config) {
    this._config = {
      title: 'acoGO! Intercom',
      door_entity: '',
      gate_entity: '',
      ...config
    };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (!hass) return;

    // Auto-discover locks/buttons for acogo if not set or invalid
    if (!this._config.door_entity || !hass.states[this._config.door_entity]) {
      for (const eid in hass.states) {
        if (eid.startsWith('lock.') && eid.includes('acogo') && eid.includes('door')) {
          this._config.door_entity = eid;
          break;
        }
      }
    }
    if (!this._config.gate_entity || !hass.states[this._config.gate_entity]) {
      for (const eid in hass.states) {
        if (eid.startsWith('lock.') && eid.includes('acogo') && (eid.includes('gate') || eid.includes('f2'))) {
          this._config.gate_entity = eid;
          break;
        }
      }
    }
  }

  disconnectedCallback() {
    this._stopStream();
  }

  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        ha-card {
          overflow: hidden;
          background: #0d121c;
          border-radius: 14px;
          border: 1px solid rgba(255, 255, 255, 0.1);
          box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
          color: #f8fafc;
        }
        .header {
          padding: 14px 18px;
          display: flex;
          justify-content: space-between;
          align-items: center;
          border-bottom: 1px solid rgba(255, 255, 255, 0.08);
          background: rgba(255, 255, 255, 0.02);
        }
        .title {
          font-size: 16px;
          font-weight: 600;
          display: flex;
          align-items: center;
          gap: 10px;
          color: #f1f5f9;
        }
        .version-badge {
          font-size: 10px;
          font-weight: 600;
          color: #94a3b8;
          background: rgba(255, 255, 255, 0.08);
          padding: 2px 7px;
          border-radius: 6px;
          letter-spacing: 0.4px;
          border: 1px solid rgba(255, 255, 255, 0.08);
        }
        .badge {
          font-size: 11px;
          padding: 4px 10px;
          border-radius: 20px;
          font-weight: 600;
          text-transform: uppercase;
          letter-spacing: 0.5px;
        }
        .badge-idle { background: rgba(148, 163, 184, 0.2); color: #cbd5e1; }
        .badge-connecting { background: rgba(245, 158, 11, 0.2); color: #f59e0b; }
        .badge-streaming { background: rgba(16, 185, 129, 0.2); color: #10b981; }
        .badge-error { background: rgba(239, 68, 68, 0.2); color: #ef4444; }

        .video-container {
          position: relative;
          width: 100%;
          aspect-ratio: 16 / 9;
          background: #020617;
          display: flex;
          align-items: center;
          justify-content: center;
          overflow: hidden;
        }
        video {
          width: 100%;
          height: 100%;
          object-fit: cover;
          display: none;
          background: #000;
        }
        .overlay-idle, .overlay-connecting {
          position: absolute;
          inset: 0;
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          gap: 12px;
          text-align: center;
          padding: 20px;
        }
        .lens-graphic {
          width: 80px;
          height: 80px;
          border-radius: 50%;
          border: 3px solid #00d2ff;
          display: flex;
          align-items: center;
          justify-content: center;
          background: radial-gradient(circle, #0284c7 0%, #0369a1 45%, #082f49 100%);
          box-shadow: 0 0 25px rgba(0, 210, 255, 0.35);
        }
        .start-btn {
          background: #0284c7;
          color: #fff;
          border: none;
          padding: 10px 22px;
          border-radius: 8px;
          font-size: 14px;
          font-weight: 600;
          cursor: pointer;
          display: flex;
          align-items: center;
          gap: 8px;
          transition: background 0.2s, transform 0.1s;
          box-shadow: 0 4px 12px rgba(2, 132, 199, 0.4);
        }
        .start-btn:hover { background: #0369a1; }
        .start-btn:active { transform: scale(0.98); }

        .spinner {
          width: 44px;
          height: 44px;
          border: 4px solid rgba(255, 255, 255, 0.1);
          border-top-color: #f59e0b;
          border-radius: 50%;
          animation: spin 1s linear infinite;
        }
        @keyframes spin {
          to { transform: rotate(360deg); }
        }

        .controls {
          padding: 14px 18px;
          display: grid;
          grid-template-columns: 1fr 1fr 1fr;
          gap: 10px;
          background: rgba(255, 255, 255, 0.02);
          border-top: 1px solid rgba(255, 255, 255, 0.08);
        }
        .btn {
          padding: 11px 14px;
          border: none;
          border-radius: 8px;
          font-size: 13px;
          font-weight: 600;
          cursor: pointer;
          display: flex;
          align-items: center;
          justify-content: center;
          gap: 6px;
          transition: filter 0.2s, transform 0.1s;
        }
        .btn:hover { filter: brightness(1.15); }
        .btn:active { transform: scale(0.98); }
        .btn-door { background: #15803d; color: #fff; }
        .btn-gate { background: #b45309; color: #fff; }
        .btn-snapshot { background: #334155; color: #f8fafc; }
        .btn-stop { background: #b91c1c; color: #fff; }

        .timer-bar {
          height: 3px;
          background: #10b981;
          width: 0%;
          transition: width 1s linear;
        }
      </style>

      <ha-card>
        <div class="header">
          <div class="title">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M23 7l-7 5 7 5V7z"></path>
              <rect x="1" y="5" width="15" height="14" rx="2" ry="2"></rect>
            </svg>
            ${this._config.title}
            <span class="version-badge">v1.0.9</span>
          </div>
          <div id="statusBadge" class="badge badge-idle">${this._statusText}</div>
        </div>

        <div class="timer-bar" id="timerBar"></div>

        <div class="video-container">
          <video id="videoPlayer" autoplay playsinline muted></video>

          <div id="overlayIdle" class="overlay-idle">
            <div class="lens-graphic">
              <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2">
                <circle cx="12" cy="12" r="10"></circle>
                <circle cx="12" cy="12" r="3"></circle>
              </svg>
            </div>
            <div>
              <div style="font-size: 15px; font-weight: 500; color: #e2e8f0; margin-bottom: 4px;">Прямой видеопоток WebRTC</div>
              <div style="font-size: 12px; color: #64748b;">30 FPS | Сверхнизкая задержка | AWS Kinesis</div>
            </div>
            <button class="start-btn" id="startBtn">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"></path></svg>
              Включить камеру
            </button>
          </div>

          <div id="overlayConnecting" class="overlay-connecting" style="display: none;">
            <div class="spinner"></div>
            <div id="connectingStepTitle" style="font-size: 14px; font-weight: 500; color: #f59e0b;">[1/3] Запрос сессии облака...</div>
            <div id="connectingStepDetail" style="font-size: 12px; color: #94a3b8;">Инициализация WebRTC Kinesis...</div>
          </div>
        </div>

        <div class="controls">
          <button class="btn btn-door" id="doorBtn">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect>
              <path d="M7 11V7a5 5 0 0 1 10 0v4"></path>
            </svg>
            Дверь 1
          </button>
          <button class="btn btn-gate" id="gateBtn">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M3 21h18M5 21V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v16"></path>
            </svg>
            Ворота 2
          </button>
          <button class="btn btn-snapshot" id="snapBtn">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"></path>
              <circle cx="12" cy="13" r="4"></circle>
            </svg>
            Снимок
          </button>
        </div>
      </ha-card>
    `;

    this._bindEvents();
  }

  _bindEvents() {
    const root = this.shadowRoot;
    root.getElementById('startBtn').addEventListener('click', () => this._startStream());
    root.getElementById('doorBtn').addEventListener('click', () => this._unlockDoor());
    root.getElementById('gateBtn').addEventListener('click', () => this._unlockGate());
    root.getElementById('snapBtn').addEventListener('click', () => this._takeSnapshot());
  }

  _updateStatus(status, text) {
    this._status = status;
    this._statusText = text;
    const badge = this.shadowRoot.getElementById('statusBadge');
    if (badge) {
      badge.className = `badge badge-${status}`;
      badge.textContent = text;
    }
  }

  _updateConnectingStep(title, detail) {
    const root = this.shadowRoot;
    const titleEl = root.getElementById('connectingStepTitle');
    const detailEl = root.getElementById('connectingStepDetail');
    if (titleEl) titleEl.textContent = title;
    if (detailEl) detailEl.textContent = detail;
  }

  async _startStream() {
    if (this._status === 'connecting' || this._status === 'streaming') return;

    this._streamActive = true;
    this._updateStatus('connecting', 'ПОДКЛЮЧЕНИЕ...');
    this._updateConnectingStep('[1/3] Запрос сессии облака...', 'Получение параметров AWS Kinesis...');
    this.shadowRoot.getElementById('overlayIdle').style.display = 'none';
    this.shadowRoot.getElementById('overlayConnecting').style.display = 'flex';

    // 30-second watchdog fail-safe (allows cellular LTE modem time to warm up)
    clearTimeout(this._connectTimeout);
    this._connectTimeout = setTimeout(() => {
      if (this._status === 'connecting') {
        console.warn('[acoGO WebRTC] Connection timeout reached (30s)');
        this._updateStatus('error', 'ТАЙМАУТ');
        alert('Таймаут подключения (30с): вызывная панель не ответила на видео-вызов. Линия освобождена.');
        this._stopStream();
      }
    }, 30000);

    try {
      const resp = await this._hass.callWS({
        type: 'call_service',
        domain: 'acogo',
        service: 'start_webrtc_stream',
        service_data: {
          device_id: this._config ? this._config.device_id : undefined
        },
        return_response: true
      });

      const data = resp.response;
      if (!data || !data.wss_url) {
        throw new Error('Не получены параметры AWS Kinesis WebRTC');
      }

      await this._connectWebRtc(data);
    } catch (err) {
      console.error('[acoGO WebRTC Card] Error starting stream:', err);
      clearTimeout(this._connectTimeout);
      clearInterval(this._waitInterval);
      this._updateStatus('error', 'ОШИБКА');
      this.shadowRoot.getElementById('overlayConnecting').style.display = 'none';
      this.shadowRoot.getElementById('overlayIdle').style.display = 'flex';
      alert('Ошибка включения видеопотока: ' + (err.message || err));
      this._stopStream();
    }
  }

  async _connectWebRtc(data) {
    const { wss_url, ice_servers, channel_name, timeout } = data;
    const video = this.shadowRoot.getElementById('videoPlayer');

    this._updateConnectingStep('[2/3] Обмен SDP и ICE...', 'Инициализация WebRTC PeerConnection...');

    this._pendingIceCandidates = [];
    this._hasRemoteDescription = false;

    this._pc = new RTCPeerConnection({
      iceServers: ice_servers || []
    });

    // CRITICAL: Declare video receiver transceiver and force H.264 codec preferences
    const transceiver = this._pc.addTransceiver('video', { direction: 'recvonly' });
    if (typeof RTCRtpReceiver !== 'undefined' && typeof RTCRtpReceiver.getCapabilities === 'function') {
      const cap = RTCRtpReceiver.getCapabilities('video');
      if (cap && cap.codecs) {
        const h264Codecs = cap.codecs.filter(c => c.mimeType.toLowerCase() === 'video/h264');
        if (h264Codecs.length > 0 && typeof transceiver.setCodecPreferences === 'function') {
          try {
            transceiver.setCodecPreferences(h264Codecs);
            console.log('[acoGO WebRTC] Applied setCodecPreferences for H.264 codecs:', h264Codecs.length);
          } catch (e) {
            console.warn('[acoGO WebRTC] setCodecPreferences failed:', e);
          }
        }
      }
    }

    // Create DataChannel matching official acoGO Android/iOS app
    const chName = channel_name || `aco_${data.device_id}`;
    try {
      this._dataChannel = this._pc.createDataChannel(chName);
      this._dataChannel.onopen = () => {
        console.log('[acoGO WebRTC] DataChannel open, sending handshake');
        try {
          this._dataChannel.send(JSON.stringify({ network: { type: 'wifi' } }));
        } catch (e) {}
      };
      this._dataChannel.onmessage = (evt) => {
        console.log('[acoGO WebRTC] Message from master:', evt.data);
      };
    } catch (e) {
      console.warn('[acoGO WebRTC] DataChannel init error:', e);
    }

    this._pc.ondatachannel = (evt) => {
      const channel = evt.channel;
      channel.onopen = () => {
        try {
          channel.send(JSON.stringify({ network: { type: 'wifi' } }));
        } catch (e) {}
      };
      channel.onmessage = (msg) => {
        console.log('[acoGO WebRTC] Master channel message:', msg.data);
      };
    };

    this._pc.ontrack = (event) => {
      console.log('[acoGO WebRTC] Received remote track:', event.track.kind);
      this._updateConnectingStep('[3/3] Запуск видеопотока H.264...', 'Буферизация видеокадров');

      if (event.streams && event.streams[0]) {
        video.srcObject = event.streams[0];
      } else {
        if (!video.srcObject) {
          video.srcObject = new MediaStream();
        }
        video.srcObject.addTrack(event.track);
      }

      video.muted = true;
      video.play().catch(e => console.warn('[acoGO WebRTC] Video play error:', e));
      video.style.display = 'block';

      clearTimeout(this._connectTimeout);
      clearInterval(this._waitInterval);
      this.shadowRoot.getElementById('overlayConnecting').style.display = 'none';
      this.shadowRoot.getElementById('overlayIdle').style.display = 'none';
      this._updateStatus('streaming', 'ПРЯМОЙ ЭФИР');
      this._startCountdown(timeout || 45);
    };

    this._pc.onicecandidate = (event) => {
      if (event.candidate && this._ws && this._ws.readyState === WebSocket.OPEN) {
        const cand = event.candidate.toJSON ? event.candidate.toJSON() : {
          candidate: event.candidate.candidate,
          sdpMid: event.candidate.sdpMid,
          sdpMLineIndex: event.candidate.sdpMLineIndex,
          usernameFragment: event.candidate.usernameFragment
        };
        const msg = {
          action: 'ICE_CANDIDATE',
          messagePayload: safeBtoa(cand)
        };
        this._ws.send(JSON.stringify(msg));
      }
    };

    this._pc.oniceconnectionstatechange = () => {
      console.log('[acoGO WebRTC] ICE connection state:', this._pc.iceConnectionState);
      if (this._pc.iceConnectionState === 'connected' || this._pc.iceConnectionState === 'completed') {
        this._updateConnectingStep('[2/3] Обмен SDP и ICE...', 'ICE соединение установлено');
      } else if (this._pc.iceConnectionState === 'failed') {
        clearTimeout(this._connectTimeout);
        clearInterval(this._waitInterval);
        this._updateStatus('error', 'СБОЙ P2P');
        alert('Не удалось установить WebRTC P2P соединение с домофоном (ICE failed)');
        this._stopStream();
      }
    };

    this._ws = new WebSocket(wss_url);

    this._ws.onopen = async () => {
      try {
        console.log('[acoGO WebRTC] Signaling WebSocket open, creating offer...');
        this._updateConnectingStep('[2/3] Обмен SDP и ICE...', 'WS подключен, создание SDP Offer...');
        const offer = await this._pc.createOffer();
        await this._pc.setLocalDescription(offer);

        let sdpClean = compactH264Sdp(this._pc.localDescription.sdp);
        sdpClean = filterPrivateCandidates(sdpClean);

        const msg = {
          action: 'SDP_OFFER',
          messagePayload: safeBtoa({
            type: this._pc.localDescription.type,
            sdp: sdpClean
          })
        };
        this._ws.send(JSON.stringify(msg));

        // Start waiting countdown only AFTER the offer is successfully sent
        this._waitingSeconds = 0;
        this._updateConnectingStep('[2/3] Обмен SDP и ICE...', 'SDP Offer отправлен, ожидание домофона (0с / 30с)...');

        clearInterval(this._waitInterval);
        this._waitInterval = setInterval(() => {
          this._waitingSeconds++;
          if (this._status === 'connecting') {
            this._updateConnectingStep('[2/3] Обмен SDP и ICE...', `SDP Offer отправлен, ожидание домофона (${this._waitingSeconds}с / 30с)...`);
          }
        }, 1000);
      } catch (e) {
        console.error('[acoGO WebRTC] Error in onopen:', e);
        clearInterval(this._waitInterval);
        this._updateConnectingStep('[Ошибка]', `Сбой отправки оффера: ${e.message || e}`);
        this._stopStream();
      }
    };

    this._ws.onmessage = async (evt) => {
      if (!evt.data || !evt.data.trim()) {
        return;
      }
      try {
        const msg = JSON.parse(evt.data);
        console.log('[acoGO WebRTC] Received WS message type:', msg.messageType);

        if (msg.messageType === 'SDP_ANSWER') {
          clearInterval(this._waitInterval);
          console.log('[acoGO WebRTC] Received SDP_ANSWER');
          this._updateConnectingStep('[2/3] Обмен SDP и ICE...', 'SDP Answer получен, согласование ICE...');
          const payload = JSON.parse(atob(msg.messagePayload));
          await this._pc.setRemoteDescription(new RTCSessionDescription(payload));
          this._hasRemoteDescription = true;

          // Flush queued candidates safely
          console.log(`[acoGO WebRTC] Flushing ${this._pendingIceCandidates.length} pending ICE candidates`);
          for (const cand of this._pendingIceCandidates) {
            try {
              await this._pc.addIceCandidate(new RTCIceCandidate(cand));
            } catch (err) {
              console.warn('[acoGO WebRTC] Error adding queued ICE candidate:', err);
            }
          }
          this._pendingIceCandidates = [];
        } else if (msg.messageType === 'ICE_CANDIDATE') {
          const payload = JSON.parse(atob(msg.messagePayload));
          if (payload && (payload.candidate || payload.candidate === '')) {
            if (!this._hasRemoteDescription) {
              this._pendingIceCandidates.push(payload);
            } else {
              try {
                await this._pc.addIceCandidate(new RTCIceCandidate(payload));
              } catch (err) {
                console.warn('[acoGO WebRTC] Error adding ICE candidate:', err);
              }
            }
          }
        } else if (msg.messageType === 'STATUS_RESPONSE' || msg.statusResponse) {
          const st = msg.statusResponse || msg;
          console.warn('[acoGO WebRTC] Status response from Kinesis:', st);
          if (st.statusCode && st.statusCode >= 400) {
            clearInterval(this._waitInterval);
            clearTimeout(this._connectTimeout);
            this._updateConnectingStep('[Ошибка]', `Kinesis: ${st.description || st.statusCode}`);
            alert(`Ошибка AWS Kinesis Signaling: ${st.description || st.statusCode}`);
            this._stopStream();
          }
        }
      } catch (e) {
        console.warn('[acoGO WebRTC] Signaling parse error:', e);
      }
    };

    this._ws.onerror = (err) => {
      console.error('[acoGO WebRTC] WebSocket error:', err);
      clearInterval(this._waitInterval);
      this._updateConnectingStep('[Ошибка]', 'Ошибка WebSocket соединения с Kinesis');
    };

    this._ws.onclose = (evt) => {
      console.log('[acoGO WebRTC] Signaling WebSocket closed', evt.code, evt.reason);
      clearInterval(this._waitInterval);
      if (this._status === 'connecting') {
        this._updateConnectingStep('[Закрыт]', `WebSocket закрыт (${evt.code})`);
      }
    };
  }

  _startCountdown(seconds) {
    this._countdown = seconds;
    const bar = this.shadowRoot.getElementById('timerBar');
    bar.style.width = '100%';

    clearInterval(this._countdownInterval);
    this._countdownInterval = setInterval(() => {
      this._countdown--;
      const pct = (this._countdown / seconds) * 100;
      bar.style.width = `${pct}%`;
      if (this._countdown <= 0) {
        clearInterval(this._countdownInterval);
        this._stopStream();
      }
    }, 1000);
  }

  _stopStream() {
    clearTimeout(this._connectTimeout);
    clearInterval(this._waitInterval);
    clearInterval(this._countdownInterval);
    const bar = this.shadowRoot.getElementById('timerBar');
    if (bar) bar.style.width = '0%';

    if (this._dataChannel) {
      try { this._dataChannel.close(); } catch (e) {}
      this._dataChannel = null;
    }
    if (this._pc) {
      try { this._pc.close(); } catch (e) {}
      this._pc = null;
    }
    if (this._ws) {
      try { this._ws.close(); } catch (e) {}
      this._ws = null;
    }

    const video = this.shadowRoot.getElementById('videoPlayer');
    if (video) {
      video.srcObject = null;
      video.style.display = 'none';
    }

    const overlayIdle = this.shadowRoot.getElementById('overlayIdle');
    if (overlayIdle) overlayIdle.style.display = 'flex';
    const overlayConn = this.shadowRoot.getElementById('overlayConnecting');
    if (overlayConn) overlayConn.style.display = 'none';

    this._updateStatus('idle', 'ГОТОВ К ТРАНСЛЯЦИИ');

    // Notify backend to close session and release intercom line unconditionally
    if (this._streamActive) {
      this._streamActive = false;
      if (this._hass) {
        const devId = (this._config && this._config.device_id) ? this._config.device_id : undefined;
        this._hass.callService('acogo', 'stop_webrtc_stream', {
          device_id: devId
        }).catch(() => {});
      }
    }
  }

  _takeSnapshot() {
    const video = this.shadowRoot.getElementById('videoPlayer');
    if (!video || video.style.display === 'none' || !video.videoWidth) {
      alert('Сначала включите камеру, чтобы сделать снимок с живого видео!');
      return;
    }

    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

    const a = document.createElement('a');
    a.href = canvas.toDataURL('image/jpeg', 0.95);
    a.download = `acogo_snapshot_${Date.now()}.jpg`;
    a.click();
  }

  _unlockDoor() {
    const doorEntity = this._config.door_entity || 'lock.ulitsa_acogo_julianow_door_lock';
    if (this._hass && this._hass.states[doorEntity]) {
      this._hass.callService('lock', 'unlock', { entity_id: doorEntity });
    } else {
      this._hass.callService('button', 'press', { entity_id: 'button.ulitsa_acogo_julianow_open_door' }).catch(() => {
        alert('Сущность замка двери не найдена в Home Assistant');
      });
    }
  }

  _unlockGate() {
    const gateEntity = this._config.gate_entity || 'lock.ulitsa_acogo_julianow_gate_f2';
    if (this._hass && this._hass.states[gateEntity]) {
      this._hass.callService('lock', 'unlock', { entity_id: gateEntity });
    } else {
      this._hass.callService('button', 'press', { entity_id: 'button.ulitsa_acogo_julianow_open_gate' }).catch(() => {
        alert('Сущность замка ворот не найдена в Home Assistant');
      });
    }
  }
}

customElements.define('acogo-webrtc-card', AcoGoWebRtcCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: 'acogo-webrtc-card',
  name: 'acoGO! Live WebRTC Camera',
  description: 'Прямой видеопоток 30 FPS с домофона acoGO через браузерный WebRTC'
});
console.info('%c ACOGO-WEBRTC-CARD %c v1.0.9 Loaded ', 'background:#0284c7;color:#fff;font-weight:bold;', 'background:#0d121c;color:#10b981;');
