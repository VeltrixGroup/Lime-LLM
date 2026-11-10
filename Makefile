# StoreGuard — common tasks
#
#   make dev              # Postgres + backend (:8000) + frontend with hot reload (:5173)
#   make serve            # Postgres + everything on one port: control panel + live detection (8000)
#   make frontend-dev     # control panel UI with hot reload at :5173 (needs make serve)
#   make dashboard        # standalone detection UI only (8765)
#   make cloud            # control plane only, no detection
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

.PHONY: help install sync dev serve frontend-dev dashboard cloud frontend-build cloud-frontend-build \
	dashboard-frontend-build run run-show test clean data \
	db-up db-down db-logs db-shell migrate optimize

help:
	@echo "StoreGuard targets:"
	@echo "  make install                sync Python deps (uv sync)"
	@echo "  make data                   create data/ folder for videos"
	@echo "  make dev                    backend :$(CLOUD_PORT) + frontend hot reload :5173 together (Ctrl+C stops both)"
	@echo "  make serve                  control panel + live detection, one port: http://$(HOST):$(CLOUD_PORT)  (live view at /live/)"
	@echo "  make frontend-dev           control panel UI with hot reload at http://localhost:5173 (run make serve too)"
	@echo "  make dashboard              standalone detection UI only at http://$(HOST):$(PORT)"
	@echo "  make cloud                  control plane only, no detection, at http://$(HOST):$(CLOUD_PORT)"
	@echo "  make frontend-build         rebuild both Vue frontends (cloud cabinet + dashboard)"
	@echo "  make cloud-frontend-build   rebuild only the cloud cabinet's Vue frontend into cloud/static/"
	@echo "  make dashboard-frontend-build  rebuild only the dashboard's Vue frontend into dashboard/static/"
	@echo "  make run                    run pipeline (CONFIG=$(CONFIG))"
	@echo "  make run-show               run pipeline with preview windows"
	@echo "  make test                   run pytest"
	@echo "  make db-up                  start Postgres in Docker (data kept in a volume)"
	@echo "  make db-down                stop Postgres (data stays)"
	@echo "  make db-logs / db-shell     Postgres logs / psql shell"
	@echo "  make optimize               build the TensorRT engine (NVIDIA) and show the speed-up"
	@echo "  make migrate                apply database migrations (alembic upgrade head)"
	@echo ""
	@echo "Normal use: make serve, then open http://$(HOST):$(CLOUD_PORT)"
	@echo "Changed a frontend? make frontend-build, then restart make serve"
	@echo "Overrides: HOST PORT CLOUD_PORT DEVICE CONFIG DATA"

# With an NVIDIA GPU also install TensorRT (plain `uv sync` would remove it).
install sync:
	@if command -v nvidia-smi >/dev/null 2>&1; then \
		$(UV) sync --extra tensorrt || { echo "TensorRT could not be installed - continuing without it"; $(UV) sync; }; \
	else $(UV) sync; fi

optimize: install
	$(UV) run storeguard optimize --benchmark --device $(DEVICE)

data:
	mkdir -p $(DATA)
	@echo "Drop .mp4 files into $(DATA)/ then run: make dashboard"

serve: install data db-up migrate
	@echo "→ http://$(HOST):$(CLOUD_PORT)  (control panel; live detection at /live/, videos from $(DATA)/)"
	$(UV) run storeguard cloud --host $(HOST) --port $(CLOUD_PORT) --with-dashboard --device $(DEVICE) --data $(DATA) --config $(CONFIG)

# Backend and the Vite dev server together; Ctrl+C (or either one exiting)
# stops both. Open http://localhost:5173 while editing the frontend, or
# http://127.0.0.1:$(CLOUD_PORT) for the built version.
dev: install data db-up migrate
	@cd $(CLOUD_FRONTEND) && [ -d node_modules ] || npm install
	@echo "→ backend:  http://$(HOST):$(CLOUD_PORT)"
	@echo "→ frontend: http://localhost:5173  (hot reload)"
	@trap 'kill 0' INT TERM EXIT; \
	$(UV) run storeguard cloud --host $(HOST) --port $(CLOUD_PORT) --with-dashboard --device $(DEVICE) --data $(DATA) --config $(CONFIG) & \
	(cd $(CLOUD_FRONTEND) && npm run dev) & \
	wait

frontend-dev:
	@echo "→ http://localhost:5173  (API calls go to make serve on :$(CLOUD_PORT))"
	cd $(CLOUD_FRONTEND) && npm install && npm run dev

dashboard: install data
	@echo "→ http://$(HOST):$(PORT)  (videos from $(DATA)/)"
	$(UV) run storeguard dashboard --host $(HOST) --port $(PORT) --device $(DEVICE) --data $(DATA) --config $(CONFIG)

cloud: install db-up migrate
	@echo "→ http://$(HOST):$(CLOUD_PORT)  (control panel only, no detection)"
	$(UV) run storeguard cloud --host $(HOST) --port $(CLOUD_PORT)

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

db-up:
	@docker compose up -d --wait db
	@echo "→ Postgres on 127.0.0.1:$${STOREGUARD_DB_PORT:-5432} (db/user: storeguard)"

db-down:
	docker compose down

db-logs:
	docker compose logs -f db

db-shell:
	docker compose exec db psql -U storeguard -d storeguard

migrate:
	$(UV) run alembic upgrade head
