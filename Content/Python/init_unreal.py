# Copyright Roundtree. All Rights Reserved.
"""
Editor startup hook.

Unreal runs this automatically for every enabled plugin that has a
Content/Python folder, so the CineDirector menus appear without the user
importing anything. Menu registration is deferred until the level editor exists,
because the Tools menu is not built yet at startup.
"""

import unreal


def _register():
    try:
        import cinedirector.panel as panel
        return panel.register_menus()
    except Exception:
        import traceback
        unreal.log_error("CineDirector: menu registration failed:\n"
                         + traceback.format_exc())
        return True    # stop retrying; the error is already reported


def _on_tick(_delta_seconds):
    """Retry until the Tools menu exists, then unhook."""
    if _register():
        unreal.unregister_slate_post_tick_callback(_on_tick.handle)


if not _register():
    _on_tick.handle = unreal.register_slate_post_tick_callback(_on_tick)
