PYTHON ?= python3

.PHONY: test lint help

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m py_compile rancher-migration-validator.py

help:
	./rancher-migration-validator.py --help
