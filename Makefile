.PHONY: help build deploy validate test clean
STACK  ?= creative-tester-dev
REGION ?= us-east-1

help:
	@echo "Targets: build | deploy | validate | test | clean"
	@echo "  STACK=$(STACK)  REGION=$(REGION)"

build:
	sam build

deploy: build
	sam deploy --stack-name $(STACK) --region $(REGION) \
		--capabilities CAPABILITY_IAM CAPABILITY_AUTO_EXPAND --resolve-s3

validate:
	sam validate --lint

test:
	PYTHONPATH=shared/src python -m pytest -q

clean:
	rm -rf .aws-sam
	find . -type d -name __pycache__ -exec rm -rf {} +
