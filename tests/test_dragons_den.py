"""Tests for klippy/extras/dragons_den.py using mocked Klipper objects."""
import os
import shutil

import pytest

import dragons_den
from fake_klipper import (CommandError, ConfigError, FakeConfig, FakePrinter,
                          make_module)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "config", "dragonsnest.cfg")

BASE_OPTIONS = {
    'tool_count': 2,
    'kinematics_type': 'fixed_gantry',
    'probe_type': 'usb_camera',
}


def make(tmp_path, new_api=True, ready=True, **options):
    opts = dict(BASE_OPTIONS)
    opts.update(options)
    printer = FakePrinter(str(tmp_path / "printer.cfg"), new_api=new_api)
    den = dragons_den.load_config(FakeConfig(printer, opts))
    printer.send_event("klippy:connect")
    if ready:
        printer.send_event("klippy:ready")
    return printer, den


def approx3(values):
    return pytest.approx(list(values), abs=1e-6)


def reload_state(tmp_path):
    return dragons_den.NestStore(str(tmp_path / "dragonsnest.cfg")).load()


######################################################################
# Configuration
######################################################################

def test_defaults(tmp_path):
    _, den = make(tmp_path)
    assert den.dock_x_travel == -5.0
    assert den.lock_retract_dist == 3.0
    assert den.clear_y_dist == 70.0
    assert den.state.active_tool == -1
    assert den.nest.path == str(tmp_path / "dragonsnest.cfg")


@pytest.mark.parametrize("options", [
    {'probe_type': 'laser'},
    {'kinematics_type': 'delta'},
    {'tool_count': 0},
    {'probe_type': 'sexball'},                  # ball_od missing
    {'ball_center': '1, 2, 3, 4'},
    {'camera_position': '150, 20'},             # needs x, y, z
])
def test_invalid_config(tmp_path, options):
    with pytest.raises(ConfigError):
        make(tmp_path, **options)


def test_t_commands_registered(tmp_path):
    printer, _ = make(tmp_path, tool_count=3)
    for name in ("T0", "T1", "T2"):
        assert name in printer.gcode.handlers
    assert "T3" not in printer.gcode.handlers


def test_t_commands_optional(tmp_path):
    printer, _ = make(tmp_path, register_t_commands='False')
    assert "T0" not in printer.gcode.handlers
    assert "DD_SELECT_TOOL" in printer.gcode.handlers


######################################################################
# dragonsnest.cfg persistence
######################################################################

def test_store_roundtrip(tmp_path):
    path = str(tmp_path / "nest.cfg")
    store = dragons_den.NestStore(path)
    state = dragons_den.NestState()
    state.active_tool = 1
    state.docks = {0: [10., 10., None], 1: [80.5, 11.25, 12.5]}
    state.offsets = {1: [0.125, -0.25, 0.05]}
    state.reference = {'method': 'usb_camera', 'pos': [150., 20., 10.]}
    store.save(state)
    loaded = store.load()
    assert loaded.active_tool == 1
    assert loaded.docks == state.docks
    assert loaded.offsets[1] == approx3(state.offsets[1])
    assert loaded.reference == state.reference
    # Every state line is a comment for Klipper's config parser
    text = open(path).read()
    after = text.split(dragons_den.STATE_MARKER, 1)[1]
    assert all(l.startswith("#") for l in after.splitlines() if l)


def test_store_preserves_user_config(tmp_path):
    path = tmp_path / "dragonsnest.cfg"
    shutil.copy(TEMPLATE, str(path))
    original = path.read_text()
    store = dragons_den.NestStore(str(path))
    assert store.load().active_tool == -1
    state = dragons_den.NestState()
    state.active_tool = 0
    store.save(state)
    store.save(state)   # second save replaces the block, does not append
    text = path.read_text()
    assert text.startswith(original.rstrip("\n"))
    assert text.count(dragons_den.STATE_MARKER) == 1
    assert "[dragons_den]" in text


def test_store_follows_symlink(tmp_path):
    real = tmp_path / "real.cfg"
    real.write_text("[dragons_den]\ntool_count: 2\n")
    link = tmp_path / "dragonsnest.cfg"
    os.symlink(str(real), str(link))
    state = dragons_den.NestState()
    state.active_tool = 1
    dragons_den.NestStore(str(link)).save(state)
    assert os.path.islink(str(link))
    assert dragons_den.NestStore(str(real)).load().active_tool == 1


def test_corrupt_state_is_config_error(tmp_path):
    (tmp_path / "dragonsnest.cfg").write_text(
        "[dragons_den]\n\n%s\n#~# [state]\n#~# active_tool = abc\n"
        % dragons_den.STATE_MARKER)
    with pytest.raises(ConfigError):
        make(tmp_path)


def test_state_survives_restart(tmp_path):
    shutil.copy(TEMPLATE, str(tmp_path / "dragonsnest.cfg"))
    printer, den = make(tmp_path, tool_count=3)
    g = printer.gcode
    g.run("DD_SET_OFFSET TOOL=2 X=0.5 Y=-0.25 Z=0.1")
    g.run("DD_SET_ACTIVE_TOOL TOOL=2")
    printer2, den2 = make(tmp_path, tool_count=3)
    assert den2.state.active_tool == 2
    assert den2.state.offset(2) == approx3([0.5, -0.25, 0.1])
    assert den2.get_status()['offsets']['T2']['x'] == pytest.approx(0.5)


######################################################################
# Prompts and jogging
######################################################################

