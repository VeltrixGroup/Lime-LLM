# StoreGuard — common tasks
#
#   make dashboard   # open UI; videos from data/
#   make run         # pipeline from CONFIG
#   make test        # pytest
#   make help        # list targets

UV       ?= uv
HOST     ?= 127.0.0.1
PORT     ?= 8765
DEVICE   ?= auto
CONFIG   ?= configs/example.yaml
DATA     ?= data

.PHONY: help install sync dashboard run run-show test clean data

help:
	@echo "StoreGuard targets:"
	@echo "  make install     sync Python deps (uv sync)"
	@echo "  make data        create data/ folder for videos"
	@echo "  make dashboard   open detection UI at http://$(HOST):$(PORT)"
	@echo "  make run         run pipeline (CONFIG=$(CONFIG))"
	@echo "  make run-show    run pipeline with preview windows"
	@echo "  make test        run pytest"
	@echo ""
	@echo "Put videos in $(DATA)/ then: make dashboard"
	@echo "Overrides: HOST PORT DEVICE CONFIG DATA"

install sync:
	$(UV) sync

data:
	mkdir -p $(DATA)
	@echo "Drop .mp4 files into $(DATA)/ then run: make dashboard"

dashboard: install data
	@echo "→ http://$(HOST):$(PORT)  (videos from $(DATA)/)"
	$(UV) run storeguard dashboard --host $(HOST) --port $(PORT) --device $(DEVICE) --data $(DATA) --config $(CONFIG)

run: install
	$(UV) run storeguard run --config $(CONFIG)

run-show: install
	$(UV) run storeguard run --config $(CONFIG) --show

test: install
	$(UV) run pytest -q

clean:
	rm -rf .pytest_cache
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} + 2>/dev/null || true
