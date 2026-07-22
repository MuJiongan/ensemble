.PHONY: install backend frontend dev build serve test clean

VENV := backend/.venv

install:
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	cd backend && .venv/bin/pip install -e ".[dev]"
	cd frontend && npm install

backend:
	cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000

frontend:
	cd frontend && npm run dev

dev:
	@trap 'kill 0' INT TERM EXIT; \
	$(MAKE) backend & \
	$(MAKE) frontend & \
	wait

# Production: build the UI once, then serve it and /api from one loopback-only
# process. A private reverse proxy such as Tailscale Serve can safely expose it.
build:
	cd frontend && npm run build

serve:
	@test -f frontend/dist/index.html || (echo "Run 'make build' first" && exit 1)
	cd backend && .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1

test:
	cd backend && .venv/bin/pytest -q

clean:
	rm -f backend/*.db
	rm -rf backend/__pycache__ backend/app/__pycache__ backend/app/*/__pycache__
	rm -rf frontend/node_modules frontend/dist
