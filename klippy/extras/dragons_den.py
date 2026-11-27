# Dragons Den: automated hotend changer support for Klipper
#
# Copyright (C) 2026  ABG3D
#
# This file may be distributed under the terms of the GNU GPLv3 license.
"""Dragons Den hotend changer.

Provides tool changes (T0..Tn), dock location calibration and tool offset
calibration (eddy, sexball, usb_camera). All interaction happens through
Klipper's G-code action prompts ("// action:prompt_..."), which Mainsail and
Fluidd render natively.

Persistent state (active tool, dock coordinates, tool offsets, the T0
reference measurement) lives in a machine-written block at the bottom of
dragonsnest.cfg. Every line of that block starts with "#~#", so Klipper reads
it as a comment when the file is included from printer.cfg; this module parses
and rewrites it.

Offset convention: a tool's offset is the toolhead position where that tool's
nozzle reached the reference point minus the position where T0's nozzle did.
It is applied directly as a G-code offset (SET_GCODE_OFFSET), so T0 is always
0,0,0.
"""
import configparser
import logging
import os

VERSION = "0.0.1"

try:
    from . import manual_probe
except ImportError:  # imported outside klippy/extras (unit tests)
    manual_probe = None

KINEMATICS_TYPES = {'fixed_gantry': 'fixed_gantry',
                    'floating_gantry': 'floating_gantry'}
PROBE_TYPES = {'eddy': 'eddy', 'sexball': 'sexball',
               'usb_camera': 'usb_camera'}
MAX_TOOLS = 32
JOG_STEPS = (0.01, 0.05, 0.1, 1.0, 10.0)
AXES = "xyz"

STATE_MARKER = ("#~# ---- Dragons Nest state (written by dragons_den;"
                " do not edit below) ----")
STATE_PREFIX = "#~#"
DEFAULT_HEADER = [
    "# Dragons Den - Dragons Nest",
    "# The block below is written by dragons_den. Do not edit it by hand.",
]

TOOLCHANGE_STATE = "_dragons_den_toolchange"


def _num(value):
    """Format a number for prompts and G-code: 0.100 -> 0.1, 10.0 -> 10"""
    return ("%.3f" % (value,)).rstrip('0').rstrip('.')


def _xyz(pos):
    return "X=%.3f Y=%.3f Z=%.3f" % (pos[0], pos[1], pos[2])


######################################################################
# dragonsnest.cfg persistence
######################################################################

class NestError(Exception):
    pass


class NestState:
    """In-memory copy of everything persisted to dragonsnest.cfg."""
    def __init__(self):
        self.active_tool = -1
        self.docks = {}        # tool -> [x, y, z or None]
        self.offsets = {}      # tool -> [x, y, z]
        self.reference = None  # {'method': probe_type, 'pos': [x, y, z]}

    def offset(self, tool):
        if tool is None or tool <= 0:
            return [0., 0., 0.]
        return list(self.offsets.get(tool, [0., 0., 0.]))


class NestStore:
    """Reads and writes the "#~#" state block of dragonsnest.cfg.

    Lines above the state marker belong to the user and are preserved
    verbatim. Writes are atomic (temp file + rename) and follow symlinks.
    """
    def __init__(self, path):
        self.path = path

    def _read_lines(self):
        try:
            with open(self.path, 'r') as f:
                return f.read().splitlines()
        except (IOError, OSError):
            return None

    @staticmethod
    def _split(lines):
        user, body = [], []
        in_state = False
        for line in lines:
            if line.strip() == STATE_MARKER:
                in_state = True
                continue
            if in_state and line.startswith(STATE_PREFIX):
                body.append(line[len(STATE_PREFIX):].strip())
            else:
                user.append(line)
        return user, body

    def load(self):
        state = NestState()
        lines = self._read_lines()
        if not lines:
            return state
        _, body = self._split(lines)
        cp = configparser.ConfigParser(interpolation=None)
        try:
            cp.read_string("\n".join(body))
            if cp.has_section('state'):
                state.active_tool = cp.getint('state', 'active_tool',
                                              fallback=-1)
            if cp.has_section('reference'):
                state.reference = {
                    'method': cp.get('reference', 'method'),
                    'pos': [cp.getfloat('reference', a) for a in AXES]}
            for section in cp.sections():
                parts = section.split()
                if len(parts) != 2 or parts[0] != 'tool':
                    continue
                tool = int(parts[1].lstrip('Tt'))
                if cp.has_option(section, 'dock_x'):
                    z = None
                    if cp.has_option(section, 'dock_z'):
                        z = cp.getfloat(section, 'dock_z')
                    state.docks[tool] = [cp.getfloat(section, 'dock_x'),
                                         cp.getfloat(section, 'dock_y'), z]
                if cp.has_option(section, 'offset_x'):
                    state.offsets[tool] = [
                        cp.getfloat(section, 'offset_' + a) for a in AXES]
        except (configparser.Error, ValueError) as e:
            raise NestError("Unable to parse %s: %s" % (self.path, e))
        return state

    @staticmethod
    def encode(state):
        body = ["[state]",
                "version = %s" % (VERSION,),
                "active_tool = %d" % (state.active_tool,)]
        if state.reference is not None:
            body += ["", "[reference]",
                     "method = %s" % (state.reference['method'],)]
            body += ["%s = %.6f" % (a, v)
                     for a, v in zip(AXES, state.reference['pos'])]
        for tool in sorted(set(state.docks) | set(state.offsets)):
            body += ["", "[tool T%d]" % (tool,)]
            dock = state.docks.get(tool)
            if dock is not None:
                body += ["dock_x = %.6f" % (dock[0],),
                         "dock_y = %.6f" % (dock[1],)]
                if dock[2] is not None:
                    body.append("dock_z = %.6f" % (dock[2],))
            offset = state.offsets.get(tool)
            if offset is not None:
                body += ["offset_%s = %.6f" % (a, v)
                         for a, v in zip(AXES, offset)]
        return body

    def save(self, state):
        lines = self._read_lines()
        if lines is None:
            lines = list(DEFAULT_HEADER)
        user, _ = self._split(lines)
        while user and not user[-1].strip():
            user.pop()
        out = user + ["", STATE_MARKER]
        out += [(STATE_PREFIX + " " + l) if l else STATE_PREFIX
                for l in self.encode(state)]
        real = os.path.realpath(self.path)
        tmp = real + ".tmp"
        with open(tmp, 'w') as f:
            f.write("\n".join(out) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, real)


