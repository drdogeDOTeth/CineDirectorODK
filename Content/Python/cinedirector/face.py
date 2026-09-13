# Copyright Roundtree. All Rights Reserved.
"""
Face analysis: a mesh's morph targets mapped onto canonical slots.

Port of CineFaceTypes.h and CineFaceAnalyzer.cpp. Lipsync and emotions are
authored once against slots, so they work on any face whose blendshapes can be
recognised. Recognised families: ARKit's 52, Oculus visemes, VRM/MMD, Reallusion
/ Character Creator, MetaHuman rig-logic controls, and the Otherside M2 phoneme
set found on the ODK's own avatar faces.
"""

import unreal

# ---------------------------------------------------------------------------
# Canonical slots
# ---------------------------------------------------------------------------

JAW_OPEN = "JawOpen"
MOUTH_CLOSE = "MouthClose"            # lips pressed shut over the jaw (M/B/P)
MOUTH_WIDE = "MouthWide"              # stretched, EE/IH shapes and a smile's width
MOUTH_PUCKER = "MouthPucker"          # rounded OO/UW
MOUTH_FUNNEL = "MouthFunnel"          # open-rounded OH
MOUTH_SMILE = "MouthSmile"
MOUTH_FROWN = "MouthFrown"
MOUTH_PRESS = "MouthPress"
MOUTH_UPPER_UP = "MouthUpperUp"       # upper-teeth reveal on open and wide vowels
MOUTH_LOWER_DOWN = "MouthLowerDown"   # lower-teeth reveal
VISEME_FV = "VisemeFV"                # labiodental fricatives (F/V)
VISEME_L = "VisemeL"                  # tongue-up alveolar (L)
VISEME_TH = "VisemeTH"                # dental fricative (TH)
VISEME_CH = "VisemeCH"                # affricates (CH/J/SH)
NOSE_SNEER = "NoseSneer"
BROW_UP = "BrowUp"
BROW_DOWN = "BrowDown"                # knit / anger
BROW_SAD = "BrowSad"                  # inner brows up (worry, sadness)
EYE_BLINK = "EyeBlink"
EYE_WIDE = "EyeWide"
EYE_SQUINT = "EyeSquint"
EYE_LOOK_LEFT = "EyeLookLeft"
EYE_LOOK_RIGHT = "EyeLookRight"
EYE_LOOK_UP = "EyeLookUp"
EYE_LOOK_DOWN = "EyeLookDown"
EXPR_HAPPY = "ExprHappy"
EXPR_ANGRY = "ExprAngry"
EXPR_SAD = "ExprSad"
EXPR_SURPRISED = "ExprSurprised"

SLOTS = (
    JAW_OPEN, MOUTH_CLOSE, MOUTH_WIDE, MOUTH_PUCKER, MOUTH_FUNNEL,
    MOUTH_SMILE, MOUTH_FROWN, MOUTH_PRESS, MOUTH_UPPER_UP, MOUTH_LOWER_DOWN,
    VISEME_FV, VISEME_L, VISEME_TH, VISEME_CH, NOSE_SNEER,
    BROW_UP, BROW_DOWN, BROW_SAD, EYE_BLINK, EYE_WIDE, EYE_SQUINT,
    EYE_LOOK_LEFT, EYE_LOOK_RIGHT, EYE_LOOK_UP, EYE_LOOK_DOWN,
    EXPR_HAPPY, EXPR_ANGRY, EXPR_SAD, EXPR_SURPRISED,
)

# The mouth slots that are mutually exclusive on a vowel-per-frame rig. When two
# blendshape families both reach these, one of them has to be dropped.
EXCLUSIVE_MOUTH_SLOTS = frozenset((JAW_OPEN, MOUTH_WIDE, MOUTH_PUCKER,
                                   MOUTH_FUNNEL, MOUTH_CLOSE))


def normalize(name):
    """Lowercase with every non-alphanumeric stripped, so Mouth_Smile_L is mouthsmilel."""
    return "".join(c.lower() for c in str(name) if c.isalnum())


# ---------------------------------------------------------------------------
# Exact tables, keyed by normalized name
# ---------------------------------------------------------------------------

