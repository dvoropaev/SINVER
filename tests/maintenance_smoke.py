import sys, tempfile, sqlite3
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sinver as s
from contextlib import closing
with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp)
    db=root/'new.sqlite'; backups=root/'backups'
    state=s.DatabaseMaintenance(db,backups); state.prepare(); assert state.ready,state.error
    app=s.create_app(db,backups); s.install_maintenance_gate(app,state)
    assert app.test_client().get('/', follow_redirects=True).status_code==200
    old=root/'old.sqlite'
    original=s.INIT_SQL_PATH.read_text().split('-- Таблица зон DNS.')[1]
    with closing(sqlite3.connect(old)) as c:
        c.executescript('BEGIN;\n-- Таблица зон DNS.'+original)
        c.execute("INSERT INTO roles(role_name,color) VALUES('test','red')");c.commit()
    state=s.DatabaseMaintenance(old,backups); state.prepare(); assert not state.ready and not state.error
    app=s.create_app(old,backups);s.install_maintenance_gate(app,state);client=app.test_client()
    assert client.get('/servers').status_code==503
    assert client.post('/maintenance/confirm',data={'token':'bad'}).status_code==403
    assert client.post('/maintenance/confirm',data={'token':state.token}).status_code==302,state.error
    assert state.ready and state.backup.exists()
    with closing(sqlite3.connect(old)) as c:
        assert c.execute('SELECT role_name FROM roles').fetchone()==('test',)
        c.execute("UPDATE sinver_meta SET value='9.0.0'");c.commit()
    newer=s.DatabaseMaintenance(old,backups); newer.prepare();assert not newer.ready and '9.0.0' in newer.error
    # A backup failure must leave the user database untouched.
    blocked = root/'blocked'
    blocked.write_text('not a directory')
    backup_failure = s.DatabaseMaintenance(old, blocked)
    with closing(sqlite3.connect(old)) as c:
        c.execute("UPDATE sinver_meta SET value='0.0.0'"); c.commit()
        c.execute('DROP TABLE sinver_meta'); c.commit()
    before = old.read_bytes()
    backup_failure.prepare(); backup_failure.migrate()
    assert not backup_failure.ready and backup_failure.error
    assert old.read_bytes() == before
    # Version-only update, then real steps and complete rollback on second-step error.
    s.SINVER_VERSION='0.0.1'
    sync=s.DatabaseMaintenance(db,backups);sync.prepare();assert sync.ready,sync.error
    with closing(sqlite3.connect(db)) as c:
        assert c.execute("SELECT migration_type FROM migration_history").fetchone()==('version_sync',)
    s.MIGRATIONS_PATH=root/'migrations';(s.MIGRATIONS_PATH/'0.1.0').mkdir(parents=True)
    (s.MIGRATIONS_PATH/'0.1.0'/'001.sql').write_text("UPDATE sinver_meta SET value='changed' WHERE key='database_version';")
    (s.MIGRATIONS_PATH/'0.1.0'/'002.sql').write_text('INVALID SQL;')
    s.SINVER_VERSION='0.1.0'
    failed=s.DatabaseMaintenance(db,backups);failed.prepare();assert failed.steps
    failed.migrate();assert not failed.ready and '002' in failed.error and failed.backup.exists()
    with closing(sqlite3.connect(db)) as c:
        assert c.execute("SELECT value FROM sinver_meta WHERE key='database_version'").fetchone()==('0.0.1',)
        assert c.execute('SELECT count(*) FROM migration_history').fetchone()==(1,)
    # Successful step and invalid resulting schema also exercise post-validation rollback.
    (s.MIGRATIONS_PATH/'0.1.0'/'002.sql').write_text('SELECT 1;')
    success=s.DatabaseMaintenance(db,backups);success.prepare();success.migrate();assert success.ready,success.error
    s.SINVER_VERSION='0.2.0';(s.MIGRATIONS_PATH/'0.2.0').mkdir()
    (s.MIGRATIONS_PATH/'0.2.0'/'003.sql').write_text('DROP TABLE roles;')
    invalid=s.DatabaseMaintenance(db,backups);invalid.prepare();invalid.migrate();assert not invalid.ready and 'Missing table' in invalid.error
    with closing(sqlite3.connect(db)) as c:
        assert c.execute("SELECT value FROM sinver_meta WHERE key='database_version'").fetchone()==('0.1.0',)
        assert c.execute('SELECT * FROM roles').fetchall()==[]
print('Bootstrap / maintenance checks passed')
