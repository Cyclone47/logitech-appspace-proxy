# Logitech Tap Scheduler Appspace Proxy (Home Assistant Add-on)

Repurpose a **Logitech Tap Scheduler** (`CollabOS`, e.g. `VR0028`) to display **Home Assistant** (or any web dashboard) full-screen at crisp desktop resolution—without unlocking the bootloader, without rooting, and without night-mode screen blanking.

## How It Works
1. Among all built-in CollabOS Service Providers, **Appspace (`com.appspace.app`)** is the only provider whose Android `network_security_config.xml` trusts user-installed CA certificates (`<certificates src="user" />`).
2. This add-on runs a lightweight Python HTTP/HTTPS proxy on port `8080` that:
   - **Passes through 100% of normal traffic** (Home Assistant, CDNs, WebRTC camera streams, IPv4 & IPv6) untouched.
   - **Intercepts only `app15.cloud.appspace.com` and `*.appspaceusercontent.com`**:
     - Strips `BlankScreen_Schedule` (`23:00–06:00`), `Display.Screen Burn Mode`, and `OperatingHours` from `configscripthandler.ashx` so the screen never turns black at night.
     - Hijacks the Appspace WebView Card (`/index.html`) to serve a custom **Fullscreen Dashboard Wrapper** that completes Appspace's `CardApi` `postMessage` handshake (`{ message: "onapiready" }` + `{ message: "loaded" }`) so the `...` loading spinner immediately disappears.
     - Applies **desktop resolution scaling** (`67%` default = `1280p` desktop layout on the 16:10 screen) and adds a **top-center pull-down drawer** for changing URLs or live zoom (`50% (1920p)` – `100%`) with auto-hide.

---

## Installation in Home Assistant

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**.
2. Click the **⋮ (three dots)** in the top-right corner → **Repositories**.
3. Add:
   ```text
   https://github.com/Cyclone47/logitech-appspace-proxy
   ```
4. Click **Logitech Tap Scheduler Appspace Proxy** → **Install** → **Start**.

---

## Tap Scheduler Setup

1. **Install the Root CA Certificate on the Tap Scheduler:**
   - Download `http://<YOUR_HA_IP>:8080/ca.crt` (or copy `/share/local-root-ca.crt`).
   - Open your Tap Scheduler's Local Network Access web UI (`https://<TAP_SCHEDULER_IP>/`) → **System → More settings → CA certificate** → install `local-root-ca.crt`.
   - *(Tip: If you already installed a CA certificate from a local PC test, paste its base64 into `ca_cert_base64` and `ca_key_base64` in the Add-on Configuration tab so you don't have to reinstall the certificate on the Tap Scheduler!)*
2. **Set the Proxy on the Tap Scheduler:**
   - On the Tap Scheduler (`https://<TAP_SCHEDULER_IP>/` or Android Network Settings), set HTTP Proxy to `<YOUR_HA_IP>` and Port `8080`.
3. **Select Appspace as Service Provider:**
   - Once paired with a free Appspace account containing a WebView card channel, the proxy automatically hijacks the card and loads your Home Assistant dashboard!

---

## Using the Pull-Down Menu on the Tap Scheduler
- **Swipe down** (or **tap**) on the subtle pill handle at the **top-center** of the screen to open the URL & Zoom bar.
- Adjust resolution scale (`50% (1920p)`, `60% (1420p)`, `67% (1280p)`, `75%`, `100%`, or `−`/`+`). Your choice is saved in `localStorage`.
- The drawer automatically hides after `8` seconds of inactivity (or when you tap outside / tap **▲ Hide**).