def test_dock_prompt_contents(tmp_path):
    printer, _ = make(tmp_path)
    g = printer.gcode
    g.run("DD_CALIBRATE_DOCKS")
    prompts = g.prompts()
    assert prompts[0] == "// action:prompt_begin Dock calibration: T0 (1/2)"
    assert prompts[-1] == "// action:prompt_show"
    assert ("// action:prompt_button Accept|DD_RESPOND ACTION=accept"
            "|primary") in prompts
    assert ("// action:prompt_footer_button Cancel|DD_RESPOND"
            " ACTION=cancel|error") in prompts
    assert any("Motor skips" in p or "motor skips" in p for p in prompts)
    assert not any("DD_JOG Z=" in p for p in prompts)  # fixed gantry


def test_floating_prompt_has_z_jog(tmp_path):
    printer, _ = make(tmp_path, kinematics_type='floating_gantry')
    printer.gcode.run("DD_CALIBRATE_DOCKS")
    assert any("DD_JOG Z=" in p for p in printer.gcode.prompts())


def test_jog_step_rerenders(tmp_path):
    printer, den = make(tmp_path)
    g = printer.gcode
    g.run("DD_CALIBRATE_DOCKS")
    g.raw.clear()
    g.run("DD_JOG_STEP STEP=0.05")
    assert den.jog_step == 0.05
    assert "// action:prompt_button 0.05|DD_JOG_STEP STEP=0.05|primary" \
        in g.prompts()
    assert any("DD_JOG X=-0.05" in p for p in g.prompts())


def test_jog_requires_home_outside_wizard(tmp_path):
    printer, _ = make(tmp_path)
    with pytest.raises(CommandError):
        printer.gcode.run("DD_JOG X=1")


@pytest.mark.parametrize("new_api", [True, False])
def test_unhomed_jog_in_dock_wizard(tmp_path, new_api):
    """Both toolhead.set_position APIs (homing_axes str / tuple)."""
    printer, _ = make(tmp_path, new_api=new_api)
    th = printer.toolhead
    printer.gcode.run("DD_CALIBRATE_DOCKS")
    printer.gcode.run("DD_JOG X=5")
    assert th.homed == {'x', 'y'}          # Z untouched on fixed gantry
    assert th.get_position()[0] == pytest.approx(155.)
    assert any("not homed" in p for p in printer.gcode.prompts())
    with pytest.raises(CommandError):
        printer.gcode.run("DD_JOG Z=1")


######################################################################
# Dock calibration
######################################################################

def test_dock_calibration_fixed_gantry(tmp_path):
    printer, den = make(tmp_path)
    g, th = printer.gcode, printer.toolhead
    g.run("DD_CALIBRATE_DOCKS")
    # User pushes T0 into its front dock entry by hand (unhomed, motors off)
    th.hand_move(x=50., y=10., z=40.)
    g.run("DD_RESPOND ACTION=accept")
    # Pull-back before homing goes +Y (out of the front dock): the assumed
    # mid-travel Y of 150 becomes 150 + 70
    assert th.moves[0][0][1] == pytest.approx(220.)
    homes = [s for s in g.scripts if s.startswith("G28")]
    assert homes == ["G28 Y", "G28 X"]
    assert den.state.docks[0][:2] == pytest.approx([50., 10.])
    assert den.state.docks[0][2] is None
    assert den.state.active_tool == 0
    assert reload_state(tmp_path).docks[0][:2] == pytest.approx([50., 10.])

    # Slow verification dock: approach at +Y, enter -Y, pull out +Y
    th.moves.clear()
    g.run("DD_RESPOND ACTION=verify_yes")
    targets = [m[0] for m in th.moves]
    assert [t[:2] for t in targets] == [
        (50., 80.), (50., 10.), (45., 10.), (45., 10.), (45., 80.)]
    assert targets[3][3] == pytest.approx(-3.)      # unlock retract
    assert all(m[1] == den.verify_speed for m in th.moves[:3])
    assert den.state.active_tool == -1

    # Next tool, moved by hand after motors off
    g.run("DD_RESPOND ACTION=continue")
    assert den.state.active_tool == 1
    g.run("DD_RESPOND ACTION=motors_off")
    th.hand_move(x=120., y=10.5)
    g.run("DD_RESPOND ACTION=accept")
    assert den.state.docks[1][:2] == pytest.approx([120., 10.5])
    g.run("DD_RESPOND ACTION=verify_no")
    assert any("T1 is still attached" in p for p in g.prompts())
    g.run("DD_RESPOND ACTION=continue")
    assert den._wizard is None
    assert g.raw[-1] == "// action:prompt_end"
    assert den.state.active_tool == 1
    state = reload_state(tmp_path)
    assert sorted(state.docks) == [0, 1]


def test_dock_calibration_floating_gantry(tmp_path):
    printer, den = make(tmp_path, tool_count=1,
                        kinematics_type='floating_gantry')
    g, th = printer.gcode, printer.toolhead
    g.run("DD_CALIBRATE_DOCKS")
    th.hand_move(x=60., y=20., z=30.)
    g.run("DD_RESPOND ACTION=accept")
    assert [s for s in g.scripts if s.startswith("G28")] == \
        ["G28 Y", "G28 X", "G28 Z"]
    assert den.state.docks[0] == pytest.approx([60., 20., 30.])
    assert reload_state(tmp_path).docks[0] == pytest.approx([60., 20., 30.])
    th.moves.clear()
    g.run("DD_RESPOND ACTION=verify_yes")
    targets = [m[0] for m in th.moves]
    assert targets[0][:3] == pytest.approx((300., 300., 30.))  # rise first
    assert targets[1][:3] == pytest.approx((60., 90., 30.))    # approach +Y
    assert targets[2][:3] == pytest.approx((60., 20., 30.))    # enter -Y
    assert targets[-1][:3] == pytest.approx((55., 90., 30.))   # pull out +Y
    g.run("DD_RESPOND ACTION=continue")
    assert den._wizard is None


