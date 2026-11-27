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


def make(tmp_path, new_api=True, **options):
    opts = dict(BASE_OPTIONS)
    opts.update(options)
    printer = FakePrinter(str(tmp_path / "printer.cfg"), new_api=new_api)
    den = dragons_den.load_config(FakeConfig(printer, opts))
    printer.send_event("klippy:connect")
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
    assert "DRAGONS_SELECT_TOOL" in printer.gcode.handlers


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
    g.run("DRAGONS_SET_OFFSET TOOL=2 X=0.5 Y=-0.25 Z=0.1")
    g.run("DRAGONS_SET_ACTIVE_TOOL TOOL=2")
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
    g.run("DRAGONS_CALIBRATE_DOCKS")
    prompts = g.prompts()
    assert prompts[0] == "// action:prompt_begin Dock calibration: T0 (1/2)"
    assert prompts[-1] == "// action:prompt_show"
    assert ("// action:prompt_button Accept|DRAGONS_RESPOND ACTION=accept"
            "|primary") in prompts
    assert ("// action:prompt_footer_button Cancel|DRAGONS_RESPOND"
            " ACTION=cancel|error") in prompts
    assert any("Motor skips" in p or "motor skips" in p for p in prompts)
    assert not any("DRAGONS_JOG Z=" in p for p in prompts)  # fixed gantry


def test_floating_prompt_has_z_jog(tmp_path):
    printer, _ = make(tmp_path, kinematics_type='floating_gantry')
    printer.gcode.run("DRAGONS_CALIBRATE_DOCKS")
    assert any("DRAGONS_JOG Z=" in p for p in printer.gcode.prompts())


def test_jog_step_rerenders(tmp_path):
    printer, den = make(tmp_path)
    g = printer.gcode
    g.run("DRAGONS_CALIBRATE_DOCKS")
    g.raw.clear()
    g.run("DRAGONS_JOG_STEP STEP=0.05")
    assert den.jog_step == 0.05
    assert "// action:prompt_button 0.05|DRAGONS_JOG_STEP STEP=0.05|primary" \
        in g.prompts()
    assert any("DRAGONS_JOG X=-0.05" in p for p in g.prompts())


def test_jog_requires_home_outside_wizard(tmp_path):
    printer, _ = make(tmp_path)
    with pytest.raises(CommandError):
        printer.gcode.run("DRAGONS_JOG X=1")


@pytest.mark.parametrize("new_api", [True, False])
def test_unhomed_jog_in_dock_wizard(tmp_path, new_api):
    """Both toolhead.set_position APIs (homing_axes str / tuple)."""
    printer, _ = make(tmp_path, new_api=new_api)
    th = printer.toolhead
    printer.gcode.run("DRAGONS_CALIBRATE_DOCKS")
    printer.gcode.run("DRAGONS_JOG X=5")
    assert th.homed == {'x', 'y'}          # Z untouched on fixed gantry
    assert th.get_position()[0] == pytest.approx(155.)
    assert any("not homed" in p for p in printer.gcode.prompts())
    with pytest.raises(CommandError):
        printer.gcode.run("DRAGONS_JOG Z=1")


######################################################################
# Dock calibration
######################################################################

def test_dock_calibration_fixed_gantry(tmp_path):
    printer, den = make(tmp_path)
    g, th = printer.gcode, printer.toolhead
    g.run("DRAGONS_CALIBRATE_DOCKS")
    # User pushes T0 into its front dock entry by hand (unhomed, motors off)
    th.hand_move(x=50., y=10., z=40.)
    g.run("DRAGONS_RESPOND ACTION=accept")
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
    g.run("DRAGONS_RESPOND ACTION=verify_yes")
    targets = [m[0] for m in th.moves]
    assert [t[:2] for t in targets] == [
        (50., 80.), (50., 10.), (45., 10.), (45., 10.), (45., 80.)]
    assert targets[3][3] == pytest.approx(-3.)      # unlock retract
    assert all(m[1] == den.verify_speed for m in th.moves[:3])
    assert den.state.active_tool == -1

    # Next tool, moved by hand after motors off
    g.run("DRAGONS_RESPOND ACTION=continue")
    assert den.state.active_tool == 1
    g.run("DRAGONS_RESPOND ACTION=motors_off")
    th.hand_move(x=120., y=10.5)
    g.run("DRAGONS_RESPOND ACTION=accept")
    assert den.state.docks[1][:2] == pytest.approx([120., 10.5])
    g.run("DRAGONS_RESPOND ACTION=verify_no")
    assert any("T1 is still attached" in p for p in g.prompts())
    g.run("DRAGONS_RESPOND ACTION=continue")
    assert den._wizard is None
    assert g.raw[-1] == "// action:prompt_end"
    assert den.state.active_tool == 1
    state = reload_state(tmp_path)
    assert sorted(state.docks) == [0, 1]


