# Logitech Tap Scheduler — Home Assistant Add-on & HACS Integration (`v1.3.0`)

Repurpose a **Logitech Tap Scheduler** (`CollabOS`, e.g. `VR0028`) to display **Home Assistant** (or any web dashboard) full-screen at crisp desktop resolution—without unlocking the bootloader, without rooting, and with full Home Assistant control over **Screen Sleep/Wake, Brightness, Edge Glow, Auto-Sleep, Screensaver Styles, Anti-Burn-In, Camera Popups, and Hidden Logitech/Appspace Menus**.

This single GitHub repository works as **BOTH**:
1. **A Home Assistant Add-on Repository** (`logitech_appspace_proxy`) — runs the port `8080` HTTP/HTTPS proxy, strips Appspace night-blanking schedules, injects `Remote.Enable=True` (`port 55537`), reverse-proxies the Tap's CollabOS Web UI (`https://tap-settings.local/`), and serves the sleek pull-down menu & hardware bridge.
2. **A HACS Custom Integration Repository** (`custom_components/tap_scheduler`) — exposes your Tap Scheduler as a native **Device in Home Assistant** (complete with real Hardware Serial Number, CollabOS Firmware Version, RAM/Storage diagnostics, and neatly categorized Controls & Configuration entities).

---

## Features (`v1.3.0`)

### 1. Sleek On-Screen Pull-Down Bar (Swipe down or tap the top-center handle)
- **Compact Primary Row:**
  - `🏠 HA` button, `URL Input` + `Go` + `↻ Reload`
  - **Single Unfoldable `🔍 Zoom ▾` Popover:** Tap to unfold fine `−` / `+` steppers and resolution presets (`50% · 1706×1066`, `60% · 1422×888`, `67% ★ · 1280×800`, `75% · 1138×711`, `85% · 1004×627`, `100% · 853×533`).
  - **Unfoldable `🎛️ Controls ▾` Tray:** Expands Brightness (`1–100%`), Glow/LED color chips, `⚡ Flash Alert`, `⚙️ Tap CollabOS Web UI` (`https://tap-settings.local/`), and `🔓 Native Logitech / Appspace Menu`.
  - `🌙 Sleep` button (Tap anywhere on screen to wake).

### 2. Native Home Assistant Entities (via HACS Integration)
#### Main Controls
- **`light.logitech_tap_scheduler_screen`** — Turn screen On/Off (with Tap-to-Wake sync) & adjust Brightness (`1–100%`).
- **`light.logitech_tap_scheduler_led_edge_glow`** — RGB Color Picker + single unfoldable **Effect** menu (`solid`, `pulse`, `breathe`, `flash`, plus one-tap presets: `Preset: Available (Green)`, `Preset: Occupied (Red)`, `Preset: Check-In (Yellow)`, `Preset: Purple`, `Preset: ADA Blue`, `Preset: ADA Orange`).
- **`text.logitech_tap_scheduler_send_toast_banner`** — Type any text and press Enter to pop up a notification banner on the tablet screen.
- **`button.logitech_tap_scheduler_flash_visual_alert`** — Wakes the screen and flashes a high-contrast RGB alert border.
- **`button.logitech_tap_scheduler_return_to_home_dashboard`** — Returns the tablet to your main Home Assistant dashboard.
- **`button.logitech_tap_scheduler_reload_dashboard`** — Reloads the current dashboard iframe.

#### Configuration (Cleanly grouped under Configuration)
- **`select.logitech_tap_scheduler_display_zoom_resolution`** — Single unfoldable dropdown (`50%` to `100%` with pixel dimensions).
- **`select.logitech_tap_scheduler_auto_sleep_timeout`** — Automatically sleep the screen after `1`, `5`, `10`, `15`, `30`, or `60` minutes of touch inactivity (`Disabled` by default), waking immediately on touch.
- **`select.logitech_tap_scheduler_sleep_screensaver_style`** — Choose between `Pure Black (Screen Off)`, `Floating Digital Clock`, or `Dimmed Dashboard (5%)`.
- **`switch.logitech_tap_scheduler_anti_burn_in_pixel_shift`** — Periodically shifts the frame by `1px` to prevent LCD image retention.
- **`text.logitech_tap_scheduler_dashboard_url`** — View or change the active dashboard URL.
- **`button.logitech_tap_scheduler_open_tap_collabos_web_ui`** — Opens the Tap Scheduler's hidden local CollabOS Web Admin UI (`https://tap-settings.local/`) right on the tablet screen.
- **`button.logitech_tap_scheduler_open_native_logitech_appspace_menu`** — Sends `?remote=system` via WebSocket `55537` and triggers card exit hooks.
- **`button.logitech_tap_scheduler_resync_appspace_config_palette`** & **`restart_appspace_player`**.

#### Sensors & Diagnostics
- **`binary_sensor.logitech_tap_scheduler_screen_touch_activity`** (`occupancy`) — Turns `on` when someone touches or wakes the screen (great for triggering room lights!).
- **`binary_sensor.logitech_tap_scheduler_connected`** (`connectivity`) — Online status + full hardware attributes (`serial_number`, `collabos_firmware`, `appspace_version`, `memory`, `storage`, `resolution`, `tap_ip`).

---

## Step 1: Install / Update the Home Assistant Add-on
1. In Home Assistant, go to **Settings → Add-ons → Add-on Store** (click **⋮ → Check for updates**).
2. Update **Logitech Tap Scheduler Appspace Proxy** to `1.3.0` and click **Restart**.

## Step 2: Install / Update the Companion HACS Integration
1. In **HACS**, add `https://github.com/Cyclone47/logitech-appspace-proxy` as a custom **Integration** repository (or click **Update information** → **Redownload** on **Logitech Tap Scheduler**).
2. Restart Home Assistant and add **Logitech Tap Scheduler** under **Settings → Devices & Services**.
