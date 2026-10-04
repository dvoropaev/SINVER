#!/usr/bin/env python3
"""SINVER: локальное веб-приложение для инвентаризации серверов и DNS.

Рефакторинг (без изменения URL/форм/поведения):
- Вынесены повторяющиеся операции SQLite (q/execute/get_one).
- Вынесено подключение к PostgreSQL + опции таймаутов в единый helper.
- Упорядочена логика PowerDNS preview/apply, меньше дублирования connect().
- Валидации/конвертации параметров аккуратнее, меньше “скрытых” исключений.
- Небольшие улучшения читаемости и типизации.
"""

from __future__ import annotations

import argparse
import shutil
import ipaddress
import secrets
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from flask import Flask, Response, flash, g, redirect, render_template_string, request, send_file, url_for

try:
    import psycopg2
    from psycopg2 import extras as psycopg2_extras
except Exception:  # noqa: BLE001
    psycopg2 = None
    psycopg2_extras = None


BASE_DIR = Path(__file__).resolve().parent
INIT_SQL_PATH = BASE_DIR / "init_db.sql"
INSTALL_INIT_SQL_PATH = Path("/usr/share/sinver/init_db.sql")
DEFAULT_PORT = 5173
MAX_TOOLTIP_LEN = 50
SUBDOMAIN_RECORD_TYPES = ("A", "AAAA")
POWERDNS_SYNC_RECORD_TYPES = frozenset((*SUBDOMAIN_RECORD_TYPES, "SOA"))

# PostgreSQL session timeouts (ms)
PG_STATEMENT_TIMEOUT_MS = 30_000
PG_LOCK_TIMEOUT_MS = 5_000
PG_IDLE_IN_TX_TIMEOUT_MS = 30_000


REQUIRED_TABLES = {
    "zones",
    "roles",
    "hostings",
    "servers",
    "ip_addresses",
    "subdomains",
    "subdomain_ip_map",
}


