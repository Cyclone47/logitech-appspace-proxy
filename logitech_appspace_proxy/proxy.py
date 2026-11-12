#!/usr/bin/env python3
"""
Home Assistant Add-on: Logitech Tap Scheduler Appspace Proxy (v1.3.0)
- Hijacks the Appspace WebView Card on Logitech Tap Scheduler (CollabOS)
- Provides full-screen Home Assistant dashboard with desktop resolution scaling
- Clean, compact pull-down bar with unfoldable Zoom dropdown & expandable Controls tray
- Hidden features:
  - Auto-Sleep Inactivity Timer (Tap-to-Wake)
  - Screensaver modes (Pure Black, Floating Digital Clock, Dimmed 5%)
  - Anti-Burn-In Pixel Shifter
  - Visual Strobe / Flash Alert + Toast Notifications
  - Native Logitech / Appspace Device Menu escape (WebSocket 55537 + container exit hooks)
  - Built-in reverse proxy for Tap CollabOS Web Settings (https://tap-settings.local/)
  - Dynamic timestamp & ApplicationProperties injection for Remote.Enable=True and LED palettes
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
    "tap_ip": "192.168.178.178",
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


def make_appspace_timestamp() -> str:
    return datetime.datetime.now().strftime("%m/%d/%Y %I:%M:%S %p")


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
    "led_mode": "off",  # off, available (green), occupied (red), checkin (yellow), purple, ada_available (blue), ada_occupied (orange)
    "color_palette": "default",  # default or ada
    "glow_on": False,
    "glow_color": [168, 85, 247],  # RGB
    "glow_effect": "solid",  # solid, pulse, breathe, flash
    "auto_sleep_minutes": 0,  # 0 = disabled, or 1, 5, 10, 15, 30, 60
    "screensaver_mode": "black",  # black, clock, dim
    "burn_in_protection": True,
    "toast_text": "",
    "toast_id": 0,
    "toast_duration": 6,
    "flash_id": 0,
    "flash_color": [239, 68, 68],
    "pending_action": "",
    "PendingActionId": 0,
    "tap_ip": str(OPTIONS.get("tap_ip") or "192.168.178.178"),
    "last_seen": 0.0,
    "last_touch": 0.0,
    "script_timestamp": make_appspace_timestamp(),
    "appspace_capabilities": {},
    "appspace_host_probe": {},
}


def update_state(bump_version: bool = True, **kwargs) -> dict:
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
        snap["touched_recently"] = (now - snap["last_touch"]) < 60 if snap["last_touch"] > 0 else False
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
    transition: transform 0.4s ease;
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

  /* Software brightness dimmer overlay */
  #dimmerOverlay {{
    position: fixed;
    inset: 0;
    z-index: 9000;
    background: #000000;
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.35s ease;
  }}

  /* Ambient Edge Glow / Alert Frame */
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
    50% {{ opacity: 0.2; }}
  }}
  @keyframes glowBreathe {{
    0%, 100% {{ opacity: 0.95; }}
    50% {{ opacity: 0.4; }}
  }}
  @keyframes glowFlash {{
    0%, 100% {{ opacity: 1; border-width: 14px; }}
    25%, 75% {{ opacity: 0.1; border-width: 4px; }}
    50% {{ opacity: 1; border-width: 18px; }}
  }}
  #edgeGlow.pulse {{ animation: glowPulse 1.1s infinite ease-in-out; }}
  #edgeGlow.breathe {{ animation: glowBreathe 3.2s infinite ease-in-out; }}
  #edgeGlow.flash {{ animation: glowFlash 0.6s 6 ease-in-out; }}

  /* Fullscreen Sleep / Screensaver Curtain (Tap anywhere to wake!) */
  #sleepCurtain {{
    position: fixed;
    inset: 0;
    z-index: 20000;
    background: #000000;
    display: none;
    align-items: center;
    justify-content: center;
    flex-direction: column;
    cursor: pointer;
  }}
  #sleepCurtain.sleeping {{
    display: flex;
  }}
  #sleepCurtain.dim-mode {{
    background: rgba(0, 0, 0, 0.94);
  }}
  #sleepClock {{
    display: none;
    text-align: center;
    color: rgba(248, 250, 252, 0.45);
    font-weight: 300;
    pointer-events: none;
    transition: transform 1.5s ease;
  }}
  #sleepClock .time {{
    font-size: 84px;
    letter-spacing: 2px;
    font-variant-numeric: tabular-nums;
  }}
  #sleepClock .date {{
    font-size: 20px;
    margin-top: 6px;
    color: rgba(148, 163, 184, 0.45);
  }}

  /* Toast Notification Banner */
  #toastBanner {{
    position: fixed;
    bottom: 28px;
    left: 50%;
    transform: translate(-50%, 130%);
    z-index: 15000;
    background: rgba(15, 23, 42, 0.96);
    border: 1px solid rgba(56, 189, 248, 0.55);
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.75);
    color: #f8fafc;
    padding: 14px 28px;
    border-radius: 999px;
    font-size: 16px;
    font-weight: 600;
    transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1), opacity 0.3s ease;
    opacity: 0;
    pointer-events: none;
    max-width: 85vw;
    text-align: center;
  }}
  #toastBanner.show {{
    transform: translate(-50%, 0);
    opacity: 1;
  }}

  /* Top-center pull-down gesture zone */
  #pullZone {{
    position: fixed;
    top: 0;
    left: 35%;
    width: 30%;
    height: 22px;
    z-index: 10001;
    display: flex;
    align-items: flex-start;
    justify-content: center;
    padding-top: 4px;
    cursor: pointer;
    touch-action: none;
  }}
  #pullPill {{
    width: 64px;
    height: 5px;
    border-radius: 999px;
    background: rgba(255, 255, 255, 0.24);
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.45);
    transition: opacity 0.25s, transform 0.25s, background 0.25s;
  }}
  #pullZone:hover #pullPill, #pullZone:active #pullPill {{
    background: rgba(56, 189, 248, 0.85);
    transform: scaleX(1.15);
  }}

  /* Slide-down menu bar */
  #drawer {{
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    z-index: 10002;
    background: rgba(15, 23, 42, 0.96);
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    border-bottom: 1px solid #334155;
    padding: 10px 14px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    transform: translateY(-115%);
    transition: transform 0.25s cubic-bezier(0.16, 1, 0.3, 1);
    box-shadow: 0 12px 32px rgba(0, 0, 0, 0.65);
  }}
  #drawer.open {{
    transform: translateY(0);
  }}

  #backdrop {{
    position: fixed;
    inset: 0;
    z-index: 10000;
    background: rgba(0, 0, 0, 0.25);
    display: none;
  }}
  #backdrop.open {{
    display: block;
  }}

  .toolbar-row {{
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    position: relative;
  }}
  .btn {{
    background: #1e293b;
    color: #f8fafc;
    border: 1px solid #475569;
    border-radius: 8px;
    padding: 7px 12px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }}
  .btn:active, .btn.active {{ background: #334155; border-color: #38bdf8; }}
  .btn-primary {{ background: #2563eb; border-color: #3b82f6; }}
  .btn-ha {{ background: #0284c7; border-color: #38bdf8; }}
  .btn-sleep {{ background: #4f46e5; border-color: #818cf8; }}
  .url-input {{
    flex: 1;
    min-width: 160px;
    background: #090d16;
    color: #f8fafc;
    border: 1px solid #475569;
    border-radius: 8px;
    padding: 7px 11px;
    font-size: 13px;
    outline: none;
  }}

  /* Unfoldable Zoom Popover */
  .dropdown-wrap {{
    position: relative;
    display: inline-block;
  }}
  .popover {{
    position: absolute;
    top: calc(100% + 6px);
    right: 0;
    min-width: 220px;
    background: #0f172a;
    border: 1px solid #475569;
    border-radius: 10px;
    padding: 8px;
    box-shadow: 0 14px 32px rgba(0, 0, 0, 0.8);
    display: none;
    flex-direction: column;
    gap: 5px;
    z-index: 10010;
  }}
  .popover.open {{
    display: flex;
  }}
  .popover-stepper {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: #1e293b;
    border-radius: 8px;
    padding: 4px 6px;
    margin-bottom: 4px;
  }}
  .popover-item {{
    background: transparent;
    color: #cbd5e1;
    border: 1px solid transparent;
    border-radius: 7px;
    padding: 6px 10px;
    font-size: 12px;
    font-weight: 600;
    text-align: left;
    cursor: pointer;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }}
  .popover-item:hover, .popover-item.active {{
    background: #0284c7;
    color: #ffffff;
    border-color: #38bdf8;
  }}

  /* Expandable Controls Tray */
  #moreTray {{
    display: none;
    flex-direction: column;
    gap: 8px;
    padding-top: 8px;
    border-top: 1px solid rgba(71, 85, 105, 0.45);
  }}
  #moreTray.open {{
    display: flex;
  }}

  .control-group {{
    display: flex;
    align-items: center;
    gap: 6px;
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
    padding: 5px 11px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
  }}
  .chip:active, .chip.active {{
    background: #0284c7;
    color: #ffffff;
    border-color: #38bdf8;
  }}
  .slider {{
    width: 110px;
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
  <div id="sleepCurtain" onclick="wakeScreen()" ontouchstart="wakeScreen()">
    <div id="sleepClock">
      <div class="time" id="clockTime">00:00</div>
      <div class="date" id="clockDate"></div>
    </div>
  </div>
  <div id="toastBanner"></div>

  <div id="drawer" onpointerdown="resetAutoHide()" onkeydown="resetAutoHide()">
    <!-- Primary Compact Row -->
    <div class="toolbar-row">
      <button class="btn btn-ha" onclick="loadUrl('{default_url}')">🏠 HA</button>
      <input id="urlBox" class="url-input" type="text" value="{default_url}"
             placeholder="Enter URL (https://...)"
             onkeydown="if(event.key==='Enter') navigateInput()">
      <button class="btn btn-primary" onclick="navigateInput()">Go</button>
      <button class="btn" onclick="reloadFrame()" title="Reload">↻</button>

      <!-- Single Unfoldable Zoom Setting -->
      <div class="dropdown-wrap">
        <button class="btn" id="zoomBtn" onclick="toggleZoomMenu(event)">
          🔍 <span id="zoomBtnText">{default_zoom_pct}%</span> ▾
        </button>
        <div class="popover" id="zoomPopover" onclick="event.stopPropagation()">
          <div class="popover-stepper">
            <button class="btn" style="padding:4px 10px" onclick="stepZoom(-0.05)">−</button>
            <span id="zoomLabel" style="font-size:13px;font-weight:700;color:#f8fafc;">{default_zoom_pct}%</span>
            <button class="btn" style="padding:4px 10px" onclick="stepZoom(0.05)">+</button>
          </div>
          <button class="popover-item" id="z50" onclick="setZoom(0.50)"><span>50%</span><span style="opacity:0.75">1706×1066</span></button>
          <button class="popover-item" id="z60" onclick="setZoom(0.60)"><span>60%</span><span style="opacity:0.75">1422×888</span></button>
          <button class="popover-item" id="z67" onclick="setZoom(0.67)"><span>67% ★</span><span style="opacity:0.75">1280×800</span></button>
          <button class="popover-item" id="z75" onclick="setZoom(0.75)"><span>75%</span><span style="opacity:0.75">1138×711</span></button>
          <button class="popover-item" id="z85" onclick="setZoom(0.85)"><span>85%</span><span style="opacity:0.75">1004×627</span></button>
          <button class="popover-item" id="z100" onclick="setZoom(1.00)"><span>100%</span><span style="opacity:0.75">853×533</span></button>
        </div>
      </div>

      <!-- Unfoldable Controls & Hidden Settings Button -->
      <button class="btn" id="moreBtn" onclick="toggleMoreTray()">🎛️ Controls ▾</button>
      <button class="btn btn-sleep" onclick="sleepScreen()">🌙 Sleep</button>
      <button class="btn" onclick="closeDrawer()">▲</button>
    </div>

    <!-- Unfoldable Controls & Device Settings Tray -->
    <div id="moreTray">
      <div class="toolbar-row">
        <div class="control-group">
          <span class="label">🔆 Brightness:</span>
          <input id="brightSlider" class="slider" type="range" min="1" max="100" value="100"
                 oninput="setBrightness(this.value, true)">
          <span id="brightLabel" class="label" style="min-width:34px;color:#f8fafc;">100%</span>
        </div>
        <div class="spacer"></div>
        <div class="control-group">
          <span class="label">💡 Glow / LED:</span>
          <span class="chip" onclick="triggerLed('available', [34,197,94])">🟢 Green</span>
          <span class="chip" onclick="triggerLed('occupied', [239,68,68])">🔴 Red</span>
          <span class="chip" onclick="triggerLed('checkin', [234,179,8])">🟡 Yellow</span>
          <span class="chip" onclick="triggerLed('purple', [168,85,247])">🟣 Purple</span>
          <span class="chip" onclick="triggerLed('ada_available', [59,130,246])">🔵 Blue</span>
          <span class="chip" onclick="triggerLed('off', null)">⚫ Off</span>
        </div>
      </div>
      <div class="toolbar-row">
        <div class="control-group">
          <span class="label">🛠️ Hidden & System:</span>
          <span class="chip" onclick="triggerFlashAlert([56,189,248])">⚡ Flash Alert</span>
          <span class="chip" onclick="loadUrl('https://tap-settings.local/')">⚙️ Tap CollabOS Web UI</span>
          <span class="chip" onclick="escapeToAppspaceMenu()">🔓 Native Logitech / Appspace Menu</span>
        </div>
      </div>
    </div>
  </div>

  <div id="frameWrap">
    <iframe id="browserFrame" src="{default_url}" allow="autoplay; fullscreen; microphone; camera"></iframe>
  </div>

  <script>
    const frameWrap = document.getElementById('frameWrap');
    const frame = document.getElementById('browserFrame');
    const urlBox = document.getElementById('urlBox');
    const drawer = document.getElementById('drawer');
    const backdrop = document.getElementById('backdrop');
    const pullZone = document.getElementById('pullZone');
    const zoomLabel = document.getElementById('zoomLabel');
    const zoomBtnText = document.getElementById('zoomBtnText');
    const zoomPopover = document.getElementById('zoomPopover');
    const moreTray = document.getElementById('moreTray');
    const dimmerOverlay = document.getElementById('dimmerOverlay');
    const edgeGlow = document.getElementById('edgeGlow');
    const sleepCurtain = document.getElementById('sleepCurtain');
    const sleepClock = document.getElementById('sleepClock');
    const clockTime = document.getElementById('clockTime');
    const clockDate = document.getElementById('clockDate');
    const toastBanner = document.getElementById('toastBanner');
    const brightSlider = document.getElementById('brightSlider');
    const brightLabel = document.getElementById('brightLabel');

    let currentZoom = {default_zoom};
    let currentBrightness = 100;
    let isSleeping = false;
    let screensaverMode = 'black';
    let autoSleepMinutes = 0;
    let burnInProtection = true;
    let lastInteractionTime = Date.now();
    let lastStateVersion = 0;
    let lastToastId = 0;
    let lastFlashId = 0;
    let lastActionId = 0;

    function pushTelemetry(payload) {{
      fetch('/_telemetry', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(payload)
      }}).catch(function() {{}});
    }}

    function toggleZoomMenu(e) {{
      if (e) e.stopPropagation();
      resetAutoHide();
      zoomPopover.classList.toggle('open');
    }}

    function toggleMoreTray() {{
      resetAutoHide();
      moreTray.classList.toggle('open');
    }}

    document.addEventListener('click', function() {{
      zoomPopover.classList.remove('open');
    }});

    function applyZoom(scale, report) {{
      currentZoom = Math.min(1.5, Math.max(0.35, Math.round(scale * 100) / 100));
      const pct = Math.round(currentZoom * 100);
      frame.style.width = (100 / currentZoom) + 'vw';
      frame.style.height = (100 / currentZoom) + 'vh';
      frame.style.transform = 'scale(' + currentZoom + ')';
      frame.style.webkitTransform = 'scale(' + currentZoom + ')';
      zoomLabel.textContent = pct + '%';
      zoomBtnText.textContent = pct + '%';
      ['50', '60', '67', '75', '85', '100'].forEach(function(p) {{
        const el = document.getElementById('z' + p);
        if (el) el.classList.toggle('active', Math.abs(pct - parseInt(p, 10)) <= 2);
      }});
      if (report) pushTelemetry({{ zoom: currentZoom }});
    }}

    function setZoom(scale) {{
      resetAutoHide();
      applyZoom(scale, true);
      zoomPopover.classList.remove('open');
    }}
    function stepZoom(delta) {{
      resetAutoHide();
      applyZoom(currentZoom + delta, true);
    }}

    function setBrightness(val, report) {{
      currentBrightness = Math.max(1, Math.min(100, parseInt(val, 10) || 100));
      brightSlider.value = currentBrightness;
      brightLabel.textContent = currentBrightness + '%';
      const dimAlpha = ((100 - currentBrightness) / 100) * 0.92;
      dimmerOverlay.style.opacity = dimAlpha.toFixed(3);
      if (report) pushTelemetry({{ brightness: currentBrightness }});
    }}

    function updateSleepClock() {{
      const now = new Date();
      clockTime.textContent = now.toLocaleTimeString([], {{ hour: '2-digit', minute: '2-digit', hour12: false }});
      clockDate.textContent = now.toLocaleDateString([], {{ weekday: 'long', month: 'short', day: 'numeric' }});
      if (burnInProtection) {{
        const dx = Math.floor(Math.random() * 60) - 30;
        const dy = Math.floor(Math.random() * 60) - 30;
        sleepClock.style.transform = 'translate(' + dx + 'px, ' + dy + 'px)';
      }}
    }}

    function applyScreensaverStyle() {{
      sleepCurtain.classList.toggle('dim-mode', screensaverMode === 'dim');
      sleepClock.style.display = (screensaverMode === 'clock') ? 'block' : 'none';
      if (screensaverMode === 'clock') updateSleepClock();
    }}

    function sleepScreen(report) {{
      isSleeping = true;
      applyScreensaverStyle();
      sleepCurtain.classList.add('sleeping');
      closeDrawer();
      if (report !== false) pushTelemetry({{ screen_on: false }});
    }}

    function wakeScreen(report) {{
      lastInteractionTime = Date.now();
      if (!isSleeping && report !== false) return;
      isSleeping = false;
      sleepCurtain.classList.remove('sleeping');
      if (report !== false) pushTelemetry({{ screen_on: true, touch: true }});
    }}

    function applyGlow(on, rgb, effect) {{
      edgeGlow.classList.remove('pulse', 'breathe', 'flash');
      if (!on || !rgb || rgb.length < 3) {{
        edgeGlow.style.opacity = '0';
        edgeGlow.style.borderColor = 'transparent';
        edgeGlow.style.boxShadow = 'none';
        return;
      }}
      const c = 'rgb(' + rgb[0] + ',' + rgb[1] + ',' + rgb[2] + ')';
      const cGlow = 'rgba(' + rgb[0] + ',' + rgb[1] + ',' + rgb[2] + ', 0.88)';
      edgeGlow.style.borderColor = c;
      edgeGlow.style.boxShadow = 'inset 0 0 36px 8px ' + cGlow + ', inset 0 0 90px 18px rgba(' + rgb[0] + ',' + rgb[1] + ',' + rgb[2] + ', 0.38)';
      edgeGlow.style.opacity = '1';
      if (effect === 'pulse') edgeGlow.classList.add('pulse');
      if (effect === 'breathe') edgeGlow.classList.add('breathe');
      if (effect === 'flash') edgeGlow.classList.add('flash');
    }}

    function triggerFlashAlert(rgb) {{
      const prevGlowOn = edgeGlow.style.opacity === '1';
      applyGlow(true, rgb || [239, 68, 68], 'flash');
      if (isSleeping) wakeScreen(true);
      setTimeout(function() {{
        if (!prevGlowOn) applyGlow(false, null, 'solid');
      }}, 3800);
    }}

    let toastTimer = null;
    function showToast(text, durationSec) {{
      if (!text) return;
      toastBanner.textContent = text;
      toastBanner.classList.add('show');
      if (toastTimer) clearTimeout(toastTimer);
      toastTimer = setTimeout(function() {{
        toastBanner.classList.remove('show');
      }}, (durationSec || 6) * 1000);
    }}

    applyZoom(currentZoom, false);

    let hideTimer = null;
    function resetAutoHide() {{
      lastInteractionTime = Date.now();
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
      zoomPopover.classList.remove('open');
      if (hideTimer) {{ clearTimeout(hideTimer); hideTimer = null; }}
    }}

    let startY = null;
    pullZone.addEventListener('touchstart', function(e) {{
      lastInteractionTime = Date.now();
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

    // Track touch interactions globally for occupancy sensor & auto-sleep timer
    window.addEventListener('pointerdown', function() {{
      lastInteractionTime = Date.now();
      pushTelemetry({{ touch: true }});
    }}, {{ passive: true }});

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

    // Periodic clock update, Auto-Sleep check, and Anti-Burn-In Pixel Shifter
    setInterval(function() {{
      if (isSleeping && screensaverMode === 'clock') {{
        updateSleepClock();
      }}
      if (!isSleeping && autoSleepMinutes > 0) {{
        const idleMs = Date.now() - lastInteractionTime;
        if (idleMs >= autoSleepMinutes * 60000) {{
          sleepScreen(true);
        }}
      }}
      if (!isSleeping && burnInProtection) {{
        const px = (Math.floor(Date.now() / 60000) % 3) - 1;
        const py = (Math.floor(Date.now() / 120000) % 3) - 1;
        frameWrap.style.transform = 'translate(' + px + 'px, ' + py + 'px)';
      }} else {{
        frameWrap.style.transform = 'none';
      }}
    }}, 15000);

    // Appspace Host WebView Handshake & Hardware Bridge
    (function() {{
      const params = new URLSearchParams(window.location.search);
      const cardId = params.get('cardId') || '';
      let appWindow = null;
      let appOrigin = '*';
      let msMessaging = null;
      let hasProbedHost = false;

      window.msPostMessaging = function(message) {{
        try {{
          const obj = typeof message === 'string' ? JSON.parse(message) : message;
          handleHostMessage({{ data: obj, origin: 'mswebview' }});
        }} catch (e) {{}}
      }};
      window.msRetrieveMessaging = function() {{
        if (msMessaging) {{
          const out = JSON.stringify(msMessaging);
          msMessaging = [];
          return out;
        }}
        return '[]';
      }};

      function sendToHost(msgObj) {{
        msgObj.cardId = cardId;
        if (msMessaging) {{
          msMessaging.push(msgObj);
        }}
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

      function probeCordovaHost() {{
        if (hasProbedHost) return;
        hasProbedHost = true;
        ['index.html', 'cordova_plugins.js', 'cordova.js'].forEach(function(file, idx) {{
          const reqId = 8000 + idx;
          sendToHost({{
            message: 'api.xhr',
            url: file,
            settings: {{ type: 'GET', dataType: 'text' }},
            $requestId: reqId,
            callback: {{ $requestId: reqId }}
          }});
        }});
      }}

      function handleHostMessage(event) {{
        if (!event || !event.data) return;
        const m = String(event.data.message || '').toLowerCase();
        if (event.source) {{
          appWindow = event.source;
          appOrigin = event.origin || '*';
        }}
        if (m === 'appspaceapp.webview.postmessage.init') {{
          sendToHost({{ message: 'onapiready' }});
          setTimeout(function() {{ sendToHost({{ message: 'loaded' }}); }}, 100);
        }} else if (m === 'appspaceapp.mswebview.postmessage.init') {{
          msMessaging = [];
          sendToHost({{ message: 'onapiready' }});
          setTimeout(function() {{ sendToHost({{ message: 'loaded' }}); }}, 100);
        }} else if (m === 'api.init') {{
          sendToHost({{ message: 'loaded' }});
          pushTelemetry({{ appspace_init: event.data, host_origin: event.origin || '' }});
          setTimeout(probeCordovaHost, 500);
        }} else if (m === 'api.response') {{
          pushTelemetry({{ host_probe_response: event.data }});
        }} else if (m === 'onmodelupdate' || m === 'play') {{
          sendToHost({{ message: 'loaded' }});
        }}
      }}

      window.addEventListener('message', handleHostMessage, false);

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
        const colorWord = (mode === 'occupied') ? 'red'
                        : (mode === 'checkin') ? 'yellow'
                        : (mode === 'ada_available') ? 'blue'
                        : (mode === 'ada_occupied') ? 'orange'
                        : (mode === 'purple') ? 'purple'
                        : (mode === 'off') ? 'off' : 'green';
        [
          {{ message: 'executedevice', action: 'setstatuslight', card: 'scheduleboard', parameters: {{ status: statusName, color: hex, state: statusName }} }},
          {{ message: 'executedevice', action: 'statuslight', card: 'scheduleboard', parameters: {{ status: statusName, color: colorWord }} }},
          {{ message: 'executedevice', action: 'setstatuslight', card: 'webview', parameters: {{ status: statusName, color: hex }} }},
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

      // Escape to native Appspace / Logitech Device Menu
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

            if (typeof st.auto_sleep_minutes === 'number') autoSleepMinutes = st.auto_sleep_minutes;
            if (st.screensaver_mode) {{
              screensaverMode = st.screensaver_mode;
              if (isSleeping) applyScreensaverStyle();
            }}
            if (typeof st.burn_in_protection === 'boolean') burnInProtection = st.burn_in_protection;

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
              showToast(st.toast_text, st.toast_duration || 6);
            }}
            if (st.flash_id && st.flash_id !== lastFlashId) {{
              lastFlashId = st.flash_id;
              triggerFlashAlert(st.flash_color || [239, 68, 68]);
            }}
            if (st.PendingActionId && st.PendingActionId !== lastActionId) {{
              lastActionId = st.PendingActionId;
              if (st.pending_action === 'reload') reloadFrame();
              if (st.pending_action === 'reload_all') window.location.reload(true);
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
    snap = get_state_snapshot()
    palette = snap.get("color_palette", "default")
    script_ts = snap.get("script_timestamp", "11/12/2026 02:00:00 AM")

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

    # Reliably inject Remote.Enable=True, hotarea.enable=True, and statuslight.colorpalette into ApplicationProperties
    injected_props = (
        f"player.capabilities.supportfilecaching=True;\r\n"
        f"player.devicemenu.hotarea.enable=True;\r\n"
        f"Remote.Enable=True;\r\n"
        f"player.statuslight.enable=True;\r\n"
        f"player.statuslight.colorpalette={palette};"
    )
    if "player.capabilities.supportfilecaching=True;" in text:
        # Remove any existing duplicates first
        text = re.sub(r"player\.devicemenu\.hotarea\.enable=.*?;[\r\n]*", "", text)
        text = re.sub(r"Remote\.Enable=.*?;[\r\n]*", "", text)
        text = re.sub(r"player\.statuslight\.colorpalette=.*?;[\r\n]*", "", text)
        text = text.replace("player.capabilities.supportfilecaching=True;", injected_props, 1)

    text = re.sub(
        r"<DATA>TSVAL-.*?-TSVAL</DATA>",
        f"<DATA>TSVAL-{script_ts}-TSVAL</DATA>",
        text,
    )
    log_msg(f"  [APPSPACE] Patched script (ts={script_ts}, palette={palette}, Remote.Enable=True)!")
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

            if "appspace" in host.lower():
                update_state(bump_version=False, tap_ip=client_ip, last_seen=time.time())

            if not target.startswith("/_state") and not target.startswith("/api/state"):
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

            # 2. Live State & Control REST API
            if path_only in ("/_state", "/api/state"):
                if "appspace" in host.lower():
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
                    if "host_origin" in data:
                        with state_lock:
                            probe = dict(DEVICE_STATE.get("appspace_host_probe") or {})
                            probe["host_origin"] = data["host_origin"]
                            updates["appspace_host_probe"] = probe
                    log_msg(f"  [APPSPACE API.INIT TELEMETRY] origin={data.get('host_origin')}")
                if "host_probe_response" in data:
                    resp_obj = data["host_probe_response"]
                    req_id = str(resp_obj.get("requestId", "unknown"))
                    with state_lock:
                        probe = dict(DEVICE_STATE.get("appspace_host_probe") or {})
                        probe[f"req_{req_id}"] = str(resp_obj.get("result", ""))[:2000]
                        updates["appspace_host_probe"] = probe
                    log_msg(f"  [APPSPACE HOST PROBE] reqId={req_id} len={len(str(resp_obj.get('result', '')))}")
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
                if "auto_sleep_minutes" in cmd:
                    updates["auto_sleep_minutes"] = max(0, int(cmd["auto_sleep_minutes"]))
                if "screensaver_mode" in cmd:
                    updates["screensaver_mode"] = str(cmd["screensaver_mode"]).lower()
                if "burn_in_protection" in cmd:
                    updates["burn_in_protection"] = bool(cmd["burn_in_protection"])
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
                    new_palette = "ada" if mode.startswith("ada_") else "default"
                    with state_lock:
                        old_palette = DEVICE_STATE.get("color_palette", "default")
                    if new_palette != old_palette:
                        updates["color_palette"] = new_palette
                        updates["script_timestamp"] = make_appspace_timestamp()
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
                    updates["toast_duration"] = int(cmd.get("duration") or 6)

                action = str(cmd.get("action") or "").lower()
                if action:
                    snap = get_state_snapshot()
                    tap_ip = snap.get("tap_ip") or str(OPTIONS.get("tap_ip") or "192.168.178.178")
                    if action == "wake":
                        updates["screen_on"] = True
                    elif action == "sleep":
                        updates["screen_on"] = False
                    elif action == "return_home":
                        default_home = str(OPTIONS.get("default_url") or "http://homeassistant.local:8123")
                        updates["url"] = default_home
                        updates["base_url"] = default_home
                        updates["popup_expires_at"] = 0.0
                    elif action == "open_collabos_settings":
                        updates["url"] = "https://tap-settings.local/"
                        updates["popup_expires_at"] = 0.0
                    elif action == "flash_alert":
                        with state_lock:
                            updates["flash_id"] = int(DEVICE_STATE.get("flash_id", 0)) + 1
                        if "color" in cmd and isinstance(cmd["color"], list) and len(cmd["color"]) == 3:
                            updates["flash_color"] = [int(x) for x in cmd["color"]]
                        updates["screen_on"] = True
                    elif action in ("reload", "reload_all", "open_device_menu"):
                        with state_lock:
                            updates["PendingActionId"] = int(DEVICE_STATE.get("PendingActionId", 0)) + 1
                        updates["pending_action"] = action
                        if action == "open_device_menu":
                            threading.Thread(
                                target=send_appspace_ws_command, args=(tap_ip, "?remote=system"), daemon=True
                            ).start()
                    elif action == "restart_app":
                        updates["script_timestamp"] = make_appspace_timestamp()
                        with state_lock:
                            updates["PendingActionId"] = int(DEVICE_STATE.get("PendingActionId", 0)) + 1
                        updates["pending_action"] = "reload_all"
                        threading.Thread(
                            target=send_appspace_ws_command, args=(tap_ip, "?system=restart"), daemon=True
                        ).start()
                    elif action == "resync_app":
                        updates["script_timestamp"] = make_appspace_timestamp()
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
                if "/index.html" in target.lower() and (
                    "appspaceusercontent.com" in host.lower() or "/fastnetdata/" in target.lower()
                ):
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
                    ts_str = get_state_snapshot().get("script_timestamp", "11/12/2026 02:00:00 AM")
                    resp_body = ts_str.encode("utf-8")

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
        f"Logitech Tap Scheduler Appspace Proxy Add-on v1.3.0 listening on 0.0.0.0:8080 "
        f"(default_url={OPTIONS.get('default_url')}, default_zoom={OPTIONS.get('default_zoom')})"
    )
    server.serve_forever()
