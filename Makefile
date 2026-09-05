# StoreGuard — common tasks
#
#   make dashboard        # open UI; videos from data/
#   make cloud            # run the cloud control plane (cabinet)
#   make frontend-build   # rebuild both Vue frontends
#   make run              # pipeline from CONFIG
#   make test             # pytest
#   make help             # list targets

UV               ?= uv
HOST             ?= 127.0.0.1
PORT             ?= 8765
CLOUD_PORT       ?= 8000
DEVICE           ?= auto
CONFIG           ?= configs/example.yaml
DATA             ?= data
CLOUD_FRONTEND     := src/storeguard/cloud/frontend
DASHBOARD_FRONTEND := src/storeguard/dashboard/frontend

.PHONY: help install sync dashboard cloud frontend-build cloud-frontend-build \
	dashboard-frontend-build run run-show test clean data

help:
	@echo "StoreGuard targets:"
	@echo "  make install                sync Python deps (uv sync)"
	@echo "  make data                   create data/ folder for videos"
	@echo "  make dashboard              open detection UI at http://$(HOST):$(PORT)"
	@echo "  make cloud                  run the cloud control plane at http://$(HOST):$(CLOUD_PORT) (--dev, local sqlite)"
	@echo "  make frontend-build         rebuild both Vue frontends (cloud cabinet + dashboard)"
	@echo "  make cloud-frontend-build   rebuild only the cloud cabinet's Vue frontend into cloud/static/"
	@echo "  make dashboard-frontend-build  rebuild only the dashboard's Vue frontend into dashboard/static/"
	@echo "  make run                    run pipeline (CONFIG=$(CONFIG))"
	@echo "  make run-show               run pipeline with preview windows"
	@echo "  make test                   run pytest"
	@echo ""
	@echo "Put videos in $(DATA)/ then: make dashboard"
	@echo "Changed a frontend? make frontend-build, then make cloud / make dashboard"
	@echo "Note: 'Live view' in the cabinet proxies to the dashboard at http://127.0.0.1:$(PORT) — start both to use it"
	@echo "Overrides: HOST PORT CLOUD_PORT DEVICE CONFIG DATA"

install sync:
	$(UV) sync

data:
	mkdir -p $(DATA)
	@echo "Drop .mp4 files into $(DATA)/ then run: make dashboard"

dashboard: install data
	@echo "→ http://$(HOST):$(PORT)  (videos from $(DATA)/)"
	$(UV) run storeguard dashboard --host $(HOST) --port $(PORT) --device $(DEVICE) --data $(DATA) --config $(CONFIG)

cloud: install
	@echo "→ http://$(HOST):$(CLOUD_PORT)  (--dev: local sqlite, tables created on startup)"
	$(UV) run storeguard cloud --host $(HOST) --port $(CLOUD_PORT) --dev

frontend-build: cloud-frontend-build dashboard-frontend-build

cloud-frontend-build:
	cd $(CLOUD_FRONTEND) && npm install && npm run build

dashboard-frontend-build:
	cd $(DASHBOARD_FRONTEND) && npm install && npm run build

run: install
	$(UV) run storeguard run --config $(CONFIG)

run-show: install
	$(UV) run storeguard run --config $(CONFIG) --show

test: install
	$(UV) run pytest -q

clean:
	rm -rf .pytest_cache
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} + 2>/dev/null || true
