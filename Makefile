.PHONY: build-WebhookFunction build-ReviewWorkerFunction build-MiningWorkerFunction local-up local-bootstrap local-down

local-up:
	podman compose up -d localstack

local-bootstrap:
	bash scripts/bootstrap-localstack.sh

local-down:
	podman compose stop localstack

build-WebhookFunction:
	python -m pip install -r requirements.txt -t "$(ARTIFACTS_DIR)"
	cp *.py config.example.yaml "$(ARTIFACTS_DIR)"

build-ReviewWorkerFunction:
	python -m pip install -r requirements.txt -t "$(ARTIFACTS_DIR)"
	cp *.py config.example.yaml "$(ARTIFACTS_DIR)"

build-MiningWorkerFunction:
	python -m pip install -r requirements.txt -t "$(ARTIFACTS_DIR)"
	cp *.py config.example.yaml "$(ARTIFACTS_DIR)"