"""Minimal stand-ins for the Klipper objects dragons_den talks to.

The toolhead keeps two frames, as real hardware does:
  * phys      - where the toolhead physically is (true machine coordinates)
  * cmd_pos   - where Klipper believes it is (per stepper, cartesian here)
and each stepper counts MCU steps. Moving by hand with motors off changes only
`phys`; commanded moves change all three; homing drives `phys` to the
endstop, counts the travelled steps and resets `cmd_pos`.
"""
import shlex
import types

SENTINEL = object()


class CommandError(Exception):
    pass


class ConfigError(Exception):
    pass


class FakeReactor:
    def __init__(self):
        self.now = 100.

    def monotonic(self):
        self.now += 0.001
        return self.now

    def register_callback(self, callback, waketime=None):
        # Run at once: tests drive time explicitly
        callback(self.monotonic())

    def pause(self, waketime):
        self.now = max(self.now, waketime)
        return self.now


class FakeButtons:
    """Stands in for klippy/extras/buttons.py. press(pin, 1/0) simulates
    the MCU reporting a pin change."""
    def __init__(self, reactor):
        self.reactor = reactor
        self.callbacks = {}

    def register_buttons(self, pins, callback):
        for pin in pins:
            self.callbacks[pin] = callback

    def press(self, pin, state):
        self.callbacks[pin](self.reactor.monotonic(), 1 if state else 0)


class FakePrintStats:
    def __init__(self):
        self.state = 'standby'

    def get_status(self, eventtime):
        return {'state': self.state}


class FakePauseResume:
    def __init__(self):
        self.pauses = 0

    def send_pause_command(self):
        self.pauses += 1


class FakeConfig:
    error = ConfigError

    def __init__(self, printer, options, name='dragons_den'):
        self.printer = printer
        self.options = {k: str(v) for k, v in options.items()}
        self.name = name

    def get_printer(self):
        return self.printer

    def get_name(self):
        return self.name

    def _get(self, option, default, parser, minval=None, maxval=None,
             above=None, below=None):
        if option not in self.options:
            if default is SENTINEL:
                raise ConfigError("Option '%s' must be specified" % option)
            return default
        try:
            value = parser(self.options[option])
        except ValueError:
            raise ConfigError("Unable to parse option '%s'" % option)
        if minval is not None and value < minval:
            raise ConfigError("Option '%s' below minimum" % option)
        if maxval is not None and value > maxval:
            raise ConfigError("Option '%s' above maximum" % option)
        if above is not None and value <= above:
            raise ConfigError("Option '%s' must be above %s" % (option, above))
        if below is not None and value >= below:
            raise ConfigError("Option '%s' must be below %s" % (option, below))
        return value

    def get(self, option, default=SENTINEL):
        return self._get(option, default, str)

    def getint(self, option, default=SENTINEL, minval=None, maxval=None):
        return self._get(option, default, int, minval, maxval)

    def getfloat(self, option, default=SENTINEL, minval=None, maxval=None,
                 above=None, below=None):
        return self._get(option, default, float, minval, maxval, above, below)

    def getboolean(self, option, default=SENTINEL):
        def parse(v):
            if v.lower() in ('true', '1', 'yes'):
                return True
            if v.lower() in ('false', '0', 'no'):
                return False
            raise ValueError(v)
        return self._get(option, default, parse)

    def getchoice(self, option, choices, default=SENTINEL):
        value = self.get(option, default)
        if value not in choices:
            raise ConfigError("Choice '%s' for option '%s' is not valid"
                              % (value, option))
        return choices[value]

    def getfloatlist(self, option, default=SENTINEL, sep=',', count=None):
        def parse(v):
            values = [float(p.strip()) for p in v.split(sep) if p.strip()]
            if count is not None and len(values) != count:
                raise ValueError(v)
            return values
        return self._get(option, default, parse)


class FakeGCmd:
    error = CommandError

    def __init__(self, gcode, command, params):
        self.gcode = gcode
        self.command = command
        self.params = params

    def get_command(self):
        return self.command

    def _get(self, name, default, parser, minval=None, maxval=None,
             above=None, below=None):
        if name not in self.params:
            if default is SENTINEL:
                raise CommandError("Error on '%s': missing %s"
                                   % (self.command, name))
            return default
        value = parser(self.params[name])
        if minval is not None and value < minval:
            raise CommandError("%s must have minimum of %s" % (name, minval))
        if maxval is not None and value > maxval:
            raise CommandError("%s must have maximum of %s" % (name, maxval))
        if above is not None and value <= above:
            raise CommandError("%s must be above %s" % (name, above))
        if below is not None and value >= below:
            raise CommandError("%s must be below %s" % (name, below))
        return value

    def get(self, name, default=SENTINEL):
        return self._get(name, default, str)

    def get_int(self, name, default=SENTINEL, minval=None, maxval=None):
        return self._get(name, default, int, minval, maxval)

    def get_float(self, name, default=SENTINEL, minval=None, maxval=None,
                  above=None, below=None):
        return self._get(name, default, float, minval, maxval, above, below)

    def respond_info(self, msg, log=True):
        self.gcode.respond_info(msg)

    def respond_raw(self, msg):
        self.gcode.respond_raw(msg)


