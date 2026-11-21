.PHONY: up down logs test test-integration lint load demo demo-real smoke-real spend

export HOST_UID := $(shell id -u)
export HOST_GID := $(shell id -g)

up:            ## build and start api, 2 workers, mocklab, postgres
	@test -f .env || cp .env.example .env
	docker compose up -d --build --wait

down:          ## stop everything and delete the database volume
	docker compose down -v

logs:
	docker compose logs -f api worker

test:          ## unit + DB tests (needs postgres from `make up`)
	docker compose --profile tests run --rm tests pytest -q tests/unit tests/db

test-integration:  ## end-to-end failure injection against the running stack
	docker compose --profile tests run --rm tests pytest -q -m integration tests/integration

lint:
	docker compose --profile tests run --rm --no-deps tests sh -c "ruff check --no-cache . && ruff format --no-cache --check . && mypy --cache-dir=/tmp/mypy app mocklab"

load:          ## 1,000 messages incl. duplicates -> LOAD_REPORT.md
	docker compose --profile load run --rm loadtest

demo:          ## scripted video walkthrough (mock LLM, zero cost)
	bash scripts/video_demo.sh

demo-real:     ## <=40 real-LLM messages (worker gets API_KEY from .env), then workers go back to mock
	@grep -q '^API_KEY=.\+' .env || (echo "set API_KEY in .env first" && exit 1)
	LLM_MODE=real docker compose up -d --wait worker
	mkdir -p docs
	docker compose --profile tests run --rm --user "$(HOST_UID):$(HOST_GID)" -v ./docs:/srv/docs tests \
		python -m scripts.demo_real_llm --out docs/REAL_LLM_DEMO.md; status=$$?; \
		docker compose up -d worker; exit $$status

smoke-real:    ## <=3 live LLM calls: plain completion + check_slots tool round trip
	docker compose run --rm --no-deps -e LLM_MODE=real worker python -m app.agent.llm.smoke

spend:         ## actual real-LLM spend from the append-only ledger
	docker compose --profile tests run --rm tests python -m scripts.spend_report
