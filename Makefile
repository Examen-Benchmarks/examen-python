lint-types: lint build

run: 
	uv run -m  uvicorn adapters.web.fastapi:api --reload

lint:
	uv run ruff format . 
	uv run ruff check --fix . 
	uv run ruff check --fix --select I .

check-lint:
	uv run ruff format . --check 
	uv run ruff check . 
	uv run ruff check --select I .

build: 
	uv run mypy --strict .
	uv lock
# 	uv export --no-hashes > requirements.txt

test: 
	uv run pytest --cov=. --cov-report=html

check: check-lint build test
all: lint build test

# IMAGE := registry.cordos.fr/des/ai-chatter
# TAG ?= $(shell git rev-parse --short HEAD)
# NAMESPACE ?= ai-chatter-staging

# publish:
# 	docker build -t $(IMAGE):$(TAG) -t $(IMAGE):latest .
# 	docker push $(IMAGE):$(TAG)
# 	docker push $(IMAGE):latest

# deploy:
# 	kubectl set image deployment/api -n $(NAMESPACE) \
# 		api=$(IMAGE):$(TAG) \
# 		migrations=$(IMAGE):$(TAG)
# 	kubectl set image deployment/worker -n $(NAMESPACE) \
# 		worker=$(IMAGE):$(TAG)
# 	kubectl set image deployment/scheduler -n $(NAMESPACE) \
# 		scheduler=$(IMAGE):$(TAG)
# 	kubectl set image deployment/telegram-bot -n $(NAMESPACE) \
# 		telegram-bot=$(IMAGE):$(TAG)
# 	kubectl rollout status deployment/api -n $(NAMESPACE) --timeout=5m
# 	kubectl rollout status deployment/worker -n $(NAMESPACE) --timeout=5m
# 	kubectl rollout status deployment/scheduler -n $(NAMESPACE) --timeout=5m
# 	kubectl rollout status deployment/telegram-bot -n $(NAMESPACE) --timeout=5m

# publish-deploy: publish deploy