def test_dock_calibration_with_jog_when_homed(tmp_path):
    printer, den = make(tmp_path, tool_count=1)
    g = printer.gcode
    g.run("G28")
    g.run("DD_CALIBRATE_DOCKS")
    g.run("DD_JOG X=-100")
    g.run("DD_JOG_STEP STEP=0.1")
    g.run("DD_JOG Y=-290.3")
    g.run("DD_RESPOND ACTION=accept")
    assert den.state.docks[0][:2] == pytest.approx([200., 9.7])


def test_dock_calibration_rejects_rear_dock(tmp_path):
    """Default limit: dock Y must be below the middle of Y travel (150)."""
    printer, den = make(tmp_path)
    g, th = printer.gcode, printer.toolhead
    g.run("DD_CALIBRATE_DOCKS")
    th.hand_move(x=50., y=290.)
    with pytest.raises(CommandError, match="front dock bar"):
        g.run("DD_RESPOND ACTION=accept")
    assert den.state.docks == {}
    assert reload_state(tmp_path).docks == {}
    # The wizard stays on T0 so the user can re-position and retry
    assert den._wizard['step'] == 'position' and den._wizard['tool'] == 0
    g.run("DD_RESPOND ACTION=motors_off")
    th.hand_move(x=50., y=150.)            # exactly mid-travel: still rear
    with pytest.raises(CommandError, match="must be < 150.000"):
        g.run("DD_RESPOND ACTION=accept")
    g.run("DD_RESPOND ACTION=motors_off")
    th.hand_move(x=50., y=149.)
    g.run("DD_RESPOND ACTION=accept")
    assert den.state.docks[0][:2] == pytest.approx([50., 149.])


def test_front_dock_max_y_option(tmp_path):
    printer, den = make(tmp_path, front_dock_max_y=40)
    g, th = printer.gcode, printer.toolhead
    g.run("DD_CALIBRATE_DOCKS")
    th.hand_move(x=50., y=45.)
    with pytest.raises(CommandError, match="must be <= 40.000"):
        g.run("DD_RESPOND ACTION=accept")
    g.run("DD_RESPOND ACTION=motors_off")
    th.hand_move(x=50., y=40.)             # inclusive limit
    g.run("DD_RESPOND ACTION=accept")
    assert den.state.docks[0][:2] == pytest.approx([50., 40.])


def test_tool_change_rejects_stored_rear_dock(tmp_path):
    printer, den = make(tmp_path)
    den.state.docks = {0: [50., 10., None], 1: [120., 290., None]}
    g = printer.gcode
    g.run("G28")
    g.run("DD_SET_ACTIVE_TOOL TOOL=0")
    printer.toolhead.moves.clear()
    with pytest.raises(CommandError, match="T1 dock Y=290.000"):
        g.run("T1")
    assert printer.toolhead.moves == []     # rejected before any motion
    assert den.state.active_tool == 0


def test_dock_cancel(tmp_path):
    printer, den = make(tmp_path)
    printer.gcode.run("DD_CALIBRATE_DOCKS")
    printer.gcode.run("DD_RESPOND ACTION=cancel")
    assert den._wizard is None
    with pytest.raises(CommandError):
        printer.gcode.run("DD_RESPOND ACTION=accept")


######################################################################
# Tool change kinematics
######################################################################

def setup_tools(printer, den, floating=False):
    z = (lambda v: v) if floating else (lambda v: None)
    den.state.docks = {0: [50., 10., z(40.)], 1: [120., 10., z(42.)],
                       2: [190., 10., z(41.)]}
    den.state.offsets = {1: [1., -0.5, 0.2], 2: [-0.3, 0.4, -0.1]}
    printer.gcode.run("G28")
    printer.gcode.run("DD_SET_ACTIVE_TOOL TOOL=0")
    printer.toolhead.manual_move([100., 100., 20.], 100.)
    printer.toolhead.moves.clear()


def test_tool_change_fixed_gantry(tmp_path):
    printer, den = make(tmp_path, tool_count=3)
    setup_tools(printer, den)
    g, th = printer.gcode, printer.toolhead
    g.run("T1")
    assert [m[0] for m in th.moves] == [
        (100., 100., 25., 0.),                      # z hop
        (50., 80., 25., 0.),                        # T0 approach (+Y side)
        (50., 10., 25., 0.),                        # enter moving -Y
        (45., 10., 25., 0.),                        # slide into dock
        (45., 10., 25., -3.),                       # unlock
        (45., 80., 25., -3.),                       # pull back +Y
        (115., 80., 25., -3.),                      # T1 approach (+Y side)
        (115., 10., 25., -3.),                      # engage moving -Y
        (115., 10., 25., 0.),                       # lock
        (120., 10., 25., 0.),                       # slide out (+5)
        (120., 80., 25., 0.),                       # pull back +Y
        (101., 99.5, 25., 0.),                      # return + offset delta
    ]
    assert g.scripts[-2] == ("RESTORE_GCODE_STATE"
                             " NAME=_dragons_den_toolchange MOVE=0")
    assert g.scripts[-1] == ("SET_GCODE_OFFSET X_ADJUST=1.000000"
                             " Y_ADJUST=-0.500000 Z_ADJUST=0.200000 MOVE=1"
                             " MOVE_SPEED=15.000")
    assert den.state.active_tool == 1
    assert reload_state(tmp_path).active_tool == 1
    # T1 -> T2 applies the offset difference
    g.run("T2")
    assert g.scripts[-1].startswith(
        "SET_GCODE_OFFSET X_ADJUST=-1.300000 Y_ADJUST=0.900000"
        " Z_ADJUST=-0.300000")
    assert den.state.active_tool == 2


def test_tool_change_floating_gantry(tmp_path):
    printer, den = make(tmp_path, tool_count=3,
                        kinematics_type='floating_gantry')
    setup_tools(printer, den, floating=True)
    th = printer.toolhead
    printer.gcode.run("T2")
    zs = [m[0][2] for m in th.moves]
    assert zs[:3] == [25., 40., 40.]          # hop, rise to T0 dock Z, approach
    assert 41. in zs                          # T2 dock Z
    assert zs[-1] == 25.                      # back to safe Z
    assert den.state.active_tool == 2


