#!/usr/bin/env python3
"""
Home Assistant Add-on: Logitech Tap Scheduler Appspace Proxy (v1.2.0)
- Hijacks the Appspace WebView Card on Logitech Tap Scheduler (CollabOS)
- Provides full-screen Home Assistant dashboard with desktop resolution scaling
- Pull-down drawer with URL bar, Zoom presets, Brightness slider, Sleep (Tap-to-Wake),
  LED / Edge-Glow controls, Tap Web Settings proxy, and Native Appspace/Logitech Menu escape
- Exposes a local REST API (/api/state, /api/control) for the companion Home Assistant
  (HACS) integration (`tap_scheduler`) so Home Assistant can control Screen Power,
  Brightness, LEDs, Edge Glow, URL/Camera popups, and Device Menu actions natively.
"""
import base64
import datetime
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import socketserver
import ssl
import struct
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

OPTIONS_PATH = Path("/data/options.json")
DATA_DIR = Path("/data") if Path("/data").exists() else Path(__file__).resolve().parent / "data"
SHARE_DIR = Path("/share") if Path("/share").exists() else DATA_DIR
SSL_DIR = Path("/ssl") if Path("/ssl").exists() else DATA_DIR
APP_DIR = Path(__file__).resolve().parent

DEFAULT_OPTIONS = {
    "default_url": "http://homeassistant.local:8123",
    "default_zoom": 0.67,
    "auto_hide_seconds": 8,
    "disable_night_blanking": True,
    "ca_cert_base64": "",
    "ca_key_base64": "",
}


def load_options() -> dict:
    opts = dict(DEFAULT_OPTIONS)
    if OPTIONS_PATH.exists():
        try:
            loaded = json.loads(OPTIONS_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                opts.update(loaded)
        except Exception as e:
            print(f"[WARN] Failed to parse /data/options.json: {e}", flush=True)
    return opts


OPTIONS = load_options()
CERTS_DIR = DATA_DIR / "certs"
HOST_CERTS_DIR = CERTS_DIR / "hosts"
CERTS_DIR.mkdir(parents=True, exist_ok=True)
HOST_CERTS_DIR.mkdir(parents=True, exist_ok=True)

CA_KEY_PATH = CERTS_DIR / "local-root-ca.key"
CA_CRT_PATH = CERTS_DIR / "local-root-ca.crt"

# Live state shared between the Tap Scheduler WebView and Home Assistant Integration
state_lock = threading.Lock()
DEVICE_STATE = {
    "version": 1,
    "screen_on": True,
    "brightness": 100,  # 1..100
    "url": str(OPTIONS.get("default_url") or "http://homeassistant.local:8123"),
    "base_url": str(OPTIONS.get("default_url") or "http://homeassistant.local:8123"),
    "popup_expires_at": 0.0,
    "zoom": float(OPTIONS.get("default_zoom") or 0.67),
    "led_mode": "off",  # off, available (green), occupied (red), checkin (yellow), purple, blue
    "color_palette": "default",  # default or ada
    "glow_on": False,
    "glow_color": [168, 85, 247],  # RGB
    "glow_effect": "solid",  # solid, pulse, breathe
    "toast_text": "",
    "toast_id": 0,
    "pending_action": "",
    "PendingActionId": 0,
    "tap_ip": "",
    "last_seen": 0.0,
    "last_touch": 0.0,
    "appspace_capabilities": {},
}


def update_state( bump_version: bool = True, **kwargs) -> dict:
    with state_lock:
        for k, v in kwargs.items():
            DEVICE_STATE[k] = v
        if bump_version:
            DEVICE_STATE["version"] = int(DEVICE_STATE.get("version", 1)) + 1
        return dict(DEVICE_STATE)


def get_state_snapshot() -> dict:
    now = time.time()
    with state_lock:
        if DEVICE_STATE["popup_expires_at"] > 0 and now >= DEVICE_STATE["popup_expires_at"]:
            DEVICE_STATE["url"] = DEVICE_STATE["base_url"]
            DEVICE_STATE["popup_expires_at"] = 0.0
            DEVICE_STATE["version"] += 1
        snap = dict(DEVICE_STATE)
        snap["online"] = (now - snap["last_seen"]) < 180 if snap["last_seen"] > 0 else False
        return snap


def log_msg(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def send_appspace_ws_command(tap_ip: str, command: str) -> bool:
    """
    Connects to the Appspace App's local WebSocket remote control port (55537)
    on the Tap Scheduler and sends a command like '?remote=system' or '?system=restart'.
    """
    if not tap_ip:
        return False
    try:
        sock = socket.create_connection((tap_ip, 55537), timeout=3)
        ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {tap_ip}:55537\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        sock.sendall(handshake)
        resp = sock.recv(4096)
        if b"101" not in resp:
            sock.close()
            return False
        payload = command.encode("utf-8")
        mask_key = os.urandom(4)
        header = bytearray([0x81])  # FIN + Text frame
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask_key)
        masked = bytearray(payload[i] ^ mask_key[i % 4] for i in range(length))
        sock.sendall(header + masked)
        time.sleep(0.15)
        sock.close()
        log_msg(f"  [APPSPACE WS 55537] Sent '{command}' to {tap_ip}:55537")
        return True
    except Exception as e:
        log_msg(f"  [APPSPACE WS 55537] Could not send '{command}' to {tap_ip}:55537 ({e})")
        return False


def ensure_ca():
    """Load CA from HA options, /share, /ssl, /data/certs, or generate a new Root CA."""
    b64_crt = (OPTIONS.get("ca_cert_base64") or "").strip()
    b64_key = (OPTIONS.get("ca_key_base64") or "").strip()
    if b64_crt and b64_key:
        try:
            CA_CRT_PATH.write_bytes(base64.b64decode(b64_crt))
            CA_KEY_PATH.write_bytes(base64.b64decode(b64_key))
            log_msg("Loaded Root CA certificate & key from add-on configuration (ca_cert_base64 / ca_key_base64).")
        except Exception as e:
            log_msg(f"[WARN] Failed to decode ca_cert_base64 / ca_key_base64: {e}")

    if not (CA_CRT_PATH.exists() and CA_KEY_PATH.exists()):
        for folder in (SHARE_DIR, SSL_DIR):
            cand_crt = folder / "local-root-ca.crt"
            cand_key = folder / "local-root-ca.key"
            if cand_crt.exists() and cand_key.exists():
                shutil.copyfile(cand_crt, CA_CRT_PATH)
                shutil.copyfile(cand_key, CA_KEY_PATH)
                log_msg(f"Imported existing Root CA from {folder}/local-root-ca.crt & .key")
                break

    if not (CA_CRT_PATH.exists() and CA_KEY_PATH.exists()):
        log_msg("Generating new Local Root CA (TapScheduler Local Root CA)...")
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name(
            [
                x509.NameAttribute(NameOID.COUNTRY_NAME, "NL"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "TapScheduler Local CA"),
                x509.NameAttribute(NameOID.COMMON_NAME, "TapScheduler Local Root CA"),
            ]
        )
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=7))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        CA_KEY_PATH.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        CA_CRT_PATH.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        log_msg(f"Generated Root CA at {CA_CRT_PATH}")

    try:
        if SHARE_DIR.exists():
            shutil.copyfile(CA_CRT_PATH, SHARE_DIR / "local-root-ca.crt")
    except Exception:
        pass


