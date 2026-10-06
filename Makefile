.PHONY: install

DESTDIR ?=

install: sinver.py database/init_db.sql sinver.service sinver.toml
	install -d $(DESTDIR)/var/lib/sinver/backups $(DESTDIR)/var/log/sinver $(DESTDIR)/usr/share/sinver/migrations $(DESTDIR)/usr/bin $(DESTDIR)/etc/systemd/system
	install -m 755 ./sinver.py $(DESTDIR)/usr/bin/sinver
	install -m 644 ./database/init_db.sql $(DESTDIR)/usr/share/sinver/init_db.sql
	cp -R ./database/migrations/. $(DESTDIR)/usr/share/sinver/migrations/
	if [ ! -e $(DESTDIR)/etc/sinver.toml ]; then \
		install -m 644 ./sinver.toml $(DESTDIR)/etc/sinver.toml; \
	fi
	install -m 644 ./sinver.service $(DESTDIR)/etc/systemd/system/sinver.service
	if [ -z "$(DESTDIR)" ]; then systemctl daemon-reload; fi