def test_tool_change_cold_extruder_uses_force_move(tmp_path):
    printer, den = make(tmp_path, tool_count=3)
    setup_tools(printer, den)
    printer.extruder.heater.can_extrude = False
    printer.gcode.run("T1")
    assert printer.force_moves == [('extruder', -3.), ('extruder', 3.)]
    assert all(m[0][3] == 0. for m in printer.toolhead.moves)


def test_park_and_pickup_from_empty(tmp_path):
    printer, den = make(tmp_path, tool_count=3)
    setup_tools(printer, den)
    g = printer.gcode
    g.run("DD_SELECT_TOOL TOOL=-1")
    assert den.state.active_tool == -1
    assert g.scripts[-1].endswith("MOVE=0 MOVE_SPEED=15.000")
    printer.toolhead.moves.clear()
    g.run("T1")
    assert den.state.active_tool == 1
    assert printer.toolhead.moves[1][0][:2] == (115., 80.)   # straight pickup


def test_tool_change_errors(tmp_path):
    printer, den = make(tmp_path, tool_count=3)
    g = printer.gcode
    g.run("DD_SET_ACTIVE_TOOL TOOL=0")
    with pytest.raises(CommandError, match="home"):
        g.run("T1")
    g.run("G28")
    with pytest.raises(CommandError, match="dock for T0"):
        g.run("T1")
    with pytest.raises(CommandError):
        g.run("DD_SELECT_TOOL TOOL=3")
    printer.toolhead.moves.clear()
    g.run("T0")                       # already active: no motion
    assert printer.toolhead.moves == []


######################################################################
# Offset calibration
######################################################################

def test_offsets_sexball(tmp_path):
    printer, den = make(tmp_path, probe_type='sexball', ball_od=6.35,
                        ball_center="150, 150")
    mp = make_module(dragons_den)
    g, th = printer.gcode, printer.toolhead
    g.run("G28")
    g.run("DD_SET_ACTIVE_TOOL TOOL=0")
    g.run("DD_CALIBRATE_OFFSETS TOOL=1")   # no reference -> T0 first
    assert den._wizard['step'] == 'align' and den._wizard['tool'] == 0
    assert th.get_position()[:3] == pytest.approx([150., 150., 11.35])
    # T0: align by hand, OK, homing measures XY
    g.run("DD_RESPOND ACTION=motors_off")
    th.hand_move(x=150.4, y=149.8)
    g.scripts.clear()
    g.run("DD_RESPOND ACTION=ok")
    assert [s for s in g.scripts if s.startswith("G28")] == \
        ["G28 Y", "G28 X", "G28 Z"]
    assert den._wizard['step'] == 'manual_z'
    assert th.get_position()[:2] == pytest.approx([150.4, 149.8])
    mp.finish([150.4, 149.8, 6.5])                  # paper test ACCEPT
    assert den.state.reference['pos'] == pytest.approx([150.4, 149.8, 6.5])
    # T1: no docks, so the wizard asks for a manual swap
    assert den._wizard['step'] == 'attach' and den._wizard['tool'] == 1
    g.run("DD_RESPOND ACTION=attached")
    assert den.state.active_tool == 1
    g.run("DD_RESPOND ACTION=motors_off")
    th.hand_move(x=151.6, y=149.3)
    g.run("DD_RESPOND ACTION=ok")
    mp.finish([151.6, 149.3, 6.2])
    assert den.state.offsets[1] == pytest.approx([1.2, -0.5, -0.3])
    assert den._wizard['step'] == 'done'
    assert printer.gcode.offset_cmds[-1] == {
        'X': '1.200000', 'Y': '-0.500000', 'Z': '-0.300000', 'MOVE': '0'}
    assert reload_state(tmp_path).offsets[1] == pytest.approx(
        [1.2, -0.5, -0.3])
    g.run("DD_RESPOND ACTION=close")
    assert den._wizard is None


def test_offsets_sexball_manual_probe_abort(tmp_path):
    printer, den = make(tmp_path, probe_type='sexball', ball_od=6.35)
    mp = make_module(dragons_den)
    g = printer.gcode
    g.run("DD_SET_ACTIVE_TOOL TOOL=0")
    g.run("DD_CALIBRATE_OFFSETS TOOL=0")
    g.run("DD_RESPOND ACTION=ok")
    mp.finish(None)
    assert den._wizard is None
    assert den.state.reference is None


def test_offsets_eddy_with_auto_tool_change(tmp_path):
    printer, den = make(tmp_path, probe_type='eddy',
                        eddy_z_point="100, 100")
    probe = printer.add_probe()
    probe.z_for_tool = lambda: {0: 0.40, 1: 0.65}[den.state.active_tool]
    den.state.docks = {0: [50., 10., None], 1: [120., 10., None]}
    g, th = printer.gcode, printer.toolhead
    g.run("G28")
    g.run("DD_SET_ACTIVE_TOOL TOOL=0")
    g.run("DD_CALIBRATE_OFFSETS")
    assert den._wizard['step'] == 'select'
    assert any("All tools|DD_RESPOND ACTION=select TOOL=-1" in p
               for p in g.prompts())
    g.run("DD_RESPOND ACTION=select TOOL=-1")
    assert den._wizard['tool'] == 0 and den._wizard['step'] == 'align'
    g.run("DD_RESPOND ACTION=motors_off")
    th.hand_move(x=100.2, y=99.9)
    g.run("DD_RESPOND ACTION=ok")
    assert probe.calls[-1] == {'METHOD': 'tap'}
    assert den.state.reference['pos'] == pytest.approx([100.2, 99.9, 0.40])
    # T1 picked up automatically (homed + docks known)
    assert den.state.active_tool == 1
    assert "SET_GCODE_OFFSET" in " ".join(g.scripts)
    assert den._wizard['step'] == 'align' and den._wizard['tool'] == 1
    g.run("DD_RESPOND ACTION=motors_off")
    th.hand_move(x=101.0, y=99.4)
    g.run("DD_RESPOND ACTION=ok")
    # T1 probed the same bed spot: z point shifted by its XY offset
    probe_moves = [m[0] for m in th.moves if m[0][2] == pytest.approx(5.)]
    assert any(m[:2] == pytest.approx((100.8, 99.5)) for m in probe_moves)
    assert den.state.offsets[1] == pytest.approx([0.8, -0.5, 0.25])
    assert den._wizard['step'] == 'done'


