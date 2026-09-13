# Copyright Roundtree. All Rights Reserved.
"""
Rule-based shot parser: plain language in, ShotPlan out.

Port of ShotGrammarParser.cpp. This is the fallback whenever no model backend is
configured or a request to one fails, and it is deterministic, so the same words
always give the same shots.

Only `resolve_target` needs the scene, and it only reads labels, so everything
here works against any object exposing `.actors` with `.label`.
"""

import re

from . import plan as cd_plan
from .plan import Segment, ShotPlan


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def contains_phrase(text, phrase):
    """True when `phrase` occurs in `text` on word boundaries. Both lowercase."""
    start = 0
    length = len(phrase)
    if not length:
        return False
    while True:
        index = text.find(phrase, start)
        if index < 0:
            return False
        start_ok = index == 0 or not text[index - 1].isalnum()
        end = index + length
        end_ok = end >= len(text) or not text[end].isalnum()
        if start_ok and end_ok:
            return True
        start = index + 1


def contains_any(text, phrases):
    return any(contains_phrase(text, p) for p in phrases)


def _collapse(text):
    return " ".join(text.split())


def _normalize_separators(text):
    return text.replace("_", " ").replace("-", " ").replace(".", " ")


def _match_number(text, pattern):
    """First numeric capture of `pattern`, or None."""
    found = re.search(pattern, text)
    if not found:
        return None
    try:
        return float(found.group(1))
    except (TypeError, ValueError):
        return None


# Phrases that mean "fit this in frame" rather than "make this small in frame".
#
# No trailing spaces: contains_phrase matches on word boundaries, so "the whole "
# would need a non-alphanumeric character after the space and never fires.
FIT_PHRASES = (
    "the whole", "whole of", "all of", "entire", "entirety",
    "fit", "in full", "full extent", "full view",
    "see all of", "show all of", "frame all", "frame the whole",
    "as much as", "top to bottom", "end to end",
)

# Phrases that mean the level itself is the subject. Multi-word wherever a bare
# noun would misfire: "level" alone would turn every eye-level shot into one of
# these.
LEVEL_PHRASES = (
    "everything", "all of it", "the whole thing",
    "terrain", "landscape", "the island", "whole island", "entire island",
    "the level", "whole level", "entire level",
    "the map", "whole map", "entire map",
    "the scene", "whole scene", "entire scene",
    "the area", "whole area", "entire area",
    "the world", "whole world", "entire world",
    "the environment", "the place", "whole place",
)


# A one-word tail of a label is not offered as a name when the word is part of
# the shot grammar itself. Without this, "orbit it from the left" picks the actor
# INTERACT_Simulation_Left as the subject, because "left" is a legal suffix of
# its label and a label match outranks the pronoun carrying the real subject over.
# The full label still matches, so an actor genuinely called "Left" is reachable.
RESERVED_LABEL_WORDS = frozenset((
    "left", "right", "front", "back", "behind", "rear", "side", "up", "down",
    "over", "under", "above", "below", "top", "bottom", "near", "far", "mid",
    "close", "closeup", "wide", "medium", "long", "full", "high", "low",
    "static", "orbit", "circle", "pan", "tilt", "zoom", "crane", "truck",
    "dolly", "push", "pull", "boom", "jib", "arc", "flyover", "aerial", "drone",
    "shot", "take", "camera", "cam", "lens", "focus", "angle", "frame",
    "light", "lights", "sun", "sky", "fog", "mist", "haze", "shadow",
    "dawn", "morning", "noon", "day", "afternoon", "sunset", "dusk", "night",
    "midnight", "overcast", "golden", "hour",
    "slow", "fast", "quick", "seconds", "second", "then", "cut", "next",
    "start", "end", "begin", "main", "default", "new", "old", "target",
))


def build_label_candidates(label):
    """
    Every spelling of an actor label a user might plausibly type, so
    "VOID_NPC_wyn943" is found by "the npc", "wyn943" or "void npc wyn943".
    Longest match wins at the call site.
    """
    candidates = []

    def add(value, min_len=3):
        value = _collapse(value)
        if len(value) >= min_len and value not in candidates:
            candidates.append(value)

    normalized = _normalize_separators(label.lower())
    add(normalized)

    # Split camel case and letter/digit boundaries into words.
    camel_chars = []
    for i, ch in enumerate(label):
        if i > 0:
            prev = label[i - 1]
            boundary = ((ch.isupper() and prev.islower())
                        or (ch.isalnum() and prev.isalnum()
                            and ch.isdigit() != prev.isdigit()))
            if boundary:
                camel_chars.append(" ")
        camel_chars.append(ch)
    camel = _normalize_separators("".join(camel_chars).lower())
    add(camel)

    # Without trailing digits, so "Knight_2" is found by "the knight".
    for existing in list(candidates):
        add(existing.rstrip("0123456789 "))

    # Word suffixes, since the trailing words are usually the noun while leading
    # words are pack prefixes like "SM" or "VOID_NPC" that would misfire.
    #
    # Suffixes are taken from BOTH spellings. The camel split drops digit words,
    # so "VOID_NPC_wyn943" gives "npc wyn" there; the normalized split keeps the
    # token whole and gives "wyn943", which is what a user actually types.
    for source, drop_digit_words in ((camel, True), (normalized, False)):
        words = [w for w in source.split(" ") if w]
        if drop_digit_words:
            words = [w for w in words if not w.isdigit()]
        if not words:
            continue
        add(" ".join(words))
        for first in range(1, len(words)):
            suffix = words[first:]
            if len(suffix) == 1 and suffix[0] in RESERVED_LABEL_WORDS:
                continue
            add(" ".join(suffix), 4 if len(suffix) == 1 else 3)

    # Single words with leading digits stripped ("4003gasmask" -> "gasmask").
    for word in normalized.split(" "):
        stripped = word.lstrip("0123456789")
        if stripped != word and stripped not in RESERVED_LABEL_WORDS:
            add(stripped, 4)

    # Condensed variants, so a label typed without separators still matches.
    for existing in list(candidates):
        add(existing.replace(" ", ""), 4)

    return candidates


