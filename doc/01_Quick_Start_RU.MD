# 1. Быстрый старт

[Содержание](README_RU.md) · [Далее: Конфигурация](02_Configuration_RU.MD)

После выполнения этой главы SINVER будет работать как служба Linux, вход в веб-интерфейс будет защищён NGINX, а записи учебной зоны будут отправлены в PowerDNS.

## 1.1. Подготовка

Команды рассчитаны на Debian 12 или Ubuntu 24.04 с systemd. Выполняйте их в терминале сервера от обычного пользователя с доступом к `sudo`. Требуется Python **3.11 или новее**: SINVER использует встроенный модуль `tomllib`.

В примере все компоненты установлены на одном узле:

```text
Браузер → HTTPS :443 → NGINX + пароль → HTTP 127.0.0.1:8080 → SINVER
                                                               ├─ SQLite: учёт объектов
                                                               └─ PostgreSQL :5432: записи DNS
PowerDNS → PostgreSQL :5432 → ответы DNS на 127.0.0.1:53
```

| Пример | Значение |
| --- | --- |
| `sinver.example.net` | Имя веб-интерфейса. **Для сертификата замените его своим действующим доменным именем** |
| `admin@example.net` | Заменяемый адрес электронной почты для уведомлений о сертификате |
| `example.net` | Учебная DNS-зона; её можно использовать для локальной проверки |
| `web01`, `api` | Имя первого сервера и имя поддомена |
| `192.0.2.10` | Адрес из диапазона для документации; замените реальным адресом при использовании зоны |
| `2610071200` | Условный начальный SERIAL, одинаковый в PowerDNS и SINVER |

SINVER не создаёт учётные записи пользователей приложения. Защитите доступ до использования его через сеть. Через NGINX все вошедшие пользователи получают одинаковые возможности, включая изменение и удаление данных.

## 1.2. Установка и первый запуск SINVER

Установите зависимости из пакетов дистрибутива:

```bash
sudo apt update
sudo apt install --yes git make python3 python3-flask python3-psycopg2 curl sqlite3
python3 --version
/usr/bin/python3 -c 'import flask, psycopg2, tomllib; print("Зависимости доступны")'
```

Flask обеспечивает веб-интерфейс; `psycopg2` нужен для обновления PowerDNS через PostgreSQL. Без `psycopg2` остальные вкладки доступны, но отправка в PowerDNS завершится ошибкой.

Получите ветку `doc` и установите приложение:

```bash
git clone --branch doc --single-branch https://github.com/dvoropaev/SINVER.git
cd SINVER
git branch --show-current
sudo make install
sudo systemctl enable --now sinver.service
sudo systemctl status sinver.service --no-pager
curl -I http://127.0.0.1:8080/
```

`git branch --show-current` должен вывести `doc`. Для HTTP-запроса к `/` ожидается `302` с переходом на `/servers`.

Установщик создаёт системного пользователя `sinver`, копирует программу в `/usr/bin/sinver`, конфигурацию в `/etc/sinver.toml`, схему SQLite в `/usr/share/sinver/` и unit-файл systemd. Существующий конфигурационный файл сохраняется. `make install` не запускает службу самостоятельно.

При первом запуске SINVER автоматически создаёт `/var/lib/sinver/sinver.sqlite`. **Не загружайте схему SQLite вручную в уже созданную базу.** PostgreSQL понадобится позже; собственные объекты SINVER хранятся в SQLite.

Если служба не запускается, проверьте:

```bash
sudo journalctl -u sinver.service -n 100 --no-pager
sudo tail -n 100 /var/log/sinver/sinver.log
```

На сервере с графической средой можно открыть `http://127.0.0.1:8080`. Для временной проверки с рабочей станции создайте SSH-туннель, заменив имя пользователя и адрес сервера:

```bash
ssh -N -L 18080:127.0.0.1:8080 user@server-address
```

Затем откройте `http://127.0.0.1:18080` на рабочей станции. SINVER при этом продолжает слушать loopback-интерфейс сервера.

## 1.3. NGINX: HTTPS и вход по паролю

### Предварительные условия для сертификата

Для проверки домена через HTTP-01 нужны:

- действующее доменное имя вместо `sinver.example.net`;
- запись A, указывающая на публичный IPv4 сервера; при наличии AAAA IPv6 тоже должен вести на этот сервер;
- входящие подключения к TCP-портам **80 и 443** через межсетевой экран и, при необходимости, перенаправление портов на маршрутизаторе;
- отсутствие другого веб-сервера на этих портах.

