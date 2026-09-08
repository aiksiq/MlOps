.PHONY: install generate bench check clean

UV := $(shell if command -v uv >/dev/null 2>&1; then command -v uv; else echo .venv/Scripts/uv.exe; fi)

install:
	$(UV) sync

generate:
	$(UV) run python -m src.generate

bench:
	$(UV) run python -m src.bench

check:
	bash tests/check.sh

clean:
	rm -rf docs/bench.json out1.txt out2.txt params.yaml.bak
