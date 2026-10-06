.PHONY: install

DESTDIR ?=

install: sinver.py sinver_database.py sqlite/init_db.sql sinver.service
	install -d "$(DESTDIR)/var/sinver" "$(DESTDIR)/usr/bin" \
		"$(DESTDIR)/usr/share/sinver/migrations" "$(DESTDIR)/etc/systemd/system"
	install -m 755 ./sinver.py "$(DESTDIR)/usr/bin/sinver"
	install -m 444 ./sinver_database.py "$(DESTDIR)/usr/share/sinver/sinver_database.py"
	# Remove the immutable flag used by older installers before updating the schema.
	if command -v chattr >/dev/null 2>&1; then \
		chattr -i "$(DESTDIR)/usr/share/sinver/init_db.sql" 2>/dev/null || true; \
	fi
	install -m 444 ./sqlite/init_db.sql "$(DESTDIR)/usr/share/sinver/init_db.sql"
	find sqlite/migrations -type f ! -name .gitkeep | while IFS= read -r source; do \
		install -D -m 444 "$$source" "$(DESTDIR)/usr/share/sinver/$${source#sqlite/}" || exit 1; \
	done
	install -m 644 ./sinver.service "$(DESTDIR)/etc/systemd/system/sinver.service"
	if [ -z "$(DESTDIR)" ]; then \
		systemctl daemon-reload; \
	fi