def _build_exact():
    table = {}

    def add(name, slot, scale=1.0, family=""):
        table[normalize(name)] = (slot, scale, family)

    # -- ARKit -------------------------------------------------------------
    add("jawOpen", JAW_OPEN, family="arkit")
    add("mouthClose", MOUTH_CLOSE, family="arkit")
    add("mouthPucker", MOUTH_PUCKER, family="arkit")
    add("mouthFunnel", MOUTH_FUNNEL, family="arkit")
    add("mouthSmileLeft", MOUTH_SMILE, family="arkit")
    add("mouthSmileRight", MOUTH_SMILE, family="arkit")
    add("mouthFrownLeft", MOUTH_FROWN, family="arkit")
    add("mouthFrownRight", MOUTH_FROWN, family="arkit")
    add("mouthPressLeft", MOUTH_PRESS, family="arkit")
    add("mouthPressRight", MOUTH_PRESS, family="arkit")
    add("mouthStretchLeft", MOUTH_WIDE, family="arkit")
    add("mouthStretchRight", MOUTH_WIDE, family="arkit")
    add("mouthUpperUpLeft", MOUTH_UPPER_UP, family="arkit")
    add("mouthUpperUpRight", MOUTH_UPPER_UP, family="arkit")
    add("mouthLowerDownLeft", MOUTH_LOWER_DOWN, family="arkit")
    add("mouthLowerDownRight", MOUTH_LOWER_DOWN, family="arkit")
    add("noseSneerLeft", NOSE_SNEER, family="arkit")
    add("noseSneerRight", NOSE_SNEER, family="arkit")
    add("browInnerUp", BROW_SAD, family="arkit")
    add("browOuterUpLeft", BROW_UP, family="arkit")
    add("browOuterUpRight", BROW_UP, family="arkit")
    add("browDownLeft", BROW_DOWN, family="arkit")
    add("browDownRight", BROW_DOWN, family="arkit")
    add("eyeBlinkLeft", EYE_BLINK, family="arkit")
    add("eyeBlinkRight", EYE_BLINK, family="arkit")
    add("eyeWideLeft", EYE_WIDE, family="arkit")
    add("eyeWideRight", EYE_WIDE, family="arkit")
    add("eyeSquintLeft", EYE_SQUINT, family="arkit")
    add("eyeSquintRight", EYE_SQUINT, family="arkit")
    # ARKit gaze. A left eye turning "in" is the pair looking right.
    add("eyeLookInLeft", EYE_LOOK_RIGHT, family="arkit")
    add("eyeLookInRight", EYE_LOOK_LEFT, family="arkit")
    add("eyeLookOutLeft", EYE_LOOK_LEFT, family="arkit")
    add("eyeLookOutRight", EYE_LOOK_RIGHT, family="arkit")
    add("eyeLookUpLeft", EYE_LOOK_UP, family="arkit")
    add("eyeLookUpRight", EYE_LOOK_UP, family="arkit")
    add("eyeLookDownLeft", EYE_LOOK_DOWN, family="arkit")
    add("eyeLookDownRight", EYE_LOOK_DOWN, family="arkit")

    # -- Oculus visemes ----------------------------------------------------
    add("viseme_aa", JAW_OPEN, family="oculus")
    add("viseme_E", MOUTH_WIDE, family="oculus")
    add("viseme_ih", MOUTH_WIDE, 0.7, family="oculus")
    add("viseme_oh", MOUTH_FUNNEL, family="oculus")
    add("viseme_ou", MOUTH_PUCKER, family="oculus")
    add("viseme_PP", MOUTH_CLOSE, family="oculus")
    add("viseme_SS", MOUTH_WIDE, 0.5, family="oculus")
    add("viseme_FF", VISEME_FV, family="oculus")
    add("viseme_TH", VISEME_TH, family="oculus")
    add("viseme_DD", VISEME_L, family="oculus")
    add("viseme_nn", VISEME_L, 0.85, family="oculus")
    add("viseme_CH", VISEME_CH, family="oculus")

    # -- VRM / MMD: A-I-U-E-O vowels (exclusive), full-face emotions, gaze --
    add("A", JAW_OPEN, family="vowel")
    add("I", MOUTH_WIDE, family="vowel")
    add("U", MOUTH_PUCKER, family="vowel")
    add("E", MOUTH_WIDE, 0.6, family="vowel")
    add("O", MOUTH_FUNNEL, family="vowel")
    add("Joy", EXPR_HAPPY)
    add("Fun", EXPR_HAPPY, 0.85)
    add("Happy", EXPR_HAPPY)
    add("Angry", EXPR_ANGRY)
    add("Anger", EXPR_ANGRY)
    add("Sorrow", EXPR_SAD)
    add("Sad", EXPR_SAD)
    add("Surprised", EXPR_SURPRISED)
    add("Surprise", EXPR_SURPRISED)
    add("Smile", MOUTH_SMILE)
    add("Blink", EYE_BLINK)
    add("Blink_L", EYE_BLINK)
    add("Blink_R", EYE_BLINK)
    add("BlinkLeft", EYE_BLINK)
    add("BlinkRight", EYE_BLINK)
    add("Eye_Close", EYE_BLINK)
    add("EyeClose", EYE_BLINK)
    add("LookLeft", EYE_LOOK_LEFT)
    add("LookRight", EYE_LOOK_RIGHT)
    add("LookUp", EYE_LOOK_UP)
    add("LookDown", EYE_LOOK_DOWN)
    add("Look_Left", EYE_LOOK_LEFT)
    add("Look_Right", EYE_LOOK_RIGHT)
    add("Look_Up", EYE_LOOK_UP)
    add("Look_Down", EYE_LOOK_DOWN)

    # -- Reallusion / Character Creator ------------------------------------
    add("V_Open", JAW_OPEN, family="reallusion")
    add("V_Wide", MOUTH_WIDE, family="reallusion")
    add("V_Tight_O", MOUTH_PUCKER, family="reallusion")
    add("V_Explosive", MOUTH_CLOSE, family="reallusion")
    add("V_Lip_Open", MOUTH_FUNNEL, 0.7, family="reallusion")
    add("V_Dental_Lip", VISEME_FV, family="reallusion")
    add("V_Tongue_up", VISEME_L, family="reallusion")
    add("V_Tongue_Raise", VISEME_L, family="reallusion")
    add("V_Affricate", VISEME_CH, family="reallusion")

    # -- Otherside M2: a phoneme set on the ODK's own avatar faces ----------
    # These sit alongside a full ARKit set on the same mesh, so they are tagged
    # as their own family and dropped from the mouth when ARKit is also present.
    add("aaa_M", JAW_OPEN, family="phoneme")
    add("ahh_M", JAW_OPEN, 0.85, family="phoneme")
    add("eh_M", MOUTH_WIDE, 0.6, family="phoneme")
    add("iee_M", MOUTH_WIDE, family="phoneme")
    add("uuu_M", MOUTH_PUCKER, family="phoneme")
    add("ohh_M", MOUTH_FUNNEL, family="phoneme")
    add("www_M", MOUTH_PUCKER, 0.8, family="phoneme")
    add("schwa_M", JAW_OPEN, 0.45, family="phoneme")
    add("mbp_M", MOUTH_CLOSE, family="phoneme")
    add("fff_M", VISEME_FV, family="phoneme")
    add("tth_M", VISEME_TH, family="phoneme")
    add("ssh_M", VISEME_CH, family="phoneme")
    add("sss_M", MOUTH_WIDE, 0.5, family="phoneme")
    add("lntd_M", VISEME_L, family="phoneme")
    add("rrr_M", MOUTH_PUCKER, 0.5, family="phoneme")
    add("gk_M", JAW_OPEN, 0.4, family="phoneme")
    # M2 full-face emotions and micro-shapes: these do not clash with ARKit.
    add("happy_M", EXPR_HAPPY)
    add("angry_M", EXPR_ANGRY)
    add("sad_M", EXPR_SAD)
    add("surprise_M", EXPR_SURPRISED)
    add("mouth_open_M", JAW_OPEN, family="phoneme")
    add("mouth_wide_M", MOUTH_WIDE, family="phoneme")
    add("mouth_narrow_M", MOUTH_PUCKER, 0.7, family="phoneme")
    add("mouth_close_M", MOUTH_CLOSE, family="phoneme")
    add("blink_L", EYE_BLINK)
    add("blink_R", EYE_BLINK)
    add("squint_L", EYE_SQUINT)
    add("squint_R", EYE_SQUINT)

    return table


