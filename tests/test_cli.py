"""The command line, as a user (or ssm.sh) runs it."""
import subprocess
import sys
import unittest

from helpers import ROOT, ProjectCase


class Cli(ProjectCase):
    def run_santa(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(ROOT / "Santa.py"), *args], cwd=cwd or self.dir,
                              capture_output=True, text=True, timeout=120)

    def test_dry_run_output_and_exit_code(self):
        self.project(rules={})
        r = self.run_santa("--dry-run")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("✅ Un tirage valide existe (rien n'a été écrit).", r.stdout)
        self.assertIn("📧 Aperçu du message", r.stdout)
        self.assertIn("Rudolph", r.stdout)

    def test_real_draw_prints_files_and_exits_zero(self):
        self.project(rules={})
        r = self.run_santa("--year", "2026")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("✅ Tirage généré : 6 mails dans secretSantaFiles/", r.stdout)
        self.assertIn("🗂️  Historique enregistré dans history/2026.json", r.stdout)
        self.assertNotIn("→", r.stdout)                       # no pair ever printed

    def test_infeasible_exits_one_with_hint(self):
        self.project(names=("A", "B", "C"), rules={"forced": [{"from": "A", "to": "B"}, {"from": "A", "to": "C"}]})
        r = self.run_santa("--dry-run")
        self.assertEqual(r.returncode, 1)
        self.assertIn("❌ Aucun tirage ne respecte les règles strictes.", r.stdout)
        self.assertIn("💡", r.stdout)

    def test_user_errors_exit_one_with_a_message_and_no_traceback(self):
        self.project(rules={"forbidden": [{"from": "Nobody", "to": "Bob"}]})
        r = self.run_santa("--dry-run")
        self.assertEqual(r.returncode, 1)
        self.assertIn("❌ Participant inconnu « Nobody »", r.stdout)
        self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_base_dir_from_another_folder(self):
        self.project(rules={})
        elsewhere = self.dir / "run_from_here"
        elsewhere.mkdir()
        r = self.run_santa("--base-dir", str(self.dir), "--year", "2026", cwd=elsewhere)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.dir / "history/2026.json").exists())
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_seed_warns(self):
        self.project(rules={})
        r = self.run_santa("--seed", "3", "--year", "2026")
        self.assertIn("--seed", r.stdout)


if __name__ == "__main__":
    unittest.main()
