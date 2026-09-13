# Copyright Roundtree. All Rights Reserved.
"""
Shot-plan data model and wire contract for CineDirector ODK.

This is a faithful port of the C++ plugin's ShotPlanTypes.h and ShotPlanJson.cpp,
kept deliberately free of any `unreal` import so the model, the JSON schema and the
reply parser can be exercised outside the editor. The wire tokens here are the same
tokens the 5.8 C++ plugin uses, so one planner (rule-based or LLM) can feed either
executor.

Actor references are carried as plain label strings. Binding a label to a live actor
is the executor's job, because only it has a level to look in.
"""

import json

# ---------------------------------------------------------------------------
# Wire vocabularies. These strings ARE the schema enums; adding a move means
# adding it here and in the schema below.
# ---------------------------------------------------------------------------

MOVES = (
    "static", "dolly_in", "dolly_out", "orbit_cw", "orbit_ccw",
    "truck_left", "truck_right", "crane_up", "crane_down",
    "pan_left", "pan_right", "tilt_up", "tilt_down",
    "zoom_in", "zoom_out", "flyover",
)

SHOT_SIZES = (
    "unspecified", "extreme_close_up", "close_up", "medium_close_up",
    "medium", "wide", "extreme_wide", "fit",
)

ANGLES = ("eye_level", "low", "high", "overhead")

VIEW_SIDES = ("front", "behind", "left", "right", "over_shoulder")

EASINGS = ("ease_in_out", "linear")

TIMES_OF_DAY = (
    "unchanged", "dawn", "morning", "noon", "afternoon", "golden_hour",
    "sunset", "dusk", "night", "midnight", "overcast",
)

# Moves whose amount is an angle in degrees rather than a distance in cm.
ANGULAR_MOVES = frozenset((
    "orbit_cw", "orbit_ccw", "pan_left", "pan_right", "tilt_up", "tilt_down",
))


# Spellings a model reaches for that are not the wire word. Only applied when the
# canonical form is legal for the field being read, so the same table can serve
# every field without one field's synonym leaking into another.
#
# This matters for the backends that get the schema in the prompt rather than as
# a strict constraint: there, "push_in" is a plausible thing to receive, and
# flattening it to "static" would quietly turn a dolly into a locked-off shot.
_SYNONYMS = {
    # moves
    "push_in": "dolly_in", "push": "dolly_in", "dolly_forward": "dolly_in",
    "move_in": "dolly_in", "in": "dolly_in",
    "pull_back": "dolly_out", "pull_out": "dolly_out", "dolly_back": "dolly_out",
    "pullback": "dolly_out", "out": "dolly_out",
    "orbit": "orbit_cw", "circle": "orbit_cw", "arc": "orbit_cw",
    "orbit_clockwise": "orbit_cw", "revolve": "orbit_cw",
    "orbit_counter_clockwise": "orbit_ccw", "orbit_anticlockwise": "orbit_ccw",
    "orbit_ccw_left": "orbit_ccw",
    "boom_up": "crane_up", "jib_up": "crane_up", "pedestal_up": "crane_up",
    "boom_down": "crane_down", "jib_down": "crane_down",
    "pedestal_down": "crane_down",
    "dolly_left": "truck_left", "track_left": "truck_left",
    "crab_left": "truck_left",
    "dolly_right": "truck_right", "track_right": "truck_right",
    "crab_right": "truck_right",
    "aerial": "flyover", "drone": "flyover", "fly_over": "flyover",
    "locked_off": "static", "locked": "static", "lock_off": "static",
    "none": "static", "hold": "static", "still": "static", "fixed": "static",
    # framing
    "ecu": "extreme_close_up", "extreme_closeup": "extreme_close_up",
    "cu": "close_up", "closeup": "close_up", "close": "close_up",
    "mcu": "medium_close_up", "medium_closeup": "medium_close_up",
    "ms": "medium", "medium_shot": "medium", "mid": "medium",
    "ws": "wide", "wide_shot": "wide", "long": "wide", "long_shot": "wide",
    "full": "wide", "full_shot": "wide",
    "ews": "extreme_wide", "establishing": "extreme_wide",
    "extreme_long": "extreme_wide", "very_wide": "extreme_wide",
    # angles
    "low_angle": "low", "worms_eye": "low", "up": "low",
    "high_angle": "high", "down": "high",
    "birds_eye": "overhead", "bird_eye": "overhead", "birdseye": "overhead",
    "top_down": "overhead", "topdown": "overhead", "above": "overhead",
    "eye": "eye_level", "neutral": "eye_level", "level": "eye_level",
    # sides
    "back": "behind", "rear": "behind", "from_behind": "behind",
    "over_the_shoulder": "over_shoulder", "ots": "over_shoulder",
    "shoulder": "over_shoulder",
    # easing
    "smooth": "ease_in_out", "ease": "ease_in_out", "ease_in": "ease_in_out",
    "ease_out": "ease_in_out", "constant": "linear", "even": "linear",
    # light
    "sunrise": "dawn", "daybreak": "dawn", "magic_hour": "golden_hour",
    "sundown": "sunset", "twilight": "dusk", "evening": "dusk",
    "cloudy": "overcast", "grey": "overcast", "gray": "overcast",
    "day": "noon", "midday": "noon", "nighttime": "night",
}


