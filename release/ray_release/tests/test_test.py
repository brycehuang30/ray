import asyncio
import json
import sys
import os
import platform
from unittest import mock

import aioboto3
import boto3
import pytest
from unittest.mock import patch, AsyncMock

from ray_release.bazel import bazel_runfile
from ray_release.configs.global_config import (
    init_global_config,
    get_global_config,
)
from ray_release.test import (
    Test,
    TestResult,
    TestState,
    TestType,
    ResultStatus,
    _convert_env_list_to_dict,
    DATAPLANE_ECR_REPO,
    DATAPLANE_ECR_ML_REPO,
    MACOS_TEST_PREFIX,
    LINUX_TEST_PREFIX,
    WINDOWS_TEST_PREFIX,
    MACOS_BISECT_DAILY_RATE_LIMIT,
)


init_global_config(bazel_runfile("release/ray_release/configs/oss_config.yaml"))


def _stub_test(val: dict) -> Test:
    test = Test(
        {
            "name": "test",
            "cluster": {},
        }
    )
    test.update(val)
    return test


def _stub_test_result(status: ResultStatus) -> TestResult:
    return TestResult(
        status=status.value,
        commit="1234567890",
        branch="master",
        url="url",
        timestamp=0,
        pull_request="1",
        rayci_step_id="123",
    )


def test_is_byod_cluster():
    assert not _stub_test({}).is_byod_cluster()
    assert _stub_test({"cluster": {"byod": {}}}).is_byod_cluster()
    assert _stub_test({"cluster": {"byod": {"type": "gpu"}}}).is_byod_cluster()
    with mock.patch.dict(os.environ, {"BUILDKITE_PULL_REQUEST": "1"}):
        assert _stub_test({"cluster": {"byod": {}}}).is_byod_cluster()
    with mock.patch.dict(os.environ, {"BUILDKITE_PULL_REQUEST": "false"}):
        assert _stub_test({"cluster": {"byod": {}}}).is_byod_cluster()


def test_convert_env_list_to_dict():
    with mock.patch.dict(os.environ, {"ENV": "env"}):
        assert _convert_env_list_to_dict(["a=b", "c=d=e", "ENV"]) == {
            "a": "b",
            "c": "d=e",
            "ENV": "env",
        }


def test_get_python_version():
    assert _stub_test({}).get_python_version() == "3.9"
    assert _stub_test({"python": "3.11"}).get_python_version() == "3.11"


def test_get_ray_image():
    os.environ["BUILDKITE_BRANCH"] = "master"
    os.environ["BUILDKITE_COMMIT"] = "1234567890"
    assert (
        _stub_test(
            {
                "python": "3.9",
                "cluster": {"byod": {}},
            }
        ).get_ray_image()
        == "rayproject/ray:123456-py39-cpu"
    )
    assert (
        _stub_test(
            {
                "python": "3.9",
                "cluster": {
                    "byod": {
                        "type": "gpu",
                    }
                },
            }
        ).get_ray_image()
        == "rayproject/ray-ml:123456-py39-gpu"
    )
    os.environ["BUILDKITE_BRANCH"] = "releases/1.0.0"
    assert (
        _stub_test({"cluster": {"byod": {}}}).get_ray_image()
        == "rayproject/ray:1.0.0.123456-py39-cpu"
    )
    with mock.patch.dict(os.environ, {"BUILDKITE_PULL_REQUEST": "123"}):
        assert (
            _stub_test({"cluster": {"byod": {}}}).get_ray_image()
            == "rayproject/ray:pr-123.123456-py39-cpu"
        )
    with mock.patch.dict(os.environ, {"RAY_IMAGE_TAG": "my_tag"}):
        assert (
            _stub_test({"cluster": {"byod": {}}}).get_ray_image()
            == "rayproject/ray:my_tag"
        )


def test_get_anyscale_byod_image():
    os.environ["BUILDKITE_BRANCH"] = "master"
    os.environ["BUILDKITE_COMMIT"] = "1234567890"
    assert (
        _stub_test({"python": "3.7", "cluster": {"byod": {}}}).get_anyscale_byod_image()
        == f"{get_global_config()['byod_ecr']}/{DATAPLANE_ECR_REPO}:123456-py37-cpu"
    )
    assert _stub_test(
        {
            "python": "3.8",
            "cluster": {
                "byod": {
                    "type": "gpu",
                }
            },
        }
    ).get_anyscale_byod_image() == (
        f"{get_global_config()['byod_ecr']}/" f"{DATAPLANE_ECR_ML_REPO}:123456-py38-gpu"
    )
    assert _stub_test(
        {
            "python": "3.8",
            "cluster": {
                "byod": {
                    "type": "gpu",
                    "post_build_script": "foo.sh",
                }
            },
        }
    ).get_anyscale_byod_image() == (
        f"{get_global_config()['byod_ecr']}"
        f"/{DATAPLANE_ECR_ML_REPO}:123456-py38-gpu-"
        "ab7ed2b7a7e8d3f855a7925b0d296b0f9c75fac91882aba47854d92d27e13e53"
    )