# ---------------------------------------------------------------------------
# Clause splitting
# ---------------------------------------------------------------------------

PHRASE_BREAKS = (" then ", " cut to ", " next, ", " after that ", " followed by ")


def split_into_segments(description):
    """Break a description into clauses, one per intended shot."""
    work = description.lower()
    for break_phrase in PHRASE_BREAKS:
        work = work.replace(break_phrase, ";")

    clauses = []
    current = []
    for i, ch in enumerate(work):
        is_break = ch in (";", "\n", "\r")
        if ch == ".":
            # Never split inside a decimal, as in "3.5 seconds".
            prev_digit = i > 0 and work[i - 1].isdigit()
            next_digit = i + 1 < len(work) and work[i + 1].isdigit()
            is_break = not (prev_digit and next_digit)
        if is_break:
            clauses.append("".join(current))
            current = []
        else:
            current.append(ch)
    clauses.append("".join(current))

    return [c for c in clauses if len(c.strip()) >= 3]


def resolve_target(clause, scene, exclude_label=None):
    """
    Best actor label mentioned in the clause, longest candidate winning.
    Returns an ActorInfo-like object or None.
    """
    normalized = _collapse(_normalize_separators(clause))

    best = None
    best_score = 0
    for info in scene.actors:
        if exclude_label and info.label == exclude_label:
            continue
        for candidate in build_label_candidates(info.label):
            if len(candidate) > best_score and contains_phrase(normalized, candidate):
                best = info
                best_score = len(candidate)
    return best


# ---------------------------------------------------------------------------
# Vocabulary tables
# ---------------------------------------------------------------------------

LOOK_PHRASES = (
    (" looking at ", False), (" look at ", False), (" looks at ", False),
    (" keep looking at ", False), (" gazing at ", False), (" gaze at ", False),
    (" aimed at ", False), (" aiming at ", False), (" aim at ", False),
    (" facing ", False), (" pointed at ", False), (" pointing at ", False),
    (" locked on ", True), (" lock on ", True), (" lock onto ", True),
    (" watching ", True), (" tracking ", True), (" track ", True),
    (" following ", True), (" follow shot of ", True), (" follow ", True),
    (" stays on ", True), (" stay on ", True), (" keep on ", True),
)

FOCUS_ON_PHRASES = (
    " focus on ", " autofocus on ", " auto focus on ", " pull focus to ",
    " refocus on ", " re-focus on ", " keep focus on ", " stay focused on ",
)

ONE_TAKE_PHRASES = (
    "one take", "one continuous", "single take", "continuous shot",
    "continuous take", "oner", "unbroken", "no cuts", "without cutting",
    "one shot", "long take",
)


def _parse_lens(text, seg):
    """Focal length and aperture. Runs first so 'wide-angle' is not a wide shot."""
    recognized = False
    wide_angle_lens = contains_any(text, ("wide angle", "wide-angle"))

    focal = _match_number(text, r"(\d+(?:\.\d+)?)\s*mm\b")
    if focal is not None:
        seg.focal_length_mm = focal
        recognized = True
    elif wide_angle_lens:
        seg.focal_length_mm = 18.0
        recognized = True
    elif contains_phrase(text, "telephoto"):
        seg.focal_length_mm = 135.0
        recognized = True
    elif contains_any(text, ("portrait lens", "portrait")):
        seg.focal_length_mm = 85.0

    aperture = _match_number(text, r"f\s*/?\s*(\d+(?:\.\d+)?)")
    if aperture is not None:
        seg.aperture = aperture
        recognized = True
    elif contains_any(text, ("shallow focus", "shallow depth", "shallow dof",
                             "blurry background", "bokeh")):
        seg.aperture = 1.4
        recognized = True
    elif contains_any(text, ("deep focus", "deep depth", "everything in focus")):
        seg.aperture = 11.0
        recognized = True

    return recognized, wide_angle_lens


