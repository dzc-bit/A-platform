.PHONY: up down logs ps health backend-test frontend-build compose-config reset-demo \
	stack-up stack-down stack-ps stack-health stack-config stack-smoke

POWERSHELL ?= powershell

up:
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs -f

ps:
	docker compose ps

health:
	docker compose exec backend python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health').read().decode())"

backend-test:
	cd backend && python -m pytest

frontend-build:
	cd frontend && corepack enable && pnpm install --frozen-lockfile && pnpm run build

compose-config:
	docker compose config -q

reset-demo:
	docker compose down -v

# The Dify official compose project is intentionally kept separate from the
# course stack. Use these targets only after Docker Desktop/Engine is running.
stack-up:
	$(POWERSHELL) -NoProfile -ExecutionPolicy Bypass -File scripts/stack.ps1 -Action up -WithDify

stack-down:
	$(POWERSHELL) -NoProfile -ExecutionPolicy Bypass -File scripts/stack.ps1 -Action down -WithDify

stack-ps:
	$(POWERSHELL) -NoProfile -ExecutionPolicy Bypass -File scripts/stack.ps1 -Action ps -WithDify

stack-health:
	$(POWERSHELL) -NoProfile -ExecutionPolicy Bypass -File scripts/stack.ps1 -Action health -WithDify

stack-smoke:
	$(POWERSHELL) -NoProfile -ExecutionPolicy Bypass -File scripts/stack.ps1 -Action smoke -WithDify

stack-config:
	$(POWERSHELL) -NoProfile -ExecutionPolicy Bypass -File scripts/stack.ps1 -Action config -WithDify
