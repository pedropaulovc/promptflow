from pathlib import Path  # noqa: C4726

PROMPTFLOW_ROOT = Path(__file__).parent.parent
RUNTIME_TEST_CONFIGS_ROOT = Path(PROMPTFLOW_ROOT / "tests/test_configs/runtime")
EXECUTOR_REQUESTS_ROOT = Path(PROMPTFLOW_ROOT / "tests/test_configs/executor_api_requests")
MODEL_ROOT = Path(PROMPTFLOW_ROOT / "tests/test_configs/e2e_samples")
CONNECTION_FILE = (PROMPTFLOW_ROOT / "connections.json").resolve().absolute().as_posix()
ENV_FILE = (PROMPTFLOW_ROOT / ".env").resolve().absolute().as_posix()

# below constants are used for pfazure and global config tests
DEFAULT_SUBSCRIPTION_ID = "96aede12-2f73-41cb-b983-6d11a904839b"
DEFAULT_RESOURCE_GROUP_NAME = "promptflow"
DEFAULT_WORKSPACE_NAME = "promptflow-eastus"
DEFAULT_COMPUTE_INSTANCE_NAME = "ci-lin-cpu-sp"
DEFAULT_RUNTIME_NAME = "test-runtime-ci"
DEFAULT_REGISTRY_NAME = "promptflow-preview"
