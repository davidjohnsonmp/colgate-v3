#!/usr/bin/env python3
# Colgate by Hiccup Audio
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Trains Colgate's field and the numbers around it on reference renders, and scores them.

    python3 tools/calibration/calibrate.py SONG_FOLDER [SONG_FOLDER ...] --build BUILD_DIR
                                           [--write source/engine/FieldTuning.h] [--check] [--score]

Each song folder holds one or more 48 kHz WAVs with "original" in the name, and
the renders of that song, in any subfolders. Names and folders say what a render
is, with words or underscores in any order:

    ref 1/master mode/recover 15 tame 15 bias 15 brighten 15 boost 30.wav
    ref 1/recover_50.wav

A render counts as Master if its name or folder says "master", and otherwise as
Basic. A render nearly identical to its original is reported and left out, since
that means the plug-in wasn't processing. Name each render after what was really set:
two earlier Master renders labelled Boost 30 turned out to be at Boost 50, and
their name decides what the training assumes.

Trains one field for both modes, with the mode as one of its inputs, on every
render, and fits each mode's other numbers with it. Prints how closely that
matches each render, and with --write, writes the result for the engine. With
--check it first scores each song with a field trained only on the other songs,
which is a fair guess at how a new song would do; that takes about seven times
as long. With --score it instead runs the built engine and compares the actual
audio.

