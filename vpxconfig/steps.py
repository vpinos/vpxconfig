"""Wizard layout: which settings appear on which page, and in which group.

The settings themselves are `Setting` objects (see settings.py); to add one, put a `Setting(...)`
line in a group below. Scope: only what differs between the default ini and the reference
three-screen cabinet config (see ../three-screen-cabinet-config.md).
"""
from .inputs import input_groups
from .plugins import plugin_steps
from .settings import DISPLAY, Setting as S, resolve


def _priority(section, renderer_id, renderer_name, initial=None):
    """`Priority.<renderer>`: which renderer is used on a window (VPX: 0..100, default 0, higher wins)."""
    return S(section, f"Priority.{renderer_id}", type="int", default="0", min="0", max="100", initial=initial,
             label=renderer_name,
             description=f"Priority of the '{renderer_name}' renderer on the {section} display. "
                         "Higher values are prioritized over lower ones.")


def _window(prefix, title, description, priorities=()):
    """Backglass / ScoreView: like Playfield, picking a display fills in its full size."""
    return {
        "id": prefix.lower(), "title": title, "description": description,
        "groups": [
            {"title": "Output", "fields": [
                S(prefix, f"{prefix}Output"), S(prefix, f"{prefix}Display", type=DISPLAY),
                S(prefix, f"{prefix}Width"), S(prefix, f"{prefix}Height")]},
            *([{"title": "Renderer priority", "fields": list(priorities)}] if priorities else []),
        ],
        "size_keys": (f"{prefix}.{prefix}Width", f"{prefix}.{prefix}Height"),
    }


STEPS = [
    {
        # VPXConfig's own configuration: which ini to start from and where the new file is written.
        "id": "start", "title": "Start", "panels": ["base", "target"],
        "description": "Configure VPXConfig itself: the ini file to start from and where the new file is written.",
        "groups": [],
    },
    {
        "id": "general", "title": "General",
        "description": "Basics that apply to the whole installation.",
        "groups": [
            {"title": "View", "fields": [S("Player", "BGSet")]},
            {"title": "Performance", "fields": [
                S("Player", "SyncMode", label="Synchronization Mode"),
                S("Player", "MaxFramerate"), S("Player", "MaxTexDimension")]},
        ],
    },
    {
        "id": "playfield", "title": "Playfield",
        "description": "The main screen the table is rendered on, and its physical dimensions.",
        "groups": [
            {"title": "Display", "fields": [
                S("Player", "PlayfieldDisplay", type=DISPLAY),
                S("Player", "PlayfieldFullScreen", label="Playfield FullScreen",
                  options=[("0", "Disabled"), ("1", "Enabled")],
                  description="Enable borderless fullscreen mode (Disabled = windowed)."),
                S("Player", "PlayfieldFSWidth"), S("Player", "PlayfieldFSHeight")]},
            {"title": "Physical screen", "fields": [
                S("Player", "ScreenWidth", placeholder=""), S("Player", "ScreenHeight", placeholder=""),
                S("Player", "ScreenInclination", placeholder="")]},
        ],
        "size_keys": ("Player.PlayfieldFSWidth", "Player.PlayfieldFSHeight"),
    },
    _window("Backglass", "Backglass", "The backglass artwork window."),
    _window("ScoreView", "ScoreView", "The score / DMD window.", priorities=[
        _priority("ScoreView", "ScoreView", "ScoreView", initial="1"),
        _priority("ScoreView", "PUP", "PinUp Player", initial="3"),
        _priority("ScoreView", "B2SLegacyDMD", "B2S Legacy DMD", initial="2"),
    ]),
    {
        "id": "input", "title": "Input",
        "description": "Input devices and button mappings. Click \"Press a key\" on a mapping, then press the keyboard key you want: "
                       "only the Key;<number> part changes, entries for other devices (a game controller, for example) "
                       "are kept exactly as they are. The Devices line lists every known device (separated by ';'); "
                       "device names can't be detected, so it is shown as it appears in your ini.",
        "groups": input_groups(),
    },
    *plugin_steps(),   # one page per plugin, under the "Plugins" header of the sidebar
    {"id": "review", "title": "Review & Save", "review": True,
     "description": "Check what will change, then write the new ini file."},
]


def build_steps(template, steps=STEPS):
    """Resolve every Setting against the template. Returns the steps as JSON-able dicts."""
    out_steps, seen = [], set()
    for step in steps:
        out = {k: v for k, v in step.items() if k != "groups"}
        out["groups"] = []
        for group in step.get("groups", []):
            fields = [resolve(s, template, step["title"], group["title"]) for s in group["fields"]]
            for f in fields:
                f["parent"] = step.get("parent", "")
                if f["id"] in seen:
                    raise ValueError(f"{f['id']} is defined twice")
                seen.add(f["id"])
            out["groups"].append({"title": group["title"], "fields": fields})
        out_steps.append(out)
    return out_steps


def all_fields(steps):
    return {f["id"]: f for s in steps for g in s["groups"] for f in g["fields"]}
