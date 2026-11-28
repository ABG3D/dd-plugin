"""Tests for the Dragons Den panel features: settings persistence, prompt
mirroring, per-tool dock calibration, test dock, paper Z-test, the Mainsail
sidebar theme merge, the Moonraker component and install.sh."""
import asyncio
import configparser
import importlib.util
import json
import os
import shutil
import subprocess

import pytest

import dragons_den
from fake_klipper import CommandError, make_module
from test_dragons_den import TEMPLATE, make, reload_state, setup_tools

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def user_section(path):
    """Parse the [dragons_den] section the way Klipper would."""
    cp = configparser.ConfigParser(inline_comment_prefixes=('#',))
    cp.read(str(path))
    return dict(cp['dragons_den'])


######################################################################
# Status for the panel
######################################################################

def test_status_exposes_settings(tmp_path):
    _, den = make(tmp_path, front_dock_max_y=40)
    s = den.get_status()['settings']
    assert set(s) == set(dragons_den.SETTINGS) | {'dock_sensor_pin_t0',
                                                 'dock_sensor_pin_t1'}
    assert s['dock_sensors_enabled'] is True
    assert s['toolhead_sensor_pin'] is None
    assert {k: s[k] for k in ('tool_count', 'kinematics_type', 'probe_type',
                              'dock_x_travel', 'lock_retract_dist',
                              'clear_y_dist', 'ball_od', 'front_dock_max_y',
                              'camera_url', 'eddy_xy_target')} == {
        'tool_count': 2, 'kinematics_type': 'fixed_gantry',
        'probe_type': 'usb_camera', 'dock_x_travel': -5.0,
        'lock_retract_dist': 3.0, 'clear_y_dist': 70.0, 'ball_od': 0.0,
        'front_dock_max_y': 40.0, 'camera_url': '/webcam/?action=stream',
        'eddy_xy_target': None}
    assert den.get_status()['prompt'] is None


def test_prompt_is_mirrored_in_status(tmp_path):
    printer, den = make(tmp_path)
    g = printer.gcode
    g.run("DD_CALIBRATE_DOCKS")
    prompt = den.get_status()['prompt']
    assert prompt['title'] == "Dock calibration: T0 (1/2)"
    buttons = [b for it in prompt['items'] if it['type'] == 'buttons'
               for b in it['buttons']]
    assert {'label': 'Accept', 'gcode': 'DD_RESPOND ACTION=accept',
            'style': 'primary'} in buttons
    assert any(b['gcode'] == 'DD_JOG X=1' for b in buttons)
    assert prompt['footer'] == [{'label': 'Cancel', 'style': 'error',
                                 'gcode': 'DD_RESPOND ACTION=cancel'}]
    json.dumps(den.get_status())          # Moonraker must serialize it
    g.run("DD_RESPOND ACTION=cancel")
    assert den.get_status()['prompt'] is None


######################################################################
# DD_SET_CONFIG
######################################################################

def test_save_settings_updates_dragonsnest(tmp_path):
    nest = tmp_path / "dragonsnest.cfg"
    shutil.copy(TEMPLATE, str(nest))
    printer, den = make(tmp_path)
    g = printer.gcode
    g.run("DD_SET_ACTIVE_TOOL TOOL=1")        # creates the state block
    g.run("DD_SET_CONFIG TOOL_COUNT=4 KINEMATICS_TYPE=floating_gantry"
          " PROBE_TYPE=sexball BALL_OD=6.35 DOCK_X_TRAVEL=-4.5"
          " LOCK_RETRACT_DIST=2.5 CLEAR_Y_DIST=60 FRONT_DOCK_MAX_Y=40")
    section = user_section(nest)
    assert section['tool_count'] == '4'
    assert section['kinematics_type'] == 'floating_gantry'
    assert section['probe_type'] == 'sexball'
    assert section['ball_od'] == '6.35'              # was commented out
    assert section['dock_x_travel'] == '-4.5'
    assert section['lock_retract_dist'] == '2.5'
    assert section['clear_y_dist'] == '60'
    assert section['front_dock_max_y'] == '40'
    text = nest.read_text()
    assert text.count("tool_count:") == 1             # replaced, not added
    assert "#   Number of tools" in text               # comments kept
    assert reload_state(tmp_path).active_tool == 1    # state block kept
    assert den.get_status()['restart_pending'] is True
    assert den.get_status()['pending_settings']['probe_type'] == 'sexball'
    assert den.get_status()['pending_settings']['tool_count'] == 4
    # Running settings only change after a restart
    assert den.tool_count == 2
    # Clearing an optional value comments it out
    g.run("DD_SET_CONFIG FRONT_DOCK_MAX_Y=none")
    assert 'front_dock_max_y' not in user_section(nest)


