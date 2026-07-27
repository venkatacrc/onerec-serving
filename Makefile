.PHONY: preflight install download pull smoke bench report slides all stop clean \
	platform-bootstrap platform-apply platform-test capacity-plan

preflight:
	./scripts/00_preflight_check.sh

install:
	./scripts/01_install_base_deps.sh

download:
	./scripts/02_download_model.sh

pull:
	bash -c 'source scripts/env.sh && docker pull "$$VLLM_IMAGE" && docker pull "$$SGLANG_IMAGE" && docker pull "$$TRTLLM_IMAGE"'

smoke:
	bash -c 'source scripts/env.sh && \
		./scripts/10_serve_vllm.sh --run-name smoke --tp 1 --gpus 0 --dtype bfloat16 --max-model-len 8192 && \
		./scripts/20_smoke_test.sh --port 8000 && \
		./scripts/90_stop_serving.sh vllm'

bench:
	bash -c 'source scripts/env.sh && source "$$VENV_DIR/bin/activate" && \
		python3 bench/run_matrix.py --matrix configs/matrix.yaml'

report:
	bash -c 'source scripts/env.sh && source "$$VENV_DIR/bin/activate" && \
		python3 bench/generate_report.py --results-dir results --out-dir results/report'

slides:
	bash -c 'source scripts/env.sh && source "$$VENV_DIR/bin/activate" && \
		python3 bench/generate_slides.py --results-dir results --report-dir results/report'

all:
	./run_all.sh

stop:
	./scripts/90_stop_serving.sh

clean: stop
	rm -rf results/* logs/*
	touch results/.gitkeep logs/.gitkeep

# --- Production layer (platform/) -- see docs/PRODUCTION_ARCHITECTURE.md ---

platform-bootstrap:
	./platform/bootstrap_k8s.sh

platform-apply:
	kubectl apply -f platform/k8s/00-namespace.yaml
	kubectl apply -f platform/k8s/

platform-test:
	bash -c 'source scripts/env.sh && source "$$VENV_DIR/bin/activate" && \
		pip install --quiet -r platform/requirements-dev.txt && \
		python3 platform/router/test_router_local.py && \
		python3 platform/client_sdk/test_client_local.py && \
		python3 platform/feature_store/test_feature_store_local.py && \
		python3 platform/registry/test_registry_local.py'

capacity-plan:
	bash -c 'source scripts/env.sh && source "$$VENV_DIR/bin/activate" && \
		python3 bench/capacity_planner.py --target-qps $${QPS:-50} --avg-output-tokens $${AVG_OUTPUT_TOKENS:-256} \
			--out results/report/CAPACITY_PLAN.md'
