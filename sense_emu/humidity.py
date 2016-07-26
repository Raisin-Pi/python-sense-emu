from __future__ import (
    unicode_literals,
    absolute_import,
    print_function,
    division,
    )
nstr = str
str = type('')

import sys
import os
import io
import mmap
import errno
from struct import Struct
from collections import namedtuple, deque
from random import Random
from time import time
from threading import Thread, Event
try:
    from statistics import mean
except ImportError:
    from .compat import mean

from .common import clamp


# See HTS221 data-sheet for details of register values
HUMIDITY_DATA = Struct(
    '<'   # little endian
    'B'   # humidity sensor type
    '6p'  # humidity sensor name
    'B'   # H0
    'B'   # H1
    'H'   # T0
    'H'   # T1
    'h'   # H0_OUT
    'h'   # H1_OUT
    'h'   # T0_OUT
    'h'   # T1_OUT
    'h'   # H_OUT
    'h'   # T_OUT
    )

HumidityData = namedtuple('HumidityData',
    ('type', 'name', 'H0', 'H1', 'T0', 'T1', 'H0_OUT', 'H1_OUT', 'T0_OUT', 'T1_OUT', 'H_OUT', 'T_OUT'))


def humidity_filename():
    """
    Return the filename used represent the state of the emulated sense HAT's
    humidity sensor. On UNIX we try ``/dev/shm`` then fall back to ``/tmp``; on
    Windows we use whatever ``%TEMP%`` contains
    """
    fname = 'rpi-sense-emu-humidity'
    if sys.platform.startswith('win'):
        # just use a temporary file on Windows
        return os.path.join(os.environ['TEMP'], fname)
    else:
        if os.path.exists('/dev/shm'):
            return os.path.join('/dev/shm', fname)
        else:
            return os.path.join('/tmp', fname)


def init_humidity():
    """
    Opens the file representing the state of the humidity sensor. The
    file-like object is returned.

    If the file already exists we simply make sure it is the right size. If
    the file does not already exist, it is created and zeroed.
    """
    try:
        # Attempt to open the humidity sensor's file and ensure it's the right
        # size
        fd = io.open(humidity_filename(), 'r+b', buffering=0)
        fd.seek(HUMIDITY_DATA.size)
        fd.truncate()
    except IOError as e:
        # If the screen's device file doesn't exist, create it with reasonable
        # initial values
        if e.errno == errno.ENOENT:
            fd = io.open(humidity_filename(), 'w+b', buffering=0)
            fd.write(b'\x00' * HUMIDITY_DATA.size)
        else:
            raise
    return fd


class HumidityServer(object):
    def __init__(self, simulate_noise=True):
        self._random = Random()
        self._fd = init_humidity()
        self._map = mmap.mmap(self._fd.fileno(), 0, access=mmap.ACCESS_WRITE)
        data = self._read()
        if data.type != 2:
            self._write(HumidityData(2, b'HTS221', 0, 100, 0, 100, 0, 25600, 0, 6400, 0, 0))
            self._humidity = 45.0
            self._temperature = 20.0
        else:
            self._humidity = data.H_OUT / 256
            self._temperature = data.T_OUT / 64
        self._noise_thread = None
        self._noise_event = Event()
        self._noise_write()
        # The deque lengths are selected to accurately represent the response
        # time of the sensors
        self._humidities = deque(maxlen=77)
        self._temperatures = deque(maxlen=31)
        self.simulate_noise = simulate_noise

    def close(self):
        if self._fd:
            self.simulate_noise = False
            self._map.close()
            self._fd.close()
            self._fd = None
            self._map = None

    def _perturb(self, value, error):
        """
        Return *value* perturbed by +/- *error* which is derived from a
        gaussian random generator.
        """
        # We use an internal Random() instance here to avoid a threading issue
        # with the gaussian generator (could use locks, but an instance of
        # Random is easier and faster)
        return value + self._random.gauss(0, 0.2) * error

    def _read(self):
        return HumidityData(*HUMIDITY_DATA.unpack_from(self._map))

    def _write(self, value):
        HUMIDITY_DATA.pack_into(self._map, 0, *value)

    @property
    def humidity(self):
        return self._humidity

    @humidity.setter
    def humidity(self, value):
        self._humidity = value
        if not self._noise_thread:
            self._noise_write()

    @property
    def temperature(self):
        return self._temperature

    @temperature.setter
    def temperature(self, value):
        self._temperature = value
        if not self._noise_thread:
            self._noise_write()

    @property
    def simulate_noise(self):
        return self._noise_thread is not None

    @simulate_noise.setter
    def simulate_noise(self, value):
        if value and not self._noise_thread:
            self._noise_event.clear()
            self._noise_thread = Thread(target=self._noise_loop)
            self._noise_thread.daemon = True
            self._noise_thread.start()
        elif self._noise_thread and not value:
            self._noise_event.set()
            self._noise_thread.join()
            self._noise_thread = None
            self._noise_write()

    def _noise_loop(self):
        while not self._noise_event.wait(0.13):
            self._noise_write()

    def _noise_write(self):
        if self.simulate_noise:
            self._humidities.append(self._perturb(self.humidity, (
                3.5 if 20 <= self.humidity <= 80 else
                5.0)))
            self._temperatures.append(self._perturb(self.temperature, (
                0.5 if 15 <= self.temperature <= 40 else
                1.0 if 0 <= self.temperature <= 60 else
                2.0)))
            humidity = mean(self._humidities)
            temperature = mean(self._temperatures)
        else:
            humidity = self.humidity
            temperature = self.temperature
        self._write(self._read()._replace(
            H_OUT=int(clamp(humidity, 0, 100) * 256),
            T_OUT=int(clamp(temperature, -40, 120) * 64)))

