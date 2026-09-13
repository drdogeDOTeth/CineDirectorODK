# Copyright Roundtree. All Rights Reserved.
"""
Where the camera stands, before the move is applied.

Pure framing maths ported from ShotPlanExecutor.cpp. Subjects arrive as
SubjectSample snapshots so nothing here touches the editor, which keeps the
framing rules testable on their own.
"""

import math

from . import vec


class SubjectSample(object):
    """A frozen read of one actor: bounds, root location, face point and facing."""

    __slots__ = ("center", "extent", "location", "head", "facing_dir")

    def __init__(self, center, extent, location, head=None, facing_dir=None):
        self.center = center
        self.extent = extent
        self.location = location
        self.head = head                      # mid-face point, or None
        self.facing_dir = facing_dir or vec.FORWARD

    @property
    def has_head(self):
        return self.head is not None

    @property
    def body_radius(self):
        return max(vec.length(self.extent), 25.0)

    def height_point(self, height01):
        low = self.center[2] - self.extent[2]
        high = self.center[2] + self.extent[2]
        return (self.center[0], self.center[1], low + (high - low) * height01)

    @property
    def face_point(self):
        """Mid-face when there is a head, else high on the bounds."""
        return self.head if self.has_head else self.height_point(0.88)


def effective_shot_size(seg):
    """
    The shot size that actually drives framing. A zoom-in or a focus pull with no
    stated size still wants the face, not the belly of the bounds.
    """
    if seg.shot_size != "unspecified":
        return seg.shot_size
    if seg.move == "zoom_in":
        return "medium_close_up"
    if seg.track_focus:
        return "medium_close_up"
    return "unspecified"


def framing_factor(size):
    """Camera distance as a multiple of the subject's framing radius."""
    return {
        "extreme_close_up": 2.6,
        "close_up": 3.3,
        "medium_close_up": 3.6,
        "medium": 4.5,
        "wide": 8.0,
        "extreme_wide": 15.0,
    }.get(size, 4.5)


# Default full-frame sensor width. The executor sets the real filmback on the
# camera, so this only has to agree with that default.
SENSOR_WIDTH_MM = 36.0

# A little air around a fitted subject, so it does not touch the frame edge.
FIT_MARGIN = 1.12


def fit_distance(radius, focal_length_mm):
    """
    How far back a sphere of this radius has to be to fill the frame.

    The multiples above answer "how small is the subject in frame", which is the
    right question for a person and the wrong one for a landscape: they are
    ratios, so a 500 m subject at extreme wide lands 7.5 km away and reads as a
    speck. This answers "how far back do I have to be to see all of it", which
    is what someone asking for a shot of the whole terrain actually means.
    """
    focal = focal_length_mm if focal_length_mm > 0.0 else 35.0
    half_fov = math.atan((SENSOR_WIDTH_MM * 0.5) / focal)
    return max(radius / max(math.tan(half_fov), 1e-4) * FIT_MARGIN, 40.0)


class SubjectFraming(object):
    __slots__ = ("point", "radius", "has_head")

    def __init__(self, point=vec.ZERO, radius=100.0, has_head=False):
        self.point = point
        self.radius = radius
        self.has_head = has_head


def resolve_subject_framing(sample, size):
    """
    Interest point and framing radius for a subject. Tight shots on a character
    with a head bone aim at the face; wides stay body-centred.
    """
    out = SubjectFraming()
    if sample is None:
        return out

    body_center = sample.center
    body_radius = sample.body_radius
    has_head = sample.has_head
    face_point = sample.face_point

    out.has_head = has_head

    if size == "extreme_close_up":
        out.point = face_point
        out.radius = (vec.clamp(body_radius * 0.22, 16.0, 40.0)
                      if has_head else body_radius * 0.22)

    elif size == "close_up":
        # Full face, not skull-only: keep the chin and a little air.
        point = face_point
        if has_head:
            drop = vec.clamp(body_radius * 0.02, 2.0, 8.0)
            point = (point[0], point[1], point[2] - drop)
        out.point = point
        out.radius = (vec.clamp(body_radius * 0.38, 30.0, 70.0)
                      if has_head else body_radius * 0.32)

    elif size == "medium_close_up":
        # Chest-up: face in frame, shoulders read, not locked to the skull.
        out.point = (vec.lerp(body_center, face_point, 0.62)
                     if has_head else sample.height_point(0.78))
        out.radius = body_radius * 0.48

    elif size == "medium":
        out.point = (vec.lerp(body_center, face_point, 0.45)
                     if has_head else sample.height_point(0.62))
        out.radius = body_radius * 0.72

    elif size == "unspecified":
        # Default character framing: upper body, face-biased.
        if has_head:
            out.point = vec.lerp(body_center, face_point, 0.70)
            out.radius = body_radius * 0.65
        else:
            out.point = body_center
            out.radius = body_radius

    else:  # wide, extreme_wide, fit
        out.point = body_center
        out.radius = body_radius

    return out


