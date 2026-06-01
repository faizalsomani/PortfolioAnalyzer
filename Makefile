PYTHON ?= python3
VENV := .venv
BIN := $(VENV)/bin
PY := $(BIN)/python
PIP := $(PY) -m pip
DB ?= data/app.db
HOST ?= 127.0.0.1
PORT ?= 8000

.PHONY: help setup backend demo-data test sql db-summary clean

help:
	@echo "Available commands:"
	@echo "  make setup       Create .venv and install dependencies"
	@echo "  make backend     Start FastAPI on http://$(HOST):$(PORT)"
	@echo "  make demo-data   Create demo PDFs and sync them into SQLite"
	@echo "  make test        Run Python compile checks and unit tests"
	@echo "  make sql         Open SQLite shell for $(DB)"
	@echo "  make db-summary  Print companies and recent processing runs"
	@echo "  make clean       Delete local DB/input/processed data"

$(VENV)/.installed: requirements.txt
	$(PYTHON) -m venv $(VENV)
	$(PY) -m ensurepip --upgrade
	$(PIP) install --upgrade --force-reinstall pip
	$(PIP) install -r requirements.txt
	touch $(VENV)/.installed

setup: $(VENV)/.installed
	@echo "Virtualenv ready at $(VENV)"

backend: $(VENV)/.installed
	$(PY) -m uvicorn backend.main:app --reload --host $(HOST) --port $(PORT)

demo-data: $(VENV)/.installed
	$(PY) -m backend.demo_data

test: $(VENV)/.installed
	$(PY) -m py_compile backend/*.py main.py
	$(PY) -m pytest -q

sql:
	sqlite3 $(DB)

db-summary:
	sqlite3 $(DB) ".headers on" ".mode column" \
		"SELECT id, display_name, status FROM companies ORDER BY id;" \
		"SELECT id, company_id, status, mode, model, substr(error, 1, 80) AS error FROM processing_runs ORDER BY id DESC LIMIT 10;"

clean:
	rm -rf data/app.db data/input data/processed
	mkdir -p data/input data/processed