def test_save_probe_specific_settings(tmp_path):
    nest = tmp_path / "dragonsnest.cfg"
    shutil.copy(TEMPLATE, str(nest))
    printer, _ = make(tmp_path)
    printer.gcode.run(
        'DD_SET_CONFIG PROBE_TYPE=eddy EDDY_XY_TARGET=120,130.5'
        ' EDDY_Z_POINT=150,150 EDDY_PROBE_METHOD=tap PROBE_START_Z=4'
        ' CAMERA_URL="/webcam2/?action=stream" CAMERA_POSITION=150,20,10'
        ' CAMERA_CIRCLE_PX=80 BALL_CENTER=none')
    section = user_section(nest)
    assert section['probe_type'] == 'eddy'
    assert section['eddy_xy_target'] == '120, 130.5'
    assert section['eddy_z_point'] == '150, 150'
    assert section['eddy_probe_method'] == 'tap'
    assert section['probe_start_z'] == '4'
    assert section['camera_url'] == '/webcam2/?action=stream'
    assert section['camera_position'] == '150, 20, 10'
    assert section['camera_circle_px'] == '80'
    assert 'ball_center' not in section


def test_settings_probe_groups():
    groups = {k: v.get('probe') for k, v in dragons_den.SETTINGS.items()}
    assert {k for k, p in groups.items() if p == 'sexball'} == {
        'ball_od', 'ball_center', 'ball_clearance'}
    assert {k for k, p in groups.items() if p == 'eddy'} == {
        'eddy_xy_target', 'eddy_z_point', 'eddy_probe_method'}
    assert {k for k, p in groups.items() if p == 'usb_camera'} == {
        'camera_position', 'camera_url', 'camera_circle_px'}


def test_save_settings_adds_missing_section(tmp_path):
    printer, _ = make(tmp_path)
    printer.gcode.run("DD_SET_CONFIG TOOL_COUNT=3")
    assert user_section(tmp_path / "dragonsnest.cfg")['tool_count'] == '3'


@pytest.mark.parametrize("params", [
    "TOOL_COUNT=0", "TOOL_COUNT=x", "KINEMATICS_TYPE=delta",
    "PROBE_TYPE=laser", "CLEAR_Y_DIST=0", "LOCK_RETRACT_DIST=-1",
    "PROBE_TYPE=sexball BALL_OD=0", "PROBE_TYPE=sexball", "",
    "EDDY_XY_TARGET=1,2,3", "CAMERA_POSITION=1,2", "BALL_CENTER=a,b",
    "CAMERA_CIRCLE_PX=0", "PROBE_START_Z=0",
])
def test_save_settings_rejects_invalid(tmp_path, params):
    printer, _ = make(tmp_path)
    with pytest.raises(CommandError):
        printer.gcode.run(("DD_SET_CONFIG " + params).strip())
    assert not (tmp_path / "dragonsnest.cfg").exists()


######################################################################
# Per-tool dock calibration and test dock
######################################################################

def test_single_tool_dock_calibration(tmp_path):
    printer, den = make(tmp_path, tool_count=3)
    g, th = printer.gcode, printer.toolhead
    g.run("DD_CALIBRATE_DOCKS TOOL=1 SINGLE=1")
    assert den.get_status()['prompt']['title'] == \
        "Dock calibration: T1 (2/3)"
    th.hand_move(x=120., y=12.)
    g.run("DD_RESPOND ACTION=accept")
    g.run("DD_RESPOND ACTION=verify_no")
    assert any("Dock T1 is calibrated." in p for p in g.prompts())
    g.run("DD_RESPOND ACTION=continue")
    assert den._wizard is None                  # did not move on to T2
    assert sorted(den.state.docks) == [1]


