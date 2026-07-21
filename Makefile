.PHONY: dev test lint ingest-sample

dev:
	docker compose up --build

test:
	cd apps/api && pytest -q

lint:
	cd apps/api && python -m compileall -q app tests

ingest-sample:
	cd apps/api && python -m app.market_data.cli ingest-csv --date 2026-07-20 --file ../../sample_data/settlements.csv
