# dd-plugin: Dragons Den for Klipper

Klipper plugin for the **Dragons Den** automated hotend changer (v0.0.1).

- Tool changes `T0`..`Tn` with dock/undock kinematics and per-tool G-code offsets
- `DD_CALIBRATE_DOCKS`: guided dock location calibration (fixed or floating gantry)
- `DD_CALIBRATE_OFFSETS`: guided tool offset calibration with an **eddy** probe, a **sexball** (calibration ball) or a **USB camera**
- Wizards run as native Klipper action prompts, so they work in **Mainsail and Fluidd** with no frontend changes
- A **Dragons Den** entry in Mainsail's left sidebar opens a control panel for settings, calibration, tool changes and the USB camera view
- Optional **toolhead and per-dock sensors**: the attached tool is detected automatically (also when you swap a hotend by hand), every dock and pickup is checked, and a hotend lost mid-print pauses the print
- Dock coordinates, tool offsets and the active tool persist in `dragonsnest.cfg` across restarts
- All commands use the `DD_` prefix (`DD_CALIBRATE_DOCKS`, `DD_STATUS`, `DD_SET_CONFIG`, ...)

> **Status:** v0.0.1 has been tested against mocked Klipper objects only (`tests/`), not on a printer yet. Run the first tool changes slowly and keep a hand near the power switch.

## Repository layout

```
klippy/extras/dragons_den.py           Klipper module ([dragons_den])
config/dragonsnest.cfg                 Config template + persisted state
moonraker/dragons_den.py               Moonraker component (serves the pages below)
moonraker/dragons_den_panel.html       Dragons Den control panel (Mainsail sidebar)
moonraker/dragons_den_camera.html      Standalone camera page: live feed, crosshair, jog, Save
mainsail/navi.json, mainsail/custom.css  Mainsail sidebar entry and its styling
scripts/theme_install.py               Merges the sidebar entry into <config>/.theme/
install.sh                             Symlink installer
tests/                                 pytest suite with Klipper mocks
```

## How a tool change works

The extruder stays on the shuttle; the hotend is what docks. For each dock the module stores the **entry point**: the toolhead position where the hotend's bolt heads sit in the dock entry holes.

**Docks are front-mounted.** The dock bar sits at the front of the printer (low Y) with the docks opening toward `+Y` (the print area). The toolhead enters a dock moving `-Y` and pulls out moving `+Y`. Dock positions in the rear are rejected: a dock's Y must be below the middle of the Y axis, or at most `front_dock_max_y` if you set it. A rejected dock is not saved, and a tool change to a stored rear dock fails with an error.

**Docking (put tool away):**
1. Z hop (`z_hop`), travel to the approach point `(dock_x, dock_y + clear_y_dist)`; on a floating gantry also move to `dock_z`
2. Move `-Y` to the entry point
3. Slide X by `dock_x_travel` (default -5 mm) into the dock
4. Retract the extruder `lock_retract_dist` (default 3 mm) to unlock the hotend
5. Pull back `+Y` by `clear_y_dist` (default 70 mm)

**Pickup** is the reverse: approach at `(dock_x + dock_x_travel, dock_y + clear_y_dist)`, move `-Y` in, extrude `lock_retract_dist` to lock, slide X back (+5 mm), pull back `+Y`.

The toolhead then returns to where it started, shifted by the new tool's offset, and the G-code state is restored with `SET_GCODE_OFFSET X_ADJUST/Y_ADJUST/Z_ADJUST`, so any Z babystepping is kept.

If the extruder is below `min_extrude_temp`, the lock moves use `FORCE_MOVE` instead. That needs `enable_force_move: True` under `[force_move]`, or a hot hotend.

## Toolhead and dock sensors

Both kinds of sensor are optional and independent. Each is a switch read like an endstop or button: **triggered means a hotend is seated**. Prefix the pin with `!` to invert it and `^` to enable the pull-up.

```ini
[dragons_den]
tool_count: 3
toolhead_sensor_pin: ^PG11      # hotend seated on the toolhead
dock_sensor_pin_t0: ^PG12       # T0 seated in its dock
dock_sensor_pin_t1: ^PG13
dock_sensor_pin_t2: ^toolboard:PB5
#pause_on_detach: True          # PAUSE when a hotend is lost mid-print
#detach_gcode:                  # extra G-code run when that happens
#sensor_settle_time: 0.3        # seconds to let the pins report after a move
```

What the sensors do:

