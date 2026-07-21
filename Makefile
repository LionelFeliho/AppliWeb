.PHONY: dev test lint ingest-sample ingest-g10-demo ingest-credit-demo

dev:
	docker compose up --build

test:
	cd apps/api && pytest -q

lint:
	cd apps/api && python -m compileall -q app tests

ingest-sample:
	cd apps/api && python -m app.market_data.cli ingest-csv --date 2026-07-20 --file ../../sample_data/settlements.csv

ingest-g10-demo:
	cd apps/api && python -m app.market_data.cli ingest-csv --date 2026-07-20 --file ../../sample_data/g10_xccy_demo.csv

ingest-credit-demo:
	cd apps/api && python -m app.market_data.cli ingest-csv --date 2026-07-20 --file ../../sample_data/credit_cross_section_demo.csv