EXACT_TABLE = _build_exact()

# Fuzzy fallbacks, tested in order. First hit wins for a given morph.
FUZZY_TABLE = (
    # Gaze before the generic eye patterns, so LookLeft does not become a blink.
    ("lookleft", EYE_LOOK_LEFT, 1.0),
    ("lookright", EYE_LOOK_RIGHT, 1.0),
    ("lookup", EYE_LOOK_UP, 1.0),
    ("lookdown", EYE_LOOK_DOWN, 1.0),
    ("eyelookleft", EYE_LOOK_LEFT, 1.0),
    ("eyelookright", EYE_LOOK_RIGHT, 1.0),
    ("eyelookup", EYE_LOOK_UP, 1.0),
    ("eyelookdown", EYE_LOOK_DOWN, 1.0),
    ("gazeleft", EYE_LOOK_LEFT, 1.0),
    ("gazeright", EYE_LOOK_RIGHT, 1.0),
    ("visemeth", VISEME_TH, 1.0),
    ("dentalfricative", VISEME_TH, 1.0),
    ("dentallip", VISEME_FV, 1.0),
    ("labiodental", VISEME_FV, 1.0),
    ("tongueup", VISEME_L, 1.0),
    ("tongueraise", VISEME_L, 1.0),
    ("affricate", VISEME_CH, 1.0),
    ("jawopen", JAW_OPEN, 1.0),
    ("mouthopen", JAW_OPEN, 1.0),
    ("jawdrop", JAW_OPEN, 1.0),
    ("openmouth", JAW_OPEN, 1.0),
    ("mouthah", JAW_OPEN, 1.0),
    ("blink", EYE_BLINK, 1.0),
    ("eyesclosed", EYE_BLINK, 1.0),
    ("eyeclose", EYE_BLINK, 1.0),
    ("eyewide", EYE_WIDE, 1.0),
    ("squint", EYE_SQUINT, 1.0),
    ("smile", MOUTH_SMILE, 1.0),
    ("happy", EXPR_HAPPY, 0.9),
    ("joy", EXPR_HAPPY, 1.0),
    ("angry", EXPR_ANGRY, 1.0),
    ("anger", EXPR_ANGRY, 1.0),
    ("sorrow", EXPR_SAD, 1.0),
    ("frown", MOUTH_FROWN, 1.0),
    ("sadmouth", MOUTH_FROWN, 1.0),
    ("surprised", EXPR_SURPRISED, 1.0),
    ("surprise", EXPR_SURPRISED, 1.0),
    ("pucker", MOUTH_PUCKER, 1.0),
    ("kiss", MOUTH_PUCKER, 1.0),
    ("funnel", MOUTH_FUNNEL, 1.0),
    ("mouthwide", MOUTH_WIDE, 1.0),
    ("stretch", MOUTH_WIDE, 0.8),
    ("upperlipraise", MOUTH_UPPER_UP, 1.0),
    ("mouthupperup", MOUTH_UPPER_UP, 1.0),
    ("upperlipup", MOUTH_UPPER_UP, 1.0),
    ("lowerlipdepress", MOUTH_LOWER_DOWN, 1.0),
    ("mouthlowerdown", MOUTH_LOWER_DOWN, 1.0),
    ("lowerlipdown", MOUTH_LOWER_DOWN, 1.0),
    ("mouthpress", MOUTH_PRESS, 1.0),
    ("lipspressed", MOUTH_PRESS, 1.0),
    ("mouthclose", MOUTH_CLOSE, 1.0),
    ("sneer", NOSE_SNEER, 1.0),
    ("snarl", NOSE_SNEER, 1.0),
    ("browsup", BROW_UP, 1.0),
    ("browup", BROW_UP, 1.0),
    ("browraise", BROW_UP, 1.0),
    ("browsdown", BROW_DOWN, 1.0),
    ("browdown", BROW_DOWN, 1.0),
    ("browfurrow", BROW_DOWN, 1.0),
    ("angrybrow", BROW_DOWN, 1.0),
    ("browinnerup", BROW_SAD, 1.0),
    ("worried", BROW_SAD, 0.8),
)