ensure_ca()
ca_key = serialization.load_pem_private_key(CA_KEY_PATH.read_bytes(), password=None)
ca_cert = x509.load_pem_x509_certificate(CA_CRT_PATH.read_bytes())

cert_lock = threading.Lock()
cert_cache = {}


def get_ssl_context_for_host(hostname: str) -> ssl.SSLContext:
    hostname = hostname.split(":")[0].strip("[]").lower()
    with cert_lock:
        if hostname in cert_cache:
            return cert_cache[hostname]

        safe_name = re.sub(r"[^a-z0-9._-]", "_", hostname)
        cert_file = HOST_CERTS_DIR / f"{safe_name}.pem"

        if not cert_file.exists():
            host_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            subject = x509.Name(
                [
                    x509.NameAttribute(NameOID.ORGANIZATION_NAME, "TapScheduler Local Portal"),
                    x509.NameAttribute(NameOID.COMMON_NAME, hostname[:64]),
                ]
            )
            san_list = [x509.DNSName(hostname)]
            try:
                ip = ipaddress.ip_address(hostname)
                san_list.append(x509.IPAddress(ip))
            except ValueError:
                pass

            now = datetime.datetime.now(datetime.timezone.utc)
            cert = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(ca_cert.subject)
                .public_key(host_key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=7))
                .not_valid_after(now + datetime.timedelta(days=825))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(
                    x509.KeyUsage(
                        digital_signature=True,
                        content_commitment=False,
                        key_encipherment=True,
                        data_encipherment=False,
                        key_agreement=False,
                        key_cert_sign=False,
                        crl_sign=False,
                        encipher_only=False,
                        decipher_only=False,
                    ),
                    critical=True,
                )
                .add_extension(x509.SubjectAlternativeName(san_list), critical=False)
                .add_extension(
                    x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                    critical=False,
                )
                .add_extension(
                    x509.SubjectKeyIdentifier.from_public_key(host_key.public_key()),
                    critical=False,
                )
                .add_extension(
                    x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                    critical=False,
                )
                .sign(ca_key, hashes.SHA256())
            )
            pem_bytes = (
                cert.public_bytes(serialization.Encoding.PEM)
                + host_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.TraditionalOpenSSL,
                    encryption_algorithm=serialization.NoEncryption(),
                )
                + ca_cert.public_bytes(serialization.Encoding.PEM)
            )
            cert_file.write_bytes(pem_bytes)

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(cert_file))
        cert_cache[hostname] = ctx
        return ctx


