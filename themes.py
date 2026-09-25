"""
Booth themes. Pick one with THEME in booth.py (or BOOTH_THEME=... on the
command line). Each theme has its own screen UI page in static/ and its own
text on the printed strip.
"""

THEMES = {
    "hatchery": {
        "page":    "index.html",               # screen UI, in static/
        "caption": "The Hatchery",             # written on the polaroid top
        "handle":  "@bc_hatchery",             # next to the logo in the chin
        "credit":  "built by @s.ofile !",      # small print under the handle
        "logo":    "media/hatchery_logo.png",  # None = no logo
        "ink":     (125, 59, 74),              # on-screen receipt text colour
    },
    "walsh203": {
        "page":    "walsh203.html",
        "caption": "Walsh203",
        "handle":  "oh man we so touse",
        "credit":  "built by @s.ofile !",
        "logo":    None,
        "ink":     (123, 16, 34),              # BC maroon, matches the UI
    },
}
