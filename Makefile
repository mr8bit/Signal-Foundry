.PHONY: init check check-python check-js check-compose build up down logs status backup https-up deploy deploy-https

init:
	python3 ops.py init-env

check: check-python check-js check-compose

check-python:
	@set -e; name=signal-foundry-test-$$$$; \
	docker run -d --rm --name $$name -e POSTGRES_DB=signal -e POSTGRES_USER=signal -e POSTGRES_PASSWORD=test \
	  -p 127.0.0.1::5432 --tmpfs /var/lib/postgresql/data:rw \
	  postgres:17-alpine@sha256:eee6c6e1b0894fa4fe0075eb054af9fd1cf39a35321c2d4110626cfe506411fb >/dev/null; \
	trap 'docker rm -f $$name >/dev/null 2>&1 || true' EXIT; \
	until docker exec $$name pg_isready -U signal -d signal >/dev/null 2>&1; do sleep 1; done; \
	docker build -t signal-foundry:test . >/dev/null; \
	mapping=$$(docker port $$name 5432/tcp); \
	docker run --rm --network host --read-only --tmpfs /tmp:rw,mode=1777 \
	  --tmpfs /app/data:rw,uid=10001,gid=10001,mode=0700 -v "$$PWD/tests:/app/tests:ro" \
	  -v "$$PWD/deploy:/app/deploy:ro" \
	  -v "$$PWD/README.md:/app/README.md:ro" \
	  -e PGHOST=127.0.0.1 -e PGPORT=$${mapping##*:} -e PGDATABASE=signal -e PGUSER=signal -e PGPASSWORD=test \
	  signal-foundry:test python -m unittest discover -s tests -q

check-js:
	node --check web/dashboard.js
	node --check web/signal-ranking.js
	node --test tests/signal-ranking.test.cjs

check-compose:
	POSTGRES_PASSWORD=test docker compose config --quiet
	POSTGRES_PASSWORD=test docker compose -f compose.yaml -f compose.https.yaml config --quiet
	POSTGRES_PASSWORD=test docker compose -f compose.yaml -f compose.release.yaml config --quiet
	POSTGRES_PASSWORD=test docker compose -f compose.yaml -f compose.release.yaml -f compose.https.yaml config --quiet

build:
	docker compose build --pull

up:
	docker compose up -d --build --wait --wait-timeout 120 worker web

https-up:
	docker compose -f compose.yaml -f compose.https.yaml up -d --build --wait --wait-timeout 120 worker web proxy

deploy:
	docker compose -f compose.yaml -f compose.release.yaml up -d --no-build --wait --wait-timeout 120 worker web

deploy-https:
	docker compose -f compose.yaml -f compose.release.yaml -f compose.https.yaml up -d --no-build --wait --wait-timeout 120 worker web proxy

down:
	docker compose --profile telegram down

logs:
	docker compose logs --tail 100 -f worker web

status:
	docker compose ps
	docker compose exec -T web python -c "from urllib.request import urlopen; print(urlopen('http://127.0.0.1:8765/health/ready', timeout=5).read().decode())"

backup:
	docker compose --profile ops run --rm --no-deps backup