def _parse_framing(text, seg, wide_angle_lens):
    """Shot size. A 'slight close-up' is deliberately softer than a true one."""
    if contains_any(text, ("extreme close", "extreme close-up", "ecu")):
        seg.shot_size = "extreme_close_up"
    elif contains_any(text, ("slight close-up", "slight close up", "slight closeup",
                             "slightly close-up", "slightly close up", "slightly closeup",
                             "loose close-up", "loose close up", "soft close-up",
                             "soft close up", "almost close-up", "near close-up",
                             "gentle close-up")):
        seg.shot_size = "medium_close_up"
        seg.notes.append("Slight close-up -> medium close-up framing "
                         "(chest-up, not face-tight).")
    elif contains_any(text, ("medium close", "medium close-up", "medium close up", "mcu")):
        seg.shot_size = "medium_close_up"
    elif contains_any(text, ("close-up", "close up", "closeup")):
        seg.shot_size = "close_up"
    elif contains_any(text, ("slight medium", "slightly medium", "loose medium",
                             "soft medium")):
        seg.shot_size = "wide"
        seg.notes.append("Slight medium -> wide-medium framing.")
    elif contains_any(text, FIT_PHRASES):
        # "the whole X" is not the same request as "a wide of X": the first
        # wants X to fill the frame, the second wants X small inside it.
        seg.shot_size = "fit"
    elif contains_any(text, ("extreme wide", "very wide", "establishing")):
        seg.shot_size = "extreme_wide"
    elif not wide_angle_lens and contains_any(text, ("wide shot", "wide")):
        seg.shot_size = "wide"
    elif contains_any(text, ("medium shot", "medium", "mid shot", "mid-shot")):
        seg.shot_size = "medium"

    return seg.shot_size != "unspecified"


def _parse_angle(text, seg):
    if contains_any(text, ("overhead", "top-down", "top down", "bird's eye",
                           "birds eye", "directly above")):
        seg.angle = "overhead"
    elif contains_any(text, ("low angle", "low-angle", "from below", "looking up")):
        seg.angle = "low"
    elif contains_any(text, ("high angle", "high-angle", "from above", "looking down")):
        seg.angle = "high"
    else:
        return False
    return True


def _parse_view_side(text, seg):
    """
    Possessive sides are relative to the FACE forward, because void and VRM roots
    are often 90 degrees off the mesh. Plain sides are viewport-relative.
    "from the front" still means the character's face, which is what users expect.
    """
    if contains_any(text, ("its front", "his front", "her front", "their front",
                           "from its front", "from his front", "from her front",
                           "from their front", "from the front", "from front",
                           "to the front")):
        seg.view_side = "front"
        seg.actor_relative_side = True
    elif contains_any(text, ("its back", "its rear", "his back", "her back",
                             "their back", "behind it", "behind him", "behind her",
                             "behind them")):
        seg.view_side = "behind"
        seg.actor_relative_side = True
    elif contains_any(text, ("its left", "his left", "her left", "their left")):
        seg.view_side = "left"
        seg.actor_relative_side = True
    elif contains_any(text, ("its right", "his right", "her right", "their right")):
        seg.view_side = "right"
        seg.actor_relative_side = True
    elif contains_any(text, ("over the shoulder", "over-the-shoulder", "ots")):
        seg.view_side = "over_shoulder"
    elif contains_any(text, ("from behind", "behind")):
        seg.view_side = "behind"
    elif contains_any(text, ("from the left", "from left")):
        seg.view_side = "left"
    elif contains_any(text, ("from the right", "from right")):
        seg.view_side = "right"


def _parse_look_at(text, seg, scene):
    """
    "orbit around the tower looking at the knight" aims the lens at a different
    actor than the move pivots on. Track and follow words also lock autofocus.
    """
    for phrase, also_track in LOOK_PHRASES:
        index = text.find(phrase)
        if index < 0:
            continue

        look_target = resolve_target(text[index + len(phrase):], scene)
        if look_target is None:
            continue

        seg.look_at = look_target.label
        seg.look_at_target = True
        if also_track:
            seg.track_focus = True

        # The move's subject comes from the words BEFORE the phrase, not from
        # whichever label scored best across the whole clause.
        move_target = resolve_target(text[:index], scene)
        if move_target is not None:
            seg.target = move_target.label
        elif not seg.target:
            # "follow the Knight" with no earlier subject: look target is subject.
            seg.target = look_target.label

        # Ride the subject on a real follow, or when pivot and aim are the same
        # character. "orbit around A looking at B" keeps B aim-only.
        same_subject = bool(seg.target) and seg.target == look_target.label
        if also_track or same_subject:
            seg.follow_subject = True
            seg.track_focus = True
        return True
    return False


