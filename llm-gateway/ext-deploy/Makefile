# Used by `sam build` (BuildMethod: makefile). Builds an arm64 zip for Python 3.13.
build-GatewayFunction:
	uv export --frozen --no-dev --no-hashes --no-emit-project -o $(ARTIFACTS_DIR)/requirements.txt
	uv pip install --target $(ARTIFACTS_DIR) --python-version 3.13 --python-platform aarch64-manylinux2014 --only-binary :all: -r $(ARTIFACTS_DIR)/requirements.txt
	cp -r src/gateway $(ARTIFACTS_DIR)/gateway
	cp run.sh $(ARTIFACTS_DIR)/run.sh
	chmod +x $(ARTIFACTS_DIR)/run.sh
