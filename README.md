# FS Relocator

Relocate your aircraft to **any place or coordinate** in Microsoft Flight Simulator 2024 / 2020. Free flight normally lets you start only from an airport (by ICAO code); FS Relocator removes that limit — enter a flight, then move the plane anywhere you like by place name or latitude/longitude.

Fully local, no account, no external calls, no paid APIs.

---

## Download

Go to the **Releases** page and download the latest `FSRelocator-vX.Y.zip` (recommended) or `FSRelocator-vX.Y.exe`. Unzip and double-click `FSRelocator.exe` — no installation needed.

## Before you start

- Microsoft Flight Simulator 2024 (or 2020) installed, and **you have entered a flight at least once** (the aircraft is loaded).
- Windows 10 / 11 with **Edge WebView2** (the embedded UI depends on it; it ships with Windows).
- The release EXE already bundles `SimConnect.dll`. If you ever see a "SimConnect.dll not found" message, copy the DLL from the MSFS SDK into the same folder as the EXE.

## How to use

1. Open the tool — it connects to MSFS automatically; the status dot turns green when connected.
2. **Search** tab: type a place name (e.g. `Eiffel Tower`), search, then pick a candidate.
3. **Target** tab: enter `latitude, longitude` directly, or fill latitude / longitude / altitude (m) / heading (°) separately.
4. Click **Go to Location** — the aircraft is teleported to the target.

### Main features

| Feature | Description |
| --- | --- |
| Map only | Collapse the side panel and keep just the map window — great to overlay on the sim full-screen. |
| Always on top | The window floats above the simulator. |
| Follow aircraft | Map auto-centers on the current aircraft position. |
| Zoom by altitude | Zoom level adjusts automatically with flight altitude. |
| Search & pick | Selecting a place auto-disables "Follow" so your target stays centered. |
| North up / Heading up | Toggle map orientation; a compass rotates with it. |
| Auto-locate on launch | On startup the map centers on the aircraft and matches the chosen orientation. |

Keyboard shortcuts: `Ctrl+Alt+T` teleport to armed target · `Ctrl+Alt+E` resume control · `Ctrl+Alt+G` bring this window to front.

## Notes

- The release EXE is **not code-signed**. Windows SmartScreen may show "Windows protected your PC" on first launch — click **More info → Run anyway**.
- Map tiles: Esri (worldwide), cached on disk. An offline satellite basemap (zoom 0–5) is bundled so the world is never blank at low zoom.
- Geocoding: a built-in offline gazetteer plus online geocoders (Photon, Nominatim, ArcGIS).

## Build from source

```bash
pip install -r requirements.txt
pip install pyinstaller
python build.py                 # -> dist/FSRelocator.exe
python build.py --distpath out  # output to a chosen directory
```

For development, run `python app.py` directly.

## Automated release

This repo uses GitHub Actions (`.github/workflows/release.yml`):

- Pushing a `v*` tag builds and publishes the Release on a cloud Windows runner.
- Or trigger manually: **Actions → Build & Release → Run workflow**.

The version number comes from `APP_VERSION` in `strings.py`; bump it before releasing.

## Feedback & Support

Found a bug, or have a suggestion? I'd love to hear from you — this tool gets better from real-world use.

- **Open an issue** on this repository (preferred).
- Or email me at **jerryxiao@msn.com**.

I read every report and fix issues as they come up.

## License

[MIT](LICENSE)