# Normalized ARKit names that, in quantity, say a mesh carries the full set.
ARKIT_MARKERS = frozenset(normalize(n) for n in (
    "jawOpen", "mouthPucker", "mouthFunnel", "mouthClose",
    "mouthSmileLeft", "mouthSmileRight", "mouthStretchLeft", "mouthStretchRight",
    "mouthUpperUpLeft", "mouthUpperUpRight", "mouthLowerDownLeft", "mouthLowerDownRight",
    "mouthFrownLeft", "mouthFrownRight", "mouthPressLeft", "mouthPressRight",
    "browInnerUp", "browDownLeft", "browDownRight",
    "browOuterUpLeft", "browOuterUpRight",
    "eyeBlinkLeft", "eyeBlinkRight", "eyeWideLeft", "eyeWideRight",
))

METAHUMAN_BINDINGS = (
    (JAW_OPEN, ("CTRL_expressions_jawOpen",), 1.0),
    (MOUTH_CLOSE, ("CTRL_expressions_mouthLipsTogetherUL",
                   "CTRL_expressions_mouthLipsTogetherUR",
                   "CTRL_expressions_mouthLipsTogetherDL",
                   "CTRL_expressions_mouthLipsTogetherDR"), 1.0),
    (MOUTH_WIDE, ("CTRL_expressions_mouthStretchL",
                  "CTRL_expressions_mouthStretchR"), 0.8),
    (MOUTH_PUCKER, ("CTRL_expressions_mouthLipsPurseUL",
                    "CTRL_expressions_mouthLipsPurseUR",
                    "CTRL_expressions_mouthLipsPurseDL",
                    "CTRL_expressions_mouthLipsPurseDR"), 1.0),
    (MOUTH_FUNNEL, ("CTRL_expressions_mouthLipsFunnelUL",
                    "CTRL_expressions_mouthLipsFunnelUR",
                    "CTRL_expressions_mouthLipsFunnelDL",
                    "CTRL_expressions_mouthLipsFunnelDR"), 1.0),
    (MOUTH_SMILE, ("CTRL_expressions_mouthCornerPullL",
                   "CTRL_expressions_mouthCornerPullR"), 1.0),
    (MOUTH_FROWN, ("CTRL_expressions_mouthCornerDepressL",
                   "CTRL_expressions_mouthCornerDepressR"), 1.0),
    (MOUTH_PRESS, ("CTRL_expressions_mouthPressUL",
                   "CTRL_expressions_mouthPressUR",
                   "CTRL_expressions_mouthPressDL",
                   "CTRL_expressions_mouthPressDR"), 1.0),
    (MOUTH_UPPER_UP, ("CTRL_expressions_mouthUpperLipRaiseL",
                      "CTRL_expressions_mouthUpperLipRaiseR"), 1.0),
    (MOUTH_LOWER_DOWN, ("CTRL_expressions_mouthLowerLipDepressL",
                        "CTRL_expressions_mouthLowerLipDepressR"), 1.0),
    (NOSE_SNEER, ("CTRL_expressions_noseWrinkleL",
                  "CTRL_expressions_noseWrinkleR"), 1.0),
    (BROW_UP, ("CTRL_expressions_browRaiseOuterL",
               "CTRL_expressions_browRaiseOuterR"), 1.0),
    (BROW_DOWN, ("CTRL_expressions_browDownL",
                 "CTRL_expressions_browDownR"), 1.0),
    (BROW_SAD, ("CTRL_expressions_browRaiseInL",
                "CTRL_expressions_browRaiseInR"), 1.0),
    (EYE_BLINK, ("CTRL_expressions_eyeBlinkL",
                 "CTRL_expressions_eyeBlinkR"), 1.0),
    (EYE_WIDE, ("CTRL_expressions_eyeWidenL",
                "CTRL_expressions_eyeWidenR"), 1.0),
    (EYE_SQUINT, ("CTRL_expressions_eyeSquintInnerL",
                  "CTRL_expressions_eyeSquintInnerR"), 1.0),
    (EYE_LOOK_LEFT, ("CTRL_eyes_lookLeft",), 1.0),
    (EYE_LOOK_RIGHT, ("CTRL_eyes_lookRight",), 1.0),
    (EYE_LOOK_UP, ("CTRL_eyes_lookUp",), 1.0),
    (EYE_LOOK_DOWN, ("CTRL_eyes_lookDown",), 1.0),
)


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------