def _parse_move(text, seg, recognized_anything):
    """Zoom is checked first so 'zoom in' never reads as a dolly."""
    if contains_any(text, ("zoom in", "zoom into", "zooming in")):
        seg.move = "zoom_in"
    elif contains_any(text, ("zoom out", "zooming out")):
        seg.move = "zoom_out"
    elif contains_any(text, ("orbit", "circle around", "circle", "rotate around",
                             "revolve", "arc around", "arc")):
        ccw = contains_any(text, ("counter-clockwise", "counterclockwise",
                                  "counter clockwise", "anticlockwise",
                                  "anti-clockwise", "ccw"))
        seg.move = "orbit_ccw" if ccw else "orbit_cw"
    elif contains_any(text, ("flyover", "fly over", "fly-over", "drone shot",
                             "drone", "aerial", "fly past", "flyby", "fly by")):
        seg.move = "flyover"
    elif contains_any(text, ("dolly in", "push in", "push-in", "move in",
                             "move closer", "moves closer", "approach",
                             "push towards", "move towards", "dolly towards",
                             "dolly toward")):
        seg.move = "dolly_in"
    elif contains_any(text, ("dolly out", "pull out", "pull back", "pull-back",
                             "pull away", "move away", "moves away", "retreat",
                             "back away")):
        seg.move = "dolly_out"
    elif contains_any(text, ("crane up", "boom up", "pedestal up", "rise",
                             "rises", "ascend", "craning up")):
        seg.move = "crane_up"
    elif contains_any(text, ("crane down", "boom down", "pedestal down",
                             "descend", "lower down", "sink down")):
        seg.move = "crane_down"
    elif contains_any(text, ("truck left", "track left", "slide left",
                             "strafe left", "move left")):
        seg.move = "truck_left"
    elif contains_any(text, ("truck right", "track right", "slide right",
                             "strafe right", "move right")):
        seg.move = "truck_right"
    elif contains_any(text, ("pan left", "pans left")):
        seg.move = "pan_left"
    elif contains_any(text, ("pan right", "pans right", "pan")):
        seg.move = "pan_right"
    elif contains_any(text, ("tilt up", "tilts up")):
        seg.move = "tilt_up"
    elif contains_any(text, ("tilt down", "tilts down", "tilt")):
        seg.move = "tilt_down"
    elif contains_any(text, ("static", "locked", "locked-off", "still", "hold",
                             "stationary")):
        seg.move = "static"
    else:
        seg.move = "static"
        if recognized_anything:
            seg.notes.append("No camera move recognized - using a static shot.")
    return seg.move != "static"


def _parse_move_amount(text, seg):
    angular = seg.move in ("orbit_cw", "orbit_ccw", "pan_left", "pan_right",
                           "tilt_up", "tilt_down")
    if angular:
        degrees = _match_number(text, r"(\d+(?:\.\d+)?)\s*(?:°|deg\b|degree|degrees)")
        if degrees is not None:
            seg.move_amount = degrees
        elif contains_any(text, ("full orbit", "full circle", "all the way around", "360")):
            seg.move_amount = 360.0
        elif contains_any(text, ("half orbit", "half circle", "halfway around", "180")):
            seg.move_amount = 180.0
        elif contains_any(text, ("quarter", "90")):
            seg.move_amount = 90.0
    elif seg.move in ("zoom_in", "zoom_out"):
        # "zoom to 85mm": the mm number is the zoom target, not the start lens.
        if seg.focal_length_mm > 0.0 and contains_any(text, ("zoom to", "zoom into")):
            seg.move_amount = seg.focal_length_mm
            seg.focal_length_mm = 0.0
    else:
        # Metres or centimetres. The metres pattern needs a word boundary after
        # "m", so a lens token like "55mm" can never match it.
        metres = _match_number(
            text, r"(\d+(?:\.\d+)?)\s*(?:meters|meter|metres|metre|m)\b")
        if metres is not None:
            seg.move_amount = metres * 100.0
        else:
            cm = _match_number(
                text, r"(\d+(?:\.\d+)?)\s*(?:cm|centimeters|centimetres|units)\b")
            if cm is not None:
                seg.move_amount = cm


def _parse_focus(text, seg, scene):
    recognized = False
    mentions_rack = contains_any(text, ("rack focus", "pull focus", "focus pull"))

    if mentions_rack:
        from_index = text.find(" from ")
        to_index = text.find(" to ", max(from_index, 0))
        if from_index >= 0 and to_index > from_index:
            from_actor = resolve_target(text[from_index:to_index], scene)
            if from_actor is not None:
                seg.target = from_actor.label
            to_actor = resolve_target(text[to_index:], scene, exclude_label=seg.target)
            if to_actor is not None:
                seg.rack_focus_to = to_actor.label
        if not seg.rack_focus_to:
            seg.notes.append('Rack focus requested but could not resolve both '
                             'actors ("rack focus from <actor> to <actor>").')
        seg.track_focus = False
        seg.deep_focus = False
        seg.fixed_focus = False
        recognized = True

    elif contains_any(text, ("deep focus", "deep depth", "everything in focus",
                             "infinite focus", "no depth of field", "disable dof",
                             "no dof")):
        seg.deep_focus = True
        seg.track_focus = False
        seg.fixed_focus = False
        if seg.aperture <= 0.0:
            seg.aperture = 11.0
        recognized = True

    elif contains_any(text, ("fixed focus", "locked focus", "lock focus",
                             "no autofocus", "no auto focus", "manual focus only")):
        seg.fixed_focus = True
        seg.track_focus = False
        seg.deep_focus = False
        recognized = True

    elif contains_any(text, ("focus on", "track focus", "tracking focus",
                             "follow focus", "keep focus", "stay focused",
                             "autofocus", "auto focus", "auto-focus",
                             "pull focus to", "refocus on", "re-focus on")):
        seg.track_focus = True
        seg.deep_focus = False
        seg.fixed_focus = False
        recognized = True

        for phrase in FOCUS_ON_PHRASES:
            index = text.find(phrase)
            if index < 0:
                continue
            focus_target = resolve_target(text[index + len(phrase):], scene)
            if focus_target is not None:
                seg.look_at = focus_target.label
                if not seg.target:
                    seg.target = focus_target.label
            break

    elif seg.target and 0.0 < seg.aperture <= 2.8:
        # Shallow depth of field on a named subject keeps autofocus glued to them.
        seg.track_focus = True

    return recognized