def _token(value, allowed, fallback, field="", warnings=None):
    """Case-insensitive token lookup that falls back instead of raising."""
    if isinstance(value, str):
        low = value.strip().lower().replace("-", "_").replace(" ", "_")
        if low in allowed:
            return low
        canonical = _SYNONYMS.get(low)
        if canonical is not None and canonical in allowed:
            return canonical
        if low and warnings is not None:
            # Say so: a silently downgraded move is the difference between the
            # shot the user asked for and a locked-off one.
            warnings.append("Did not recognise %s \"%s\"; used \"%s\"."
                            % (field or "value", value.strip(), fallback))
    return fallback


def _clamp(value, low, high):
    return low if value < low else (high if value > high else value)


def _num(obj, field, default):
    value = obj.get(field, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _flag(obj, field, default):
    value = obj.get(field, None)
    return value if isinstance(value, bool) else default


def _text(obj, field):
    value = obj.get(field, None)
    return value.strip() if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

class Look(object):
    """Colour grade for one shot. Neutral unless apply_grade is set."""

    __slots__ = ("apply_grade", "saturation", "contrast", "gain",
                 "white_temp_kelvin", "white_tint", "motion_blur",
                 "scene_color_tint")

    def __init__(self):
        self.apply_grade = False
        self.saturation = 1.0
        self.contrast = 1.0
        self.gain = 1.0
        self.white_temp_kelvin = 0.0   # 0 leaves the camera default (~6500)
        self.white_tint = 0.0          # -1 magenta .. 1 green
        self.motion_blur = -1.0        # -1 leaves the default
        self.scene_color_tint = (1.0, 1.0, 1.0)

    def to_dict(self):
        return {
            "apply_grade": self.apply_grade,
            "saturation": self.saturation,
            "contrast": self.contrast,
            "gain": self.gain,
            "white_temp_kelvin": self.white_temp_kelvin,
            "white_tint": self.white_tint,
            "motion_blur": self.motion_blur,
        }

    @classmethod
    def from_dict(cls, obj):
        look = cls()
        if not isinstance(obj, dict):
            return look
        look.apply_grade = _flag(obj, "apply_grade", False)
        look.saturation = _clamp(_num(obj, "saturation", 1.0), 0.0, 4.0)
        look.contrast = _clamp(_num(obj, "contrast", 1.0), 0.0, 4.0)
        look.gain = _clamp(_num(obj, "gain", 1.0), 0.0, 4.0)
        look.white_temp_kelvin = _clamp(_num(obj, "white_temp_kelvin", 0.0), 0.0, 15000.0)
        look.white_tint = _clamp(_num(obj, "white_tint", 0.0), -1.0, 1.0)
        look.motion_blur = _clamp(_num(obj, "motion_blur", -1.0), -1.0, 1.0)
        return look


class Segment(object):
    """One camera and one move. "close-up, then orbit" yields two of these."""

    def __init__(self):
        # Provenance and identity
        self.raw_text = ""
        self.camera_label = ""      # executor fills this in when empty

        # Subjects, held as labels; the executor resolves them against the level.
        self.target = ""
        self.look_at = ""           # lens aims here even while the move pivots on target
        self.rack_focus_to = ""

        # Framing
        self.move = "static"
        self.move_amount = 0.0      # degrees for angular moves, cm otherwise, mm for zoom
        self.shot_size = "unspecified"
        # Frame the whole level rather than one actor.
        self.frame_level = False
        self.angle = "eye_level"
        self.view_side = "front"
        self.actor_relative_side = False
        self.easing = "ease_in_out"
        self.duration_seconds = 5.0

        # Lens and focus
        self.focal_length_mm = 0.0   # 0 uses the 35mm default
        self.aperture = 0.0          # 0 uses f/2.8
        self.track_focus = False
        self.deep_focus = False
        self.fixed_focus = False
        self.look_at_target = True
        self.follow_subject = False

        # Operator feel
        self.handheld_intensity = 0.0
        self.dutch_angle_deg = 0.0

        # Post-process overrides; 0 leaves the camera default.
        self.film_grain = 0.0
        self.vignette = 0.0
        self.chromatic_aberration = 0.0
        self.bloom = 0.0
        self.lens_flare = 0.0

        self.look = Look()
        self.style_kit_name = ""

        # Optional filmback override; sensor height 0 keeps the cine default.
        self.filmback_sensor_width_mm = 0.0
        self.filmback_sensor_height_mm = 0.0

        # Level lighting for this shot
        self.time_of_day = "unchanged"
        self.fog_density = -1.0      # negative leaves the level fog alone
        self.god_rays = False
        self.volumetric_fog = False

        self.notes = []

    # -- convenience -------------------------------------------------------

    @property
    def has_subject(self):
        return bool(self.target) or bool(self.look_at)

    @property
    def is_angular_move(self):
        return self.move in ANGULAR_MOVES

    @property
    def aim_label(self):
        """Which actor the lens should point at, preferring an explicit look_at."""
        return self.look_at or self.target

    def to_dict(self):
        return {
            "raw_text": self.raw_text,
            "target": self.target,
            "look_at": self.look_at,
            "rack_focus_to": self.rack_focus_to,
            "move": self.move,
            "move_amount": self.move_amount,
            "shot_size": self.shot_size,
            "frame_level": self.frame_level,
            "angle": self.angle,
            "view_side": self.view_side,
            "actor_relative_side": self.actor_relative_side,
            "easing": self.easing,
            "duration_seconds": self.duration_seconds,
            "focal_length_mm": self.focal_length_mm,
            "aperture": self.aperture,
            "track_focus": self.track_focus,
            "deep_focus": self.deep_focus,
            "fixed_focus": self.fixed_focus,
            "look_at_target": self.look_at_target,
            "follow_subject": self.follow_subject,
            "handheld_intensity": self.handheld_intensity,
            "dutch_angle_deg": self.dutch_angle_deg,
            "film_grain": self.film_grain,
            "vignette": self.vignette,
            "chromatic_aberration": self.chromatic_aberration,
            "bloom": self.bloom,
            "lens_flare": self.lens_flare,
            "look": self.look.to_dict(),
            "style_kit_name": self.style_kit_name,
            "time_of_day": self.time_of_day,
            "fog_density": self.fog_density,
            "god_rays": self.god_rays,
            "volumetric_fog": self.volumetric_fog,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, obj):
        """Read one segment from a model reply, clamping every value into range."""
        seg = cls()
        seg.raw_text = _text(obj, "raw_text")

        seg.target = _text(obj, "target")
        seg.look_at = _text(obj, "look_at")
        seg.rack_focus_to = _text(obj, "rack_focus_to")

        warnings = []
        seg.move = _token(obj.get("move"), MOVES, "static", "move", warnings)
        seg.shot_size = _token(obj.get("shot_size"), SHOT_SIZES, "unspecified",
                               "shot size", warnings)
        seg.frame_level = _flag(obj, "frame_level", False)
        seg.angle = _token(obj.get("angle"), ANGLES, "eye_level", "angle", warnings)
        seg.view_side = _token(obj.get("view_side"), VIEW_SIDES, "front",
                               "view side", warnings)
        seg.easing = _token(obj.get("easing"), EASINGS, "ease_in_out",
                            "easing", warnings)
        seg.time_of_day = _token(obj.get("time_of_day"), TIMES_OF_DAY, "unchanged",
                                 "time of day", warnings)

        seg.actor_relative_side = _flag(obj, "actor_relative_side", False)
        seg.duration_seconds = _clamp(_num(obj, "duration_seconds", 5.0), 0.1, 600.0)
        seg.move_amount = _num(obj, "move_amount", 0.0)
        seg.focal_length_mm = _clamp(_num(obj, "focal_length_mm", 0.0), 0.0, 1000.0)
        seg.aperture = _clamp(_num(obj, "aperture", 0.0), 0.0, 32.0)

        seg.deep_focus = _flag(obj, "deep_focus", False)
        seg.fixed_focus = _flag(obj, "fixed_focus", False)
        seg.look_at_target = _flag(obj, "look_at_target", True)
        seg.follow_subject = _flag(obj, "follow_subject", False)

        # Focus tracking only means something with a subject, and is mutually
        # exclusive with the two manual-focus modes.
        has_subject = bool(seg.target) or bool(seg.look_at)
        seg.track_focus = (_flag(obj, "track_focus", has_subject)
                           and has_subject and not seg.deep_focus and not seg.fixed_focus)

        seg.handheld_intensity = _clamp(_num(obj, "handheld_intensity", 0.0), 0.0, 5.0)
        seg.dutch_angle_deg = _num(obj, "dutch_angle_deg", 0.0)

        seg.film_grain = _clamp(_num(obj, "film_grain", 0.0), 0.0, 1.0)
        seg.vignette = _clamp(_num(obj, "vignette", 0.0), 0.0, 1.0)
        seg.chromatic_aberration = _clamp(_num(obj, "chromatic_aberration", 0.0), 0.0, 5.0)
        seg.bloom = _clamp(_num(obj, "bloom", 0.0), 0.0, 8.0)
        seg.lens_flare = _clamp(_num(obj, "lens_flare", 0.0), 0.0, 8.0)

        seg.look = Look.from_dict(obj.get("look"))
        seg.style_kit_name = _text(obj, "style_kit_name")

        seg.fog_density = _clamp(_num(obj, "fog_density", -1.0), -1.0, 1.0)
        seg.god_rays = _flag(obj, "god_rays", False)
        seg.volumetric_fog = _flag(obj, "volumetric_fog", False)

        notes = obj.get("notes")
        if isinstance(notes, list):
            seg.notes = [n.strip() for n in notes if isinstance(n, str) and n.strip()]
        seg.notes = list(seg.notes) + warnings

        return seg


# Fields that describe the picture rather than the shot. A style kit, the grain
# and bloom it carries, the grade, the operator's feel: a director states these
# once and they hold for the scene. Framing, move, angle and duration are the
# opposite, and are never carried.
LOOK_SCALARS = (
    "film_grain", "vignette", "chromatic_aberration", "bloom", "lens_flare",
    "handheld_intensity", "dutch_angle_deg",
    "filmback_sensor_width_mm", "filmback_sensor_height_mm",
)


def _look_snapshot(seg):
    state = dict((field, getattr(seg, field)) for field in LOOK_SCALARS)
    state["style_kit_name"] = seg.style_kit_name
    state["look"] = seg.look
    return state


def apply_look_continuity(plan):
    """
    Carry each shot's look onto the shots that follow it.

    "bodycam chase, then a close-up on her face" should not lose the bodycam on
    the cut. Naming a style kit replaces the look wholesale rather than layering,
    so switching from noir to bodycam mid-plan gives bodycam and not both.

    Returns the labels of the shots that inherited something, for the notes.
    """
    state = None
    inherited = []

    for index, seg in enumerate(plan.segments):
        if seg.style_kit_name:
            # A named kit is a deliberate reset: it becomes the whole look.
            state = _look_snapshot(seg)
            continue

        if state is not None:
            touched = False
            for field in LOOK_SCALARS:
                if getattr(seg, field) == 0.0 and state.get(field, 0.0) != 0.0:
                    setattr(seg, field, state[field])
                    touched = True
            if not seg.look.apply_grade and state["look"].apply_grade:
                seg.look = state["look"]
                touched = True
            if state["style_kit_name"]:
                seg.style_kit_name = state["style_kit_name"]
                touched = True
            if touched:
                inherited.append(index)

        if state is None:
            state = _look_snapshot(seg)
        else:
            # Anything this shot states for itself joins the running look.
            for field in LOOK_SCALARS:
                value = getattr(seg, field)
                if value != 0.0:
                    state[field] = value
            if seg.look.apply_grade:
                state["look"] = seg.look

    return inherited


class ShotPlan(object):
    """The full result of interpreting one description."""

    def __init__(self):
        self.segments = []
        self.create_camera_cuts = True
        self.one_continuous_shot = False

        # Set when a plan came from the fallback parser after a backend failed.
        self.provider_note = ""

    def to_dict(self):
        return {
            "one_continuous_shot": self.one_continuous_shot,
            "create_camera_cuts": self.create_camera_cuts,
            "segments": [s.to_dict() for s in self.segments],
        }

    def to_json(self, indent=2):
        return json.dumps(self.to_dict(), indent=indent)

    @property
    def total_seconds(self):
        return sum(s.duration_seconds for s in self.segments)


# ---------------------------------------------------------------------------
# Reply parsing
# ---------------------------------------------------------------------------

def extract_json_object(text):
    """
    Pull the first balanced {...} out of a model reply, skipping over string
    literals so a brace inside raw_text or a note does not throw off the depth
    count. Returns None when there is no object.
    """
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        ch = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


class PlanError(Exception):
    """Raised when a reply cannot be read as a shot plan."""


def parse_plan(reply):
    """
    Turn a model reply into a ShotPlan. Raises PlanError with a message fit for
    the panel's status line when the reply is unusable.
    """
    blob = extract_json_object(reply)
    if blob is None:
        raise PlanError("The model did not return a shot plan (no JSON object in the reply).")

    try:
        root = json.loads(blob)
    except ValueError:
        raise PlanError("The model's shot plan was not valid JSON.")

    if not isinstance(root, dict):
        raise PlanError("The model's shot plan was not a JSON object.")

    raw_segments = root.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise PlanError("The model's shot plan contained no shots.")

    plan = ShotPlan()
    plan.one_continuous_shot = _flag(root, "one_continuous_shot", False)
    plan.create_camera_cuts = _flag(root, "create_camera_cuts", True)
    plan.segments = [Segment.from_dict(s) for s in raw_segments if isinstance(s, dict)]

    if not plan.segments:
        raise PlanError("The model's shot plan had no readable shots in it.")

    # A model that states a look once and then omits it on later shots meant for
    # it to hold, the same as a person would.
    apply_look_continuity(plan)

    return plan


def describe_plan(plan):
    """One-line-per-shot summary, shown in the panel and the log."""
    count = len(plan.segments)
    out = "%d shot%s%s" % (
        count,
        "" if count == 1 else "s",
        ", one continuous take" if plan.one_continuous_shot else "",
    )

    for index, seg in enumerate(plan.segments):
        out += "\n  %d. %s | %s | %s | %s | %.1fs" % (
            index + 1,
            "(whole level)" if seg.frame_level else (seg.target or "(viewport)"),
            seg.move,
            seg.shot_size,
            seg.angle,
            seg.duration_seconds,
        )
        if seg.focal_length_mm > 0.0:
            out += " | %.0fmm" % seg.focal_length_mm
        if seg.aperture > 0.0:
            out += " | f/%.1f" % seg.aperture
        if seg.handheld_intensity > 0.0:
            out += " | handheld %.2f" % seg.handheld_intensity
        if seg.time_of_day != "unchanged":
            out += " | %s" % seg.time_of_day
        if seg.style_kit_name:
            out += " | %s" % seg.style_kit_name
        if seg.raw_text:
            out += '\n       "%s"' % seg.raw_text
        for note in seg.notes:
            out += "\n       note: %s" % note

    return out


# ---------------------------------------------------------------------------
# The strict JSON schema. Every property required, additionalProperties false:
# that is what all three providers' strict structured-output modes demand.
# Neutral values stand in for "not applicable" so nothing has to be nullable.
# ---------------------------------------------------------------------------

def build_schema():
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["one_continuous_shot", "create_camera_cuts", "segments"],
        "properties": {
            "one_continuous_shot": {
                "type": "boolean",
                "description": "True only for an explicit single unbroken take: every segment chains onto one camera with no cuts.",
            },
            "create_camera_cuts": {
                "type": "boolean",
                "description": "Normally true so the sequence plays shot to shot. False leaves the cameras uncut.",
            },
            "segments": {
                "type": "array",
                "description": "One entry per shot, in playback order.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "raw_text", "target", "look_at", "rack_focus_to", "move",
                        "move_amount", "shot_size", "angle", "view_side",
                        "actor_relative_side", "easing", "duration_seconds",
                        "focal_length_mm", "aperture", "track_focus", "deep_focus",
                        "fixed_focus", "look_at_target", "follow_subject",
                        "handheld_intensity", "dutch_angle_deg", "film_grain",
                        "vignette", "chromatic_aberration", "bloom", "lens_flare",
                        "look", "style_kit_name", "time_of_day", "fog_density",
                        "god_rays", "volumetric_fog", "frame_level", "notes",
                    ],
                    "properties": {
                        "raw_text": {"type": "string", "description": "The clause of the request this shot came from."},
                        "target": {"type": "string", "description": "Exact label of the subject actor, copied from the scene list. Empty means no subject: the shot is framed from the current viewport camera."},
                        "look_at": {"type": "string", "description": "Actor the lens stays aimed at when it differs from target (orbit around the tower looking at the knight). Empty otherwise."},
                        "rack_focus_to": {"type": "string", "description": "Second actor for a rack focus pull. Empty otherwise."},
                        "move": {"type": "string", "enum": list(MOVES)},
                        "move_amount": {"type": "number", "description": "0 picks a sensible default. Orbit/pan/tilt: degrees. Dolly/truck/crane/flyover: centimetres. Zoom: target focal length in mm."},
                        "shot_size": {"type": "string", "enum": list(SHOT_SIZES)}, "frame_level": {"type": "boolean", "description": "True when the request is about the whole level or area rather than one actor (\"show the whole map\", \"everything\"). The executor frames the combined bounds of the scene, backdrops excluded, and target is ignored."},
                        "angle": {"type": "string", "enum": list(ANGLES)},
                        "view_side": {"type": "string", "enum": list(VIEW_SIDES)},
                        "actor_relative_side": {"type": "boolean", "description": "True when the side was phrased possessively (its left, their back); false means screen-relative as the viewport sees it now."},
                        "easing": {"type": "string", "enum": list(EASINGS)},
                        "duration_seconds": {"type": "number", "description": "Shot length. Default 5 when unstated; 2-3 for cuts in an action beat, 8-12 for a slow establishing move."},
                        "focal_length_mm": {"type": "number", "description": "0 uses the 35mm default. 18-24 wide, 35 neutral, 85 portrait, 135+ telephoto."},
                        "aperture": {"type": "number", "description": "f-number. 0 uses f/2.8. 1.4-2 shallow, 8-16 deep."},
                        "track_focus": {"type": "boolean", "description": "Keep autofocus locked on the subject. True whenever there is a subject and focus is not deep or fixed."},
                        "deep_focus": {"type": "boolean", "description": "Hold near-infinite focus so the whole stage stays sharp."},
                        "fixed_focus": {"type": "boolean", "description": "Lock one manual focus distance from setup and never re-pull."},
                        "look_at_target": {"type": "boolean", "description": "Aim the camera at the subject for the whole move. Usually true when there is a subject."},
                        "follow_subject": {"type": "boolean", "description": "Travel with a moving subject, keeping the camera offset in the subject's space. Only for explicit follow/track/lock-on language."},
                        "handheld_intensity": {"type": "number", "description": "0 locked off, 0.4 subtle, 0.8 handheld, 1.5 very shaky."},
                        "dutch_angle_deg": {"type": "number", "description": "Camera roll in degrees for canted framing. 0 for level."},
                        "film_grain": {"type": "number", "description": "0-1. 0 leaves the camera default."},
                        "vignette": {"type": "number", "description": "0-1. 0 leaves the camera default."},
                        "chromatic_aberration": {"type": "number", "description": "0-1. 0 leaves the camera default."},
                        "bloom": {"type": "number", "description": "0-2. 0 leaves the camera default."},
                        "lens_flare": {"type": "number", "description": "0-1. 0 leaves the camera default."},
                        "look": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["apply_grade", "saturation", "contrast", "gain",
                                         "white_temp_kelvin", "white_tint", "motion_blur"],
                            "description": "Colour grade. Leave apply_grade false and the neutral values unless the request asks for a look (horror, noir, bodycam, warm, bleak).",
                            "properties": {
                                "apply_grade": {"type": "boolean"},
                                "saturation": {"type": "number", "description": "1 neutral, below 1 desaturated, above 1 punchy."},
                                "contrast": {"type": "number", "description": "1 neutral."},
                                "gain": {"type": "number", "description": "1 neutral, 0.9 darker, 1.1 brighter."},
                                "white_temp_kelvin": {"type": "number", "description": "0 leaves the camera default (~6500). 3200 warm, 9000 cold."},
                                "white_tint": {"type": "number", "description": "-1 magenta to 1 green, 0 none."},
                                "motion_blur": {"type": "number", "description": "0-1, or -1 to leave the default."},
                            },
                        },
                        "style_kit_name": {"type": "string", "description": "Short name for the look applied, shown in the shot notes. Empty when no style was asked for."},
                        "time_of_day": {"type": "string", "enum": list(TIMES_OF_DAY), "description": "Re-keys the level's sun. Use unchanged unless the request names a time or weather."},
                        "fog_density": {"type": "number", "description": "-1 leaves the level fog alone. 0.02 light haze, 0.2 heavy."},
                        "god_rays": {"type": "boolean"},
                        "volumetric_fog": {"type": "boolean"},
                        "notes": {"type": "array", "items": {"type": "string"}, "description": "Short notes on assumptions made or parts of the request that could not be honoured. Empty array when everything was straightforward."},
                    },
                },
            },
        },
    }