Домен `example.net` зарезервирован для примеров. Получить сертификат Let's Encrypt для него по этим командам нельзя. Если сервер доступен только во внутренней сети, используйте сертификат корпоративного центра сертификации либо процедуру DNS-01 у своего DNS-провайдера. Конфигурация прокси и защиты паролем остаётся такой же; пути к сертификату заменяются.

### Установка и создание пароля

```bash
sudo apt install --yes nginx apache2-utils certbot python3-certbot-nginx
sudo htpasswd -c /etc/nginx/sinver.htpasswd admin
sudo chown root:www-data /etc/nginx/sinver.htpasswd
sudo chmod 0640 /etc/nginx/sinver.htpasswd
```

Введите отдельный пароль для пользователя `admin`. Файл хранит хеш пароля. Для добавления следующего пользователя выполняйте `htpasswd` **без `-c`**, иначе существующий файл будет заменён:

```bash
sudo htpasswd /etc/nginx/sinver.htpasswd operator
```

### Получение сертификата

Создайте файл сайта:

```bash
sudoedit /etc/nginx/sites-available/sinver
```

Начальная конфигурация открывает только HTTP-сайт для выпуска сертификата. Если IPv6 на узле отключён, удалите строки `listen [::]:…` из приведённых конфигураций и не публикуйте AAAA этого сайта:

```nginx
server {
    listen 80;
    listen [::]:80;
    listen [::]:80;
    server_name sinver.example.net;

    location / {
        return 404;
    }
}
```

Замените домен в файле, включите сайт и получите сертификат:

```bash
sudo ln -s /etc/nginx/sites-available/sinver /etc/nginx/sites-enabled/sinver
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx --domain sinver.example.net --email admin@example.net --agree-tos --no-eff-email --redirect
```

Команда `ln -s` выполняется один раз. Если ссылка уже существует, проверьте её, а не создавайте повторно. Подставьте свои домен и адрес почты в команду Certbot.

### Подключение SINVER к прокси

После успешного получения сертификата замените содержимое `/etc/nginx/sites-available/sinver`:

```nginx
server {
    listen 80;
    listen [::]:80;
    listen [::]:80;
    server_name sinver.example.net;
    return 301 https://sinver.example.net$request_uri;
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    listen [::]:443 ssl;
    server_name sinver.example.net;

    ssl_certificate /etc/letsencrypt/live/sinver.example.net/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/sinver.example.net/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    auth_basic "SINVER";
    auth_basic_user_file /etc/nginx/sinver.htpasswd;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cookie_flags session secure;
        proxy_read_timeout 120s;
    }
}
```

Замените **все** вхождения `sinver.example.net`, включая каталоги сертификата. Оставьте SINVER на `127.0.0.1:8080`; значение `http_addr` в `/etc/sinver.toml` менять не требуется.

```bash
sudo nginx -t
sudo systemctl reload nginx
curl -I https://sinver.example.net/
curl -u admin -I https://sinver.example.net/
sudo certbot renew --dry-run
systemctl list-timers --all certbot.timer
```

Без пароля ожидается HTTP `401`, с правильным паролем — переход `302` на `/servers`. `curl -u admin` запрашивает пароль, не сохраняя его в аргументах команды. Откройте `https://sinver.example.net` в браузере и введите данные пользователя NGINX.

`renew --dry-run` проверяет продление. Если таймер не активен, включите его командой `sudo systemctl enable --now certbot.timer`. Порт 80 должен оставаться доступным для последующих проверок HTTP-01.

## 1.4. PostgreSQL и PowerDNS

PowerDNS Authoritative отвечает за записи вашей зоны. Для SINVER требуется **backend gpgsql и схема PostgreSQL PowerDNS**. PowerDNS Recursor, HTTP API и PostgreSQL с произвольной структурой не заменяют этот backend.

В учебной установке PowerDNS слушает только `127.0.0.1:53`, PostgreSQL доступен приложению через `127.0.0.1:5432`. Эта настройка позволяет проверить DNS на самом сервере.

### Установка и проверка порта 53

```bash
sudo ss -lntup 'sport = :53'
sudo apt install --yes postgresql postgresql-client pdns-server pdns-backend-pgsql dnsutils
sudo systemctl enable --now postgresql
```

