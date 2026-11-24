"""Comprehensive tests for the Scoped & Versioned Software Seed Template System."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import tomllib
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from obol.cli.main import run_cli
from obol.core.hub import ObolHub
from obol.core.models import TemplateScope
from obol.core.storage import load_component_snapshot
from obol.core.sync import SyncEngine
from obol.core.toml_io import dumps_toml
from obol.core.workspace import ObolWorkspace
from obol.gui.dialogs import (
    AddComponentDialog,
    NewProjectWizardDialog,
    SaveSeedTemplateDialog,
    TemplateManagerDialog,
)


def _make_rpp(marker: str) -> str:
    return (
        '<REAPER_PROJECT 0.1 "7.0" 1732320000\n'
        '  RECORD_PATH "C:/Old/Path" ""\n'
        '  RENDER_FILE "C:/Old/Out"\n'
        f'  <TRACK {{11111111-2222-3333-4444-555555555555}}\n'
        f'    NAME "{marker}"\n'
        "  >\n"
        ">\n"
    )


class TestScopedVersionedSeedTemplates(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.hub = ObolHub(hub_dir=self.root / "hub")
        self.ws = ObolWorkspace(self.hub)
        self.tm = self.hub.template_manager

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_toml_roundtrip_and_readonly_queries_create_no_dirs(self) -> None:
        root_empty = self.root / "empty_root"
        root_empty.mkdir()

        # Read-only queries must never create .obol directories
        resolved = self.tm.resolve_seed_template("reaper", client_root=root_empty)
        self.assertIsNone(resolved)
        avail = self.tm.seeds.list_available_for_software("reaper", client_root=root_empty)
        self.assertEqual(avail, [])
        self.assertFalse((root_empty / ".obol").exists())

        # TOML round-trip with multiple versions
        src = self.root / "seed_v1.rpp"
        src.write_text(_make_rpp("Roundtrip Track"), encoding="utf-8")
        self.tm.seeds.add_version(
            "reaper",
            "default",
            src,
            TemplateScope.CLIENT_ROOT,
            comment="v1 note",
            tags=["48k", "stereo"],
            client_root=root_empty,
        )
        src2 = self.root / "seed_v2.rpp"
        src2.write_text(_make_rpp("Roundtrip Track V2"), encoding="utf-8")
        self.tm.seeds.add_version(
            "reaper",
            "default",
            src2,
            TemplateScope.CLIENT_ROOT,
            comment="v2 note",
            client_root=root_empty,
            set_as_default_version=True,
        )
        man = self.tm.seeds.load_manifest("reaper", TemplateScope.CLIENT_ROOT, client_root=root_empty)
        parsed = tomllib.loads(dumps_toml(man.to_dict()))
        self.assertEqual(parsed["default_template"], "default")
        self.assertEqual(parsed["templates"]["default"]["default_version"], 2)
        self.assertEqual(len(parsed["templates"]["default"]["versions"]), 2)

    def test_per_root_isolation_enclosing_root_and_rpp_patching(self) -> None:
        root_a = self.root / "root_a"
        root_b = self.root / "root_b"
        root_c = self.root / "root_c"
        for d in (root_a, root_b, root_c):
            d.mkdir()

        rpp_a = self.root / "a_template.rpp"
        rpp_b = self.root / "b_template.rpp"
        rpp_a.write_text(_make_rpp("ROOT_A_BUS"), encoding="utf-8")
        rpp_b.write_text(_make_rpp("ROOT_B_BUS"), encoding="utf-8")

        self.tm.seeds.add_version("reaper", "default", rpp_a, TemplateScope.CLIENT_ROOT, client_root=root_a)
        self.tm.seeds.add_version("reaper", "default", rpp_b, TemplateScope.CLIENT_ROOT, client_root=root_b)

        # Point Hub's active client root to root_b, yet create a project inside root_a:
        # enclosing root detection must still pick root_a!
        self.hub.set_client_root(root_b)
        proj_a = self.ws.init_project("show_a", root_a / "show_a", project_template="single_track")
        proj_b = self.ws.init_project("show_b", root_b / "show_b", project_template="single_track")
        proj_c = self.ws.init_project("show_c", root_c / "show_c", project_template="single_track")

        arr_a = (proj_a / "arrangement" / "working" / "v001" / "arrangement_v001.rpp").read_text(encoding="utf-8")
        arr_b = (proj_b / "arrangement" / "working" / "v001" / "arrangement_v001.rpp").read_text(encoding="utf-8")
        arr_c = (proj_c / "arrangement" / "working" / "v001" / "arrangement_v001.rpp").read_text(encoding="utf-8")

        self.assertIn("ROOT_A_BUS", arr_a)
        self.assertIn("ROOT_B_BUS", arr_b)
        # Unconfigured root_c falls back to built-in legacy reaper_default.rpp ("Main Track")
        self.assertIn("Main Track", arr_c)

        # ReaperPlugin must patch RECORD_PATH / RENDER_FILE on the working copy without mutating the stored seed
        self.assertIn('RECORD_PATH "../../repo/recorded"', arr_a)
        self.assertIn('RENDER_FILE "../../published"', arr_a)
        stored_seed_a = (root_a / ".obol" / "templates" / "seeds" / "reaper" / "default" / "v001" / "default_v001.rpp").read_text(encoding="utf-8")
        self.assertIn('RECORD_PATH "C:/Old/Path"', stored_seed_a)

        # Provenance recorded on initial working bundle
        snap_a = load_component_snapshot(proj_a, "arrangement")
        self.assertEqual(snap_a.working_versions[0].custom.get("seed_template"), "seed:client_root:reaper:default:v001")

    def test_three_level_cascade_and_clear_override(self) -> None:
        croot = self.root / "wkly_root"
        croot.mkdir()
        self.hub.set_client_root(croot)

        rpp_global = self.root / "g.rpp"
        rpp_root = self.root / "r.rpp"
        rpp_proj = self.root / "p.rpp"
        rpp_global.write_text(_make_rpp("GLOBAL_SEED"), encoding="utf-8")
        rpp_root.write_text(_make_rpp("ROOT_SEED"), encoding="utf-8")
        rpp_proj.write_text(_make_rpp("PROJECT_SEED"), encoding="utf-8")

        self.tm.seeds.add_version("reaper", "default", rpp_global, TemplateScope.GLOBAL)
        proj = self.ws.init_project("ep_01", croot / "ep_01", project_template="simple_media")
        self.assertIn("GLOBAL_SEED", (proj / "audio" / "working" / "v001" / "audio_v001.rpp").read_text(encoding="utf-8"))

        # Add root default -> overrides global for new components
        self.tm.seeds.add_version("reaper", "default", rpp_root, TemplateScope.CLIENT_ROOT, client_root=croot)
        self.ws.create_component(proj, "sfx", task_template="reaper_daw")
        self.assertIn("ROOT_SEED", (proj / "sfx" / "working" / "v001" / "sfx_v001.rpp").read_text(encoding="utf-8"))

        # Add project override -> overrides root
        self.tm.seeds.add_version("reaper", "custom_ep", rpp_proj, TemplateScope.PROJECT, project_root=proj)
        self.ws.create_component(proj, "music", task_template="reaper_daw")
        self.assertIn("PROJECT_SEED", (proj / "music" / "working" / "v001" / "music_v001.rpp").read_text(encoding="utf-8"))

        # Clear project default override -> re-inherits from Client Root
        self.tm.seeds.set_default_template("reaper", "", TemplateScope.PROJECT, project_root=proj)
        self.ws.create_component(proj, "foley", task_template="reaper_daw")
        self.assertIn("ROOT_SEED", (proj / "foley" / "working" / "v001" / "foley_v001.rpp").read_text(encoding="utf-8"))

    def test_multiple_named_streams_pinning_and_explicit_override(self) -> None:
        croot = self.root / "studio_root"
        croot.mkdir()
        self.hub.set_client_root(croot)

        for ver in (1, 2, 3):
            f = self.root / f"def_{ver}.rpp"
            f.write_text(_make_rpp(f"DEFAULT_V{ver}"), encoding="utf-8")
            self.tm.seeds.add_version("reaper", "default", f, TemplateScope.CLIENT_ROOT, client_root=croot)

        pod = self.root / "pod.rpp"
        pod.write_text(_make_rpp("PODCAST_MIX"), encoding="utf-8")
        self.tm.seeds.add_version(
            "reaper",
            "podcast_mix",
            pod,
            TemplateScope.CLIENT_ROOT,
            client_root=croot,
            set_as_default_template=False,
        )

        proj = self.ws.init_project("album", croot / "album", project_template="simple_media")
        # Always Latest (default_version == 0) picks v003
        self.assertIn("DEFAULT_V3", (proj / "audio" / "working" / "v001" / "audio_v001.rpp").read_text(encoding="utf-8"))

        # Pin v002 -> even after adding v004, new tasks use v002
        self.tm.seeds.set_default_version("reaper", "default", 2, TemplateScope.CLIENT_ROOT, client_root=croot)
        f4 = self.root / "def_4.rpp"
        f4.write_text(_make_rpp("DEFAULT_V4"), encoding="utf-8")
        self.tm.seeds.add_version("reaper", "default", f4, TemplateScope.CLIENT_ROOT, client_root=croot)
        self.ws.create_component(proj, "vocals", task_template="reaper_daw")
        self.assertIn("DEFAULT_V2", (proj / "vocals" / "working" / "v001" / "vocals_v001.rpp").read_text(encoding="utf-8"))

        # Explicit override selects v001 without changing default
        self.ws.create_component(
            proj,
            "drums",
            task_template="reaper_daw",
            seed_override="seed:client_root:reaper:default:v001",
        )
        self.assertIn("DEFAULT_V1", (proj / "drums" / "working" / "v001" / "drums_v001.rpp").read_text(encoding="utf-8"))

        # Switch default template to podcast_mix
        self.tm.seeds.set_default_template("reaper", "podcast_mix", TemplateScope.CLIENT_ROOT, client_root=croot)
        self.ws.create_component(proj, "dialogue", task_template="reaper_daw")
        self.assertIn("PODCAST_MIX", (proj / "dialogue" / "working" / "v001" / "dialogue_v001.rpp").read_text(encoding="utf-8"))

    def test_version_up_locking_bundle_delete_reconcile_and_vault_sync(self) -> None:
        croot = self.root / "root_ops"
        croot.mkdir()
        self.hub.set_client_root(croot)

        rpp1 = self.root / "starter.rpp"
        rpp1.write_text(_make_rpp("STARTER_V1"), encoding="utf-8")
        self.tm.seeds.add_version("reaper", "default", rpp1, TemplateScope.CLIENT_ROOT, client_root=croot)

        # Place a backup file in v001 to verify version_up_template filters ignore_patterns
        v1_dir = croot / ".obol" / "templates" / "seeds" / "reaper" / "default" / "v001"
        (v1_dir / "junk.rpp-bak").write_text("bak", encoding="utf-8")

        prof = self.hub.get_software_profile("reaper")
        _, v2 = self.tm.seeds.version_up_template(
            "reaper",
            "default",
            TemplateScope.CLIENT_ROOT,
            client_root=croot,
            ignore_patterns=prof.ignore_patterns,
            lock_previous=True,
            preferred_extensions=prof.extensions,
        )
        self.assertEqual(v2.version, 2)
        v2_dir = croot / ".obol" / "templates" / "seeds" / "reaper" / "default" / "v002"
        self.assertFalse((v2_dir / "junk.rpp-bak").exists())
        # v001 primary file is read-only locked
        v1_file = v1_dir / "default_v001.rpp"
        self.assertEqual(v1_file.stat().st_mode & stat.S_IWRITE, 0)

        # Safe deletion of read-only v001
        self.tm.seeds.set_default_version("reaper", "default", 1, TemplateScope.CLIENT_ROOT, client_root=croot)
        self.assertTrue(self.tm.seeds.delete_version("reaper", "default", 1, TemplateScope.CLIENT_ROOT, client_root=croot))
        self.assertFalse(v1_dir.exists())
        man_after_del = self.tm.seeds.load_manifest("reaper", TemplateScope.CLIENT_ROOT, client_root=croot)
        self.assertEqual(man_after_del.templates["default"].default_version, 0)

        # Self-healing reconciliation picks up a manually placed v005 folder
        v5_dir = croot / ".obol" / "templates" / "seeds" / "reaper" / "default" / "v005"
        v5_dir.mkdir(parents=True)
        (v5_dir / "manual_v005.rpp").write_text(_make_rpp("MANUAL_V5"), encoding="utf-8")
        man_rec = self.tm.seeds.load_manifest("reaper", TemplateScope.CLIENT_ROOT, client_root=croot)
        self.assertIn(5, [v.version for v in man_rec.templates["default"].versions])

        # Folder bundle seed (Ableton)
        bundle_src = self.root / "ableton_tpl"
        bundle_src.mkdir()
        (bundle_src / "template.als").write_text("ALS_DATA", encoding="utf-8")
        (bundle_src / "Samples").mkdir()
        (bundle_src / "Samples" / "kick.wav").write_bytes(b"KICK")
        (bundle_src / "Backup").mkdir()
        (bundle_src / "Backup" / "old.als").write_text("IGNORE", encoding="utf-8")

        ab_prof = self.hub.get_software_profile("ableton")
        self.tm.seeds.add_version(
            "ableton",
            "live_set",
            bundle_src,
            TemplateScope.CLIENT_ROOT,
            client_root=croot,
            ignore_patterns=ab_prof.ignore_patterns,
            lock_previous=ab_prof.lock_previous_versions,
            preferred_extensions=ab_prof.extensions,
        )
        proj = self.ws.init_project("live_proj", croot / "live_proj", project_template="simple_media")
        self.ws.create_component(proj, "live_perf", task_template="generic_media", software_override="ableton")
        w_dir = proj / "live_perf" / "working" / "v001"
        self.assertTrue((w_dir / "live_perf_v001.als").is_file())
        self.assertTrue((w_dir / "Samples" / "kick.wav").is_file())
        self.assertFalse((w_dir / "Backup").exists())

        # Vault metadata sync mirrors Project-scoped templates
        vault_dir = self.root / "vault"
        self.hub.connect_vault(vault_dir)
        proj_rpp = self.root / "proj_override.rpp"
        proj_rpp.write_text(_make_rpp("PROJ_VAULT"), encoding="utf-8")
        self.tm.seeds.add_version("reaper", "proj_tpl", proj_rpp, TemplateScope.PROJECT, project_root=proj)
        SyncEngine(self.hub).sync_metadata("live_proj")
        self.assertTrue(
            (vault_dir / "projects" / "live_proj" / ".obol" / "templates" / "seeds" / "reaper" / "manifest.toml").is_file()
        )

    def test_cli_seed_commands_end_to_end(self) -> None:
        croot = self.root / "cli_root"
        croot.mkdir()
        self.assertEqual(run_cli(["root", "set", str(croot)], hub=self.hub), 0)

        rpp1 = self.root / "cli_1.rpp"
        rpp1.write_text(_make_rpp("CLI_V1"), encoding="utf-8")
        self.assertEqual(
            run_cli(
                ["seed", "add", str(rpp1), "--software", "reaper", "--name", "show", "--scope", "client_root", "-m", "init"],
                hub=self.hub,
            ),
            0,
        )
        self.assertEqual(
            run_cli(
                ["seed", "version-up", "--software", "reaper", "--name", "show", "--scope", "client_root", "-m", "v2"],
                hub=self.hub,
            ),
            0,
        )
        self.assertEqual(
            run_cli(
                ["seed", "set-default", "--software", "reaper", "--name", "show", "--version", "1", "--scope", "client_root"],
                hub=self.hub,
            ),
            0,
        )

        buf = StringIO()
        with redirect_stdout(buf):
            rc = run_cli(["seed", "resolve", "reaper", "--root", str(croot), "--json"], hub=self.hub)
        self.assertEqual(rc, 0)
        res_json = json.loads(buf.getvalue().strip())
        self.assertTrue(res_json["found"])
        self.assertEqual(res_json["template_name"], "show")
        self.assertEqual(res_json["version"], 1)

        buf_list = StringIO()
        with redirect_stdout(buf_list):
            rc = run_cli(["seed", "list", "--software", "reaper", "--scope", "client_root", "--json"], hub=self.hub)
        self.assertEqual(rc, 0)
        list_json = json.loads(buf_list.getvalue().strip())
        self.assertEqual(list_json["scopes"][0]["default_template"], "show")

        self.assertEqual(run_cli(["project", "create", "cli_p", "--template", "simple_media"], hub=self.hub), 0)
        self.assertEqual(
            run_cli(
                [
                    "task",
                    "create",
                    "sfx",
                    "-p",
                    "cli_p",
                    "--template",
                    "reaper_daw",
                    "--seed",
                    "seed:client_root:reaper:show:v002",
                ],
                hub=self.hub,
            ),
            0,
        )
        snap = load_component_snapshot(croot / "cli_p", "sfx")
        self.assertEqual(snap.working_versions[0].custom.get("seed_template"), "seed:client_root:reaper:show:v002")

        self.assertEqual(
            run_cli(["seed", "delete", "--software", "reaper", "--name", "show", "--version", "2", "--scope", "client_root"], hub=self.hub),
            0,
        )

    def test_gui_dialogs_and_wizard_reactive_preview(self) -> None:
        root_x = self.root / "gui_root_x"
        root_y = self.root / "gui_root_y"
        root_x.mkdir()
        root_y.mkdir()
        self.hub.set_client_root(root_x)

        rpp_x = self.root / "gui_x.rpp"
        rpp_y = self.root / "gui_y.rpp"
        rpp_x.write_text(_make_rpp("GUI_X"), encoding="utf-8")
        rpp_y.write_text(_make_rpp("GUI_Y"), encoding="utf-8")

        # SaveSeedTemplateDialog creates v001 in root_x
        save_dlg = SaveSeedTemplateDialog(
            None,
            self.hub,
            source_file=rpp_x,
            software_key="reaper",
            client_root=root_x,
            default_comment="GUI saved",
        )
        save_dlg._save()
        self.assertIsNotNone(save_dlg.saved_version)
        self.assertEqual(save_dlg.saved_version.version, 1)

        # Add v001 to root_y via TemplateManagerDialog Tab 3 controls
        self.tm.seeds.add_version("reaper", "pod", rpp_y, TemplateScope.CLIENT_ROOT, client_root=root_y)
        self.tm.seeds.add_version("reaper", "pod", rpp_y, TemplateScope.CLIENT_ROOT, client_root=root_y)

        tpl_dlg = TemplateManagerDialog(None, self.hub, initial_tab=2, initial_root=root_y)
        self.assertEqual(tpl_dlg.tabs.currentIndex(), 2)
        self.assertGreater(tpl_dlg.seed_software_list.count(), 0)
        # Select reaper in seed_software_list
        for i in range(tpl_dlg.seed_software_list.count()):
            if tpl_dlg.seed_software_list.item(i).data(256) == "reaper":  # Qt.ItemDataRole.UserRole == 256
                tpl_dlg.seed_software_list.setCurrentRow(i)
                break
        self.assertEqual(tpl_dlg.seed_versions_table.rowCount(), 2)
        # Pin v001 via combo
        pin_idx = tpl_dlg.seed_default_ver_combo.findData(1)
        self.assertGreaterEqual(pin_idx, 0)
        tpl_dlg.seed_default_ver_combo.setCurrentIndex(pin_idx)
        tpl_dlg._seed_apply_default_version()
        self.assertIn("v001 (pinned)", tpl_dlg.seed_effective_banner.text())

        # NewProjectWizardDialog reactively updates preview when Client Root changes
        wiz = NewProjectWizardDialog(None, self.hub, self.ws)
        wiz.proj_tpl_combo.setCurrentText("simple_media")
        wiz.root_dir_edit.setText(str(root_x))
        root_item = wiz.preview_tree.topLevelItem(0)
        self.assertIsNotNone(root_item)
        # Find audio component child seed label
        audio_texts = [
            root_item.child(i).child(0).child(0).text(1)
            for i in range(root_item.childCount())
            if root_item.child(i).text(0) == "audio/"
        ]
        self.assertTrue(audio_texts and "[Root] default v001" in audio_texts[0])

        wiz.root_dir_edit.setText(str(root_y))
        root_item_y = wiz.preview_tree.topLevelItem(0)
        audio_texts_y = [
            root_item_y.child(i).child(0).child(0).text(1)
            for i in range(root_item_y.childCount())
            if root_item_y.child(i).text(0) == "audio/"
        ]
        self.assertTrue(audio_texts_y and "[Root] pod v001 (pinned)" in audio_texts_y[0])

        # AddComponentDialog shows Auto resolved label and supports explicit override
        proj_x = self.ws.init_project("p_x", root_x / "p_x", project_template="simple_media")
        add_dlg = AddComponentDialog(None, self.hub, project_root=proj_x)
        self.assertIn("[Root] default v001", add_dlg.seed_combo.itemText(0))
        self.assertIn("seed:client_root:reaper:default:v001", [add_dlg.seed_combo.itemData(i) for i in range(add_dlg.seed_combo.count())])


if __name__ == "__main__":
    unittest.main()
