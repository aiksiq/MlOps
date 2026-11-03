PYTHON ?= python3
.PHONY: check check-fast flow-stub flow-hw67 api-stub api-model benchmark

check:
	$(PYTHON) -m compileall -q release.py lab.py service.py api.py api_server.py inference.py prefect_flow.py hw67_integration.py benchmark_api.py tests
	$(PYTHON) -m unittest discover -s tests -v

check-fast: check

flow-stub:
	$(PYTHON) prefect_flow.py --workspace .lab-flow run

flow-hw67:
	$(PYTHON) prefect_flow.py --workspace .release-flow hw67 --hw67-root ../hw67-broken

api-stub:
	$(PYTHON) api_server.py --workspace .lab --port 8091 --warmup-seconds 5

api-model:
	$(PYTHON) api_server.py --backend model --workspace .release-flow --hw67-root ../hw67-broken --port 8091

benchmark:
	$(PYTHON) benchmark_api.py --workspace .release --backend model --hw67-root ../hw67-broken --requests 4