def test_test_dock_slow_front_plus_y(tmp_path):
    printer, den = make(tmp_path, tool_count=3)
    setup_tools(printer, den)
    g, th = printer.gcode, printer.toolhead
    g.run("T1")
    th.moves.clear()
    g.run("DD_TEST_DOCK")
    targets = [m[0] for m in th.moves]
    assert targets[0][2] == pytest.approx(th.get_position()[2])    # z hop
    assert [t[:2] for t in targets[1:]] == [
        (120., 80.), (120., 10.), (115., 10.), (115., 10.), (115., 80.)]
    assert targets[4][3] == pytest.approx(-3.)     # unlock retract
    # XY moves at verify_speed; the extruder unlock runs at lock_speed
    assert [m[1] for m in th.moves[1:]] == [den.verify_speed] * 3 + \
        [den.lock_speed, den.verify_speed]
    assert den.state.active_tool == -1
    assert reload_state(tmp_path).active_tool == -1
    assert g.offset_cmds[-1] == {'X_ADJUST': '-1.000000',
                                 'Y_ADJUST': '0.500000',
                                 'Z_ADJUST': '-0.200000', 'MOVE': '0'}
    with pytest.raises(CommandError, match="no tool"):
        g.run("DD_TEST_DOCK")


def test_test_dock_rejects_rear_dock(tmp_path):
    printer, den = make(tmp_path)
    den.state.docks = {0: [50., 290., None]}
    printer.gcode.run("G28")
    printer.gcode.run("DD_SET_ACTIVE_TOOL TOOL=0")
    printer.toolhead.moves.clear()
    with pytest.raises(CommandError, match="front dock bar"):
        printer.gcode.run("DD_TEST_DOCK")
    assert printer.toolhead.moves == []


######################################################################
# Paper Z-test
######################################################################

def test_paper_test(tmp_path):
    printer, den = make(tmp_path, tool_count=3, paper_test_point="100, 120")
    mp = make_module(dragons_den)
    setup_tools(printer, den)
    g, th = printer.gcode, printer.toolhead
    with pytest.raises(CommandError, match="TOOL=0 first"):
        g.run("DD_PAPER_TEST TOOL=1")
    g.run("DD_PAPER_TEST")                      # active tool: T0
    assert th.get_position()[:3] == pytest.approx([100., 120., 5.])
    assert den.get_status()['wizard'] == 'paper'
    mp.finish([100., 120., 0.30])
    assert den.state.paper_ref_z == pytest.approx(0.30)
    assert reload_state(tmp_path).paper_ref_z == pytest.approx(0.30)
    assert den._wizard is None
    # T1: picked up automatically, moved to the same bed spot (XY offset)
    g.run("DD_PAPER_TEST TOOL=1")
    assert den.state.active_tool == 1
    assert th.get_position()[:3] == pytest.approx([101., 119.5, 5.2])
    mp.finish([101., 119.5, 0.75])
    assert den.state.offsets[1] == pytest.approx([1., -0.5, 0.45])  # XY kept
    assert g.offset_cmds[-1] == {'Z_ADJUST': '0.250000', 'MOVE': '0'}
    assert den.get_status()['paper_reference'] is True


def test_paper_test_abort_and_errors(tmp_path):
    printer, den = make(tmp_path)
    mp = make_module(dragons_den)
    g = printer.gcode
    with pytest.raises(CommandError, match="no tool attached"):
        g.run("DD_PAPER_TEST")
    g.run("DD_SET_ACTIVE_TOOL TOOL=0")
    with pytest.raises(CommandError, match="home"):
        g.run("DD_PAPER_TEST")
    g.run("G28")
    g.run("DD_PAPER_TEST")
    mp.finish(None)
    assert den._wizard is None
    assert den.state.paper_ref_z is None


