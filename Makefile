.PHONY: install

install: sinver.py init_db.sql sinver.service
	mkdir -p /var/sinver /usr/share/sinver
	install -m 755 ./sinver.py /usr/bin/sinver
	if [ ! -e /usr/share/sinver/init_db.sql ]; then \
		install -m 444 ./init_db.sql /usr/share/sinver/init_db.sql; \
	fi
	if command -v chattr >/dev/null 2>&1; then \
		chattr +i /usr/share/sinver/init_db.sql 2>/dev/null || true; \
	fi
	if [ ! -e /var/sinver/sinver.sqlite ]; then \
		sqlite3 /var/sinver/sinver.sqlite < ./init_db.sql; \
	fi
	install -m 644 ./sinver.service /etc/systemd/system/sinver.service
	systemctl daemon-reload
