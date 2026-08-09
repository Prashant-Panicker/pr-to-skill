.PHONY: build-WebhookFunction build-ReviewWorkerFunction build-MiningWorkerFunction

build-WebhookFunction:
	python -m pip install -r requirements.txt -t "$(ARTIFACTS_DIR)"
	cp *.py config.example.yaml "$(ARTIFACTS_DIR)"

build-ReviewWorkerFunction:
	python -m pip install -r requirements.txt -t "$(ARTIFACTS_DIR)"
	cp *.py config.example.yaml "$(ARTIFACTS_DIR)"

build-MiningWorkerFunction:
	python -m pip install -r requirements.txt -t "$(ARTIFACTS_DIR)"
	cp *.py config.example.yaml "$(ARTIFACTS_DIR)"