######################################################################
# Mainsail sidebar theme (scripts/theme_install.py)
######################################################################

def load_theme_module():
    spec = importlib.util.spec_from_file_location(
        "theme_install", os.path.join(ROOT, "scripts", "theme_install.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ASSETS = os.path.join(ROOT, "mainsail")


def test_navi_asset_is_valid():
    entries = json.load(open(os.path.join(ASSETS, "navi.json")))
    assert entries[0]['title'] == "Dragons Den"
    assert entries[0]['href'] == "/server/dragons_den/panel"
    assert entries[0]['icon'].startswith("M")      # MDI SVG path


def test_theme_install_merges_and_is_idempotent(tmp_path):
    theme = load_theme_module()
    tdir = tmp_path / ".theme"
    tdir.mkdir()
    user_entry = {"title": "Docs", "href": "https://example.com"}
    (tdir / "navi.json").write_text(json.dumps([user_entry]))
    (tdir / "custom.css").write_text("body { color: red; }\n")
    theme.install(str(tdir), ASSETS)
    theme.install(str(tdir), ASSETS)               # re-run: no duplicates
    navi = json.loads((tdir / "navi.json").read_text())
    assert navi[0] == user_entry
    assert [e['title'] for e in navi] == ["Docs", "Dragons Den"]
    css = (tdir / "custom.css").read_text()
    assert css.startswith("body { color: red; }")
    assert css.count(theme.CSS_BEGIN) == 1
    assert "dragons_den/panel" in css
    theme.uninstall(str(tdir), ASSETS)
    assert json.loads((tdir / "navi.json").read_text()) == [user_entry]
    assert (tdir / "custom.css").read_text() == "body { color: red; }\n"


def test_theme_install_fresh_and_uninstall_cleans_up(tmp_path):
    theme = load_theme_module()
    tdir = tmp_path / ".theme"                     # does not exist yet
    theme.install(str(tdir), ASSETS)
    assert json.loads((tdir / "navi.json").read_text())[0]['title'] == \
        "Dragons Den"
    theme.uninstall(str(tdir), ASSETS)
    assert not (tdir / "navi.json").exists()
    assert not (tdir / "custom.css").exists()


def test_theme_install_refuses_invalid_navi(tmp_path):
    theme = load_theme_module()
    tdir = tmp_path / ".theme"
    tdir.mkdir()
    (tdir / "navi.json").write_text("{not json")
    assert theme.main(["x", "install", str(tdir), ASSETS]) == 1
    assert (tdir / "navi.json").read_text() == "{not json"


def test_install_sh_deploys_theme(tmp_path):
    klipper = tmp_path / "klipper"
    (klipper / "klippy" / "extras").mkdir(parents=True)
    config = tmp_path / "config"
    (config / ".theme").mkdir(parents=True)
    (config / ".theme" / "custom.css").write_text("/* mine */\n")
    script = os.path.join(ROOT, "install.sh")
    args = ["bash", script, "-n", "-k", str(klipper),
            "-m", str(tmp_path / "no-moonraker"), "-c", str(config)]
    out = subprocess.run(args, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    link = klipper / "klippy" / "extras" / "dragons_den.py"
    assert os.path.islink(str(link))
    assert (config / "dragonsnest.cfg").exists()
    assert "Dragons Den" in (config / ".theme" / "navi.json").read_text()
    assert (config / ".theme" / "custom.css").read_text() \
        .startswith("/* mine */")
    out = subprocess.run(args + ["-u"], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert not os.path.lexists(str(link))
    assert not (config / ".theme" / "navi.json").exists()
    assert (config / ".theme" / "custom.css").read_text() == "/* mine */\n"
    assert (config / "dragonsnest.cfg").exists()


######################################################################
# Moonraker component and panel page
######################################################################

class FakeMoonrakerConfig:
    def __init__(self, server):
        self.server = server

    def get_server(self):
        return self.server

    def get(self, option, default=None):
        return default


class FakeServer:
    def __init__(self, reject_kwargs=()):
        self.endpoints = {}
        self.reject_kwargs = set(reject_kwargs)

    def register_endpoint(self, path, methods, callback, **kwargs):
        if self.reject_kwargs & set(kwargs):
            raise TypeError("unexpected keyword")
        self.endpoints[path] = (callback, kwargs)


def load_component():
    spec = importlib.util.spec_from_file_location(
        "dd_moonraker", os.path.join(ROOT, "moonraker", "dragons_den.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_moonraker_component_serves_pages():
    mod = load_component()
    server = FakeServer()
    mod.load_component(FakeMoonrakerConfig(server))
    assert set(server.endpoints) == {"/server/dragons_den/camera",
                                     "/server/dragons_den/panel",
                                     "/server/dragons_den/info"}
    callback, kwargs = server.endpoints["/server/dragons_den/panel"]
    assert kwargs['wrap_result'] is False
    assert kwargs['content_type'].startswith("text/html")
    html = asyncio.run(callback(None))
    assert "<title>Dragons Den</title>" in html
    info = asyncio.run(server.endpoints["/server/dragons_den/info"][0](None))
    assert info['panel_page'] == "/server/dragons_den/panel"


def test_moonraker_component_older_api_fallback():
    mod = load_component()
    server = FakeServer(reject_kwargs={'auth_required'})
    mod.load_component(FakeMoonrakerConfig(server))
    _, kwargs = server.endpoints["/server/dragons_den/panel"]
    assert 'auth_required' not in kwargs and kwargs['wrap_result'] is False


def test_panel_page_has_all_controls():
    html = open(os.path.join(ROOT, "moonraker",
                             "dragons_den_panel.html")).read()
    for needle in [
        "DD_SET_CONFIG", "Save to dragonsnest.cfg",
        'name="tool_count"', 'name="kinematics_type"',
        'name="dock_x_travel"', 'name="lock_retract_dist"',
        'name="clear_y_dist"', 'name="probe_type"', 'name="ball_od"',
        "Calibrate All Docks", "SINGLE=1", "DD_TEST_DOCK",
        "Calibrate Offsets", "DD_PAPER_TEST", "TESTZ Z=",
        "DD_CAMERA_SAVE", "[0.01, 0.05, 0.1, 1.0]",
        "DD_SELECT_TOOL TOOL=-1", "Dock Active Tool",
        "/printer/objects/query?dragons_den", "href=\"/\"",
        "tool-indicator", "sensor-badges", "Detached", "tool_state",
        "mainsail&key=uiSettings", "--v-theme-primary", "v-card", "v-btn",
        "/server/files/config/.theme/custom.css",
        "Enable Dock Sensors", "DOCK_SENSORS_ENABLED=", "dock_sensor_pin_t",
        "Active Tool Override", "override-tool",
    ]:
        assert needle in html, needle
    assert "DRAGONS_" not in html


def test_panel_settings_match_module():
    """Every DD_SET_CONFIG option has a field, inside the data-probe group
    of the probe_type it belongs to (shown only for that probe)."""
    import re
    html = open(os.path.join(ROOT, "moonraker",
                             "dragons_den_panel.html")).read()
    form = html[html.index('<form id="settings"'):html.index('</form>')]
    names = set(re.findall(r'name="([a-z_]+)"', form))
    assert names == set(dragons_den.SETTINGS)
    groups = {}
    for probe, body in re.findall(
            r'<div data-probe="([a-z_]+)">(.*?)\n        </div>', form, re.S):
        groups[probe] = set(re.findall(r'name="([a-z_]+)"', body))
    expected = {}
    for key, spec in dragons_den.SETTINGS.items():
        if spec.get('probe'):
            expected.setdefault(spec['probe'], set()).add(key)
    assert groups == expected
    for probe in dragons_den.PROBE_TYPES:
        assert 'data-probe-cal="%s"' % probe in html


def test_theme_css_has_no_hardcoded_colors():
    css = open(os.path.join(ROOT, "mainsail", "custom.css")).read()
    import re
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b|rgb\(", css)
