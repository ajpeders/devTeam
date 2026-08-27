.PHONY: all venv test test-all test-api test-agents clean

VENV_DIR  := .venv
PYTEST    := $(VENV_DIR)/bin/python -m pytest

all: venv

# ── Python venv ──────────────────────────────────────────────────────
venv: $(VENV_DIR)/bin/activate

$(VENV_DIR)/bin/activate: pyproject.toml
	python3 -m venv $(VENV_DIR)
	$(VENV_DIR)/bin/pip install --upgrade pip
	$(VENV_DIR)/bin/pip install -e .
	touch $(VENV_DIR)/bin/activate

# ── Tests ──────────────────────────────────────────────────────────────
test: test-api test-agents

# What CI runs. `test` above only covers test_server.py + agents/, which misses
# colocated suites like daemon/test_config.py.
test-all: venv
	$(PYTEST) -q

test-api: venv
	$(PYTEST) daemon/api/test_server.py -q

test-agents: venv
	$(PYTEST) agents/ -q

# ── Cleanup ──────────────────────────────────────────────────────────
clean:
	rm -rf $(VENV_DIR)
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true