Порт 53 может быть занят BIND, dnsmasq или другой DNS-службой. Привязка к `0.0.0.0:53` конфликтует с выбранным адресом PowerDNS. `systemd-resolved`, слушающий только `127.0.0.53:53`, обычно не мешает явной привязке PowerDNS к `127.0.0.1:53`. Уточните адреса из вывода `ss`; не отключайте службу разрешения имён без настройки её замены.

### Создание базы и отдельных учётных записей

Следующие действия предназначены для **новой, пустой базы `powerdns`**. Для существующего PowerDNS сначала сделайте резервную копию и используйте его действующую схему.

Откройте административную сессию PostgreSQL:

```bash
sudo -u postgres psql
```

Выполните в `psql`:

```sql
CREATE ROLE powerdns_owner NOLOGIN;
CREATE ROLE pdns_reader LOGIN;
CREATE ROLE sinver_writer LOGIN;
SET password_encryption = 'scram-sha-256';
\password pdns_reader
\password sinver_writer
CREATE DATABASE powerdns OWNER powerdns_owner;
\connect powerdns
SET ROLE powerdns_owner;
\i /usr/share/doc/pdns-backend-pgsql/schema.pgsql.sql
RESET ROLE;

REVOKE ALL ON DATABASE powerdns FROM PUBLIC;
GRANT CONNECT ON DATABASE powerdns TO pdns_reader, sinver_writer;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO pdns_reader, sinver_writer;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO pdns_reader;
GRANT SELECT ON domains TO sinver_writer;
GRANT SELECT, INSERT, DELETE ON records TO sinver_writer;
GRANT USAGE ON SEQUENCE records_id_seq TO sinver_writer;
\quit
```

Команды `\password` запрашивают пароли. Запомните отдельный пароль `sinver_writer`: он понадобится в карточке зоны SINVER. Не используйте учётную запись `postgres` в приложении.

Схема берётся из установленного пакета backend, чтобы соответствовать его версии. Для другого выпуска дистрибутива проверьте путь через `dpkg -L pdns-backend-pgsql`. Не импортируйте `database/init_db.sql` из SINVER в PostgreSQL: это схема собственной SQLite-базы приложения.

| Учётная запись | Назначение и права |
| --- | --- |
| `powerdns_owner` | Владелец схемы без возможности входа; используется администратором при настройке |
| `pdns_reader` | PowerDNS читает таблицы. Пример использует зоны типа `NATIVE`, без управления зоной через API и без операций записи PowerDNS |
| `sinver_writer` | SINVER читает `domains`, читает, добавляет и удаляет `records`, получает новые значения `records_id_seq` |

Права `sinver_writer` распространяются на записи всех зон этой базы. Это минимальный набор **операций**, а не разграничение доступа по зонам.

### Проверка локальных подключений

Узнайте путь к файлу правил входа:

```bash
sudo -u postgres psql -c 'SHOW hba_file;'
```

В `pg_hba.conf` должны быть правила, разрешающие вход обеим учётным записям с loopback. При необходимости добавьте их **перед** более общими правилами, которые могут перехватить эти подключения:

```text
host powerdns pdns_reader   127.0.0.1/32 scram-sha-256
host powerdns sinver_writer 127.0.0.1/32 scram-sha-256
```

Примените правила и проверьте вход:

```bash
sudo -u postgres psql -c 'SELECT pg_reload_conf();'
psql -h 127.0.0.1 -U pdns_reader -d powerdns -W -c 'SELECT count(*) FROM domains;'
psql -h 127.0.0.1 -U sinver_writer -d powerdns -W -c 'SELECT count(*) FROM records;'
```

Для стандартного локального кластера PostgreSQL изменение `listen_addresses` не требуется. SINVER не имеет поля порта PostgreSQL; для этой инструкции используйте стандартный порт **5432**.

### Настройка backend PowerDNS

В `/etc/powerdns/pdns.conf` задайте:

```ini
local-address=127.0.0.1
local-port=53
include-dir=/etc/powerdns/pdns.d
```

Изменяйте существующие строки; не добавляйте повторяющиеся определения. Создайте или отредактируйте `/etc/powerdns/pdns.d/gpgsql.conf`:

```ini
launch=gpgsql
gpgsql-host=127.0.0.1
gpgsql-port=5432
gpgsql-dbname=powerdns
gpgsql-user=pdns_reader
gpgsql-password=REPLACE_WITH_PDNS_READER_PASSWORD
```

Вместо `REPLACE_WITH_PDNS_READER_PASSWORD` укажите пароль `pdns_reader`. Проверьте, что в других подключаемых файлах нет активного backend и второго определения `launch`. Закройте файл с паролем от остальных пользователей:

