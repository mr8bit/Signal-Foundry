# Signal в эксплуатации

Сервис рассчитан на один Linux-хост с Docker Compose v2. Общее состояние хранится в PostgreSQL, координация процессов использует advisory locks. Репликация между хостами не предусмотрена.

Для хоста выделите от 2 ГБ RAM и локальный диск для volumes. Нужны Docker Engine и Compose v2; Python 3 нужен для `ops.py init-env`, Node.js 22 — только для локальных JS-проверок. Для запуска контейнеров Node.js не требуется. Проверки и сборка образа также выполняются в GitHub Actions (`.github/workflows/ci.yml`).

## Подготовка репозитория

В Git входят код, тесты, конфигурация источников и примеры окружения. `.env`, локальные данные, кэш, сгенерированный HTML, резервные копии и приватные ключи исключены через `.gitignore`. Docker-контекст ограничен исходниками приложения, `config.json`, каталогом `web` и lock-файлом зависимостей; локальные данные и секреты в него не передаются.

```sh
make init     # создаёт .env, дополняет существующий
make check    # Python, JavaScript, оба Compose-файла
make build
make up
```

`make check` требует Node.js и Docker Compose, но не запускает сетевой сбор источников. Удалённый Git-репозиторий подключается отдельно; URL и доступ к нему не входят в конфигурацию сервиса. Не публикуйте файлы из `data` и `.env` вместе с исходниками.

## Запуск

```sh
python3 ops.py init-env
# Для существующей установки: сначала остановите прежний bot.py/server.py.
docker compose build
docker compose --profile ops run --rm import
docker compose up -d --wait worker web
docker compose ps
```

Импорт необязателен для новой установки. Команда вызывает `python ops.py migrate-sqlite /import` и переносит архив, HTTP-кэш, расписание, attention, embeddings и историю Telegram из SQLite-файлов `./data` в PostgreSQL, сохраняя `first_seen`. Исходные SQLite-файлы сохраняются, существующие строки назначения и непустые файлы лент не заменяются. Не импортируйте одновременно с записью старого сборщика.

`init-env` создаёт `.env` с правами 0600, случайными `SIGNAL_ADMIN_TOKEN` и `POSTGRES_PASSWORD`. В существующий файл команда добавляет отсутствующий `POSTGRES_PASSWORD`, не меняя остальные настройки. `POSTGRES_DB`, `POSTGRES_USER` и `POSTGRES_PASSWORD` обязательны. Файл нельзя коммитить; он не попадает в образ. Переменные Compose берутся из этого файла, настройки источников — из `config.json`.

После старта:

- Дашборд: http://127.0.0.1:8765/
- JSON: `/api/news`
- RSS: `/api/rss?sort=day` (также `now`, `week`, `month`, `year`)
- Состояние: `/api/status`
- Жив ли веб: `/health/live`
- Готов ли сервис: `/health/ready`
- Метрики Prometheus: `/metrics`

До первого успешного сбора дашборд отдаёт 503 и автоматически перезагружается. При наличии импортированного снимка лента доступна сразу. `docker compose --wait` проверяет работоспособность процессов; доступность свежей публикации проверяйте отдельно через `/health/ready`.

## Процессы и данные

`worker` читает внешние источники с общим HTTP-кэшем в PostgreSQL. TTL, паузы, Retry-After и устаревший кэш при сбоях задаются секцией `cache` в `config.json`. Обновление дашборда и частые запросы API не запускают внешние запросы. Ручное обновление использует тот же кэш и не обходит TTL. Для последующего исторического ранжирования worker ежедневно сохраняет в `story_daily` последнее и пиковое Attention каждой истории, число и сумму наблюдений; компактные rollups хранятся 400 дней.

`web` — Gunicorn с двумя процессами по четыре потока. Он читает готовые поколения HTML/JSON; новый набор файлов становится виден атомарно. Сбой сборки оставляет предыдущую публикацию доступной. Архив истории используется для окон от суток до года; актуальные фильтры и оценка `editorial-v7` применяются при построении.