Needs numpy and scipy. See README.md beside this file.
"""
import argparse, glob, json, os, re, subprocess, tempfile, time, warnings
import numpy as np
import scipy.io.wavfile as wavfile
import scipy.signal as sg
from scipy.optimize import least_squares, minimize

warnings.filterwarnings('ignore')

RATE = 48000
HOP = 128
FRAME, STEP = 2048, 256
FPS = RATE / HOP
FADE_Z = np.array([28.0, 33.0, 36.0, 38.0, 39.5, 40.5, 41.3])
FADE = np.array([1.0, 1.0, 0.95, 0.85, 0.62, 0.2, 0.0])
BOOST_Z = np.array([26.0, 30.0, 33.0, 35.0, 37.0, 38.5, 40.3])
FIELD_EXPONENT, BOOST_EXPONENT = 0.35, 0.5
FLOOR_DB, POWER_FLOOR = 60.0, 7.94e-11
ATTACK, RELEASE, SLOW_FIELD_SECONDS = 0.12, 0.04, 2.0
LOUDNESS_ATTACK, LOUDNESS_RELEASE = 0.03, 0.08
BRIGHTEN_BEND = 0.25
DARKEN_KNEE = 40.0
GAIN_LIMIT_DB = 30.0
DRY_DB = -32.0             # a render this close to its original wasn't processed
QUIET = 2e-5               # below this, a band reads as noise

# The field's inputs, in the order PerceptualField gives them: a band's level and
# its neighbours', its place in the spectrum, the mode, its level over other time
# scales, and spread over neighbouring bands.
LEVEL_OFFSETS = (0, -3, -2, -1, 1, 2, 3)
TIME_SCALES = (0.02, 0.3, 1.2)
SPREADS = (1.0, 3.0)
MODE_INPUT = {'basic': 0.0, 'master': 1.0}
# 15/15 matters most. In the reference it is 50/50's shape at a third of the size,
# so plain renders with Recover equal to Tame, up to 50%, count this many times.
EQUAL_WEIGHT = 5.0
HIDDEN = 16
DECAY = 1e-4               # weight decay on the field
TRAIN_STEP = 2             # every other frame is plenty to train on
ROUNDS = ((200, 12), (150, 20))  # field iterations, then fitting steps for the numbers


def erb(hz): return 21.4 * np.log10(1 + 0.00437 * hz)
def from_erb(z): return (10 ** (z / 21.4) - 1) / 0.00437


Z0 = erb(30.0)
HZ = from_erb(Z0 + 0.8 * np.arange(int(np.floor((erb(18500.0) - Z0) / 0.8)) + 1))
Z = erb(HZ)
B = len(HZ)
PIVOT_Z = erb(900.0)
TOP = np.interp(Z, FADE_Z, FADE)
t = np.clip((Z - 23.0) / 7.0, 0, 1)
TREBLE = t * t * (3 - 2 * t)


def load(path):
    rate, x = wavfile.read(path)
    if rate != RATE:
        raise SystemExit(f'{path}: expected {RATE} Hz, found {rate}')
    x = x.astype(np.float64) if x.dtype.kind == 'f' else x.astype(np.float64) / float(np.iinfo(x.dtype).max)
    return x if x.ndim == 2 else x[:, None]


def parse_setting(name):
    name = name.lower().replace('birhgten', 'brighten').replace('birghten', 'brighten').replace('_', ' ')
    both = 'brighten bias' in name
    name = name.replace('brighten bias', 'bias')
    def value(word):
        m = re.search(word + r'\s*(-?\d+)', name)
        return int(m.group(1)) if m else 0
    s = [value(w) for w in ('recover', 'tame', 'bias', 'brighten', 'boost')]
    if both:
        s[3] = s[2]
    return tuple(s)


def find_song(folder):
    """Returns {mode: (original path, {setting: path})}, keeping only real renders."""
    originals, renders = {}, {'basic': {}, 'master': {}}
    for path in sorted(glob.glob(os.path.join(folder, '**', '*.wav'), recursive=True)):
        relative = os.path.relpath(path, folder).lower()
        mode = 'master' if 'master' in os.path.basename(os.path.normpath(folder)).lower() + '/' + relative else 'basic'
        if 'original' in os.path.basename(relative):
            originals[mode] = path
        elif any(w in relative for w in ('recover', 'tame', 'boost', 'brighten')):
            renders[mode][parse_setting(os.path.basename(relative))] = path
    song = {}
    for mode, found in renders.items():
        original = originals.get(mode) or originals.get('basic') or originals.get('master')
        if not found or original is None:
            continue
        x = load(original)
        kept = {}
        for setting, path in found.items():
            y = load(path)
            if len(y) == len(x) and 10 * np.log10(np.mean((aligned(x, y) - x) ** 2) / np.mean(x ** 2)) < DRY_DB:
                print(f'  left out, nearly identical to the original: {path}')
            else:
                kept[setting] = path
        if kept:
            song[mode] = (original, kept)
    return song


def delay_of(a, b):
    # Whitened above 300 Hz, because a large bass lift moves a plain correlation's
    # peak: 111 samples away on one Boost render.
    n = 2 ** int(np.ceil(np.log2(len(a) + len(b))))
    f = np.fft.rfftfreq(n, 1 / RATE)
    c = np.fft.rfft(b.sum(1), n) * np.conj(np.fft.rfft(a.sum(1), n))
    c = np.fft.irfft(c * ((f > 300) & (f < 8000)) / (np.abs(c) + 1e-20), n)
    i = int(np.argmax(np.abs(c)))
    return i - n if i > n // 2 else i


def aligned(x, y):
    d = delay_of(x, y)
    pad = np.zeros((abs(d), y.shape[1]))
    return np.vstack([y[d:], pad]) if d >= 0 else np.vstack([pad, y[:d]])


def band_matrix():
    f = np.fft.rfftfreq(FRAME, 1 / RATE)
    z = erb(np.maximum(f, 1.0))
    m = np.zeros((B, len(f)))
    for k, c in enumerate(Z):
        u = np.abs(z - c) / 0.8
        m[k] = np.where(u < 1, 0.5 * (1 + np.cos(np.pi * u)), 0)
    m[0, f < HZ[0]] = 1
    m[-1, f > HZ[-1]] = 1
    m[:, f < 15] = 0
    return m


MATRIX = band_matrix()
WINDOW = np.hanning(FRAME + 1)[:-1]


def band_power(x):
    n = (len(x) - FRAME) // STEP + 1
    out = np.empty((n, FRAME // 2 + 1))
    for i in range(n):
        out[i] = np.abs(np.fft.rfft(WINDOW * x[i * STEP:i * STEP + FRAME])) ** 2
    return (MATRIX @ out.T).T


def smooth_energy(raw, attack, release):
    up, down = [1 - np.exp(-1 / (FPS * s)) for s in (attack, release)]
    out, state = np.zeros_like(raw), raw[:40].mean(0)
    for i, row in enumerate(raw):
        d = row - state
        state = state + np.where(d > 0, up, down) * d
        out[i] = state
    return out


def keep_loudness(energy, gain, exponent):
    """Broadband dB holding the sum of power^exponent over the bands."""
    wanted = ((energy + POWER_FLOOR) ** exponent - POWER_FLOOR ** exponent).sum(1)
    db, gained = np.zeros(len(energy)), energy * 10 ** (gain / 10)
    for _ in range(4):
        scale = 10 ** (db / 10)[:, None]
        total = gained * scale + POWER_FLOOR
        powered = total ** exponent
        value = (powered - POWER_FLOOR ** exponent).sum(1)
        slope = (exponent * powered * (gained * scale / total)).sum(1)
        db = db + np.clip((wanted - value) / (slope * 0.2302585 + 1e-15), -8, 8)
    return db


def iso226(hz, phon):
    freq = [20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500]
    af = [.532, .506, .480, .455, .432, .409, .387, .367, .349, .330, .315, .301, .288, .276, .267, .259, .253, .250, .246, .244, .243, .243, .243, .242, .242, .245, .254, .271, .301]
    lu = [-31.6, -27.2, -23.0, -19.1, -15.9, -13.0, -10.3, -8.1, -6.2, -4.5, -3.1, -2.0, -1.1, -.4, 0, .3, .5, 0, -2.7, -4.1, -1.0, 1.7, 2.5, 1.2, -2.1, -7.1, -11.2, -10.7, -3.1]
    tf = [78.5, 68.7, 59.5, 51.1, 44.0, 37.5, 31.5, 26.5, 22.1, 17.9, 14.4, 11.4, 8.6, 6.2, 4.4, 3.0, 2.2, 2.4, 3.5, 1.7, -1.3, -4.2, -6.0, -5.4, -1.5, 6.0, 12.6, 13.9, 12.3]
    lf = np.log(np.clip(hz, 20, 12500))
    a, l, tt = [np.interp(lf, np.log(freq), v) for v in (af, lu, tf)]
    return 10 / a * np.log10(4.47e-3 * (10 ** (.025 * phon) - 1.15) + (.4 * 10 ** ((tt + l) / 10 - 9)) ** a) - l + 94


def contour_difference(boost, reference=50.0):
    d = lambda hz: boost - (iso226(hz, reference + boost) - iso226(hz, reference))
    return d(HZ) - d(np.array([1700.0]))[0]


def relative_db(energy):
    """Each band against the whole signal, with a floor."""
    level = 10 * np.log10(energy + 1e-13)
    whole = 10 * np.log10(energy.sum(-1, keepdims=True) + 1e-13)
    return np.maximum(level, whole - FLOOR_DB) - whole


def scaled(db): return (db + 25.0) / 12.0


def neighbour(a, offset):
    """The band offset away, with the end bands standing in past either end."""
    return a[..., np.clip(np.arange(B) + offset, 0, B - 1)]


def follow(raw, seconds):
    """One pole, the same both ways, as in PerceptualField::track()."""
    a = 1 - np.exp(-1 / (FPS * seconds))
    y, _ = sg.lfilter([a], [1, a - 1], raw, axis=0, zi=raw[:1] * (1 - a))
    return y


def spread_matrix(width):
    d = np.arange(B)[:, None] - np.arange(B)[None, :]
    k = np.exp(-0.5 * (d / width) ** 2)
    return k / k.sum(1, keepdims=True)


def field_inputs(raw, energy, at, mode):
    level = scaled(relative_db(energy[at]))
    columns = [neighbour(level, o) for o in LEVEL_OFFSETS]
    columns.append(np.broadcast_to((Z - 20.0) / 12.0, level.shape))
    columns.append(np.full(level.shape, MODE_INPUT[mode]))
    for seconds in TIME_SCALES:
        columns.append(scaled(relative_db(follow(raw, seconds)[at])))
    e = energy[at]
    whole = 10 * np.log10(e.sum(1, keepdims=True) + 1e-13)
    for width in SPREADS:
        columns.append(scaled(np.maximum(10 * np.log10(e @ spread_matrix(width).T + 1e-13), whole - FLOOR_DB) - whole))
    return np.stack(columns, -1)


def plain_equal(setting):
    """15/15 and its kin: Recover equal to Tame, to within 2, up to 50%, nothing else on."""
    recover, tame, bias, brighten, boost = setting
    return bias == 0 and brighten == 0 and boost == 0 and abs(recover - tame) <= 2 and max(recover, tame) <= 50


class Clip:
    """One song in one mode: the field's inputs from the original, measured gain per render."""
    def __init__(self, name, mode, original, renders, build):
        self.name, self.mode = name, mode
        x = load(original)
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 'bands.f32')
            subprocess.check_call([os.path.join(build, 'colgate_bands'), original, path])
            raw = np.fromfile(path, dtype=np.float32).reshape(-1, B).astype(np.float64)
        power = [band_power(x[:, c]) for c in range(x.shape[1])]
        total = sum(power)
        self.active = (total > total.mean(0) * 0.01) & (total > QUIET)
        n = len(total)
        # A target frame is centred FRAME / 2 samples in. The analyser has heard
        # everything up to there, and nothing after it.
        at = np.clip((STEP * np.arange(n) + FRAME // 2) // HOP, 0, len(raw) - 1)
        energy = smooth_energy(raw, ATTACK, RELEASE)
        self.loudness = smooth_energy(raw, LOUDNESS_ATTACK, LOUDNESS_RELEASE)[at]
        self.inputs = field_inputs(raw, energy, at, mode)
        self.targets = {}
        for setting, path in renders.items():
            y = aligned(x, load(path))
            self.targets[setting] = np.mean([10 * np.log10((band_power(y[:, c]) + 1e-14) / (power[c] + 1e-14))
                                             for c in range(x.shape[1])], axis=0)
        # Scored and trained on every band with signal in it, after the first 40 frames.
        self.mask = self.active.copy()
        self.mask[:40] = False
        self.rows = np.arange(40, n - 20, TRAIN_STEP)
        self.m = self.mask[self.rows].astype(np.float64)
        self.x = self.inputs[self.rows].reshape(-1, self.inputs.shape[-1])
        self.y = {s: y[self.rows] for s, y in self.targets.items()}
        # Each render counts the same, whatever the size of its gains, except plain
        # ones with the controls equal, which count EQUAL_WEIGHT times.
        self.w = {s: (EQUAL_WEIGHT if plain_equal(s) else 1.0) / max((self.m * self.shape_part(y) ** 2).sum(), 1e-9)
                  for s, y in self.y.items()}

    def shape_part(self, u):
        """What's left after the broadband gain of each frame, which the loudness rule sets."""
        c = (self.m * TOP * u).sum(1) / np.maximum((self.m * TOP * TOP).sum(1), 1e-9)
        return u - TOP * c[:, None]


class Net:
    """Two tanh layers, plus a straight path from the inputs, to the Recover and Tame fields."""
    def __init__(self, inputs, hidden):
        self.shapes = [(inputs, hidden), (hidden,), (hidden, hidden), (hidden,), (hidden, 2), (2,), (inputs, 2)]
        self.sizes = [int(np.prod(s)) for s in self.shapes]
        self.n = sum(self.sizes)
        self.inputs, self.hidden = inputs, hidden

    def unpack(self, theta):
        out, i = [], 0
        for shape, size in zip(self.shapes, self.sizes):
            out.append(theta[i:i + size].reshape(shape))
            i += size
        return out

    def start(self, seed):
        """Random hidden layers, and a straight path that starts as a plain level field:
        a band above its neighbourhood is pulled down, one below it lifted."""
        r = np.random.default_rng(seed)
        n, h = self.inputs, self.hidden
        parts = [r.normal(0, 1 / np.sqrt(n), (n, h)), np.zeros(h), r.normal(0, 1 / np.sqrt(h), (h, h)), np.zeros(h),
                 r.normal(0, .1 / np.sqrt(h), (h, 2)), np.zeros(2), np.zeros((n, 2))]
        own, near, tame_scale, zero_recover, zero_tame = -0.0506, -0.0746, 1.7775, -2.3766, -5.9291
        k = spread_matrix(1.0)[25, 22:29]
        for j, o in enumerate(LEVEL_OFFSETS):
            v = (own if o == 0 else 0.0) + near * k[o + 3]
            parts[6][j] = [12 * v, 12 * v * tame_scale]
        parts[5][:] = [zero_recover - 25 * parts[6][:7, 0].sum() / 12, zero_tame - 25 * parts[6][:7, 1].sum() / 12]
        return np.concatenate([p.ravel() for p in parts])

    def forward(self, theta, x):
        w1, b1, w2, b2, w3, b3, ws = self.unpack(theta)
        h1 = np.tanh(x @ w1 + b1)
        h2 = np.tanh(h1 @ w2 + b2)
        return h2 @ w3 + b3 + x @ ws, (x, h1, h2)

    def backward(self, theta, cache, d):
        x, h1, h2 = cache
        w1, b1, w2, b2, w3, b3, ws = self.unpack(theta)
        a2 = (d @ w3.T) * (1 - h2 * h2)
        a1 = (a2 @ w2.T) * (1 - h1 * h1)
        return np.concatenate([g.ravel() for g in (x.T @ a1, a1.sum(0), h1.T @ a2, a2.sum(0), h2.T @ d, d.sum(0), x.T @ d)])


# The numbers around the field, per mode. These starts are version 1.4's; one with no
# render to measure it stays at its start.
NUMBERS = ['exponent', 'softness', 'exponent_high', 'bias_line', 'bias_knee', 'tilt_low', 'tilt_high', 'tilt_knee',
           'darken_low', 'darken_high', 'calm', 'boost_scale'] + [f'boost_treble{i}' for i in range(1, 7)] + ['boost_growth']
LIMITS = dict(exponent=(.5, 1.4), softness=(-1.0, 3.0), exponent_high=(.2, 1.6), bias_line=(0, 12), bias_knee=(0, 3),
              tilt_low=(-.2, 2), tilt_high=(-.2, 2), tilt_knee=(2, 60), darken_low=(-.1, 1), darken_high=(0, 1.5), calm=(-1, 3),
              boost_scale=(0, 3), boost_treble1=(-3, 3), boost_treble2=(-3, 3), boost_treble3=(-3, 3), boost_treble4=(-3, 3),
              boost_treble5=(-6, 8), boost_treble6=(-6, 10), boost_growth=(-1, 1.5))
START = {
    'basic': dict(exponent=.9, softness=np.log(3.0), exponent_high=.6513, bias_line=2.4754, bias_knee=.4536, tilt_low=.1541,
                  tilt_high=.371, tilt_knee=3.6127, darken_low=.0072, darken_high=.2691, calm=.5437, boost_scale=1.2085,
                  boost_treble1=.1316, boost_treble2=-.2695, boost_treble3=.1882, boost_treble4=-.2233, boost_treble5=2.6471,
                  boost_treble6=4.5436, boost_growth=.1618),
    'master': dict(exponent=.9, softness=np.log(3.0), exponent_high=.6256, bias_line=1.3223, bias_knee=1.3285, tilt_low=.1374,
                   tilt_high=.2924, tilt_knee=2.0, darken_low=.0692, darken_high=.2067, calm=.3187, boost_scale=1.1829,
                   boost_treble1=.153, boost_treble2=-.2145, boost_treble3=.0764, boost_treble4=-.2584, boost_treble5=2.0845,
                   boost_treble6=3.9101, boost_growth=.1618),
}
# softness is kept as its logarithm while fitting, so it stays positive.


def amount(v, q):
    if v <= 0:
        return 0.0
    return (v / 50.0) ** (q['exponent'] if v <= 50 else q['exponent_high'])


def slow(f, a):
    y, _ = sg.lfilter([a], [1, a - 1], f, axis=0, zi=f[:1] * (1 - a))
    return y


def slow_adjoint(v, a):
    z = sg.lfilter([a], [1, a - 1], v[::-1], axis=0)[::-1].copy()
    z[0] += (((1 - a) ** (np.arange(len(v)) + 1.0))[:, None] * v).sum(0)
    return z


def tilt_of(q, b):
    fp = Z - PIVOT_Z
    if b < 0:
        tilt = np.where(fp < 0, q['darken_low'] * fp, q['darken_high'] * DARKEN_KNEE * np.tanh(fp / DARKEN_KNEE))
    else:
        tilt = np.where(fp < 0, q['tilt_low'] * fp, q['tilt_high'] * q['tilt_knee'] * np.tanh(fp / q['tilt_knee']))
    return np.sign(b) * (1 - np.exp(-abs(b) / BRIGHTEN_BEND)) / (1 - np.exp(-1 / BRIGHTEN_BEND)) * tilt


def shape_of(q, for_recover, for_tame, setting, seconds):
    """Gain from the fields, before the broadband gain, and its slopes against each field."""
    recover, tame, bias, brighten, _ = setting
    s_recover, s_tame = amount(recover, q), amount(tame, q)
    b, calm = brighten / 100.0, None
    if b != 0:
        # Positive Brighten calms the treble's movement around its own slow average.
        a = 1 - np.exp(-seconds / SLOW_FIELD_SECONDS)
        k = 1 - q['calm'] * b * TREBLE
        for_recover = k * for_recover + (1 - k) * slow(for_recover, a)
        for_tame = k * for_tame + (1 - k) * slow(for_tame, a)
        calm = (a, k)
    knee = np.exp(2 * q['softness'])
    above = lambda f, v: (.5 * (f + np.sqrt(f * f + v)), .5 * (1 + f / np.sqrt(f * f + v)))
    below = lambda f, v: (.5 * (f - np.sqrt(f * f + v)), .5 * (1 - f / np.sqrt(f * f + v)))
    up, d_up = above(for_recover, knee)
    down, d_down = below(for_tame, knee)
    biased = bias / 100.0
    if biased != 0 and recover != tame:
        # Bias moves the line and softens one knee. The shared part keeps Recover equal
        # to Tame free of it, as in the reference.
        line, wider = -biased * q['bias_line'], np.exp(q['bias_knee'] * biased)
        up_line, d_up_line = above(for_recover - line, knee * wider * wider)
        down_line, d_down_line = below(for_tame - line, knee / (wider * wider))
        shared = min(s_recover, s_tame)
        out = s_recover * up_line + s_tame * down_line + shared * (up + down - up_line - down_line)
        g_recover = (s_recover - shared) * d_up_line + shared * d_up
        g_tame = (s_tame - shared) * d_down_line + shared * d_down
    else:
        out, g_recover, g_tame = s_recover * up + s_tame * down, s_recover * d_up, s_tame * d_down
    if b != 0:
        out = out + .5 * (s_recover + s_tame) * tilt_of(q, b)
    return out, g_recover, g_tame, calm


def back_through_calm(g, calm):
    if calm is None:
        return g
    a, k = calm
    return k * g + slow_adjoint((1 - k) * g, a)


def boost_of(q, boost):
    if boost == 0:
        return 0.0
    treble = np.interp(Z, BOOST_Z, np.r_[0.0, [q[f'boost_treble{i}'] for i in range(1, 7)]], left=0.0)
    return max(1.0, abs(boost) / 30.0) ** q['boost_growth'] * (q['boost_scale'] * contour_difference(boost) + treble * boost / 30.0)


def fields(net, theta, clip, every_frame=False):
    x = clip.inputs.reshape(-1, clip.inputs.shape[-1]) if every_frame else clip.x
    out, cache = net.forward(theta, x)
    return out[:, 0].reshape(-1, B), out[:, 1].reshape(-1, B), cache


def loss(theta, net, clips, numbers):
    total, grad = 0.0, np.zeros_like(theta)
    for c in clips:
        for_recover, for_tame, cache = fields(net, theta, c)
        q, seconds = numbers[c.mode], TRAIN_STEP * STEP / RATE
        g_recover, g_tame = np.zeros_like(for_recover), np.zeros_like(for_tame)
        for setting, y in c.y.items():
            shape, d_recover, d_tame, calm = shape_of(q, for_recover, for_tame, setting, seconds)
            r = c.shape_part(y - TOP * (shape + boost_of(q, setting[4])))
            total += c.w[setting] * (c.m * r * r).sum()
            d = -2 * c.w[setting] * c.m * TOP * r
            g_recover += back_through_calm(d * d_recover, calm)
            g_tame += back_through_calm(d * d_tame, calm)
        grad += net.backward(theta, cache, np.stack([g_recover.ravel(), g_tame.ravel()], 1))
    return total + DECAY * (theta * theta).sum(), grad + 2 * DECAY * theta


def measured_numbers(clips, mode):
    """Only what some render measures; the rest keep their starting values."""
    settings = [s for c in clips if c.mode == mode for s in c.y]
    names = {'exponent', 'softness'}
    if any(max(s[0], s[1]) > 50 for s in settings):
        names.add('exponent_high')
    if any(s[2] != 0 and s[0] != s[1] for s in settings):
        names |= {'bias_line', 'bias_knee'}
    if any(s[3] > 0 for s in settings):
        names |= {'tilt_low', 'tilt_high', 'tilt_knee'}
    if any(s[3] < 0 for s in settings):
        names |= {'darken_low', 'darken_high'}
    if any(s[3] != 0 for s in settings):
        names.add('calm')
    if any(s[4] != 0 for s in settings):
        names |= {'boost_scale'} | {f'boost_treble{i}' for i in range(1, 7)}
    if any(abs(s[4]) > 30 for s in settings):
        names.add('boost_growth')
    return [n for n in NUMBERS if n in names]


def fit_numbers(net, theta, clips, numbers, mode, steps):
    names = measured_numbers(clips, mode)
    if not names or not any(c.mode == mode for c in clips):
        return
    found = {id(c): fields(net, theta, c)[:2] for c in clips if c.mode == mode}

    def residuals(v):
        q = {**numbers[mode], **dict(zip(names, v))}
        out = []
        for c in clips:
            if c.mode != mode:
                continue
            for_recover, for_tame = found[id(c)]
            for setting, y in c.y.items():
                shape = shape_of(q, for_recover, for_tame, setting, TRAIN_STEP * STEP / RATE)[0]
                r = c.shape_part(y - TOP * (shape + boost_of(q, setting[4])))
                out.append((r * np.sqrt(c.w[setting]))[c.m > 0])
        return np.concatenate(out)

    low, high = np.array([LIMITS[n][0] for n in names]), np.array([LIMITS[n][1] for n in names])
    start = np.clip([numbers[mode][n] for n in names], low + 1e-6, high - 1e-6)
    v = least_squares(residuals, start, bounds=(low, high), max_nfev=steps, diff_step=2e-3).x
    numbers[mode].update(zip(names, map(float, v)))


def train(clips, seed=0, say=print):
    net = Net(clips[0].inputs.shape[-1], HIDDEN)
    theta = net.start(seed)
    numbers = {m: dict(START[m]) for m in START}
    started = time.time()
    for iterations, steps in ROUNDS:
        theta = minimize(loss, theta, args=(net, clips, numbers), jac=True, method='L-BFGS-B',
                         options=dict(maxiter=iterations, maxcor=30)).x
        for mode in numbers:
            fit_numbers(net, theta, clips, numbers, mode, steps)
        say(f'    trained {iterations} steps ({time.time() - started:.0f} s)')
    # Basic has no render above Boost 30, so it borrows Master's growth.
    if not any(abs(s[4]) > 30 for c in clips if c.mode == 'basic' for s in c.y):
        numbers['basic']['boost_growth'] = numbers['master']['boost_growth']
    return net, theta, numbers


def predict(net, theta, q, clip, setting):
    for_recover, for_tame, _ = fields(net, theta, clip, every_frame=True)
    shape = shape_of(q, for_recover, for_tame, setting, STEP / RATE)[0]
    shape = GAIN_LIMIT_DB * np.tanh(shape / GAIN_LIMIT_DB)
    out = TOP * (shape + keep_loudness(clip.loudness, TOP * shape, FIELD_EXPONENT)[:, None])
    if setting[4] != 0:
        b = boost_of(q, setting[4])
        out = out + TOP * (b + keep_loudness(clip.loudness, TOP * b, BOOST_EXPONENT)[:, None])
    return out


def score(net, theta, q, clip, setting):
    y, g, m = clip.targets[setting], predict(net, theta, q, clip, setting), clip.mask
    return 1 - ((y[m] - g[m]) ** 2).sum() / ((y[m] - y[m].mean()) ** 2).sum(), np.sqrt(((y[m] - g[m]) ** 2).mean())


def report(rows, title):
    print(title)
    for name, mode, setting, (r2, error) in rows:
        print(f'  {name:16} {mode:7} {str(setting):26} R2 {r2:.3f}   error {error:.3f} dB')
    for mode in ('basic', 'master'):
        r = [s[0] for _, m, _, s in rows if m == mode]
        if r:
            print(f'  {mode}: mean R2 {np.mean(r):.3f} over {len(r)} renders, {sum(v >= .95 for v in r)} at 0.95 or more, lowest {min(r):.3f}')


# ---- FieldTuning.h

def _f(v):
    s = '%.7g' % float(v)
    return s + ('' if 'e' in s or '.' in s else '.0') + 'f'


def _vector(a): return '{ ' + ', '.join(_f(v) for v in np.ravel(a)) + ' }'


def _matrix(a): return '{\n' + ',\n'.join('        ' + _vector(row) for row in np.atleast_2d(a)) + ' }'


def header(net, theta, numbers, note):
    w1, b1, w2, b2, w3, b3, ws = net.unpack(theta)
    s = f'''// Colgate by Hiccup Audio
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Written by tools/calibration/calibrate.py from the reference renders: the learned
// field and the numbers around it. Don't edit by hand; run the calibration again
// (see tools/calibration/README.md).
//
// {note}
#pragma once

#include "PerceptualField.h"

namespace colgate
{{

struct FieldNetwork
{{
    // Inputs: the band's level relative to the whole signal, then the levels 3, 2
    // and 1 bands below it and 1, 2 and 3 above it, each as (dB + 25) / 12; then its
    // ERB rate as (z - 20) / 12; then the mode, 0 for Basic and 1 for Master. Two
    // tanh layers and a direct path from the inputs give the Recover and Tame fields,
    // in dB.
    static constexpr int inputs = {net.inputs};
    static constexpr int hidden = {net.hidden};
    static constexpr bool modeInput = true;

    // Then the band's level over {len(TIME_SCALES)} more time scales, each against its own total,
    // and with its power spread over {len(SPREADS)} widths of neighbouring bands, all scaled
    // like the levels above.
    static constexpr int timeScales = {len(TIME_SCALES)};
    static constexpr float timeScaleSeconds[{len(TIME_SCALES)}] = {_vector(TIME_SCALES)};
    static constexpr int spreads = {len(SPREADS)};
    static constexpr float spreadBands[{len(SPREADS)}] = {_vector(SPREADS)};

    float inputWeights[inputs][hidden];
    float inputBias[hidden];
    float hiddenWeights[hidden][hidden];
    float hiddenBias[hidden];
    float outputWeights[hidden][2];
    float outputBias[2];
    float directWeights[inputs][2];
}};

namespace fieldtuning
{{
inline constexpr FieldNetwork kNetwork {{
    {_matrix(w1)},
    {_vector(b1)},
    {_matrix(w2)},
    {_vector(b2)},
    {_matrix(w3)},
    {_vector(b3)},
    {_matrix(ws)}
}};

'''
    for mode, name in (('basic', 'kBasic'), ('master', 'kMaster')):
        q = numbers[mode]
        s += (f'inline constexpr ModeTuning {name} {{\n    &kNetwork, {_f(MODE_INPUT[mode])},\n    {_f(np.exp(q["softness"]))},\n'
              f'    {_f(q["exponent"])}, {_f(q["exponent_high"])},\n    {_f(q["bias_line"])}, {_f(q["bias_knee"])},\n'
              f'    {_f(q["tilt_low"])}, {_f(q["tilt_high"])}, {_f(q["tilt_knee"])},\n    {_f(q["darken_low"])}, {_f(q["darken_high"])},\n'
              f'    {_f(q["calm"])},\n    {_f(ATTACK)}, {_f(RELEASE)},\n    {_f(LOUDNESS_ATTACK)}, {_f(LOUDNESS_RELEASE)},\n'
              f'    {_f(q["boost_scale"])}, {_f(q["boost_growth"])},\n    {_vector([q[f"boost_treble{i}"] for i in range(1, 7)])}\n}};\n\n')
    return s + '} // namespace fieldtuning\n\n} // namespace colgate\n'


def run_fit(songs, build, write=None, check=False):
    clips = [Clip(name, mode, *song[mode], build) for name, song in songs.items() for mode in ('basic', 'master') if mode in song]
    if not clips:
        raise SystemExit('No renders to train on')
    if check and len(songs) > 1:
        rows = []
        for held in songs:
            others = [c for c in clips if c.name != held]
            if not others:
                continue
            print(f'training without {held}', flush=True)
            net, theta, numbers = train(others)
            rows += [(c.name, c.mode, s, score(net, theta, numbers[c.mode], c, s)) for c in clips if c.name == held for s in sorted(c.targets)]
        report(rows, '\neach song scored with a field trained on the others')
    print('training on every song', flush=True)
    net, theta, numbers = train(clips)
    rows = [(c.name, c.mode, s, score(net, theta, numbers[c.mode], c, s)) for c in clips for s in sorted(c.targets)]
    report(rows, '\nevery song, with the field trained on all of them')
    count = {m: sum(1 for c in clips if c.mode == m) for m in MODE_INPUT}
    text = header(net, theta, numbers, f'Trained on {count["basic"]} songs in Basic and {count["master"]} in Master.')
    if write:
        with open(write, 'w') as f:
            f.write(text)
        print(f'wrote {write}')
    else:
        print('\nrun again with --write source/engine/FieldTuning.h to keep this')


def lufs(x):
    b1, a1 = [1.53512485958697, -2.69169618940638, 1.19839281085285], [1, -1.69065929318241, 0.73248077421585]
    b2, a2 = [1, -2, 1], [1, -1.99004745483398, 0.99007225036621]
    p = sum(sg.lfilter(b2, a2, sg.lfilter(b1, a1, x[:, c])) ** 2 for c in range(x.shape[1]))
    size, step = int(.4 * RATE), int(.1 * RATE)
    blocks = np.array([p[i:i + size].mean() for i in range(0, len(p) - size, step)])
    loud = -0.691 + 10 * np.log10(blocks + 1e-30)
    gate = loud > -70
    gate &= loud > -0.691 + 10 * np.log10(blocks[gate].mean()) - 10
    return -0.691 + 10 * np.log10(blocks[gate].mean())


def fractional_delay(a, b):
    n = 2 ** int(np.ceil(np.log2(len(a))))
    A, C = np.fft.rfft(a[:, 0], n), np.fft.rfft(b[:, 0], n)
    f = np.fft.rfftfreq(n, 1 / RATE)
    m = (f > 150) & (f < 5000)
    c = C[m] * np.conj(A[m])
    w, om = np.abs(c), 2 * np.pi * f[m]
    return -(w * np.angle(c) * om).sum() / (w * om ** 2).sum() * RATE


def advanced(y, samples):
    f = np.fft.rfftfreq(len(y), 1 / RATE)
    return np.fft.irfft(np.fft.rfft(y, axis=0) * np.exp(2j * np.pi * f * samples / RATE)[:, None], len(y), axis=0)


def run_score(songs, build):
    print('song             mode    setting                      R2   curve  corr  total    dry   null   LUFS ref  col')
    for name, song in songs.items():
        for mode, (original, renders) in song.items():
            x = load(original)
            for s, path in sorted(renders.items()):
                y = aligned(x, load(path))
                n = len(x) - 4096
                a, b = x[:n], y[:n]
                with tempfile.TemporaryDirectory() as folder:
                    out = os.path.join(folder, 'out.wav')
                    subprocess.check_call([os.path.join(build, 'colgate_render'), original, out, '--recover', str(s[0]), '--tame', str(s[1]),
                                           '--bias', str(s[2]), '--brighten', str(s[3]), '--boost', str(s[4]), '--mode', mode])
                    c = load(out)[:n]
                total = lambda v: sum(band_power(v[:, k]) for k in range(v.shape[1]))
                power = total(a)
                gr, gc = [10 * np.log10((total(v) + 1e-14) / (power + 1e-14)) for v in (b, c)]
                weight = power / power.sum(0, keepdims=True)
                mr, mc = (gr * weight).sum(0), (gc * weight).sum(0)
                act = (power > power.mean(0) * 0.02) & (power > QUIET)
                act[:40] = False
                dr, dc = gr - mr, gc - mc
                rms = lambda v: np.sqrt((v[act] ** 2).mean())
                shifted = advanced(b, fractional_delay(c, b))
                edge = 2048
                null = 10 * np.log10(((shifted - c)[edge:-edge] ** 2).mean() / ((shifted - a)[edge:-edge] ** 2).mean())
                r2 = 1 - ((gr - gc)[act] ** 2).sum() / ((gr[act] - gr[act].mean()) ** 2).sum()
                print('{:16} {:7} {:26} {:5.3f} {:5.2f}  {:4.2f}  {:5.2f}  {:5.2f}  {:+5.1f}  {:+6.2f} {:+6.2f}'.format(
                    name, mode, str(s), r2, np.sqrt(((mr - mc)[2:-2] ** 2).mean()), np.corrcoef(dr[act], dc[act])[0, 1], rms(gr - gc), rms(gr),
                    null, lufs(b) - lufs(a), lufs(c) - lufs(a)), flush=True)



if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('folders', nargs='+', help='one folder per song')
    parser.add_argument('--build', required=True, help='folder holding colgate_render and colgate_bands')
    parser.add_argument('--write', metavar='FILE', help='where to write the trained field, normally source/engine/FieldTuning.h')
    parser.add_argument('--check', action='store_true', help='first score each song with a field trained on the others (slow)')
    parser.add_argument('--score', action='store_true', help="compare the engine's audio instead of training")
    args = parser.parse_args()
    songs = {}
    for folder in args.folders:
        name = os.path.basename(os.path.normpath(folder))
        songs[name] = find_song(folder)
        if not songs[name]:
            raise SystemExit(f'Found no usable renders in {folder}')
    if args.score:
        run_score(songs, args.build)
    else:
        run_fit(songs, args.build, args.write, args.check)