def test_dock_calibration_floating_gantry(tmp_path):
    printer, den = make(tmp_path, tool_count=1,
                        kinematics_type='floating_gantry')
    g, th = printer.gcode, printer.toolhead
    g.run("DRAGONS_CALIBRATE_DOCKS")
    th.hand_move(x=60., y=20., z=30.)
    g.run("DRAGONS_RESPOND ACTION=accept")
    assert [s for s in g.scripts if s.startswith("G28")] == \
        ["G28 Y", "G28 X", "G28 Z"]
    assert den.state.docks[0] == pytest.approx([60., 20., 30.])
    assert reload_state(tmp_path).docks[0] == pytest.approx([60., 20., 30.])
    th.moves.clear()
    g.run("DRAGONS_RESPOND ACTION=verify_yes")
    targets = [m[0] for m in th.moves]
    assert targets[0][:3] == pytest.approx((300., 300., 30.))  # rise first
    assert targets[1][:3] == pytest.approx((60., 90., 30.))    # approach +Y
    assert targets[2][:3] == pytest.approx((60., 20., 30.))    # enter -Y
    assert targets[-1][:3] == pytest.approx((55., 90., 30.))   # pull out +Y
    g.run("DRAGONS_RESPOND ACTION=continue")
    assert den._wizard is None


def test_dock_calibration_with_jog_when_homed(tmp_path):
    printer, den = make(tmp_path, tool_count=1)
    g = printer.gcode
    g.run("G28")
    g.run("DRAGONS_CALIBRATE_DOCKS")
    g.run("DRAGONS_JOG X=-100")
    g.run("DRAGONS_JOG_STEP STEP=0.1")
    g.run("DRAGONS_JOG Y=-290.3")
    g.run("DRAGONS_RESPOND ACTION=accept")
    assert den.state.docks[0][:2] == pytest.approx([200., 9.7])


def test_dock_calibration_rejects_rear_dock(tmp_path):
    """Default limit: dock Y must be below the middle of Y travel (150)."""
    printer, den = make(tmp_path)
    g, th = printer.gcode, printer.toolhead
    g.run("DRAGONS_CALIBRATE_DOCKS")
    th.hand_move(x=50., y=290.)
    with pytest.raises(CommandError, match="front dock bar"):
        g.run("DRAGONS_RESPOND ACTION=accept")
    assert den.state.docks == {}
    assert reload_state(tmp_path).docks == {}
    # The wizard stays on T0 so the user can re-position and retry
    assert den._wizard['step'] == 'position' and den._wizard['tool'] == 0
    g.run("DRAGONS_RESPOND ACTION=motors_off")
    th.hand_move(x=50., y=150.)            # exactly mid-travel: still rear
    with pytest.raises(CommandError, match="must be < 150.000"):
        g.run("DRAGONS_RESPOND ACTION=accept")
    g.run("DRAGONS_RESPOND ACTION=motors_off")
    th.hand_move(x=50., y=149.)
    g.run("DRAGONS_RESPOND ACTION=accept")
    assert den.state.docks[0][:2] == pytest.approx([50., 149.])


def test_front_dock_max_y_option(tmp_path):
    printer, den = make(tmp_path, front_dock_max_y=40)
    g, th = printer.gcode, printer.toolhead
    g.run("DRAGONS_CALIBRATE_DOCKS")
    th.hand_move(x=50., y=45.)
    with pytest.raises(CommandError, match="must be <= 40.000"):
        g.run("DRAGONS_RESPOND ACTION=accept")
    g.run("DRAGONS_RESPOND ACTION=motors_off")
    th.hand_move(x=50., y=40.)             # inclusive limit
    g.run("DRAGONS_RESPOND ACTION=accept")
    assert den.state.docks[0][:2] == pytest.approx([50., 40.])


def test_tool_change_rejects_stored_rear_dock(tmp_path):
    printer, den = make(tmp_path)
    den.state.docks = {0: [50., 10., None], 1: [120., 290., None]}
    g = printer.gcode
    g.run("G28")
    g.run("DRAGONS_SET_ACTIVE_TOOL TOOL=0")
    printer.toolhead.moves.clear()
    with pytest.raises(CommandError, match="T1 dock Y=290.000"):
        g.run("T1")
    assert printer.toolhead.moves == []     # rejected before any motion
    assert den.state.active_tool == 0


def test_dock_cancel(tmp_path):
    printer, den = make(tmp_path)
    printer.gcode.run("DRAGONS_CALIBRATE_DOCKS")
    printer.gcode.run("DRAGONS_RESPOND ACTION=cancel")
    assert den._wizard is None
    with pytest.raises(CommandError):
        printer.gcode.run("DRAGONS_RESPOND ACTION=accept")


