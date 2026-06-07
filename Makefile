# ─────────────────────────────────────────────────────────────────────────────
# job-agent Makefile
#
# Common workflow:
#   make install           # local Python deps for testing
#   make secrets ...       # set SSM parameters (one-time per environment)
#   make deploy            # build + plan + apply
#   make invoke-collector  # run collector now (skip the schedule)
#   make logs-collector    # tail collector logs
# ─────────────────────────────────────────────────────────────────────────────

PY := python
TF := terraform
AWS := aws
REGION ?= us-east-1
TF_DIR := terraform
BUILD_DIR := build
SRC_DIR := src
ENV_NAME ?= prod
# Disable MSYS path conversion on Windows/Git Bash
export MSYS_NO_PATHCONV=1
FUNCTIONS := collector scorer notifier

.PHONY: help install build clean test lint deploy plan apply destroy \
        invoke-collector invoke-notifier secrets \
        logs-collector logs-scorer logs-notifier

help:
	@echo "Targets:"
	@echo "  install            Install local dev deps"
	@echo "  build              Package all three Lambdas into ./build/"
	@echo "  test               Run pytest"
	@echo "  lint               Run ruff"
	@echo "  plan               terraform plan"
	@echo "  deploy             build + terraform apply"
	@echo "  destroy            terraform destroy"
	@echo "  secrets            Store API keys in SSM"
	@echo "                       make secrets ANTHROPIC_API_KEY=... [JSEARCH_API_KEY=...]"
	@echo "  invoke-collector   Manually trigger one collector run"
	@echo "  invoke-notifier    Manually trigger one notifier run"
	@echo "  logs-collector     Tail collector CloudWatch logs"

# ─────────────────────────────────────────────────────────────────────────────
# Local dev
# ─────────────────────────────────────────────────────────────────────────────
install:
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements-dev.txt

lint:
	$(PY) -m ruff check $(SRC_DIR) tests

test:
	$(PY) -m pytest tests/ -v