def test_offsets_eddy_requires_probe(tmp_path):
    printer, _ = make(tmp_path, probe_type='eddy')
    with pytest.raises(CommandError, match="probe_eddy_current"):
        printer.gcode.run("DD_CALIBRATE_OFFSETS")


def test_offsets_usb_camera(tmp_path):
    printer, den = make(tmp_path, tool_count=3,
                        camera_position="150, 20, 10")
    den.state.docks = {0: [50., 10., None], 1: [120., 10., None],
                       2: [190., 10., None]}
    g, th = printer.gcode, printer.toolhead
    g.run("G28")
    g.run("DD_SET_ACTIVE_TOOL TOOL=0")
    g.run("DD_CALIBRATE_OFFSETS TOOL=2")
    assert den._wizard['step'] == 'camera' and den._wizard['tool'] == 0
    assert th.get_position()[:3] == pytest.approx([150., 20., 10.])
    assert any("/server/dragons_den/camera" in p for p in g.prompts())
    g.run("DD_JOG X=0.05")
    g.run("DD_JOG Y=-0.1")
    g.run("DD_JOG Z=0.01")
    g.run("DD_CAMERA_SAVE")
    assert den.state.reference == {'method': 'usb_camera',
                                   'pos': pytest.approx([150.05, 19.9, 10.01])}
    # T2 changed automatically, then moved over the camera
    assert den.state.active_tool == 2
    assert den._wizard['tool'] == 2
    assert th.get_position()[:3] == pytest.approx([150., 20., 10.])
    g.run("DD_JOG X=1")
    g.run("DD_JOG Y=0.5")
    g.run("DD_JOG Z=-0.2")
    g.run("DD_RESPOND ACTION=save")
    assert den.state.offsets[2] == pytest.approx([0.95, 0.6, -0.21])
    status = den.get_status()
    assert status['offsets']['T2']['x'] == pytest.approx(0.95)
    assert status['wizard_step'] == 'done'
    # A later single-tool run reuses the stored T0 reference
    g.run("DD_RESPOND ACTION=close")
    g.run("DD_CALIBRATE_OFFSETS TOOL=1")
    assert den._wizard['tool'] == 1
    assert den.state.active_tool == 1


def test_camera_save_outside_wizard(tmp_path):
    printer, _ = make(tmp_path)
    with pytest.raises(CommandError):
        printer.gcode.run("DD_CAMERA_SAVE")


def test_camera_requires_home(tmp_path):
    printer, den = make(tmp_path, camera_position="150, 20, 10")
    g = printer.gcode
    g.run("DD_SET_ACTIVE_TOOL TOOL=0")
    g.run("DD_CALIBRATE_OFFSETS TOOL=0")
    assert any("Home all|DD_RESPOND ACTION=home" in p
               for p in g.prompts())
    with pytest.raises(CommandError, match="home"):
        g.run("DD_RESPOND ACTION=save")
    g.run("DD_RESPOND ACTION=home")
    assert printer.toolhead.get_position()[:3] == pytest.approx(
        [150., 20., 10.])
    g.run("DD_RESPOND ACTION=save")
    assert den.state.reference['pos'] == pytest.approx([150., 20., 10.])


def test_offset_store_rejects_reference_from_other_method(tmp_path):
    printer, den = make(tmp_path)
    den.state.reference = {'method': 'sexball', 'pos': [0., 0., 0.]}
    assert den._offset_queue(1) == [0, 1]


######################################################################
# DD_ command names
######################################################################

def test_dd_commands_registered(tmp_path):
    printer, _ = make(tmp_path)
    for name in ("DD_SELECT_TOOL", "DD_SET_ACTIVE_TOOL", "DD_SET_OFFSET",
                 "DD_CALIBRATE_DOCKS", "DD_CALIBRATE_OFFSETS", "DD_JOG",
                 "DD_JOG_STEP", "DD_RESPOND", "DD_CAMERA_SAVE", "DD_ABORT",
                 "DD_STATUS", "DD_TEST_DOCK", "DD_PAPER_TEST",
                 "DD_SET_CONFIG"):
        assert name in printer.gcode.handlers, name
    assert not [n for n in printer.gcode.handlers if n.startswith("DRAGONS")]


######################################################################
# Toolhead / dock sensors and automatic tool detection
######################################################################

TH_PIN = "^PG11"
DOCK_PINS = {0: "PG12", 1: "PG13", 2: "PG14"}


def make_sensored(tmp_path, docks=(0, 1, 2), **options):
    opts = {'tool_count': 3, 'toolhead_sensor_pin': TH_PIN}
    for t in docks:
        opts['dock_sensor_pin_t%d' % t] = DOCK_PINS[t]
    opts.update(options)
    printer, den = make(tmp_path, **opts)
    return printer, den, printer.lookup_object('buttons')


def seat(buttons, toolhead=None, docks=()):
    """Set the sensor pins: toolhead True/False, docks {tool: seated}."""
    for tool, seated in dict(docks).items():
        buttons.press(DOCK_PINS[tool], seated)
    if toolhead is not None:
        buttons.press(TH_PIN, toolhead)