SYSTEM_PROMPT = """You are a cinematographer laying out camera coverage inside Unreal Engine's Sequencer. You turn a plain-language request into a shot plan that a deterministic executor spawns cine cameras from.

Rules:
- Return only the plan, matching the given schema exactly. No prose, no markdown fence.
- Split the request into shots the way an editor would: a new framing, a new subject, or a cut phrase ("then", "cut to", "next") starts a new segment. A single continuous move stays one segment.
- Only name actors that appear in the scene list, copying the label exactly. If the request names something that is not there, leave target empty and say so in notes.
- With no subject the shot is anchored to the current viewport camera, which is a legitimate choice for establishing and abstract moves.
- Motivate every choice: lens and framing follow the emotional beat, not a default. Close, long lenses and shallow focus for intimacy; wide lenses and deep focus for scale and unease; low angles for power; handheld for urgency.
- Timing is part of the grammar. A held wide breathes at 8-12s; a cut inside an action beat is 1.5-3s. Do not give every shot the same length.
- Framing size answers "how small is the subject in the frame". wide and extreme_wide make it small, which is right for a person and wrong for a landscape: at extreme_wide a 500m subject lands 7.5km away. Use fit when the request is about seeing all of something (a building, a cliff, a terrain, "the whole X"); it puts the camera exactly far enough back for the subject to fill the frame at whatever focal length you chose.
- Set frame_level when the request is about the level itself rather than any actor in it ("show the whole map", "everything"), and leave target empty. Never name a sky sphere or galaxy dome as a target; they are kilometres wide and are not subjects.
- A shot's look carries forward: style_kit_name, grain, vignette, chromatic aberration, bloom, flare, the grade and the handheld feel all hold for the shots after them unless a later shot names a different style kit, which replaces the look wholesale. State the look on the shot that establishes it and leave it neutral afterwards.
- Leave a field at its neutral value when the request does not call for it. Unrequested grain, vignette, dutch angles and colour grades read as noise, not style.
- Set time_of_day, fog and god rays only when the request describes light or weather; they re-key the whole level.
- Keep notes short and only for real assumptions or things you could not do."""


