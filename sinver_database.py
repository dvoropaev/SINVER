"""Bootstrap SQLite independently of the inventory application's schema."""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
import sys
import tempfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Callable

from flask import Flask, redirect, render_template_string, request, send_file


METADATA_SQL = """
CREATE TABLE IF NOT EXISTS sinver_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS migration_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_version TEXT NOT NULL,
    to_version TEXT NOT NULL,
    migration_id TEXT,
    migration_type TEXT NOT NULL CHECK (migration_type IN ('migration', 'version_sync')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed')),
    error_message TEXT
);
"""


def semver(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value):
        raise ValueError(f"Некорректная SemVer-версия: {value!r}")
    return tuple(int(part) for part in value.split("."))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def execute_sql(conn: sqlite3.Connection, sql: str) -> None:
    """SQL steps own one transaction; rollback of the whole upgrade uses backup."""
    try:
        conn.executescript("BEGIN IMMEDIATE;\n" + sql + "\nCOMMIT;")
    except Exception:
        conn.rollback()
        raise


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@dataclass(frozen=True)
class MigrationStep:
    migration_id: str
    version: str
    apply: Callable[[sqlite3.Connection], None]


def checks(sql: str) -> set[str]:
    """Extract balanced CHECK clauses, including nested function calls."""
    result = set()
    for match in re.finditer(r"\bCHECK\s*\(", sql, re.IGNORECASE):
        depth = 1
        end = match.end()
        while end < len(sql) and depth:
            depth += (sql[end] == "(") - (sql[end] == ")")
            end += 1
        result.add("".join(sql[match.start():end].lower().split()))
    return result


def indexes(conn: sqlite3.Connection, table: str) -> set[tuple]:
    result = set()
    for row in conn.execute(f'PRAGMA index_list("{table}")'):
        name, unique, partial = row[1], row[2], row[4]
        columns = tuple(item[2] for item in conn.execute(f'PRAGMA index_info("{name}")'))
        sql_row = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        predicate = ""
        if partial and sql_row and sql_row[0]:
            predicate = "".join(sql_row[0].lower().split("where", 1)[1].split())
        result.add((unique, columns, predicate))
    return result