######################################################################
# Tool change kinematics
######################################################################

def setup_tools(printer, den, floating=False):
    z = (lambda v: v) if floating else (lambda v: None)
    den.state.docks = {0: [50., 10., z(40.)], 1: [120., 10., z(42.)],
                       2: [190., 10., z(41.)]}
    den.state.offsets = {1: [1., -0.5, 0.2], 2: [-0.3, 0.4, -0.1]}
    printer.gcode.run("G28")
    printer.gcode.run("DRAGONS_SET_ACTIVE_TOOL TOOL=0")
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
    g.run("DRAGONS_SELECT_TOOL TOOL=-1")
    assert den.state.active_tool == -1
    assert g.scripts[-1].endswith("MOVE=0 MOVE_SPEED=15.000")
    printer.toolhead.moves.clear()
    g.run("T1")
    assert den.state.active_tool == 1
    assert printer.toolhead.moves[1][0][:2] == (115., 80.)   # straight pickup


def test_tool_change_errors(tmp_path):
    printer, den = make(tmp_path, tool_count=3)
    g = printer.gcode
    g.run("DRAGONS_SET_ACTIVE_TOOL TOOL=0")
    with pytest.raises(CommandError, match="home"):
        g.run("T1")
    g.run("G28")
    with pytest.raises(CommandError, match="dock for T0"):
        g.run("T1")
    with pytest.raises(CommandError):
        g.run("DRAGONS_SELECT_TOOL TOOL=3")
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
    g.run("DRAGONS_SET_ACTIVE_TOOL TOOL=0")
    g.run("DRAGONS_CALIBRATE_OFFSETS TOOL=1")   # no reference -> T0 first
    assert den._wizard['step'] == 'align' and den._wizard['tool'] == 0
    assert th.get_position()[:3] == pytest.approx([150., 150., 11.35])
    # T0: align by hand, OK, homing measures XY
    g.run("DRAGONS_RESPOND ACTION=motors_off")
    th.hand_move(x=150.4, y=149.8)
    g.scripts.clear()
    g.run("DRAGONS_RESPOND ACTION=ok")
    assert [s for s in g.scripts if s.startswith("G28")] == \
        ["G28 Y", "G28 X", "G28 Z"]
    assert den._wizard['step'] == 'manual_z'
    assert th.get_position()[:2] == pytest.approx([150.4, 149.8])
    mp.finish([150.4, 149.8, 6.5])                  # paper test ACCEPT
    assert den.state.reference['pos'] == pytest.approx([150.4, 149.8, 6.5])
    # T1: no docks, so the wizard asks for a manual swap
    assert den._wizard['step'] == 'attach' and den._wizard['tool'] == 1
    g.run("DRAGONS_RESPOND ACTION=attached")
    assert den.state.active_tool == 1
    g.run("DRAGONS_RESPOND ACTION=motors_off")
    th.hand_move(x=151.6, y=149.3)
    g.run("DRAGONS_RESPOND ACTION=ok")
    mp.finish([151.6, 149.3, 6.2])
    assert den.state.offsets[1] == pytest.approx([1.2, -0.5, -0.3])
    assert den._wizard['step'] == 'done'
    assert printer.gcode.offset_cmds[-1] == {
        'X': '1.200000', 'Y': '-0.500000', 'Z': '-0.300000', 'MOVE': '0'}
    assert reload_state(tmp_path).offsets[1] == pytest.approx(
        [1.2, -0.5, -0.3])
    g.run("DRAGONS_RESPOND ACTION=close")
    assert den._wizard is None