def _apply_style_kits(text, seg):
    """
    Named looks set lens, post-process, grade and filmback defaults. Kits only
    fill empty slots, so a later explicit "85mm" is not fought by the pack.
    """
    recognized = [False]

    def fill_focal(mm):
        if seg.focal_length_mm <= 0.0:
            seg.focal_length_mm = mm

    def fill_aperture(f):
        if seg.aperture <= 0.0:
            seg.aperture = f

    def fill_zero(name, value):
        if getattr(seg, name) <= 0.0 < value:
            setattr(seg, name, value)

    def set_look(sat, contrast, gain, temp_k, tint, scene_tint):
        seg.look.apply_grade = True
        seg.look.saturation = sat
        seg.look.contrast = contrast
        seg.look.gain = gain
        seg.look.white_temp_kelvin = temp_k
        seg.look.white_tint = tint
        seg.look.scene_color_tint = scene_tint

    def scope_filmback():
        if seg.filmback_sensor_height_mm <= 0.0:
            seg.filmback_sensor_width_mm = 23.76   # ~2.39:1 Super-35 scope plate
            seg.filmback_sensor_height_mm = 9.94

    def imax_filmback():
        if seg.filmback_sensor_height_mm <= 0.0:
            seg.filmback_sensor_width_mm = 70.0
            seg.filmback_sensor_height_mm = 46.0

    def tag(name):
        if not seg.style_kit_name:
            seg.style_kit_name = name
        elif name not in seg.style_kit_name:
            seg.style_kit_name += " + " + name
        recognized[0] = True

    bodycam = contains_any(text, ("bodycam", "body cam", "body-cam", "go-pro",
                                  "gopro", "action cam", "helmet cam", "pov cam"))
    cctv = contains_any(text, ("cctv", "security cam", "surveillance",
                               "security camera", "dashcam", "dash cam"))
    found_footage = bodycam or cctv or contains_any(
        text, ("found footage", "found-footage", "camcorder", "recovered tape"))
    crt_vhs = contains_any(text, ("crt", "scanline", "scan line", "scanlines",
                                  "vhs", "old tv", "tube tv", "raster", "retro tv"))
    nolan = contains_any(text, ("nolan", "christopher nolan", "inception style",
                                "dunkirk style", "imax style", "imax look",
                                "tenet style"))
    horror = contains_any(text, ("horror", "scary", "creepy", "haunted",
                                 "nightmare", "terror"))
    action = contains_any(text, ("action style", "action movie", "action look",
                                 "blockbuster", "set piece", "explosive style",
                                 "bayhem")) or (
        contains_phrase(text, "action") and not contains_phrase(text, "action cam"))
    cinematic = contains_any(text, ("cinematic", "cinemascope", "letterbox",
                                    "anamorphic", "scope look", "film look",
                                    "movie look"))
    noir = contains_any(text, ("noir", "neo-noir", "neo noir")) and not contains_phrase(text, "neon")
    thriller = contains_phrase(text, "thriller")
    romance = contains_any(text, ("romance", "romantic", "dreamy look",
                                  "soft romantic"))
    cyberpunk = contains_any(text, ("cyberpunk", "neon noir", "blade runner"))
    doc = contains_any(text, ("documentary", "doc style", "run and gun",
                              "run-and-gun"))
    music_video = contains_any(text, ("music video", "music-video", "mv style",
                                      "pop video"))
    western = contains_any(text, ("western", "spaghetti western", "desert epic"))
    indie = contains_any(text, ("indie film", "indie look", "mumblecore", "a24"))

    if found_footage:
        fill_focal(16.0 if bodycam else 18.0)
        fill_aperture(5.6)      # deeper so the whole messy frame stays readable
        if seg.handheld_intensity <= 0.0:
            seg.handheld_intensity = 1.35 if bodycam else 0.95
        fill_zero("film_grain", 0.65)
        fill_zero("vignette", 0.75)
        fill_zero("chromatic_aberration", 2.5)
        if seg.dutch_angle_deg == 0.0 and bodycam:
            seg.dutch_angle_deg = 4.0
        set_look(0.55, 1.15, 0.92, 5200.0, 0.12, (0.85, 1.0, 0.88))
        seg.look.motion_blur = 0.55
        seg.fixed_focus = seg.fixed_focus or cctv   # security cams rarely rack
        tag("bodycam" if bodycam else ("cctv" if cctv else "found footage"))

    if crt_vhs:
        fill_focal(28.0)
        fill_zero("film_grain", 0.75)
        fill_zero("vignette", 0.55)
        fill_zero("chromatic_aberration", 3.5)
        fill_zero("bloom", 1.2)
        if seg.handheld_intensity <= 0.0:
            seg.handheld_intensity = 0.35
        set_look(0.7, 1.2, 0.95, 7000.0, 0.18, (0.9, 1.05, 0.92))
        tag("CRT/VHS")

    if nolan:
        fill_focal(40.0)
        fill_aperture(8.0)
        # f/8 already reads deep. Only force non-tracking deep focus with no named
        # subject, so "nolan + orbit around X" still pulls focus on X.
        if not seg.target and not seg.look_at and not seg.track_focus:
            seg.deep_focus = True
            seg.track_focus = False
        fill_zero("film_grain", 0.18)
        fill_zero("vignette", 0.25)
        set_look(0.78, 1.28, 0.94, 6200.0, -0.04, (0.95, 0.98, 1.05))
        imax_filmback()
        seg.look.motion_blur = 0.35
        tag("Nolan/IMAX")

    if horror and not found_footage:
        fill_focal(35.0)
        fill_aperture(1.8)
        fill_zero("film_grain", 0.55)
        fill_zero("vignette", 0.8)
        fill_zero("chromatic_aberration", 1.5)
        if seg.dutch_angle_deg == 0.0:
            seg.dutch_angle_deg = 8.0
        if seg.handheld_intensity <= 0.0:
            seg.handheld_intensity = 0.35
        set_look(0.5, 1.3, 0.88, 5600.0, 0.08, (0.92, 0.95, 1.05))
        tag("horror")

    if action and not bodycam:
        fill_focal(28.0)
        fill_aperture(2.8)
        if seg.handheld_intensity <= 0.0:
            seg.handheld_intensity = 0.7
        fill_zero("film_grain", 0.22)
        fill_zero("vignette", 0.35)
        fill_zero("lens_flare", 1.5)
        fill_zero("bloom", 1.1)
        set_look(1.08, 1.32, 0.98, 6000.0, 0.0, (1.0, 0.98, 0.95))
        seg.look.motion_blur = 0.65
        tag("action")

    if cinematic and not nolan:
        fill_focal(50.0)
        fill_aperture(2.0)
        fill_zero("film_grain", 0.2)
        fill_zero("vignette", 0.35)
        set_look(0.9, 1.15, 0.98, 6000.0, 0.0, (1.0, 1.0, 1.0))
        scope_filmback()
        tag("cinematic")

    if noir:
        fill_focal(50.0)
        fill_aperture(2.8)
        fill_zero("film_grain", 0.45)
        fill_zero("vignette", 0.85)
        set_look(0.15, 1.4, 0.9, 6500.0, 0.0, (0.95, 0.95, 1.0))
        tag("noir")

    if thriller and not horror:
        fill_focal(40.0)
        fill_aperture(2.4)
        fill_zero("vignette", 0.55)
        fill_zero("film_grain", 0.28)
        set_look(0.72, 1.25, 0.94, 5800.0, 0.05, (0.95, 0.97, 1.05))
        tag("thriller")

    if romance:
        fill_focal(65.0)
        fill_aperture(1.6)
        fill_zero("bloom", 2.0)
        fill_zero("vignette", 0.3)
        fill_zero("film_grain", 0.12)
        set_look(1.12, 1.05, 1.04, 5600.0, -0.05, (1.05, 0.98, 0.95))
        tag("romance")

    if cyberpunk:
        fill_focal(35.0)
        fill_aperture(1.8)
        fill_zero("bloom", 2.5)
        fill_zero("chromatic_aberration", 2.0)
        fill_zero("vignette", 0.5)
        fill_zero("lens_flare", 1.2)
        set_look(1.35, 1.3, 0.95, 7200.0, 0.1, (1.05, 0.9, 1.15))
        if seg.time_of_day == "unchanged":
            seg.time_of_day = "night"
        tag("cyberpunk")

    if doc and not found_footage:
        fill_focal(35.0)
        fill_aperture(4.0)
        if seg.handheld_intensity <= 0.0:
            seg.handheld_intensity = 0.55
        fill_zero("film_grain", 0.25)
        set_look(0.95, 1.05, 1.0, 5600.0, 0.0, (1.0, 1.0, 1.0))
        tag("documentary")

    if music_video:
        fill_focal(28.0)
        fill_zero("bloom", 2.0)
        fill_zero("chromatic_aberration", 2.5)
        fill_zero("film_grain", 0.3)
        if seg.handheld_intensity <= 0.0:
            seg.handheld_intensity = 0.45
        set_look(1.3, 1.2, 1.02, 6500.0, 0.0, (1.0, 1.0, 1.0))
        tag("music video")

    if western:
        fill_focal(35.0)
        fill_aperture(5.6)
        fill_zero("film_grain", 0.35)
        fill_zero("vignette", 0.4)
        set_look(0.95, 1.18, 1.02, 4800.0, -0.08, (1.08, 0.98, 0.85))
        if seg.time_of_day == "unchanged":
            seg.time_of_day = "golden_hour"
        scope_filmback()
        tag("western")

    if indie:
        fill_focal(40.0)
        fill_aperture(2.0)
        fill_zero("film_grain", 0.35)
        fill_zero("vignette", 0.4)
        set_look(0.85, 1.1, 0.97, 5400.0, 0.04, (1.02, 0.98, 0.95))
        tag("indie")

    return recognized[0]