def test_sensor_status(tmp_path):
    printer, den, buttons = make_sensored(tmp_path, docks=(0, 1))
    st = den.get_status()
    assert st['sensors'] == {'toolhead': False,
                             'docks': {'T0': False, 'T1': False, 'T2': None}}
    assert st['tool_state'] == 'none' and st['detected_tool'] == -1
    seat(buttons, docks={0: True, 1: True})
    assert den.get_status()['sensors']['docks'] == {
        'T0': True, 'T1': True, 'T2': None}
    printer.gcode.run("DD_STATUS")
    assert "Sensors: toolhead empty | T0 dock seated T1 dock seated" \
        " T2 dock n/a" in printer.gcode.info[-1]


def test_no_sensors_status(tmp_path):
    _, den = make(tmp_path)
    st = den.get_status()
    assert st['sensors'] == {'toolhead': None,
                             'docks': {'T0': None, 'T1': None}}
    assert st['tool_state'] == 'none' and st['detected_tool'] is None


def test_manual_hand_attachment_is_detected(tmp_path):
    printer, den, buttons = make_sensored(tmp_path)
    den.state.offsets = {1: [1., -0.5, 0.2]}
    seat(buttons, docks={0: True, 1: True, 2: True})
    assert den.state.active_tool == -1
    # T1 lifted out of its dock by hand: nothing on the toolhead yet
    seat(buttons, docks={1: False})
    assert den.state.active_tool == -1
    # ... and pushed onto the toolhead
    seat(buttons, toolhead=True)
    assert den.state.active_tool == 1
    assert reload_state(tmp_path).active_tool == 1
    assert den.get_status()['tool_state'] == 'attached'
    assert printer.gcode.offset_cmds[-1] == {
        'X_ADJUST': '1.000000', 'Y_ADJUST': '-0.500000',
        'Z_ADJUST': '0.200000', 'MOVE': '0'}
    assert "Dragons Den: T1 detected on the toolhead" in printer.gcode.info
    # Removed by hand again (not printing): no tool, offset taken back
    seat(buttons, toolhead=False)
    assert den.state.active_tool == -1
    assert den.get_status()['tool_state'] == 'none'
    assert printer.gcode.offset_cmds[-1]['X_ADJUST'] == '-1.000000'
    assert "Dragons Den: T1 removed from the toolhead" in printer.gcode.info


def test_hand_swap_between_tools(tmp_path):
    printer, den, buttons = make_sensored(tmp_path)
    seat(buttons, docks={0: True, 1: True, 2: True})
    seat(buttons, docks={2: False}, toolhead=True)
    assert den.state.active_tool == 2
    # T2 back in its dock and T0 attached without the toolhead sensor
    # ever opening (fast swap): the single empty dock decides
    seat(buttons, docks={2: True, 0: False})
    assert den.state.active_tool == 0


def test_ambiguous_sensors_report_unknown(tmp_path):
    printer, den, buttons = make_sensored(tmp_path)
    setup_tools(printer, den)
    den.state.active_tool = -1
    seat(buttons, docks={0: True}, toolhead=True)   # T1 and T2 docks empty
    assert den.state.active_tool == -1
    st = den.get_status()
    assert st['tool_state'] == 'unknown' and st['detected_tool'] == -1
    with pytest.raises(CommandError, match="unidentified hotend"):
        printer.gcode.run("T1")
    # Resolved as soon as one of the docks reports its tool
    seat(buttons, docks={2: True})
    assert den.state.active_tool == 1
    assert den.get_status()['tool_state'] == 'attached'


def test_active_tool_kept_without_its_dock_sensor(tmp_path):
    printer, den, buttons = make_sensored(tmp_path, docks=(0, 1))
    seat(buttons, docks={0: True, 1: True})
    printer.gcode.run("DD_SET_ACTIVE_TOOL TOOL=2")
    seat(buttons, toolhead=True)
    assert den.state.active_tool == 2
    assert den.get_status()['tool_state'] == 'attached'


def printing_with_t1(tmp_path, **options):
    printer, den, buttons = make_sensored(tmp_path, **options)
    stats = printer.add_print_stats()
    setup_tools(printer, den)
    seat(buttons, docks={0: True, 2: True, 1: False}, toolhead=True)
    printer.gcode.run("DD_SET_ACTIVE_TOOL TOOL=1")
    stats.state = 'printing'
    return printer, den, buttons, stats


def test_unexpected_detachment_pauses_print(tmp_path):
    printer, den, buttons, stats = printing_with_t1(tmp_path)
    offsets_before = list(printer.gcode.offset_cmds)
    seat(buttons, toolhead=False)                   # T1 fell off
    assert den.detached is True
    assert stats.state == 'paused'
    assert printer.lookup_object('pause_resume').pauses == 1
    assert "PAUSE" in printer.gcode.scripts
    assert "!! Dragons Den: T1 detached from the toolhead!" \
        in printer.gcode.raw
    st = den.get_status()
    assert st['tool_state'] == 'detached'
    assert st['active_tool'] == 1                   # offsets stay applied
    assert st['prompt']['title'] == "Dragons Den: tool detached"
    assert printer.gcode.offset_cmds == offsets_before
    with pytest.raises(CommandError, match="detached"):
        printer.gcode.run("T0")
    # Re-attached by hand: detected, error cleared
    seat(buttons, toolhead=True)
    assert den.detached is False
    assert den.get_status()['tool_state'] == 'attached'
    assert den.state.active_tool == 1
    assert "Dragons Den: T1 re-attached" in printer.gcode.info
    printer.gcode.run("DD_RESPOND ACTION=close")
    assert den.get_status()['prompt'] is None


