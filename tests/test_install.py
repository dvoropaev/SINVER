import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def test_install_and_upgrade_only_copy_assets_and_preserve_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            for name in ("Makefile", "sinver.py", "sinver_database.py", "sinver.service"):
                shutil.copy2(ROOT / name, source / name)
            shutil.copytree(ROOT / "sqlite", source / "sqlite")
            staged = root / "package root"
            command = ["make", "install", f"DESTDIR={staged}"]
            subprocess.run(command, cwd=source, check=True, capture_output=True)
            installed_schema = staged / "usr/share/sinver/init_db.sql"
            self.assertEqual(installed_schema.read_bytes(), (source / "sqlite/init_db.sql").read_bytes())
            self.assertEqual(installed_schema.stat().st_mode & 0o777, 0o444)
            self.assertTrue((staged / "usr/share/sinver/migrations").is_dir())
            self.assertFalse((staged / "usr/share/sinver/migrations/.gitkeep").exists())
            database = staged / "var/sinver/sinver.sqlite"
            self.assertFalse(database.exists())
            database.write_bytes(b"existing user database, must remain untouched")
            (source / "sqlite/init_db.sql").write_text("-- replacement schema\n")
            release = source / "sqlite/migrations/0.1.0"
            release.mkdir()
            (release / "001_example.sql").write_text("-- future migration\n")
            subprocess.run(command, cwd=source, check=True, capture_output=True)
            self.assertEqual(database.read_bytes(), b"existing user database, must remain untouched")
            self.assertEqual(installed_schema.read_text(), "-- replacement schema\n")
            installed_migration = staged / "usr/share/sinver/migrations/0.1.0/001_example.sql"
            self.assertEqual(installed_migration.read_text(), "-- future migration\n")
            self.assertEqual(installed_migration.stat().st_mode & 0o777, 0o444)
            environment = dict(os.environ, PYTHONPATH=str(staged / "usr/share/sinver"))
            result = subprocess.run([os.sys.executable, str(staged / "usr/bin/sinver"), "--version"],
                                    env=environment, check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), "SINVER 0.0.0")


if __name__ == "__main__":
    unittest.main()
