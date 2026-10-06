import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock

import sinver
from sinver_database import DatabaseBootstrap, create_maintenance_app, semver


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "sqlite" / "init_db.sql"


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "inventory.sqlite"
        self.schema = self.root / "schema.sql"
        self.schema.write_text(SCHEMA.read_text(), encoding="utf-8")
        self.migrations = self.root / "migrations"
        self.migrations.mkdir()
        self.backups = self.root / "backups"

    def bootstrap(self, version="0.0.0", **kwargs):
        state = DatabaseBootstrap(self.db, version, self.schema, self.migrations,
                                  self.backups, sinver.validate_database, **kwargs)
        self.addCleanup(state.close)
        state.inspect()
        return state

    def initialize(self, version="0.0.0", *, legacy=False):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(self.schema.read_text())
            if legacy:
                conn.executescript("DROP TABLE sinver_meta; DROP TABLE migration_history;")
            else:
                conn.execute("UPDATE sinver_meta SET value=? WHERE key='database_version'", (version,))
                conn.commit()
            conn.execute("INSERT INTO roles(role_name, color) VALUES ('retained', '#123456')")
            conn.commit()

    def sql(self, sql, params=()):
        with closing(sqlite3.connect(self.db)) as conn:
            result = conn.execute(sql, params).fetchall()
            conn.commit()
            return result

    def migration(self, version, name, sql):
        directory = self.migrations / version
        directory.mkdir(exist_ok=True)
        (directory / (name + ".sql")).write_text(sql, encoding="utf-8")

    def client(self, state):
        factory = Mock(side_effect=lambda: sinver.create_app(self.db))
        app = create_maintenance_app(state, factory)
        app.config["TESTING"] = True
        return app.test_client(), factory

    def token(self, client):
        page = client.get("/maintenance").get_data(as_text=True)
        return re.search(r'name="token" value="([^"]+)"', page).group(1)

    def test_missing_database_has_independent_web_creation(self):
        state = self.bootstrap()
        client, factory = self.client(state)
        self.assertEqual(state.status, "missing")
        self.assertFalse(self.db.exists())
        self.assertIn(b"Maintenance mode", client.get("/servers").data)
        factory.assert_not_called()
        self.assertEqual(client.post("/maintenance/create").status_code, 403)
        response = client.post("/maintenance/create", data={"token": self.token(client)}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Servers", response.data)
        self.assertEqual(state.status, "ready")
        self.assertEqual(self.sql("SELECT value FROM sinver_meta"), [("0.0.0",)])
        self.assertEqual(list(self.backups.glob("*")), [])

    def test_empty_database_can_be_created(self):
        self.db.touch()
        state = self.bootstrap()
        state.create_database()
        self.assertEqual(state.status, "ready")

    def test_missing_schema_keeps_maintenance_working(self):
        state = DatabaseBootstrap(self.db, "0.0.0", None, self.migrations, self.backups, sinver.validate_database)
        self.addCleanup(state.close)
        state.inspect()
        client, factory = self.client(state)
        client.post("/maintenance/create", data={"token": self.token(client)})
        self.assertEqual(state.status, "missing")
        self.assertIn("инициализации", state.error)
        self.assertFalse(self.db.exists())
        factory.assert_not_called()

    def test_legacy_metadata_requires_web_confirmation_and_backup(self):
        self.initialize(legacy=True)
        before = self.db.read_bytes()
        state = self.bootstrap()
        client, factory = self.client(state)
        self.assertEqual(state.status, "migration_required")
        state.upgrade()
        self.assertEqual(self.db.read_bytes(), before)
        token = self.token(client)
        self.assertEqual(client.post("/maintenance/upgrade", data={"token": token}).status_code, 400)
        factory.assert_not_called()
        client.post("/maintenance/upgrade", data={"token": token, "confirm": "yes"})
        self.assertEqual(state.status, "ready")
        self.assertEqual(self.sql("SELECT role_name FROM roles"), [("retained",)])
        self.assertEqual(self.sql("SELECT migration_id, migration_type, status FROM migration_history"),
                         [("bootstrap_metadata", "migration", "success")])
        self.assertTrue(state.backup_path.exists())
        with closing(sqlite3.connect(state.backup_path)) as conn:
            self.assertFalse(conn.execute("SELECT name FROM sqlite_master WHERE name='sinver_meta'").fetchall())
        with client.get("/maintenance/backup/" + state.backup_path.name) as response:
            self.assertEqual(response.status_code, 200)

    def test_newer_database_blocks_main_and_does_not_modify_data(self):
        self.initialize("1.0.0")
        before = self.db.read_bytes()
        state = self.bootstrap()
        client, factory = self.client(state)
        self.assertEqual(state.status, "newer")
        response = client.get("/zones")
        self.assertIn("потеряны".encode(), response.data)
        self.assertIn("Установите SINVER 1.0.0".encode(), response.data)
        self.assertEqual(self.db.read_bytes(), before)
        factory.assert_not_called()

    def test_corrupt_database_does_not_break_maintenance(self):
        self.db.write_bytes(b"This is not SQLite")
        state = self.bootstrap()
        client, factory = self.client(state)
        self.assertEqual(state.status, "incompatible")
        self.assertEqual(client.get("/").status_code, 200)
        factory.assert_not_called()

    def test_version_sync_needs_no_backup_or_confirmation(self):
        self.initialize()
        state = self.bootstrap("0.0.1")
        self.assertEqual(state.status, "ready")
        self.assertEqual(self.sql("SELECT value FROM sinver_meta"), [("0.0.1",)])
        self.assertEqual(self.sql("SELECT from_version, to_version, migration_type FROM migration_history"),
                         [("0.0.0", "0.0.1", "version_sync")])
        self.assertFalse(self.backups.exists())

    def test_multi_version_plan_is_sorted_and_finishes_with_version_sync(self):
        self.initialize()
        self.migration("0.2.0", "002_data", "INSERT INTO roles(role_name,color) VALUES ('second','#000000');")
        self.migration("0.1.0", "001_data", "INSERT INTO roles(role_name,color) VALUES ('first','#000000');")
        state = self.bootstrap("0.3.0")
        self.assertEqual([step.migration_id for step in state.plan], ["001_data", "002_data"])
        state.upgrade(confirmed=True)
        self.assertEqual(state.status, "ready")
        self.assertEqual(self.sql("SELECT role_name FROM roles ORDER BY id"), [("retained",), ("first",), ("second",)])
        self.assertEqual(self.sql("SELECT migration_type FROM migration_history ORDER BY id"),
                         [("migration",), ("migration",), ("version_sync",)])
        self.assertTrue(state.backup_path.exists())

    def test_failed_second_step_restores_all_changes_and_persists_failure(self):
        self.initialize()
        self.migration("0.1.0", "001_first", "INSERT INTO roles(role_name,color) VALUES ('first','#000000');")
        self.migration("0.1.0", "002_fail", "SELECT * FROM absent_table;")
        self.migration("0.1.0", "003_never", "INSERT INTO roles(role_name,color) VALUES ('third','#000000');")
        state = self.bootstrap("0.1.0")
        state.upgrade(confirmed=True)
        self.assertEqual(state.status, "failed")
        self.assertIn("002_fail", state.error)
        self.assertEqual(self.sql("SELECT role_name FROM roles"), [("retained",)])
        self.assertEqual(self.sql("SELECT value FROM sinver_meta"), [("0.0.0",)])
        self.assertEqual(self.sql("SELECT * FROM migration_history"), [])
        restarted = self.bootstrap("0.1.0")
        self.assertEqual(restarted.status, "failed")
        self.assertIn("002_fail", restarted.error)
        self.assertIn("Original database restored", state.log_path.read_text())
        self.assertTrue(state.backup_path.exists())

    def test_failed_post_validation_restores_original_schema(self):
        self.initialize()
        self.migration("0.1.0", "001_bad_schema", "DROP INDEX idx_ip_primary_per_server;")
        state = self.bootstrap("0.1.0")
        state.upgrade(confirmed=True)
        self.assertEqual(state.status, "failed")
        self.assertIn("post-migration validation", state.error)
        self.assertTrue(self.sql("SELECT name FROM sqlite_master WHERE name='idx_ip_primary_per_server'"))
        self.assertEqual(self.sql("SELECT value FROM sinver_meta"), [("0.0.0",)])

    def test_interrupted_transition_is_restored_on_restart_even_if_version_matches(self):
        self.initialize()
        self.migration("0.1.0", "001_data", "SELECT 1;")
        state = self.bootstrap("0.1.0")
        self.backups.mkdir()
        state._copy_database(self.db, state.backup_path)
        state._save_operation("0.0.0", "Interrupted upgrade", state.backup_path, status="running")
        self.sql("UPDATE sinver_meta SET value='0.1.0' WHERE key='database_version'")
        self.sql("DELETE FROM roles")
        restarted = self.bootstrap("0.1.0")
        self.assertEqual(restarted.status, "failed")
        self.assertEqual(self.sql("SELECT role_name FROM roles"), [("retained",)])
        self.assertEqual(self.sql("SELECT value FROM sinver_meta"), [("0.0.0",)])
        self.assertIn("прервана", restarted.error)
        client, factory = self.client(restarted)
        client.get("/servers")
        factory.assert_not_called()
        client.post("/maintenance/retry", data={"token": self.token(client)})
        self.assertEqual(restarted.status, "migration_required")

    def test_backup_failure_never_runs_migration(self):
        self.initialize(legacy=True)
        before = self.db.read_bytes()
        self.backups.write_text("not a directory")
        state = self.bootstrap()
        state.upgrade(confirmed=True)
        self.assertEqual(state.status, "failed")
        self.assertEqual(self.db.read_bytes(), before)
        self.assertIn("backup", state.error)

    def test_wal_backup_includes_committed_data_and_can_restore_it(self):
        self.initialize()
        with closing(sqlite3.connect(self.db)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("INSERT INTO roles(role_name,color) VALUES ('wal-data','#000000')")
            writer.commit()
            self.migration("0.1.0", "001_fail", "DELETE FROM roles; SELECT * FROM absent_table;")
            state = self.bootstrap("0.1.0")
            state.upgrade(confirmed=True)
            self.assertEqual(state.status, "failed")
            self.assertEqual(self.sql("SELECT role_name FROM roles ORDER BY id"), [("retained",), ("wal-data",)])
            with closing(sqlite3.connect(state.backup_path)) as backup:
                self.assertEqual(backup.execute("SELECT COUNT(*) FROM roles").fetchone(), (2,))

    def test_unsupported_path_and_duplicate_ids_block_main(self):
        self.initialize()
        state = self.bootstrap("1.0.0", min_supported_version="0.1.0")
        self.assertEqual(state.status, "incompatible")
        self.assertIn("пути обновления нет", state.error)
        self.migration("0.1.0", "same_id", "SELECT 1;")
        self.migration("0.2.0", "same_id", "SELECT 1;")
        other = self.bootstrap("0.2.0")
        self.assertEqual(other.status, "incompatible")
        self.assertIn("Повторяющийся ID", other.error)

    def test_incompatible_columns_and_checks_are_rejected(self):
        self.initialize()
        self.sql("ALTER TABLE zones RENAME COLUMN minimum TO broken")
        state = self.bootstrap()
        self.assertEqual(state.status, "incompatible")
        self.assertIn("minimum", state.error)
        self.db.unlink()
        text = self.schema.read_text().replace("CHECK (length(role_name) <= 128)", "CHECK (1=1)")
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript(text)
        state.inspect()
        self.assertEqual(state.status, "incompatible")
        self.assertIn("CHECK", state.error)

    def test_validation_does_not_create_a_missing_database(self):
        self.assertFalse(sinver.validate_database(self.db)[0])
        self.assertFalse(self.db.exists())

    def test_creation_failure_does_not_leave_partial_database(self):
        self.schema.write_text("CREATE TABLE incomplete(id INTEGER); invalid SQL;")
        state = self.bootstrap()
        state.create_database()
        self.assertEqual(state.status, "missing")
        self.assertFalse(self.db.exists())

    def test_semver_is_numeric_and_rejects_invalid_values(self):
        self.assertLess(semver("0.2.0"), semver("0.10.0"))
        for value in ("01.0.0", "v1.0.0", "1.2", "-1.0.0", "1.0.0 extra"):
            with self.assertRaises(ValueError):
                semver(value)


if __name__ == "__main__":
    unittest.main()
