# Copyright Roundtree. All Rights Reserved.
"""
Tiny Z-up vector helpers on plain (x, y, z) tuples.

Framing maths lives in pure Python so it can be reasoned about and tested
without the editor. Only scene.py and executor.py convert to and from
unreal.Vector / unreal.Rotator at the boundary.
"""

import math

ZERO = (0.0, 0.0, 0.0)
FORWARD = (1.0, 0.0, 0.0)
UP = (0.0, 0.0, 1.0)


def add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def mul(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def length(a):
    return math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])


def dist(a, b):
    return length(sub(a, b))


def is_near_zero(a, tol=1e-4):
    return length(a) < tol


def normal(a, fallback=ZERO):
    n = length(a)
    return fallback if n < 1e-8 else (a[0] / n, a[1] / n, a[2] / n)


def normal_2d(a, fallback=ZERO):
    """Flatten to the XY plane and normalise. Used for every facing decision."""
    flat = (a[0], a[1], 0.0)
    n = length(flat)
    return fallback if n < 1e-8 else (flat[0] / n, flat[1] / n, 0.0)


def lerp(a, b, t):
    return (a[0] + (b[0] - a[0]) * t,
            a[1] + (b[1] - a[1]) * t,
            a[2] + (b[2] - a[2]) * t)


def clamp(value, low, high):
    return low if value < low else (high if value > high else value)


def yaw_degrees(direction):
    """World yaw of a direction, degrees. Zero for a degenerate vector."""
    flat = normal_2d(direction)
    if is_near_zero(flat):
        return 0.0
    return math.degrees(math.atan2(flat[1], flat[0]))


def spherical_offset(azimuth_deg, elevation_deg):
    """Unit vector at the given yaw/pitch, Z-up."""
    az = math.radians(azimuth_deg)
    el = math.radians(elevation_deg)
    return (math.cos(el) * math.cos(az),
            math.cos(el) * math.sin(az),
            math.sin(el))


def rotator_from_direction(direction):
    """
    (pitch, yaw, roll) in degrees that points along `direction`.
    Matches FRotationMatrix::MakeFromX, which is what a look-at needs.
    """
    d = normal(direction, FORWARD)
    yaw = math.degrees(math.atan2(d[1], d[0]))
    horizontal = math.sqrt(d[0] * d[0] + d[1] * d[1])
    pitch = math.degrees(math.atan2(d[2], horizontal))
    return (pitch, yaw, 0.0)


def direction_from_rotator(rot):
    """Forward vector of a (pitch, yaw, roll) rotator in degrees."""
    pitch = math.radians(rot[0])
    yaw = math.radians(rot[1])
    cp = math.cos(pitch)
    return (cp * math.cos(yaw), cp * math.sin(yaw), math.sin(pitch))


def normalize_angle(degrees):
    """Wrap to (-180, 180]."""
    a = math.fmod(degrees + 180.0, 360.0)
    if a < 0.0:
        a += 360.0
    return a - 180.0


def unwind_towards(angle, reference):
    """
    Return `angle` shifted by whole turns so it is within 180 degrees of
    `reference`. Keeps a baked yaw track from spinning the wrong way between
    samples, which is the Euler continuity problem in the C++ executor.
    """
    return reference + normalize_angle(angle - reference)