class CurveTarget(object):
    """One curve driven by a slot. A slot may drive several (left/right pairs)."""

    __slots__ = ("name", "scale", "family")

    def __init__(self, name, scale=1.0, family=""):
        self.name = str(name)
        self.scale = scale
        self.family = family

    def __repr__(self):
        return "%s*%.2f" % (self.name, self.scale)


class FaceProfile(object):
    """How one mesh's face maps onto the canonical slots."""

    def __init__(self):
        self.mesh_name = ""
        self.metahuman = False
        # Mutually-exclusive vowel morphs: lipsync picks one dominant mouth shape
        # per frame instead of layering ARKit-style.
        self.exclusive_visemes = False
        # A rich layered set (ARKit-style jaw/pucker/smile/brows).
        self.layered_blendshapes = False
        # The ARKit mouth path was selected on a mesh that had two candidates.
        self.layered_arkit_mouth = False
        self.slots = {slot: [] for slot in SLOTS}
        self.notes = []
        self.unrecognized = 0
        self.morph_count = 0

    def has(self, slot):
        return bool(self.slots.get(slot))

    @property
    def mapped_slot_count(self):
        return sum(1 for slot in SLOTS if self.slots.get(slot))

    def targets(self, slot):
        return self.slots.get(slot, [])

    def describe(self):
        lines = ["%s: %d of %d slots mapped from %d morph targets."
                 % (self.mesh_name, self.mapped_slot_count, len(SLOTS),
                    self.morph_count)]
        if self.metahuman:
            lines.append("MetaHuman rig-logic controls.")
        if self.exclusive_visemes:
            lines.append("Exclusive visemes: one mouth shape per frame.")
        if self.layered_blendshapes:
            lines.append("Layered blendshapes (ARKit-style).")
        for slot in SLOTS:
            targets = self.slots.get(slot)
            if targets:
                lines.append("  %-16s %s" % (slot, ", ".join(repr(t) for t in targets)))
        missing = [s for s in SLOTS if not self.slots.get(s)]
        if missing:
            lines.append("  unmapped: " + ", ".join(missing))
        for note in self.notes:
            lines.append("  note: " + note)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _looks_metahuman(mesh):
    try:
        skeleton = mesh.skeleton
    except Exception:
        return False
    if skeleton is None:
        return False
    try:
        if "Face_Archetype" in skeleton.get_name():
            return True
    except Exception:
        pass
    # Curve metadata is the other tell, but enumerating it needs a curve type in
    # 5.5 and the call shape varies, so failure here is not conclusive.
    for enum_name in ("RawCurveTrackTypes",):
        try:
            curve_type = getattr(unreal, enum_name).RCT_FLOAT
            for identifier in skeleton.get_curve_identifiers(curve_type):
                if str(identifier.get_editor_property("name")).startswith(
                        "CTRL_expressions_"):
                    return True
        except Exception:
            continue
    return False


