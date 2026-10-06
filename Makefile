.PHONY: install install-user
.DEFAULT_GOAL := install

DESTDIR ?=

# Staging must also work without root or a sinver account on the build host.
ifeq ($(DESTDIR),)
ROOT_OWNER = -o root -g root
DATA_OWNER = -o sinver -g sinver
CONFIG_OWNER = -o root -g sinver
endif

install-user:
	@if [ -z "$(DESTDIR)" ]; then \
		set -e; \
		if [ "$$(id -u)" != 0 ]; then \
			echo "Run make install as root, or use DESTDIR for staging." >&2; exit 1; \
		fi; \
		if ! getent group sinver >/dev/null; then groupadd --system sinver; fi; \
		if ! getent passwd sinver >/dev/null; then \
			login_shell=$$(command -v nologin || true); \
			useradd --system --gid sinver --home-dir /var/lib/sinver \
				--no-create-home --shell "$${login_shell:-/bin/false}" sinver; \
		fi; \
		if [ "$$(id -u sinver)" = 0 ] || [ "$$(getent group sinver | cut -d: -f3)" = 0 ]; then \
			echo "sinver must be an unprivileged user and group." >&2; exit 1; \
		fi; \
	fi

install: sinver.py database/init_db.sql sinver.service sinver.toml install-user
	install -d -m 0755 $(ROOT_OWNER) "$(DESTDIR)/usr/bin" "$(DESTDIR)/usr/share/sinver" "$(DESTDIR)/usr/share/sinver/migrations" "$(DESTDIR)/etc/systemd/system"
	install -d -m 0750 $(DATA_OWNER) "$(DESTDIR)/var/lib/sinver" "$(DESTDIR)/var/log/sinver"
	install -d -m 0700 $(DATA_OWNER) "$(DESTDIR)/var/lib/sinver/backups"
	install -m 0755 $(ROOT_OWNER) ./sinver.py "$(DESTDIR)/usr/bin/sinver"
	install -m 0644 $(ROOT_OWNER) ./database/init_db.sql "$(DESTDIR)/usr/share/sinver/init_db.sql"
	cp -R ./database/migrations/. "$(DESTDIR)/usr/share/sinver/migrations/"
	find "$(DESTDIR)/usr/share/sinver/migrations" -type d -exec chmod 0755 {} +
	find "$(DESTDIR)/usr/share/sinver/migrations" -type f -exec chmod 0644 {} +
	if [ ! -e "$(DESTDIR)/etc/sinver.toml" ]; then \
		install -m 0640 $(CONFIG_OWNER) ./sinver.toml "$(DESTDIR)/etc/sinver.toml"; \
	fi
	chmod 0640 "$(DESTDIR)/etc/sinver.toml"
	install -m 0644 $(ROOT_OWNER) ./sinver.service "$(DESTDIR)/etc/systemd/system/sinver.service"
	if [ -z "$(DESTDIR)" ]; then \
		set -e; \
		chown -R root:root /usr/share/sinver; \
		chown root:sinver /etc/sinver.toml; \
		chown -R sinver:sinver /var/lib/sinver /var/log/sinver; \
	fi
	find "$(DESTDIR)/var/lib/sinver" -path "$(DESTDIR)/var/lib/sinver/backups" -prune -o -type d -exec chmod 0750 {} +
	find "$(DESTDIR)/var/log/sinver" -type d -exec chmod 0750 {} +
	find "$(DESTDIR)/var/lib/sinver" "$(DESTDIR)/var/log/sinver" -type f -exec chmod 0600 {} +
	find "$(DESTDIR)/var/lib/sinver/backups" -type d -exec chmod 0700 {} +
	if [ -z "$(DESTDIR)" ]; then systemctl daemon-reload; fi