def build_portal_html() -> str:
    snap = get_state_snapshot()
    default_url = snap["url"]
    default_zoom = snap["zoom"]
    auto_hide_ms = int(OPTIONS.get("auto_hide_seconds") or 8) * 1000
    default_zoom_pct = int(round(default_zoom * 100))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Tap Scheduler Browser</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{
    width: 100vw;
    height: 100vh;
    overflow: hidden;
    background: #000000;
    color: #f8fafc;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    user-select: none;
    -webkit-user-select: none;
  }}

  #frameWrap {{
    position: fixed;
    inset: 0;
    width: 100vw;
    height: 100vh;
    overflow: hidden;
    z-index: 1;
    background: #0f172a;
  }}
  #browserFrame {{
    position: absolute;
    top: 0;
    left: 0;
    border: none;
    background: #111827;
    transform-origin: 0 0;
    -webkit-transform-origin: 0 0;
  }}

  /* Software brightness dimmer overlay (pointer-events: none so touch passes through) */
  #dimmerOverlay {{
    position: fixed;
    inset: 0;
    z-index: 9000;
    background: #000000;
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.35s ease;
  }}

  /* Ambient Edge Glow / Alert Frame (pointer-events: none) */
  #edgeGlow {{
    position: fixed;
    inset: 0;
    z-index: 9001;
    pointer-events: none;
    opacity: 0;
    transition: opacity 0.3s ease, box-shadow 0.3s ease, border-color 0.3s ease;
    border: 6px solid transparent;
  }}
  @keyframes glowPulse {{
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.25; }}
  }}
  @keyframes glowBreathe {{
    0%, 100% {{ opacity: 0.95; }}
    50% {{ opacity: 0.45; }}
  }}
  #edgeGlow.pulse {{ animation: glowPulse 1.2s infinite ease-in-out; }}
  #edgeGlow.breathe {{ animation: glowBreathe 3.2s infinite ease-in-out; }}

  /* Fullscreen Sleep / Screen-Off Curtain (Tap anywhere to wake!) */
  #sleepCurtain {{
    position: fixed;
    inset: 0;
    z-index: 20000;
    background: #000000;
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.3s ease;
    display: flex;
    align-items: center;
    justify-content: center;
  }}
  #sleepCurtain.sleeping {{
    opacity: 1;
    pointer-events: auto;
    cursor: pointer;
  }}

  /* Floating Toast Notification */
  #toastBanner {{
    position: fixed;
    bottom: 24px;
    left: 50%;
    transform: translateX(-50%) translateY(120px);
    z-index: 15000;
    background: rgba(15, 23, 42, 0.95);
    color: #f8fafc;
    border: 1px solid #38bdf8;
    border-radius: 12px;
    padding: 12px 22px;
    font-size: 16px;
    font-weight: 600;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.7);
    transition: transform 0.3s cubic-bezier(0.22, 1, 0.36, 1);
    pointer-events: none;
  }}
  #toastBanner.show {{
    transform: translateX(-50%) translateY(0);
  }}

  /* Top-center pull-down gesture zone */
  #pullZone {{
    position: fixed;
    top: 0;
    left: 50%;
    transform: translateX(-50%);
    width: 320px;
    height: 22px;
    z-index: 9998;
    display: flex;
    align-items: flex-start;
    justify-content: center;
    padding-top: 5px;
    cursor: pointer;
    touch-action: none;
  }}
  #pullPill {{
    width: 56px;
    height: 5px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.28);
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.5);
    transition: opacity 0.25s, transform 0.2s;
  }}
  #pullZone:active #pullPill {{
    background: rgba(255, 255, 255, 0.75);
    transform: scaleX(1.15);
  }}

  #backdrop {{
    position: fixed;
    inset: 0;
    z-index: 9999;
    background: rgba(0, 0, 0, 0.35);
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.22s ease;
  }}
  #backdrop.open {{
    opacity: 1;
    pointer-events: auto;
  }}

  #drawer {{
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    z-index: 10000;
    background: rgba(15, 23, 42, 0.96);
    backdrop-filter: blur(10px);
    border-bottom: 1px solid #334155;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.65);
    transform: translateY(-110%);
    -webkit-transform: translateY(-110%);
    transition: transform 0.24s cubic-bezier(0.22, 1, 0.36, 1);
    padding: 10px 14px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }}
  #drawer.open {{
    transform: translateY(0);
    -webkit-transform: translateY(0);
  }}

  .toolbar-row {{
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }}
  .btn {{
    background: #334155;
    color: #f8fafc;
    border: 1px solid #475569;
    border-radius: 8px;
    padding: 7px 11px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
  }}
  .btn:active {{ background: #475569; }}
  .btn-primary {{ background: #2563eb; border-color: #3b82f6; }}
  .btn-ha {{ background: #0284c7; border-color: #38bdf8; }}
  .btn-sleep {{ background: #4f46e5; border-color: #818cf8; }}
  .url-input {{
    flex: 1;
    min-width: 180px;
    background: #090d16;
    color: #f8fafc;
    border: 1px solid #475569;
    border-radius: 8px;
    padding: 7px 11px;
    font-size: 13px;
    outline: none;
  }}
  .zoom-group {{
    display: flex;
    align-items: center;
    gap: 5px;
    flex-wrap: wrap;
  }}
  .label {{
    font-size: 12px;
    color: #94a3b8;
    font-weight: 600;
    margin-right: 2px;
  }}
  .chip {{
    background: #1e293b;
    color: #cbd5e1;
    border: 1px solid #334155;
    border-radius: 999px;
    padding: 4px 10px;
    font-size: 12px;
    cursor: pointer;
    white-space: nowrap;
  }}
  .chip:active, .chip.active {{
    background: #0284c7;
    color: #ffffff;
    border-color: #38bdf8;
  }}
  .slider {{
    width: 90px;
    accent-color: #38bdf8;
    cursor: pointer;
  }}
  .spacer {{ flex: 1; }}
</style>
</head>
<body>
  <div id="pullZone" title="Swipe down or tap for menu">
    <div id="pullPill"></div>
  </div>

  <div id="backdrop" onclick="closeDrawer()"></div>
  <div id="dimmerOverlay"></div>
  <div id="edgeGlow"></div>
  <div id="sleepCurtain" onclick="wakeScreen()" ontouchstart="wakeScreen()"></div>
  <div id="toastBanner"></div>

  <div id="drawer" onpointerdown="resetAutoHide()" onkeydown="resetAutoHide()">
    <!-- Row 1: Navigation & Sleep -->
    <div class="toolbar-row">
      <button class="btn btn-ha" onclick="loadUrl('{default_url}')">🏠 HA</button>
      <input id="urlBox" class="url-input" type="text" value="{default_url}"
             placeholder="Enter URL (https://...)"
             onkeydown="if(event.key==='Enter') navigateInput()">
      <button class="btn btn-primary" onclick="navigateInput()">Go</button>
      <button class="btn" onclick="reloadFrame()">↻</button>
      <button class="btn btn-sleep" onclick="sleepScreen()">🌙 Screen Off</button>
      <button class="btn" onclick="closeDrawer()">▲ Hide</button>
    </div>

    <!-- Row 2: Resolution / Zoom & Brightness Dimmer -->
    <div class="toolbar-row">
      <div class="zoom-group">
        <span class="label">Zoom:</span>
        <button class="btn" onclick="stepZoom(-0.05)">−</button>
        <span id="zoomLabel" class="label" style="min-width:38px;text-align:center;color:#f8fafc;">{default_zoom_pct}%</span>
        <button class="btn" onclick="stepZoom(0.05)">+</button>
        <span class="chip" id="z50" onclick="setZoom(0.50)">50% (1920p)</span>
        <span class="chip" id="z60" onclick="setZoom(0.60)">60% (1420p)</span>
        <span class="chip" id="z67" onclick="setZoom(0.67)">67% (1280p)</span>
        <span class="chip" id="z75" onclick="setZoom(0.75)">75%</span>
        <span class="chip" id="z100" onclick="setZoom(1.00)">100%</span>
      </div>
      <div class="spacer"></div>
      <div class="zoom-group">
        <span class="label">🔆 Brightness:</span>
        <input id="brightSlider" class="slider" type="range" min="5" max="100" value="100"
               oninput="setBrightness(this.value, true)">
        <span id="brightLabel" class="label" style="min-width:34px;color:#f8fafc;">100%</span>
      </div>
    </div>

    <!-- Row 3: LEDs / Edge Glow & Device Settings Escape -->
    <div class="toolbar-row">
      <div class="zoom-group">
        <span class="label">💡 LED / Glow:</span>
        <span class="chip" onclick="triggerLed('available', [34,197,94])">🟢 Green</span>
        <span class="chip" onclick="triggerLed('occupied', [239,68,68])">🔴 Red</span>
        <span class="chip" onclick="triggerLed('checkin', [234,179,8])">🟡 Yellow</span>
        <span class="chip" onclick="triggerLed('purple', [168,85,247])">🟣 Purple</span>
        <span class="chip" onclick="triggerLed('ada_available', [59,130,246])">🔵 Blue</span>
        <span class="chip" onclick="triggerLed('off', null)">⚫ Off</span>
      </div>
      <div class="spacer"></div>
      <div class="zoom-group">
        <span class="chip" onclick="loadUrl('https://tap-settings.local/')">⚙️ Tap Web Settings</span>
        <span class="chip" onclick="escapeToAppspaceMenu()">🔓 Logitech / Appspace Menu</span>
      </div>
    </div>
  </div>

  <div id="frameWrap">
    <iframe id="browserFrame" src="{default_url}"></iframe>
  </div>

  <script>
    const frame = document.getElementById('browserFrame');
    const urlBox = document.getElementById('urlBox');
    const drawer = document.getElementById('drawer');
    const backdrop = document.getElementById('backdrop');
    const pullZone = document.getElementById('pullZone');
    const zoomLabel = document.getElementById('zoomLabel');
    const dimmerOverlay = document.getElementById('dimmerOverlay');
    const edgeGlow = document.getElementById('edgeGlow');
    const sleepCurtain = document.getElementById('sleepCurtain');
    const brightSlider = document.getElementById('brightSlider');
    const brightLabel = document.getElementById('brightLabel');
    const toastBanner = document.getElementById('toastBanner');

    let currentZoom = {default_zoom};
    let currentBrightness = 100;
    let isSleeping = false;
    let lastStateVersion = 0;
    let lastToastId = 0;
    let lastActionId = 0;

    try {{
      const saved = parseFloat(localStorage.getItem('tap_browser_zoom'));
      if (!isNaN(saved) && saved >= 0.35 && saved <= 1.5) currentZoom = saved;
    }} catch (e) {{}}

    function pushTelemetry(payload) {{
      try {{
        fetch('/_telemetry', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify(payload)
        }}).catch(function() {{}});
      }} catch (e) {{}}
    }}

    function applyZoom(z, notifyServer) {{
      currentZoom = Math.round(Math.max(0.35, Math.min(1.5, z)) * 100) / 100;
      const invPct = (100 / currentZoom).toFixed(4) + '%';
      frame.style.width = invPct;
      frame.style.height = invPct;
      frame.style.transform = 'scale(' + currentZoom + ')';
      frame.style.webkitTransform = 'scale(' + currentZoom + ')';
      zoomLabel.textContent = Math.round(currentZoom * 100) + '%';
      try {{ localStorage.setItem('tap_browser_zoom', String(currentZoom)); }} catch (e) {{}}
      [50, 60, 67, 75, 100].forEach(function(pct) {{
        const el = document.getElementById('z' + pct);
        if (el) el.classList.toggle('active', Math.round(currentZoom * 100) === pct);
      }});
      if (notifyServer) pushTelemetry({{ zoom: currentZoom }});
    }}
    function setZoom(z) {{ applyZoom(z, true); resetAutoHide(); }}
    function stepZoom(delta) {{ applyZoom(currentZoom + delta, true); resetAutoHide(); }}

    function setBrightness(val, notifyServer) {{
      currentBrightness = Math.max(1, Math.min(100, parseInt(val, 10) || 100));
      brightSlider.value = currentBrightness;
      brightLabel.textContent = currentBrightness + '%';
      const dimOpacity = ((100 - currentBrightness) / 100) * 0.92;
      dimmerOverlay.style.opacity = dimOpacity.toFixed(3);
      if (notifyServer) {{
        resetAutoHide();
        pushTelemetry({{ brightness: currentBrightness }});
      }}
    }}

    function sleepScreen(notifyServer) {{
      isSleeping = true;
      sleepCurtain.classList.add('sleeping');
      closeDrawer();
      if (notifyServer !== false) pushTelemetry({{ screen_on: false }});
    }}

    function wakeScreen(notifyServer) {{
      if (!isSleeping && notifyServer === false) return;
      isSleeping = false;
      sleepCurtain.classList.remove('sleeping');
      if (notifyServer !== false) pushTelemetry({{ screen_on: true, touch: true }});
    }}

    function applyGlow(on, rgb, effect) {{
      if (!on || !rgb) {{
        edgeGlow.style.opacity = '0';
        edgeGlow.className = '';
        return;
      }}
      const c = 'rgb(' + rgb[0] + ',' + rgb[1] + ',' + rgb[2] + ')';
      edgeGlow.style.borderColor = c;
      edgeGlow.style.boxShadow = 'inset 0 0 36px ' + c + ', inset 0 0 12px ' + c;
      edgeGlow.style.opacity = '1';
      edgeGlow.className = (effect === 'pulse' || effect === 'breathe') ? effect : '';
    }}

    let toastTimer = null;
    function showToast(text) {{
      if (!text) return;
      toastBanner.textContent = text;
      toastBanner.classList.add('show');
      if (toastTimer) clearTimeout(toastTimer);
      toastTimer = setTimeout(function() {{
        toastBanner.classList.remove('show');
      }}, 6000);
    }}

    applyZoom(currentZoom, false);

    let hideTimer = null;
    function resetAutoHide() {{
      if (hideTimer) clearTimeout(hideTimer);
      hideTimer = setTimeout(closeDrawer, {auto_hide_ms});
    }}
    function openDrawer() {{
      drawer.classList.add('open');
      backdrop.classList.add('open');
      resetAutoHide();
    }}
    function closeDrawer() {{
      drawer.classList.remove('open');
      backdrop.classList.remove('open');
      if (hideTimer) {{ clearTimeout(hideTimer); hideTimer = null; }}
    }}

    let startY = null;
    pullZone.addEventListener('touchstart', function(e) {{
      if (e.touches && e.touches.length > 0) startY = e.touches[0].clientY;
    }}, {{ passive: true }});
    pullZone.addEventListener('touchmove', function(e) {{
      if (startY !== null && e.touches && e.touches.length > 0) {{
        if (e.touches[0].clientY - startY > 10) {{
          openDrawer();
          startY = null;
        }}
      }}
    }}, {{ passive: true }});
    pullZone.addEventListener('click', function() {{
      openDrawer();
    }});

    function normalize(u) {{
      u = u.trim();
      if (!u) return '';
      if (!/^https?:\\/\\//i.test(u)) u = 'https://' + u;
      return u;
    }}
    function loadUrl(u, fromServer) {{
      const target = normalize(u);
      if (!target) return;
      urlBox.value = target;
      if (frame.src !== target) frame.src = target;
      closeDrawer();
      if (!fromServer) pushTelemetry({{ url: target }});
    }}
    function reloadFrame() {{
      frame.src = urlBox.value || frame.src;
      closeDrawer();
    }}
    function navigateInput() {{ loadUrl(urlBox.value, false); }}

    // Appspace Host WebView Handshake & Hardware Bridge
    (function() {{
      const params = new URLSearchParams(window.location.search);
      const cardId = params.get('cardId') || '';
      let appWindow = null;
      let appOrigin = '*';

      function sendToHost(msgObj) {{
        msgObj.cardId = cardId;
        try {{
          if (appWindow) appWindow.postMessage(msgObj, appOrigin || '*');
        }} catch (e) {{}}
        try {{
          if (window.parent && window.parent !== window) window.parent.postMessage(msgObj, '*');
        }} catch (e) {{}}
        try {{
          if (window.opener) window.opener.postMessage(msgObj, '*');
        }} catch (e) {{}}
      }}

      window.addEventListener('message', function(event) {{
        if (!event || !event.data) return;
        const m = String(event.data.message || '').toLowerCase();
        if (event.source) {{
          appWindow = event.source;
          appOrigin = event.origin || '*';
        }}
        if (m === 'appspaceapp.webview.postmessage.init' || m === 'appspaceapp.mswebview.postmessage.init') {{
          sendToHost({{ message: 'onapiready' }});
          setTimeout(function() {{ sendToHost({{ message: 'loaded' }}); }}, 100);
        }} else if (m === 'api.init') {{
          sendToHost({{ message: 'loaded' }});
          pushTelemetry({{ appspace_init: event.data }});
        }} else if (m === 'onmodelupdate' || m === 'play') {{
          sendToHost({{ message: 'loaded' }});
        }}
      }}, false);

      sendToHost({{ message: 'onapiready' }});
      setTimeout(function() {{ sendToHost({{ message: 'loaded' }}); }}, 200);
      setTimeout(function() {{ sendToHost({{ message: 'loaded' }}); }}, 800);
      setTimeout(function() {{ sendToHost({{ message: 'loaded' }}); }}, 2000);

      // Trigger both Physical LED postMessages and On-Screen Edge Glow
      window.setPhysicalLedPostMessage = function(mode) {{
        const colorMap = {{
          available: '#00FF00',
          occupied: '#FF0000',
          checkin: '#FFFF00',
          ada_available: '#0000FF',
          ada_occupied: '#FF8000',
          purple: '#A855F7',
          off: '#000000'
        }};
        const hex = colorMap[mode] || '#000000';
        const statusName = (mode === 'occupied' || mode === 'ada_occupied') ? 'occupied'
                         : (mode === 'checkin') ? 'pending'
                         : (mode === 'off') ? 'off' : 'available';
        [
          {{ message: 'executedevice', action: 'setstatuslight', parameters: {{ status: statusName, color: hex }} }},
          {{ message: 'executedevice', action: 'statuslight', parameters: {{ status: statusName, color: hex }} }},
          {{ message: 'statuslight', status: statusName, color: hex }},
          {{ message: 'api.statuslight', status: statusName, color: hex }}
        ].forEach(sendToHost);
      }};

      window.triggerLed = function(mode, rgb) {{
        resetAutoHide();
        window.setPhysicalLedPostMessage(mode);
        if (mode === 'off' || !rgb) {{
          applyGlow(false, null, 'solid');
          pushTelemetry({{ led_mode: 'off', glow_on: false }});
        }} else {{
          applyGlow(true, rgb, 'solid');
          pushTelemetry({{ led_mode: mode, glow_on: true, glow_color: rgb }});
        }}
      }};

      // Escape to native Appspace / Logitech Device Menu (via local WS 55537 + server WS 55537 + executedevice)
      window.escapeToAppspaceMenu = function() {{
        closeDrawer();
        pushTelemetry({{ action: 'open_device_menu' }});
        try {{
          const ws = new WebSocket('ws://127.0.0.1:55537');
          ws.onopen = function() {{ ws.send('?remote=system'); setTimeout(function() {{ ws.close(); }}, 300); }};
        }} catch (e) {{}}
        ['pointer-events:none', 'width:0px', 'height:0px', 'display:none'].forEach(function(pair) {{
          const kv = pair.split(':');
          sendToHost({{
            message: 'executedevice',
            action: 'setcontainerstyle',
            card: 'webview',
            parameters: {{ property: kv[0], value: kv[1] }}
          }});
        }});
        sendToHost({{ message: 'error' }});
        sendToHost({{ message: 'complete' }});
      }};

      // Poll /_state every 1.5s so Home Assistant automations & entities update the screen in real time!
      setInterval(function() {{
        fetch('/_state')
          .then(function(r) {{ return r.json(); }})
          .then(function(st) {{
            if (!st || !st.version) return;
            if (st.version === lastStateVersion) return;
            lastStateVersion = st.version;

            if (st.screen_on === false && !isSleeping) sleepScreen(false);
            if (st.screen_on === true && isSleeping) wakeScreen(false);
            if (typeof st.brightness === 'number' && st.brightness !== currentBrightness) {{
              setBrightness(st.brightness, false);
            }}
            if (typeof st.zoom === 'number' && Math.abs(st.zoom - currentZoom) > 0.01) {{
              applyZoom(st.zoom, false);
            }}
            if (st.url && normalize(st.url) !== normalize(urlBox.value)) {{
              loadUrl(st.url, true);
            }}
            applyGlow(Boolean(st.glow_on), st.glow_color || [168, 85, 247], st.glow_effect || 'solid');
            if (st.led_mode) {{
              window.setPhysicalLedPostMessage(st.led_mode);
            }}
            if (st.toast_id && st.toast_id !== lastToastId && st.toast_text) {{
              lastToastId = st.toast_id;
              showToast(st.toast_text);
            }}
            if (st.PendingActionId && st.PendingActionId !== lastActionId) {{
              lastActionId = st.PendingActionId;
              if (st.pending_action === 'reload') reloadFrame();
              if (st.pending_action === 'open_device_menu') window.escapeToAppspaceMenu();
            }}
          }})
          .catch(function() {{}});
      }}, 1500);
    }})();
  </script>
</body>
</html>
"""


INTERCEPT_DOMAINS = (
    "app15.cloud.appspace.com",
    "appspaceusercontent.com",
    "portal.local",
    "tap-settings.local",
)


def should_passthrough_tls(host: str) -> bool:
    h = host.rsplit(":", 1)[0].strip("[]").lower()
    return not any(h == d or h.endswith("." + d) for d in INTERCEPT_DOMAINS)


def tunnel_sockets(sock1: socket.socket, sock2: socket.socket):
    def forward(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except Exception:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except Exception:
                pass

    t1 = threading.Thread(target=forward, args=(sock1, sock2), daemon=True)
    t2 = threading.Thread(target=forward, args=(sock2, sock1), daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=120)
    t2.join(timeout=120)
    try:
        sock2.close()
    except Exception:
        pass


def patch_appspace_script(raw_bytes: bytes) -> bytes:
    text = raw_bytes.decode("utf-8", errors="replace")
    if OPTIONS.get("disable_night_blanking", True):
        text = re.sub(
            r"<LayoutSchedule>\s*<LayoutScheduleID>7</LayoutScheduleID>.*?</LayoutSchedule>",
            "",
            text,
            flags=re.DOTALL,
        )
        text = text.replace("Display.Screen Burn Mode=1;", "Display.Screen Burn Mode=0;")
        text = text.replace("Display.Screen Burn Time=0:00, 6:00;", "Display.Screen Burn Time=;")
        text = re.sub(
            r"Player\.Configuration\.NonOperatingHours\.Schedule=.*?;",
            'Player.Configuration.NonOperatingHours.Schedule={"IsEnabled":false,"Daypartings":[]};',
            text,
        )
        text = text.replace(
            "Player.Configuration.OperatingHours.Schedule.IsEnabled=true;",
            "Player.Configuration.OperatingHours.Schedule.IsEnabled=false;",
        )
        text = re.sub(
            r"Player\.Configuration\.OperatingHours\.Schedule=.*?;",
            'Player.Configuration.OperatingHours.Schedule={"isEnabled":false,"daypartings":[]};',
            text,
        )

    # Enable Appspace local WebSocket remote control on port 55537 (?remote=system)
    if "Remote.Enable=" not in text:
        text = text.replace(
            "player.devicemenu.hotarea.enable=True;",
            "player.devicemenu.hotarea.enable=True;\r\nRemote.Enable=True;",
        )

    # Apply requested status light palette (default = Green/Red/Yellow, ada = Blue/Orange/Yellow)
    snap = get_state_snapshot()
    palette = snap.get("color_palette", "default")
    text = re.sub(r"player\.statuslight\.colorpalette=.*?;", f"player.statuslight.colorpalette={palette};", text)

    text = re.sub(
        r"<DATA>TSVAL-.*?-TSVAL</DATA>",
        "<DATA>TSVAL-11/11/2026 11:48:00 PM-TSVAL</DATA>",
        text,
    )
    log_msg("  [APPSPACE] Patched script: disabled night blanking & enabled Remote.Enable=True (port 55537)!")
    return text.encode("utf-8")


def read_chunked_body(rfile) -> bytes:
    chunks = []
    while True:
        line = rfile.readline(65536).decode("latin-1").strip()
        if not line:
            break
        chunk_len = int(line.split(";")[0], 16)
        if chunk_len == 0:
            rfile.readline(65536)
            break
        chunks.append(rfile.read(chunk_len))
        rfile.readline(65536)
    return b"".join(chunks)


def send_json_response(wfile, status_code: int, data: dict):
    payload = json.dumps(data).encode("utf-8")
    wfile.write(
        f"HTTP/1.1 {status_code} OK\r\n"
        f"Content-Type: application/json; charset=utf-8\r\n"
        f"Access-Control-Allow-Origin: *\r\n"
        f"Cache-Control: no-store\r\n"
        f"Content-Length: {len(payload)}\r\n"
        f"Connection: close\r\n\r\n".encode("latin-1")
        + payload
    )
    wfile.flush()


class ProxyHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            self.handle_one_connection(self.rfile, self.wfile, is_tls=False, tls_host=None)
        except Exception as e:
            log_msg(f"Connection closed/err from {self.client_address[0]}: {e}")

    def handle_one_connection(self, rfile, wfile, is_tls=False, tls_host=None):
        while True:
            req_line = rfile.readline(65536)
            if not req_line:
                return
            req_line_str = req_line.decode("latin-1").strip()
            if not req_line_str:
                return
            parts = req_line_str.split(" ")
            if len(parts) < 3:
                return
            method, target, version = parts[0], parts[1], parts[2]

            headers = {}
            raw_headers = []
            while True:
                hline = rfile.readline(65536).decode("latin-1")
                if not hline or hline in ("\r\n", "\n"):
                    break
                raw_headers.append(hline)
                if ":" in hline:
                    k, v = hline.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            host = headers.get("host", tls_host or "")
            client_ip = self.client_address[0]

            # Record Tap Scheduler IP when it talks to Appspace
            if "appspace" in host.lower():
                update_state(bump_version=False, tap_ip=client_ip, last_seen=time.time())

            if not target.startswith("/_state"):
                log_msg(f"{client_ip} -> {method} {target} (Host: {host}, TLS: {is_tls})")

            if method.upper() == "CONNECT":
                hp = target.rsplit(":", 1)
                connect_host = hp[0].strip("[]")
                connect_port = int(hp[1]) if len(hp) > 1 else 443

                if should_passthrough_tls(connect_host):
                    try:
                        remote = socket.create_connection((connect_host, connect_port), timeout=10)
                        wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                        wfile.flush()
                        tunnel_sockets(self.connection, remote)
                    except Exception as e:
                        log_msg(f"  [PASSTHROUGH ERR] {connect_host}:{connect_port} -> {e}")
                    return

                wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                wfile.flush()
                ctx = get_ssl_context_for_host(connect_host)
                tls_sock = ctx.wrap_socket(self.connection, server_side=True)
                self.connection = tls_sock
                new_rfile = tls_sock.makefile("rb", -1)
                new_wfile = tls_sock.makefile("wb", 0)
                self.handle_one_connection(new_rfile, new_wfile, is_tls=True, tls_host=connect_host)
                return

            if headers.get("upgrade", "").lower() == "websocket":
                connect_host = host.rsplit(":", 1)[0].strip("[]")
                connect_port = 443 if is_tls else 80
                raw_sock = socket.create_connection((connect_host, connect_port), timeout=10)
                if is_tls:
                    up_ctx = ssl.create_default_context()
                    up_sock = up_ctx.wrap_socket(raw_sock, server_hostname=connect_host)
                else:
                    up_sock = raw_sock
                up_sock.sendall(req_line + "".join(raw_headers).encode("latin-1") + b"\r\n")
                tunnel_sockets(self.connection, up_sock)
                return

            if headers.get("transfer-encoding", "").lower() == "chunked":
                body = read_chunked_body(rfile)
            else:
                content_len = int(headers.get("content-length", "0"))
                body = rfile.read(content_len) if content_len > 0 else b""

            path_only = urlparse(target).path.lower()

            # 1. Root CA download endpoint
            if path_only in ("/ca.crt", "/local-root-ca.crt"):
                crt_bytes = CA_CRT_PATH.read_bytes()
                wfile.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/x-x509-ca-cert\r\n"
                    b'Content-Disposition: attachment; filename="local-root-ca.crt"\r\n'
                    b"Content-Length: " + str(len(crt_bytes)).encode() + b"\r\n"
                    b"Connection: close\r\n\r\n" + crt_bytes
                )
                wfile.flush()
                return

            # 2. Live State & Control REST API (used by Tap Scheduler WebView AND Home Assistant Integration)
            if path_only in ("/_state", "/api/state"):
                if "appspaceusercontent.com" in host.lower():
                    update_state(bump_version=False, tap_ip=client_ip, last_seen=time.time())
                send_json_response(wfile, 200, get_state_snapshot())
                return

            if path_only == "/_telemetry" and method.upper() == "POST":
                try:
                    data = json.loads(body.decode("utf-8"))
                except Exception:
                    data = {}
                updates = {"tap_ip": client_ip, "last_seen": time.time()}
                if data.get("touch"):
                    updates["last_touch"] = time.time()
                for k in ("screen_on", "brightness", "zoom", "url", "led_mode", "glow_on", "glow_color"):
                    if k in data:
                        updates[k] = data[k]
                        if k == "url":
                            updates["base_url"] = data[k]
                if "appspace_init" in data:
                    updates["appspace_capabilities"] = data["appspace_init"]
                    log_msg(f"  [APPSPACE API.INIT TELEMETRY] {json.dumps(data['appspace_init'])[:600]}")
                if data.get("action") == "open_device_menu":
                    threading.Thread(
                        target=send_appspace_ws_command, args=(client_ip, "?remote=system"), daemon=True
                    ).start()
                send_json_response(wfile, 200, update_state(bump_version=True, **updates))
                return

            if path_only == "/api/control" and method.upper() == "POST":
                try:
                    cmd = json.loads(body.decode("utf-8"))
                except Exception:
                    cmd = {}
                log_msg(f"  [HA API CONTROL] {cmd}")
                updates = {}
                if "screen_on" in cmd:
                    updates["screen_on"] = bool(cmd["screen_on"])
                if "brightness" in cmd:
                    updates["brightness"] = max(1, min(100, int(cmd["brightness"])))
                if "zoom" in cmd:
                    updates["zoom"] = max(0.35, min(1.5, float(cmd["zoom"])))
                if "url" in cmd and cmd["url"]:
                    updates["url"] = str(cmd["url"])
                    duration = float(cmd.get("duration") or 0)
                    if duration > 0:
                        updates["popup_expires_at"] = time.time() + duration
                    else:
                        updates["base_url"] = str(cmd["url"])
                        updates["popup_expires_at"] = 0.0
                if "led_mode" in cmd:
                    mode = str(cmd["led_mode"]).lower()
                    updates["led_mode"] = mode
                    updates["color_palette"] = "ada" if mode.startswith("ada_") else "default"
                if "glow_on" in cmd:
                    updates["glow_on"] = bool(cmd["glow_on"])
                if "glow_color" in cmd and isinstance(cmd["glow_color"], list) and len(cmd["glow_color"]) == 3:
                    updates["glow_color"] = [int(x) for x in cmd["glow_color"]]
                if "glow_effect" in cmd:
                    updates["glow_effect"] = str(cmd["glow_effect"]).lower()
                if "toast" in cmd and cmd["toast"]:
                    with state_lock:
                         updates["toast_id"] = int(DEVICE_STATE.get("toast_id", 0)) + 1
                    updates["toast_text"] = str(cmd["toast"])

                action = str(cmd.get("action") or "").lower()
                if action:
                    snap = get_state_snapshot()
                    tap_ip = snap.get("tap_ip") or "192.168.178.178"
                    if action == "wake":
                        updates["screen_on"] = True
                    elif action == "sleep":
                        updates["screen_on"] = False
                    elif action in ("reload", "open_device_menu"):
                        with state_lock:
                            updates["PendingActionId"] = int(DEVICE_STATE.get("PendingActionId", 0)) + 1
                        updates["pending_action"] = action
                        if action == "open_device_menu":
                            threading.Thread(
                                target=send_appspace_ws_command, args=(tap_ip, "?remote=system"), daemon=True
                            ).start()
                    elif action == "restart_app":
                        threading.Thread(
                            target=send_appspace_ws_command, args=(tap_ip, "?system=restart"), daemon=True
                        ).start()
                    elif action == "resync_app":
                        threading.Thread(
                            target=send_appspace_ws_command, args=(tap_ip, "?system=resync"), daemon=True
                        ).start()

                new_state = update_state(bump_version=True, **updates)
                send_json_response(wfile, 200, new_state)
                return

            # 3. Reverse-proxy https://tap-settings.local/ to the Tap Scheduler's Local Network Access UI
            if "tap-settings.local" in host.lower():
                tap_ip = client_ip
                log_msg(f"  [TAP WEB SETTINGS] Proxying {method} {target} -> https://{tap_ip}:443{target}")
                req_headers = {
                    k: v
                    for k, v in headers.items()
                    if k not in ("transfer-encoding", "connection", "accept-encoding", "host", "origin", "referer")
                }
                req_headers["Host"] = tap_ip
                req_headers["Origin"] = f"https://{tap_ip}"
                req_headers["Referer"] = f"https://{tap_ip}/"
                req_headers["Connection"] = "close"
                if method.upper() in ("POST", "PUT"):
                    req_headers["Content-Length"] = str(len(body))
                conn = http.client.HTTPSConnection(
                    tap_ip, 443, context=ssl._create_unverified_context(), timeout=15
                )
                conn.request(
                    method,
                    target,
                    body=body if method.upper() in ("POST", "PUT") or body else None,
                    headers=req_headers,
                )
                resp = conn.getresponse()
                resp_body = resp.read()
                status = resp.status
                reason = resp.reason
                resp_headers = resp.getheaders()
                conn.close()
                out = [f"HTTP/1.1 {status} {reason}\r\n".encode("latin-1")]
                for hk, hv in resp_headers:
                    if hk.lower() in (
                        "transfer-encoding",
                        "content-length",
                        "connection",
                        "content-encoding",
                        "x-frame-options",
                        "content-security-policy",
                    ):
                        continue
                    out.append(f"{hk}: {hv}\r\n".encode("latin-1"))
                out.append(f"Content-Length: {len(resp_body)}\r\n".encode("latin-1"))
                out.append(b"Connection: close\r\n\r\n")
                out.append(resp_body)
                wfile.write(b"".join(out))
                wfile.flush()
                return

            # 4. Appspace & AppspaceUserContent HTTPS traffic
            if "appspace.com" in host.lower() or "appspaceusercontent.com" in host.lower():
                if "appspaceusercontent.com" in host.lower() and "/index.html" in target.lower():
                    log_msg(f"  [APPSPACE CARD HIJACK] Serving Local Browser UI as index.html for {target}")
                    payload = build_portal_html().encode("utf-8")
                    wfile.write(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: text/html; charset=utf-8\r\n"
                        b"Cache-Control: no-store\r\n"
                        b"Content-Length: " + str(len(payload)).encode() + b"\r\n"
                        b"Connection: close\r\n\r\n" + payload
                    )
                    wfile.flush()
                    return

                connect_host = host.rsplit(":", 1)[0].strip("[]")
                connect_port = 443 if is_tls else 80
                req_headers = {
                    k: v
                    for k, v in headers.items()
                    if k not in ("transfer-encoding", "connection", "accept-encoding")
                }
                req_headers["Connection"] = "close"
                if method.upper() in ("POST", "PUT"):
                    req_headers["Content-Length"] = str(len(body))

                if is_tls:
                    conn = http.client.HTTPSConnection(
                        connect_host, connect_port, context=ssl.create_default_context(), timeout=20
                    )
                else:
                    conn = http.client.HTTPConnection(connect_host, connect_port, timeout=20)

                conn.request(
                    method,
                    target,
                    body=body if method.upper() in ("POST", "PUT") or body else None,
                    headers=req_headers,
                )
                resp = conn.getresponse()
                resp_body = resp.read()
                status = resp.status
                reason = resp.reason
                resp_headers = resp.getheaders()
                conn.close()
                log_msg(f"  [APPSPACE UPSTREAM] {method} {target} -> {status} ({len(resp_body)} bytes)")

                if "configscripthandler.ashx" in target.lower() and "operation=timestamp" in target.lower():
                    resp_body = b"11/11/2026 11:48:00 PM"

                if "configscripthandler.ashx" in target.lower() and "operation=script" in target.lower():
                    tpl_path = APP_DIR / "appspace-script-template.txt"
                    if b"10320724" not in resp_body and tpl_path.exists():
                        log_msg("  [APPSPACE] Upstream script has no WebView card -> injecting saved template!")
                        resp_body = tpl_path.read_bytes()
                        m = re.search(r"[?&]id=([0-9a-fA-F-]+)", target)
                        if m:
                            new_id = m.group(1).encode("utf-8")
                            resp_body = resp_body.replace(b"a94e52af-472c-473e-8ccf-6cae14f6daa5", new_id)
                    resp_body = patch_appspace_script(resp_body)

                out = [f"HTTP/1.1 {status} {reason}\r\n".encode("latin-1")]
                for hk, hv in resp_headers:
                    if hk.lower() in ("transfer-encoding", "content-length", "connection", "content-encoding"):
                        continue
                    out.append(f"{hk}: {hv}\r\n".encode("latin-1"))
                out.append(f"Content-Length: {len(resp_body)}\r\n".encode("latin-1"))
                out.append(b"Connection: close\r\n\r\n")
                out.append(resp_body)
                wfile.write(b"".join(out))
                wfile.flush()
                return

            # 5. Any direct HTTP request to the proxy itself -> serve the Portal UI
            log_msg(f"  [LOCAL PORTAL] Serving Browser UI for {host}{target}")
            payload = build_portal_html().encode("utf-8")
            wfile.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/html; charset=utf-8\r\n"
                b"Cache-Control: no-store\r\n"
                b"Content-Length: " + str(len(payload)).encode() + b"\r\n"
                b"Connection: close\r\n\r\n" + payload
            )
            wfile.flush()
            return


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    server = ThreadedTCPServer(("0.0.0.0", 8080), ProxyHandler)
    log_msg(
        f"Logitech Tap Scheduler Appspace Proxy Add-on v1.2.0 listening on 0.0.0.0:8080 "
        f"(default_url={OPTIONS.get('default_url')}, default_zoom={OPTIONS.get('default_zoom')})"
    )
    server.serve_forever()