def _morph_names(mesh):
    try:
        return [str(n) for n in mesh.get_all_morph_target_names()]
    except Exception:
        return []


def analyze(mesh, prefer_layered_arkit_mouth=True):
    """Map a skeletal mesh's face onto the canonical slots."""
    profile = FaceProfile()
    if mesh is None:
        profile.notes.append("No skeletal mesh.")
        return profile

    try:
        profile.mesh_name = mesh.get_name()
    except Exception:
        profile.mesh_name = "<unknown>"

    if _looks_metahuman(mesh):
        profile.metahuman = True
        for slot, curves, scale in METAHUMAN_BINDINGS:
            for curve in curves:
                profile.slots[slot].append(CurveTarget(curve, scale, "metahuman"))
        profile.notes.append("MetaHuman face detected - driving CTRL_expressions "
                             "rig controls.")
        return profile

    morphs = _morph_names(mesh)
    profile.morph_count = len(morphs)
    if not morphs:
        profile.notes.append("'%s' has no morph targets. Facial animation needs a "
                             "face mesh with blendshapes." % profile.mesh_name)
        return profile

    arkit_hits = 0
    vowel_hits = 0
    phoneme_hits = 0

    for morph in morphs:
        norm = normalize(morph)
        if norm in ARKIT_MARKERS:
            arkit_hits += 1

        exact = EXACT_TABLE.get(norm)
        if exact is not None:
            slot, scale, family = exact
            profile.slots[slot].append(CurveTarget(morph, scale, family))
            if family == "vowel":
                vowel_hits += 1
            elif family == "phoneme":
                phoneme_hits += 1
            continue

        matched = False
        for fragment, slot, scale in FUZZY_TABLE:
            if fragment in norm:
                profile.slots[slot].append(CurveTarget(morph, scale, "fuzzy"))
                matched = True
                break
        if not matched:
            profile.unrecognized += 1

    arkit_rich = arkit_hits >= 6
    vowel_mouth = vowel_hits >= 3
    phoneme_mouth = phoneme_hits >= 3

    profile.layered_blendshapes = arkit_rich
    profile.layered_arkit_mouth = bool(prefer_layered_arkit_mouth and arkit_rich)

    if profile.layered_arkit_mouth:
        profile.exclusive_visemes = False
    elif vowel_mouth or phoneme_mouth:
        profile.exclusive_visemes = True

    # A dual export carries two or three families that reach the same slots, and
    # each one moves the whole feature. Left and right halves of one family are
    # meant to stack; two rival families are not, so per slot only the preferred
    # family survives wherever it has anything to offer.
    preferred = None
    if arkit_rich and profile.layered_arkit_mouth:
        preferred = "arkit"
    elif phoneme_mouth:
        preferred = "phoneme"
    elif vowel_mouth:
        preferred = "vowel"

    if preferred is not None:
        dropped, slots_touched = _prefer_family(profile, preferred)
        if dropped:
            profile.notes.append(
                "Dual face export: kept the %s set and dropped %d rival target%s "
                "across %d slot%s, so no feature is driven twice."
                % (preferred, dropped, "" if dropped == 1 else "s",
                   slots_touched, "" if slots_touched == 1 else "s"))

    if profile.exclusive_visemes:
        # An exclusive rig's mouth slots are single centre shapes, and a phoneme
        # set offers several near-synonyms per slot (aaa, ahh, schwa and gk all
        # open the jaw). Energy-band lipsync cannot tell them apart, so one
        # carrier per slot is driven and the variants are left at rest.
        collapsed = _collapse_to_single_carrier(profile)
        if collapsed:
            profile.notes.append(
                "Exclusive rig: reduced %d mouth slot%s to a single carrier shape "
                "so the jaw is not opened several times over."
                % (collapsed, "" if collapsed == 1 else "s"))
        profile.notes.append("Exclusive visemes: one dominant mouth shape per frame.")
    elif arkit_rich:
        profile.notes.append("Layered ARKit mouth: shapes stack per frame.")

    if profile.unrecognized:
        profile.notes.append("%d morph target%s not recognised and left alone."
                             % (profile.unrecognized,
                                "" if profile.unrecognized == 1 else "s"))

    return profile