######################################################################
# Main module
######################################################################

class DragonsDen:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.toolhead = None
        # Core parameters
        self.tool_count = config.getint('tool_count', 2, minval=1,
                                        maxval=MAX_TOOLS)
        self.kinematics_type = config.getchoice(
            'kinematics_type', KINEMATICS_TYPES, 'fixed_gantry')
        self.floating = self.kinematics_type == 'floating_gantry'
        self.dock_x_travel = config.getfloat('dock_x_travel', -5.0)
        self.lock_retract_dist = config.getfloat('lock_retract_dist', 3.0,
                                                 minval=0.)
        self.clear_y_dist = config.getfloat('clear_y_dist', 70.0, above=0.)
        # Docks sit on a front-mounted dock bar (low Y). Defaults to the
        # front half of the Y axis when not set.
        self.front_dock_max_y = config.getfloat('front_dock_max_y', None)
        self.probe_type = config.getchoice('probe_type', PROBE_TYPES,
                                           'usb_camera')
        # Motion
        self.travel_speed = config.getfloat('travel_speed', 150., above=0.)
        self.dock_speed = config.getfloat('dock_speed', 50., above=0.)
        self.verify_speed = config.getfloat('verify_speed', 10., above=0.)
        self.z_speed = config.getfloat('z_speed', 15., above=0.)
        self.jog_speed = config.getfloat('jog_speed', 50., above=0.)
        self.lock_speed = config.getfloat('lock_speed', 20., above=0.)
        self.z_hop = config.getfloat('z_hop', 5., minval=0.)
        self.extruder_name = config.get('extruder', 'extruder')
        register_t = config.getboolean('register_t_commands', True)
        # sexball
        self.ball_od = config.getfloat('ball_od', 0., minval=0.)
        self.ball_center = config.getfloatlist('ball_center', None)
        if self.ball_center is not None and len(self.ball_center) not in (2,
                                                                         3):
            raise config.error("ball_center must be 'x, y' or 'x, y, z'")
        self.ball_clearance = config.getfloat('ball_clearance', 5., minval=0.)
        # eddy
        self.eddy_xy_target = config.getfloatlist('eddy_xy_target', None,
                                                  count=2)
        self.eddy_z_point = config.getfloatlist('eddy_z_point', None, count=2)
        self.eddy_probe_method = config.get('eddy_probe_method', 'tap').strip()
        self.probe_start_z = config.getfloat('probe_start_z', 5., above=0.)
        # usb_camera
        self.camera_position = config.getfloatlist('camera_position', None,
                                                   count=3)
        self.camera_url = config.get('camera_url', '/webcam/?action=stream')
        self.camera_circle_px = config.getint('camera_circle_px', 60,
                                              minval=1)
        if self.probe_type == 'sexball' and self.ball_od <= 0.:
            raise config.error(
                "ball_od (caliper measurement) must be set when probe_type"
                " is sexball")
        # Persistence
        nest_file = config.get('nest_file', None)
        if nest_file is None:
            main_cfg = self.printer.get_start_args().get('config_file', '')
            nest_file = os.path.join(os.path.dirname(main_cfg),
                                     'dragonsnest.cfg')
        self.nest = NestStore(os.path.expanduser(nest_file))
        try:
            self.state = self.nest.load()
        except NestError as e:
            logging.exception("dragons_den: %s", e)
            raise config.error(str(e))
        if self.state.active_tool >= self.tool_count:
            self.state.active_tool = -1
        # Wizard state
        self._wizard = None
        self.jog_step = 1.0
        # Events and commands
        self.printer.register_event_handler("klippy:connect",
                                            self._handle_connect)
        cmds = [
            ('DRAGONS_SELECT_TOOL', self.cmd_DRAGONS_SELECT_TOOL,
             "Dock the active tool and pick up TOOL (-1 parks only)"),
            ('DRAGONS_SET_ACTIVE_TOOL', self.cmd_DRAGONS_SET_ACTIVE_TOOL,
             "Tell Dragons Den which tool is attached (no motion)"),
            ('DRAGONS_SET_OFFSET', self.cmd_DRAGONS_SET_OFFSET,
             "Manually set a tool offset relative to T0"),
            ('DRAGONS_CALIBRATE_DOCKS', self.cmd_DRAGONS_CALIBRATE_DOCKS,
             "Start the dock location calibration wizard"),
            ('DRAGONS_CALIBRATE_OFFSETS', self.cmd_DRAGONS_CALIBRATE_OFFSETS,
             "Start the tool offset calibration wizard"),
            ('DRAGONS_JOG', self.cmd_DRAGONS_JOG,
             "Relative jog used by the Dragons Den wizards"),
            ('DRAGONS_JOG_STEP', self.cmd_DRAGONS_JOG_STEP,
             "Set the wizard jog step in mm"),
            ('DRAGONS_RESPOND', self.cmd_DRAGONS_RESPOND,
             "Button handler for Dragons Den prompts"),
            ('DRAGONS_CAMERA_SAVE', self.cmd_DRAGONS_CAMERA_SAVE,
             "Save the current tool position in usb_camera calibration"),
            ('DRAGONS_ABORT', self.cmd_DRAGONS_ABORT,
             "Abort the running Dragons Den wizard"),
            ('DRAGONS_STATUS', self.cmd_DRAGONS_STATUS,
             "Report Dragons Den docks, offsets and active tool"),
        ]
        for name, func, desc in cmds:
            self.gcode.register_command(name, func, desc=desc)
        if register_t:
            for tool in range(self.tool_count):
                self.gcode.register_command(
                    "T%d" % (tool,), self._make_tool_cmd(tool),
                    desc="Dragons Den: change to tool T%d" % (tool,))

    def _handle_connect(self):
        self.toolhead = self.printer.lookup_object('toolhead')

    def _make_tool_cmd(self, tool):
        def cmd(gcmd):
            self.change_tool(tool)
        return cmd

    ##################################################################
    # Status
    ##################################################################

    def get_status(self, eventtime=None):
        w = self._wizard or {}
        docks = {}
        for tool, d in self.state.docks.items():
            docks["T%d" % tool] = {'x': d[0], 'y': d[1], 'z': d[2]}
        offsets = {}
        for tool in range(self.tool_count):
            if tool == 0 or tool in self.state.offsets:
                o = self.state.offset(tool)
                offsets["T%d" % tool] = {'x': o[0], 'y': o[1], 'z': o[2]}
        return {
            'version': VERSION,
            'tool_count': self.tool_count,
            'active_tool': self.state.active_tool,
            'kinematics_type': self.kinematics_type,
            'probe_type': self.probe_type,
            'docks': docks,
            'offsets': offsets,
            'wizard': w.get('kind', ''),
            'wizard_step': w.get('step') or '',
            'wizard_tool': -1 if w.get('tool') is None else w['tool'],
            'jog_step': self.jog_step,
            'camera_url': self.camera_url,
            'camera_circle_px': self.camera_circle_px,
        }

    ##################################################################
    # Helpers
    ##################################################################

    def _error(self, msg):
        return self.printer.command_error("Dragons Den: " + msg)

    def _respond(self, msg):
        self.gcode.respond_info("Dragons Den: " + msg)

    def _save(self):
        try:
            self.nest.save(self.state)
        except (IOError, OSError) as e:
            logging.exception("dragons_den: unable to write %s",
                              self.nest.path)
            raise self._error("unable to write %s: %s" % (self.nest.path, e))

    def _run(self, script):
        self.gcode.run_script_from_command(script)

    def _check_tool(self, tool, allow_none=False):
        lo = -1 if allow_none else 0
        if tool < lo or tool >= self.tool_count:
            raise self._error("invalid tool %d (tool_count is %d)"
                              % (tool, self.tool_count))

    def _toolhead_status(self):
        return self.toolhead.get_status(self.reactor.monotonic())

    def _homed_axes(self):
        return self._toolhead_status()['homed_axes']

    def _require_homed(self, axes):
        homed = self._homed_axes()
        missing = [a for a in axes if a not in homed]
        if missing:
            raise self._error("home %s first (G28)"
                              % (''.join(missing).upper(),))

    def _require_dock(self, tool):
        if tool not in self.state.docks:
            raise self._error("dock for T%d is not calibrated"
                              " (run DRAGONS_CALIBRATE_DOCKS)" % (tool,))
        self._check_front_dock(tool, self.state.docks[tool][1])

    def _front_dock_limit(self):
        """(limit, inclusive): dock Y must be <= front_dock_max_y when set,
        otherwise strictly in the front half of the Y axis."""
        if self.front_dock_max_y is not None:
            return self.front_dock_max_y, True
        status = self._toolhead_status()
        y_min = status['axis_minimum'][1]
        y_max = status['axis_maximum'][1]
        return (y_min + y_max) / 2., False

    def _check_front_dock(self, tool, dock_y):
        """Docks live on a front-mounted dock bar: reject rear positions."""
        limit, inclusive = self._front_dock_limit()
        if dock_y <= limit if inclusive else dock_y < limit:
            return
        raise self._error(
            "T%d dock Y=%.3f is not on the front dock bar (must be %s %.3f)."
            " Docks must be front-mounted at low Y; recalibrate with"
            " DRAGONS_CALIBRATE_DOCKS%s." % (
                tool, dock_y, "<=" if inclusive else "<", limit,
                "" if inclusive else " or set front_dock_max_y"))

    def _max_z(self):
        return self._toolhead_status()['axis_maximum'][2]

    def _move(self, x=None, y=None, z=None, speed=None):
        self.toolhead.manual_move([x, y, z], speed or self.travel_speed)

    def _set_kinematic_position(self, pos, axes):
        """Set the toolhead position and mark `axes` as homed.

        Newer Klipper takes homing_axes as a string ("xy"), older releases
        as a tuple of axis indexes. A string passed to the old API raises
        TypeError while indexing the rails, so try the new form first.
        """
        try:
            self.toolhead.set_position(pos, homing_axes=axes)
        except TypeError:
            self.toolhead.set_position(
                pos, homing_axes=tuple(AXES.index(a) for a in axes))

    def _ensure_kinematic(self, axes):
        """Allow motion on unhomed axes by assuming the toolhead is at the
        middle of each unhomed axis. Only used inside wizards, where the true
        position is measured afterwards by homing. Returns True if any axis
        was assumed."""
        homed = self._homed_axes()
        missing = [a for a in axes if a not in homed]
        if not missing:
            return False
        status = self._toolhead_status()
        mins, maxs = status['axis_minimum'], status['axis_maximum']
        pos = self.toolhead.get_position()
        for a in missing:
            i = AXES.index(a)
            pos[i] = (mins[i] + maxs[i]) / 2.
        self._set_kinematic_position(pos, axes)
        if self._wizard is not None:
            self._wizard['assumed'] = True
        return True

    def _locate_by_homing(self, pre_move, home_axes):
        """Measure where the toolhead is right now in machine coordinates.

        Records every kinematic stepper's MCU step count, runs pre_move and
        the homing sequence, then walks the counted steps back from the
        homed position. MCU step counts are not reset by homing or
        SET_KINEMATIC_POSITION, so this works from an unhomed start and for
        any kinematics (cartesian, corexy, ...)."""
        th = self.toolhead
        th.wait_moves()
        kin = th.get_kinematics()
        steppers = kin.get_steppers()
        before = {s.get_name(): s.get_mcu_position() for s in steppers}
        if pre_move is not None:
            pre_move()
        for axis in home_axes:
            self._run("G28 %s" % (axis.upper(),))
        th.wait_moves()
        stepper_pos = {}
        for s in steppers:
            moved = (s.get_mcu_position() - before[s.get_name()]) \
                * s.get_step_dist()
            stepper_pos[s.get_name()] = s.get_commanded_position() - moved
        pos = kin.calc_position(stepper_pos)
        if self._wizard is not None:
            self._wizard['assumed'] = False
        return [pos[0], pos[1], pos[2]]

    def _lift(self, dist):
        pos = self.toolhead.get_position()
        self._move(z=min(pos[2] + dist, self._max_z()), speed=self.z_speed)

    def _safe_travel(self, x, y, z):
        """Travel to x, y, z: rise first, move XY, then descend."""
        pos = self.toolhead.get_position()
        high = min(max(pos[2], z), self._max_z())
        if high > pos[2]:
            self._move(z=high, speed=self.z_speed)
        self._move(x=x, y=y)
        if z != high:
            self._move(z=z, speed=self.z_speed)

    def _extruder_lock(self, dist):
        """Run the extruder to lock (+) or unlock (-) the hotend."""
        if not dist:
            return
        extruder = self.printer.lookup_object(self.extruder_name, None)
        if extruder is None:
            raise self._error("extruder '%s' not found" % (self.extruder_name,))
        if extruder.get_heater().can_extrude:
            e = self.toolhead.get_position()[3]
            self.toolhead.manual_move([None, None, None, e + dist],
                                      self.lock_speed)
            return
        try:
            self._run("FORCE_MOVE STEPPER=%s DISTANCE=%.4f VELOCITY=%.3f"
                      % (self.extruder_name, dist, self.lock_speed))
        except self.printer.command_error as e:
            raise self._error(
                "extruder is below min_extrude_temp and FORCE_MOVE failed"
                " (%s). Heat the hotend or set enable_force_move: True in"
                " [force_move]." % (e,))

    ##################################################################
    # Dock / undock kinematics
    ##################################################################

    def _dock_xyz(self, tool):
        x, y, z = self.state.docks[tool]
        return x, y, (z if self.floating else None)

    def _approach(self, x, y, z, travel_speed):
        """Travel to the approach point in front of a dock. On a floating
        gantry rise to dock Z before the XY travel if it is higher."""
        if z is not None and z > self.toolhead.get_position()[2]:
            self._move(z=z, speed=self.z_speed)
        self._move(x=x, y=y, speed=travel_speed)
        if z is not None and z != self.toolhead.get_position()[2]:
            self._move(z=z, speed=self.z_speed)

    def _approach_y(self, dock_y):
        """Docks are front-mounted and open toward +Y (the print area): the
        clearance point is behind the entry, at dock_y + clear_y_dist. The
        toolhead enters the dock moving -Y and pulls out moving +Y."""
        return dock_y + self.clear_y_dist

    def _dock_tool(self, tool, speed, travel_speed, return_z=None):
        """Put the attached tool into its dock.

        approach (x, y + clear) -> entry (x, y) moving -Y -> slide X by
        dock_x_travel -> extruder retract unlocks -> pull back +Y by
        clear_y_dist."""
        x, y, z = self._dock_xyz(tool)
        ay = self._approach_y(y)
        self._approach(x, ay, z, travel_speed)
        self._move(y=y, speed=speed)
        self._move(x=x + self.dock_x_travel, speed=speed)
        self._extruder_lock(-self.lock_retract_dist)
        self._move(y=ay, speed=speed)
        if z is not None and return_z is not None:
            self._move(z=return_z, speed=self.z_speed)

    def _pickup_tool(self, tool, speed, travel_speed, return_z=None):
        """Take a tool out of its dock (reverse of _dock_tool)."""
        x, y, z = self._dock_xyz(tool)
        ay = self._approach_y(y)
        self._approach(x + self.dock_x_travel, ay, z, travel_speed)
        self._move(y=y, speed=speed)
        self._extruder_lock(self.lock_retract_dist)
        self._move(x=x, speed=speed)
        self._move(y=ay, speed=speed)
        if z is not None and return_z is not None:
            self._move(z=return_z, speed=self.z_speed)

    def change_tool(self, target):
        self._check_tool(target, allow_none=True)
        current = self.state.active_tool
        if target == current:
            self._respond("T%d already active" % (target,) if target >= 0
                          else "no tool attached")
            return
        self._require_homed(AXES)
        if current >= 0:
            self._require_dock(current)
        if target >= 0:
            self._require_dock(target)
            if target != 0 and target not in self.state.offsets:
                self._respond("warning: T%d has no calibrated offset"
                              % (target,))
        th = self.toolhead
        self._run("SAVE_GCODE_STATE NAME=%s" % (TOOLCHANGE_STATE,))
        th.wait_moves()
        start = th.get_position()
        safe_z = min(start[2] + self.z_hop, self._max_z())
        self._move(z=safe_z, speed=self.z_speed)
        if current >= 0:
            self._dock_tool(current, self.dock_speed, self.travel_speed,
                            return_z=safe_z)
            self.state.active_tool = -1
            self._save()
        if target >= 0:
            self._pickup_tool(target, self.dock_speed, self.travel_speed,
                              return_z=safe_z)
            self.state.active_tool = target
            self._save()
        old = self.state.offset(current)
        new = self.state.offset(target)
        delta = [n - o for n, o in zip(new, old)]
        if target >= 0:
            self._move(x=start[0] + delta[0], y=start[1] + delta[1])
        self._run("RESTORE_GCODE_STATE NAME=%s MOVE=0" % (TOOLCHANGE_STATE,))
        self._run("SET_GCODE_OFFSET X_ADJUST=%.6f Y_ADJUST=%.6f Z_ADJUST=%.6f"
                  " MOVE=%d MOVE_SPEED=%.3f"
                  % (delta[0], delta[1], delta[2], 1 if target >= 0 else 0,
                     self.z_speed))
        self._respond("T%d active" % (target,) if target >= 0
                      else "parked, no tool attached")

    ##################################################################
    # Prompts (Mainsail / Fluidd action prompts)
    ##################################################################

    def _prompt(self, title, items, footer=()):
        """items: ('text', str) | ('button', label, gcode, style)
        | ('group', [(label, gcode, style), ...])"""
        out = ["action:prompt_begin %s" % (title,)]
        for item in items:
            if item[0] == 'text':
                out.append("action:prompt_text %s" % (item[1],))
            elif item[0] == 'button':
                out.append("action:prompt_button %s|%s|%s" % item[1:])
            elif item[0] == 'group':
                out.append("action:prompt_button_group_start")
                for b in item[1]:
                    out.append("action:prompt_button %s|%s|%s" % b)
                out.append("action:prompt_button_group_end")
        for b in footer:
            out.append("action:prompt_footer_button %s|%s|%s" % b)
        out.append("action:prompt_show")
        for line in out:
            self.gcode.respond_raw("// " + line)

    def _close_prompt(self):
        self.gcode.respond_raw("// action:prompt_end")

    @staticmethod
    def _action(name, **params):
        extra = "".join(" %s=%s" % (k.upper(), v)
                        for k, v in sorted(params.items()))
        return "DRAGONS_RESPOND ACTION=%s%s" % (name, extra)

    def _cancel_button(self):
        return ("Cancel", self._action('cancel'), "error")

    def _position_text(self):
        pos = self.toolhead.get_position()
        text = "Toolhead: %s" % (_xyz(pos),)
        if (self._wizard or {}).get('assumed'):
            text += " (not homed: relative only)"
        return ('text', text)

    def _jog_items(self, allow_z):
        step = _num(self.jog_step)
        items = [('text', "Jog step %s mm (Z+ moves the nozzle away from"
                          " the bed)" % (step,)),
                 ('group', [(_num(s), "DRAGONS_JOG_STEP STEP=%s" % _num(s),
                             "primary" if s == self.jog_step else "secondary")
                            for s in JOG_STEPS])]
        for axis in "XY" + ("Z" if allow_z else ""):
            items.append(('group', [
                ("%s-" % axis, "DRAGONS_JOG %s=-%s" % (axis, step), "info"),
                ("%s+" % axis, "DRAGONS_JOG %s=%s" % (axis, step), "info")]))
        return items

    def _render(self):
        w = self._wizard
        if w is None or w.get('step') is None:
            return
        render = getattr(self, "_render_%s_%s" % (w['kind'], w['step']),
                         None)
        if render is not None:
            render()

    def _end_wizard(self, message=None):
        self._wizard = None
        self._close_prompt()
        if message:
            self._respond(message)

    ##################################################################
    # Jogging
    ##################################################################

    def _free_jog_axes(self):
        """Axes the current wizard step lets the user jog while unhomed."""
        w = self._wizard
        if w is None:
            return ""
        if w['kind'] == 'docks' and w['step'] == 'position':
            return "xyz" if self.floating else "xy"
        if w['kind'] == 'offsets' and w['step'] == 'align':
            return "xyz"
        return ""

    def cmd_DRAGONS_JOG(self, gcmd):
        delta = [gcmd.get_float(a.upper(), 0.) for a in AXES]
        axes = "".join(a for a, d in zip(AXES, delta) if d)
        if not axes:
            return
        homed = self._homed_axes()
        if any(a not in homed for a in axes):
            free = self._free_jog_axes()
            if not all(a in free for a in axes):
                raise gcmd.error("Dragons Den: home %s before jogging"
                                 % (axes.upper(),))
            self._ensure_kinematic(free)
        pos = self.toolhead.get_position()
        speed = self.z_speed if delta[2] else self.jog_speed
        self.toolhead.manual_move(
            [p + d if d else None for p, d in zip(pos[:3], delta)], speed)
        self._render()

    def cmd_DRAGONS_JOG_STEP(self, gcmd):
        self.jog_step = gcmd.get_float('STEP', above=0., maxval=50.)
        self._render()

    ##################################################################
    # Simple commands
    ##################################################################

    def cmd_DRAGONS_SELECT_TOOL(self, gcmd):
        self.change_tool(gcmd.get_int('TOOL', minval=-1))

    def cmd_DRAGONS_SET_ACTIVE_TOOL(self, gcmd):
        tool = gcmd.get_int('TOOL', minval=-1)
        self._check_tool(tool, allow_none=True)
        self.state.active_tool = tool
        self._save()
        self._respond("active tool set to %s"
                      % ("T%d" % tool if tool >= 0 else "none",))

    def cmd_DRAGONS_SET_OFFSET(self, gcmd):
        tool = gcmd.get_int('TOOL', minval=1)
        self._check_tool(tool)
        cur = self.state.offset(tool)
        self.state.offsets[tool] = [gcmd.get_float(a.upper(), c)
                                    for a, c in zip(AXES, cur)]
        self._save()
        self._respond("T%d offset %s" % (tool,
                                         _xyz(self.state.offsets[tool])))

    def cmd_DRAGONS_STATUS(self, gcmd):
        lines = ["Dragons Den v%s: %s, probe %s, active %s" % (
            VERSION, self.kinematics_type, self.probe_type,
            "T%d" % self.state.active_tool if self.state.active_tool >= 0
            else "none")]
        for tool in range(self.tool_count):
            dock = self.state.docks.get(tool)
            dock_s = "not calibrated" if dock is None else (
                "X=%.3f Y=%.3f" % (dock[0], dock[1])
                + ("" if dock[2] is None else " Z=%.3f" % dock[2]))
            off_s = "reference" if tool == 0 else (
                _xyz(self.state.offsets[tool]) if tool in self.state.offsets
                else "not calibrated")
            lines.append("T%d dock: %s | offset: %s" % (tool, dock_s, off_s))
        gcmd.respond_info("\n".join(lines))

    def cmd_DRAGONS_ABORT(self, gcmd):
        self._end_wizard("calibration aborted")

    def cmd_DRAGONS_RESPOND(self, gcmd):
        action = gcmd.get('ACTION').lower()
        if action in ('cancel', 'close'):
            self._end_wizard("calibration cancelled" if action == 'cancel'
                             else None)
            return
        w = self._wizard
        if w is None:
            raise gcmd.error("Dragons Den: no calibration wizard is running")
        if action == 'motors_off':
            self._run("M84")
            self._render()
            return
        if action == 'home':
            self._run("G28")
            if w['kind'] == 'offsets' and w.get('tool') is not None:
                self._offsets_goto_target(w['tool'])
            self._render()
            return
        if w['kind'] == 'docks':
            self._docks_action(gcmd, action)
        else:
            self._offsets_action(gcmd, action)

    ##################################################################
    # Dock calibration wizard
    ##################################################################

    def cmd_DRAGONS_CALIBRATE_DOCKS(self, gcmd):
        start = gcmd.get_int('TOOL', 0, minval=0, maxval=self.tool_count - 1)
        self._wizard = {'kind': 'docks', 'step': 'position', 'tool': start,
                        'verified': False}
        self._render()

    def _render_docks_position(self):
        tool = self._wizard['tool']
        axes = "X, Y and Z" if self.floating else "X and Y"
        home = "Y, X and Z" if self.floating else "Y and X"
        items = [
            ('text', "1. Attach T%d to the toolhead." % (tool,)),
            ('text', "2. Jog (or turn motors off and push by hand) until the"
                     " bolt heads sit centered in the T%d dock entry hole."
                     " Align %s." % (tool, axes)),
            ('text', "Docks are on the front dock bar (low Y); the toolhead"
                     " enters moving -Y and pulls out moving +Y."),
            ('text', "3. Press Accept: the toolhead pulls back %smm in +Y and"
                     " homes %s to measure the dock position."
                     % (_num(self.clear_y_dist), home)),
            ('text', "WARNING: motor skips require restarting the sequence."),
            self._position_text(),
        ]
        items += self._jog_items(allow_z=self.floating)
        items.append(('group', [
            ("Accept", self._action('accept'), "primary"),
            ("Motors off", self._action('motors_off'), "warning")]))
        self._prompt("Dock calibration: T%d (%d/%d)"
                     % (tool, tool + 1, self.tool_count), items,
                     footer=[self._cancel_button()])

    def _render_docks_verify(self):
        tool = self._wizard['tool']
        dock = self.state.docks[tool]
        pos = "X=%.3f Y=%.3f" % (dock[0], dock[1])
        if dock[2] is not None:
            pos += " Z=%.3f" % (dock[2],)
        items = [
            ('text', "Dock T%d saved: %s" % (tool, pos)),
            ('text', "Run slow verification dock?"),
            ('text', "Yes moves to the entry point at %s mm/s, slides X %smm,"
                     " retracts %smm to unlock and pulls back +Y %smm, leaving"
                     " T%d in its dock." % (
                         _num(self.verify_speed), _num(self.dock_x_travel),
                         _num(self.lock_retract_dist),
                         _num(self.clear_y_dist), tool)),
            ('group', [("Yes", self._action('verify_yes'), "primary"),
                       ("No", self._action('verify_no'), "secondary")]),
        ]
        self._prompt("Dock calibration: T%d" % (tool,), items,
                     footer=[self._cancel_button()])

    def _render_docks_swap(self):
        w = self._wizard
        tool = w['tool']
        last = tool >= self.tool_count - 1
        items = []
        if w['verified']:
            items.append(('text', "T%d is parked in its dock." % (tool,)))
        if last:
            items.append(('text', "All %d docks are calibrated." %
                          (self.tool_count,)))
            if not w['verified']:
                items.append(('text', "T%d is still attached." % (tool,)))
            items.append(('button', "Finish", self._action('continue'),
                          "primary"))
        else:
            if not w['verified']:
                items.append(('text', "Manually dock T%d." % (tool,)))
            items.append(('text', "Attach T%d to the toolhead, then press"
                                  " Continue." % (tool + 1,)))
            items.append(('button', "Continue", self._action('continue'),
                          "primary"))
        self._prompt("Dock calibration: T%d" % (tool,), items,
                     footer=[self._cancel_button()])

    def _docks_action(self, gcmd, action):
        w = self._wizard
        tool = w['tool']
        step = w['step']
        if step == 'position' and action == 'accept':
            axes = "xyz" if self.floating else "xy"
            self._ensure_kinematic(axes)

            def pull_back():
                y = self.toolhead.get_position()[1]
                self._move(y=self._approach_y(y), speed=self.dock_speed)
            pos = self._locate_by_homing(
                pull_back, "yxz" if self.floating else "yx")
            # Reject rear positions before anything is saved; the prompt
            # stays on this tool so the user can re-position and retry.
            self._check_front_dock(tool, pos[1])
            self.state.docks[tool] = [pos[0], pos[1],
                                      pos[2] if self.floating else None]
            self.state.active_tool = tool
            self._save()
            self._respond("T%d dock saved at X=%.3f Y=%.3f%s" % (
                tool, pos[0], pos[1],
                " Z=%.3f" % pos[2] if self.floating else ""))
            w['step'] = 'verify'
        elif step == 'verify' and action == 'verify_yes':
            self._require_homed("xyz" if self.floating else "xy")
            self._dock_tool(tool, self.verify_speed, self.verify_speed)
            self.state.active_tool = -1
            self._save()
            w['verified'] = True
            w['step'] = 'swap'
        elif step == 'verify' and action == 'verify_no':
            w['verified'] = False
            w['step'] = 'swap'
        elif step == 'swap' and action == 'continue':
            if tool >= self.tool_count - 1:
                self._end_wizard("dock calibration complete")
                return
            w['tool'] = tool + 1
            w['verified'] = False
            w['step'] = 'position'
            self.state.active_tool = tool + 1
            self._save()
        else:
            raise gcmd.error("Dragons Den: unexpected action '%s' in step"
                             " '%s'" % (action, step))
        self._render()

    ##################################################################
    # Offset calibration wizard
    ##################################################################

    def _reference(self):
        ref = self.state.reference
        if ref is None or ref['method'] != self.probe_type:
            return None
        return ref['pos']

    def _offset_queue(self, tool):
        if tool is None or tool < 0:
            return list(range(self.tool_count))
        if tool != 0 and self._reference() is None:
            return [0, tool]
        return [tool]

    def cmd_DRAGONS_CALIBRATE_OFFSETS(self, gcmd):
        if self.probe_type == 'eddy' and \
                self.printer.lookup_object('probe', None) is None:
            raise gcmd.error("Dragons Den: probe_type eddy needs a configured"
                             " [probe_eddy_current]")
        if self.probe_type == 'sexball' and manual_probe is None:
            raise gcmd.error("Dragons Den: manual_probe is unavailable")
        tool = gcmd.get_int('TOOL', None, minval=-1,
                            maxval=self.tool_count - 1)
        self._wizard = {'kind': 'offsets', 'step': None, 'queue': [],
                        'tool': None, 'results': {}}
        if tool is None:
            self._wizard['step'] = 'select'
            self._render()
            return
        self._wizard['queue'] = self._offset_queue(tool)
        self._offsets_next(gcmd)

    def _render_offsets_select(self):
        buttons = [("T%d" % t, self._action('select', tool=t),
                    "primary" if t == 0 else "secondary")
                   for t in range(self.tool_count)]
        items = [('text', "Probe type: %s. T0 is the reference (0,0,0);"
                          " calibrate it before the other tools."
                  % (self.probe_type,)),
                 ('button', "All tools", self._action('select', tool=-1),
                  "primary")]
        for i in range(0, len(buttons), 4):
            items.append(('group', buttons[i:i + 4]))
        self._prompt("Tool offset calibration", items,
                     footer=[self._cancel_button()])

    def _can_auto_change(self, tool):
        homed = self._homed_axes()
        if any(a not in homed for a in AXES):
            return False
        cur = self.state.active_tool
        return tool in self.state.docks and (cur < 0
                                             or cur in self.state.docks)

    def _offsets_next(self, gcmd):
        w = self._wizard
        if not w['queue']:
            self._offsets_finish()
            return
        tool = w['queue'].pop(0)
        w['tool'] = tool
        if self.state.active_tool != tool:
            if not self._can_auto_change(tool):
                w['step'] = 'attach'
                self._render()
                return
            self.change_tool(tool)
        self._offsets_measure()

    def _offsets_measure(self):
        w = self._wizard
        w['step'] = 'camera' if self.probe_type == 'usb_camera' else 'align'
        if all(a in self._homed_axes() for a in AXES):
            self._offsets_goto_target(w['tool'])
        self._render()

    def _target_xyz(self):
        """Toolhead position for T0 at the calibration target, or None."""
        if self.probe_type == 'usb_camera':
            return self.camera_position
        if self.probe_type == 'sexball':
            if self.ball_center is None:
                return None
            return [self.ball_center[0], self.ball_center[1],
                    self._ball_top() + self.ball_clearance]
        target = self.eddy_xy_target or self.eddy_z_point
        if target is None:
            return None
        return [target[0], target[1], self.probe_start_z]

    def _offsets_goto_target(self, tool):
        target = self._target_xyz()
        if target is None:
            return
        off = self.state.offset(tool)
        self._safe_travel(target[0] + off[0], target[1] + off[1],
                          target[2] + off[2])

    def _ball_top(self):
        if self.ball_center is not None and len(self.ball_center) == 3:
            return self.ball_center[2] + self.ball_od / 2.
        return self.ball_od

    def _render_offsets_attach(self):
        tool = self._wizard['tool']
        cur = self.state.active_tool
        items = []
        if cur >= 0:
            items.append(('text', "Return T%d to its dock by hand." % (cur,)))
        items += [('text', "Attach T%d to the toolhead, then press"
                           " Continue." % (tool,)),
                  ('button', "T%d attached" % (tool,),
                   self._action('attached'), "primary")]
        self._prompt("Tool offsets: attach T%d" % (tool,), items,
                     footer=[self._cancel_button()])

    def _render_offsets_align(self):
        tool = self._wizard['tool']
        if self.probe_type == 'sexball':
            target = "the center of the calibration ball (OD %s mm)" % (
                _num(self.ball_od),)
            z_note = ("Then lower the nozzle onto the top of the ball with"
                      " TESTZ (paper test) and ACCEPT.")
        else:
            target = "the XY reference target"
            z_note = ("Then the eddy probe measures Z at the probe point%s."
                      % (" (METHOD=%s)" % self.eddy_probe_method
                         if self.eddy_probe_method else ""))
        items = [
            ('text', "Jog the T%d nozzle until it is centered over %s."
             % (tool, target)),
            ('text', "Press OK: Z lifts %smm and the printer homes Y, X and Z"
                     " to measure the position. %s" % (_num(self.z_hop),
                                                       z_note)),
            self._position_text(),
        ]
        items += self._jog_items(allow_z=True)
        items.append(('group', [
            ("OK", self._action('ok'), "primary"),
            ("Motors off", self._action('motors_off'), "warning")]))
        self._prompt("Tool offsets: T%d XY" % (tool,), items,
                     footer=[self._cancel_button()])

    def _render_offsets_camera(self):
        tool = self._wizard['tool']
        homed = all(a in self._homed_axes() for a in AXES)
        items = [
            ('text', "Open http://<printer>/server/dragons_den/camera for the"
                     " live feed with crosshair."),
            ('text', "Center the T%d nozzle on the crosshair and focus it to"
                     " the reference circle, then press Save." % (tool,)),
        ]
        if not homed:
            items.append(('text', "The printer must be homed first."))
            items.append(('button', "Home all", self._action('home'),
                          "warning"))
        else:
            items.append(self._position_text())
            items += self._jog_items(allow_z=True)
            items.append(('button', "Save T%d" % (tool,),
                          self._action('save'), "primary"))
        self._prompt("Tool offsets: T%d camera" % (tool,), items,
                     footer=[self._cancel_button()])

    def _render_offsets_done(self):
        items = []
        for tool, off in sorted(self._wizard['results'].items()):
            items.append(('text', "T%d: %s" % (tool, _xyz(off))))
        items.append(('button', "Close", self._action('close'), "primary"))
        self._prompt("Tool offsets saved", items)

    def _offsets_action(self, gcmd, action):
        w = self._wizard
        step = w['step']
        if step == 'select' and action == 'select':
            tool = gcmd.get_int('TOOL', minval=-1, maxval=self.tool_count - 1)
            w['queue'] = self._offset_queue(tool)
            self._offsets_next(gcmd)
        elif step == 'attach' and action == 'attached':
            self.state.active_tool = w['tool']
            self._save()
            self._offsets_measure()
        elif step == 'align' and action == 'ok':
            self._align_accept(gcmd)
        elif step == 'camera' and action == 'save':
            self._camera_save(gcmd)
        else:
            raise gcmd.error("Dragons Den: unexpected action '%s' in step"
                             " '%s'" % (action, step))

    def _align_accept(self, gcmd):
        w = self._wizard
        tool = w['tool']
        self._ensure_kinematic(AXES)
        pos = self._locate_by_homing(lambda: self._lift(self.z_hop), "yxz")
        w['xy'] = pos[:2]
        self._respond("T%d XY measured at X=%.3f Y=%.3f"
                      % (tool, pos[0], pos[1]))
        if self.probe_type == 'sexball':
            self._safe_travel(pos[0], pos[1],
                              self._ball_top() + self.ball_clearance)
            w['step'] = 'manual_z'
            self._close_prompt()
            self._respond("lower T%d onto the top of the ball with TESTZ"
                          " (paper test), then ACCEPT" % (tool,))
            manual_probe.ManualProbeHelper(self.printer, gcmd,
                                           self._manual_z_done)
            return
        # eddy: every nozzle probes the same spot on the bed
        ref = self._reference() if tool != 0 else None
        off_x = pos[0] - ref[0] if ref else 0.
        off_y = pos[1] - ref[1] if ref else 0.
        point = self.eddy_z_point or pos[:2]
        self._safe_travel(point[0] + off_x, point[1] + off_y,
                          self.probe_start_z)
        cmd = "PROBE"
        if self.eddy_probe_method:
            cmd += " METHOD=%s" % (self.eddy_probe_method,)
        self._run(cmd)
        probe = self.printer.lookup_object('probe')
        z = probe.get_status(self.reactor.monotonic())['last_z_result']
        self._lift(self.z_hop)
        self._offsets_store(tool, [pos[0], pos[1], z])
        self._offsets_next(gcmd)

    def _manual_z_done(self, kin_pos):
        w = self._wizard
        if w is None or w.get('step') != 'manual_z':
            return
        if kin_pos is None:
            self._end_wizard("manual Z probe aborted, calibration stopped")
            return
        xy = w['xy']
        self._lift(self.ball_clearance)
        self._offsets_store(w['tool'], [xy[0], xy[1], kin_pos[2]])
        self._offsets_next(None)

    def _camera_save(self, gcmd):
        self._require_homed(AXES)
        self.toolhead.wait_moves()
        pos = self.toolhead.get_position()[:3]
        self._offsets_store(self._wizard['tool'], pos)
        self._lift(self.z_hop)
        self._offsets_next(gcmd)

    def cmd_DRAGONS_CAMERA_SAVE(self, gcmd):
        w = self._wizard
        if w is None or w['kind'] != 'offsets' or w['step'] != 'camera':
            raise gcmd.error("Dragons Den: no usb_camera calibration is"
                             " waiting for Save")
        self._camera_save(gcmd)

    def _offsets_store(self, tool, pos):
        if tool == 0:
            self.state.reference = {'method': self.probe_type,
                                    'pos': list(pos)}
            offset = [0., 0., 0.]
        else:
            ref = self._reference()
            if ref is None:
                raise self._error("calibrate T0 (the reference) first")
            offset = [p - r for p, r in zip(pos, ref)]
            self.state.offsets[tool] = offset
        self._save()
        self._wizard['results'][tool] = offset
        self._respond("T%d offset %s" % (tool, _xyz(offset)))

    def _offsets_finish(self):
        cur = self.state.active_tool
        if cur >= 0:
            off = self.state.offset(cur)
            self._run("SET_GCODE_OFFSET X=%.6f Y=%.6f Z=%.6f MOVE=0"
                      % tuple(off))
        self._wizard['step'] = 'done'
        self._wizard['tool'] = None
        self._render()


def load_config(config):
    return DragonsDen(config)