def framing_distance(seg, size, radius):
    """Camera distance for a framing, before any move runs."""
    if size == "fit":
        # Solved from the real lens, so the subject fills the frame whatever
        # its size. A longer lens does not push the camera back here; it pulls
        # it back exactly as far as the narrower field of view requires.
        return fit_distance(radius, seg.focal_length_mm)

    # A longer lens sits further back to hold the same framing.
    lens_scale = (seg.focal_length_mm / 35.0) if seg.focal_length_mm > 0.0 else 1.0
    return max(radius * framing_factor(size) * lens_scale, 40.0)


class ShotGeometry(object):
    """Camera placement relative to the subject, before the move runs."""

    __slots__ = ("has_target", "target_point", "radius", "distance",
                 "azimuth_deg", "elevation_deg", "has_look_at", "aim_point")

    def __init__(self):
        self.has_target = False
        self.target_point = vec.ZERO
        self.radius = 100.0
        self.distance = 500.0
        self.azimuth_deg = 0.0        # world yaw of (camera - target)
        self.elevation_deg = 0.0
        self.has_look_at = False
        self.aim_point = vec.ZERO

    def camera_position(self, azimuth=None, elevation=None, distance=None,
                        pivot=None):
        """World position at a spherical frame around the pivot."""
        az = self.azimuth_deg if azimuth is None else azimuth
        el = self.elevation_deg if elevation is None else elevation
        dist = self.distance if distance is None else distance
        origin = self.target_point if pivot is None else pivot
        return vec.add(origin, vec.mul(vec.spherical_offset(az, el), dist))


def compute_geometry(seg, target_sample, look_at_sample, view_loc, view_rot):
    """Opening placement for a shot that starts fresh (not chained into a take)."""
    geo = ShotGeometry()
    size = effective_shot_size(seg)

    if target_sample is not None:
        frame = resolve_subject_framing(target_sample, size)
        geo.has_target = True
        geo.target_point = frame.point
        geo.radius = frame.radius

        # Two frames of reference for sides:
        #  - Possessive ("its left") uses FACE forward, since void and VRM roots
        #    are often 90 degrees off the mesh.
        #  - Plain ("from the left") is viewer-relative: front is the side facing
        #    the editor viewport right now.
        # Left swings opposite ways because the viewer looks toward the subject
        # while the character faces out from their own front.
        if seg.actor_relative_side:
            facing_yaw = vec.yaw_degrees(target_sample.facing_dir)
            left_swing = -90.0
        else:
            to_viewer = vec.sub(view_loc, geo.target_point)
            facing_yaw = math.degrees(math.atan2(to_viewer[1], to_viewer[0]))
            left_swing = 90.0

        geo.azimuth_deg = {
            "front": facing_yaw,
            "behind": facing_yaw + 180.0,
            "left": facing_yaw + left_swing,
            "right": facing_yaw - left_swing,
            "over_shoulder": facing_yaw + 145.0,
        }.get(seg.view_side, facing_yaw)

        geo.elevation_deg = {
            "low": -18.0,
            "high": 30.0,
            "overhead": 75.0,
        }.get(seg.angle, 0.0)

        geo.distance = framing_distance(seg, size, geo.radius)

        if seg.view_side == "over_shoulder":
            geo.distance *= 0.7
            geo.elevation_deg += 8.0

    else:
        # No subject: anchor on the point the viewport camera is looking at.
        geo.target_point = vec.add(view_loc, vec.mul(vec.direction_from_rotator(view_rot), 500.0))
        geo.radius = 100.0
        geo.distance = 500.0

        offset = vec.sub(view_loc, geo.target_point)
        geo.azimuth_deg = math.degrees(math.atan2(offset[1], offset[0]))
        span = max(vec.length(offset), 1.0)
        geo.elevation_deg = math.degrees(math.asin(vec.clamp(offset[2] / span, -1.0, 1.0)))

    geo.aim_point = geo.target_point
    if look_at_sample is not None:
        # Aim at the look-at actor with the same framing intent.
        geo.aim_point = resolve_subject_framing(look_at_sample, size).point
        geo.has_look_at = True

    return geo


def compute_geometry_chained(seg, target_sample, look_at_sample, prev_pos, prev_rot):
    """
    Placement for the next move of a continuous take. The camera stays where the
    previous move left it and the spherical frame is derived from that offset, so
    framing and view-side words only position the very first move. The interest
    point still updates, so a push-in inside a oner lands on the face.
    """
    geo = ShotGeometry()
    size = effective_shot_size(seg)

    if target_sample is not None:
        frame = resolve_subject_framing(target_sample, size)
        geo.has_target = True
        geo.target_point = frame.point
        geo.radius = frame.radius
    else:
        geo.target_point = vec.add(prev_pos, vec.mul(vec.direction_from_rotator(prev_rot), 500.0))
        geo.radius = 100.0

    offset = vec.sub(prev_pos, geo.target_point)
    geo.distance = max(vec.length(offset), 1.0)
    geo.azimuth_deg = math.degrees(math.atan2(offset[1], offset[0]))
    geo.elevation_deg = math.degrees(
        math.asin(vec.clamp(offset[2] / geo.distance, -1.0, 1.0)))

    geo.aim_point = geo.target_point
    if look_at_sample is not None:
        geo.aim_point = resolve_subject_framing(look_at_sample, size).point
        geo.has_look_at = True

    return geo