@patch("github.Repository")
@patch("github.Issue")
def test_is_jailed_with_open_issue(mock_repo, mock_issue) -> None:
    assert not Test(state="passing").is_jailed_with_open_issue(mock_repo)
    mock_repo.get_issue.return_value = mock_issue
    mock_issue.state = "open"
    assert Test(state="jailed", github_issue_number="1").is_jailed_with_open_issue(
        mock_repo
    )
    mock_issue.state = "closed"
    assert not Test(state="jailed", github_issue_number="1").is_jailed_with_open_issue(
        mock_repo
    )


def test_is_stable() -> None:
    assert Test().is_stable()
    assert Test(stable=True).is_stable()
    assert not Test(stable=False).is_stable()


@patch.dict(
    os.environ,
    {
        "BUILDKITE_BRANCH": "food",
        "BUILDKITE_PULL_REQUEST": "1",
        "RAYCI_STEP_ID": "g4_s5",
    },
)
def test_result_from_bazel_event() -> None:
    result = TestResult.from_bazel_event(
        {
            "testResult": {"status": "PASSED"},
        }
    )
    assert result.is_passing()
    assert result.branch == "food"
    assert result.pull_request == "1"
    assert result.rayci_step_id == "g4_s5"
    result = TestResult.from_bazel_event(
        {
            "testResult": {"status": "FAILED"},
        }
    )
    assert result.is_failing()


def test_from_bazel_event() -> None:
    test = Test.from_bazel_event(
        {
            "id": {"testResult": {"label": "//ray/ci:test"}},
        },
        "ci",
    )
    assert test.get_name() == f"{platform.system().lower()}://ray/ci:test"
    assert test.get_oncall() == "ci"


@patch.object(boto3, "client")
@patch.dict(
    os.environ,
    {"BUILDKITE_PIPELINE_ID": get_global_config()["ci_pipeline_postmerge"][0]},
)
def test_update_from_s3(mock_client) -> None:
    mock_object = mock.Mock()
    mock_object.return_value.get.return_value.read.return_value = json.dumps(
        {
            "state": "failing",
            "team": "core",
            "github_issue_number": "1234",
        }
    ).encode("utf-8")
    mock_client.return_value.get_object = mock_object
    test = _stub_test({"team": "ci"})
    test.update_from_s3()
    assert test.get_state() == TestState.FAILING
    assert test.get_oncall() == "ci"
    assert test["github_issue_number"] == "1234"


@patch("ray_release.test.Test._get_s3_name")
@patch("ray_release.test.Test.gen_from_s3")
def test_gen_from_name(mock_gen_from_s3, _) -> None:
    mock_gen_from_s3.return_value = [
        _stub_test({"name": "a"}),
        _stub_test({"name": "good"}),
        _stub_test({"name": "test"}),
    ]

    assert Test.gen_from_name("good").get_name() == "good"


def test_get_test_type() -> None:
    assert (
        _stub_test({"name": f"{LINUX_TEST_PREFIX}_test"}).get_test_type()
        == TestType.LINUX_TEST
    )
    assert (
        _stub_test({"name": f"{MACOS_TEST_PREFIX}_test"}).get_test_type()
        == TestType.MACOS_TEST
    )
    assert (
        _stub_test({"name": f"{WINDOWS_TEST_PREFIX}_test"}).get_test_type()
        == TestType.WINDOWS_TEST
    )
    assert _stub_test({"name": "release_test"}).get_test_type() == TestType.RELEASE_TEST


def test_get_bisect_daily_rate_limit() -> None:
    assert (
        _stub_test({"name": f"{MACOS_TEST_PREFIX}_test"}).get_bisect_daily_rate_limit()
    ) == MACOS_BISECT_DAILY_RATE_LIMIT


def test_get_s3_name() -> None:
    assert Test._get_s3_name("linux://python/ray/test") == "linux:__python_ray_test"


def test_is_high_impact() -> None:
    assert _stub_test(
        {"name": "test", Test.KEY_IS_HIGH_IMPACT: "true"}
    ).is_high_impact()
    assert not _stub_test(
        {"name": "test", Test.KEY_IS_HIGH_IMPACT: "false"}
    ).is_high_impact()
    assert not _stub_test({"name": "test"}).is_high_impact()


@patch("ray_release.test.Test._gen_test_result")
def test_gen_test_results(mock_gen_test_result) -> None:
    def _mock_gen_test_result(
        client: aioboto3.Session.client,
        bucket: str,
        key: str,
    ) -> TestResult:
        return (
            _stub_test_result(ResultStatus.SUCCESS)
            if key == "good"
            else _stub_test_result(ResultStatus.ERROR)
        )

    mock_gen_test_result.side_effect = AsyncMock(side_effect=_mock_gen_test_result)

    results = asyncio.run(
        _stub_test({})._gen_test_results(
            bucket="bucket",
            keys=["good", "bad", "bad", "good"],
        )
    )
    assert [result.status for result in results] == [
        ResultStatus.SUCCESS.value,
        ResultStatus.ERROR.value,
        ResultStatus.ERROR.value,
        ResultStatus.SUCCESS.value,
    ]


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