```bash
sudo chown root:pdns /etc/powerdns/pdns.d/gpgsql.conf
sudo chmod 0640 /etc/powerdns/pdns.d/gpgsql.conf
```

### Создание начальной зоны

**SINVER не создаёт зону в PowerDNS.** До первой отправки в таблице `domains` уже должна быть зона с тем же именем, а в `records` — ровно одна SOA-запись этой зоны.

Откройте `sudo -u postgres psql -d powerdns` и выполните:

```sql
SET ROLE powerdns_owner;
BEGIN;
INSERT INTO domains (name, type) VALUES ('example.net', 'NATIVE');

INSERT INTO records (domain_id, name, type, content, ttl, disabled, auth)
SELECT id, 'example.net', 'SOA',
       'ns1.example.net. hostmaster.example.net. 2610071200 3600 600 1209600 300',
       3600, false, true
FROM domains WHERE name = 'example.net';

INSERT INTO records (domain_id, name, type, content, ttl, disabled, auth)
SELECT id, 'example.net', 'NS', 'ns1.example.net', 3600, false, true
FROM domains WHERE name = 'example.net';
COMMIT;
\quit
```

NS указывает авторитетный сервер имён, SOA содержит параметры зоны и её номер версии. Время в примере задано в секундах. `RNAME = hostmaster.example.net.` обозначает адрес почты `hostmaster@example.net`, а не имя другого сервера. Подробности полей приведены в главе [«Зоны»](09_Zones_RU.MD).

Запустите PowerDNS и проверьте ответы:

```bash
sudo systemctl enable --now pdns
sudo systemctl restart pdns
sudo systemctl status pdns --no-pager
dig @127.0.0.1 example.net SOA +norecurse
dig @127.0.0.1 example.net NS +norecurse
```

Ожидаются SOA с SERIAL `2610071200`, NS `ns1.example.net.` и флаг `aa` — авторитетный ответ. Если служба не запускается, используйте `sudo journalctl -u pdns -n 100 --no-pager`.

Учебная зона пока не делегирована в публичной DNS. Для такой публикации потребуются реальные доступные серверы имён, их A/AAAA, открытые **UDP и TCP 53**, NS у регистратора и, для серверов внутри своей зоны, glue-записи у родительской зоны. Адреса серверов имён в управляемой зоне обязательно включите в данные SINVER: при Push существующие A/AAAA, отсутствующие в SINVER, удаляются.

## 1.5. Добавление первого сервера

Все следующие действия выполняются в защищённом веб-интерфейсе. Общее устройство описано в главе [«веб-интерфейс»](03_Web_Interface_RU.MD).

### Шаг 1. Добавьте зону

Откройте **Zones → Add zone** и заполните:

| Поле | Значение для учебного примера |
| --- | --- |
| Zone | `example.net` — без завершающей точки |
| MNAME | `ns1.example.net.` |
| RNAME | `hostmaster.example.net.` |
| SERIAL | `2610071200` — такой же, как в начальной SOA PostgreSQL |
| REFRESH | `3600` |
| RETRY | `600` |
| EXPIRE | `1209600` |
| MINIMUM | `300` |
| PostgreSQL address | `127.0.0.1` |
| DB name | `powerdns` |
| PostgreSQL user | `sinver_writer` |
| PostgreSQL password | Пароль `sinver_writer` |
| Description | Например, `Учебная зона` |

Нажмите **Create**. Завершающие точки у `MNAME` и `RNAME` позволяют также корректно использовать экспорт Bind9. Имя самой зоны в SINVER должно в точности совпадать с `domains.name` в PostgreSQL.

### Шаг 2. Добавьте сервер

Откройте **Servers → Add server**:

| Поле | Значение |
| --- | --- |
| Hostname | `web01` — без `.example.net` |
| Primary zone | `example.net` |
| Role | `None` |
| Hosting | `None` |
| Description | `Первый веб-сервер` |

Нажмите **Create**. Запись сервера представляет объект инвентаризации; SINVER не устанавливает ПО на этот сервер и не опрашивает его. Роли и хостинги можно добавить позднее: см. главы [«Серверы»](04_Servers_RU.MD), [«Роли»](07_Roles_RU.MD) и [«Хостинги»](08_Hostings_RU.MD).

### Шаг 3. Добавьте основной IP

Откройте **IP addresses → Add IP**:

| Поле | Значение |
| --- | --- |
| Type | `IPv4` |
| IP | `192.0.2.10` |
| Server | `web01` |
| Role | `None` |
| Primary | `Yes` |

