import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from vpxconfig import fs, server, settings, system
from vpxconfig.generate import changes, generate
from vpxconfig.ini import IniDocument, parse_comment
from vpxconfig.settings import Setting, resolve, validate

HERE = Path(__file__).parent
TEMPLATE = HERE.parent / "VPinballX.ini"


class IniTests(unittest.TestCase):
    def test_parse_options_default(self):
        m = parse_comment(["Display Mode: Select windowed or fullscreen [Default: 'Windowed', 0='Windowed', 1='Borderless Fullscreen']"])
        self.assertEqual((m.label, m.default), ("Display Mode", "0"))
        self.assertEqual(m.options, [("0", "Windowed"), ("1", "Borderless Fullscreen")])

    def test_parse_range_and_bool(self):
        m = parse_comment(["Width: Width of the window [Default: 16384 in 0 .. 16384]"])
        self.assertEqual((m.default, m.min, m.max), ("16384", "0", "16384"))
        self.assertEqual(parse_comment(["AutoSaveOn: Enable Autosave [Default: 1]"]).default, "1")

    def test_multiline_comment(self):
        m = parse_comment(["Limit Framerate:  [Default: -1.0 in -1.0 .. 1000.0]:", "  -1 will limit FPS"])
        self.assertEqual((m.label, m.default, m.detail), ("Limit Framerate", "-1.0", ["-1 will limit FPS"]))

    def test_set_replaces_in_place_and_preserves_everything_else(self):
        text = TEMPLATE.read_text()
        doc = IniDocument(text)
        doc.set("Player", "BGSet", "1")
        out = doc.to_text().split("\n")
        old = text.split("\n")
        self.assertEqual(len(out), len(old))
        self.assertEqual([i for i, (a, b) in enumerate(zip(old, out)) if a != b], [old.index(next(l for l in old if l.startswith("BGSet")))])
        self.assertEqual(IniDocument(doc.to_text()).get("Player", "BGSet"), "1")

    def test_set_missing_key_and_section(self):
        doc = IniDocument("[A]\nx = 1\n\n\n[B]\n")
        doc.set("A", "y", "2")
        doc.set("C", "z", "3")
        self.assertEqual(doc.to_text(), "[A]\nx = 1\ny = 2\n\n\n[B]\n\n[C]\nz = 3\n")   # the final newline is kept
        no_newline = IniDocument("[A]\nx = 1")
        no_newline.set("B", "y", "2")
        self.assertEqual(no_newline.to_text(), "[A]\nx = 1\n\n[B]\ny = 2")


class StepsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = server.App(TEMPLATE, "unused", tempfile.mkdtemp() + "/state.json")

    def test_every_field_resolves_against_template(self):
        self.assertGreater(len(self.app.fields), 10)

    def test_only_writes_what_is_set_and_differs_from_default(self):
        f = self.app.fields
        vals = {**self.app.state["values"], "Player.BGSet": "1", "Player.MaxFramerate": "60.0", "Player.MaxTexDimension": "16384",  # == default
                "Player.ScreenInclination": "0.0", "Backglass.BackglassWndX": ""}
        base = IniDocument(TEMPLATE.read_text())
        ids = [c["id"] for c in changes(f, vals, base)]
        self.assertEqual(sorted(ids), ["Player.BGSet", "Player.MaxFramerate"])


    def test_playfield_display_is_a_monitor_field(self):
        f = self.app.fields["Player.PlayfieldDisplay"]
        self.assertEqual((f["type"], f["step"]), ("display", "Playfield"))

    def test_backglass_and_scoreview_have_display_and_size(self):
        for step, extra in (("Backglass", []), ("ScoreView", ["Priority.B2SLegacyDMD", "Priority.PUP", "Priority.ScoreView"])):
            keys = sorted(f["key"] for f in self.app.fields.values() if f["step"] == step and not f["parent"])   # not the plugin of that name
            self.assertEqual(keys, sorted([f"{step}Display", f"{step}Height", f"{step}Output", f"{step}Width"] + extra))
        for step in self.app.steps:
            if step["id"] in ("playfield", "backglass", "scoreview"):
                self.assertEqual(len(step["size_keys"]), 2)

    def test_scoreview_priorities(self):
        for key, label, initial in (("ScoreView", "ScoreView", "1"), ("PUP", "PinUp Player", "3"),
                                    ("B2SLegacyDMD", "B2S Legacy DMD", "2")):
            f = self.app.fields[f"ScoreView.Priority.{key}"]
            self.assertEqual((f["type"], f["default"], f["min"], f["max"], f["label"], f["group"], f["initial"]),
                             ("int", "0", "0", "100", label, "Renderer priority", initial))
            self.assertIn("Higher values are prioritized over lower ones", f["help"])
            self.assertIn("between 0 and 100", validate(f, "101"))

    def test_starting_answers_are_the_initial_values(self):
        values = self.app.state["values"]
        self.assertEqual({k: v for k, v in values.items() if "Priority" in k},
                         {"ScoreView.Priority.ScoreView": "1", "ScoreView.Priority.PUP": "3", "ScoreView.Priority.B2SLegacyDMD": "2"})
        # The base file already has the starting answers that differ from VPX's default, so nothing shows as changed.
        self.assertEqual(self.app.preview()["changes"], [])

    def test_autosave_is_not_offered(self):
        """AutoSaveOn is a setting of VPX's Windows table editor, so the wizard doesn't ask about it."""
        self.assertNotIn("Editor.AutoSaveOn", self.app.fields)
        self.assertFalse([f for f in self.app.fields.values() if f["section"] == "Editor"])
        # a loaded file's AutoSaveOn is simply carried through untouched
        doc = IniDocument(TEMPLATE.read_text())
        doc.set("Editor", "AutoSaveOn", "0")
        path = Path(tempfile.mkdtemp()) / "mine.ini"
        path.write_text(doc.to_text())
        app = server.App(TEMPLATE, None, path.with_name("state.json"))
        app.set_base(str(path))
        self.assertEqual(IniDocument(app.preview()["text"]).get("Editor", "AutoSaveOn"), "0")
        self.assertEqual(app.preview()["changes"], [])

    def test_a_fresh_start_on_the_base_file_shows_no_changes(self):
        preview = self.app.preview()
        self.assertEqual((preview["changes"], preview["problems"]), ([], []))
        self.assertEqual(preview["text"], TEMPLATE.read_text())          # the output would be identical to the base file

    def test_the_base_file_holds_every_starting_answer_that_differs_from_the_vpx_default(self):
        base = IniDocument(TEMPLATE.read_text())
        for fid, f in self.app.fields.items():
            if f["initial"] and f["initial"] != f["default"]:
                self.assertEqual(base.get(f["section"], f["key"]), f["initial"], fid)

    def test_a_value_equal_to_vpxs_default_is_always_left_blank(self):
        """There is no option for this any more: blank means the default, so an explicit default is never written."""
        self.app.update_state({"values": {"Player.MaxFramerate": "-1.0", "Player.BGSet": "0", "Player.MaxTexDimension": "16384"}})
        preview = self.app.preview()
        self.assertEqual(preview["changes"], [])
        self.assertEqual(preview["text"], TEMPLATE.read_text())
        self.assertNotIn("omit_defaults", self.app.public_state())
        self.app.update_state({"omit_defaults": False})           # an old page sending the removed option changes nothing
        self.assertEqual(self.app.preview()["changes"], [])

    def test_the_whole_base_file_is_written_with_only_the_changed_values_different(self):
        self.app.update_state({"values": {"Player.BGSet": "1"}})
        out, base = self.app.preview()["text"].split("\n"), TEMPLATE.read_text().split("\n")
        self.assertEqual(len(out), len(base))
        self.assertEqual([(a, b) for a, b in zip(base, out) if a != b], [("BGSet = ", "BGSet = 1")])

    def test_physical_size_has_no_placeholder(self):
        for key in ("ScreenWidth", "ScreenHeight", "ScreenInclination"):
            self.assertEqual(self.app.fields[f"Player.{key}"]["placeholder"], "")
        self.assertEqual(self.app.fields["Player.PlayfieldFSWidth"]["placeholder"], "16384")

    def test_generate(self):
        text, todo = generate(TEMPLATE.read_text(), self.app.fields, {**self.app.state["values"], "Player.BGSet": "1"})
        self.assertEqual(IniDocument(text).get("Player", "BGSet"), "1")
        self.assertEqual(len(todo), 1)


