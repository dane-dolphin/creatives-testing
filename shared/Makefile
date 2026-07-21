# SAM invokes `make build-SharedLayer` here (ContentUri: ../shared/) during `sam build`.
# The only third-party dep is boto3, which the Lambda runtime already provides, so the
# layer is just the pure ct_shared source — no wheels to build.
build-SharedLayer:
	mkdir -p "$(ARTIFACTS_DIR)/python"
	cp -r src/ct_shared "$(ARTIFACTS_DIR)/python/"