def _parse_effects(text, seg):
    recognized = False

    if contains_any(text, ("handheld", "hand-held", "hand held", "shaky",
                           "shaking", "unsteady", "documentary style")):
        subtle_words = ("slightly", "slight", "subtle", "subtly", "a little", "gentle")
        heavy_words = ("very", "heavy", "heavily", "violent", "violently", "extreme")
        if contains_any(text, subtle_words):
            seg.handheld_intensity = 0.4
        elif contains_any(text, heavy_words):
            seg.handheld_intensity = 1.5
        elif seg.handheld_intensity <= 0.0:
            seg.handheld_intensity = 0.8
        else:
            # A style kit already set shake; plain "handheld" raises the floor.
            seg.handheld_intensity = max(seg.handheld_intensity, 0.8)
        recognized = True

    if contains_any(text, ("dutch", "canted", "dutch angle", "canted angle")):
        if contains_any(text, ("slight", "slightly", "subtle", "gentle")):
            seg.dutch_angle_deg = 6.0
        elif contains_any(text, ("heavy", "extreme", "hard", "strong")):
            seg.dutch_angle_deg = 22.0
        else:
            seg.dutch_angle_deg = 12.0
        recognized = True

    # One heaviness reading per clause scales every effect mentioned in it.
    subtle = contains_any(text, ("slight", "slightly", "subtle", "subtly",
                                 "a little", "light", "gentle"))
    heavy = contains_any(text, ("heavy", "heavily", "strong", "intense",
                                "extreme", "very"))

    def intensity(subtle_value, normal, heavy_value):
        return heavy_value if heavy else (subtle_value if subtle else normal)

    def bump(name, value):
        # Explicit effect words win over, or raise, style-kit defaults.
        setattr(seg, name, max(getattr(seg, name), value))

    if contains_any(text, ("film grain", "grainy", "grain")):
        bump("film_grain", intensity(0.15, 0.4, 0.8))
        recognized = True
    if contains_any(text, ("vignette", "vignetting")):
        bump("vignette", intensity(0.3, 0.6, 0.9))
        recognized = True
    if contains_any(text, ("chromatic aberration", "chromatic", "color fringing",
                           "fringing")):
        bump("chromatic_aberration", intensity(1.0, 2.0, 4.0))
        recognized = True
    if contains_any(text, ("bloom", "glow", "glowing")):
        bump("bloom", intensity(1.0, 1.5, 3.0))
        recognized = True
    if contains_any(text, ("lens flare", "lens flares", "flare", "flares")):
        bump("lens_flare", intensity(0.5, 2.0, 4.0))
        recognized = True

    return recognized