class BaseFileTests(unittest.TestCase):
    """Using an existing VPinballX.ini as the base."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        doc = IniDocument(TEMPLATE.read_text())
        doc.set("Player", "BGSet", "1")
        doc.set("Player", "MaxFramerate", "60.0")
        doc.set("Player", "PlayfieldFullScreen", "0")          # explicit, but equal to the default
        doc.set("Backglass", "BackglassDisplay", "Some Monitor (DP-1)")
        doc.set("Player", "SomeKeyFromAnotherBuild", "keep me")  # not a wizard field
        doc.set("ScoreView", "Priority.PUP", "7")               # the file's own value, not the suggested 3
        self.base = self.dir / "mine.ini"
        self.base.write_text(doc.to_text())
        self.state = self.dir / "state.json"
        self.app = server.App(TEMPLATE, self.dir / "out.ini.new", self.state)

    def test_default_base_is_the_project_ini(self):
        st = self.app.public_state()
        self.assertEqual(st["base"], {"path": "", "name": "VPinballX.ini", "is_default": True, "backup": "", "error": ""})

    def test_load_fills_answers_and_produces_no_changes(self):
        st = self.app.set_base(str(self.base))
        self.assertEqual(st["base"]["name"], "mine.ini")
        self.assertEqual(st["values"]["Player.BGSet"], "1")
        self.assertEqual(st["values"]["Player.MaxFramerate"], "60.0")
        self.assertNotIn("Player.MaxTexDimension", st["values"])
        self.assertEqual(self.app.preview()["changes"], [])
        self.assertEqual(self.app.preview()["text"], self.base.read_text())

    def test_edit_clear_and_untouched_keys(self):
        self.app.set_base(str(self.base))
        self.app.update_state({"values": {"Player.MaxFramerate": "30", "Player.BGSet": "",
                                          "Player.PlayfieldFullScreen": ""}})
        ch = {c["id"]: c for c in self.app.preview()["changes"]}
        self.assertEqual((ch["Player.MaxFramerate"]["value"], ch["Player.MaxFramerate"]["was"]), ("30", "60.0"))
        self.assertEqual((ch["Player.BGSet"]["value"], ch["Player.BGSet"]["was"]), ("", "1"))   # cleared to default
        self.assertNotIn("Player.PlayfieldFullScreen", ch)   # "0" already equals the default
        out = IniDocument(self.app.preview()["text"])
        self.assertEqual(out.get("Player", "MaxFramerate"), "30")
        self.assertEqual(out.get("Player", "BGSet"), "")
        self.assertEqual(out.get("Player", "SomeKeyFromAnotherBuild"), "keep me")

    def test_choosing_the_default_value_clears_a_non_default_one_from_the_loaded_file(self):
        self.app.set_base(str(self.base))                      # the file has BGSet = 1
        self.app.update_state({"values": {"Player.BGSet": "0"}})   # 0 is VPX's default
        change = next(c for c in self.app.preview()["changes"] if c["key"] == "BGSet")
        self.assertEqual((change["was"], change["value"]), ("1", ""))
        self.assertEqual(IniDocument(self.app.preview()["text"]).get("Player", "BGSet"), "")

    def test_blank_path_goes_back_to_project_ini_and_resets_answers_to_the_starting_ones(self):
        self.app.set_base(str(self.base))
        st = self.app.set_base("  ")
        self.assertTrue(st["base"]["is_default"])
        self.assertEqual(st["values"], self.app._initial_values())
        self.assertNotIn("Player.BGSet", st["values"])
        self.assertEqual(IniDocument(self.app.preview()["text"]).get("Player", "BGSet"), "")

    def test_a_loaded_file_replaces_the_starting_answers_and_a_cleared_one_stays_cleared(self):
        st = self.app.set_base(str(self.base))
        self.assertEqual(st["values"]["ScoreView.Priority.PUP"], "7")          # the file's value replaces the suggested 3
        fresh = server.App(TEMPLATE, self.dir / "o.new", self.dir / "s2.json")
        fresh.update_state({"values": {"ScoreView.Priority.PUP": ""}})                 # user clears a suggested value
        again = server.App(TEMPLATE, self.dir / "o.new", self.dir / "s2.json")
        self.assertEqual(again.state["values"]["ScoreView.Priority.PUP"], "")
        self.assertEqual(again.state["values"]["ScoreView.Priority.ScoreView"], "1")   # untouched ones keep their start

    def test_bad_paths(self):
        (self.dir / "notes.txt").write_text("hello")
        for path, msg in [("relative/VPinballX.ini", "full path"), (str(self.dir / "nope.ini"), "No file"),
                          (str(self.dir), "No file"), (str(self.dir / "notes.txt"), "doesn't look like")]:
            with self.assertRaises(ValueError) as cm:
                self.app.set_base(path)
            self.assertIn(msg, str(cm.exception))
        self.assertTrue(self.app.public_state()["base"]["is_default"])   # failed loads change nothing

    def test_quotes_and_tilde_are_accepted(self):
        self.assertEqual(self.app.set_base(f'"{self.base}"')["base"]["path"], str(self.base))

    def test_backup_is_created_next_to_the_file(self):
        st = self.app.set_base(str(self.base))
        backup = Path(st["base"]["backup"])
        self.assertEqual(backup, self.dir / "mine.ini.vpconfigbackup")
        self.assertEqual(backup.read_bytes(), self.base.read_bytes())

    def test_same_backup_is_reused_and_a_different_one_is_never_overwritten(self):
        first = Path(self.app.set_base(str(self.base))["base"]["backup"])
        again = Path(self.app.set_base(str(self.base))["base"]["backup"])
        self.assertEqual(again, first)
        self.assertEqual(len(list(self.dir.glob("*.vpconfigbackup"))), 1)
        original = first.read_bytes()
        self.base.write_text(self.base.read_text() + "; edited later\n")
        newer = Path(self.app.set_base(str(self.base))["base"]["backup"])
        self.assertNotEqual(newer, first)
        self.assertTrue(newer.name.startswith("mine.ini.") and newer.name.endswith(".vpconfigbackup"))
        self.assertEqual(first.read_bytes(), original)          # the first backup is untouched
        self.assertEqual(newer.read_bytes(), self.base.read_bytes())

    def test_no_backup_for_project_ini_or_rejected_files(self):
        self.app.set_base("")
        (self.dir / "notes.txt").write_text("hello")
        with self.assertRaises(ValueError):
            self.app.set_base(str(self.dir / "notes.txt"))
        self.assertEqual(list(self.dir.glob("*.vpconfigbackup")), [])
        self.assertEqual(self.app.public_state()["base"]["backup"], "")

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_unwritable_directory_fails_the_load(self):
        ro = self.dir / "ro"
        ro.mkdir()
        target = ro / "VPinballX.ini"
        target.write_text(self.base.read_text())
        ro.chmod(0o555)
        try:
            with self.assertRaises(ValueError) as cm:
                self.app.set_base(str(target))
            self.assertIn("backup", str(cm.exception))
            self.assertTrue(self.app.public_state()["base"]["is_default"])
        finally:
            ro.chmod(0o755)

    def test_backup_path_survives_restart(self):
        backup = self.app.set_base(str(self.base))["base"]["backup"]
        again = server.App(TEMPLATE, self.dir / "out.ini.new", self.state)
        self.assertEqual(again.public_state()["base"]["backup"], backup)
        self.assertEqual(len(list(self.dir.glob("*.vpconfigbackup"))), 1)   # restarting does not back up again

    def test_base_survives_restart_and_vanished_file_falls_back(self):
        self.app.set_base(str(self.base))
        again = server.App(TEMPLATE, self.dir / "out.ini.new", self.state)
        self.assertEqual(again.public_state()["base"]["path"], str(self.base))
        self.assertEqual(again.public_state()["values"]["Player.BGSet"], "1")
        self.base.unlink()
        gone = server.App(TEMPLATE, self.dir / "out.ini.new", self.state)
        st = gone.public_state()
        self.assertTrue(st["base"]["is_default"])
        self.assertIn("Saved base file not used", st["base"]["error"])


class SettingTests(unittest.TestCase):
    """The Setting structure: template comment supplies whatever is not given explicitly."""

    @classmethod
    def setUpClass(cls):
        cls.template = IniDocument(TEMPLATE.read_text())

    def test_only_section_and_key_needed_for_a_key_in_the_template(self):
        f = resolve(Setting("Player", "MaxFramerate"), self.template, "General")
        self.assertEqual((f["label"], f["type"], f["default"], f["min"], f["max"]),
                         ("Limit Framerate", "float", "-1.0", "-1.0", "1000.0"))
        self.assertEqual(f["step"], "General")

    def test_explicit_values_win_over_the_template(self):
        f = resolve(Setting("Player", "MaxFramerate", label="FPS cap", description="Cap.", default="60.0",
                            min="30", max="240", placeholder="", notes=["a", "b"]), self.template)
        self.assertEqual((f["label"], f["help"], f["default"], f["min"], f["max"], f["placeholder"], f["detail"]),
                         ("FPS cap", "Cap.", "60.0", "30", "240", "", ["a", "b"]))

    def test_a_key_that_is_not_in_the_template_is_fully_described_here(self):
        s = Setting("Backglass", "Priority.PUP", type="int", label="PUP priority", default="0", min="0", max="9",
                    description="Which content wins on this window.")
        f = resolve(s, self.template)
        self.assertEqual((f["id"], f["type"], f["default"], f["label"]), ("Backglass.Priority.PUP", "int", "0", "PUP priority"))
        self.assertEqual(validate(f, "5"), None)
        self.assertIn("between", validate(f, "10"))

    def test_key_not_in_template_without_a_type_is_rejected(self):
        with self.assertRaises(ValueError) as cm:
            resolve(Setting("Backglass", "Priority.PUP"), self.template)
        self.assertIn("not in the template", str(cm.exception))

    def test_bad_definitions_are_caught(self):
        bad = [(Setting("Player", "BGSet", default="9"), "not one of the options"),
               (Setting("Player", "BGSet", type="nonsense"), "type must be one of"),
               (Setting("Player", "MaxFramerate", min="10", max="5"), "min is greater than max"),
               (Setting("X", "Y", type="select", options=[]), "needs options"),
               (Setting("X", "Y", type="int", default="abc"), "not a number"),
               (Setting("X", "Y", type="select", options=[("1", "a"), ("1", "b")]), "duplicate")]
        for setting, msg in bad:
            with self.assertRaises(ValueError, msg=msg) as cm:
                resolve(setting, self.template)
            self.assertIn(msg, str(cm.exception))

    def test_validate_acceptable_values(self):
        f = {k: v for k, v in self.__class__.fields().items()}
        self.assertIsNone(validate(f["Player.BGSet"], ""))               # blank = VPX default
        self.assertIsNone(validate(f["Player.BGSet"], "1"))
        self.assertIn("must be one of", validate(f["Player.BGSet"], "7"))
        self.assertIsNone(validate(f["Player.PlayfieldFSWidth"], "3840"))
        self.assertIn("whole number", validate(f["Player.PlayfieldFSWidth"], "38.5"))
        self.assertIn("between", validate(f["Player.PlayfieldFSWidth"], "99999"))
        self.assertIn("must be a number", validate(f["Player.ScreenWidth"], "abc"))
        self.assertIn("must be a number", validate(f["Player.ScreenWidth"], "nan"))
        self.assertIsNone(validate(f["Player.ScreenWidth"], "69.849998"))
        self.assertIsNone(validate(f["Backglass.BackglassDisplay"], "anything goes for a display name"))

    @classmethod
    def fields(cls):
        return server.App(TEMPLATE, "x", tempfile.mkdtemp() + "/s.json").fields

    def test_a_setting_missing_from_the_base_file_is_added_when_written(self):
        f = resolve(Setting("Backglass", "Priority.PUP", type="int", default="0", min="0", max="9"), self.template)
        text, todo = generate("[Backglass]\nBackglassOutput = 1\n\n[Player]\nx = 1\n", {f["id"]: f}, {f["id"]: "3"})
        self.assertEqual(IniDocument(text).get("Backglass", "Priority.PUP"), "3")
        self.assertEqual(len(todo), 1)

    def test_every_plugin_in_the_template_has_a_setting(self):
        """Fails when the template gains a plugin the wizard doesn't know about."""
        fields = self.fields()
        plugins = [sec for sec in self.template.sections() if sec.startswith("Plugin.") and self.template.has(sec, "Enable")]
        self.assertEqual(len(plugins), 19)          # the 10 of the default ini + the 9 added to the base file
        for sec in plugins:
            self.assertIn(f"{sec}.Enable", fields)
            self.assertEqual(fields[f"{sec}.Enable"]["type"], "bool")

    def test_every_wizard_setting_is_in_the_base_file(self):
        t = self.template
        self.assertEqual([fid for fid, f in self.fields().items() if not t.has(f["section"], f["key"])], [])

    def test_base_file_comments_agree_with_the_code_definitions(self):
        """The plugin and priority settings are defined in code AND commented in the base file; they must not drift."""
        from vpxconfig.plugins import plugin_groups
        from vpxconfig.steps import STEPS
        from vpxconfig.inputs import input_groups
        explicit = [s for g in plugin_groups() for s in g["fields"]]
        explicit += [s for st in STEPS if st["id"] == "scoreview" for g in st["groups"] for s in g["fields"]
                     if s.key.startswith("Priority.")]
        explicit += [s for g in input_groups() for s in g["fields"]]
        self.assertEqual(len(explicit), 88 + 56)     # 85 plugin settings + 3 ScoreView priorities + 56 Input settings
        for s in explicit:
            full = resolve(s, self.template)
            bare = resolve(Setting(s.section, s.key), self.template)       # what the base file alone says
            attrs = ["label", "help", "detail", "default", "default_label", "options", "min", "max"]
            if s.type != "mapping":                                        # a comment can't say "this is a key mapping"
                attrs.append("type")
            for attr in attrs:
                self.assertEqual(full[attr], bare[attr], f"{full['id']}.{attr}: code and base file disagree")

    def test_sync_base_tool_restores_a_missing_section_and_key_without_touching_other_lines(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("sync_base", HERE.parent / "tools" / "sync_base.py")
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        doc = IniDocument(TEMPLATE.read_text())
        first, _ = doc._sections["Plugin.HelloWorld"]                       # cut this whole section out of the base
        nxt = min(v[0] for v in doc._sections.values() if v[0] > first)
        trimmed = "\n".join(doc.lines[:first - 3] + doc.lines[nxt:])
        trimmed = trimmed.replace("; B2S Hide DMD: Hide the DMD. [Default: 1]\nB2SHideDMD = \n\n", "")   # and one key
        fields = self.fields()
        text, added, _ = tool.sync(trimmed, fields)
        self.assertEqual(sorted(added), ["Plugin.B2SLegacy.B2SHideDMD", "Plugin.HelloWorld.Enable"])
        out = IniDocument(text)
        self.assertEqual(out.meta("Plugin.HelloWorld", "Enable").default, "0")
        self.assertEqual(out.meta("Plugin.B2SLegacy", "B2SHideDMD").default, "1")
        self.assertTrue(set(trimmed.split("\n")) <= set(text.split("\n")))    # nothing existing was changed or removed
        self.assertEqual(tool.sync(text, fields)[1], [])                        # a second run adds nothing

    def test_sync_base_fills_starting_answers_but_never_overwrites_a_value(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("sync_base", HERE.parent / "tools" / "sync_base.py")
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        fields = self.fields()
        blanked = TEMPLATE.read_text().replace("Priority.PUP = 3", "Priority.PUP = ").replace("Devices = Key", "Devices = ")
        text, added, valued = tool.sync(blanked, fields)
        self.assertEqual((added, sorted(valued)), ([], ["Input.Devices", "ScoreView.Priority.PUP"]))
        self.assertEqual(IniDocument(text).get("ScoreView", "Priority.PUP"), "3")
        kept = TEMPLATE.read_text().replace("Priority.PUP = 3", "Priority.PUP = 9")
        self.assertEqual(IniDocument(tool.sync(kept, fields)[0]).get("ScoreView", "Priority.PUP"), "9")     # a value is never overwritten
        self.assertEqual(tool.sync(TEMPLATE.read_text(), fields)[1:], ([], []))                             # the base file is complete

    def test_plugin_defaults_come_from_the_template(self):
        fields = self.fields()
        self.assertEqual(fields["Plugin.DMDUtil.Enable"]["default_label"], "Off")
        self.assertEqual(fields["Plugin.PinMAME.Enable"]["default_label"], "On")
        self.assertEqual((fields["Plugin.PUP.Enable"]["label"], fields["Plugin.PUP.Enable"]["group"]), ("Enable", "PUP"))
        self.assertEqual(fields["Plugin.PUP.Enable"]["help"], "Enable PinUp player plugin")

    def test_plugin_parameters_come_from_the_plugin_sources(self):
        f = self.fields()
        self.assertEqual((f["Plugin.PUP.MainVol"]["type"], f["Plugin.PUP.MainVol"]["default"],
                          f["Plugin.PUP.MainVol"]["min"], f["Plugin.PUP.MainVol"]["max"]), ("float", "1.0", "0.0", "1.0"))
        self.assertEqual((f["Plugin.PUP.BGPadLeft"]["max"], f["Plugin.PUP.BGPadLeft"]["help"]), ("4096", "Left Padding of backglass"))
        self.assertEqual(f["Plugin.B2SLegacy.ScoreViewDMDOverlay"]["type"], "bool")
        self.assertEqual(f["Plugin.B2SLegacy.ScoreViewDMDX"]["max"], "65535")
        self.assertEqual(f["Plugin.PinMAME.PinMAMEPath"]["type"], "text")
        self.assertEqual(f["Plugin.PinMAME.PinMAMEPath"]["placeholder"], "")

    def test_b2slegacy_has_every_key_of_the_reference_config_in_order(self):
        """The 22 parameters listed for [Plugin.B2SLegacy] in the reference config, in that order."""
        page = next(st for st in server.App(TEMPLATE, "x", tempfile.mkdtemp() + "/s.json").steps if st["id"] == "plugin-b2slegacy")
        group = page["groups"][0]
        self.assertEqual([f["key"] for f in group["fields"]], [
            "Enable", "BackglassDMDOverlay", "BackglassDMDAutoPos", "BackglassDMDX", "BackglassDMDY", "BackglassDMDW",
            "BackglassDMDH", "ScoreViewDMDOverlay", "ScoreViewDMDAutoPos", "ScoreViewDMDX", "ScoreViewDMDY",
            "ScoreViewDMDW", "ScoreViewDMDH", "B2SHideGrill", "B2SHideB2SDMD", "B2SHideB2SBackglass", "B2SHideDMD",
            "B2SDualMode", "B2SBackglassWidth", "B2SBackglassHeight", "B2SDMDWidth", "B2SDMDHeight", "B2SDMDFlipY"])

    def test_b2s_defaults_come_from_the_plugin_source(self):
        f = self.fields()
        p = "Plugin.B2SLegacy."
        self.assertEqual(f[p + "B2SHideDMD"]["default_label"], "On")
        for key in ("B2SHideGrill", "B2SHideB2SDMD", "B2SHideB2SBackglass", "B2SDMDFlipY"):
            self.assertEqual(f[p + key]["default_label"], "Off", key)
        self.assertEqual([(o["value"], o["label"]) for o in f[p + "B2SDualMode"]["options"]], [("1", "Authentic"), ("2", "Fantasy")])
        self.assertEqual(f[p + "B2SDualMode"]["default_label"], "Authentic")
        self.assertIsNotNone(validate(f[p + "B2SDualMode"], "0"))
        self.assertEqual([f[p + k]["default"] for k in ("B2SBackglassWidth", "B2SBackglassHeight", "B2SDMDWidth", "B2SDMDHeight")],
                         ["1024", "768", "512", "128"])
        self.assertIn("between 0 and 16384", validate(f[p + "B2SDMDWidth"], "20000"))

    def test_altsound_pinmame_cheat_and_pup_folder(self):
        f = self.fields()
        self.assertEqual((f["Plugin.AltSound.Folder"]["type"], f["Plugin.AltSound.Folder"]["default"],
                          f["Plugin.AltSound.Folder"]["label"]), ("text", "", "AltSound Folder"))
        self.assertTrue(f["Plugin.AltSound.Folder"]["detail"])
        self.assertEqual((f["Plugin.PinMAME.Cheat"]["type"], f["Plugin.PinMAME.Cheat"]["default_label"],
                          f["Plugin.PinMAME.Cheat"]["label"]), ("bool", "Off", "Cheat Mode"))
        self.assertEqual((f["Plugin.PUP.PUPFolder"]["type"], f["Plugin.PUP.PUPFolder"]["label"]), ("text", "PinUp Player Folder"))

    def test_plugins_that_are_not_in_the_default_ini(self):
        f = self.fields()
        extras = ["vpx", "UpscaleDMD", "B2S", "DOF", "HelloScript", "Inspector", "RemoteControl", "AlphaDMD", "HelloWorld"]
        for name in extras:
            e = f[f"Plugin.{name}.Enable"]
            self.assertEqual((e["type"], e["default_label"], e["group"]), ("bool", "Off", name), name)   # VPX's generic default
            self.assertTrue(e["help"], name)
        self.assertIn("crash", " ".join(f["Plugin.vpx.Enable"]["detail"]))
        port = f["Plugin.Inspector.port"]
        self.assertEqual((port["type"], port["default"], port["min"], port["max"]), ("int", "2113", "1024", "65535"))
        self.assertIn("between 1024 and 65535", validate(port, "80"))
        self.assertFalse([k for k in f if "Kiki" in k])            # development tooling, never offered

    def test_writing_a_plugin_that_the_base_file_lacks_creates_its_section(self):
        text, todo = generate("[Player]\nBGSet = \n", self.fields(), {"Plugin.Inspector.Enable": "1", "Plugin.Inspector.port": "3000"})
        doc = IniDocument(text)
        self.assertEqual((doc.get("Plugin.Inspector", "Enable"), doc.get("Plugin.Inspector", "port")), ("1", "3000"))
        self.assertEqual(len(todo), 2)

    def test_every_plugin_parameter_has_a_description_and_pinmame_sound_is_left_out(self):
        f = self.fields()
        for fid, field in f.items():
            if fid.startswith("Plugin.") and not fid.endswith(".Enable"):
                self.assertTrue(field["help"], f"{fid} has no description")
        self.assertNotIn("Plugin.PinMAME.Sound", f)          # meaning unknown, deliberately not offered

    def test_every_shipped_setting_is_valid_and_unique(self):
        fields = self.fields()
        self.assertEqual(len(fields), 163)
        for f in fields.values():
            self.assertIn(f["type"], settings.TYPES)


class InputPageTests(unittest.TestCase):
    """The Input page: devices and the button mappings given by the user (VPX's built-in defaults + ShowRules)."""

    # What the user asked for (non-blank values of their list).
    USER_LIST = {
        "Devices": "Key",
        "Mapping.LeftFlipper": "Key;225", "Mapping.RightFlipper": "Key;229", "Mapping.LeftStagedFlipper": "Key;225",
        "Mapping.RightStagedFlipper": "Key;229", "Mapping.LeftMagna": "Key;224", "Mapping.RightMagna": "Key;228",
        "Mapping.UIUp": "Key;224", "Mapping.UIDown": "Key;228", "Mapping.UILeft": "Key;225", "Mapping.UIRight": "Key;229",
        "Mapping.LaunchBall": "Key;40", "Mapping.LeftNudge": "Key;29", "Mapping.RightNudge": "Key;56",
        "Mapping.CenterNudge": "Key;44", "Mapping.Tilt": "Key;23", "Mapping.Credit1": "Key;34", "Mapping.Credit2": "Key;33",
        "Mapping.Credit3": "Key;32", "Mapping.Credit4": "Key;35", "Mapping.Start": "Key;30", "Mapping.ExtraBall": "Key;5",
        "Mapping.Lockbar": "Key;226", "Mapping.Pause": "Key;19", "Mapping.PerfOverlay": "Key;68", "Mapping.ExitGame": "Key;41",
        "Mapping.InGameUI": "Key;69", "Mapping.VolumeDown": "Key;45", "Mapping.VolumeUp": "Key;46", "Mapping.ShowRules": "Key;21",
        "Mapping.SlamTilt": "Key;74", "Mapping.CoinDoor": "Key;77", "Mapping.Reset": "Key;60", "Mapping.Service1": "Key;36",
        "Mapping.Service2": "Key;37", "Mapping.Service3": "Key;38", "Mapping.Service4": "Key;39", "Mapping.Service5": "Key;35",
        "Mapping.Service6": "Key;75", "Mapping.Service7": "Key;45", "Mapping.VRCenter": "Key;93", "Mapping.VRUp": "Key;96",
        "Mapping.VRDown": "Key;90", "Mapping.GenTournament": "Key;226 & Key;30", "Mapping.DebugBalls": "Key;18",
        "Mapping.Debugger": "Key;7", "Mapping.ToggleStereo": "Key;67",
    }
    BLANK = ["NudgeSensorCount", "PlungerSensorCount", "Mapping.Service8", "Mapping.VRFront", "Mapping.VRBack",
             "Mapping.Custom1", "Mapping.Custom2", "Mapping.Custom3", "Mapping.Custom4"]

    @classmethod
    def setUpClass(cls):
        cls.app = server.App(TEMPLATE, "x", tempfile.mkdtemp() + "/s.json")

    def test_devices_are_one_line_not_broken_out_per_device(self):
        """A game controller (or any other device) shows up inside `Devices` and in the mapping strings; the page has no
        per-device entries, because device ids and names are machine specific and can't be detected."""
        keys = {f["key"] for f in self.app.fields.values() if f["step"] == "Input"}
        self.assertFalse([k for k in keys if k.startswith("Device.")])
        self.assertFalse([k for k in keys if "SDLJoy" in k or k.startswith(("Mapping.Plunger", "Mapping.Nudge"))])
        self.assertIn("Devices", keys)

    def test_the_page_has_every_setting_of_the_list_and_starts_with_its_values(self):
        listed = set(self.USER_LIST) | set(self.BLANK)
        self.assertEqual({f["key"] for f in self.app.fields.values() if f["step"] == "Input"}, listed)
        starting = {f["key"]: self.app.state["values"][fid] for fid, f in self.app.fields.items()
                    if f["step"] == "Input" and fid in self.app.state["values"]}
        self.assertEqual(starting, self.USER_LIST)
        for key in self.BLANK:
            self.assertNotIn(f"Input.{key}", self.app.state["values"])

    def test_defaults_are_vpxs_builtin_ones(self):
        f = self.app.fields
        self.assertEqual(f["Input.Mapping.LeftFlipper"]["default"], "Key;225")
        self.assertEqual(f["Input.Mapping.ShowRules"]["default"], "")                       # no built-in key
        self.assertEqual(f["Input.Mapping.ShowRules"]["initial"], "Key;21")                 # the user's choice
        self.assertEqual(f["Input.Mapping.GenTournament"]["default"], "Key;226 & Key;30")
        self.assertEqual(f["Input.Mapping.VRFront"]["default"], "")
        self.assertEqual(f["Input.NudgeSensorCount"]["max"], "100")

    def test_only_what_differs_from_vpxs_defaults_is_written(self):
        # Devices and ShowRules are the only values that differ from VPX's defaults, and the base file already has both.
        self.assertEqual([c for c in self.app.preview()["changes"] if c["step"] == "Input"], [])
        base = IniDocument(TEMPLATE.read_text())
        self.assertEqual((base.get("Input", "Devices"), base.get("Input", "Mapping.ShowRules")), ("Key", "Key;21"))
        # ...and the mappings that equal VPX's built-in defaults stay blank in the output (blank means the default)
        out = IniDocument(self.app.preview()["text"])
        self.assertEqual((out.get("Input", "Mapping.LeftFlipper"), out.get("Input", "Mapping.GenTournament")), ("", ""))
        self.assertEqual((out.get("Input", "Devices"), out.get("Input", "Mapping.ShowRules")), ("Key", "Key;21"))

    def test_mapping_validation(self):
        f = self.app.fields["Input.Mapping.LeftFlipper"]
        for good in ("", "Key;225", "Key;226 & Key;30", "Key;225 | SDLJoy_030081b85e0400008e02000014010000_1;514;o;-0.300000"):
            self.assertIsNone(validate(f, good), good)
        for bad in ("Key;0", "Key;999", "Key;abc", "Key;225 |", " & Key;3", "Key;"):
            self.assertIsNotNone(validate(f, bad), bad)

    def test_no_label_contains_a_colon(self):
        """Labels are written into the base file as '; Label: description', so a colon would split them wrongly."""
        self.assertEqual([fid for fid, f in self.app.fields.items() if ":" in f["label"]], [])

    def test_key_table_used_by_the_browser(self):
        import re
        js = (HERE.parent / "web" / "keys.js").read_text()
        keys = [(int(n), code, name) for n, code, name in re.findall(r'\[(\d+), "([^"]+)", "((?:[^"\\]|\\.)*)"\]', js)]
        self.assertGreater(len(keys), 100)
        self.assertEqual(len({k[0] for k in keys}), len(keys))                 # scancodes unique
        self.assertEqual(len({k[1] for k in keys}), len(keys))                 # browser codes unique
        table = {n: (code, name) for n, code, name in keys}
        self.assertEqual(table[225], ("ShiftLeft", "Left Shift"))
        self.assertEqual(table[229], ("ShiftRight", "Right Shift"))
        self.assertEqual(table[44], ("Space", "Space"))
        self.assertEqual(table[40], ("Enter", "Enter"))
        self.assertEqual(table[93], ("Numpad5", "Keypad 5"))
        used = {int(n) for f in self.app.fields.values() if f["type"] == "mapping"
                for n in re.findall(r"Key;(\d+)", f["default"] + " " + f["initial"])}
        self.assertEqual(sorted(used - set(table)), [])                        # every key we pre-fill can be named


class LoadedControllerTests(unittest.TestCase):
    """An ini saved on a setup with a game controller (the Xbox 360 sample) is displayed and editable, with nothing about
    that controller known to the code: the lines are read from the loaded file."""

    JOY = "SDLJoy_030081b85e0400008e02000014010000_1"
    LINES = {
        "Devices": f"Key;{JOY}", "NudgeSensorCount": "1", "PlungerSensorCount": "1",
        "Device.Key.Type": "1", "Device.Key.Name": "Keyboards", "Device.Mouse.Type": "",
        f"Device.{JOY}.Type": "2", f"Device.{JOY}.NoAutoLayout": "", f"Device.{JOY}.Name": "Xbox 360 Controller #1",
        "Mapping.LeftFlipper": f"Key;225 | {JOY};514;o;-0.300000", "Mapping.LeftMagna": f"Key;224 | {JOY};4",
        "Mapping.LaunchBall": f"Key;40 | {JOY};0", "Mapping.Start": f"Key;30 | {JOY};1",
        "Mapping.Plunger0.Position": f"{JOY};516;P;0.000000;1.000000;1.000000", "Mapping.Plunger0.Velocity": "",
        "Mapping.Nudge0.Type": "0", "Mapping.Nudge0.Strength": "1.0", "Mapping.Nudge0.CabWeight": "",
        "Mapping.Nudge0.X": f"{JOY};512;P;0.000000;1.000000;1.000000", "Mapping.Nudge0.Y": f"{JOY};513;P;0.000000;1.000000;1.000000",
        f"Device.{JOY}.Element0": "512;A;Left Stick X", f"Device.{JOY}.Element6": "0;B;A",
    }

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        doc = IniDocument(TEMPLATE.read_text())
        for key, value in self.LINES.items():
            doc.set("Input", key, value)
        self.ini = self.dir / "controller.ini"
        self.ini.write_text(doc.to_text())
        self.state = self.dir / "state.json"
        self.app = server.App(TEMPLATE, self.dir / "out.new", self.state)

    def input_step(self):
        return next(s for s in self.app.steps if s["id"] == "input")

    def test_a_fresh_start_has_no_device_specific_fields(self):
        self.assertEqual(len(self.app.fields), 163)
        self.assertEqual([g["title"] for g in self.input_step()["groups"]][-1], "Custom buttons")

    def test_loading_the_file_adds_its_lines_typed_from_vpxs_definitions(self):
        self.app.set_base(str(self.ini))
        f = self.app.fields
        joy = f"Input.Device.{self.JOY}"
        self.assertEqual((f[joy + ".Type"]["type"], [o["label"] for o in f[joy + ".Type"]["options"]][2]), ("select", "Joystick"))
        self.assertEqual(f[joy + ".NoAutoLayout"]["type"], "bool")
        self.assertEqual(f[joy + ".Name"]["type"], "text")
        self.assertEqual(f[joy + ".Element0"]["group"], "Device elements (written by VPX)")
        self.assertEqual((f["Input.Mapping.Nudge0.Type"]["default_label"], f["Input.Mapping.Nudge0.Type"]["options"][2]["label"]),
                         ("Game Controller", "Cabinet Sensor"))
        strength, weight = f["Input.Mapping.Nudge0.Strength"], f["Input.Mapping.Nudge0.CabWeight"]
        self.assertEqual((strength["type"], strength["default"], strength["max"]), ("float", "1.0", "2.0"))
        self.assertEqual((weight["default"], weight["max"]), ("113.0", "200.0"))
        self.assertEqual(f["Input.Mapping.Plunger0.Position"]["type"], "text")
        self.assertEqual(f["Input.Mapping.Nudge0.X"]["group"], "Plunger and nudge sensors")
        # the file's own lines are shown as they are: the label is the key
        self.assertEqual(f[joy + ".Name"]["label"], f"Device.{self.JOY}.Name")

    def test_values_are_shown_as_in_the_file_and_nothing_changes_until_edited(self):
        st = self.app.set_base(str(self.ini))
        v = st["values"]
        self.assertEqual(v["Input.Devices"], f"Key;{self.JOY}")
        self.assertEqual(v[f"Input.Device.{self.JOY}.Name"], "Xbox 360 Controller #1")
        self.assertEqual(v["Input.Mapping.LeftFlipper"], f"Key;225 | {self.JOY};514;o;-0.300000")
        self.assertEqual(v["Input.Mapping.Plunger0.Position"], f"{self.JOY};516;P;0.000000;1.000000;1.000000")
        preview = self.app.preview()
        self.assertEqual((preview["changes"], preview["problems"]), ([], []))
        self.assertEqual(preview["text"], self.ini.read_text())

    def test_editing_touches_only_that_line_and_is_validated(self):
        self.app.set_base(str(self.ini))
        self.app.update_state({"values": {
            "Input.Mapping.LeftFlipper": f"Key;20 | {self.JOY};514;o;-0.300000",     # remap: only the key part changes
            "Input.Mapping.Nudge0.CabWeight": "120", "Input.Mapping.Nudge0.Strength": "5",
            f"Input.Device.{self.JOY}.Name": "My pad"}})
        preview = self.app.preview()
        self.assertEqual([p["label"] for p in preview["problems"]], ["Mapping.Nudge0.Strength"])   # 5 is above the max of 2
        self.app.update_state({"values": {"Input.Mapping.Nudge0.Strength": "1.5"}})
        out = IniDocument(self.app.preview()["text"])
        self.assertEqual(out.get("Input", "Mapping.LeftFlipper"), f"Key;20 | {self.JOY};514;o;-0.300000")
        self.assertEqual(out.get("Input", "Mapping.Nudge0.CabWeight"), "120")
        self.assertEqual(out.get("Input", f"Device.{self.JOY}.Name"), "My pad")
        original = IniDocument(self.ini.read_text())
        changed = [k for k in original.keys("Input") if original.get("Input", k) != out.get("Input", k)]
        self.assertEqual(sorted(changed), sorted(["Mapping.LeftFlipper", "Mapping.Nudge0.CabWeight", "Mapping.Nudge0.Strength",
                                                  f"Device.{self.JOY}.Name"]))

    def test_going_back_to_the_default_base_drops_the_extra_fields(self):
        self.app.set_base(str(self.ini))
        self.assertGreater(len(self.app.fields), 163)
        self.app.set_base("")
        self.assertEqual(len(self.app.fields), 163)
        self.assertFalse([k for k in self.app.state["values"] if self.JOY in k])

    def test_the_extra_fields_and_edits_survive_a_restart(self):
        self.app.set_base(str(self.ini))
        self.app.update_state({"values": {f"Input.Device.{self.JOY}.Name": "My pad"}})
        again = server.App(TEMPLATE, self.dir / "out.new", self.state)
        self.assertIn(f"Input.Device.{self.JOY}.Name", again.fields)
        self.assertEqual(again.state["values"][f"Input.Device.{self.JOY}.Name"], "My pad")
        self.assertEqual(any(g.get("collapsed") for g in next(s for s in again.steps if s["id"] == "input")["groups"]), True)

    def test_an_unknown_mapping_or_line_is_still_shown(self):
        doc = IniDocument(self.ini.read_text())
        doc.set("Input", "Mapping.SomeNewAction", "Key;9")
        doc.set("Input", "SomeOtherLine", "hello")
        self.ini.write_text(doc.to_text())
        self.app.set_base(str(self.ini))
        self.assertEqual(self.app.fields["Input.Mapping.SomeNewAction"]["type"], "mapping")
        self.assertEqual(self.app.fields["Input.SomeOtherLine"]["type"], "text")
        self.assertEqual(self.app.state["values"]["Input.SomeOtherLine"], "hello")


class PluginPagesTests(unittest.TestCase):
    """Each plugin has its own page, listed under the "Plugins" header of the sidebar."""

    @classmethod
    def setUpClass(cls):
        cls.app = server.App(TEMPLATE, "x", tempfile.mkdtemp() + "/s.json")
        cls.pages = [s for s in cls.app.steps if s.get("parent") == "Plugins"]

    def test_one_page_per_plugin_in_order(self):
        from vpxconfig.plugins import EXTRA_PLUGINS, PLUGINS
        self.assertEqual([s["title"] for s in self.pages], PLUGINS + [name for name, _, _ in EXTRA_PLUGINS])
        self.assertEqual(len(self.pages), 19)
        self.assertEqual(len({s["id"] for s in self.app.steps}), len(self.app.steps))          # ids unique
        self.assertEqual(self.pages[0]["id"], "plugin-altsound")
        self.assertNotIn("plugins", [s["id"] for s in self.app.steps])                          # no single Plugins page any more

    def test_the_pages_are_consecutive_between_input_and_review(self):
        ids = [s["id"] for s in self.app.steps]
        self.assertEqual(ids[:6], ["start", "general", "playfield", "backglass", "scoreview", "input"])
        self.assertEqual(ids[-1], "review")
        self.assertEqual([s["id"] for s in self.app.steps[6:-1]], [p["id"] for p in self.pages])

    def test_each_page_has_the_plugins_settings_and_a_description(self):
        for page in self.pages:
            self.assertEqual(len(page["groups"]), 1, page["title"])
            fields = page["groups"][0]["fields"]
            self.assertEqual(fields[0]["key"], "Enable", page["title"])
            self.assertTrue(all(f["section"] == f"Plugin.{page['title']}" for f in fields), page["title"])
            self.assertTrue(all(f["step"] == page["title"] and f["parent"] == "Plugins" for f in fields), page["title"])
            self.assertTrue(page["description"], page["title"])
        on_pages = [f["id"] for p in self.pages for f in p["groups"][0]["fields"]]
        self.assertEqual(sorted(on_pages), sorted(fid for fid, f in self.app.fields.items() if f["section"].startswith("Plugin.")))
        by_title = {p["title"]: len(p["groups"][0]["fields"]) for p in self.pages}
        self.assertEqual((by_title["DMDUtil"], by_title["B2SLegacy"], by_title["PUP"], by_title["Inspector"], by_title["WMP"]),
                         (24, 23, 15, 2, 1))

    def test_review_names_carry_the_plugins_header(self):
        self.app.update_state({"values": {"Plugin.PUP.MainVol": "0.5"}})
        change = next(c for c in self.app.preview()["changes"] if c["key"] == "MainVol")
        self.assertEqual((change["parent"], change["step"], change["group"]), ("Plugins", "PUP", "PUP"))

    def test_the_scoreview_window_page_and_the_scoreview_plugin_page_are_different(self):
        window = next(s for s in self.app.steps if s["id"] == "scoreview")
        plugin = next(s for s in self.app.steps if s["id"] == "plugin-scoreview")
        self.assertEqual((window.get("parent", ""), plugin["parent"]), ("", "Plugins"))
        self.assertIn("ScoreView.ScoreViewOutput", [f["id"] for g in window["groups"] for f in g["fields"]])
        self.assertIn("Plugin.ScoreView.LayoutFolder", [f["id"] for g in plugin["groups"] for f in g["fields"]])


class OutputFileTests(unittest.TestCase):
    """Where the new ini is written: VPX's own settings location by default, or a path the user chooses."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.home = self.dir / "home"
        self.home.mkdir()
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)                      # never touch the real ~/.local/share
        self.addCleanup(lambda: os.environ.__setitem__("HOME", old_home) if old_home is not None else os.environ.pop("HOME", None))
        self.state = self.dir / "state.json"
        self.app = server.App(TEMPLATE, None, self.state)         # no explicit output: the default location
        self.default = self.home / ".local" / "share" / "VPinballX" / "10.8" / "VPinballX.ini"

    def change_something(self):
        self.app.update_state({"values": {"Player.BGSet": "1"}})

    def test_the_default_is_where_vpx_keeps_its_settings(self):
        self.assertEqual(server.default_output(), self.default)
        out = self.app.public_state()["output"]
        self.assertEqual((out["path"], out["name"], out["is_default"], out["exists"], out["folder_exists"]),
                         (str(self.default), "VPinballX.ini", True, False, False))

    def test_saving_to_the_default_creates_the_folders_and_the_file(self):
        self.change_something()
        r = self.app.save()
        self.assertEqual((r["path"], r["count"], r["backup"]), (str(self.default), 1, ""))
        self.assertEqual(IniDocument.from_file(self.default).get("Player", "BGSet"), "1")
        self.assertEqual(list(self.default.parent.iterdir()), [self.default])                 # no leftover .tmp file

    def test_an_existing_file_is_backed_up_and_keeps_its_permissions(self):
        self.default.parent.mkdir(parents=True)
        self.default.write_text("[Player]\nold = 1\n")
        self.default.chmod(0o600)
        self.change_something()
        r = self.app.save()
        backup = Path(r["backup"])
        self.assertEqual(backup, self.default.with_name("VPinballX.ini.vpconfigbackup"))
        self.assertEqual(backup.read_text(), "[Player]\nold = 1\n")                            # the previous content
        self.assertEqual(stat.S_IMODE(self.default.stat().st_mode), 0o600)
        self.assertEqual(IniDocument.from_file(self.default).get("Player", "BGSet"), "1")

    def test_a_different_backup_is_never_overwritten(self):
        self.default.parent.mkdir(parents=True)
        self.default.write_text("first\n")
        self.change_something()
        first = Path(self.app.save()["backup"])
        self.default.write_text("second\n")                     # the file changed since the first backup
        second = Path(self.app.save()["backup"])
        self.assertNotEqual(first, second)
        self.assertEqual((first.read_text(), second.read_text()), ("first\n", "second\n"))

    def test_choosing_another_file(self):
        target = self.dir / "elsewhere" / "my.ini"
        out = self.app.set_output(str(target))["output"]
        self.assertEqual((out["path"], out["is_default"], out["override"], out["folder_exists"]), (str(target), False, str(target), False))
        self.change_something()
        self.app.save()
        self.assertTrue(target.is_file())
        self.assertFalse(self.default.exists())                  # the default location was not touched

    def test_blank_goes_back_to_the_default(self):
        self.app.set_output(str(self.dir / "my.ini"))
        out = self.app.set_output("  ")["output"]
        self.assertEqual((out["path"], out["is_default"]), (str(self.default), True))

    def test_bad_output_paths(self):
        (self.dir / "afolder").mkdir()
        for path, msg in [("relative/x.ini", "full path"), (str(self.dir / "afolder"), "folder"),
                          (str(self.dir / "x.ini.vpconfigbackup"), "backup file name"), (str(TEMPLATE), "never overwritten")]:
            with self.assertRaises(ValueError) as cm:
                self.app.set_output(path)
            self.assertIn(msg, str(cm.exception))
        self.assertTrue(self.app.public_state()["output"]["is_default"])                     # failures change nothing

    def test_the_base_file_can_never_be_the_output(self):
        via_dots = str(TEMPLATE.parent / "web" / ".." / TEMPLATE.name)
        with self.assertRaises(ValueError):
            self.app.set_output(via_dots)
        link = self.dir / "link.ini"
        link.symlink_to(TEMPLATE)
        with self.assertRaises(ValueError):
            self.app.set_output(str(link))

    def test_tilde_and_quotes_are_accepted(self):
        self.assertEqual(self.app.set_output('"~/mine/VPinballX.ini"')["output"]["path"], str(self.home / "mine" / "VPinballX.ini"))

    def test_the_choice_survives_a_restart_and_a_bad_saved_path_falls_back(self):
        self.app.set_output(str(self.dir / "my.ini"))
        again = server.App(TEMPLATE, None, self.state)
        self.assertEqual(again.public_state()["output"]["path"], str(self.dir / "my.ini"))
        saved = json.loads(self.state.read_text())
        saved["output_path"] = str(TEMPLATE)                    # e.g. edited by hand
        self.state.write_text(json.dumps(saved))
        self.assertTrue(server.App(TEMPLATE, None, self.state).public_state()["output"]["is_default"])

    def test_nothing_is_written_or_backed_up_while_a_value_is_not_acceptable(self):
        self.default.parent.mkdir(parents=True)
        self.default.write_text("keep\n")
        self.app.update_state({"values": {"Player.PlayfieldFSWidth": "99999"}})
        r = self.app.save()
        self.assertIn("not acceptable", r["error"])
        self.assertEqual(self.default.read_text(), "keep\n")
        self.assertFalse(self.default.with_name("VPinballX.ini.vpconfigbackup").exists())

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_an_unwritable_folder_is_reported_and_changes_nothing(self):
        ro = self.dir / "ro"
        ro.mkdir()
        target = ro / "VPinballX.ini"
        target.write_text("keep\n")
        ro.chmod(0o555)
        try:
            self.app.set_output(str(target))
            self.change_something()
            r = self.app.save()
            self.assertIn("Could not", r["error"])
            self.assertEqual(target.read_text(), "keep\n")
        finally:
            ro.chmod(0o755)

    def test_loading_a_file_and_saving_over_it_keeps_the_original_as_a_backup(self):
        mine = self.dir / "mine.ini"
        mine.write_text(TEMPLATE.read_text())
        self.app.set_base(str(mine))
        self.app.set_output(str(mine))
        self.change_something()
        r = self.app.save()
        self.assertEqual(Path(r["backup"]).read_text(), TEMPLATE.read_text())
        self.assertEqual(IniDocument.from_file(mine).get("Player", "BGSet"), "1")

    def test_folder_listing_can_start_in_a_folder_that_does_not_exist_yet(self):
        missing = self.home / ".local" / "share" / "VPinballX" / "10.8"
        with self.assertRaises(ValueError):
            fs.list_dir(str(missing))
        self.assertEqual(fs.list_dir(str(missing), nearest=True)["path"], str(self.home))


class OutputApiTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        app = server.App(TEMPLATE, self.dir / "default.ini", self.dir / "state.json")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app, None))
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.h = {"X-VPX-Config": "1", "Content-Type": "application/json"}

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def call(self, method, path, body=None, headers=None):
        req = urllib.request.Request(self.base + path, method=method, headers=headers or {},
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_state_reports_the_output_and_it_can_be_changed(self):
        code, st = self.call("GET", "/api/state")
        self.assertEqual((st["output"]["path"], st["output"]["is_default"]), (str(self.dir / "default.ini"), True))
        self.assertEqual(self.call("POST", "/api/output", {"path": str(self.dir / "x.ini")})[0], 403)      # needs the header
        code, st = self.call("POST", "/api/output", {"path": str(self.dir / "x.ini")}, self.h)
        self.assertEqual((code, st["output"]["path"]), (200, str(self.dir / "x.ini")))
        code, err = self.call("POST", "/api/output", {"path": "relative.ini"}, self.h)
        self.assertEqual(code, 400)
        self.assertIn("full path", err["error"])

    def test_save_goes_to_the_chosen_file(self):
        self.call("POST", "/api/output", {"path": str(self.dir / "x.ini")}, self.h)
        self.call("PUT", "/api/state", {"values": {"Player.BGSet": "1"}}, self.h)
        code, r = self.call("POST", "/api/save", {}, self.h)
        self.assertEqual((code, r["path"], r["count"]), (200, str(self.dir / "x.ini"), 1))
        self.assertFalse((self.dir / "default.ini").exists())

    def test_fs_nearest(self):
        code, r = self.call("GET", f"/api/fs?path={self.dir}/no/such/folder&nearest=1")
        self.assertEqual((code, r["path"]), (200, str(self.dir)))
        self.assertEqual(self.call("GET", f"/api/fs?path={self.dir}/no/such/folder")[0], 400)


class ReleaseTests(unittest.TestCase):
    """Versioning, the one-file executable's file locations, and the release tooling."""

    ROOT = HERE.parent

    def run_tool(self, *args):
        return subprocess.run([sys.executable, str(self.ROOT / "tools" / "check_version.py"), *args], capture_output=True, text=True)

    def test_the_version_looks_like_0_5_or_0_5_1(self):
        import vpxconfig
        self.assertRegex(vpxconfig.__version__, r"^\d+\.\d+(\.\d+)?$")

    def test_the_version_is_shown_on_the_command_line_and_by_the_api(self):
        import vpxconfig
        out = subprocess.run([sys.executable, str(self.ROOT / "run.py"), "--version"], capture_output=True, text=True)
        self.assertEqual((out.returncode, out.stdout.strip()), (0, f"VPXConfig {vpxconfig.__version__}"))
        app = server.App(TEMPLATE, None, Path(tempfile.mkdtemp()) / "s.json")
        self.assertEqual(app.info()["version"], vpxconfig.__version__)

    def test_the_web_page_shows_the_version(self):
        self.assertIn("`Version: v${s.version}`", (self.ROOT / "web" / "app.js").read_text())
        self.assertNotIn("Visual Pinball X", (self.ROOT / "web" / "index.html").read_text())     # no leftover tagline

    def test_the_tag_check(self):
        import vpxconfig
        v = vpxconfig.__version__
        ok = self.run_tool(f"v{v}")
        self.assertEqual((ok.returncode, "matches" in ok.stdout), (0, True))
        for bad in (f"v{v}.1", v, f"V{v}", f"v.{v}", "v99.99"):
            r = self.run_tool(bad)
            self.assertNotEqual(r.returncode, 0, bad)
            self.assertIn(f"expected v{v}", r.stderr)
        self.assertNotEqual(self.run_tool().returncode, 0)                       # no tag given

    def test_paths_from_source_use_the_project_folder(self):
        from vpxconfig import paths
        self.assertFalse(paths.frozen())
        self.assertEqual(paths.resource_dir(), paths.PROJECT)
        self.assertEqual(paths.state_path(), paths.PROJECT / "state.json")

    def test_paths_in_the_executable(self):
        """Bundled files come from PyInstaller's unpack folder; the state goes to the config folder, which survives runs."""
        from unittest import mock
        from vpxconfig import paths
        bundle = self.ROOT / "web"
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "_MEIPASS", str(bundle), create=True):
            self.assertEqual(paths.resource_dir(), bundle)
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/somewhere/config"}):
                self.assertEqual(paths.state_path(), Path("/somewhere/config/vpxconfig/state.json"))
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative/is/ignored"}):
                self.assertEqual(paths.state_path(), Path.home() / ".config" / "vpxconfig" / "state.json")
            env = {k: v for k, v in os.environ.items() if k != "XDG_CONFIG_HOME"}
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(paths.state_path(), Path.home() / ".config" / "vpxconfig" / "state.json")

    def test_the_state_folder_is_created_when_needed(self):
        state = Path(tempfile.mkdtemp()) / "config" / "vpxconfig" / "state.json"      # neither folder exists yet
        app = server.App(TEMPLATE, None, state)
        app.update_state({"values": {"Player.BGSet": "1"}})
        self.assertEqual(json.loads(state.read_text())["values"]["Player.BGSet"], "1")


    def load_tool(self, name):
        spec = importlib.util.spec_from_file_location(name, self.ROOT / "tools" / f"{name}.py")
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        return tool

    def test_set_version_accepts_only_real_versions(self):
        tool = self.load_tool("set_version")
        for text, want in (("0.6", "0.6"), ("v0.6", "0.6"), (" 1.2.3 ", "1.2.3"), ("v10.20", "10.20")):
            self.assertEqual(tool.normalise(text), want)
        for bad in ("", "v", "0", "0.", "v.0.6", "0.6-rc1", "0.6.1.2", "abc", "0.6; rm -rf /", "$(id)", "0.6\n1.0"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                tool.normalise(bad)

    def test_set_version_rewrites_only_the_version_line(self):
        tool = self.load_tool("set_version")
        source = (self.ROOT / "vpxconfig" / "__init__.py").read_text()
        new = tool.with_version(source, "0.6")
        self.assertIn('__version__ = "0.6"', new)
        self.assertEqual(len(new.splitlines()), len(source.splitlines()))
        with self.assertRaises(ValueError):
            tool.with_version("nothing here", "0.6")

    def test_set_version_command_line(self):
        target = Path(tempfile.mkdtemp()) / "__init__.py"
        target.write_text('"""doc"""\n__version__ = "0.5"\n')
        run = lambda *a: subprocess.run([sys.executable, str(self.ROOT / "tools" / "set_version.py"), *a], capture_output=True, text=True)
        self.assertEqual(run("v0.7", str(target)).returncode, 0)
        self.assertEqual(target.read_text(), '"""doc"""\n__version__ = "0.7"\n')
        bad = run("not-a-version", str(target))
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("is not a version", bad.stderr)
        self.assertEqual(target.read_text(), '"""doc"""\n__version__ = "0.7"\n')       # unchanged after a bad input
        self.assertNotEqual(run().returncode, 0)

    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML not installed")
    def test_the_workflow_avoids_the_github_deprecation_warnings(self):
        """Node 20 actions are deprecated (these are the first major versions that run on Node 24), and "-latest" runner
        labels move to new Ubuntu releases on GitHub's schedule."""
        import re
        import yaml
        wf = yaml.safe_load((self.ROOT / ".github" / "workflows" / "release.yml").read_text())
        first_node24 = {"actions/checkout": 5, "actions/setup-python": 6, "actions/upload-artifact": 6, "actions/download-artifact": 7}
        seen = set()
        for job in wf["jobs"].values():
            self.assertNotIn("latest", job["runs-on"])
            for step in job["steps"]:
                if "uses" in step:
                    action, _, ref = step["uses"].partition("@")
                    self.assertIn(action, first_node24, f"unexpected action {action}")
                    self.assertRegex(ref, r"^v\d+$")
                    self.assertGreaterEqual(int(ref[1:]), first_node24[action], step["uses"])
                    seen.add(action)
        self.assertEqual(seen, set(first_node24))

    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML not installed")
    def test_the_manual_run_asks_for_a_version(self):
        import yaml
        wf = yaml.safe_load((self.ROOT / ".github" / "workflows" / "release.yml").read_text())
        inputs = wf.get("on", wf.get(True))["workflow_dispatch"]["inputs"]
        self.assertEqual((inputs["version"]["required"], inputs["version"]["type"]), (True, "string"))
        self.assertEqual((inputs["publish"]["type"], inputs["publish"]["default"]), ("boolean", False))   # off unless ticked
        # the typed version reaches the shell only through an environment variable, never pasted into a script
        for job in wf["jobs"].values():
            for step in job["steps"]:
                self.assertNotIn("inputs.", step.get("run", ""), step.get("name"))
        text = (self.ROOT / ".github" / "workflows" / "release.yml").read_text()
        self.assertIn("tools/set_version.py", text)
        release = wf["jobs"]["release"]
        self.assertIn("inputs.publish", release["if"])                    # a manual run publishes only when asked
        self.assertIn("refs/tags/", release["if"])

    def test_the_spec_bundles_what_the_app_reads_at_runtime(self):
        spec = (self.ROOT / "vpxconfig.spec").read_text()
        self.assertIn('("web", "web")', spec)
        self.assertIn('("VPinballX.ini", ".")', spec)
        self.assertIn('["run.py"]', spec)
        self.assertTrue((self.ROOT / "web").is_dir() and (self.ROOT / "VPinballX.ini").is_file())
        self.assertIn("pyinstaller", (self.ROOT / "requirements-build.txt").read_text().lower())

    def test_the_executable_is_simply_called_vpxconfig(self):
        """Linux x86_64 only, and the version is in the release and in --version, so the file name carries neither."""
        build = (self.ROOT / "tools" / "build.sh").read_text()
        self.assertNotIn("linux-", build)
        self.assertNotIn("${VERSION}", build)
        self.assertIn("sha256sum vpxconfig > vpxconfig.sha256", build)
        self.assertIn('name="vpxconfig"', (self.ROOT / "vpxconfig.spec").read_text().replace("\n    ", " "))
        for name in ("release.yml",):
            text = (self.ROOT / ".github" / "workflows" / name).read_text()
            self.assertIn("dist/vpxconfig", text)
            self.assertNotIn("linux-x86_64 ", text.replace("vpxconfig-linux-x86_64", ""))   # no platform suffix on the file itself
            self.assertNotIn("vpxconfig-v", text)

    def test_the_build_uses_a_virtual_environment_not_the_system_python(self):
        build = (self.ROOT / "tools" / "build.sh").read_text()
        self.assertIn("-m venv .venv", build)
        self.assertIn(".venv/bin/python -m pip install", build)
        self.assertIn(".venv/", (self.ROOT / ".gitignore").read_text())

    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML not installed")
    def test_the_release_workflow(self):
        import yaml
        wf = yaml.safe_load((self.ROOT / ".github" / "workflows" / "release.yml").read_text())
        triggers = wf.get("on", wf.get(True))
        self.assertEqual(triggers["push"]["tags"], ["v*"])
        self.assertIn("workflow_dispatch", triggers)
        build, release = wf["jobs"]["build"], wf["jobs"]["release"]
        commands = " ".join(step.get("run", "") for step in build["steps"])
        for needed in ("check_version.py", "unittest discover", "tools/build.sh", "smoke_test_exe.sh"):
            self.assertIn(needed, commands)
        self.assertEqual(release["needs"], "build")
        self.assertIn("refs/tags/", release["if"])                       # only tags publish; a manual run just builds
        self.assertEqual(release["permissions"], {"contents": "write"})
        self.assertEqual(wf["permissions"], {"contents": "read"})        # least privilege everywhere else
        self.assertIn("gh release create", " ".join(step.get("run", "") for step in release["steps"]))


class ShutdownTests(unittest.TestCase):
    """The web page's "Quit" button: POST /api/shutdown stops the server."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        app = server.App(TEMPLATE, self.dir / "out.ini", self.dir / "state.json")
        self.httpd = server.create_server(app, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self):
        if self.thread.is_alive():
            self.httpd.shutdown()
            self.thread.join(5)
        self.httpd.server_close()

    def post(self, headers=None):
        req = urllib.request.Request(self.base + "/api/shutdown", method="POST", data=b"{}", headers=headers or {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def test_the_endpoint_stops_the_server_after_answering(self):
        code, body = self.post({"X-VPX-Config": "1", "Content-Type": "application/json"})
        self.assertEqual((code, body), (200, {"ok": True}))
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive(), "serve_forever did not return")
        with self.assertRaises(OSError):                                    # nothing is listening any more
            urllib.request.urlopen(self.base + "/api/state", timeout=2)

    def test_it_is_protected_like_the_other_write_actions(self):
        code, body = self.post()                                            # no X-VPX-Config header: a page on another site can't do this
        self.assertEqual(code, 403)
        code, body = self.post({"X-VPX-Config": "1", "Host": "evil.example:80"})    # DNS-rebinding style request
        self.assertEqual(code, 403)
        self.thread.join(0.5)
        self.assertTrue(self.thread.is_alive())                             # still running after both refusals
        self.assertEqual(urllib.request.urlopen(self.base + "/api/state").status, 200)

    def test_a_get_does_not_stop_it(self):
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.base + "/api/shutdown")
        self.assertTrue(self.thread.is_alive())

    def test_the_endpoint_is_off_when_no_shutdown_callback_is_given(self):
        app = server.App(TEMPLATE, self.dir / "o.ini", self.dir / "s.json")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app, None))     # e.g. the other tests' servers
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}/api/shutdown", method="POST", data=b"{}",
                                         headers={"X-VPX-Config": "1"})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req)
            with cm.exception:
                self.assertEqual(cm.exception.code, 404)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_the_page_has_the_button_and_asks_first(self):
        web = HERE.parent / "web"
        self.assertIn('id="shutdown"', (web / "index.html").read_text())
        js = (web / "app.js").read_text()
        self.assertIn('"/api/shutdown"', js)
        self.assertLess(js.index("confirm(\"Quit VPXConfig?"), js.index('"/api/shutdown"'))      # the question comes before the request
        self.assertIn("VPXConfig has stopped", js)


class FolderListingTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        (self.d / "sub").mkdir()
        (self.d / ".hiddendir").mkdir()
        (self.d / "Zed").mkdir()
        for name in ("VPinballX.ini", "VPinballX.ini.mine", "notes.txt", ".hidden.ini", "x.INI.bak"):
            (self.d / name).write_text("x")

    def names(self, **kw):
        return [(e["name"], e["dir"]) for e in fs.list_dir(str(self.d), **kw)["entries"]]

    def test_folders_first_then_ini_files_and_hidden_shown_by_default(self):
        self.assertEqual(self.names(), [(".hiddendir", True), ("sub", True), ("Zed", True), (".hidden.ini", False),
                                        ("VPinballX.ini", False), ("VPinballX.ini.mine", False), ("x.INI.bak", False)])

    def test_hide_hidden_and_show_all(self):
        self.assertEqual([n for n, _ in self.names(show_hidden=False)],
                         ["sub", "Zed", "VPinballX.ini", "VPinballX.ini.mine", "x.INI.bak"])
        self.assertIn(("notes.txt", False), self.names(show_all=True))

    def test_a_file_path_lists_its_folder_with_the_file_selected(self):
        r = fs.list_dir(str(self.d / "VPinballX.ini"))
        self.assertEqual((r["path"], r["selected"]), (str(self.d), "VPinballX.ini"))
        self.assertEqual(r["parent"], str(self.d.parent))
        self.assertEqual(r["crumbs"][-1], {"name": self.d.name, "path": str(self.d)})

    def test_blank_is_home_and_root_has_no_parent(self):
        self.assertEqual(fs.list_dir("")["path"], str(Path.home()))
        self.assertEqual(fs.list_dir("/")["parent"], "")

    def test_errors(self):
        for bad, msg in [("relative/dir", "full path"), (str(self.d / "missing"), "No folder")]:
            with self.assertRaises(ValueError) as cm:
                fs.list_dir(bad)
            self.assertIn(msg, str(cm.exception))

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_unreadable_folder(self):
        locked = self.d / "locked"
        locked.mkdir()
        locked.chmod(0)
        try:
            with self.assertRaises(ValueError) as cm:
                fs.list_dir(str(locked))
            self.assertIn("Permission denied", str(cm.exception))
        finally:
            locked.chmod(0o755)


class SystemTests(unittest.TestCase):
    def test_parse_wayland_outputs(self):
        outs = system.parse_wayland_outputs((HERE / "wayland_info_output.txt").read_text())
        self.assertEqual([o["name"] for o in outs], ["DP-2", "DP-3", "HDMI-A-1"])
        lg = outs[0]
        self.assertEqual(lg["description"], "LG Electronics LG HDR 4K 0x00025EAC (DP-2)")
        self.assertEqual((lg["width"], lg["height"], lg["x"]), (3840, 2160, 0))
        hdmi = outs[2]
        self.assertEqual((hdmi["width"], hdmi["height"], hdmi["x"]), (1920, 1080, 5760))
        self.assertEqual(outs[1]["physical_width_mm"], 600)
        self.assertEqual(outs[1]["refresh_hz"], 60.0)

    def test_monitor_list_matches_the_grep_command(self):
        text = (HERE / "wayland_info_output.txt").read_text()
        grep = subprocess.run(["grep", "-oP", "description: '\\K[^']+"], input=text, capture_output=True, text=True).stdout
        self.assertEqual(sorted(m["description"] for m in system.parse_wayland_outputs(text)), sorted(grep.splitlines()))

    def test_monitors_without_geometry_are_still_listed(self):
        outs = system.parse_wayland_outputs("description: 'Foo Bar (HDMI-A-2)'\ndescription: 'Baz (eDP-1)'\n")
        self.assertEqual([(o["name"], o["description"]) for o in outs],
                         [("HDMI-A-2", "Foo Bar (HDMI-A-2)"), ("eDP-1", "Baz (eDP-1)")])

    # ---- robustness on other compositors (e.g. Weston): sockets, unquoted output, diagnostics ------------------------------

    @staticmethod
    def wl_output_only(text):
        """The wayland-info output reduced to its wl_output blocks: no xdg_output, so descriptions are unquoted."""
        keep, on = [], False
        for line in text.splitlines():
            if line.startswith("interface:"):
                on = "'wl_output'" in line
            if on:
                keep.append(line)
        return "\n".join(keep) + "\n"

    def test_unquoted_descriptions_are_found_when_there_is_no_xdg_output(self):
        text = self.wl_output_only((HERE / "wayland_info_output.txt").read_text())
        self.assertNotIn("description: '", text)
        outs = system.parse_wayland_outputs(text)
        self.assertEqual(sorted(o["name"] for o in outs), ["DP-2", "DP-3", "HDMI-A-1"])
        self.assertIn("LG Electronics LG HDR 4K 0x00025EAC (DP-2)", [o["description"] for o in outs])
        self.assertTrue(all(o["width"] > 0 and o["height"] > 0 for o in outs))            # size from the current mode

    def fake_wayland_session(self, sockets, wayland_display=None):
        from unittest import mock
        runtime = Path(tempfile.mkdtemp())
        for name in sockets:
            (runtime / name).write_text("")
        (runtime / "wayland-1.lock").write_text("")
        env = {k: v for k, v in os.environ.items() if k not in ("WAYLAND_DISPLAY", "XDG_RUNTIME_DIR")}
        env["XDG_RUNTIME_DIR"] = str(runtime)
        if wayland_display:
            env["WAYLAND_DISPLAY"] = wayland_display
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_run(self, outputs):
        """outputs: socket name -> stdout (or an error dict). Returns the list of sockets the code asked."""
        from unittest import mock
        asked = []

        def run(argv, display=None):
            asked.append(display)
            out = outputs.get(display, "")
            if isinstance(out, dict):
                return {"ok": False, "stdout": "", "stderr": out["stderr"], "exit_code": 1, "error": out["stderr"], "argv": argv}
            return {"ok": True, "stdout": out, "stderr": "", "exit_code": 0, "error": "", "argv": argv}
        patcher = mock.patch.object(system, "run", run)
        patcher.start()
        self.addCleanup(patcher.stop)
        return asked

    def test_every_socket_is_tried_until_one_has_monitors(self):
        full = (HERE / "wayland_info_output.txt").read_text()
        self.fake_wayland_session(["wayland-0", "wayland-1"])                              # WAYLAND_DISPLAY is not set
        asked = self.fake_run({"wayland-0": "interface: 'wl_seat', version: 8, name: 3\n", "wayland-1": full})
        r = system.displays()
        self.assertEqual((r["ok"], r["wayland_display"], len(r["displays"])), (True, "wayland-1", 3))
        self.assertEqual(asked, ["wayland-0", "wayland-1"])                                # the .lock file is not a socket
        tried = r["diagnostics"]["tried"]
        self.assertEqual([(t["WAYLAND_DISPLAY"], t["monitors"]) for t in tried], [("wayland-0", 0), ("wayland-1", 3)])
        self.assertEqual(r["diagnostics"]["sockets"], ["wayland-0", "wayland-1"])

    def test_the_sessions_own_wayland_display_is_tried_first(self):
        full = (HERE / "wayland_info_output.txt").read_text()
        self.fake_wayland_session(["wayland-0", "wayland-1"], wayland_display="wayland-1")
        asked = self.fake_run({"wayland-0": full, "wayland-1": full})
        self.assertEqual(system.displays()["wayland_display"], "wayland-1")
        self.assertEqual(asked, ["wayland-1"])                                              # found there: nothing else is tried

    def test_a_session_display_without_monitors_falls_back_to_another_socket(self):
        full = (HERE / "wayland_info_output.txt").read_text()
        self.fake_wayland_session(["wayland-0", "wayland-1"], wayland_display="wayland-1")
        asked = self.fake_run({"wayland-1": "", "wayland-0": full})
        r = system.displays()
        self.assertEqual((r["ok"], r["wayland_display"]), (True, "wayland-0"))
        self.assertEqual(asked, ["wayland-1", "wayland-0"])

    def test_no_monitors_anywhere_is_explained(self):
        self.fake_wayland_session(["wayland-0", "wayland-1"])
        self.fake_run({"wayland-0": "interface: 'wl_seat'\n", "wayland-1": "interface: 'wl_seat'\n"})
        r = system.displays()
        self.assertFalse(r["ok"])
        self.assertIn("wayland-0, wayland-1", r["error"])
        self.assertIn("lists no monitors", r["error"])
        self.assertEqual(len(r["diagnostics"]["tried"]), 2)
        self.assertEqual(r["diagnostics"]["tried"][0]["output_start"], ["interface: 'wl_seat'"])

    def test_a_failing_wayland_info_reports_its_own_error_and_what_was_tried(self):
        self.fake_wayland_session(["wayland-0"])
        self.fake_run({"wayland-0": {"stderr": "failed to create display: No such file or directory"}})
        r = system.displays()
        self.assertEqual((r["ok"], r["error"]), (False, "failed to create display: No such file or directory"))
        self.assertEqual(r["diagnostics"]["tried"][0]["exit_code"], 1)

    def test_no_wayland_session_at_all_still_runs_it_once(self):
        self.fake_wayland_session([])
        asked = self.fake_run({None: ""})
        r = system.displays()
        self.assertEqual(asked, [None])
        self.assertFalse(r["ok"])
        self.assertEqual(r["diagnostics"]["WAYLAND_DISPLAY"], "(not set)")

    def test_the_executable_does_not_hand_its_libraries_to_other_programs(self):
        """PyInstaller points LD_LIBRARY_PATH at its bundled libraries; wayland-info must not inherit that."""
        from unittest import mock
        with mock.patch.object(sys, "frozen", True, create=True):
            with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/tmp/_MEI123", "LD_LIBRARY_PATH_ORIG": "/usr/local/lib"}):
                self.assertEqual(system._session_env()["LD_LIBRARY_PATH"], "/usr/local/lib")
                self.assertNotIn("LD_LIBRARY_PATH_ORIG", system._session_env())
            env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH_ORIG"}
            env["LD_LIBRARY_PATH"] = "/tmp/_MEI123"
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertNotIn("LD_LIBRARY_PATH", system._session_env())                # there was none originally
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/my/libs"}):
            self.assertEqual(system._session_env()["LD_LIBRARY_PATH"], "/my/libs")        # from source: untouched

    def test_run_reports_the_exit_code_and_stderr(self):
        r = system.run([sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"])
        self.assertEqual((r["ok"], r["exit_code"], r["stdout"].strip(), r["stderr"].strip(), r["error"]), (False, 3, "out", "err", "err"))

    def test_the_page_shows_what_was_tried_when_detection_fails(self):
        js = (HERE.parent / "web" / "app.js").read_text()
        self.assertIn("What was tried", js)
        self.assertIn("displayDiagnostics", js)

    # ---- the value VPX writes for a display: "<name> [x, y]" (Window.cpp: std::format("{} [{}, {}]", name, x, y)) -------------

    WESTON_LIKE = """interface: 'wl_output', version: 4, name: 20
\tname: HDMI-A-1
\tdescription: LG HDR 4K
\tx: 1920, y: 0, scale: 1,
\tphysical_width: 600 mm, physical_height: 340 mm,
\tmake: 'LG Electronics', model: 'LG HDR 4K',
\tmode:
\t\twidth: 3840 px, height: 2160 px, refresh: 60.000 Hz,
\t\tflags: current
interface: 'wl_output', version: 4, name: 21
\tname: DP-1
\tdescription: VS278
\tx: 0, y: 0, scale: 1,
\tmake: 'Ancor Communications Inc', model: 'VS278',
\tmode:
\t\twidth: 1920 px, height: 1080 px, refresh: 60.000 Hz,
\t\tflags: current
interface: 'zxdg_output_manager_v1', version: 3, name: 9
\txdg_output_v1
\t\toutput: 20
\t\tname: 'HDMI-A-1'
\t\tdescription: 'LG HDR 4K'
\t\tlogical_x: 1920, logical_y: 0
\t\tlogical_width: 1920, logical_height: 1080
\txdg_output_v1
\t\toutput: 21
\t\tname: 'DP-1'
\t\tdescription: 'VS278'
\t\tlogical_x: 0, logical_y: 0
\t\tlogical_width: 1920, logical_height: 1080
"""

    def test_the_display_id_is_the_name_and_the_position(self):
        self.assertEqual(system.display_id("LG HDR 4K", 1920, 0), "LG HDR 4K [1920, 0]")          # the user's playfield
        self.assertEqual(system.display_id("VS278", 0, 0), "VS278 [0, 0]")                          # the user's backglass
        self.assertEqual(system.display_id("Left", -1920, -200), "Left [-1920, -200]")             # a monitor left of / above the origin

    def test_ids_from_a_weston_like_output(self):
        outs = system.parse_wayland_outputs(self.WESTON_LIKE)
        self.assertEqual([o["id"] for o in outs], ["VS278 [0, 0]", "LG HDR 4K [1920, 0]"])       # sorted by position
        self.assertEqual([o["description"] for o in outs], ["VS278", "LG HDR 4K"])
        by_name = {o["name"]: o for o in outs}
        self.assertEqual((by_name["HDMI-A-1"]["width"], by_name["HDMI-A-1"]["height"]), (1920, 1080))   # the logical size

    def test_the_layout_position_wins_over_the_wl_output_geometry_line(self):
        outs = {o["name"]: o for o in system.parse_wayland_outputs((HERE / "wayland_info_output.txt").read_text())}
        self.assertEqual(outs["DP-3"]["id"], "Ancor Communications Inc VS278 G7LMQS077266 (DP-3) [3840, 0]")   # not [0, 0] from wl_output
        self.assertEqual(outs["HDMI-A-1"]["id"], "Audio Processing Technology  Ltd HDMI  (HDMI-A-1) [5760, 0]")
        self.assertEqual(outs["DP-2"]["id"], "LG Electronics LG HDR 4K 0x00025EAC (DP-2) [0, 0]")

    def test_without_an_xdg_output_the_wl_output_position_is_used(self):
        only_wl = self.WESTON_LIKE.split("interface: 'zxdg_output_manager_v1'")[0]
        self.assertEqual([o["id"] for o in system.parse_wayland_outputs(only_wl)], ["VS278 [0, 0]", "LG HDR 4K [1920, 0]"])

    def test_the_page_stores_the_id_and_recognises_older_values(self):
        js = (HERE.parent / "web" / "app.js").read_text()
        self.assertIn("value: m.id, selected: m.id === v", js)                     # the dropdown stores "<name> [x, y]"
        self.assertIn("const looseMon", js)                                         # same monitor without position / at another position
        self.assertIn("position or format differs", js)
        self.assertIn("m.name_source", js)                                         # says where SDL's name came from
        self.assertNotIn("value: m.description", js)

    # ---- the display name is chosen like SDL 3.4 does (SDL_waylandvideo.c), because VPX takes it from SDL_GetDisplayName ----

    @staticmethod
    def one_monitor(version, wl_desc=None, xdg_desc=None, model="MODEL", xdg_pos=(1920, 0), wl_pos=(100, 50), global_id=20):
        """wayland-info -i output text for one monitor: wl_output (advertised `version`) + optional xdg_output block."""
        lines = [f"interface: 'wl_output', version: {version}, name: {global_id}"]
        if version >= 4:
            lines.append("\tname: DP-9")
            if wl_desc:
                lines.append(f"\tdescription: {wl_desc}")
        lines += [f"\tx: {wl_pos[0]}, y: {wl_pos[1]}, scale: 1,", f"\tmake: 'Maker', model: '{model}',", "\tmode:",
                  "\t\twidth: 1920 px, height: 1080 px, refresh: 60.000 Hz,", "\t\tflags: current"]
        if xdg_pos is not None or xdg_desc:
            lines += ["interface: 'zxdg_output_manager_v1', version: 3, name: 9", "\txdg_output_v1", f"\t\toutput: {global_id}",
                      "\t\tname: 'DP-9'"]
            if xdg_desc:
                lines.append(f"\t\tdescription: '{xdg_desc}'")
            if xdg_pos is not None:
                lines += [f"\t\tlogical_x: {xdg_pos[0]}, logical_y: {xdg_pos[1]}", "\t\tlogical_width: 1920, logical_height: 1080"]
        return "\n".join(lines) + "\n"

    def name_of(self, text):
        (m,) = system.parse_wayland_outputs(text)
        return m["description"], m["name_source"]

    def test_wl_output_v4_uses_the_wl_output_description_and_ignores_the_xdg_one(self):
        self.assertEqual(self.name_of(self.one_monitor(4, wl_desc="WL Desc", xdg_desc="XDG Desc")), ("WL Desc", "wl_output description"))

    def test_wl_output_v4_without_a_description_falls_back_to_the_model_even_if_xdg_has_one(self):
        """SDL: the xdg_output description is "deprecated as of wl_output v4" and is not used then."""
        self.assertEqual(self.name_of(self.one_monitor(4, xdg_desc="XDG Desc", model="LG HDR 4K")), ("LG HDR 4K", "wl_output model"))

    def test_older_wl_output_uses_the_xdg_output_description(self):
        self.assertEqual(self.name_of(self.one_monitor(3, xdg_desc="XDG Desc")), ("XDG Desc", "xdg_output description"))
        self.assertEqual(self.name_of(self.one_monitor(2, xdg_desc="XDG Desc")), ("XDG Desc", "xdg_output description"))

    def test_older_wl_output_without_xdg_uses_the_model(self):
        self.assertEqual(self.name_of(self.one_monitor(3, xdg_pos=None, model="VS278")), ("VS278", "wl_output model"))

    def test_nothing_but_a_model(self):
        self.assertEqual(self.name_of(self.one_monitor(4, xdg_pos=None, model="VS278")), ("VS278", "wl_output model"))

    def test_the_position_is_the_layout_position_or_else_the_wl_output_geometry(self):
        with_xdg = system.parse_wayland_outputs(self.one_monitor(4, wl_desc="A", xdg_pos=(1920, 0), wl_pos=(100, 50)))[0]
        self.assertEqual((with_xdg["x"], with_xdg["y"], with_xdg["id"]), (1920, 0, "A [1920, 0]"))
        without = system.parse_wayland_outputs(self.one_monitor(4, wl_desc="A", xdg_pos=None, wl_pos=(100, 50)))[0]
        self.assertEqual((without["x"], without["y"], without["id"]), (100, 50, "A [100, 50]"))

    def test_an_old_wl_output_without_a_name_line_is_still_a_monitor(self):
        """wl_output before v4 has no `name:` line; its xdg_output block (tied by `output: <id>`) supplies the connector name."""
        (m,) = system.parse_wayland_outputs(self.one_monitor(3, xdg_desc="XDG Desc"))
        self.assertEqual((m["name"], m["wl_output_version"], m["width"], m["height"]), ("DP-9", 3, 1920, 1080))
        (bare,) = system.parse_wayland_outputs(self.one_monitor(3, xdg_pos=None))
        self.assertEqual(bare["name"], "output-20")                                   # nothing names it: a stable placeholder

    def test_two_monitors_are_kept_apart(self):
        text = self.one_monitor(4, wl_desc="First", xdg_pos=(0, 0), global_id=20) + self.one_monitor(4, wl_desc="Second", xdg_pos=(1920, 0), global_id=21)
        self.assertEqual([m["id"] for m in system.parse_wayland_outputs(text)], ["First [0, 0]", "Second [1920, 0]"])

    def test_the_hyprland_sample_uses_the_wl_output_description(self):
        outs = system.parse_wayland_outputs((HERE / "wayland_info_output.txt").read_text())
        self.assertEqual({m["name_source"] for m in outs}, {"wl_output description"})
        self.assertEqual({m["wl_output_version"] for m in outs}, {4})

    def test_the_sdl_rule_is_documented_with_its_source(self):
        doc = system.sdl_display_name.__doc__
        self.assertIn("SDL_waylandvideo.c", doc)
        self.assertIn("deprecated as of wl_output v4", doc)

    def test_missing_command_is_reported_not_raised(self):
        r = system.run(["definitely-not-a-real-command"])
        self.assertFalse(r["ok"])
        self.assertIn("not installed", r["error"])


class ServerTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.out = Path(d) / "VPinballX.ini.new"
        app = server.App(TEMPLATE, self.out, Path(d) / "state.json")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app, None))
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def call(self, method, path, body=None, headers=None):
        req = urllib.request.Request(self.base + path, method=method, headers=headers or {},
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read()

    def test_static_and_api(self):
        self.assertEqual(self.call("GET", "/")[0], 200)
        self.assertEqual(self.call("GET", "/app.js")[0], 200)
        code, body = self.call("GET", "/api/steps")
        self.assertEqual(code, 200)
        steps = json.loads(body)["steps"]
        self.assertEqual([s["id"] for s in steps][:3], ["start", "general", "playfield"])
        self.assertEqual(steps[0]["panels"], ["base", "target"])
        self.assertEqual([f["id"] for g in steps[0]["groups"] for f in g["fields"]], [])          # Start: VPXConfig's own settings only
        self.assertEqual([f["id"] for g in steps[1]["groups"] for f in g["fields"]],
                         ["Player.BGSet", "Player.SyncMode", "Player.MaxFramerate", "Player.MaxTexDimension"])

    def test_path_traversal_blocked(self):
        self.assertEqual(self.call("GET", "/../VPinballX.ini")[0], 404)
        self.assertEqual(self.call("GET", "/..%2fVPinballX.ini")[0], 404)

    def test_writes_require_header(self):
        self.assertEqual(self.call("PUT", "/api/state", {"values": {}})[0], 403)
        self.assertEqual(self.call("POST", "/api/save", {})[0], 403)

    def test_save_roundtrip(self):
        h = {"X-VPX-Config": "1", "Content-Type": "application/json"}
        self.call("PUT", "/api/state", {"values": {"Player.BGSet": "1", "Bogus.Key": "x"}}, h)
        code, body = self.call("POST", "/api/save", {}, h)
        self.assertEqual(json.loads(body)["count"], 1)          # just BGSet: the starting answers are already in the base file
        self.assertEqual(IniDocument.from_file(self.out).get("Player", "BGSet"), "1")
        self.assertEqual(IniDocument.from_file(self.out).get("ScoreView", "Priority.PUP"), "3")
        self.assertNotIn(b"Bogus", self.out.read_bytes())

    def test_save_is_refused_while_a_value_is_not_acceptable(self):
        h = {"X-VPX-Config": "1", "Content-Type": "application/json"}
        self.call("PUT", "/api/state", {"values": {"Player.PlayfieldFSWidth": "99999", "Player.BGSet": "1"}}, h)
        code, body = self.call("POST", "/api/save", {}, h)
        r = json.loads(body)
        self.assertEqual(code, 400)
        self.assertEqual([p["id"] for p in r["problems"]], ["Player.PlayfieldFSWidth"])
        self.assertFalse(self.out.exists())
        self.call("PUT", "/api/state", {"values": {"Player.PlayfieldFSWidth": "3840"}}, h)
        self.assertEqual(self.call("POST", "/api/save", {}, h)[0], 200)
        self.assertTrue(self.out.exists())

    def test_fs_endpoint(self):
        d = self.out.parent
        (d / "VPinballX.ini").write_text("x")
        code, body = self.call("GET", "/api/fs?path=" + str(d))
        self.assertEqual(code, 200)
        self.assertIn("VPinballX.ini", [e["name"] for e in json.loads(body)["entries"]])
        code, body = self.call("GET", "/api/fs?path=/definitely/not/here")
        self.assertEqual(code, 400)
        self.assertIn("No folder", json.loads(body)["error"])

    def test_base_endpoint(self):
        h = {"X-VPX-Config": "1", "Content-Type": "application/json"}
        self.assertEqual(self.call("POST", "/api/base", {"path": ""})[0], 403)
        code, body = self.call("POST", "/api/base", {"path": "/definitely/not/here.ini"}, h)
        self.assertEqual(code, 400)
        self.assertIn("No file", json.loads(body)["error"])
        good = self.out.parent / "mine.ini"
        doc = IniDocument(TEMPLATE.read_text())
        doc.set("Player", "BGSet", "1")
        good.write_text(doc.to_text())
        code, body = self.call("POST", "/api/base", {"path": str(good)}, h)
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["values"]["Player.BGSet"], "1")
        self.assertEqual(json.loads(self.call("GET", "/api/state")[1])["base"]["name"], "mine.ini")

    def test_host_header_check(self):
        app = server.App(TEMPLATE, self.out, self.out.with_suffix(".state"))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app, {"localhost:1"}))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(f"http://127.0.0.1:{httpd.server_port}/api/steps")
            self.assertEqual(cm.exception.code, 403)
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
