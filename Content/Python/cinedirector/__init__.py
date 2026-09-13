# Copyright Roundtree. All Rights Reserved.
"""
CineDirector for the Otherside ODK.

Plain-language shot descriptions become cine cameras, keyframes, focus and camera
cuts in a Level Sequence. This is the Python port of the UE 5.8 C++ plugin, for
the ODK's Blueprint-only Unreal 5.5 build where no C++ module can be compiled.

Layout:
    vec       - plain-tuple vector maths, no editor dependency
    plan      - shot-plan model, JSON schema and reply parser (no editor dependency)
    geometry  - framing and move evaluation (no editor dependency)
    scene     - level snapshot: labels, bounds, facing, head points
    executor  - turns a plan into cameras, tracks and cuts
"""

__version__ = "0.1.0"
