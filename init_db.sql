PRAGMA foreign_keys = ON;

BEGIN TRANSACTION;

-- Таблица зон DNS.
CREATE TABLE IF NOT EXISTS zones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone TEXT NOT NULL UNIQUE,
    mname TEXT NOT NULL,
    rname TEXT NOT NULL,
    serial TEXT NOT NULL,
    refresh TEXT NOT NULL,
    retry TEXT NOT NULL,
    expire TEXT NOT NULL,
    minimum TEXT NOT NULL,
    description TEXT,
    psql_address TEXT,
    db_name TEXT,
    psql_user TEXT,
    psql_password TEXT,
    CHECK (length(zone) <= 255),
    CHECK (psql_address IS NULL OR length(psql_address) <= 256),
    CHECK (db_name IS NULL OR length(db_name) <= 256),
    CHECK (psql_user IS NULL OR length(psql_user) <= 256),
    CHECK (psql_password IS NULL OR length(psql_password) <= 256)
);

-- Таблица ролей для серверов, IP и поддоменов.
CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role_name TEXT NOT NULL UNIQUE,
    description TEXT,
    color TEXT NOT NULL,
    CHECK (length(role_name) <= 128),
    CHECK (length(color) <= 32)
);

-- Таблица хостингов.
CREATE TABLE IF NOT EXISTS hostings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    url TEXT,
    description TEXT,
    paid_until DATE,
    CHECK (length(name) <= 256),
    CHECK (url IS NULL OR length(url) <= 1024)
);

-- Таблица серверов.
CREATE TABLE IF NOT EXISTS servers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hostname TEXT NOT NULL UNIQUE,
    role_id INTEGER,
    hosting_id INTEGER,
    primary_zone_id INTEGER,
    description TEXT,
    FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE SET NULL,
    FOREIGN KEY (hosting_id) REFERENCES hostings(id) ON DELETE SET NULL,
    FOREIGN KEY (primary_zone_id) REFERENCES zones(id) ON DELETE RESTRICT,
    CHECK (length(hostname) <= 255)
);

-- Таблица IP-адресов серверов.
CREATE TABLE IF NOT EXISTS ip_addresses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip_type TEXT NOT NULL,
    ip TEXT NOT NULL UNIQUE,
    server_id INTEGER NOT NULL,
    role_id INTEGER,
    is_primary INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (server_id) REFERENCES servers(id) ON DELETE CASCADE,
    FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE SET NULL,
    CHECK (ip_type IN ('IPv4', 'IPv6')),
    CHECK (is_primary IN (0, 1))
);

-- Уникальность primary IP на каждом сервере.
CREATE UNIQUE INDEX IF NOT EXISTS idx_ip_primary_per_server
ON ip_addresses(server_id)
WHERE is_primary = 1;

-- Таблица поддоменов.
CREATE TABLE IF NOT EXISTS subdomains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_type TEXT NOT NULL,
    subdomain TEXT NOT NULL,
    zone_id INTEGER NOT NULL,
    role_id INTEGER,
    FOREIGN KEY (zone_id) REFERENCES zones(id) ON DELETE CASCADE,
    FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE SET NULL,
    CHECK (record_type IN ('A', 'AAAA', 'CNAME', 'TXT', 'MX', 'NS', 'SRV', 'CAA')),
    CHECK (length(subdomain) <= 255),
    UNIQUE (record_type, subdomain, zone_id)
);

-- Связь поддоменов с IP.
CREATE TABLE IF NOT EXISTS subdomain_ip_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subdomain_id INTEGER NOT NULL,
    ip_id INTEGER NOT NULL,
    FOREIGN KEY (subdomain_id) REFERENCES subdomains(id) ON DELETE CASCADE,
    FOREIGN KEY (ip_id) REFERENCES ip_addresses(id) ON DELETE CASCADE,
    UNIQUE (subdomain_id, ip_id)
);

COMMIT;
