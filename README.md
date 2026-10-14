# CycleTrainer

A minimal TrainerRoad-style indoor training app for Windows. It does four things:

* **Connects over Bluetooth** to a smart trainer (Wahoo KICKR v6 or any FTMS trainer), a heart-rate strap and, optionally, a cadence sensor.
* **Plays structured workouts in ERG mode.** The trainer holds the target watts, with intensity ±, skip, +1 min, and resistance (grade) mode.
* **Records the ride** (power, HR, cadence, virtual speed/distance, one lap per interval) and saves a `.FIT` file.
* **Uploads** to **Garmin Connect**, either manually or automatically after each ride. Strava gets the ride through Garmin's own Strava link.

You can build workouts in the editor. You can also import `.zwo` (Zwift), `.mrc`/`.erg` and `.json` files, or turn a **past ride (.fit)** into a template.

## Run it

1. Python 3.12 or newer is required; 3.14 works. If you don't have it: `winget install Python.Python.3.14`.
2. Double-click **`start.bat`**. The first run creates a private Python environment in
`%LOCALAPPDATA%\\CycleTrainer\\venv` (kept out of Google Drive) and installs the dependencies.
3. The app opens at [http://localhost:8765](http://localhost:8765). Keep the black console window open while you ride.

To try it without hardware, go to **Settings → Sensors → Simulator**, or run `start.bat --simulate`.

## Pairing the KICKR and HR strap

* Close the Wahoo app, Zwift and **TrainerRoad**. A trainer accepts only one controller at a time, so if another app holds it you'll see a "Trainer refused control" message.
* Make sure Windows Bluetooth is on. Do **not** pair the devices in Windows settings: `bleak` connects to them directly through the WinRT Bluetooth stack (`BleakScanner` + `BleakClient(address)`), the same way Chrome's Web Bluetooth does.
* Go to Settings → Sensors → **Scan**. The KICKR shows up as a trainer, and the strap as a heart-rate monitor. Click Connect. The devices are remembered and reconnect automatically next time.
* The KICKR v6 reports cadence itself. A separate cadence sensor can be added as "Cadence", and it takes priority when connected.

## Garmin Connect

Garmin only offers its upload API to approved business partners. CycleTrainer uses the community
[`garminconnect`](https://pypi.org/project/garminconnect/) library instead, which signs in the same way the Garmin mobile app does.
Sign in once in Settings → Garmin Connect, and enter the verification code if you have two-step login turned on.
With Garmin Connect linked to Strava, rides are forwarded to Strava automatically, so this app never talks to Strava directly.
Only the session token is stored (in `%LOCALAPPDATA%\\CycleTrainer`), never your password.
Because this route is unofficial, Garmin can break it when they change their login. If that happens, run
`%LOCALAPPDATA%\\CycleTrainer\\venv\\Scripts\\pip install -U garminconnect`, or use the **.FIT** button in Activities and upload the file by hand at connect.garmin.com → Import Data.

## Where things are stored

|What|Where|
|-|-|
|Workouts (`.json`)|`data\\workouts\\` next to the app|
|Rides (`.fit` + summary `.json`)|`data\\activities\\`|
|Settings (FTP, weight, paired sensors)|`data\\settings.json`|
|Garmin token|`%LOCALAPPDATA%\\CycleTrainer\\` (never synced)|

## Code map (for continuing in Claude Code)

```
run.py                 entry point (starts the server, opens the browser)
app/main.py            FastAPI routes + websocket, 1 Hz session ticker
app/session.py         ride player/recorder: targets, ERG commands, auto-pause, laps, summary (NP/IF/TSS)
app/ble/parsers.py     Bluetooth packet parsing/encoding (HR, Cycling Power, CSC, FTMS, Wahoo) - pure, unit-tested
app/ble/devices.py     bleak-based sensors with auto-reconnect, metric hub, simulator
app/workouts.py        workout model, library, .zwo/.mrc/.erg/.fit import, .zwo/.mrc export
app/fitwriter.py       FIT activity writer (fit-tool)
app/sync.py            Garmin Connect login/upload
static/                single-page UI (vanilla JS, canvas charts)
tests/                 python -m pytest tests
```

How trainer control works: on connect, the app subscribes to FTMS *Indoor Bike Data* (0x2AD2) and *Cycling Power* (0x2A63). It then sends FTMS *Request Control* and *Start* to the control point (0x2AD9). After that, every second it sends *Set Target Power* (ERG) or *Set Indoor Bike Simulation* (grade), but only when the value changes. Older KICKR firmware without FTMS falls back to Wahoo's own control characteristic.

## Ideas for next steps

* An FTP ramp test with an automatic FTP update; a training calendar or plan
* Recovery in case a ride is disrupted
* Power-match with a separate power meter; ERG smoothing at interval starts
* Package as a single `.exe` (PyInstaller) or wrap the UI in a native window (pywebview)
* ANT+ support (needs a USB stick and the `openant` library)