class DatabaseBootstrap:
    def __init__(self, db_path: Path, version: str, schema: Path | None,
                 migrations: Path, backup_dir: Path,
                 validate: Callable[[Path], tuple[bool, str]], *,
                 min_supported_version: str = "0.0.0"):
        self.db_path = db_path.resolve()
        self.version = version
        semver(version)
        self.schema = schema
        self.migrations = migrations
        self.backup_dir = backup_dir.resolve()
        self.validate = validate
        self.min_supported_version = min_supported_version
        semver(min_supported_version)
        self.lock = RLock()
        self.status = "missing"
        self.current_version: str | None = None
        self.error = ""
        self.plan: list[MigrationStep] = []
        self.backup_path: Path | None = None
        self.failure_path = self.db_path.with_name(self.db_path.name + ".maintenance-failure.json")
        self.log_path = self.db_path.with_name(self.db_path.name + ".maintenance.log")
        self.logger = logging.Logger(f"sinver.bootstrap.{id(self)}", logging.INFO)
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self.logger.addHandler(stream)
        self.log_error = ""
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(self.log_path, encoding="utf-8")
            file_handler.setFormatter(stream.formatter)
            self.logger.addHandler(file_handler)
        except OSError as exc:
            self.log_error = f"Не удалось открыть внешний журнал {self.log_path}: {exc}"
            self.logger.error(self.log_error)

    def close(self) -> None:
        for handler in self.logger.handlers:
            handler.close()

    def _read_version(self, conn: sqlite3.Connection) -> str | None:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sinver_meta'").fetchone():
            return None
        row = conn.execute("SELECT value FROM sinver_meta WHERE key='database_version'").fetchone()
        if row is None:
            raise ValueError("В sinver_meta отсутствует database_version")
        semver(row[0])
        return row[0]

    def _validate(self, *, check_version: bool = True) -> None:
        ok, reason = self.validate(self.db_path)
        if not ok:
            raise ValueError(reason)
        if self.schema is None:
            raise ValueError("Не найдена текущая SQL-схема для проверки структуры БД")
        with closing(sqlite3.connect(":memory:")) as reference, closing(connect(self.db_path, readonly=True)) as conn:
            reference.executescript(self.schema.read_text(encoding="utf-8"))
            tables = reference.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence'").fetchall()
            for table, expected_sql in tables:
                actual = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                if not actual:
                    raise ValueError(f"Отсутствует обязательная таблица {table}")
                expected_columns = {row[1]: row[2:] for row in reference.execute(f'PRAGMA table_info("{table}")')}
                actual_columns = {row[1]: row[2:] for row in conn.execute(f'PRAGMA table_info("{table}")')}
                for name, properties in expected_columns.items():
                    if actual_columns.get(name) != properties:
                        raise ValueError(f"Отсутствует или несовместим столбец {table}.{name}")
                expected_fks = {tuple(row[1:]) for row in reference.execute(f'PRAGMA foreign_key_list("{table}")')}
                actual_fks = {tuple(row[1:]) for row in conn.execute(f'PRAGMA foreign_key_list("{table}")')}
                if not expected_fks <= actual_fks or not indexes(reference, table) <= indexes(conn, table):
                    raise ValueError(f"Отсутствуют обязательные связи или индексы таблицы {table}")
                if not checks(expected_sql) <= checks(actual[0]):
                    raise ValueError(f"Отсутствуют обязательные CHECK-ограничения таблицы {table}")
            if check_version and self._read_version(conn) != self.version:
                raise ValueError(f"Итоговая версия БД должна быть {self.version}")
            if conn.execute("SELECT 1 FROM migration_history WHERE status != 'success' LIMIT 1").fetchone():
                raise ValueError("История БД содержит незавершённую или неуспешную миграцию")
        self.logger.info("Post-migration validation: success")

    def _build_plan(self, legacy: bool) -> list[MigrationStep]:
        source = self.current_version or "0.0.0"
        if semver(source) < semver(self.min_supported_version):
            raise ValueError("Поддерживаемого пути обновления нет; установите промежуточную версию SINVER")
        plan = []
        if legacy:
            plan.append(MigrationStep("bootstrap_metadata", "0.0.0", lambda conn: execute_sql(conn, METADATA_SQL)))
        ids = {step.migration_id for step in plan}
        if self.migrations.is_dir():
            releases = sorted((p for p in self.migrations.iterdir() if p.is_dir()), key=lambda p: semver(p.name))
            for release in releases:
                for path in sorted(release.glob("*.sql")):
                    if path.stem in ids:
                        raise ValueError(f"Повторяющийся ID миграции: {path.stem}")
                    ids.add(path.stem)
                    if semver(source) < semver(release.name) <= semver(self.version):
                        sql = path.read_text(encoding="utf-8")
                        plan.append(MigrationStep(path.stem, release.name, lambda conn, sql=sql: execute_sql(conn, sql)))
        self.logger.info("Upgrade path %s -> %s: %s", source, self.version,
                         ", ".join(f"{step.version}/{step.migration_id}" for step in plan) or "version_sync")
        return plan

    def inspect(self) -> None:
        with self.lock:
            self.plan = []
            self.error = ""
            self.current_version = None
            try:
                if self.failure_path.exists():
                    failure = json.loads(self.failure_path.read_text(encoding="utf-8"))
                    self.current_version = failure.get("from_version")
                    self.error = failure["error"]
                    self.backup_path = Path(failure["backup"]) if failure.get("backup") else None
                    if failure.get("status") == "running":
                        self.logger.error("Interrupted migration; restoring original database from %s", self.backup_path)
                        try:
                            if self.backup_path is None:
                                raise ValueError("Не указан backup прерванной миграции")
                            self._copy_database(self.backup_path, self.db_path)
                            self.error = "Предыдущая миграция была прервана. Исходная БД восстановлена из backup."
                            self.logger.info("Original database restored from %s", self.backup_path)
                        except Exception as exc:
                            self.error = f"Прерванная миграция; ОШИБКА ВОССТАНОВЛЕНИЯ: {exc}. Backup: {self.backup_path}"
                            self.logger.exception(self.error)
                        self._save_operation(self.current_version, self.error, self.backup_path)
                    self.status = "failed"
                    self.logger.error("Previous migration failed: %s", self.error)
                    return
                if not self.db_path.exists() or self.db_path.stat().st_size == 0:
                    self.status = "missing"
                    self.logger.info("Database absent; SINVER version %s; maintenance mode", self.version)
                    return
                with closing(connect(self.db_path, readonly=True)) as conn:
                    version = self._read_version(conn)
                self.current_version = version or "0.0.0"
                self.logger.info("Database version %s%s; SINVER version %s", self.current_version,
                                 " (legacy, без метаданных)" if version is None else "", self.version)
                if semver(self.current_version) > semver(self.version):
                    self.status = "newer"
                    self.error = f"БД новее приложения. Установите SINVER {self.current_version} или новее. Автоматический downgrade не поддерживается."
                    self.logger.error(self.error)
                    return
                self.plan = self._build_plan(version is None)
                if self.plan:
                    self.status = "migration_required"
                    self.backup_path = self.backup_dir / (
                        f"sinver-db-{self.current_version}-{datetime.now(timezone.utc):%Y-%m-%d_%H-%M-%S_%f}.sqlite3")
                    self.logger.warning("Требуется миграция схемы БД: %s -> %s; backup %s",
                                        self.current_version, self.version, self.backup_path)
                    return
                self._validate(check_version=False)
                if self.current_version != self.version:
                    self._sync_version()
                self._validate()
                self.status = "ready"
                self.logger.info("database ready")
            except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
                self.status = "incompatible"
                self.error = str(exc)
                self.logger.error("Database not ready: %s", exc)

    def _record(self, conn: sqlite3.Connection, source: str, target: str, kind: str,
                migration_id: str | None = None, *, started_at: str | None = None) -> None:
        timestamp = now()
        conn.execute("""INSERT INTO migration_history
            (from_version, to_version, migration_id, migration_type, started_at, finished_at, status, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                     (source, target, migration_id, kind, started_at or timestamp, timestamp,
                      "success", None))

    def _sync_version(self) -> None:
        if self.log_error:
            raise OSError(self.log_error)
        with closing(connect(self.db_path)) as conn, conn:
            conn.execute("UPDATE sinver_meta SET value=? WHERE key='database_version'", (self.version,))
            self._record(conn, self.current_version, self.version, "version_sync")
        self.logger.info("version_sync %s -> %s: success", self.current_version, self.version)
        self.current_version = self.version

    def create_database(self) -> None:
        with self.lock:
            if self.status != "missing":
                return
            created = False
            try:
                if self.schema is None:
                    raise ValueError("Не найден SQL-скрипт инициализации")
                if self.log_error:
                    raise OSError(self.log_error)
                if self.db_path.exists() and self.db_path.stat().st_size:
                    raise ValueError("БД уже существует; обновите страницу обслуживания")
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
                if not self.db_path.exists():
                    with self.db_path.open("xb"):
                        pass
                created = True
                with closing(connect(self.db_path)) as conn:
                    conn.executescript(self.schema.read_text(encoding="utf-8"))
                self._validate()
                self.logger.info("Database creation: success")
                self.inspect()
            except (OSError, sqlite3.Error, ValueError) as exc:
                if created:
                    self.db_path.unlink(missing_ok=True)
                self.error = f"Ошибка создания БД: {exc}"
                self.logger.error(self.error)

    def _copy_database(self, source: Path, destination: Path) -> None:
        with closing(connect(source, readonly=True)) as src, closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
            if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ValueError("Проверка целостности резервной копии не пройдена")

    def _save_operation(self, source: str, error: str, backup: Path | None, *, status: str = "failed") -> None:
        payload = json.dumps({"from_version": source, "to_version": self.version,
            "status": status, "error": error, "backup": str(backup) if backup else None},
            ensure_ascii=False)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.failure_path.parent,
                                             prefix=".sinver-operation-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.failure_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def upgrade(self, *, confirmed: bool = False) -> None:
        with self.lock:
            if not confirmed or self.status != "migration_required":
                return
            original_version = self.current_version
            failed_step = "backup"
            backup_ready = False
            try:
                if self.log_error:
                    raise OSError(self.log_error)
                # Re-read the version before touching a database changed outside SINVER.
                with closing(connect(self.db_path, readonly=True)) as conn:
                    if (self._read_version(conn) or "0.0.0") != original_version:
                        raise ValueError("Версия БД изменилась; обновите план миграции")
                self.backup_dir.mkdir(parents=True, exist_ok=True)
                self.logger.info("Backup started: %s", self.backup_path)
                with self.backup_path.open("xb"):
                    pass
                self.backup_path.chmod(0o600)
                self._copy_database(self.db_path, self.backup_path)
                backup_ready = True
                self.logger.info("Backup success: %s", self.backup_path)
                self._save_operation(original_version, "Обновление БД не завершено", self.backup_path, status="running")
                source = original_version
                with closing(connect(self.db_path)) as conn:
                    for step in self.plan:
                        failed_step = step.migration_id
                        started_at = now()
                        self.logger.info("Migration step started: %s", failed_step)
                        step.apply(conn)
                        with conn:
                            conn.execute("INSERT INTO sinver_meta (key, value) VALUES ('database_version', ?) "
                                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (step.version,))
                            self._record(conn, source, step.version, "migration", step.migration_id, started_at=started_at)
                        source = step.version
                        self.logger.info("Migration step success: %s", failed_step)
                    if source != self.version:
                        with conn:
                            conn.execute("UPDATE sinver_meta SET value=? WHERE key='database_version'", (self.version,))
                            self._record(conn, source, self.version, "version_sync")
                failed_step = "post-migration validation"
                self._validate()
                self.failure_path.unlink(missing_ok=True)
                self.logger.info("Migration success: %s -> %s; backup retained: %s",
                                 original_version, self.version, self.backup_path)
                self.inspect()
            except Exception as exc:
                self.error = f"Миграция не выполнена, шаг {failed_step}: {exc}"
                self.logger.exception(self.error)
                if backup_ready:
                    try:
                        self._copy_database(self.backup_path, self.db_path)
                        self.logger.info("Original database restored from %s", self.backup_path)
                    except Exception as restore_error:
                        self.error += f"; ОШИБКА ВОССТАНОВЛЕНИЯ: {restore_error}. Backup: {self.backup_path}"
                        self.logger.exception("Restore failed")
                self.status = "failed"
                try:
                    self._save_operation(original_version, self.error, self.backup_path if backup_ready else None)
                except OSError:
                    self.logger.exception("Cannot persist failure marker; consult external maintenance log")


MAINTENANCE_TEMPLATE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>SINVER — Maintenance mode</title>
<style>body{font:16px system-ui;background:#fdf7ef;color:#2f2a24;max-width:850px;margin:40px auto;padding:20px}
section{background:#fffdf9;border:1px solid #dfd6c8;border-radius:16px;padding:24px}button{padding:10px 16px}
code{overflow-wrap:anywhere}.error{color:#ab2525}li{margin:8px 0}</style></head><body><section>
<h1>SINVER — Maintenance mode</h1>
<p>База: <code>{{ state.db_path }}</code></p>
<p>Текущая версия БД: {{ state.current_version or 'не задана' }}. Версия SINVER: {{ state.version }}.</p>
{% if state.error %}<p class="error">{{ state.error }}</p>{% endif %}
{% if state.status == 'missing' %}<p>Создайте новую БД с текущей схемой.</p>
<form method="post" action="/maintenance/create"><input type="hidden" name="token" value="{{ token }}"><button>Создать БД</button></form>
{% elif state.status == 'migration_required' %}
<p>Требуется миграция схемы БД: {{ state.current_version }} → {{ state.version }}.</p>
<ul>{% for step in state.plan %}<li>{{ step.version }}: {{ step.migration_id }}</li>{% endfor %}</ul>
<p>Перед изменениями будет создан backup: <code>{{ state.backup_path }}</code>.
Он сохранится после обновления. При ошибке исходная БД будет восстановлена.</p>
<form method="post" action="/maintenance/upgrade"><input type="hidden" name="token" value="{{ token }}">
<label><input type="checkbox" name="confirm" value="yes" required> Подтверждаю обновление всей БД</label>
<p><button>Создать backup и обновить БД</button></p></form>
{% elif state.status == 'ready' %}<p>database ready</p><a href="/servers">Открыть SINVER</a>
{% else %}<p>Основная часть приложения заблокирована до исправления проблемы.</p>
<form method="post" action="/maintenance/retry"><input type="hidden" name="token" value="{{ token }}"><button>Повторить проверку</button></form>
{% endif %}
{% if state.status == 'newer' %}<p>Также можно вручную восстановить старую резервную копию, совместимую с этой версией SINVER.</p>
<p class="error">При восстановлении старой резервной копии будут потеряны все данные и изменения, внесённые после момента её создания.</p>{% endif %}
{% if backups %}<h2>Известные резервные копии</h2><ul>{% for name, version in backups %}
<li><a href="/maintenance/backup/{{ name }}">{{ name }}</a> — версия {{ version }}<br>
<code>{{ state.backup_dir / name }}</code></li>{% endfor %}</ul>{% endif %}
<p>Каталог backup: <code>{{ state.backup_dir }}</code></p>
<p>Внешний журнал: <code>{{ state.log_path }}</code></p>
</section></body></html>"""


def create_maintenance_app(state: DatabaseBootstrap, main_factory: Callable[[], Flask], *,
                           force_maintenance: bool = False) -> Flask:
    app = Flask("sinver_maintenance")
    token = secrets.token_urlsafe(32)
    normal_app = None

    def known_backups() -> list[tuple[str, str]]:
        backups = []
        if state.backup_dir.is_dir():
            for path in sorted(state.backup_dir.glob("sinver-db-*.sqlite3"), reverse=True):
                try:
                    with closing(connect(path, readonly=True)) as conn:
                        version = state._read_version(conn) or "0.0.0"
                    if state.status != "newer" or semver(version) <= semver(state.version):
                        backups.append((path.name, version))
                except (OSError, sqlite3.Error, ValueError):
                    continue
        return backups

    @app.before_request
    def confirm_action():
        if request.method == "POST" and not secrets.compare_digest(request.form.get("token", ""), token):
            return "Подтверждение недействительно. Обновите страницу обслуживания.", 403

    @app.get("/")
    @app.get("/maintenance")
    @app.get("/<path:path>")
    def maintenance(path=""):
        return render_template_string(MAINTENANCE_TEMPLATE, state=state, token=token, backups=known_backups())

    @app.post("/maintenance/create")
    def create():
        state.create_database()
        return redirect("/" if state.status == "ready" else "/maintenance")

    @app.post("/maintenance/upgrade")
    def upgrade():
        if request.form.get("confirm") != "yes":
            return "Необходимо подтвердить миграцию.", 400
        state.upgrade(confirmed=True)
        return redirect("/" if state.status == "ready" else "/maintenance")

    @app.post("/maintenance/retry")
    def retry():
        if state.failure_path.exists():
            failure = json.loads(state.failure_path.read_text(encoding="utf-8"))
            with closing(connect(state.db_path, readonly=True)) as conn:
                version = state._read_version(conn) or "0.0.0"
            if version != failure["from_version"]:
                return "Сначала восстановите исходную БД из указанного backup.", 409
        state.failure_path.unlink(missing_ok=True)
        state.inspect()
        return redirect("/maintenance")

    @app.get("/maintenance/backup/<name>")
    def backup(name):
        if name not in {item[0] for item in known_backups()}:
            return "Резервная копия не найдена", 404
        return send_file(state.backup_dir / name, as_attachment=True)

    @app.errorhandler(Exception)
    def error(exc):
        state.logger.exception("Maintenance request failed")
        return render_template_string(MAINTENANCE_TEMPLATE, state=state, token=token, backups=[]), 500

    maintenance_wsgi = app.wsgi_app

    def dispatch(environ, start_response):
        nonlocal normal_app
        with state.lock:
            path = environ.get("PATH_INFO", "")
            if state.status == "ready" and not path.startswith("/maintenance"):
                if not force_maintenance or path != "/":
                    if normal_app is None:
                        normal_app = main_factory()
                    return normal_app(environ, start_response)
            return maintenance_wsgi(environ, start_response)

    app.wsgi_app = dispatch
    return app
