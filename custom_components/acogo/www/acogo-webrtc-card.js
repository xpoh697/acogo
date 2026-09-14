/**
 * acoGO! Live WebRTC Camera Card for Home Assistant Lovelace
 * Direct browser-native WebRTC streaming from ACO GO! 2.0 intercoms via AWS Kinesis Video Streams.
 */

class AcoGoWebRtcCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._pc = null;
    this._ws = null;
    this._streamTimer = null;
    this._countdown = 0;
    this._countdownInterval = null;
    this._status = 'idle'; // 'idle', 'connecting', 'streaming', 'error'
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
      device_id: '',
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
    if (!this.shadowRoot) return;

    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
        }
        ha-card {
          background: #121826;
          border-radius: 16px;
          overflow: hidden;
          box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
          color: #ffffff;
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        }
        .header {
          display: flex;
          justify-content: space-between;
          align-items: center;
          padding: 16px 20px;
          background: #0d121c;
          border-bottom: 1px solid #1f293d;
        }
        .title {
          font-size: 16px;
          font-weight: 600;
          letter-spacing: 0.5px;
          color: #e2e8f0;
          display: flex;
          align-items: center;
          gap: 8px;
        }
        .badge {
          font-size: 12px;
          font-weight: 600;
          padding: 4px 10px;
          border-radius: 12px;
          text-transform: uppercase;
        }
        .badge-idle { background: #1e293b; color: #94a3b8; }
        .badge-connecting { background: #78350f; color: #f59e0b; animation: pulse 1.5s infinite; }
        .badge-streaming { background: #064e3b; color: #10b981; }
        .badge-error { background: #7f1d1d; color: #ef4444; }

        @keyframes pulse {
          0% { opacity: 0.6; }
          50% { opacity: 1; }
          100% { opacity: 0.6; }
        }

        .video-container {
          position: relative;
          width: 100%;
          aspect-ratio: 16 / 9;
          background: #090d16;
          display: flex;
          align-items: center;
          justify-content: center;
          overflow: hidden;
        }
        video {
          width: 100%;
          height: 100%;
          object-fit: contain;
          display: none;
        }
        .overlay-idle, .overlay-connecting {
          display: flex;
          flex-direction: column;
          align-items: center;
          justify-content: center;
          gap: 16px;
          color: #94a3b8;
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
          background: radial-gradient(circle, #00d2ff 0%, #1c263a 70%);
          box-shadow: 0 0 20px rgba(0, 210, 255, 0.3);
        }
        .start-btn {
          background: #0284c7;
          color: #ffffff;
          border: none;
          padding: 10px 24px;
          font-size: 15px;
          font-weight: 600;
          border-radius: 8px;
          cursor: pointer;
          transition: background 0.2s, transform 0.1s;
          display: flex;
          align-items: center;
          gap: 8px;
        }
        .start-btn:hover { background: #0369a1; }
        .start-btn:active { transform: scale(0.98); }

        .spinner {
          width: 44px;
          height: 44px;
          border: 4px solid rgba(255, 255, 255, 0.1);
          border-top: 4px solid #f59e0b;
          border-radius: 50%;
          animation: spin 1s linear infinite;
        }
        @keyframes spin {
          0% { transform: rotate(0deg); }
          100% { transform: rotate(360deg); }
        }

        .controls {
          display: grid;
          grid-template-columns: 1fr 1fr 1fr;
          gap: 10px;
          padding: 16px;
          background: #0d121c;
          border-top: 1px solid #1f293d;
        }
        .btn {
          padding: 10px 14px;
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
          </div>
          <div id="statusBadge" class="badge badge-idle">${this._statusText}</div>
        </div>

        <div class="timer-bar" id="timerBar"></div>

        <div class="video-container">
          <video id="videoPlayer" autoplay playsinline controls></video>

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
            <div style="font-size: 14px; font-weight: 500; color: #f59e0b;">Установка защищенного P2P WebRTC соединения...</div>
            <div style="font-size: 12px; color: #94a3b8;">Согласование H.264 видеопотока с домофоном</div>
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

  async _startStream() {
    if (this._status === 'connecting' || this._status === 'streaming') return;

    this._updateStatus('connecting', 'ПОДКЛЮЧЕНИЕ...');
    this.shadowRoot.getElementById('overlayIdle').style.display = 'none';
    this.shadowRoot.getElementById('overlayConnecting').style.display = 'flex';

    try {
      const resp = await this._hass.callWS({
        type: 'call_service',
        domain: 'acogo',
        service: 'start_webrtc_stream',
        service_data: {
          device_id: this._config.device_id
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
      this._updateStatus('error', 'ОШИБКА');
      this.shadowRoot.getElementById('overlayConnecting').style.display = 'none';
      this.shadowRoot.getElementById('overlayIdle').style.display = 'flex';
      alert('Ошибка включения видеопотока: ' + (err.message || err));
      this._stopStream();
    }
  }

  async _connectWebRtc(data) {
    const { wss_url, ice_servers, timeout } = data;
    const video = this.shadowRoot.getElementById('videoPlayer');

    this._pc = new RTCPeerConnection({
      iceServers: ice_servers || []
    });

    this._pc.addTransceiver('video', { direction: 'recvonly' });
    this._pc.addTransceiver('audio', { direction: 'recvonly' });

    this._pc.ontrack = (event) => {
      console.log('[acoGO WebRTC] Received remote track:', event.track.kind);
      if (event.streams && event.streams[0]) {
        video.srcObject = event.streams[0];
        video.style.display = 'block';
        this.shadowRoot.getElementById('overlayConnecting').style.display = 'none';
        this.shadowRoot.getElementById('overlayIdle').style.display = 'none';
        this._updateStatus('streaming', 'ПРЯМОЙ ЭФИР');
        this._startCountdown(timeout || 45);
      }
    };

    this._ws = new WebSocket(wss_url);

    this._pc.onicecandidate = (event) => {
      if (event.candidate && this._ws && this._ws.readyState === WebSocket.OPEN) {
        const msg = {
          action: 'ICE_CANDIDATE',
          messagePayload: btoa(JSON.stringify(event.candidate))
        };
        this._ws.send(JSON.stringify(msg));
      }
    };

    this._ws.onopen = async () => {
      console.log('[acoGO WebRTC] Signaling WebSocket open, creating offer...');
      const offer = await this._pc.createOffer();
      await this._pc.setLocalDescription(offer);

      const msg = {
        action: 'SDP_OFFER',
        messagePayload: btoa(JSON.stringify({
          type: this._pc.localDescription.type,
          sdp: this._pc.localDescription.sdp
        }))
      };
      this._ws.send(JSON.stringify(msg));
    };

    this._ws.onmessage = async (evt) => {
      try {
        const msg = JSON.parse(evt.data);
        if (msg.messageType === 'SDP_ANSWER') {
          const payload = JSON.parse(atob(msg.messagePayload));
          await this._pc.setRemoteDescription(new RTCSessionDescription(payload));
        } else if (msg.messageType === 'ICE_CANDIDATE') {
          const payload = JSON.parse(atob(msg.messagePayload));
          if (payload.candidate) {
            await this._pc.addIceCandidate(new RTCIceCandidate(payload));
          }
        }
      } catch (e) {
        console.warn('[acoGO WebRTC] Signaling parse error:', e);
      }
    };

    this._ws.onerror = (err) => {
      console.error('[acoGO WebRTC] WebSocket error:', err);
    };

    this._ws.onclose = () => {
      console.log('[acoGO WebRTC] Signaling WebSocket closed');
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
    clearInterval(this._countdownInterval);
    const bar = this.shadowRoot.getElementById('timerBar');
    if (bar) bar.style.width = '0%';

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

    // Notify backend to close session
    if (this._hass) {
      this._hass.callService('acogo', 'stop_webrtc_stream', {
        device_id: this._config.device_id
      }).catch(() => {});
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

    const dataUrl = canvas.toDataURL('image/jpeg', 0.95);
    const link = document.createElement('a');
    link.href = dataUrl;
    link.download = `acogo_snapshot_${new Date().toISOString().replace(/[:.]/g, '-')}.jpg`;
    link.click();
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
console.info('%c ACOGO-WEBRTC-CARD %c v1.0.2 Loaded ', 'background:#0284c7;color:#fff;font-weight:bold;', 'background:#0d121c;color:#10b981;');