# ─────────────────────────────────────────────────────────────────────────────
# Build — assembles each function's deploy package in ./build/<function>/
# Each package contains: function code, shared/ module, resumes/ (scorer
# only), config.yaml (collector only), and pip-installed dependencies.
# ─────────────────────────────────────────────────────────────────────────────
build: clean build-layer build-tls-layer
	@for fn in $(FUNCTIONS); do \
		echo "==> packaging $$fn"; \
		mkdir -p $(BUILD_DIR)/$$fn; \
		cp -r $(SRC_DIR)/$$fn/*.py $(BUILD_DIR)/$$fn/; \
		cp -r $(SRC_DIR)/shared $(BUILD_DIR)/$$fn/; \
		if [ "$$fn" = "collector" ]; then \
			cp config/config.yaml $(BUILD_DIR)/$$fn/; \
		fi; \
		if [ "$$fn" = "scorer" ]; then \
			cp -r resumes $(BUILD_DIR)/$$fn/; \
		fi; \
		if [ -f $(SRC_DIR)/$$fn/requirements.txt ]; then \
			$(PY) -m pip install \
				--platform manylinux2014_x86_64 \
				--target $(BUILD_DIR)/$$fn \
				--implementation cp \
				--python-version 3.12 \
				--only-binary=:all: \
				--upgrade \
				-r $(SRC_DIR)/$$fn/requirements.txt; \
		fi; \
		if [ "$$fn" = "collector" ]; then \
		    rm -rf $(BUILD_DIR)/$$fn/pandas $(BUILD_DIR)/$$fn/pandas-* \
		           $(BUILD_DIR)/$$fn/numpy $(BUILD_DIR)/$$fn/numpy-* \
		           $(BUILD_DIR)/$$fn/pyarrow $(BUILD_DIR)/$$fn/pyarrow-* \
		           $(BUILD_DIR)/$$fn/numpy.libs \
				   $(BUILD_DIR)/$$fn/tls_client $(BUILD_DIR)/$$fn/tls_client-* \
		           $(BUILD_DIR)/$$fn/botocore/data/ec2 \
		           $(BUILD_DIR)/$$fn/botocore/data/s3 \
		           $(BUILD_DIR)/$$fn/botocore/data/rds \
		           $(BUILD_DIR)/$$fn/botocore/data/sagemaker*; \
	    fi; \
	done
	@echo "==> build complete"

build-layer:
	mkdir -p $(BUILD_DIR)/layer/python
	$(PY) -m pip install pandas numpy \
		--platform manylinux2014_x86_64 \
		--target $(BUILD_DIR)/layer/python \
		--implementation cp \
		--python-version 3.12 \
		--only-binary=:all: \
		--upgrade
	cd $(BUILD_DIR)/layer && zip -r ../pandas_layer.zip python/
	@echo "==> layer built"

build-tls-layer:
	mkdir -p $(BUILD_DIR)/tls_layer/python
	$(PY) -m pip install tls-client \
		--platform manylinux2014_x86_64 \
		--target $(BUILD_DIR)/tls_layer/python \
		--implementation cp \
		--python-version 3.12 \
		--only-binary=:all: \
		--upgrade
	rm -f $(BUILD_DIR)/tls_layer/python/tls_client/dependencies/tls-client-32.dll \
	      $(BUILD_DIR)/tls_layer/python/tls_client/dependencies/tls-client-64.dll \
	      $(BUILD_DIR)/tls_layer/python/tls_client/dependencies/tls-client-arm64.dylib \
	      $(BUILD_DIR)/tls_layer/python/tls_client/dependencies/tls-client-arm64.so \
	      $(BUILD_DIR)/tls_layer/python/tls_client/dependencies/tls-client-x86.dylib \
	      $(BUILD_DIR)/tls_layer/python/tls_client/dependencies/tls-client-amd64.so
	cd $(BUILD_DIR)/tls_layer && zip -r ../tls_layer.zip python/
	@echo "==> tls layer built"   

clean:
	rm -rf $(BUILD_DIR)

# ─────────────────────────────────────────────────────────────────────────────
# Terraform
# ─────────────────────────────────────────────────────────────────────────────
plan: build
	cd $(TF_DIR) && $(TF) init -upgrade && $(TF) plan -var-file=$(ENV_NAME).tfvars

apply: build
	cd $(TF_DIR) && $(TF) init -upgrade && $(TF) apply -var-file=$(ENV_NAME).tfvars -auto-approve

deploy: apply

destroy:
	cd $(TF_DIR) && $(TF) destroy -var-file=$(ENV_NAME).tfvars

# ─────────────────────────────────────────────────────────────────────────────
# Secrets
# ─────────────────────────────────────────────────────────────────────────────
secrets:
	@if [ -z "$(ANTHROPIC_API_KEY)" ]; then \
		echo "ERROR: ANTHROPIC_API_KEY is required"; exit 1; \
	fi
	$(AWS) ssm put-parameter \
		--name "/job-agent/anthropic_api_key" \
		--value "$(ANTHROPIC_API_KEY)" \
		--type SecureString \
		--overwrite \
		--region $(REGION)
	@if [ -n "$(JSEARCH_API_KEY)" ]; then \
		$(AWS) ssm put-parameter \
			--name "/job-agent/jsearch_api_key" \
			--value "$(JSEARCH_API_KEY)" \
			--type SecureString \
			--overwrite \
			--region $(REGION); \
	fi
	@echo "secrets stored"

# ─────────────────────────────────────────────────────────────────────────────
# Manual invocation
# ─────────────────────────────────────────────────────────────────────────────
invoke-collector:
	@mkdir -p $(BUILD_DIR)
	$(AWS) lambda invoke \
		--function-name job-agent-$(ENV_NAME)-collector \
		--region $(REGION) \
		--cli-binary-format raw-in-base64-out \
		--payload '{}' \
		$(BUILD_DIR)/invoke-collector.json
	@cat $(BUILD_DIR)/invoke-collector.json && echo

invoke-notifier:
	@mkdir -p $(BUILD_DIR)
	$(AWS) lambda invoke \
		--function-name job-agent-$(ENV_NAME)-notifier \
		--region $(REGION) \
		--cli-binary-format raw-in-base64-out \
		--payload '{}' \
		$(BUILD_DIR)/invoke-notifier.json
	@cat $(BUILD_DIR)/invoke-notifier.json && echo

# ─────────────────────────────────────────────────────────────────────────────
# Logs
# ─────────────────────────────────────────────────────────────────────────────
logs-collector:
	$(AWS) logs tail /aws/lambda/job-agent-$(ENV_NAME)-collector --follow --region $(REGION)

logs-scorer:
	$(AWS) logs tail /aws/lambda/job-agent-$(ENV_NAME)-scorer --follow --region $(REGION)

logs-notifier:
	$(AWS) logs tail /aws/lambda/job-agent-$(ENV_NAME)-notifier --follow --region $(REGION)