def _collapse_to_single_carrier(profile):
    """
    Reduce each exclusive mouth slot to one target: the highest-scale one, and
    among ties the shortest name, which is reliably the canonical shape rather
    than a qualified variant. Returns the number of slots changed.
    """
    changed = 0
    for slot in EXCLUSIVE_MOUTH_SLOTS:
        targets = profile.slots.get(slot, [])
        if len(targets) < 2:
            continue
        best = sorted(targets, key=lambda t: (-t.scale, len(t.name)))[0]
        profile.slots[slot] = [best]
        changed += 1
    return changed


def _prefer_family(profile, keep):
    """
    Per slot, keep only the preferred family's targets when it has any.

    A slot that the preferred family does not reach keeps whatever it matched, so
    consonant visemes and full-face emotions still work on a mesh whose primary
    family has no equivalent. Returns (targets dropped, slots changed).
    """
    dropped = 0
    slots_touched = 0
    for slot in SLOTS:
        targets = profile.slots.get(slot, [])
        if len(targets) < 2:
            continue
        survivors = [t for t in targets if t.family == keep]
        if not survivors or len(survivors) == len(targets):
            continue
        dropped += len(targets) - len(survivors)
        slots_touched += 1
        profile.slots[slot] = survivors
    return dropped, slots_touched