def test_offsets_sexball_manual_probe_abort(tmp_path):
    printer, den = make(tmp_path, probe_type='sexball', ball_od=6.35)
    mp = make_module(dragons_den)
    g = printer.gcode
    g.run("DRAGONS_SET_ACTIVE_TOOL TOOL=0")
    g.run("DRAGONS_CALIBRATE_OFFSETS TOOL=0")
    g.run("DRAGONS_RESPOND ACTION=ok")
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
    g.run("DRAGONS_SET_ACTIVE_TOOL TOOL=0")
    g.run("DRAGONS_CALIBRATE_OFFSETS")
    assert den._wizard['step'] == 'select'
    assert any("All tools|DRAGONS_RESPOND ACTION=select TOOL=-1" in p
               for p in g.prompts())
    g.run("DRAGONS_RESPOND ACTION=select TOOL=-1")
    assert den._wizard['tool'] == 0 and den._wizard['step'] == 'align'
    g.run("DRAGONS_RESPOND ACTION=motors_off")
    th.hand_move(x=100.2, y=99.9)
    g.run("DRAGONS_RESPOND ACTION=ok")
    assert probe.calls[-1] == {'METHOD': 'tap'}
    assert den.state.reference['pos'] == pytest.approx([100.2, 99.9, 0.40])
    # T1 picked up automatically (homed + docks known)
    assert den.state.active_tool == 1
    assert "SET_GCODE_OFFSET" in " ".join(g.scripts)
    assert den._wizard['step'] == 'align' and den._wizard['tool'] == 1
    g.run("DRAGONS_RESPOND ACTION=motors_off")
    th.hand_move(x=101.0, y=99.4)
    g.run("DRAGONS_RESPOND ACTION=ok")
    # T1 probed the same bed spot: z point shifted by its XY offset
    probe_moves = [m[0] for m in th.moves if m[0][2] == pytest.approx(5.)]
    assert any(m[:2] == pytest.approx((100.8, 99.5)) for m in probe_moves)
    assert den.state.offsets[1] == pytest.approx([0.8, -0.5, 0.25])
    assert den._wizard['step'] == 'done'


def test_offsets_eddy_requires_probe(tmp_path):
    printer, _ = make(tmp_path, probe_type='eddy')
    with pytest.raises(CommandError, match="probe_eddy_current"):
        printer.gcode.run("DRAGONS_CALIBRATE_OFFSETS")


def test_offsets_usb_camera(tmp_path):
    printer, den = make(tmp_path, tool_count=3,
                        camera_position="150, 20, 10")
    den.state.docks = {0: [50., 10., None], 1: [120., 10., None],
                       2: [190., 10., None]}
    g, th = printer.gcode, printer.toolhead
    g.run("G28")
    g.run("DRAGONS_SET_ACTIVE_TOOL TOOL=0")
    g.run("DRAGONS_CALIBRATE_OFFSETS TOOL=2")
    assert den._wizard['step'] == 'camera' and den._wizard['tool'] == 0
    assert th.get_position()[:3] == pytest.approx([150., 20., 10.])
    assert any("/server/dragons_den/camera" in p for p in g.prompts())
    g.run("DRAGONS_JOG X=0.05")
    g.run("DRAGONS_JOG Y=-0.1")
    g.run("DRAGONS_JOG Z=0.01")
    g.run("DRAGONS_CAMERA_SAVE")
    assert den.state.reference == {'method': 'usb_camera',
                                   'pos': pytest.approx([150.05, 19.9, 10.01])}
    # T2 changed automatically, then moved over the camera
    assert den.state.active_tool == 2
    assert den._wizard['tool'] == 2
    assert th.get_position()[:3] == pytest.approx([150., 20., 10.])
    g.run("DRAGONS_JOG X=1")
    g.run("DRAGONS_JOG Y=0.5")
    g.run("DRAGONS_JOG Z=-0.2")
    g.run("DRAGONS_RESPOND ACTION=save")
    assert den.state.offsets[2] == pytest.approx([0.95, 0.6, -0.21])
    status = den.get_status()
    assert status['offsets']['T2']['x'] == pytest.approx(0.95)
    assert status['wizard_step'] == 'done'
    # A later single-tool run reuses the stored T0 reference
    g.run("DRAGONS_RESPOND ACTION=close")
    g.run("DRAGONS_CALIBRATE_OFFSETS TOOL=1")
    assert den._wizard['tool'] == 1
    assert den.state.active_tool == 1


def test_camera_save_outside_wizard(tmp_path):
    printer, _ = make(tmp_path)
    with pytest.raises(CommandError):
        printer.gcode.run("DRAGONS_CAMERA_SAVE")


def test_camera_requires_home(tmp_path):
    printer, den = make(tmp_path, camera_position="150, 20, 10")
    g = printer.gcode
    g.run("DRAGONS_SET_ACTIVE_TOOL TOOL=0")
    g.run("DRAGONS_CALIBRATE_OFFSETS TOOL=0")
    assert any("Home all|DRAGONS_RESPOND ACTION=home" in p
               for p in g.prompts())
    with pytest.raises(CommandError, match="home"):
        g.run("DRAGONS_RESPOND ACTION=save")
    g.run("DRAGONS_RESPOND ACTION=home")
    assert printer.toolhead.get_position()[:3] == pytest.approx(
        [150., 20., 10.])
    g.run("DRAGONS_RESPOND ACTION=save")
    assert den.state.reference['pos'] == pytest.approx([150., 20., 10.])


def test_offset_store_rejects_reference_from_other_method(tmp_path):
    printer, den = make(tmp_path)
    den.state.reference = {'method': 'sexball', 'pos': [0., 0., 0.]}
    assert den._offset_queue(1) == [0, 1]