def build_user_prompt(description, scene, send_scene_actors=True, max_actors=40):
    """
    Build the user turn: the request, the viewport anchor, and the nearest actors.
    `scene` is a cinedirector.scene.SceneContext (or anything with the same shape),
    kept duck-typed so this module stays importable without the editor.
    """
    prompt = "Shot request:\n" + (description or "").strip() + "\n\n"

    vx, vy, vz = scene.viewport_location
    prompt += "Viewport camera: position (%.0f, %.0f, %.0f), facing yaw %.0f, pitch %.0f.\n" % (
        vx, vy, vz, scene.viewport_rotation[1], scene.viewport_rotation[0])

    actors = list(scene.actors) if send_scene_actors else []
    if not actors:
        prompt += ("\nScene actors: none available - frame every shot from the "
                   "viewport camera and leave target empty.\n")
        return prompt

    # Nearest first: with a cap, the actors around the viewport are the ones the
    # director is most likely talking about.
    eye = scene.viewport_location

    def dist_sq(info):
        return ((info.location[0] - eye[0]) ** 2
                + (info.location[1] - eye[1]) ** 2
                + (info.location[2] - eye[2]) ** 2)

    actors.sort(key=dist_sq)
    count = min(len(actors), max(1, max_actors))

    prompt += ("\nScene actors (%d of %d, nearest the viewport first). Sizes are the "
               "bounds radius in cm; use them to judge framing distance:\n" % (count, len(actors)))

    for info in actors[:count]:
        prompt += '- "%s" at (%.0f, %.0f, %.0f), size %.0f, facing yaw %.0f\n' % (
            info.label, info.location[0], info.location[1], info.location[2],
            info.bounds_radius, info.facing[1])

    if count < len(actors):
        prompt += "(%d further actors omitted.)\n" % (len(actors) - count)

    return prompt