def _parse_lighting(text, seg):
    """Time of day, fog and atmosphere. Most specific phrases first."""
    recognized = False

    if contains_any(text, ("midnight", "dead of night")):
        seg.time_of_day = "midnight"
    elif contains_any(text, ("night", "nighttime", "night-time", "at night",
                             "moonlight", "moonlit", "night falls")):
        seg.time_of_day = "night"
    elif contains_any(text, ("dawn", "sunrise", "daybreak", "first light")):
        seg.time_of_day = "dawn"
    elif contains_any(text, ("golden hour", "magic hour", "golden light")):
        seg.time_of_day = "golden_hour"
    elif contains_any(text, ("sunset", "setting sun", "sun sets", "sundown")):
        seg.time_of_day = "sunset"
    elif contains_any(text, ("dusk", "twilight", "evening")):
        seg.time_of_day = "dusk"
    elif contains_any(text, ("noon", "midday", "broad daylight", "high sun")):
        seg.time_of_day = "noon"
    elif contains_phrase(text, "afternoon"):
        seg.time_of_day = "afternoon"
    elif contains_phrase(text, "morning"):
        seg.time_of_day = "morning"
    elif contains_any(text, ("overcast", "cloudy", "gloomy", "grey sky",
                             "gray sky", "grey skies", "gray skies")):
        seg.time_of_day = "overcast"

    if seg.time_of_day != "unchanged":
        recognized = True

    if contains_any(text, ("no fog", "fog lifts", "fog clears", "clear air")):
        seg.fog_density = 0.002
        recognized = True
    elif contains_any(text, ("fog", "foggy", "mist", "misty", "haze", "hazy")):
        thick = contains_any(text, ("thick", "dense", "heavy", "soupy"))
        thin = contains_any(text, ("light", "slight", "thin", "wispy", "faint",
                                   "gentle", "a bit of"))
        # Mist and haze without the word "fog" read as the lighter end.
        mist_only = not contains_any(text, ("fog", "foggy"))
        seg.fog_density = 0.15 if thick else (0.03 if (thin or mist_only) else 0.06)
        recognized = True

    if contains_any(text, ("god rays", "god-rays", "godrays", "light shafts",
                           "light shaft", "sunbeams", "sun beams", "crepuscular")):
        seg.god_rays = True
        recognized = True
    if contains_phrase(text, "volumetric"):
        seg.volumetric_fog = True
        recognized = True

    return recognized


def _parse_timing(text, seg):
    recognized = False
    explicit = False

    seconds = _match_number(
        text, r"(\d+(?:\.\d+)?)\s*(?:s\b|sec\b|secs\b|second|seconds)")
    if seconds is not None and seconds > 0.0:
        seg.duration_seconds = seconds
        explicit = True
        recognized = True
    else:
        seg.duration_seconds = {
            "static": 3.0,
            "orbit_cw": 6.0, "orbit_ccw": 6.0,
            "flyover": 8.0,
            "pan_left": 3.0, "pan_right": 3.0,
            "tilt_up": 3.0, "tilt_down": 3.0,
            "zoom_in": 3.0, "zoom_out": 3.0,
        }.get(seg.move, 4.0)

    if not explicit:
        if contains_any(text, ("slow", "slowly", "gradual", "gradually", "creep",
                               "creeping")):
            seg.duration_seconds *= 1.6
        elif contains_any(text, ("fast", "quick", "quickly", "rapid", "rapidly",
                                 "snap", "whip")):
            seg.duration_seconds *= 0.6

    if contains_any(text, ("constant speed", "linear", "no easing")):
        seg.easing = "linear"

    return recognized


