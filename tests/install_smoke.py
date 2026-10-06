"""Check an unprivileged staged install without touching host accounts or systemd."""

import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile


SOURCE = Path(__file__).resolve().parents[1]


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    source = root / "source"
    source.mkdir()
    for name in ("Makefile", "sinver.py", "sinver.service", "sinver.toml"):
        shutil.copyfile(SOURCE / name, source / name)
    shutil.copytree(SOURCE / "database", source / "database")
    # Include a future nested migration whose source permissions are restrictive.
    migration = source / "database/migrations/0.1.0/001.sql"
    migration.parent.mkdir(mode=0o700)
    migration.write_text("SELECT 1;\n")
    migration.chmod(0o600)

    guards = root / "guards"
    guards.mkdir()
    for command in ("getent", "id", "groupadd", "useradd", "chown", "systemctl"):
        guard = guards / command
        guard.write_text(f"#!/bin/sh\necho 'Unexpected host command: {command}' >&2\nexit 99\n")
        guard.chmod(0o755)
    environment = {**os.environ, "PATH": f"{guards}:{os.environ['PATH']}"}
    # A path containing spaces checks that every DESTDIR reference is quoted.
    stage = root / "package root"

    def install() -> None:
        subprocess.run(["make", f"DESTDIR={stage}", "install"], cwd=source,
                       env=environment, check=True, stdout=subprocess.DEVNULL)

    install()
    expected = {
        "usr/bin/sinver": 0o755,
        "usr/share/sinver": 0o755,
        "usr/share/sinver/init_db.sql": 0o644,
        "usr/share/sinver/migrations": 0o755,
        "usr/share/sinver/migrations/README.md": 0o644,
        "usr/share/sinver/migrations/0.1.0": 0o755,
        "usr/share/sinver/migrations/0.1.0/001.sql": 0o644,
        "etc/sinver.toml": 0o640,
        "etc/systemd/system/sinver.service": 0o644,
        "var/lib/sinver": 0o750,
        "var/lib/sinver/backups": 0o700,
        "var/log/sinver": 0o750,
    }
    for name, permissions in expected.items():
        assert mode(stage / name) == permissions, name
    assert not list(stage.rglob("*.sqlite")), "install must not create SQLite"
    assert (stage / "usr/share/sinver/init_db.sql").read_bytes() == (source / "database/init_db.sql").read_bytes()

    config = stage / "etc/sinver.toml"
    custom_config = config.read_bytes() + b"\n# user configuration\n"
    config.write_bytes(custom_config)
    config.chmod(0o644)
    database = stage / "var/lib/sinver/sinver.sqlite"
    database.write_bytes(b"existing user database")
    database.chmod(0o644)
    backup = stage / "var/lib/sinver/backups/old/copy.sqlite3"
    backup.parent.mkdir(mode=0o755)
    backup.write_bytes(b"existing backup")
    log = stage / "var/log/sinver/sinver.log"
    log.write_bytes(b"existing log")
    install()
    assert config.read_bytes() == custom_config
    assert mode(config) == 0o640
    assert database.read_bytes() == b"existing user database"
    assert backup.read_bytes() == b"existing backup"
    assert log.read_bytes() == b"existing log"
    for path in (database, backup, log):
        assert mode(path) == 0o600, path
    assert mode(backup.parent) == 0o700
    for name, permissions in expected.items():
        assert mode(stage / name) == permissions, name

print("Staged install / permissions / reinstall checks passed")
