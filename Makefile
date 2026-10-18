.PHONY: init check build up down logs status backup https-up

init:
	python3 ops.py init-env

check:
	python3 -m unittest discover -s tests -q
	node --check web/dashboard.js
	node --check web/signal-ranking.js
	node --test tests/signal-ranking.test.cjs
	docker compose config --quiet
	docker compose -f compose.yaml -f compose.https.yaml config --quiet

build:
	docker compose build --pull

up:
	docker compose up -d --wait --wait-timeout 120 worker web

https-up:
	docker compose -f compose.yaml -f compose.https.yaml up -d --wait --wait-timeout 120 worker web proxy

down:
	docker compose --profile telegram down

logs:
	docker compose logs --tail 100 -f worker web

status:
	docker compose ps
	docker compose exec -T web python -c "from urllib.request import urlopen; print(urlopen('http://127.0.0.1:8765/health/ready', timeout=5).read().decode())"

backup:
	docker compose --profile ops run --rm --no-deps backup
