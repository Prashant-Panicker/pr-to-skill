.PHONY: build-WebhookFunction build-WorkerFunction

build-WebhookFunction:
	python -m pip install -r requirements.txt -t "$(ARTIFACTS_DIR)"
	cp *.py config.example.yaml "$(ARTIFACTS_DIR)"

build-WorkerFunction:
	python -m pip install -r requirements.txt -t "$(ARTIFACTS_DIR)"
	cp *.py config.example.yaml "$(ARTIFACTS_DIR)"