`publisher` читает собранный снимок и отправляет в Telegram по рейтингу. Веб и сборщик не получают Telegram-токен. Веб-управление ограничено `SIGNAL_ADMIN_TOKEN`; в HTML и общедоступные API ключ не включается. Кнопка обновления запрашивает его и сохраняет в `sessionStorage` вкладки.

Внутренний сервис `postgres` (`postgres:17-alpine`) хранит новости, HTTP-кэш, расписание, attention, embeddings, служебную очередь, heartbeat и историю Telegram. Его volume задаёт `SIGNAL_POSTGRES_VOLUME` (по умолчанию `reddit-reposter_signal-postgres`). Старый volume `reddit-reposter_signal-data` остаётся для снимков источников и поколений публикаций, а также служит источником миграции. Redis не используется. Не удаляйте volume через `docker compose down -v`.

Контейнеры работают с UID 10001, файловая система образа доступна только для чтения, временные файлы размещаются в `/tmp`. Включены ограничения CPU/памяти, ротация логов, restart policy и healthchecks. Docker сам по себе не перезапускает контейнер только из-за статуса unhealthy: проверяйте метрики и логи.

## Обновление источников и управление

Диагностика доступности без внешних запросов: `docker compose exec worker python source_diagnostics.py`. Для проверки конкретного источника через обычный кэш добавьте `--probe --source <часть имени или URL>`. Команда учитывает TTL и паузы после ошибок.

Источники, правила и отдельные интервалы в секции `collection` редактируются в `config.json`, который подключён только для чтения. Сборщик перечитывает конфигурацию и обрабатывает по одному готовому источнику. [Расписание, паузы и backoff](COLLECTION.md). `poll_seconds` верхнего уровня относится к старому отдельному `bot.py`; production использует `collection` и индивидуальный `poll_seconds` источника. Периодические и ручные задания используют одну устойчивую очередь; одновременные запросы объединяются. После аварийного завершения незавершённое задание будет восстановлено.

Для запуска ручного сбора используйте кнопку в браузере либо `POST /api/refresh` с заголовком `Authorization: Bearer <SIGNAL_ADMIN_TOKEN>`. Без ключа возвращается 401; пустой ключ в окружении отключает управление. Не передавайте ключ в URL. При запросах из браузера Origin должен совпадать с `SIGNAL_PUBLIC_URL`.

```sh
docker compose logs --tail 100 worker web
docker compose exec worker python ops.py status
curl --fail http://127.0.0.1:8765/health/ready
```

Readiness требует heartbeat сборщика моложе 30 секунд и снимок моложе `SIGNAL_READY_MAX_AGE` (по умолчанию 7200 секунд). Это не означает доступность каждого источника: отдельные 403/429 и работа с устаревшим кэшем видны в списке источников на дашборде. Метрики содержат количество успешных/неудачных сборок, время последнего успеха, heartbeat и число опубликованных записей. Для мониторинга задайте оповещения об устаревшем heartbeat/снимке и сбоях сборки.

### node_exporter

`monitoring.py` формирует метрики `signal_foundry_*` из PostgreSQL и актуальных файлов публикации. Для интеграции с textfile collector host-wrapper запускает его внутри `worker`, используя существующее окружение базы, и атомарно записывает `signal-foundry.prom` и `signal-foundry-status.prom`. Пароли на host не нужны; старые SQLite-файлы не используются.

Установите или обновите wrapper из репозитория, сохранив путь, который вызывает systemd-таймер:

```sh
install -m 755 deploy/signal_foundry_metrics.py /usr/local/libexec/signal_foundry_metrics.py
systemctl start infra-signal-foundry.service
```

Путь к проекту и каталог node_exporter можно изменить параметрами `--project` и `--output`. Ошибка экспорта выставляет `signal_foundry_collector_success 0`; последняя успешная метрика при этом сохраняется для диагностики, но должна быть покрыта alert по её возрасту.

## Telegram

Добавьте бота администратором канала с правом публикации. Заполните в `.env` `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID`:

```sh
docker compose --profile telegram up -d publisher
docker compose logs --tail 100 publisher
```

Публикация включается этой командой сразу. Интервалы и лимиты берутся из `config.json`. Слишком старый снимок (более часа) не публикуется. Текст остаётся на языке источника; фрагмент ограничен 900 символами, заголовок — 250. Автоматического перевода/генерации пересказа нет.

Если отправка завершилась неоднозначно, запись остаётся `pending`, повторная отправка блокируется: нужно проверить канал и вручную разрешить запись в таблицах `deliveries`/`aliases` PostgreSQL по SQL-инструкции в README. Остановите publisher перед изменением записей. Telegram API не гарантирует exactly-once доставку. Не запускайте старый бот одновременно с publisher.

## Резервная копия и восстановление

```sh
make backup
# Эквивалент: docker compose --profile ops run --rm backup
```

Копия помещается в датированный каталог volume `reddit-reposter_signal-backups`: `postgres.dump` в custom-формате `pg_dump` и файлы лент. `.env` нужно хранить отдельно. Для согласованного снимка базы и файлов сначала остановите `worker` и `publisher`, затем сделайте копию и запустите процессы снова.

Скачать все копии на хост:

```sh
docker compose --profile ops run --rm -T --no-deps --entrypoint tar backup -C /backups -czf - . > signal-backups.tar.gz
```

Храните копии также вне этого Docker-хоста; периодичность задайте в своей системе резервного копирования. Сам сервис не запускает внешние планировщики и не удаляет старые копии.

Восстановление в **новые volumes**, сохраняя прежние для отката:

1. Остановите сервис: `docker compose --profile telegram down` (без `-v`).
2. Распакуйте нужный датированный каталог копии в `./restore`. В нём должны лежать `postgres.dump`, `feed-snapshot.json` и другие файлы, а не ещё один вложенный каталог.
3. Проверьте `config.json` и `.env`.
4. Задайте новые имена в `.env`, например `SIGNAL_DATA_VOLUME=reddit-reposter-restored` и `SIGNAL_POSTGRES_VOLUME=reddit-reposter-restored-postgres`.
5. Выполните `docker compose --profile ops run --rm -v "$PWD/restore:/restore:ro" import python ops.py restore /restore`. Восстановление возможно только в пустую PostgreSQL-базу.
6. Запустите `docker compose up -d --wait worker web` и проверьте `/health/ready`, историю и рейтинги. Telegram включайте отдельно после проверки.

Очередь управления при обычном импорте SQLite не переносится: старые запросы обновления не должны воспроизводиться из копии. История Telegram переносится, чтобы не отправить уже опубликованные записи снова. Для отката остановите сервис и верните предыдущие `SIGNAL_DATA_VOLUME`, `SIGNAL_POSTGRES_VOLUME` и конфигурацию.

## Обновление кода

```sh
python3 -m unittest discover -s tests -q
node --check web/dashboard.js
node --test tests/signal-ranking.test.cjs
docker compose --profile ops run --rm --no-deps backup
docker compose build
docker compose up -d --wait worker web
# Если Telegram уже настроен:
# docker compose --profile telegram up -d publisher
```

Образы Python и зависимости закреплены версиями/хэшами. Для отката образа можно использовать `SIGNAL_IMAGE` с заранее сохранённым тегом. Изменения схемы и совместимость старого образа с новой базой нужно оценивать перед обновлением; копия данных обязательна для такого отката.

## Доступ по домену

По умолчанию порт доступен только на localhost. Для сервера установите `SIGNAL_PUBLIC_URL=https://your-domain.example`, оставьте `SIGNAL_BIND_IP=127.0.0.1` и настройте HTTPS в вашем reverse proxy на этом хосте с upstream `http://127.0.0.1:8765`. Передавайте Host и X-Forwarded-Proto, ограничьте размер тела запросов и таймауты. `/health/ready` подходит для проверки upstream. Базовый `compose.yaml` не настраивает TLS; вариант с Caddy описан ниже.

