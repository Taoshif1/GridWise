.PHONY: up down logs rebuild health
up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f --tail=150

rebuild:
	docker compose build --no-cache
	docker compose up -d

health:
	curl -fsS http://127.0.0.1:1337/health