- **Automatic tool detection.** Whenever a pin changes, the plugin works out the attached tool. If the toolhead sensor is triggered and exactly one dock reports empty, that tool is the active one. This also works when you put a hotend on by hand without a tool-change command: its G-code offset is applied and it is saved as the active tool. Taking a hotend off by hand (not printing) sets the active tool to none.
- **Checked tool changes.** After docking, the toolhead must report empty and the tool's dock seated. After a pickup, the toolhead must report a hotend and the dock empty. Otherwise the command stops with an error, and the active tool keeps its last confirmed state. A tool change also refuses to start while the sensors contradict the active tool.
- **Detachment while printing.** If the toolhead sensor opens during a print without a tool change, the plugin marks the tool **Detached**, reports it with `!!` in the console, shows a prompt and runs `PAUSE` plus `detach_gcode`. With `pause_on_detach: False`, only `detach_gcode` runs. Push the hotend back on: it is detected and the error clears. Then check the print and `RESUME`. `DD_SET_ACTIVE_TOOL` clears the error too.
- During calibration wizards the plugin ignores sensor changes, because you swap tools by hand there. It re-checks the sensors when the wizard ends.

`printer.dragons_den` reports `tool_state` (`attached`, `none`, `detached`, or `unknown` when the sensors can't identify the hotend or contradict the active tool), `detected_tool`, and `sensors` (`toolhead` and `docks.T0`..`Tn` as `true`/`false`, or `null` when no pin is configured).

Without sensors, everything works as before: the active tool is whatever the last tool change or `DD_SET_ACTIVE_TOOL` set.

## Installation

### 1. Clone and install

On the printer (for example a Raspberry Pi running Klipper and Moonraker):

```bash
cd ~ && git clone https://github.com/ABG3D/dd-plugin.git
```

```bash
~/dd-plugin/install.sh
```

The installer:
- symlinks `klippy/extras/dragons_den.py` into `~/klipper/klippy/extras/`
- symlinks the Moonraker component into `~/moonraker/moonraker/components/`
- adds both symlinks to the Klipper and Moonraker `.git/info/exclude` files so `update_manager` doesn't report those repos as dirty
- copies `config/dragonsnest.cfg` into `~/printer_data/config/`, but only if that file doesn't exist yet. It is copied, not linked, because the plugin writes to it.
- adds the **Dragons Den** sidebar entry to Mainsail by merging into `~/printer_data/config/.theme/navi.json` and `custom.css`. Your own entries and styles are kept: the Dragons Den link is matched by its URL, and its CSS sits between `dragons_den` marker comments, so re-running the installer replaces it instead of duplicating it. An invalid existing `navi.json` is left untouched and reported.
- restarts `klipper` and `moonraker`

Options: `-k <klipper dir>`, `-m <moonraker dir>`, `-c <config dir>`, `-n` (don't restart), `-s` (skip the Mainsail sidebar entry), `-u` (uninstall: removes the symlinks and the sidebar entry, keeps `dragonsnest.cfg`).

### 2. printer.cfg

Add the include and edit `[dragons_den]` in `dragonsnest.cfg`:

```ini
[include dragonsnest.cfg]
```

Minimum settings:

```ini
[dragons_den]
tool_count: 3
kinematics_type: floating_gantry   # or fixed_gantry
probe_type: usb_camera             # or eddy / sexball
```

If you include the file from a subfolder (`[include dragons/dragonsnest.cfg]`), also set `nest_file: ~/printer_data/config/dragons/dragonsnest.cfg`.

If you already have `T0`, `T1`, ... macros, remove them or set `register_t_commands: False` and call `DD_SELECT_TOOL TOOL=n` from your own macros.

### 3. moonraker.conf

```ini
# Serves the control panel (/server/dragons_den/panel) and the
# USB camera alignment page (/server/dragons_den/camera)
[dragons_den]

[update_manager dragons_den]
type: git_repo
path: ~/dd-plugin
origin: https://github.com/ABG3D/dd-plugin.git
primary_branch: main
managed_services: klipper moonraker
```

Restart Moonraker. Updates then appear in Mainsail/Fluidd under *Machine → Update Manager*.

The panel and camera pages talk to Klipper through Moonraker's API. If Moonraker requires authentication, your network must be listed under `trusted_clients` in `[authorization]`.

### 4. Open the Dragons Den panel

Reload Mainsail in the browser. A **Dragons Den** item with a nozzle icon appears in the left sidebar; it opens the control panel at `http://<printer>/server/dragons_den/panel` (the **← Mainsail** button returns to the dashboard). Mainsail loads `navi.json` and `custom.css` from `.theme`, but it does not run custom JavaScript (there is no `custom.js`), so the panel is its own page served by Moonraker rather than a view inside Mainsail.

The panel has no colour scheme of its own. It uses Vuetify's theme variables (`--v-theme-*`) and class names (`v-card`, `v-btn`, `v-chip`), and fills the variables from Mainsail's saved UI settings: light or dark mode and your primary colour. It also loads your `.theme/custom.css`, so your own theme overrides apply. The sidebar entry adds no colours either.

Fluidd has no sidebar extension mechanism. Open the panel URL directly, or use the same wizards there through Klipper's action prompts.

## Dragons Den panel

The panel polls Klipper once a second and sends every action as a G-code command, so everything it does is also available from the console.

| Card | Controls |
|---|---|
| **Wizard** (while one runs) | Mirrors the open action prompt: instructions, jog buttons, Accept/OK/Save, Cancel |
| **Paper Z-Test (TESTZ)** (while Klipper's manual probe is active) | Current Z, `TESTZ` −1/−0.1/−0.05/−0.01 and +0.01/+0.05/+0.1/+1, Accept, Abort |
| **Attached Tool** | Large live indicator: `T0`..`Tn`, `None`, `Detached` or `Error` (sensors disagree), with badges for the toolhead sensor and every dock sensor (`seated`, `empty` or `n/a`) |
| **Tool Control & Status** | `T0`..`Tn` quick select, Dock Active Tool, Home All, active tool, homed axes, position, dock sensor / dock / offset table, set the attached tool without motion |
| **Dock Calibration** | Calibrate All Docks, Calibrate T0..Tn (one tool each), Test Slow Dock; shows the front-dock limit and `+Y` clearance |
| **Offset Calibration** | Calibrate Offsets and Paper Z-Test for the selected tool, plus the controls of the running `probe_type` only: the USB camera live view (crosshair, Z-focus circle, 0.01 / 0.05 / 0.1 / 1.0 mm jog, Save), or the sexball or eddy instructions |
| **Settings & Persistence** | Common options (`tool_count`, `kinematics_type`, `dock_x_travel`, `lock_retract_dist`, `clear_y_dist`, `front_dock_max_y`, `probe_start_z`, `probe_type`), then only the selected probe's options: sexball `ball_od`, `ball_center`, `ball_clearance`; eddy `eddy_xy_target`, `eddy_z_point`, `eddy_probe_method`; usb_camera `camera_position`, `camera_url`, `camera_circle_px`. **Save to dragonsnest.cfg** saves the visible fields only |
| **Console** | Recent G-code responses |

**Saving settings** writes the values into the `[dragons_den]` section of `dragonsnest.cfg` (`DD_SET_CONFIG`). Existing lines are replaced, commented-out options are uncommented in place, comments and the state block are kept. Klipper reads its config only at startup, so the panel then shows a **Restart Klipper** button. The settings must live in `dragonsnest.cfg`; if you moved `[dragons_den]` into `printer.cfg`, edit them there instead.

## Configuration reference

| Option | Default | Description |
|---|---|---|
| `tool_count` | `2` | Number of tools, `T0`..`T(n-1)`. T0 is the reference (offset 0,0,0) |
| `kinematics_type` | `fixed_gantry` | `fixed_gantry` (Trident: docks store X, Y) or `floating_gantry` (V2.4, SV08: docks store X, Y, Z) |
| `probe_type` | `usb_camera` | `eddy`, `sexball` or `usb_camera` |
| `dock_x_travel` | `-5.0` | X slide into the dock (undock uses the opposite sign) |
| `lock_retract_dist` | `3.0` | Extruder retract to unlock / extrude to lock |
| `clear_y_dist` | `70.0` | Pull-back clearance: the approach point is `dock_y + clear_y_dist` (`+Y`, toward the print area) |
| `front_dock_max_y` | middle of Y travel | Highest allowed dock Y (inclusive). Unset: dock Y must be below the middle of the Y axis |
| `travel_speed` / `dock_speed` / `verify_speed` | `150` / `50` / `10` | mm/s |
| `z_speed` / `jog_speed` / `lock_speed` | `15` / `50` / `20` | mm/s |
| `z_hop` | `5` | Z lift before a tool change and before calibration homing |
| `extruder` | `extruder` | Extruder used for the lock move |
| `register_t_commands` | `True` | Register `T0`..`Tn` |
| `ball_od` | (none) | sexball: ball diameter from calipers (required for sexball) |
| `ball_center` | (none) | sexball: estimated center `x, y` (ball on the bed) or `x, y, z` |
| `ball_clearance` | `5` | sexball: height above the ball top for approach |
| `eddy_xy_target` | (none) | eddy: fixed XY target used to align each nozzle |
| `eddy_z_point` | (none) | eddy: bed spot each nozzle probes for Z |
| `eddy_probe_method` | `tap` | eddy: sent as `PROBE METHOD=<value>`; empty for plain `PROBE` |
| `probe_start_z` | `5` | eddy: Z height to start probing from |
| `camera_position` | (none) | usb_camera: estimated `x, y, z` with T0 over the lens |
| `camera_url` | `/webcam/?action=stream` | usb_camera: MJPEG stream shown on the alignment page |
| `camera_circle_px` | `60` | usb_camera: default focus circle radius (stream pixels) |
| `paper_test_point` | bed center | Bed spot `x, y` for `DD_PAPER_TEST` |
| `toolhead_sensor_pin` | (none) | Switch that is triggered while a hotend is seated on the toolhead |
| `dock_sensor_pin_t0` .. `dock_sensor_pin_t<n-1>` | (none) | Switch that is triggered while that tool is seated in its dock |
| `pause_on_detach` | `True` | `PAUSE` when the toolhead loses its hotend during a print |
| `detach_gcode` | (none) | Extra G-code run when a hotend is lost during a print |
| `sensor_settle_time` | `0.3` | Seconds to wait for the sensor pins after a dock or pickup before checking them |
| `nest_file` | next to `printer.cfg` | Path of `dragonsnest.cfg` |

## Persistence (`dragonsnest.cfg`)

Everything you write in `dragonsnest.cfg` is left untouched. The plugin appends and maintains a block at the bottom:

```
#~# ---- Dragons Nest state (written by dragons_den; do not edit below) ----
#~# [state]
#~# active_tool = 1
#~# [tool T1]
#~# dock_x = 120.000000
#~# dock_y = 10.000000
#~# offset_x = 0.950000
...
```

Each line starts with `#`, so Klipper reads it as a comment. The plugin writes the block atomically every time a value changes. No `SAVE_CONFIG` or restart is needed.

## First-run calibration

Home and test basic motion before you start. Calibrate docks first, then offsets.

### 1. Dock locations: `DD_CALIBRATE_DOCKS`

A prompt opens for each tool, starting at T0 (`TOOL=n` starts at another tool):

1. Attach `Ti` to the toolhead by hand.
2. Jog with the prompt buttons, or press **Motors off** and push the toolhead by hand, until the bolt heads sit centered in the dock entry hole. Align X and Y on a fixed gantry; X, Y and Z on a floating gantry. Jogging works before homing: the plugin assumes mid-travel and measures the real position afterwards.
3. Press **Accept**. The toolhead pulls back `clear_y_dist` in `+Y` (out of the front dock), then homes `Y`, `X` (and `Z` on a floating gantry). The plugin counts the motor steps of that whole trip, works out the absolute dock position and saves it. A position in the rear half of the bed (or above `front_dock_max_y`) is rejected and not saved; re-position and press Accept again.
   **Motor skips corrupt the step count. If anything skips or crashes, restart the sequence.**
4. **Run slow verification dock?**
   - **Yes:** at `verify_speed` it moves to the approach point `dock_y + 70`, enters the dock moving `-Y`, slides X `-5`, retracts 3 mm to unlock and pulls back `+Y` 70 mm, leaving the tool in its dock.
   - **No:** dock the tool by hand.
5. Attach the next tool and press **Continue**.

### 2. Tool offsets: `DD_CALIBRATE_OFFSETS`

Run without arguments to pick a tool, or use `DD_CALIBRATE_OFFSETS TOOL=n`. T0 is measured first as the reference. Later single-tool runs reuse the stored T0 reference until you change `probe_type`. If docks are calibrated and the printer is homed, tools are swapped automatically. Otherwise the wizard asks you to swap by hand.

**sexball**
1. Set `ball_od` (calipers) and `ball_center` in `[dragons_den]`.
2. The nozzle moves above the estimated ball center. Jog (or turn motors off and move by hand) until the nozzle is centered over the ball, then press **OK**. Z lifts `z_hop`, the printer homes Y, X, Z, and the XY position is recorded.
3. Klipper's native manual probe opens (the Mainsail/Fluidd "TESTZ" dialog). Lower the nozzle onto the top of the ball with a paper test and **ACCEPT**. The Z result is saved.

**eddy**
1. Requires `[probe_eddy_current]`. For Z, `eddy_probe_method: tap` (default) needs a Klipper version with eddy tap support. A shuttle-mounted eddy coil can't see the nozzle without touching.
2. XY: jog the nozzle onto `eddy_xy_target` (a fixed pin or mark) and press **OK**. The printer homes to measure, as with the sexball. An eddy sensor can't locate the nozzle in XY, so this step is manual.
3. Z: every nozzle probes the same bed spot (`eddy_z_point`, adjusted for the tool's XY offset) with `PROBE METHOD=tap`.

**usb_camera**
1. Mount an upward-facing USB camera (crowsnest) and set `camera_position` and `camera_url`.
2. Open the **Dragons Den** panel from Mainsail's sidebar (the live view is in its Offset Calibration card), or the standalone page `http://<printer>/server/dragons_den/camera`. The standalone page can also be added in Mainsail as a webcam with service **HTML/iframe** so it shows on the dashboard.
3. Pick the tool and press **Start calibration**. The nozzle moves over the camera.
4. Use the 0.01 / 0.05 / 0.1 / 1.0 mm jog buttons under the feed to center the nozzle on the crosshair, and use Z to focus it to the reference circle (the slider adjusts the circle size).
5. Press **Save**. The plugin computes the offset from T0 and writes it to `dragonsnest.cfg`.

The printer prompt shows the same jog and Save controls if you'd rather stay in Mainsail/Fluidd.

**Paper Z-Test (`DD_PAPER_TEST`)** measures only a tool's Z offset, with Klipper's manual probe at `paper_test_point`. Run it for T0 first; that result is the Z reference. Each later tool then gets `offset_z = z(Ti) - z(T0)`, while its X/Y offsets stay as they were. Every nozzle tests the same bed spot, because the tool's XY offset is applied when moving there.

## G-code commands

| Command | Description |
|---|---|
| `T0`..`Tn` | Change tool |
| `DD_SELECT_TOOL TOOL=n` | Change tool; `TOOL=-1` parks the active tool |
| `DD_SET_ACTIVE_TOOL TOOL=n` | Tell the plugin which tool is attached (no motion; `-1` = none); also clears a detached-tool error |
| `DD_SET_OFFSET TOOL=n [X=] [Y=] [Z=]` | Set an offset by hand |
| `DD_CALIBRATE_DOCKS [TOOL=n] [SINGLE=1]` | Dock calibration wizard; `SINGLE=1` calibrates only `TOOL` |
| `DD_TEST_DOCK` | Slowly dock the active tool (`verify_speed`, front dock, `+Y` clearance) |
| `DD_CALIBRATE_OFFSETS [TOOL=n]` | Offset calibration wizard |
| `DD_PAPER_TEST [TOOL=n]` | Z-only offset by manual paper test (T0 first) |
| `DD_CAMERA_SAVE` | Save during usb_camera calibration |
| `DD_SET_CONFIG [OPTION=value ...]` | Write `[dragons_den]` options to `dragonsnest.cfg` (restart to apply). Accepts every option in the Settings card, e.g. `DD_SET_CONFIG PROBE_TYPE=eddy EDDY_XY_TARGET=120,130 CAMERA_URL="/webcam/?action=stream"`. `=none` unsets an optional value |
| `DD_JOG [X=] [Y=] [Z=]` / `DD_JOG_STEP STEP=` | Wizard jogging |
| `DD_STATUS` | Print docks, offsets, active tool and sensor states |
| `DD_ABORT` | Close the running wizard |

The plugin's state is also available to macros and the UI as `printer.dragons_den` (`active_tool`, `tool_state`, `sensors`, `docks`, `offsets`, `wizard`, ...).

**Renamed in this version:** all commands used to start with `DRAGONS_`. Replace `DRAGONS_` with `DD_` in your macros; `DRAGONS_SAVE_SETTINGS` is now `DD_SET_CONFIG`.

## Development

```bash
python3 -m pip install pytest
```

```bash
python3 -m pytest
```

The tests mock Klipper's `printer`, `gcode`, `toolhead` (with step-counting steppers) and `reactor`. They cover:
- fixed and floating gantry dock calibration, the front-dock limit and `+Y` clearance
- all three offset modes and the paper Z-test
- tool change kinematics and test docking
- toolhead and dock sensors: hand-attachment detection, ambiguous states, checked docks and pickups, detachment mid-print
- `dragonsnest.cfg` persistence and settings saving
- the panel's prompt mirroring
- the Mainsail sidebar merge and `install.sh`
- the Moonraker component

## License

GPLv3, see [LICENSE](LICENSE).