Если proxy тоже в Docker, подключите его к сети Compose и направьте на `web:8765`; наружу публикуйте только proxy. Не открывайте незащищённый HTTP с ключом управления в интернете. `/metrics` можно ограничить на proxy адресами вашей системы мониторинга. После изменения `.env` пересоздайте процессы через `docker compose up -d worker web`.

### HTTPS через Caddy из этого проекта

Если на сервере уже работает reverse proxy, используйте его по инструкции выше. Для отдельного сервера подготовлен `compose.https.yaml`:

1. Направьте DNS A/AAAA домена на сервер. Откройте входящие TCP 80/443; UDP 443 нужен только для HTTP/3. Эти порты должны быть свободны.
2. В `.env` задайте `SIGNAL_DOMAIN=news.example.com`, `SIGNAL_PUBLIC_URL=https://news.example.com`. Оставьте `SIGNAL_BIND_IP=127.0.0.1`. `SIGNAL_DOMAIN` содержит только имя хоста, без схемы и пути.
3. Выполните:

   ```sh
   docker compose -f compose.yaml -f compose.https.yaml config --quiet
   make build
   make https-up
   curl --fail https://news.example.com/health/ready
   ```

Caddy получает сертификат для заданного домена и сохраняет его в отдельном volume. На внешнем домене `/metrics` закрыт; мониторинг может читать его через локальный порт приложения. Значение `localhost` в примере предназначено для проверки конфигурации и не даёт публичного сертификата для сервера.

Для управления этой установкой передавайте оба файла при `logs`, `up`, `down` и обновлении:

```sh
docker compose -f compose.yaml -f compose.https.yaml logs --tail 100 proxy worker web
docker compose -f compose.yaml -f compose.https.yaml up -d --wait worker web proxy
# Остановка без удаления данных и сертификатов:
docker compose -f compose.yaml -f compose.https.yaml --profile telegram down
```

`make down` относится к базовой установке; для HTTPS используйте команду с двумя файлами выше. Telegram по-прежнему включается отдельно профилем `telegram`, после проверки канала и токена. Образы Python и Caddy закреплены digest; при плановом обновлении обновляйте digest и повторяйте проверки.

### Выпуск и откат

Для релиза используйте неизменяемый тег, например `SIGNAL_IMAGE=reddit-reposter:2026-10-18.1` в `.env`. Сначала сохраните резервную копию и предыдущий тег, затем соберите новый образ и пересоздайте процессы. На одном сервере образ можно собирать из checkout; для доставки через registry потребуется отдельно настроить адрес и права push/pull.

```sh
make check
make backup
make build
make up
make status
```

При HTTPS вместо `make up` выполняйте `make https-up`. Если publisher уже работает, пересоздайте его с теми же Compose-файлами и профилем `telegram`. `make backup` создаёт `postgres.dump` и копию файлов; для согласованной копии остановите писателей, как описано выше. Для возврата к прошлому образу верните его тег в `.env` и выполните `up --no-build`; при несовместимых данных восстановите копию в новые volumes.

Новый checkout не содержит рабочего архива. Скопировать код через Git недостаточно для переноса истории: восстановите резервную копию в volumes и перенесите `.env` отдельно. Публичный домен, внешний Git remote и registry этот проект сам не создаёт.

## Внимание (Attention)

Воркер после каждого сбора записывает показания и тратит до `attention.budget_seconds` (по умолчанию 150 с) на внешние запросы: списки «самое читаемое» изданий, Substack API, Google News RSS, HF API, GitHub API и HN Algolia. Исходящие хосты: `news.google.com`, `hn.algolia.com`, `huggingface.co`, `api.github.com` и домены рассылок Substack. Паузы между запросами задаются в `cache.host_min_intervals`. Необязательный `GITHUB_TOKEN` в `.env` (токен без scopes) поднимает лимит GitHub API с 60 до 5000 запросов в час. Отключить внешние запросы можно через `attention.enabled: false`; пассивные показания из собранных постов записываются всегда.