class FakeGCode:
    def __init__(self, printer):
        self.printer = printer
        self.handlers = {}
        self.scripts = []
        self.raw = []
        self.info = []
        self.offset_cmds = []

    def register_command(self, name, func, desc=None):
        if name in self.handlers:
            raise ConfigError("gcode command %s already registered" % name)
        self.handlers[name] = func

    def respond_raw(self, msg):
        self.raw.append(msg)

    def respond_info(self, msg, log=True):
        self.info.append(msg)

    def run_script_from_command(self, script):
        for line in script.split('\n'):
            line = line.strip()
            if line:
                self.scripts.append(line)
                self._dispatch(line)

    # Test convenience: run a command as if typed in the console
    run = run_script_from_command
    run_script = run_script_from_command

    def _dispatch(self, line):
        parts = shlex.split(line)
        cmd = parts[0].upper()
        params = {}
        for p in parts[1:]:
            if '=' in p:
                k, v = p.split('=', 1)
                params[k.upper()] = v
            else:
                params[p.upper()] = ''
        handler = self.handlers.get(cmd)
        if handler is None:
            raise CommandError("Unknown command: %s" % cmd)
        handler(FakeGCmd(self, cmd, params))

    def prompts(self):
        return [r for r in self.raw if r.startswith("// action:prompt")]


class FakeStepper:
    def __init__(self, name, step_dist):
        self.name = name
        self.step_dist = step_dist
        self.cmd_pos = 0.
        self.mcu_pos = 12345.   # arbitrary: MCU counters never start at 0

    def get_name(self):
        return self.name

    def get_mcu_position(self):
        return self.mcu_pos

    def get_commanded_position(self):
        return self.cmd_pos

    def get_step_dist(self):
        return self.step_dist


class FakeKinematics:
    def __init__(self):
        self.steppers = [FakeStepper('stepper_x', 0.0125),
                         FakeStepper('stepper_y', 0.0125),
                         FakeStepper('stepper_z', 0.0025)]

    def get_steppers(self):
        return list(self.steppers)

    def calc_position(self, stepper_positions):
        return [stepper_positions['stepper_x'], stepper_positions['stepper_y'],
                stepper_positions['stepper_z']]


class FakeToolhead:
    """Cartesian toolhead. Endstops at X max, Y max and Z min."""
    def __init__(self, printer, new_api=True, mins=(0., 0., 0.),
                 maxs=(300., 300., 250.)):
        self.printer = printer
        self.new_api = new_api
        self.mins = list(mins)
        self.maxs = list(maxs)
        self.endstops = [maxs[0], maxs[1], mins[2]]
        self.kin = FakeKinematics()
        self.phys = [150., 150., 100.]
        self.e = 0.
        self.homed = set()
        self.moves = []
        for s, p in zip(self.kin.steppers, self.phys):
            s.cmd_pos = p

    # --- Klipper API ---
    def get_position(self):
        return [s.cmd_pos for s in self.kin.steppers] + [self.e]

    def get_kinematics(self):
        return self.kin

    def wait_moves(self):
        pass

    def get_status(self, eventtime):
        return {'homed_axes': "".join(a for a in "xyz" if a in self.homed),
                'axis_minimum': list(self.mins) + [0.],
                'axis_maximum': list(self.maxs) + [0.]}

    def manual_move(self, coord, speed):
        target = self.get_position()
        for i, c in enumerate(coord):
            if c is not None:
                target[i] = c
        cur = self.get_position()
        for i in range(3):
            if target[i] == cur[i]:
                continue
            axis = "xyz"[i]
            if axis not in self.homed:
                raise CommandError("Must home axis first")
            if not self.mins[i] <= target[i] <= self.maxs[i]:
                raise CommandError("Move out of range: %s" % target)
        if target[3] != cur[3]:
            ext = self.printer.lookup_object('extruder')
            if not ext.heater.can_extrude:
                raise CommandError("Extrude below minimum temp")
        for i, s in enumerate(self.kin.steppers):
            d = target[i] - s.cmd_pos
            s.cmd_pos = target[i]
            s.mcu_pos += d / s.step_dist
            self.phys[i] += d
        self.e = target[3]
        self.moves.append((tuple(target), speed))

    def set_position(self, newpos, homing_axes=()):
        if self.new_api:
            axes = [("xyz".index(a)) for a in homing_axes]  # str required
        else:
            axes = [[0, 1, 2][a] for a in homing_axes]       # ints required
        for i, s in enumerate(self.kin.steppers):
            s.cmd_pos = newpos[i]
        self.e = newpos[3]
        for i in axes:
            self.homed.add("xyz"[i])

    # --- test helpers ---
    def hand_move(self, x=None, y=None, z=None):
        """User pushes the toolhead with motors off: no steps are counted."""
        for i, v in enumerate((x, y, z)):
            if v is not None:
                self.phys[i] = v

    def home(self, axis):
        i = "xyz".index(axis)
        s = self.kin.steppers[i]
        travel = self.endstops[i] - self.phys[i]
        s.mcu_pos += travel / s.step_dist
        self.phys[i] = self.endstops[i]
        s.cmd_pos = self.endstops[i]
        self.homed.add(axis)

    def motor_off(self):
        self.homed.clear()

    def xy_moves(self):
        return [m[0] for m in self.moves]