def test_detach_gcode_without_pause(tmp_path):
    printer, den, buttons, stats = printing_with_t1(
        tmp_path, pause_on_detach='False', detach_gcode='M117 T lost')
    printer.gcode.register_command('M117', lambda gcmd: None)
    seat(buttons, toolhead=False)
    assert den.get_status()['tool_state'] == 'detached'
    assert "PAUSE" not in printer.gcode.scripts
    assert "M117 T lost" in printer.gcode.scripts
    assert stats.state == 'printing'


def test_detachment_clears_with_set_active_tool(tmp_path):
    printer, den, buttons, stats = printing_with_t1(tmp_path)
    seat(buttons, toolhead=False)
    printer.gcode.run("DD_SET_ACTIVE_TOOL TOOL=-1")
    assert den.get_status()['tool_state'] == 'none'


def simulate_changer(monkeypatch, den, buttons, dock_ok=True, pick_ok=True):
    """Make the sensor pins follow the docking / pickup moves."""
    real_dock, real_pick = den._dock_tool, den._pickup_tool

    def dock(tool, *args, **kwargs):
        real_dock(tool, *args, **kwargs)
        if dock_ok:
            seat(buttons, toolhead=False, docks={tool: True})

    def pick(tool, *args, **kwargs):
        real_pick(tool, *args, **kwargs)
        if pick_ok:
            seat(buttons, docks={tool: False}, toolhead=True)
    monkeypatch.setattr(den, '_dock_tool', dock)
    monkeypatch.setattr(den, '_pickup_tool', pick)


def sensored_tools(tmp_path, monkeypatch, **kwargs):
    printer, den, buttons = make_sensored(tmp_path)
    setup_tools(printer, den)
    seat(buttons, docks={1: True, 2: True}, toolhead=True)
    assert den.state.active_tool == 0
    simulate_changer(monkeypatch, den, buttons, **kwargs)
    return printer, den, buttons


def test_tool_change_verified_by_sensors(tmp_path, monkeypatch):
    printer, den, buttons = sensored_tools(tmp_path, monkeypatch)
    info_before = len(printer.gcode.info)
    printer.gcode.run("T1")
    assert den.state.active_tool == 1
    assert den.get_status()['tool_state'] == 'attached'
    # Sensor changes during the change were expected: no hand-attach logic
    assert not [m for m in printer.gcode.info[info_before:]
                if "detected" in m or "removed" in m]
    assert printer.reactor.now > 100.3                 # waited to settle
    printer.gcode.run("DD_SELECT_TOOL TOOL=-1")
    assert den.state.active_tool == -1
    assert den.get_status()['sensors']['docks'] == {
        'T0': True, 'T1': True, 'T2': True}


def test_tool_change_detects_failed_pickup(tmp_path, monkeypatch):
    printer, den, _ = sensored_tools(tmp_path, monkeypatch, pick_ok=False)
    with pytest.raises(CommandError, match="T1 was not picked up: the"
                       " toolhead sensor reports no hotend and T1 is still"
                       " in its dock"):
        printer.gcode.run("T1")
    assert den.state.active_tool == -1                 # T0 did dock


def test_tool_change_detects_failed_dock(tmp_path, monkeypatch):
    printer, den, _ = sensored_tools(tmp_path, monkeypatch, dock_ok=False)
    with pytest.raises(CommandError, match="T0 did not dock"):
        printer.gcode.run("T2")
    assert den.state.active_tool == 0


def test_test_dock_verified_by_sensors(tmp_path, monkeypatch):
    printer, den, _ = sensored_tools(tmp_path, monkeypatch, dock_ok=False)
    with pytest.raises(CommandError, match="T0 did not dock"):
        printer.gcode.run("DD_TEST_DOCK")


def test_sensors_ignored_during_wizard(tmp_path):
    printer, den, buttons = make_sensored(tmp_path)
    seat(buttons, docks={0: True, 1: True, 2: True})
    printer.gcode.run("DD_CALIBRATE_DOCKS")
    seat(buttons, docks={0: False}, toolhead=True)     # T0 attached by hand
    assert den.state.active_tool == -1                 # wizard owns it
    printer.gcode.run("DD_RESPOND ACTION=cancel")
    assert den.state.active_tool == 0                  # reconciled after


######################################################################
# dock_sensors_enabled
######################################################################

def test_dock_sensors_enabled_by_default(tmp_path):
    printer, den, buttons = make_sensored(tmp_path)
    assert den.dock_sensors_enabled is True
    assert set(buttons.callbacks) == {TH_PIN} | set(DOCK_PINS.values())
    assert den.get_status()['settings']['dock_sensors_enabled'] is True


def test_dock_sensors_disabled_skips_dock_pins(tmp_path):
    printer, den, buttons = make_sensored(tmp_path,
                                          dock_sensors_enabled='False')
    assert set(buttons.callbacks) == {TH_PIN}     # dock pins not registered
    st = den.get_status()
    assert st['sensors']['docks'] == {'T0': None, 'T1': None, 'T2': None}
    assert st['settings']['dock_sensors_enabled'] is False
    # The pin options are still read, so Klipper accepts the config
    assert st['settings']['dock_sensor_pin_t1'] == DOCK_PINS[1]


def test_dock_sensors_disabled_without_toolhead_sensor(tmp_path):
    printer, den = make(tmp_path, dock_sensors_enabled='False',
                        dock_sensor_pin_t0='PG12')
    assert 'buttons' not in printer.objects        # nothing to register
    assert den.get_status()['tool_state'] == 'none'


