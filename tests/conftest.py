"""Test setup: env, ct_shared on the path, in-memory DynamoDB, and handler loaders."""

import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "shared", "src"))

# Must be set before ct_shared.config is imported (it reads env at import time).
os.environ.setdefault("TABLE_NAME", "ct-test-jobs")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("DEV_API_KEY_SECRET", "ct-test/classifier-dev-api-key")
os.environ.setdefault("CALLBACK_SECRET_NAME", "ct-test/callback-secret")


def load_handler(name):
    """Load lambdas/<name>/handler.py as a uniquely-named module (they're all 'handler')."""
    spec = importlib.util.spec_from_file_location(
        f"h_{name}", os.path.join(ROOT, "lambdas", name, "handler.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def api_handler():
    return load_handler("api")


class FakeContext:
    """Minimal Lambda context: enough time for a single loop iteration in tests."""
    def get_remaining_time_in_millis(self):
        return 30_000


@pytest.fixture
def ctx():
    return FakeContext()


@pytest.fixture
def db():
    """In-memory DynamoDB matching the SAM table (2 GSIs), fresh per test."""
    from moto import mock_aws

    with mock_aws():
        import boto3

        from ct_shared import dynamo
        dynamo._table = None  # drop any resource cached against a prior mock

        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName="ct-test-jobs",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "classifier_job_id", "AttributeType": "S"},
                {"AttributeName": "result_ref", "AttributeType": "S"},
                {"AttributeName": "submit_state", "AttributeType": "S"},
                {"AttributeName": "state_ts", "AttributeType": "N"},
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "classifier-job-index",
                    "KeySchema": [
                        {"AttributeName": "classifier_job_id", "KeyType": "HASH"},
                        {"AttributeName": "result_ref", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "KEYS_ONLY"},
                },
                {
                    "IndexName": "submit-state-index",
                    "KeySchema": [
                        {"AttributeName": "submit_state", "KeyType": "HASH"},
                        {"AttributeName": "state_ts", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
        )
        yield dynamo
        dynamo._table = None