class FakeHeater:
    def __init__(self):
        self.can_extrude = True


class FakeExtruder:
    def __init__(self):
        self.heater = FakeHeater()

    def get_heater(self):
        return self.heater


class FakeProbe:
    """PROBE result = toolhead Z at which the active nozzle touches Z=0."""
    def __init__(self):
        self.last_z = 0.
        self.z_for_tool = lambda: 0.
        self.calls = []

    def get_status(self, eventtime):
        return {'last_z_result': self.last_z}

    def run(self, params):
        self.calls.append(params)
        self.last_z = self.z_for_tool()


class FakeManualProbe:
    """Stands in for klippy/extras/manual_probe.py"""
    def __init__(self):
        self.helpers = []
        outer = self

        class ManualProbeHelper:
            def __init__(self, printer, gcmd, finalize_callback):
                self.finalize_callback = finalize_callback
                outer.helpers.append(self)
        self.ManualProbeHelper = ManualProbeHelper

    def finish(self, kin_pos):
        self.helpers[-1].finalize_callback(kin_pos)


class FakePrinter:
    command_error = CommandError

    def __init__(self, config_file, new_api=True):
        self.reactor = FakeReactor()
        self.start_args = {'config_file': config_file}
        self.objects = {}
        self.events = {}
        self.gcode = FakeGCode(self)
        self.objects['gcode'] = self.gcode
        self.toolhead = FakeToolhead(self, new_api=new_api)
        self.objects['toolhead'] = self.toolhead
        self.extruder = FakeExtruder()
        self.objects['extruder'] = self.extruder
        self.force_moves = []
        self.gcode_states = []
        self._register_builtins()

    def lookup_object(self, name, default=SENTINEL):
        if name in self.objects:
            return self.objects[name]
        if default is SENTINEL:
            raise ConfigError("Unknown config object '%s'" % name)
        return default

    def load_object(self, config, section):
        if section == 'buttons':
            if 'buttons' not in self.objects:
                self.objects['buttons'] = FakeButtons(self.reactor)
            return self.objects['buttons']
        return self.lookup_object(section)

    def add_print_stats(self):
        self.objects['print_stats'] = FakePrintStats()
        self.objects['pause_resume'] = FakePauseResume()
        self.gcode.register_command(
            'PAUSE', lambda gcmd: setattr(self.objects['print_stats'],
                                          'state', 'paused'))
        return self.objects['print_stats']

    def add_object(self, name, obj):
        self.objects[name] = obj

    def register_event_handler(self, event, callback):
        self.events.setdefault(event, []).append(callback)

    def send_event(self, event, *args):
        return [cb(*args) for cb in self.events.get(event, [])]

    def get_reactor(self):
        return self.reactor

    def get_start_args(self):
        return self.start_args

    def add_probe(self):
        probe = FakeProbe()
        self.objects['probe'] = probe
        return probe

    def _register_builtins(self):
        g = self.gcode
        th = self.toolhead

        def g28(gcmd):
            axes = [a.lower() for a in gcmd.params if a in "XYZ"] or list("xyz")
            for a in axes:
                th.home(a)

        def save_state(gcmd):
            self.gcode_states.append(('save', gcmd.params.get('NAME')))

        def restore_state(gcmd):
            self.gcode_states.append(('restore', gcmd.params.get('NAME')))

        def set_offset(gcmd):
            g.offset_cmds.append(dict(gcmd.params))

        def force_move(gcmd):
            self.force_moves.append((gcmd.params['STEPPER'],
                                     float(gcmd.params['DISTANCE'])))

        def probe(gcmd):
            self.objects['probe'].run(dict(gcmd.params))

        g.register_command('G28', g28)
        g.register_command('M84', lambda gcmd: th.motor_off())
        g.register_command('SAVE_GCODE_STATE', save_state)
        g.register_command('RESTORE_GCODE_STATE', restore_state)
        g.register_command('SET_GCODE_OFFSET', set_offset)
        g.register_command('FORCE_MOVE', force_move)
        g.register_command('PROBE', probe)


def make_module(dragons_den):
    """Return a fresh manual_probe stand-in installed into dragons_den."""
    mp = FakeManualProbe()
    dragons_den.manual_probe = types.SimpleNamespace(
        ManualProbeHelper=mp.ManualProbeHelper)
    return mp