# ---------------------------------------------------------------------------
# Move evaluation
# ---------------------------------------------------------------------------

def default_move_amount(seg, geo):
    """
    Fill in a sensible magnitude when the plan left move_amount at 0.
    Angular moves are degrees; the rest are centimetres, scaled off the framing
    distance so a move reads the same on a figurine and on a cathedral.
    """
    move = seg.move
    if seg.move_amount:
        return abs(seg.move_amount)

    if move in ("orbit_cw", "orbit_ccw"):
        return 90.0
    if move in ("pan_left", "pan_right"):
        return 45.0
    if move in ("tilt_up", "tilt_down"):
        return 25.0
    if move in ("dolly_in", "dolly_out"):
        return geo.distance * 0.45
    if move in ("truck_left", "truck_right"):
        return geo.distance * 0.5
    if move in ("crane_up", "crane_down"):
        return max(geo.radius * 2.0, geo.distance * 0.4)
    if move == "flyover":
        return max(geo.distance * 1.4, 400.0)
    return 0.0


def ease(alpha, easing):
    """Smoothstep for ease_in_out, identity for linear."""
    if easing == "linear":
        return alpha
    return alpha * alpha * (3.0 - 2.0 * alpha)


def sample_move(seg, geo, alpha, amount):
    """
    Camera position and aim point at normalised time `alpha` through the move.

    Returns (position, aim_point, focal_length_scale). The focal scale is 1.0 for
    every move except the zooms, which hold position and change the lens.
    """
    t = ease(vec.clamp(alpha, 0.0, 1.0), seg.easing)
    move = seg.move

    azimuth = geo.azimuth_deg
    elevation = geo.elevation_deg
    distance = geo.distance
    pivot = geo.target_point
    aim = geo.aim_point
    focal_scale = 1.0

    if move == "orbit_cw":
        azimuth += amount * t
    elif move == "orbit_ccw":
        azimuth -= amount * t
    elif move == "dolly_in":
        distance = max(distance - amount * t, 20.0)
    elif move == "dolly_out":
        distance = distance + amount * t
    elif move == "crane_up":
        pivot = (pivot[0], pivot[1], pivot[2])
        position = geo.camera_position(azimuth, elevation, distance, pivot)
        position = (position[0], position[1], position[2] + amount * t)
        return position, aim, focal_scale
    elif move == "crane_down":
        position = geo.camera_position(azimuth, elevation, distance, pivot)
        position = (position[0], position[1], position[2] - amount * t)
        return position, aim, focal_scale
    elif move in ("truck_left", "truck_right"):
        position = geo.camera_position(azimuth, elevation, distance, pivot)
        # Strafe perpendicular to the camera-to-target axis, in the ground plane.
        to_target = vec.normal_2d(vec.sub(aim, position), vec.FORWARD)
        right = vec.normal(vec.cross(vec.UP, to_target), (0.0, 1.0, 0.0))
        sign = -1.0 if move == "truck_left" else 1.0
        position = vec.add(position, vec.mul(right, sign * amount * t))
        return position, aim, focal_scale
    elif move == "flyover":
        position = geo.camera_position(azimuth, elevation, distance, pivot)
        forward = vec.normal_2d(vec.sub(aim, position), vec.FORWARD)
        position = vec.add(position, vec.mul(forward, amount * t))
        return position, aim, focal_scale
    elif move in ("pan_left", "pan_right", "tilt_up", "tilt_down"):
        # The body stays put; only the aim swings.
        position = geo.camera_position(azimuth, elevation, distance, pivot)
        to_aim = vec.sub(aim, position)
        span = max(vec.length(to_aim), 1.0)
        rot = vec.rotator_from_direction(to_aim)
        pitch, yaw = rot[0], rot[1]
        if move == "pan_left":
            yaw -= amount * t
        elif move == "pan_right":
            yaw += amount * t
        elif move == "tilt_up":
            pitch += amount * t
        else:
            pitch -= amount * t
        aim = vec.add(position, vec.mul(vec.direction_from_rotator((pitch, yaw, 0.0)), span))
        return position, aim, focal_scale
    elif move in ("zoom_in", "zoom_out"):
        base = seg.focal_length_mm if seg.focal_length_mm > 0.0 else 35.0
        # move_amount is a target focal length in mm; 0 doubles or halves.
        target = seg.move_amount if seg.move_amount > 0.0 else (
            base * 2.0 if move == "zoom_in" else base * 0.5)
        focal_scale = (base + (target - base) * t) / base

    position = geo.camera_position(azimuth, elevation, distance, pivot)
    return position, aim, focal_scale