@pytest.mark.parametrize("enabled", [True, False])
def test_hand_attachment_by_mode(tmp_path, enabled):
    printer, den, buttons = make_sensored(
        tmp_path, dock_sensors_enabled=str(enabled))
    if enabled:
        seat(buttons, docks={0: True, 1: True, 2: True})
        seat(buttons, docks={1: False})
    seat(buttons, toolhead=True)                    # T1 pushed on by hand
    st = den.get_status()
    if enabled:
        assert st['active_tool'] == 1 and st['tool_state'] == 'attached'
    else:
        # Only the toolhead sensor: seated, but which tool is unknown
        assert st['active_tool'] == -1 and st['tool_state'] == 'unknown'
        assert st['detected_tool'] == -1


def test_manual_tracking_without_dock_sensors(tmp_path):
    printer, den, buttons = make_sensored(tmp_path,
                                          dock_sensors_enabled='False')
    den.state.offsets = {2: [0.5, 0., 0.1]}
    seat(buttons, toolhead=True)
    assert den.get_status()['tool_state'] == 'unknown'
    # Active Tool Override in the panel
    printer.gcode.run("DD_SET_ACTIVE_TOOL TOOL=2")
    st = den.get_status()
    assert st['active_tool'] == 2 and st['tool_state'] == 'attached'
    assert reload_state(tmp_path).active_tool == 2
    # The toolhead sensor alone still confirms removal by hand ...
    seat(buttons, toolhead=False)
    assert den.state.active_tool == -1
    assert printer.gcode.offset_cmds[-1]['X_ADJUST'] == '-0.500000'
    assert reload_state(tmp_path).active_tool == -1
    # ... and a hand-attached hotend stays unidentified until overridden
    seat(buttons, toolhead=True)
    assert den.get_status()['tool_state'] == 'unknown'
    with pytest.raises(CommandError, match="unidentified hotend"):
        printer.gcode.run("T1")
    printer.gcode.run("DD_SET_ACTIVE_TOOL TOOL=0")
    assert den.get_status()['tool_state'] == 'attached'


def test_tool_changes_without_dock_sensors(tmp_path, monkeypatch):
    printer, den, buttons = make_sensored(tmp_path,
                                          dock_sensors_enabled='False')
    setup_tools(printer, den)
    seat(buttons, toolhead=True)
    assert den.get_status()['tool_state'] == 'attached'
    real_dock, real_pick = den._dock_tool, den._pickup_tool

    def dock(tool, *a, **k):
        real_dock(tool, *a, **k)
        buttons.press(TH_PIN, 0)

    def pick(tool, *a, **k):
        real_pick(tool, *a, **k)
        buttons.press(TH_PIN, 1)
    monkeypatch.setattr(den, '_dock_tool', dock)
    monkeypatch.setattr(den, '_pickup_tool', pick)
    printer.gcode.run("T2")
    assert den.state.active_tool == 2
    assert reload_state(tmp_path).active_tool == 2   # tracked in the nest
    # A pickup that leaves the toolhead empty is still caught
    monkeypatch.setattr(den, '_pickup_tool', real_pick)
    with pytest.raises(CommandError, match="T0 was not picked up: the"
                       " toolhead sensor reports no hotend"):
        printer.gcode.run("T0")
    # Restart: the active tool comes back from dragonsnest.cfg
    seat(buttons, toolhead=True)
    printer.gcode.run("DD_SET_ACTIVE_TOOL TOOL=0")
    printer2, den2, buttons2 = make_sensored(
        tmp_path, dock_sensors_enabled='False', ready=False)
    seat(buttons2, toolhead=True)                   # MCU reports the pin
    printer2.send_event("klippy:ready")             # then the startup check
    assert den2.state.active_tool == 0
    assert den2.get_status()['tool_state'] == 'attached'


def test_set_config_sensor_options_persist(tmp_path):
    import configparser
    nest = tmp_path / "dragonsnest.cfg"
    shutil.copy(TEMPLATE, str(nest))
    printer, den = make(tmp_path, tool_count=3)
    printer.gcode.run("DD_SET_CONFIG DOCK_SENSORS_ENABLED=False"
                      " TOOLHEAD_SENSOR_PIN=^PG11"
                      " DOCK_SENSOR_PIN_T0=^PG12"
                      " DOCK_SENSOR_PIN_T1=!toolboard:PB5")
    cp = configparser.ConfigParser(inline_comment_prefixes=('#',))
    cp.read(str(nest))
    section = dict(cp['dragons_den'])
    assert section['dock_sensors_enabled'] == 'False'
    assert section['toolhead_sensor_pin'] == '^PG11'
    assert section['dock_sensor_pin_t0'] == '^PG12'
    assert section['dock_sensor_pin_t1'] == '!toolboard:PB5'
    # After a restart Klipper loads them: dock pins kept but not used
    opts = dict(BASE_OPTIONS)
    opts.update({k: v for k, v in section.items()})
    printer2 = FakePrinter(str(tmp_path / "printer.cfg"))
    den2 = dragons_den.load_config(FakeConfig(printer2, opts))
    assert den2.dock_sensors_enabled is False
    assert set(printer2.objects['buttons'].callbacks) == {'^PG11'}
    # Turn them back on, clear a pin
    printer.gcode.run("DD_SET_CONFIG DOCK_SENSORS_ENABLED=on"
                      " DOCK_SENSOR_PIN_T1=none")
    cp = configparser.ConfigParser(inline_comment_prefixes=('#',))
    cp.read(str(nest))
    assert cp['dragons_den']['dock_sensors_enabled'] == 'True'
    assert 'dock_sensor_pin_t1' not in cp['dragons_den']


@pytest.mark.parametrize("params", [
    "DOCK_SENSORS_ENABLED=maybe", "TOOLHEAD_SENSOR_PIN=PG$1",
    "DOCK_SENSOR_PIN_T0=a:b:c",
])
def test_set_config_rejects_bad_sensor_values(tmp_path, params):
    printer, _ = make(tmp_path)
    with pytest.raises(CommandError):
        printer.gcode.run("DD_SET_CONFIG " + params)