Нажмите **Create**. Primary-адрес вместе с основной зоной сервера формирует запись `web01.example.net A 192.0.2.10`.

**На одном сервере допускается только один Primary-адрес суммарно для IPv4 и IPv6.** В текущей версии для замены основного адреса сначала снимите Primary у прежнего IP и сохраните его, затем назначьте Primary новому. Прямая попытка назначить второй Primary может завершиться ошибкой. См. [«IP-адреса»](05_IP_Addresses_RU.MD).

### Шаг 4. Добавьте поддомен

Откройте **Subdomains → Add subdomain**, выберите `Type = A`, `Name = api`, `Zone = example.net`, `Role = None` и нажмите **Create**.

Затем откройте карточку созданного поддомена, нажмите **Edit**, выберите `192.0.2.10` в **Linked IP addresses** и нажмите **Save**. Создание поддомена и привязка адресов выполняются отдельно. Поддомен без подходящего IP не создаёт A/AAAA в DNS.

Получится `api.example.net A 192.0.2.10`. Подробности нескольких адресов и IPv6 приведены в главе [«Поддомены»](06_Subdomains_RU.MD).

## 1.6. Отправка в PowerDNS и проверка результата

1. Откройте вкладку **DNS**.
2. В блоке **PowerDNS + PostgreSQL** выберите `example.net` и нажмите **Push**.
3. Сверьте **Remote SERIAL** и **Local SERIAL**. При выполнении примера оба равны `2610071200`. Расхождение означает, что SINVER и PowerDNS содержат разные версии зоны; выясните причину до продолжения.
4. Проверьте **PowerDNS preview**. Строки `+` обозначают добавление, строки `-` — удаление. Должны появиться A для `web01.example.net` и `api.example.net`, а SOA — замениться на SOA с **New SERIAL**. NS сохранится. Для реальной зоны новый SERIAL должен быть больше прежнего: текущая версия генерирует его по локальным часам с точностью до минуты и не проверяет увеличение. Условный начальный номер из примера замените при необходимости; при повторной отправке в ту же минуту дождитесь следующей минуты.
5. Проверьте SQL preview, затем нажмите **Confirm apply** и подтвердите операцию в диалоге браузера.
6. Дождитесь сообщения `PowerDNS update applied and local serial updated.`

**Push сначала строит план. Изменения в PostgreSQL выполняются только после Confirm apply.** План действует 10 минут; при истечении времени или перезапуске SINVER нажмите Push заново. Для текущей версии выполняйте отправку одним администратором и не изменяйте зону между просмотром и подтверждением.

**SINVER управляет всеми A, AAAA и SOA выбранной зоны.** Существующие A/AAAA, которые отсутствуют в данных приложения, попадут в план удаления. Остальные типы, включая NS, MX и TXT, сохраняются. У новых управляемых записей TTL равен **3600 секунд**; изменить его через интерфейс или `sinver.toml` нельзя. Перед первой отправкой рабочей зоны перенесите в SINVER все её A/AAAA и сохраните резервную копию PostgreSQL.

Проверьте записи напрямую у PowerDNS:

```bash
dig @127.0.0.1 web01.example.net A +norecurse
dig @127.0.0.1 api.example.net A +norecurse
dig @127.0.0.1 example.net SOA +norecurse
```

Для обоих A ожидается `192.0.2.10`; SERIAL SOA должен совпасть с новым значением в карточке зоны SINVER. Проверить базу можно отдельно:

```bash
psql -h 127.0.0.1 -U sinver_writer -d powerdns -W -c "SELECT name, type, content, ttl FROM records WHERE domain_id = (SELECT id FROM domains WHERE name = 'example.net') ORDER BY name, type;"
```

После прямой записи в PostgreSQL PowerDNS может некоторое время отвечать из собственного кэша. Для немедленной локальной проверки очистите кэш зоны:

```bash
sudo pdns_control purge 'example.net$'
```

Рекурсивные DNS-серверы клиентов сохраняют прежние ответы до истечения их TTL; очистка кэша PowerDNS их кэш не меняет. SINVER самостоятельно не выполняет NOTIFY, настройку secondary-серверов и очистку кэша.

Порядок повторных публикаций, расхождение SERIAL и экспорт Bind9 описаны в главе [«DNS»](10_DNS_RU.MD). Перед использованием рабочей зоны ознакомьтесь с [текущими проблемами](11_Current_Issues_RU.MD).