def parse_segment(clause, scene):
    """Interpret one clause. Returns a Segment, or None when nothing was found."""
    text = clause.strip()
    seg = Segment()
    seg.raw_text = text

    recognized = False

    target = resolve_target(text, scene)
    if target is not None:
        seg.target = target.label
        recognized = True
    elif contains_any(text.lower(), LEVEL_PHRASES):
        # Only when nothing else matched: an actor actually called Landscape2 is
        # a better subject than the union of everything in the level.
        seg.frame_level = True
        seg.shot_size = "fit"
        recognized = True

    lens_recognized, wide_angle_lens = _parse_lens(text, seg)
    recognized |= lens_recognized
    recognized |= _parse_framing(text, seg, wide_angle_lens)

    # "wide shot of everything" is a request to see the level, not to make the
    # level small inside the frame. Only a genuinely tight framing overrides fit.
    if seg.frame_level and seg.shot_size in ("unspecified", "wide", "extreme_wide"):
        seg.shot_size = "fit"
    recognized |= _parse_angle(text, seg)
    _parse_view_side(text, seg)
    recognized |= _parse_look_at(text, seg, scene)
    recognized |= _parse_move(text, seg, recognized)
    _parse_move_amount(text, seg)
    recognized |= _parse_focus(text, seg, scene)
    recognized |= _apply_style_kits(text, seg)
    recognized |= _parse_effects(text, seg)
    recognized |= _parse_lighting(text, seg)
    recognized |= _parse_timing(text, seg)

    # Autofocus default: a named subject with no explicit focus mode tracks.
    if seg.target and not seg.deep_focus and not seg.fixed_focus and not seg.rack_focus_to:
        seg.track_focus = True

    if not seg.target:
        seg.look_at_target = False
        if seg.move in ("orbit_cw", "orbit_ccw"):
            seg.notes.append("Orbit without a recognizable target - orbiting the "
                             "point in front of the viewport camera.")

    if not recognized and not text:
        return None
    return seg


def _apply_pronouns(plan):
    """
    "it" and "them" refer back to the previous clause's subject, as in "orbit the
    mask, then push in on it". A post-pass, because clauses parse in isolation.
    """
    prev_target = ""
    for seg in plan.segments:
        text = seg.raw_text
        has_pronoun = contains_any(text, ("it", "them", "him", "her", "they"))

        if prev_target and has_pronoun:
            aim_phrases = ("looking at it", "look at it", "aimed at it", "aim at it",
                           "watching it", "tracking it", "track it", "following it",
                           "follow it", "looking at them", "watching them",
                           "track them", "follow them")
            if not seg.look_at and contains_any(text, aim_phrases):
                seg.look_at = prev_target
                if contains_any(text, ("tracking it", "track it", "following it",
                                       "follow it", "watching it", "track them",
                                       "follow them", "watching them")):
                    seg.track_focus = True

            # "orbit around it looking at X": the whole-clause match made X both
            # pivot and aim, but the pronoun says the pivot is the previous subject.
            target_is_just_look_at = bool(seg.look_at) and seg.target == seg.look_at

            if not seg.target or target_is_just_look_at:
                seg.target = prev_target
                seg.look_at_target = True
                orbit_note = ("Orbit without a recognizable target - orbiting the "
                              "point in front of the viewport camera.")
                if orbit_note in seg.notes:
                    seg.notes.remove(orbit_note)
                seg.notes.append('"it" = \'%s\'' % prev_target)

        if seg.target:
            prev_target = seg.target


class GrammarError(Exception):
    """Raised when a description yields no shots."""


def build_shot_plan(description, scene):
    """Parse a whole description into a ShotPlan. Raises GrammarError if empty."""
    if not description or not description.strip():
        raise GrammarError('Type a shot description first - for example '
                           '"slow close-up orbit around the Knight, shallow focus".')

    clauses = split_into_segments(description)
    if not clauses:
        raise GrammarError("Could not find anything to interpret in that description.")

    plan = ShotPlan()
    for clause in clauses:
        seg = parse_segment(clause, scene)
        if seg is not None:
            plan.segments.append(seg)

    if not plan.segments:
        raise GrammarError("No shots recognized. Try move words like orbit, push in, "
                           "pull back, crane up, pan, flyover.")

    _apply_pronouns(plan)

    inherited = cd_plan.apply_look_continuity(plan)
    if inherited:
        first = plan.segments[inherited[0]]
        kit = first.style_kit_name or "look"
        first.notes.append(
            "Carried the %s over from the previous shot. Name another style to "
            "change it." % kit)

    lowered = description.lower()
    if contains_any(lowered, ONE_TAKE_PHRASES):
        plan.one_continuous_shot = True

    return plan


PROVIDER_NAME = "Rule-Based Parser (Built-in)"
