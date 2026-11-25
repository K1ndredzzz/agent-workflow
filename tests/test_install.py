"""Installation and symlink contracts for install.sh."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from tests.helpers import IsolatedDispatchCase, WORKFLOW_ROOT


class TestInstall(IsolatedDispatchCase):
    def test_install_sh_preserves_unrelated_skills_and_backs_up_real_agents_md(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            # Copy install source into temp dir so the test NEVER mutates permissions in the live checkout!
            src_copy = d / "workflow_src"
            shutil.copytree(WORKFLOW_ROOT, src_copy, ignore=shutil.ignore_patterns(".git", "__pycache__"))
            ag_skills = d / "agent_skills"
            ag_plugins = d / "agy_plugins"
            cx_skills = d / "codex_skills"
            cx_agents = d / "codex" / "AGENTS.md"
            bin_dst = d / "bin" / "agent-dispatch"
            (ag_skills / "my-custom-skill").mkdir(parents=True)
            (ag_skills / "my-custom-skill" / "SKILL.md").write_text("keep me", encoding="utf-8")
            (ag_skills / "my-grill").symlink_to(src_copy / "skills" / "grill")
            (ag_plugins / "unrelated-plugin").mkdir(parents=True)
            (ag_plugins / "unrelated-plugin" / "plugin.json").write_text('{"name":"unrelated"}\n', encoding="utf-8")
            (ag_plugins / "agent-workflow").mkdir(parents=True)
            (ag_plugins / "agent-workflow" / "plugin.json").write_text(
                '{"name":"agent-workflow","displayName":"Old Workflow Plugin"}\n', encoding="utf-8"
            )
            (ag_plugins / "agent-workflow" / "custom.txt").write_text("backup plugin", encoding="utf-8")
            cx_agents.parent.mkdir(parents=True)
            cx_agents.write_text("custom real AGENTS.md", encoding="utf-8")

            env = os.environ.copy()
            env.update(
                {
                    "AGENT_SKILLS_DIR": str(ag_skills),
                    "AGY_PLUGINS_DIR": str(ag_plugins),
                    "CODEX_SKILLS_DIR": str(cx_skills),
                    "CODEX_AGENTS_MD": str(cx_agents),
                    "BIN_TARGET": str(bin_dst),
                }
            )
            env.pop("AGY_PLUGINS_BACKUP_DIR", None)
            subprocess.run([str(src_copy / "install.sh")], check=True, env=env, capture_output=True)
            initial_links = {p: p.lstat().st_ino for p in ag_skills.iterdir()}
            plugin_inode = (ag_plugins / "agent-workflow").lstat().st_ino
            subprocess.run([str(src_copy / "install.sh")], check=True, env=env, capture_output=True)
            self.assertEqual(initial_links, {p: p.lstat().st_ino for p in ag_skills.iterdir()})
            self.assertEqual((ag_plugins / "agent-workflow").lstat().st_ino, plugin_inode)
            self.assertEqual((ag_skills / "my-grill").resolve(), src_copy / "skills" / "grill")
            # Unrelated skill and plugin preserved!
            self.assertTrue((ag_skills / "my-custom-skill" / "SKILL.md").is_file())
            self.assertTrue((ag_plugins / "unrelated-plugin" / "plugin.json").is_file())
            # Global Antigravity rules plugin linked and conflicting plugin moved OUTSIDE auto-discovery dir!
            self.assertEqual((ag_plugins / "agent-workflow").resolve(), src_copy / "agy" / "plugin")
            self.assertTrue((ag_plugins / "agent-workflow" / "rules" / "AGENTS.md").is_file())
            self.assertEqual(list(ag_plugins.glob("*.bak.*")), [], "Plugin backup must NOT remain inside AGY_PLUGINS_DIR!")
            plugin_bak_root = d / "plugins.bak"
            plugin_backups = list(plugin_bak_root.glob("agent-workflow.bak.*"))
            self.assertEqual(len(plugin_backups), 1)
            self.assertEqual(
                json.loads((plugin_backups[0] / "plugin.json").read_text(encoding="utf-8"))["displayName"],
                "Old Workflow Plugin",
            )
            self.assertEqual((plugin_backups[0] / "custom.txt").read_text(encoding="utf-8"), "backup plugin")
            # Conflicting symlink to an old checkout is also moved out of AGY_PLUGINS_DIR into plugins.bak!
            old_checkout = d / "old_checkout_plugin"
            old_checkout.mkdir()
            (old_checkout / "plugin.json").write_text('{"name":"agent-workflow","displayName":"Relocated"}\n', encoding="utf-8")
            (ag_plugins / "agent-workflow").unlink()
            (ag_plugins / "agent-workflow").symlink_to(old_checkout)
            subprocess.run([str(src_copy / "install.sh")], check=True, env=env, capture_output=True)
            self.assertEqual(list(ag_plugins.glob("*.bak.*")), [])
            self.assertEqual((ag_plugins / "agent-workflow").resolve(), src_copy / "agy" / "plugin")
            self.assertEqual(len(list(plugin_bak_root.glob("agent-workflow.bak.*"))), 2)
            # Setting AGY_PLUGINS_BACKUP_DIR inside AGY_PLUGINS_DIR fails closed!
            bad_env = dict(env, AGY_PLUGINS_BACKUP_DIR=str(ag_plugins / "nested-bak"))
            bad_res = subprocess.run([str(src_copy / "install.sh")], env=bad_env, capture_output=True, text=True)
            self.assertNotEqual(bad_res.returncode, 0)
            # Real AGENTS.md backed up, not destroyed!
            backups = list(cx_agents.parent.glob("AGENTS.md.bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "custom real AGENTS.md")
            # Orchestrator skills NOT symlinked into codex skills!
            self.assertFalse((cx_skills / "dispatch").exists())
            self.assertFalse((cx_skills / "slice").exists())
            self.assertTrue((cx_skills / "grill").is_symlink())
            self.assertTrue((ag_skills / "dispatch").is_symlink())

    def test_installer_resolves_script_and_registry_aliases(self) -> None:
        source = self.sandbox / "source"
        shutil.copytree(WORKFLOW_ROOT, source, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        script = self.sandbox / "relink"
        script.symlink_to(source / "install.sh")
        agent_skills = self.sandbox / "agent-skills"
        agent_skills.symlink_to(source / "skills")
        agy_plugins = self.sandbox / "agy-plugins"
        worker_skills = self.sandbox / "worker-skills"
        worker_skills.mkdir()
        relative = worker_skills / "research"
        relative.symlink_to("../source/skills/research")
        inode = relative.lstat().st_ino
        retired = worker_skills / "dispatch"
        retired.symlink_to("../source/skills/dispatch")
        env = dict(os.environ, AGENT_SKILLS_DIR=str(agent_skills), AGY_PLUGINS_DIR=str(agy_plugins),
                   CODEX_SKILLS_DIR=str(worker_skills), CODEX_AGENTS_MD=str(self.sandbox / "AGENTS.md"),
                   BIN_TARGET=str(self.sandbox / "agent-dispatch"))
        subprocess.run([str(script)], env=env, check=True, capture_output=True)
        self.assertTrue((agent_skills / "dispatch/SKILL.md").is_file())
        self.assertFalse((source / "skills/dispatch").is_symlink())
        self.assertFalse(retired.is_symlink())
        self.assertEqual(relative.lstat().st_ino, inode)
        self.assertEqual((agy_plugins / "agent-workflow").resolve(), source / "agy/plugin")
        self.assertEqual((self.sandbox / "agent-dispatch").resolve(), source / "bin/agent-dispatch")
        self.assertFalse(list(source.rglob("*.bak.*")))

        # Both rosters cannot be installed into the same physical directory.
        env["CODEX_SKILLS_DIR"] = str(source / "skills")
        conflict = subprocess.run([str(script)], env=env, capture_output=True)
        self.assertNotEqual(conflict.returncode, 0)
        self.assertTrue((agent_skills / "dispatch/SKILL.md").is_file())