def resolve_init_sql_path() -> Path | None:
    """Возвращает путь к init_db.sql, подходящий для текущего способа запуска."""
    candidates = (
        INIT_SQL_PATH,
        INSTALL_INIT_SQL_PATH,
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None

BASE_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>SINVER</title>
  <style>
    :root {
      --bg: #fdf7ef;
      --panel: #fffdf9;
      --line: #dfd6c8;
      --text: #2f2a24;
      --accent: #8d7b68;
      --soft: #f4ece1;
      --hover: #f9f0e4;
      --ok: #2f7d32;
      --bad: #ab2525;
    }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: Inter, Arial, sans-serif; background: var(--bg); color: var(--text); }
    header { position: sticky; top: 0; background: var(--panel); border-bottom: 1px solid var(--line); z-index: 10; }
    .wrap { max-width: 1250px; margin: 0 auto; padding: 14px 20px; }
    nav { display: flex; gap: 8px; flex-wrap: wrap; }
    nav a { text-decoration: none; color: var(--text); background: var(--soft); border: 1px solid var(--line); border-radius: 999px; padding: 8px 14px; }
    nav a.active { background: #e6dacb; }
    main { padding-bottom: 24px; }
    .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 16px; padding: 16px; margin-top: 14px; }
    h1, h2 { margin: 0 0 12px; }
    table { width: 100%; border-collapse: collapse; background: white; border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }
    th, td { border-bottom: 1px solid #efe8de; padding: 9px; vertical-align: top; }
    tr:hover { background: var(--hover); }
    form.inline { display: inline; }
    input, select, textarea { border: 1px solid var(--line); border-radius: 10px; padding: 8px; width: 100%; background: #fff; }
    .grid { display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
    .btn { background: var(--soft); border: 1px solid var(--line); border-radius: 10px; padding: 8px 14px; cursor: pointer; }
    .btn.primary { background: #d8c4b0; }
    .btn.danger { background: #f2d7d7; border-color: #d3aaaa; }
    .muted { color: #736658; }
    .dot { width: 16px; height: 16px; border-radius: 999px; border: 1px solid #222; display: inline-block; }
    .flash-ok { color: var(--ok); }
    .flash-bad { color: var(--bad); }
    .readonly [data-editable] { pointer-events: none; opacity: 0.8; }
    textarea { min-height: 140px; resize: vertical; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  </style>
</head>
<body>
<header>
  <div class="wrap">
    <nav>
      {% for item in nav_items %}
      <a href="{{ item.href }}" class="{% if item.key == current_tab %}active{% endif %}">{{ item.title }}</a>
      {% endfor %}
    </nav>
  </div>
</header>
<main class="wrap">
  {% with messages = get_flashed_messages(with_categories=true) %}
  {% if messages %}
    <div class="panel">
      {% for cat, msg in messages %}
      <div class="{{ 'flash-ok' if cat == 'ok' else 'flash-bad' }}">{{ msg }}</div>
      {% endfor %}
    </div>
  {% endif %}
  {% endwith %}
  {{ body | safe }}
</main>
<script>
  function enableEdit(formId) {
    const form = document.getElementById(formId);
    if (!form) return;
    form.classList.remove('readonly');
    for (const el of form.querySelectorAll('[data-editable]')) {
      el.removeAttribute('readonly');
      el.removeAttribute('disabled');
    }
    const saveBtn = form.querySelector('.save-btn');
    if (saveBtn) saveBtn.disabled = false;
  }
</script>
</body>
</html>
"""


@dataclass(frozen=True)
class ZoneRecord:
    fqdn: str
    record_type: str
    content: str
    ttl: int = 3600


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def make_serial() -> str:
    # YYMMDDHHMM
    return datetime.now().strftime("%y%m%d%H%M")


def parse_remote_serial(soa_content: str) -> str:
    parts = soa_content.split()
    if len(parts) < 3:
        raise ValueError("SOA content has invalid format")
    # MNAME RNAME SERIAL ...
    return parts[2]


def format_sql_preview(value: str) -> str:
    return value.replace("'", "''")


def log_powerdns(step: int, message: str) -> None:
    print(f"[PowerDNS][Step {step}] {message}", flush=True)


def _pg_options_string() -> str:
    return (
        f"-c statement_timeout={PG_STATEMENT_TIMEOUT_MS} "
        f"-c lock_timeout={PG_LOCK_TIMEOUT_MS} "
        f"-c idle_in_transaction_session_timeout={PG_IDLE_IN_TX_TIMEOUT_MS}"
    )


def pg_connect_from_zone(zone: sqlite3.Row, *, with_timeouts: bool) -> Any:
    """Создаёт psycopg2 connection из строки zones.* (не открывает курсор)."""
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is not installed")

    if not zone["psql_address"] or not zone["psql_user"]:
        raise RuntimeError("PostgreSQL connection data is incomplete")

    kwargs: dict[str, Any] = {
        "host": zone["psql_address"],
        "dbname": zone["db_name"] or "powerdns",
        "user": zone["psql_user"],
        "password": zone["psql_password"] or "",
        "connect_timeout": 5,
    }
    if with_timeouts:
        kwargs["options"] = _pg_options_string()
    return psycopg2.connect(**kwargs)


def create_app(db_path: Path, debug_mode: bool = False) -> Flask:
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(24)
    app.config["DATABASE"] = str(db_path)
    app.config["DEBUG_MODE"] = debug_mode
    app.config["POWERDNS_PLAN_CACHE"] = {}
    app.config["POWERDNS_PLAN_TTL_SECONDS"] = 600

    # -------------------------
    # SQLite helpers
    # -------------------------
    @app.before_request
    def _open_db() -> None:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")

    @app.teardown_request
    def _close_db(exc: BaseException | None) -> None:
        db = getattr(g, "db", None)
        if db is not None:
            db.close()

    def q(sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(g.db.execute(sql, tuple(params)).fetchall())

    def q_one(sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return g.db.execute(sql, tuple(params)).fetchone()

    def backup_database_before_change() -> Path:
        source_path = Path(app.config["DATABASE"]).resolve()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_path = source_path.with_name(f"{source_path.name}.{timestamp}.bak")
        shutil.copy2(source_path, backup_path)
        return backup_path

    def execute(sql: str, params: Sequence[Any] = ()) -> None:
        backup_database_before_change()
        g.db.execute(sql, tuple(params))
        g.db.commit()

    def require_row(row: sqlite3.Row | None, message: str, redirect_endpoint: str, **kwargs: Any) -> Response | sqlite3.Row:
        if row is None:
            flash(message, "bad")
            return redirect(url_for(redirect_endpoint, **kwargs))
        return row

    # -------------------------
    # UI helpers
    # -------------------------
    def nav(current: str) -> list[dict[str, str]]:
        return [
            {"key": "servers", "title": "Servers", "href": url_for("servers")},
            {"key": "ips", "title": "IP addresses", "href": url_for("ips")},
            {"key": "subdomains", "title": "Subdomains", "href": url_for("subdomains")},
            {"key": "roles", "title": "Roles", "href": url_for("roles")},
            {"key": "hostings", "title": "Hostings", "href": url_for("hostings")},
            {"key": "zones", "title": "Zones", "href": url_for("zones")},
            {"key": "dns", "title": "DNS", "href": url_for("dns")},
        ]

    def render(body: str, current_tab: str) -> str:
        return render_template_string(BASE_TEMPLATE, body=body, current_tab=current_tab, nav_items=nav(current_tab))

    def short(text: str | None) -> str:
        if not text:
            return ""
        return text[:MAX_TOOLTIP_LEN]

    def role_color(role_id: int | None) -> str | None:
        if not role_id:
            return None
        row = q_one("SELECT color FROM roles WHERE id=?", (role_id,))
        return row["color"] if row else None

    def server_display(row: sqlite3.Row) -> str:
        if row["zone"]:
            return f"{row['hostname']}.{row['zone']}"
        return row["hostname"]

    def resolve_ip_color(ip_row: sqlite3.Row) -> str | None:
        ip_direct_color = role_color(ip_row["role_id"])
        if ip_direct_color:
            return ip_direct_color
        server_row = q_one("SELECT role_id FROM servers WHERE id=?", (ip_row["server_id"],))
        if not server_row:
            return None
        return role_color(server_row["role_id"])

    def resolve_subdomain_color(subdomain_row: sqlite3.Row) -> str | None:
        subdomain_direct_color = role_color(subdomain_row["role_id"])
        if subdomain_direct_color:
            return subdomain_direct_color
        linked_ip = q_one(
            """
            SELECT ip.*
            FROM ip_addresses ip
            JOIN subdomain_ip_map sm ON sm.ip_id=ip.id
            WHERE sm.subdomain_id=?
            ORDER BY ip.is_primary DESC, ip.id
            LIMIT 1
            """,
            (subdomain_row["id"],),
        )
        if not linked_ip:
            return None
        return resolve_ip_color(linked_ip)

    # -------------------------
    # PowerDNS plan cache
    # -------------------------
    def powerdns_cache_save(preview: dict[str, Any]) -> str:
        cache: dict[str, dict[str, Any]] = app.config["POWERDNS_PLAN_CACHE"]
        ttl_seconds = int(app.config["POWERDNS_PLAN_TTL_SECONDS"])
        now = utc_now()

        expired_tokens = [token for token, payload in cache.items() if payload["expires_at"] <= now]
        for token in expired_tokens:
            cache.pop(token, None)

        token = secrets.token_urlsafe(24)
        cache[token] = {"preview": preview, "expires_at": now + timedelta(seconds=ttl_seconds)}
        return token

    def powerdns_cache_take(token: str) -> dict[str, Any] | None:
        cache: dict[str, dict[str, Any]] = app.config["POWERDNS_PLAN_CACHE"]
        payload = cache.pop(token, None)
        if not payload:
            return None
        if payload["expires_at"] <= utc_now():
            return None
        return payload["preview"]

    # -------------------------
    # DNS record builder
    # -------------------------
    def resolve_subdomain_fqdn(subdomain: str, zone_name: str) -> str:
        """Преобразует имя поддомена в fqdn с поддержкой apex-символа @."""
        if subdomain.strip() == "@":
            return zone_name
        return f"{subdomain}.{zone_name}"

    def build_zone_records(zone_id: int) -> list[ZoneRecord]:
        zone = q_one("SELECT * FROM zones WHERE id=?", (zone_id,))
        if not zone:
            raise ValueError("Zone not found")

        records: list[ZoneRecord] = []
        soa = f"{zone['mname']} {zone['rname']} {zone['serial']} {zone['refresh']} {zone['retry']} {zone['expire']} {zone['minimum']}"
        records.append(ZoneRecord(fqdn=zone["zone"], record_type="SOA", content=soa))

        for s in q(
            """
            SELECT s.hostname, z.zone, ip.ip, ip.ip_type, ip.is_primary
            FROM servers s
            JOIN zones z ON z.id=s.primary_zone_id
            JOIN ip_addresses ip ON ip.server_id=s.id
            WHERE s.primary_zone_id=?
            """,
            (zone_id,),
        ):
            if s["is_primary"]:
                records.append(ZoneRecord(f"{s['hostname']}.{s['zone']}", "A" if s["ip_type"] == "IPv4" else "AAAA", s["ip"]))

        for sd in q("SELECT * FROM subdomains WHERE zone_id=?", (zone_id,)):
            if sd["record_type"] not in SUBDOMAIN_RECORD_TYPES:
                continue
            fqdn = resolve_subdomain_fqdn(sd["subdomain"], zone["zone"])
            mapped_ips = q(
                """
                SELECT ip.ip, ip.ip_type
                FROM ip_addresses ip
                JOIN subdomain_ip_map sm ON sm.ip_id=ip.id
                WHERE sm.subdomain_id=?
                """,
                (sd["id"],),
            )
            for item in mapped_ips:
                if (sd["record_type"] == "A" and item["ip_type"] == "IPv4") or (sd["record_type"] == "AAAA" and item["ip_type"] == "IPv6"):
                    records.append(ZoneRecord(fqdn, sd["record_type"], item["ip"]))

        return sorted(records, key=lambda r: (r.fqdn, r.record_type, r.content))

    # -------------------------
    # Routes
    # -------------------------
    @app.route("/")
    def home() -> Response:
        return redirect(url_for("servers"))

    # ---- SERVERS ----
    @app.route("/servers")
    def servers() -> str:
        role_id = request.args.get("role_id")
        hosting_id = request.args.get("hosting_id")
        order = request.args.get("sort", "hostname")
        allowed = {"hostname", "paid_until", "role_name"}
        order = order if order in allowed else "hostname"

        where: list[str] = []
        params: list[Any] = []
        if role_id:
            where.append("s.role_id = ?")
            params.append(role_id)
        if hosting_id:
            where.append("s.hosting_id = ?")
            params.append(hosting_id)

        sql = """
            SELECT s.*, r.role_name, r.color, h.name AS hosting_name, h.paid_until, z.zone,
                   (SELECT ip FROM ip_addresses WHERE server_id=s.id AND is_primary=1 LIMIT 1) AS primary_ip,
                   (SELECT group_concat(ip, '\n') FROM ip_addresses WHERE server_id=s.id) AS ips
            FROM servers s
            LEFT JOIN roles r ON r.id=s.role_id
            LEFT JOIN hostings h ON h.id=s.hosting_id
            LEFT JOIN zones z ON z.id=s.primary_zone_id
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {order} COLLATE NOCASE"

        rows = q(sql, params)
        role_rows = q("SELECT id, role_name FROM roles ORDER BY role_name")
        host_rows = q("SELECT id, name FROM hostings ORDER BY name")

        body = render_template_string(
            """
            <div class="panel">
              <h1>Servers</h1>
              <form method="get" class="grid">
                <div><label>Role</label><select name="role_id"><option value="">Any</option>{% for r in role_rows %}<option value="{{r.id}}" {% if request.args.get('role_id') == r.id|string %}selected{% endif %}>{{r.role_name}}</option>{% endfor %}</select></div>
                <div><label>Hosting</label><select name="hosting_id"><option value="">Any</option>{% for h in host_rows %}<option value="{{h.id}}" {% if request.args.get('hosting_id') == h.id|string %}selected{% endif %}>{{h.name}}</option>{% endfor %}</select></div>
                <div><label>Sort by</label><select name="sort"><option value="hostname">hostname</option><option value="role_name">role</option><option value="paid_until">paid until</option></select></div>
                <div class="actions"><button class="btn primary">Apply</button><a href="{{ url_for('servers') }}" class="btn">Reset</a></div>
              </form>
            </div>
            <div class="panel">
              <table>
                <thead><tr><th>Color</th><th>Hostname</th><th>Description</th><th>Primary IP</th><th>IP addresses</th><th>Role</th><th>Hosting</th><th>Paid until</th></tr></thead>
                <tbody>
                  {% for row in rows %}
                  <tr title="{{ short(row['description']) }}">
                    <td>{% if row['color'] %}<span class="dot" style="background:{{ row['color'] }}"></span>{% endif %}</td>
                    <td><a href="{{ url_for('server_detail', server_id=row['id']) }}">{{ server_display(row) }}</a></td>
                    <td>{{ row['description'] or '—' }}</td>
                    <td>{{ row['primary_ip'] or '—' }}</td>
                    <td class="mono" style="white-space:pre-line">{{ row['ips'] or '—' }}</td>
                    <td>{{ row['role_name'] or '—' }}</td>
                    <td>{{ row['hosting_name'] or '—' }}</td>
                    <td>{{ row['paid_until'] or '—' }}</td>
                  </tr>
                  {% endfor %}
                </tbody>
              </table>
            </div>
            <div class="panel">
              <h2>Add server</h2>
              <form method="post" action="{{ url_for('server_create') }}" class="grid">
                <div><label>Hostname</label><input required name="hostname"></div>
                <div><label>Primary zone</label><select name="primary_zone_id"><option value="">None</option>{% for z in zones %}<option value="{{ z.id }}">{{ z.zone }}</option>{% endfor %}</select></div>
                <div><label>Role</label><select name="role_id"><option value="">None</option>{% for r in role_rows %}<option value="{{r.id}}">{{r.role_name}}</option>{% endfor %}</select></div>
                <div><label>Hosting</label><select name="hosting_id"><option value="">None</option>{% for h in host_rows %}<option value="{{h.id}}">{{h.name}}</option>{% endfor %}</select></div>
                <div style="grid-column:1/-1"><label>Description</label><textarea name="description"></textarea></div>
                <div class="actions"><button class="btn primary">Create</button></div>
              </form>
            </div>
            """,
            rows=rows,
            role_rows=role_rows,
            host_rows=host_rows,
            zones=q("SELECT id, zone FROM zones ORDER BY zone"),
            request=request,
            short=short,
            server_display=server_display,
        )
        return render(body, "servers")

    @app.post("/servers/create")
    def server_create() -> Response:
        execute(
            "INSERT INTO servers(hostname, primary_zone_id, role_id, hosting_id, description) VALUES (?, ?, ?, ?, ?)",
            (
                request.form["hostname"].strip(),
                request.form.get("primary_zone_id") or None,
                request.form.get("role_id") or None,
                request.form.get("hosting_id") or None,
                request.form.get("description") or None,
            ),
        )
        flash("Server created.", "ok")
        return redirect(url_for("servers"))

    @app.route("/servers/<int:server_id>", methods=["GET", "POST"])
    def server_detail(server_id: int) -> str | Response:
        if request.method == "POST":
            execute(
                "UPDATE servers SET hostname=?, primary_zone_id=?, role_id=?, hosting_id=?, description=? WHERE id=?",
                (
                    request.form["hostname"].strip(),
                    request.form.get("primary_zone_id") or None,
                    request.form.get("role_id") or None,
                    request.form.get("hosting_id") or None,
                    request.form.get("description") or None,
                    server_id,
                ),
            )
            flash("Server updated.", "ok")
            return redirect(url_for("server_detail", server_id=server_id))

        row = q_one(
            "SELECT s.*, z.zone FROM servers s LEFT JOIN zones z ON z.id=s.primary_zone_id WHERE s.id=?",
            (server_id,),
        )
        row_or_redirect = require_row(row, "Server not found.", "servers")
        if isinstance(row_or_redirect, Response):
            return row_or_redirect
        row = row_or_redirect

        ips = q("SELECT * FROM ip_addresses WHERE server_id=? ORDER BY ip", (server_id,))
        linked_subdomains = q(
            """
            SELECT DISTINCT sd.id, sd.record_type, sd.subdomain, z.zone
            FROM subdomains sd
            JOIN zones z ON z.id=sd.zone_id
            JOIN subdomain_ip_map sm ON sm.subdomain_id=sd.id
            JOIN ip_addresses ip ON ip.id=sm.ip_id
            WHERE ip.server_id=?
            ORDER BY sd.subdomain
            """,
            (server_id,),
        )
        body = render_template_string(
            """
            <div class="panel"><h1>Server: {{ title }}</h1>
            <form id="server-form" class="readonly" method="post">
              <div class="grid">
                <div><label>Hostname</label><input data-editable readonly name="hostname" value="{{ row.hostname }}"></div>
                <div><label>Primary zone</label><select data-editable disabled name="primary_zone_id"><option value="">None</option>{% for z in zones %}<option value="{{ z.id }}" {% if z.id == row.primary_zone_id %}selected{% endif %}>{{ z.zone }}</option>{% endfor %}</select></div>
                <div><label>Role</label><select data-editable disabled name="role_id"><option value="">None</option>{% for r in roles %}<option value="{{ r.id }}" {% if r.id == row.role_id %}selected{% endif %}>{{ r.role_name }}</option>{% endfor %}</select></div>
                <div><label>Hosting</label><select data-editable disabled name="hosting_id"><option value="">None</option>{% for h in hostings %}<option value="{{ h.id }}" {% if h.id == row.hosting_id %}selected{% endif %}>{{ h.name }}</option>{% endfor %}</select></div>
                <div style="grid-column:1/-1"><label>Description</label><textarea data-editable readonly name="description">{{ row.description or '' }}</textarea></div>
              </div>
              <div class="actions"><button type="button" class="btn" onclick="enableEdit('server-form')">Edit</button><button class="btn primary save-btn" disabled>Save</button></div>
            </form></div>
            <div class="panel"><h2>IP addresses</h2><ul>{% for ip in ips %}<li><a href="{{ url_for('ip_detail', ip_id=ip.id) }}">{{ ip.ip }}</a>{% if ip.is_primary %} (Primary){% endif %}</li>{% else %}<li>—</li>{% endfor %}</ul></div>
            <div class="panel"><h2>Subdomains linked to this server</h2><ul>{% for sd in linked_subdomains %}<li><a href="{{ url_for('subdomain_detail', subdomain_id=sd.id) }}">{{ sd.record_type }} {{ sd.subdomain }}.{{ sd.zone }}</a></li>{% else %}<li>—</li>{% endfor %}</ul></div>
            <div class="panel"><form method="post" action="{{ url_for('server_delete', server_id=row.id) }}" onsubmit="return confirm('Delete server and linked IP relations?')"><button class="btn danger">Delete server</button></form></div>
            """,
            row=row,
            title=f"{row['hostname']}.{row['zone']}" if row["zone"] else row["hostname"],
            zones=q("SELECT id, zone FROM zones ORDER BY zone"),
            roles=q("SELECT id, role_name FROM roles ORDER BY role_name"),
            hostings=q("SELECT id, name FROM hostings ORDER BY name"),
            ips=ips,
            linked_subdomains=linked_subdomains,
        )
        return render(body, "servers")

    @app.post("/servers/<int:server_id>/delete")
    def server_delete(server_id: int) -> Response:
        execute("DELETE FROM servers WHERE id=?", (server_id,))
        flash("Server deleted.", "ok")
        return redirect(url_for("servers"))

    # ---- IPS ----
    @app.route("/ips")
    def ips() -> str:
        ip_type = request.args.get("ip_type")
        server_id = request.args.get("server_id")
        role_id = request.args.get("role_id")
        is_primary = request.args.get("is_primary")
        order = request.args.get("sort", "ip")
        allowed = {"ip", "ip_type", "hostname", "role_name", "is_primary"}
        order = order if order in allowed else "ip"

        where: list[str] = []
        params: list[Any] = []
        if ip_type:
            where.append("ip.ip_type=?")
            params.append(ip_type)
        if server_id:
            where.append("ip.server_id=?")
            params.append(server_id)
        if role_id:
            where.append("ip.role_id=?")
            params.append(role_id)
        if is_primary in {"0", "1"}:
            where.append("ip.is_primary=?")
            params.append(is_primary)

        sql = """
            SELECT ip.*, r.role_name, r.color, s.hostname, z.zone
            FROM ip_addresses ip
            JOIN servers s ON s.id=ip.server_id
            LEFT JOIN zones z ON z.id=s.primary_zone_id
            LEFT JOIN roles r ON r.id=ip.role_id
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {order} COLLATE NOCASE"

        rows = q(sql, params)

        body = render_template_string(
            """
            <div class="panel"><h1>IP addresses</h1>
              <form method="get" class="grid">
                <div><label>Type</label><select name="ip_type"><option value="">Any</option><option value="IPv4" {% if request.args.get('ip_type') == 'IPv4' %}selected{% endif %}>IPv4</option><option value="IPv6" {% if request.args.get('ip_type') == 'IPv6' %}selected{% endif %}>IPv6</option></select></div>
                <div><label>Server</label><select name="server_id"><option value="">Any</option>{% for s in servers %}<option value="{{ s.id }}" {% if request.args.get('server_id') == s.id|string %}selected{% endif %}>{{ s.hostname }}</option>{% endfor %}</select></div>
                <div><label>Role</label><select name="role_id"><option value="">Any</option>{% for r in roles %}<option value="{{ r.id }}" {% if request.args.get('role_id') == r.id|string %}selected{% endif %}>{{ r.role_name }}</option>{% endfor %}</select></div>
                <div><label>Primary</label><select name="is_primary"><option value="">Any</option><option value="1" {% if request.args.get('is_primary') == '1' %}selected{% endif %}>Yes</option><option value="0" {% if request.args.get('is_primary') == '0' %}selected{% endif %}>No</option></select></div>
                <div><label>Sort by</label><select name="sort"><option value="ip">ip</option><option value="ip_type">type</option><option value="hostname">server</option><option value="role_name">role</option><option value="is_primary">primary</option></select></div>
                <div class="actions"><button class="btn primary">Apply</button><a href="{{ url_for('ips') }}" class="btn">Reset</a></div>
              </form>
            </div>
            <div class="panel"><table><thead><tr><th>Color</th><th>Type</th><th>IP</th><th>Server</th><th>Role</th><th>Primary</th></tr></thead><tbody>
            {% for row in rows %}
              <tr><td>{% if ip_colors[row.id] %}<span class="dot" style="background:{{ ip_colors[row.id] }}"></span>{% endif %}</td><td>{{ row.ip_type }}</td><td><a href="{{ url_for('ip_detail', ip_id=row.id) }}">{{ row.ip }}</a></td><td>{{ row.hostname }}{% if row.zone %}.{{ row.zone }}{% endif %}</td><td>{{ row.role_name or '—' }}</td><td>{{ 'Primary' if row.is_primary else '' }}</td></tr>
            {% endfor %}</tbody></table></div>
            <div class="panel"><h2>Add IP</h2>
              <form method="post" action="{{ url_for('ip_create') }}" class="grid">
                <div><label>Type</label><select name="ip_type"><option>IPv4</option><option>IPv6</option></select></div>
                <div><label>IP</label><input required name="ip"></div>
                <div><label>Server</label><select name="server_id">{% for s in servers %}<option value="{{ s.id }}">{{ s.hostname }}</option>{% endfor %}</select></div>
                <div><label>Role</label><select name="role_id"><option value="">None</option>{% for r in roles %}<option value="{{ r.id }}">{{ r.role_name }}</option>{% endfor %}</select></div>
                <div><label>Primary</label><select name="is_primary"><option value="0">No</option><option value="1">Yes</option></select></div>
                <div class="actions"><button class="btn primary">Create</button></div>
              </form>
            </div>
            """,
            rows=rows,
            servers=q("SELECT id, hostname FROM servers ORDER BY hostname"),
            roles=q("SELECT id, role_name FROM roles ORDER BY role_name"),
            ip_colors={int(row["id"]): resolve_ip_color(row) for row in rows},
            request=request,
        )
        return render(body, "ips")

    def enforce_single_primary(server_id: int, ip_id: int) -> None:
        execute("UPDATE ip_addresses SET is_primary=0 WHERE server_id=? AND id<>?", (server_id, ip_id))

    @app.post("/ips/create")
    def ip_create() -> Response:
        ip_raw = request.form["ip"].strip()
        try:
            parsed_ip = ipaddress.ip_address(ip_raw)
        except ValueError:
            flash("Invalid IP format.", "bad")
            return redirect(url_for("ips"))

        expected_type = "IPv4" if parsed_ip.version == 4 else "IPv6"
        if request.form["ip_type"] != expected_type:
            flash("IP type does not match entered address.", "bad")
            return redirect(url_for("ips"))

        execute(
            "INSERT INTO ip_addresses(ip_type, ip, server_id, role_id, is_primary) VALUES (?, ?, ?, ?, ?)",
            (
                request.form["ip_type"],
                ip_raw,
                request.form["server_id"],
                request.form.get("role_id") or None,
                int(request.form.get("is_primary") == "1"),
            ),
        )
        new_row = q_one("SELECT id FROM ip_addresses WHERE ip=?", (ip_raw,))
        new_id = int(new_row["id"]) if new_row else 0

        if request.form.get("is_primary") == "1" and new_id:
            enforce_single_primary(int(request.form["server_id"]), new_id)
            execute("UPDATE ip_addresses SET is_primary=1 WHERE id=?", (new_id,))

        flash("IP address created.", "ok")
        return redirect(url_for("ips"))

    @app.route("/ips/<int:ip_id>", methods=["GET", "POST"])
    def ip_detail(ip_id: int) -> str | Response:
        if request.method == "POST":
            server_id = int(request.form["server_id"])
            is_primary = int(request.form.get("is_primary") == "1")

            current_ip = q_one("SELECT ip_type FROM ip_addresses WHERE id=?", (ip_id,))
            if not current_ip:
                flash("IP not found.", "bad")
                return redirect(url_for("ips"))

            execute(
                "UPDATE ip_addresses SET server_id=?, role_id=?, is_primary=? WHERE id=?",
                (server_id, request.form.get("role_id") or None, is_primary, ip_id),
            )
            if is_primary:
                enforce_single_primary(server_id, ip_id)
                execute("UPDATE ip_addresses SET is_primary=1 WHERE id=?", (ip_id,))

            flash("IP address updated.", "ok")
            return redirect(url_for("ip_detail", ip_id=ip_id))

        row = q_one("SELECT * FROM ip_addresses WHERE id=?", (ip_id,))
        row_or_redirect = require_row(row, "IP not found.", "ips")
        if isinstance(row_or_redirect, Response):
            return row_or_redirect
        row = row_or_redirect

        subdomains_rows = q(
            """
            SELECT sd.id, sd.record_type, sd.subdomain, z.zone
            FROM subdomains sd
            JOIN zones z ON z.id=sd.zone_id
            JOIN subdomain_ip_map sm ON sm.subdomain_id=sd.id
            WHERE sm.ip_id=?
            ORDER BY sd.subdomain
            """,
            (ip_id,),
        )
        body = render_template_string(
            """
            <div class="panel"><h1>IP: {{ row.ip }}</h1>
              <form id="ip-form" class="readonly" method="post"><div class="grid">
                <div><label>Type</label><input readonly value="{{ row.ip_type }}"></div>
                <div><label>IP</label><input readonly value="{{ row.ip }}"></div>
                <div><label>Server</label><select data-editable disabled name="server_id">{% for s in servers %}<option value="{{ s.id }}" {% if s.id == row.server_id %}selected{% endif %}>{{ s.hostname }}</option>{% endfor %}</select></div>
                <div><label>Role</label><select data-editable disabled name="role_id"><option value="">None</option>{% for r in roles %}<option value="{{ r.id }}" {% if r.id == row.role_id %}selected{% endif %}>{{ r.role_name }}</option>{% endfor %}</select></div>
                <div><label>Primary</label><select data-editable disabled name="is_primary"><option value="0" {% if not row.is_primary %}selected{% endif %}>No</option><option value="1" {% if row.is_primary %}selected{% endif %}>Yes</option></select></div>
              </div><div class="actions"><button type="button" class="btn" onclick="enableEdit('ip-form')">Edit</button><button class="btn primary save-btn" disabled>Save</button></div></form>
            </div>
            <div class="panel"><h2>Linked subdomains</h2><ul>{% for sd in subdomains_rows %}<li><a href="{{ url_for('subdomain_detail', subdomain_id=sd.id) }}">{{ sd.record_type }} {{ sd.subdomain }}.{{ sd.zone }}</a></li>{% else %}<li>—</li>{% endfor %}</ul></div>
            <div class="panel"><form method="post" action="{{ url_for('ip_delete', ip_id=row.id) }}" onsubmit="return confirm('Delete IP?')"><button class="btn danger">Delete IP</button></form></div>
            """,
            row=row,
            servers=q("SELECT id, hostname FROM servers ORDER BY hostname"),
            roles=q("SELECT id, role_name FROM roles ORDER BY role_name"),
            subdomains_rows=subdomains_rows,
        )
        return render(body, "ips")

    @app.post("/ips/<int:ip_id>/delete")
    def ip_delete(ip_id: int) -> Response:
        execute("DELETE FROM ip_addresses WHERE id=?", (ip_id,))
        flash("IP deleted.", "ok")
        return redirect(url_for("ips"))

    # ---- SUBDOMAINS / ROLES / HOSTINGS / ZONES ----
    # Чтобы “ничего не сломалось”, эти блоки оставлены максимально близко к твоей версии.
    # (Локальные чистки – только через q/execute/q_one.)
    #
    # Ниже — без смысловых изменений, просто заменены прямые g.db.execute(...) на q/q_one/execute.

    @app.route("/subdomains")
    def subdomains() -> str:
        record_type = request.args.get("record_type")
        zone_id = request.args.get("zone_id")
        role_id = request.args.get("role_id")
        order = request.args.get("sort", "subdomain")
        allowed = {"subdomain", "record_type", "zone", "role_name"}
        order = order if order in allowed else "subdomain"

        where: list[str] = []
        params: list[Any] = []
        if record_type:
            where.append("sd.record_type=?")
            params.append(record_type)
        if zone_id:
            where.append("sd.zone_id=?")
            params.append(zone_id)
        if role_id:
            where.append("sd.role_id=?")
            params.append(role_id)

        sql = """
            SELECT sd.*, z.zone, r.role_name, r.color,
                   group_concat(ip.ip, '\n') AS ips
            FROM subdomains sd
            JOIN zones z ON z.id=sd.zone_id
            LEFT JOIN roles r ON r.id=sd.role_id
            LEFT JOIN subdomain_ip_map sm ON sm.subdomain_id=sd.id
            LEFT JOIN ip_addresses ip ON ip.id=sm.ip_id
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " GROUP BY sd.id"
        sql += f" ORDER BY {order} COLLATE NOCASE"
        rows = q(sql, params)

        body = render_template_string(
            """
            <div class="panel"><h1>Subdomains</h1>
              <p class="muted">Only A and AAAA are supported. SOA parameters are configured on the Zones tab.</p>
              <form method="get" class="grid">
                <div><label>Type</label><select name="record_type"><option value="">Any</option>{% for rt in record_types %}<option value="{{ rt }}" {% if request.args.get('record_type') == rt %}selected{% endif %}>{{ rt }}</option>{% endfor %}</select></div>
                <div><label>Zone</label><select name="zone_id"><option value="">Any</option>{% for z in zones %}<option value="{{ z.id }}" {% if request.args.get('zone_id') == z.id|string %}selected{% endif %}>{{ z.zone }}</option>{% endfor %}</select></div>
                <div><label>Role</label><select name="role_id"><option value="">Any</option>{% for r in roles %}<option value="{{ r.id }}" {% if request.args.get('role_id') == r.id|string %}selected{% endif %}>{{ r.role_name }}</option>{% endfor %}</select></div>
                <div><label>Sort by</label><select name="sort"><option value="subdomain">name</option><option value="record_type">type</option><option value="zone">zone</option><option value="role_name">role</option></select></div>
                <div class="actions"><button class="btn primary">Apply</button><a href="{{ url_for('subdomains') }}" class="btn">Reset</a></div>
              </form>
            </div>
            <div class="panel"><table><thead><tr><th>Color</th><th>Type</th><th>Subdomain</th><th>Zone</th><th>IP addresses</th><th>Role</th></tr></thead><tbody>
            {% for row in rows %}<tr><td>{% if sd_colors[row.id] %}<span class="dot" style="background:{{ sd_colors[row.id] }}"></span>{% endif %}</td><td>{{ row.record_type }}</td><td><a href="{{ url_for('subdomain_detail', subdomain_id=row.id) }}">{{ row.subdomain }}</a></td><td>{{ row.zone }}</td><td style="white-space:pre-line" class="mono">{{ row.ips or '—' }}</td><td>{{ row.role_name or '—' }}</td></tr>{% endfor %}
            </tbody></table></div>
            <div class="panel"><h2>Add subdomain</h2>
              <form method="post" action="{{ url_for('subdomain_create') }}" class="grid">
                <div><label>Type</label><select name="record_type">{% for rt in record_types %}<option>{{ rt }}</option>{% endfor %}</select></div>
                <div><label>Name</label><input required name="subdomain"></div>
                <div><label>Zone</label><select name="zone_id">{% for z in zones %}<option value="{{ z.id }}">{{ z.zone }}</option>{% endfor %}</select></div>
                <div><label>Role</label><select name="role_id"><option value="">None</option>{% for r in roles %}<option value="{{ r.id }}">{{ r.role_name }}</option>{% endfor %}</select></div>
                <div class="actions"><button class="btn primary">Create</button></div>
              </form>
            </div>
            """,
            rows=rows,
            zones=q("SELECT id, zone FROM zones ORDER BY zone"),
            roles=q("SELECT id, role_name FROM roles ORDER BY role_name"),
            record_types=SUBDOMAIN_RECORD_TYPES,
            sd_colors={int(row["id"]): resolve_subdomain_color(row) for row in rows},
            request=request,
        )
        return render(body, "subdomains")

    @app.post("/subdomains/create")
    def subdomain_create() -> Response:
        record_type = request.form["record_type"]
        if record_type not in SUBDOMAIN_RECORD_TYPES:
            flash("Unsupported record type.", "bad")
            return redirect(url_for("subdomains"))

        execute(
            "INSERT INTO subdomains(record_type, subdomain, zone_id, role_id) VALUES (?, ?, ?, ?)",
            (
                record_type,
                request.form["subdomain"].strip(),
                request.form["zone_id"],
                request.form.get("role_id") or None,
            ),
        )
        flash("Subdomain created.", "ok")
        return redirect(url_for("subdomains"))

    @app.route("/subdomains/<int:subdomain_id>", methods=["GET", "POST"])
    def subdomain_detail(subdomain_id: int) -> str | Response:
        if request.method == "POST":
            record_type = request.form["record_type"]
            if record_type not in SUBDOMAIN_RECORD_TYPES:
                flash("Unsupported record type.", "bad")
                return redirect(url_for("subdomain_detail", subdomain_id=subdomain_id))

            execute(
                "UPDATE subdomains SET record_type=?, subdomain=?, zone_id=?, role_id=? WHERE id=?",
                (
                    record_type,
                    request.form["subdomain"].strip(),
                    request.form["zone_id"],
                    request.form.get("role_id") or None,
                    subdomain_id,
                ),
            )
            chosen_ip_ids = {int(v) for v in request.form.getlist("ip_ids")}
            existing = {int(r["ip_id"]) for r in q("SELECT ip_id FROM subdomain_ip_map WHERE subdomain_id=?", (subdomain_id,))}
            for ip_id in chosen_ip_ids - existing:
                execute("INSERT INTO subdomain_ip_map(subdomain_id, ip_id) VALUES (?, ?)", (subdomain_id, ip_id))
            for ip_id in existing - chosen_ip_ids:
                execute("DELETE FROM subdomain_ip_map WHERE subdomain_id=? AND ip_id=?", (subdomain_id, ip_id))
            flash("Subdomain updated.", "ok")
            return redirect(url_for("subdomain_detail", subdomain_id=subdomain_id))

        row = q_one("SELECT * FROM subdomains WHERE id=?", (subdomain_id,))
        row_or_redirect = require_row(row, "Subdomain not found.", "subdomains")
        if isinstance(row_or_redirect, Response):
            return row_or_redirect
        row = row_or_redirect

        selected_ip_ids = {int(r["ip_id"]) for r in q("SELECT ip_id FROM subdomain_ip_map WHERE subdomain_id=?", (subdomain_id,))}
        servers_rows = q(
            """
            SELECT DISTINCT s.id, s.hostname, z.zone
            FROM servers s
            LEFT JOIN zones z ON z.id=s.primary_zone_id
            JOIN ip_addresses ip ON ip.server_id=s.id
            JOIN subdomain_ip_map sm ON sm.ip_id=ip.id
            WHERE sm.subdomain_id=?
            ORDER BY s.hostname
            """,
            (subdomain_id,),
        )
        body = render_template_string(
            """
            <div class="panel"><h1>Subdomain #{{ row.id }}</h1>
              <form id="sd-form" class="readonly" method="post"><div class="grid">
                <div><label>Type</label><select data-editable disabled name="record_type">{% for rt in record_types %}<option {% if rt == row.record_type %}selected{% endif %}>{{ rt }}</option>{% endfor %}</select></div>
                <div><label>Name</label><input data-editable readonly name="subdomain" value="{{ row.subdomain }}"></div>
                <div><label>Zone</label><select data-editable disabled name="zone_id">{% for z in zones %}<option value="{{ z.id }}" {% if z.id == row.zone_id %}selected{% endif %}>{{ z.zone }}</option>{% endfor %}</select></div>
                <div><label>Role</label><select data-editable disabled name="role_id"><option value="">None</option>{% for r in roles %}<option value="{{ r.id }}" {% if r.id == row.role_id %}selected{% endif %}>{{ r.role_name }}</option>{% endfor %}</select></div>
                <div style="grid-column:1/-1"><label>Linked IP addresses</label>
                  <select data-editable disabled name="ip_ids" multiple size="8">{% for ip in ips %}<option value="{{ ip.id }}" {% if ip.id in selected_ip_ids %}selected{% endif %}>{{ ip.ip }}</option>{% endfor %}</select>
                </div>
              </div><div class="actions"><button type="button" class="btn" onclick="enableEdit('sd-form')">Edit</button><button class="btn primary save-btn" disabled>Save</button></div></form>
            </div>
            <div class="panel"><h2>Servers mapped by this subdomain</h2><ul>{% for s in servers_rows %}<li>{{ s.hostname }}{% if s.zone %}.{{ s.zone }}{% endif %}</li>{% else %}<li>—</li>{% endfor %}</ul></div>
            <div class="panel"><form method="post" action="{{ url_for('subdomain_delete', subdomain_id=row.id) }}" onsubmit="return confirm('Delete subdomain?')"><button class="btn danger">Delete subdomain</button></form></div>
            """,
            row=row,
            record_types=SUBDOMAIN_RECORD_TYPES,
            zones=q("SELECT id, zone FROM zones ORDER BY zone"),
            roles=q("SELECT id, role_name FROM roles ORDER BY role_name"),
            ips=q("SELECT id, ip FROM ip_addresses ORDER BY ip"),
            selected_ip_ids=selected_ip_ids,
            servers_rows=servers_rows,
        )
        return render(body, "subdomains")

    @app.post("/subdomains/<int:subdomain_id>/delete")
    def subdomain_delete(subdomain_id: int) -> Response:
        execute("DELETE FROM subdomains WHERE id=?", (subdomain_id,))
        flash("Subdomain deleted.", "ok")
        return redirect(url_for("subdomains"))

    # ---- ROLES ----
    @app.route("/roles")
    def roles() -> str:
        role_name = request.args.get("role_name", "").strip()
        description = request.args.get("description", "").strip()
        color = request.args.get("color", "").strip()
        order = request.args.get("sort", "role_name")
        allowed = {"role_name", "description", "color"}
        order = order if order in allowed else "role_name"

        where_parts: list[str] = []
        params: list[Any] = []
        if role_name:
            where_parts.append("role_name LIKE ?")
            params.append(f"%{role_name}%")
        if description:
            where_parts.append("description LIKE ?")
            params.append(f"%{description}%")
        if color:
            where_parts.append("color = ?")
            params.append(color)

        where = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        rows = q(f"SELECT * FROM roles{where} ORDER BY {order} COLLATE NOCASE", params)

        body = render_template_string(
            """
            <div class="panel"><h1>Roles</h1>
              <form method="get" class="grid">
                <div><label>Name contains</label><input name="role_name" value="{{ request.args.get('role_name', '') }}"></div>
                <div><label>Description contains</label><input name="description" value="{{ request.args.get('description', '') }}"></div>
                <div><label>Color</label><input name="color" type="color" value="{{ request.args.get('color', '#8d7b68') }}"></div>
                <div><label>Sort by</label><select name="sort"><option value="role_name">name</option><option value="description">description</option><option value="color">color</option></select></div>
                <div class="actions"><button class="btn primary">Apply</button><a href="{{ url_for('roles') }}" class="btn">Reset</a></div>
              </form>
            </div>
            <div class="panel"><table><thead><tr><th>Color</th><th>Name</th><th>Description</th></tr></thead><tbody>
            {% for row in rows %}<tr title="{{ short(row['description']) }}"><td>{% if row.color %}<span class="dot" style="background:{{ row.color }}"></span>{% endif %}</td><td><a href="{{ url_for('role_detail', role_id=row.id) }}">{{ row.role_name }}</a></td><td>{{ short(row.description) or '—' }}</td></tr>{% endfor %}
            </tbody></table></div>
            <div class="panel"><h2>Add role</h2><form method="post" action="{{ url_for('role_create') }}" class="grid">
              <div><label>Name</label><input required name="role_name"></div>
              <div><label>Color</label><input name="color" type="color" value="#8d7b68"></div>
              <div style="grid-column:1/-1"><label>Description</label><textarea name="description"></textarea></div>
              <div class="actions"><button class="btn primary">Create</button></div>
            </form></div>
            """,
            rows=rows,
            request=request,
            short=short,
        )
        return render(body, "roles")

    @app.post("/roles/create")
    def role_create() -> Response:
        execute(
            "INSERT INTO roles(role_name, description, color) VALUES (?, ?, ?)",
            (request.form["role_name"], request.form.get("description") or None, request.form["color"]),
        )
        flash("Role created.", "ok")
        return redirect(url_for("roles"))

    @app.route("/roles/<int:role_id>", methods=["GET", "POST"])
    def role_detail(role_id: int) -> str | Response:
        if request.method == "POST":
            execute(
                "UPDATE roles SET role_name=?, description=?, color=? WHERE id=?",
                (request.form["role_name"], request.form.get("description") or None, request.form["color"], role_id),
            )
            flash("Role updated.", "ok")
            return redirect(url_for("role_detail", role_id=role_id))

        row = q_one("SELECT * FROM roles WHERE id=?", (role_id,))
        row_or_redirect = require_row(row, "Role not found.", "roles")
        if isinstance(row_or_redirect, Response):
            return row_or_redirect
        row = row_or_redirect

        body = render_template_string(
            """
            <div class="panel"><h1>Role: {{ row.role_name }}</h1><form id="role-form" class="readonly" method="post"><div class="grid">
            <div><label>Name</label><input data-editable readonly name="role_name" value="{{ row.role_name }}"></div>
            <div><label>Color</label><input data-editable disabled type="color" name="color" value="{{ row.color }}"></div>
            <div style="grid-column:1/-1"><label>Description</label><textarea data-editable readonly name="description">{{ row.description or '' }}</textarea></div>
            </div><div class="actions"><button type="button" class="btn" onclick="enableEdit('role-form')">Edit</button><button class="btn primary save-btn" disabled>Save</button></div></form></div>
            <div class="panel"><h2>Servers with this role</h2><ul>{% for s in servers_rows %}<li><a href="{{ url_for('server_detail', server_id=s.id) }}">{{ s.hostname }}</a></li>{% else %}<li>—</li>{% endfor %}</ul></div>
            <div class="panel"><h2>IP addresses with this role</h2><ul>{% for ip in ips_rows %}<li><a href="{{ url_for('ip_detail', ip_id=ip.id) }}">{{ ip.ip }}</a></li>{% else %}<li>—</li>{% endfor %}</ul></div>
            <div class="panel"><h2>Subdomains with this role</h2><ul>{% for sd in subdomains_rows %}<li><a href="{{ url_for('subdomain_detail', subdomain_id=sd.id) }}">{{ sd.record_type }} {{ sd.subdomain }}</a></li>{% else %}<li>—</li>{% endfor %}</ul></div>
            <div class="panel"><form method="post" action="{{ url_for('role_delete', role_id=row.id) }}" onsubmit="return confirm('Delete role?')"><button class="btn danger">Delete role</button></form></div>
            """,
            row=row,
            servers_rows=q("SELECT id, hostname FROM servers WHERE role_id=? ORDER BY hostname", (role_id,)),
            ips_rows=q("SELECT id, ip FROM ip_addresses WHERE role_id=? ORDER BY ip", (role_id,)),
            subdomains_rows=q("SELECT id, record_type, subdomain FROM subdomains WHERE role_id=? ORDER BY subdomain", (role_id,)),
        )
        return render(body, "roles")

    @app.post("/roles/<int:role_id>/delete")
    def role_delete(role_id: int) -> Response:
        execute("DELETE FROM roles WHERE id=?", (role_id,))
        flash("Role deleted.", "ok")
        return redirect(url_for("roles"))

    # ---- HOSTINGS ----
    @app.route("/hostings")
    def hostings() -> str:
        name = request.args.get("name", "").strip()
        url_filter = request.args.get("url", "").strip()
        paid_until = request.args.get("paid_until", "").strip()
        description = request.args.get("description", "").strip()
        order = request.args.get("sort", "name")
        allowed = {"name", "url", "paid_until", "description"}
        order = order if order in allowed else "name"

        where_parts: list[str] = []
        params: list[Any] = []
        if name:
            where_parts.append("name LIKE ?")
            params.append(f"%{name}%")
        if url_filter:
            where_parts.append("url LIKE ?")
            params.append(f"%{url_filter}%")
        if paid_until:
            where_parts.append("paid_until = ?")
            params.append(paid_until)
        if description:
            where_parts.append("description LIKE ?")
            params.append(f"%{description}%")

        where = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        rows = q(
            f"""
            SELECT h.*, (
                SELECT COUNT(*)
                FROM servers s
                WHERE s.hosting_id = h.id
            ) AS server_count
            FROM hostings h{where}
            ORDER BY {order} COLLATE NOCASE
            """,
            params,
        )

        body = render_template_string(
            """
            <div class="panel"><h1>Hostings</h1>
              <form method="get" class="grid">
                <div><label>Name contains</label><input name="name" value="{{ request.args.get('name', '') }}"></div>
                <div><label>URL contains</label><input name="url" value="{{ request.args.get('url', '') }}"></div>
                <div><label>Paid until</label><input type="date" name="paid_until" value="{{ request.args.get('paid_until', '') }}"></div>
                <div><label>Description contains</label><input name="description" value="{{ request.args.get('description', '') }}"></div>
                <div><label>Sort by</label><select name="sort"><option value="name">name</option><option value="url">url</option><option value="paid_until">paid until</option><option value="description">description</option></select></div>
                <div class="actions"><button class="btn primary">Apply</button><a href="{{ url_for('hostings') }}" class="btn">Reset</a></div>
              </form>
            </div>
            <div class="panel"><table><thead><tr><th>Name</th><th>URL</th><th>servers</th><th>Paid until</th><th>Description</th></tr></thead><tbody>
            {% for row in rows %}<tr title="{{ short(row['description']) }}"><td><a href="{{ url_for('hosting_detail', hosting_id=row.id) }}">{{ row.name }}</a></td><td>{% if row.url %}<a href="{{ row.url }}" target="_blank" rel="noopener noreferrer">{{ row.url }}</a>{% else %}—{% endif %}</td><td>{{ row.server_count }}</td><td>{{ row.paid_until or '—' }}</td><td>{{ short(row.description) or '—' }}</td></tr>{% endfor %}
            </tbody></table></div>
            <div class="panel"><h2>Add hosting</h2><form method="post" action="{{ url_for('hosting_create') }}" class="grid">
              <div><label>Name</label><input required name="name"></div><div><label>URL</label><input name="url" type="url"></div><div><label>Paid until</label><input name="paid_until" type="date"></div>
              <div style="grid-column:1/-1"><label>Description</label><textarea name="description"></textarea></div><div class="actions"><button class="btn primary">Create</button></div></form></div>
            """,
            rows=rows,
            request=request,
            short=short,
        )
        return render(body, "hostings")

    @app.post("/hostings/create")
    def hosting_create() -> Response:
        execute(
            "INSERT INTO hostings(name, url, paid_until, description) VALUES (?, ?, ?, ?)",
            (request.form["name"], request.form.get("url") or None, request.form.get("paid_until") or None, request.form.get("description") or None),
        )
        flash("Hosting created.", "ok")
        return redirect(url_for("hostings"))

    @app.route("/hostings/<int:hosting_id>", methods=["GET", "POST"])
    def hosting_detail(hosting_id: int) -> str | Response:
        if request.method == "POST":
            execute(
                "UPDATE hostings SET name=?, url=?, paid_until=?, description=? WHERE id=?",
                (request.form["name"], request.form.get("url") or None, request.form.get("paid_until") or None, request.form.get("description") or None, hosting_id),
            )
            flash("Hosting updated.", "ok")
            return redirect(url_for("hosting_detail", hosting_id=hosting_id))

        row = q_one("SELECT * FROM hostings WHERE id=?", (hosting_id,))
        row_or_redirect = require_row(row, "Hosting not found.", "hostings")
        if isinstance(row_or_redirect, Response):
            return row_or_redirect
        row = row_or_redirect

        body = render_template_string(
            """
            <div class="panel"><h1>Hosting: {{ row.name }}</h1><form id="hosting-form" class="readonly" method="post"><div class="grid">
            <div><label>Name</label><input data-editable readonly name="name" value="{{ row.name }}"></div>
            <div><label>URL</label><input data-editable readonly type="url" name="url" value="{{ row.url or '' }}"></div>
            <div><label>Paid until</label><input data-editable readonly type="date" name="paid_until" value="{{ row.paid_until or '' }}"></div>
            <div style="grid-column:1/-1"><label>Description</label><textarea data-editable readonly name="description">{{ row.description or '' }}</textarea></div>
            </div><div class="actions"><button type="button" class="btn" onclick="enableEdit('hosting-form')">Edit</button><button class="btn primary save-btn" disabled>Save</button></div></form></div>
            <div class="panel"><h2>Servers in this hosting</h2><ul>{% for s in servers_rows %}<li><a href="{{ url_for('server_detail', server_id=s.id) }}">{{ s.hostname }}</a></li>{% else %}<li>—</li>{% endfor %}</ul></div>
            <div class="panel"><h2>External link</h2>{% if row.url %}<a href="{{ row.url }}" target="_blank" rel="noopener noreferrer">{{ row.url }}</a>{% else %}<span>—</span>{% endif %}</div>
            <div class="panel"><form method="post" action="{{ url_for('hosting_delete', hosting_id=row.id) }}" onsubmit="return confirm('Delete hosting?')"><button class="btn danger">Delete hosting</button></form></div>
            """,
            row=row,
            servers_rows=q("SELECT id, hostname FROM servers WHERE hosting_id=? ORDER BY hostname", (hosting_id,)),
        )
        return render(body, "hostings")

    @app.post("/hostings/<int:hosting_id>/delete")
    def hosting_delete(hosting_id: int) -> Response:
        execute("DELETE FROM hostings WHERE id=?", (hosting_id,))
        flash("Hosting deleted.", "ok")
        return redirect(url_for("hostings"))

    # ---- ZONES ----
    @app.route("/zones")
    def zones() -> str:
        filters = {
            "zone": request.args.get("zone", "").strip(),
            "mname": request.args.get("mname", "").strip(),
            "rname": request.args.get("rname", "").strip(),
            "serial": request.args.get("serial", "").strip(),
            "refresh": request.args.get("refresh", "").strip(),
            "retry": request.args.get("retry", "").strip(),
            "expire": request.args.get("expire", "").strip(),
            "minimum": request.args.get("minimum", "").strip(),
            "psql_address": request.args.get("psql_address", "").strip(),
            "psql_user": request.args.get("psql_user", "").strip(),
            "description": request.args.get("description", "").strip(),
        }
        order = request.args.get("sort", "zone")
        allowed = {"zone", "mname", "rname", "serial", "refresh", "retry", "expire", "minimum", "psql_address", "psql_user", "description"}
        order = order if order in allowed else "zone"

        where_parts: list[str] = []
        params: list[Any] = []
        for field, value in filters.items():
            if not value:
                continue
            where_parts.append(f"{field} LIKE ?")
            params.append(f"%{value}%")

        where = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        rows = q(f"SELECT * FROM zones{where} ORDER BY {order} COLLATE NOCASE", params)

        body = render_template_string(
            """
            <div class="panel"><h1>Zones</h1>
              <form method="get" class="grid">
                <div><label>Zone contains</label><input name="zone" value="{{ request.args.get('zone', '') }}"></div>
                <div><label>MNAME contains</label><input name="mname" value="{{ request.args.get('mname', '') }}"></div>
                <div><label>RNAME contains</label><input name="rname" value="{{ request.args.get('rname', '') }}"></div>
                <div><label>SERIAL contains</label><input name="serial" value="{{ request.args.get('serial', '') }}"></div>
                <div><label>REFRESH contains</label><input name="refresh" value="{{ request.args.get('refresh', '') }}"></div>
                <div><label>RETRY contains</label><input name="retry" value="{{ request.args.get('retry', '') }}"></div>
                <div><label>EXPIRE contains</label><input name="expire" value="{{ request.args.get('expire', '') }}"></div>
                <div><label>MINIMUM contains</label><input name="minimum" value="{{ request.args.get('minimum', '') }}"></div>
                <div><label>psql address contains</label><input name="psql_address" value="{{ request.args.get('psql_address', '') }}"></div>
                <div><label>psql user contains</label><input name="psql_user" value="{{ request.args.get('psql_user', '') }}"></div>
                <div><label>Description contains</label><input name="description" value="{{ request.args.get('description', '') }}"></div>
                <div><label>Sort by</label><select name="sort"><option value="zone">zone</option><option value="mname">mname</option><option value="rname">rname</option><option value="serial">serial</option><option value="refresh">refresh</option><option value="retry">retry</option><option value="expire">expire</option><option value="minimum">minimum</option><option value="psql_address">psql address</option><option value="psql_user">psql user</option><option value="description">description</option></select></div>
                <div class="actions"><button class="btn primary">Apply</button><a href="{{ url_for('zones') }}" class="btn">Reset</a></div>
              </form>
            </div>
            <div class="panel"><table><thead><tr><th>Zone</th><th>MNAME</th><th>RNAME</th><th>SERIAL</th><th>REFRESH</th><th>RETRY</th><th>EXPIRE</th><th>MINIMUM</th><th>psql address</th><th>psql user</th><th>Description</th></tr></thead><tbody>
            {% for row in rows %}<tr title="{{ short(row['description']) }}"><td><a href="{{ url_for('zone_detail', zone_id=row.id) }}">{{ row.zone }}</a></td><td>{{ row.mname }}</td><td>{{ row.rname }}</td><td>{{ row.serial }}</td><td>{{ row.refresh }}</td><td>{{ row.retry }}</td><td>{{ row.expire }}</td><td>{{ row.minimum }}</td><td>{{ row.psql_address or '—' }}</td><td>{{ row.psql_user or '—' }}</td><td>{{ short(row.description) or '—' }}</td></tr>{% endfor %}
            </tbody></table></div>
            <div class="panel"><h2>Add zone</h2><form method="post" action="{{ url_for('zone_create') }}" class="grid">
              <div><label>Zone</label><input required name="zone"></div><div><label>MNAME</label><input required name="mname"></div><div><label>RNAME</label><input required name="rname"></div><div><label>SERIAL</label><input required name="serial"></div>
              <div><label>REFRESH</label><input required name="refresh"></div><div><label>RETRY</label><input required name="retry"></div><div><label>EXPIRE</label><input required name="expire"></div><div><label>MINIMUM</label><input required name="minimum"></div>
              <div><label>PostgreSQL address</label><input name="psql_address"></div><div><label>DB name</label><input name="db_name" placeholder="powerdns"></div><div><label>PostgreSQL user</label><input name="psql_user"></div><div><label>PostgreSQL password</label><input type="password" name="psql_password"></div>
              <div style="grid-column:1/-1"><label>Description</label><textarea name="description"></textarea></div><div class="actions"><button class="btn primary">Create</button></div>
            </form></div>
            """,
            rows=rows,
            request=request,
            short=short,
        )
        return render(body, "zones")

    @app.post("/zones/create")
    def zone_create() -> Response:
        data = tuple(
            request.form.get(k) or None
            for k in ("zone", "mname", "rname", "serial", "refresh", "retry", "expire", "minimum", "description", "psql_address", "db_name", "psql_user", "psql_password")
        )
        execute("INSERT INTO zones(zone,mname,rname,serial,refresh,retry,expire,minimum,description,psql_address,db_name,psql_user,psql_password) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", data)
        flash("Zone created.", "ok")
        return redirect(url_for("zones"))

    @app.route("/zones/<int:zone_id>", methods=["GET", "POST"])
    def zone_detail(zone_id: int) -> str | Response:
        if request.method == "POST":
            current = q_one("SELECT psql_password FROM zones WHERE id=?", (zone_id,))
            if not current:
                flash("Zone not found.", "bad")
                return redirect(url_for("zones"))

            password = request.form.get("psql_password")
            final_password = current["psql_password"] if password == "" else password

            execute(
                """
                UPDATE zones SET zone=?, mname=?, rname=?, serial=?, refresh=?, retry=?, expire=?, minimum=?,
                description=?, psql_address=?, db_name=?, psql_user=?, psql_password=? WHERE id=?
                """,
                (
                    request.form["zone"], request.form["mname"], request.form["rname"], request.form["serial"],
                    request.form["refresh"], request.form["retry"], request.form["expire"], request.form["minimum"],
                    request.form.get("description") or None, request.form.get("psql_address") or None,
                    request.form.get("db_name") or None, request.form.get("psql_user") or None, final_password, zone_id,
                ),
            )
            flash("Zone updated.", "ok")
            return redirect(url_for("zone_detail", zone_id=zone_id))

        row = q_one("SELECT * FROM zones WHERE id=?", (zone_id,))
        row_or_redirect = require_row(row, "Zone not found.", "zones")
        if isinstance(row_or_redirect, Response):
            return row_or_redirect
        row = row_or_redirect

        body = render_template_string(
            """
            <div class="panel"><h1>Zone: {{ row.zone }}</h1><form id="zone-form" class="readonly" method="post"><div class="grid">
            {% for name in ['zone','mname','rname','serial','refresh','retry','expire','minimum','psql_address','db_name','psql_user'] %}
              <div><label>{{ name }}</label><input data-editable readonly name="{{ name }}" value="{{ row[name] or '' }}"></div>
            {% endfor %}
            <div><label>psql_password</label><input data-editable readonly name="psql_password" type="password" placeholder="Leave empty to keep unchanged"></div>
            <div style="grid-column:1/-1"><label>Description</label><textarea data-editable readonly name="description">{{ row.description or '' }}</textarea></div>
            </div><div class="actions"><button type="button" class="btn" onclick="enableEdit('zone-form')">Edit</button><button class="btn primary save-btn" disabled>Save</button><a class="btn" href="{{ url_for('dns', zone_id=row.id) }}">Go to DNS tab</a></div></form></div>
            <div class="panel"><form method="post" action="{{ url_for('zone_delete', zone_id=row.id) }}" onsubmit="return confirm('Delete zone?')"><button class="btn danger">Delete zone</button></form></div>
            """,
            row=row,
        )
        return render(body, "zones")

    @app.post("/zones/<int:zone_id>/delete")
    def zone_delete(zone_id: int) -> Response:
        execute("DELETE FROM zones WHERE id=?", (zone_id,))
        flash("Zone deleted.", "ok")
        return redirect(url_for("zones"))

    # ---- DNS TAB ----
    @app.route("/dns", methods=["GET", "POST"])
    def dns() -> str | Response:
        zones_rows = q("SELECT id, zone FROM zones ORDER BY zone")
        selected_zone_id = int(request.values.get("zone_id", zones_rows[0]["id"] if zones_rows else 0)) if zones_rows else 0
        preview = None
        if request.method == "POST":
            action = request.form.get("action")
            if action == "bind":
                return redirect(url_for("dns_bind_download", zone_id=selected_zone_id))
            if action == "powerdns":
                force = request.form.get("force") == "1"
                preview = powerdns_preview(selected_zone_id, force=force)

        body = render_template_string(
            """
            <div class="panel"><h1>DNS</h1><p class="muted">Push to PowerDNS/PostgreSQL or generate Bind9 config.</p></div>
            <div class="panel"><h2>PowerDNS + PostgreSQL</h2>
              <form method="post" class="grid"><div><label>Zone</label><select name="zone_id">{% for z in zones_rows %}<option value="{{ z.id }}" {% if z.id == selected_zone_id %}selected{% endif %}>{{ z.zone }}</option>{% endfor %}</select></div>
                <div class="actions"><button class="btn primary" name="action" value="powerdns">Push</button></div>
              </form>
            </div>
            <div class="panel"><h2>Bind9</h2>
              <form method="post" class="grid"><div><label>Zone</label><select name="zone_id">{% for z in zones_rows %}<option value="{{ z.id }}" {% if z.id == selected_zone_id %}selected{% endif %}>{{ z.zone }}</option>{% endfor %}</select></div>
                <div class="actions"><button class="btn" name="action" value="bind">Generate config</button></div>
              </form>
            </div>
            {% if preview %}
              <div class="panel"><h2>PowerDNS preview</h2>
                {% if preview.error %}<div class="flash-bad">{{ preview.error }}</div>{% else %}
                  <p><b>Remote SERIAL:</b> {{ preview.remote_serial }} | <b>Local SERIAL:</b> {{ preview.local_serial }} | <b>New SERIAL:</b> {{ preview.new_serial }}</p>{% if preview.warning %}<div class="flash-bad">Warning: {{ preview.warning }}</div>{% endif %}
                  <pre class="mono">{{ preview.diff_text }}</pre>
                  <h3>SQL preview</h3><pre class="mono">{{ preview.sql_preview }}</pre>
                  {% if preview.require_force %}
                  <form method="post" action="{{ url_for('dns') }}">
                    <input type="hidden" name="zone_id" value="{{ selected_zone_id }}">
                    <input type="hidden" name="action" value="powerdns">
                    <input type="hidden" name="force" value="1">
                    <button class="btn">Все равно продолжить</button>
                  </form>
                  {% else %}
                  <form method="post" action="{{ url_for('dns_powerdns_apply') }}">
                    <input type="hidden" name="zone_id" value="{{ selected_zone_id }}">
                    <input type="hidden" name="plan_token" value="{{ preview.plan_token }}">
                    <button class="btn primary" onclick="return confirm('Apply update to PowerDNS?')">Confirm apply</button>
                  </form>
                  {% endif %}
                {% endif %}
              </div>
            {% endif %}
            """,
            zones_rows=zones_rows,
            selected_zone_id=selected_zone_id,
            preview=preview,
        )
        return render(body, "dns")

    def powerdns_preview(zone_id: int, force: bool = False) -> dict[str, Any]:
        log_powerdns(1, f"Начинаем подготовку push для zone_id={zone_id}, force={force}")

        zone = q_one("SELECT * FROM zones WHERE id=?", (zone_id,))
        if not zone:
            log_powerdns(1, "Зона не найдена в SQLite")
            return {"error": "Zone not found"}

        log_powerdns(1, f"Получены данные зоны из SQLite: zone={zone['zone']}, serial={zone['serial']}")

        if psycopg2 is None:
            log_powerdns(1, "Библиотека psycopg2 недоступна")
            return {"error": "psycopg2 is not installed"}

        if not zone["psql_address"] or not zone["psql_user"]:
            log_powerdns(1, "Неполные параметры подключения к PostgreSQL")
            return {"error": "PostgreSQL connection data is incomplete"}

        new_serial = make_serial()
        desired_records = build_zone_records(zone_id)

        # 1) domain_id + SOA
        try:
            log_powerdns(2, f"Ищем domain_id в domains для зоны {zone['zone']}")
            with closing(pg_connect_from_zone(zone, with_timeouts=False)) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id FROM domains WHERE name=%s", (zone["zone"],))
                    dom = cur.fetchone()
                    if not dom:
                        log_powerdns(2, "Зона не найдена в таблице domains")
                        return {"error": f"Domain {zone['zone']} not found in PowerDNS"}
                    domain_id = int(dom[0])
                    log_powerdns(2, f"Найден domain_id={domain_id}")

                    log_powerdns(3, "Получаем SOA-записи для зоны")
                    cur.execute(
                        "SELECT id, name, type, content, ttl, disabled, auth FROM records WHERE domain_id=%s AND type='SOA'",
                        (domain_id,),
                    )
                    remote_soa_records = [tuple(r) for r in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            log_powerdns(2, f"Ошибка PostgreSQL: {exc}")
            return {"error": f"PostgreSQL error: {exc}"}

        if len(remote_soa_records) != 1:
            log_powerdns(3, f"Некорректное количество SOA-записей: {len(remote_soa_records)}")
            return {"error": f"Expected exactly 1 SOA record in PowerDNS, found {len(remote_soa_records)}"}

        log_powerdns(3, "SOA-запись успешно получена")

        log_powerdns(4, "Парсим SERIAL из содержимого SOA")
        remote_serial = parse_remote_serial(remote_soa_records[0][3])
        log_powerdns(4, f"SERIAL в PowerDNS: {remote_serial}")

        serial_warning = None
        require_force = False
        log_powerdns(5, f"Сравниваем SERIAL SQLite={zone['serial']} и PostgreSQL={remote_serial}")
        if str(zone["serial"] or "") != str(remote_serial):
            serial_warning = "Local SERIAL is out of sync with Remote SERIAL."
            require_force = not force
            log_powerdns(6, f"SERIAL различается. require_force={require_force}")

        if require_force:
            log_powerdns(6, "Останавливаемся до подтверждения пользователя")
            return {
                "error": None,
                "warning": serial_warning,
                "require_force": True,
                "remote_serial": remote_serial,
                "local_serial": zone["serial"],
                "new_serial": new_serial,
                "add": [],
                "delete": [],
                "diff_text": "Ожидается подтверждение: SERIAL в SQLite и PowerDNS различаются.",
                "sql_preview": "-- ожидается подтверждение пользователя",
                "domain_id": domain_id,
            }

        # 2) read all records
        log_powerdns(8, f"Получаем все записи records для domain_id={domain_id}")
        try:
            with closing(pg_connect_from_zone(zone, with_timeouts=True)) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT id, name, type, content, ttl, disabled, auth FROM records WHERE domain_id=%s", (domain_id,))
                    remote_rows = [tuple(r) for r in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            log_powerdns(8, f"Ошибка чтения records: {exc}")
            return {"error": f"PostgreSQL error: {exc}"}

        log_powerdns(9, "Формируем новый список записей из SQLite и обновлённую SOA")
        soa_content = f"{zone['mname']} {zone['rname']} {new_serial} {zone['refresh']} {zone['retry']} {zone['expire']} {zone['minimum']}"

        desired_set = {(r.fqdn.lower(), r.record_type, r.content, 3600, False, True) for r in desired_records if r.record_type != "SOA"}
        desired_set.add((zone["zone"].lower(), "SOA", soa_content, 3600, False, True))

        normalized_remote_set = {
            (name.lower(), record_type, content, int(ttl) if ttl is not None else 3600, bool(disabled), bool(auth))
            for _, name, record_type, content, ttl, disabled, auth in remote_rows
            if record_type in POWERDNS_SYNC_RECORD_TYPES
        }

        add_set = desired_set - normalized_remote_set
        del_set = normalized_remote_set - desired_set

        log_powerdns(10, f"DIFF сформирован: add={len(add_set)}, delete={len(del_set)}")

        diff_lines = [
            "+ " + " | ".join((name, record_type, content, str(ttl), str(disabled), str(auth)))
            for name, record_type, content, ttl, disabled, auth in sorted(add_set)
        ] + [
            "- " + " | ".join((name, record_type, content, str(ttl), str(disabled), str(auth)))
            for name, record_type, content, ttl, disabled, auth in sorted(del_set)
        ]

        log_powerdns(11, "Формируем SQL-preview")
        sql_preview_lines: list[str] = []
        for name, record_type, content, ttl, disabled, auth in sorted(del_set):
            sql_preview_lines.append(
                "DELETE FROM records "
                f"WHERE domain_id={domain_id} AND name='{format_sql_preview(name)}' AND type='{format_sql_preview(record_type)}' "
                f"AND content='{format_sql_preview(content)}' AND ttl={ttl} AND disabled={'true' if disabled else 'false'} "
                f"AND auth={'true' if auth else 'false'};"
            )
        for name, record_type, content, ttl, disabled, auth in sorted(add_set):
            sql_preview_lines.append(
                "INSERT INTO records(domain_id, name, type, content, ttl, disabled, auth) VALUES "
                f"({domain_id}, '{format_sql_preview(name)}', '{format_sql_preview(record_type)}', '{format_sql_preview(content)}', {ttl}, "
                f"{'true' if disabled else 'false'}, {'true' if auth else 'false'});"
            )

        plan_token = powerdns_cache_save(
            {
                "zone_id": zone_id,
                "domain_id": domain_id,
                "new_serial": new_serial,
                "add": sorted(add_set),
                "delete": sorted(del_set),
            }
        )

        return {
            "error": None,
            "warning": serial_warning,
            "require_force": False,
            "remote_serial": remote_serial,
            "local_serial": zone["serial"],
            "new_serial": new_serial,
            "add": sorted(add_set),
            "delete": sorted(del_set),
            "diff_text": "\n".join(diff_lines) or "No changes",
            "sql_preview": "\n".join(sql_preview_lines) or "-- no SQL changes",
            "domain_id": domain_id,
            "plan_token": plan_token,
        }

    @app.post("/dns/powerdns/apply")
    def dns_powerdns_apply() -> Response:
        zone_id = int(request.form["zone_id"])
        plan_token = (request.form.get("plan_token") or "").strip()

        log_powerdns(13, f"Начинаем применение SQL для zone_id={zone_id}")
        zone = q_one("SELECT * FROM zones WHERE id=?", (zone_id,))
        if not zone:
            log_powerdns(13, "Зона не найдена")
            flash("Zone not found", "bad")
            return redirect(url_for("dns", zone_id=zone_id))

        plan = powerdns_cache_take(plan_token) if plan_token else None
        if not plan or int(plan.get("zone_id", -1)) != zone_id:
            log_powerdns(13, "План применения не найден или устарел")
            flash("Preview is expired. Please run push again and confirm apply.", "bad")
            return redirect(url_for("dns", zone_id=zone_id))

        new_serial = plan["new_serial"]

        try:
            with closing(pg_connect_from_zone(zone, with_timeouts=True)) as conn:
                with conn:  # transaction scope
                    with conn.cursor() as cur:
                        cur.execute("SELECT id FROM domains WHERE name=%s", (zone["zone"],))
                        domain_row = cur.fetchone()
                        if not domain_row:
                            raise RuntimeError(f"Domain {zone['zone']} not found in PowerDNS during apply")

                        remote_domain_id = int(domain_row[0])
                        if remote_domain_id != int(plan["domain_id"]):
                            raise RuntimeError("Domain ID changed between preview and apply")

                        delete_rows = [
                            (remote_domain_id, name, record_type, content, ttl, disabled, auth)
                            for name, record_type, content, ttl, disabled, auth in plan["delete"]
                        ]
                        if delete_rows:
                            psycopg2_extras.execute_values(
                                cur,
                                """
                                DELETE FROM records AS r
                                USING (VALUES %s) AS d(domain_id, name, type, content, ttl, disabled, auth)
                                WHERE r.domain_id=d.domain_id
                                  AND r.name=d.name
                                  AND r.type=d.type
                                  AND r.content=d.content
                                  AND r.ttl=d.ttl
                                  AND r.disabled=d.disabled
                                  AND r.auth=d.auth
                                """,
                                delete_rows,
                                page_size=200,
                            )

                        add_rows = [
                            (remote_domain_id, name, record_type, content, ttl, disabled, auth)
                            for name, record_type, content, ttl, disabled, auth in plan["add"]
                        ]
                        if add_rows:
                            psycopg2_extras.execute_values(
                                cur,
                                "INSERT INTO records(domain_id, name, type, content, ttl, disabled, auth) VALUES %s",
                                add_rows,
                                page_size=200,
                            )
        except Exception as exc:  # noqa: BLE001
            log_powerdns(14, f"Ошибка применения SQL: {exc}")
            flash(f"PowerDNS apply failed: {exc}", "bad")
            return redirect(url_for("dns", zone_id=zone_id))

        log_powerdns(14, "SQL успешно выполнен в PostgreSQL")
        log_powerdns(15, f"Обновляем SERIAL в SQLite на {new_serial}")
        execute("UPDATE zones SET serial=? WHERE id=?", (new_serial, zone_id))
        flash("PowerDNS update applied and local serial updated.", "ok")
        return redirect(url_for("dns", zone_id=zone_id))

    @app.get("/dns/bind/<int:zone_id>.db")
    def dns_bind_download(zone_id: int) -> Response:
        zone = q_one("SELECT * FROM zones WHERE id=?", (zone_id,))
        if not zone:
            flash("Zone not found.", "bad")
            return redirect(url_for("dns"))

        new_serial = make_serial()
        records = build_zone_records(zone_id)
        lines = [
            "$TTL 3600",
            f"@ IN SOA {zone['mname']} {zone['rname']} ({new_serial} {zone['refresh']} {zone['retry']} {zone['expire']} {zone['minimum']})",
        ]
        for rec in records:
            if rec.record_type == "SOA":
                continue
            lines.append(f"{rec.fqdn}. IN {rec.record_type} {rec.content}")

        payload = "\n".join(lines).encode("utf-8")
        return send_file(BytesIO(payload), as_attachment=True, download_name=f"{zone['zone']}.db", mimetype="text/plain")

    @app.errorhandler(Exception)
    def handle_exception(exc: Exception) -> tuple[str, int]:
        if app.config["DEBUG_MODE"]:
            return render(f"<div class='panel'><h1>Debug error</h1><pre class='mono'>{exc}</pre></div>", "servers"), 500
        return render("<div class='panel'><h1>Internal error</h1><p>Unexpected error occurred.</p></div>", "servers"), 500

    return app


def init_database(db_path: Path, init_sql_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(init_sql_path.read_text(encoding="utf-8"))


def validate_database(db_path: Path) -> tuple[bool, str]:
    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = REQUIRED_TABLES - tables
            if missing:
                return False, f"Missing tables: {', '.join(sorted(missing))}"

            invalid_soa_rows = conn.execute(
                """
                SELECT id FROM zones
                WHERE mname IS NULL OR rname IS NULL OR serial IS NULL OR refresh IS NULL
                   OR retry IS NULL OR expire IS NULL OR minimum IS NULL
                LIMIT 1
                """
            ).fetchone()
            if invalid_soa_rows:
                return False, "Zone SOA fields must not be NULL"

            multiple_primary = conn.execute(
                "SELECT server_id, COUNT(*) AS c FROM ip_addresses WHERE is_primary=1 GROUP BY server_id HAVING c > 1 LIMIT 1"
            ).fetchone()
            if multiple_primary:
                return False, f"Server id={multiple_primary['server_id']} has multiple primary IP addresses"
    except sqlite3.DatabaseError as exc:
        return False, f"SQLite error: {exc}"
    return True, "OK"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sinver.py",
        description="SINVER — локальный веб-интерфейс для инвентаризации серверов и DNS.",
        epilog="Пример запуска: ./sinver.py ./db.sqlite --port 5173 --debug",
    )
    parser.add_argument("db", type=Path, help="Путь к SQLite базе")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"HTTP порт (по умолчанию {DEFAULT_PORT})")
    parser.add_argument("--debug", action="store_true", help="Режим отладки: подробные ошибки в веб-интерфейсе")
    parser.add_argument("--init-if-missing", action="store_true", help="Автоматически создать БД без вопроса")
    return parser.parse_args(argv)


def ask_create_db(path: Path) -> bool:
    answer = input(f"Database {path} not found or empty. Create it now? [y/N]: ").strip().lower()
    return answer in {"y", "yes"}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    db_path: Path = args.db
    init_sql_path = resolve_init_sql_path()

    if init_sql_path is None:
        print(
            f"ERROR: init script not found. Checked: {INIT_SQL_PATH}, {INSTALL_INIT_SQL_PATH}",
            file=sys.stderr,
        )
        return 1

    needs_init = (not db_path.exists()) or (db_path.exists() and db_path.stat().st_size == 0)
    if needs_init:
        print(f"INFO: database file {db_path} is missing or empty.")
        if args.init_if_missing or ask_create_db(db_path):
            db_path.parent.mkdir(parents=True, exist_ok=True)
            init_database(db_path, init_sql_path)
            print("INFO: database created successfully.")
        else:
            print("INFO: database creation canceled.")
            return 1

    ok, reason = validate_database(db_path)
    if not ok:
        print(f"ERROR: database validation failed: {reason}", file=sys.stderr)
        return 1

    app = create_app(db_path, debug_mode=args.debug)
    print(f"SINVER started successfully: http://127.0.0.1:{args.port}")
    app.run(host="127.0.0.1", port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
