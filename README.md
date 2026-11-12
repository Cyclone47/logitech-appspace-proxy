# Logitech Tap Scheduler — Home Assistant Add-on & HACS Integration (`v1.2.0`)

Repurpose a **Logitech Tap Scheduler** (`CollabOS`, e.g. `VR0028`) to display **Home Assistant** (or any web dashboard) full-screen at crisp desktop resolution—without unlocking the bootloader, without rooting, and with full Home Assistant control over **Screen Sleep/Wake, Brightness, LED / Edge Glow, Camera Popups, and Native Logitech Settings**.

This single GitHub repository works as **BOTH**:
1. **A Home Assistant Add-on Repository** (`logitech_appspace_proxy`) — runs the port `8080` HTTP/HTTPS proxy, strips Appspace night-blanking schedules, enables Appspace local WebSocket control (`port 55537`), and serves the pull-down menu & hardware bridge.
2. **A HACS Custom Integration Repository** (`custom_components/tap_scheduler`) — exposes your Tap Scheduler as a native **Device in Home Assistant** with entities for Screen Power/Brightness, RGB LED & Edge Glow, Status LED Mode, Resolution Scale, Active URL, and Action Buttons!

---

## Features

### On-Screen Pull-Down Menu (Swipe down or tap the top-center handle)
- **URL Bar + Go + Reload (`↻`)**
- **🌙 Screen Off (Sleep Mode) + Tap-to-Wake:** Blanks the display to pitch black; touching anywhere on the glass wakes it immediately.
- **🔆 Brightness Dimmer (`5%`–`100%`)**
- **Resolution / Zoom Presets:** `50% (1920p)`, `60% (1420p)`, `67% (1280p)`, `75%`, `100%`, plus `−`/`+` buttons (saved to `localStorage`).
- **💡 LED & Ambient Edge Glow:** `🟢 Green`, `🔴 Red`, `🟡 Yellow`, `🟣 Purple`, `🔵 Blue`, `⚫ Off`.
- **⚙️ Tap Web Settings:** Built-in reverse proxy (`https://tap-settings.local/`) that opens the Tap Scheduler's own CollabOS Local Admin UI directly inside the touch screen.
- **🔓 Logitech / Appspace Menu:** Sends `?remote=system` to Appspace's local WebSocket (`port 55537`) and collapses the WebView card so you can access native Logitech CollabOS Settings.

### Native Home Assistant Entities (via HACS Integration)
- `light.logitech_tap_scheduler_screen` — Turn screen On/Off (with Tap-to-Wake sync) & adjust Brightness (`1–100%`).
- `light.logitech_tap_scheduler_led_edge_glow` — RGB Color Picker + Effects (`solid`, `pulse`, `breathe`) + syncs physical LED status mode.
- `select.logitech_tap_scheduler_status_led_mode` — `Off`, `Green (Available)`, `Red (Occupied)`, `Yellow (Check-In)`, `Purple`, `Blue (ADA Available)`, `Orange (ADA Occupied)`.
- `select.logitech_tap_scheduler_resolution_scale` — `50% (1920p)`, `60% (1420p)`, `67% (1280p)`, `75%`, `100%`.
- `text.logitech_tap_scheduler_dashboard_url` — View or change the active dashboard URL in real time.
- `button.logitech_tap_scheduler_wake_screen` / `sleep_screen` / `reload_dashboard` / `open_logitech_appspace_menu` / `restart_appspace_player`.
- `binary_sensor.logitech_tap_scheduler_connected` — Online connectivity status.
- **Services:**
  - `tap_scheduler.popup_url` — Temporarily pop up a camera view or URL for `duration` seconds, then automatically return to the main dashboard.
  - `tap_scheduler.show_toast` — Show a floating notification banner at the bottom of the Tap Scheduler screen.

---

## Step 1: Install the Home Assistant Add-on

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**.
2. Click **⋮ (top right) → Repositories** and add:
   ```text
   https://github.com/Cyclone47/logitech-appspace-proxy
   ```
3. Install **Logitech Tap Scheduler Appspace Proxy**, configure `default_url`, and click **Start**.
4. Install `http://<YOUR_HA_IP>:8080/ca.crt` on the Tap Scheduler (*System → More settings → CA certificate*) and set the Tap Scheduler's HTTP Proxy to `<YOUR_HA_IP>` port `8080`.

---

## Step 2: Install the Companion HACS Integration (Native HA Entities)

1. Open **HACS** in Home Assistant.
2. Click **⋮ (top right) → Custom repositories**.
3. Add:
   - **Repository:** `https://github.com/Cyclone47/logitech-appspace-proxy`
   - **Type:** `Integration`
4. Click **Logitech Tap Scheduler** → **Download** and **Restart Home Assistant**.
5. Go to **Settings → Devices & Services → Add Integration → Logitech Tap Scheduler** (keep default `http://127.0.0.1:8080`).
