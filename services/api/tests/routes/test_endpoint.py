# SPDX-License-Identifier: Apache-2.0
# Copyright 2022 The HuggingFace Authors.

from http import HTTPStatus
from unittest.mock import patch

from libapi.exceptions import ResponseNotReadyError
from libcommon.config import ProcessingGraphConfig
from libcommon.processing_graph import ProcessingGraph
from libcommon.queue import Queue
from libcommon.simple_cache import upsert_response
from pytest import raises

from api.config import AppConfig, EndpointConfig
from api.routes.endpoint import EndpointsDefinition, get_cache_entry_from_steps

CACHE_MAX_DAYS = 90


def test_endpoints_definition() -> None:
    config = ProcessingGraphConfig()
    graph = ProcessingGraph(config.specification)
    endpoint_config = EndpointConfig.from_env()

    endpoints_definition = EndpointsDefinition(graph, endpoint_config)
    assert endpoints_definition

    definition = endpoints_definition.steps_by_input_type_and_endpoint
    assert definition

    splits = definition["/splits"]
    assert splits is not None
    assert sorted(list(splits)) == ["config", "dataset"]
    assert splits["dataset"] is not None
    assert splits["config"] is not None
    assert len(splits["dataset"]) == 1  # Has one processing step
    assert len(splits["config"]) == 2  # Has two processing steps

    first_rows = definition["/first-rows"]
    assert first_rows is not None
    assert sorted(list(first_rows)) == ["split"]
    assert first_rows["split"] is not None
    assert len(first_rows["split"]) == 2  # Has two processing steps

    parquet = definition["/parquet"]
    assert parquet is not None
    assert sorted(list(parquet)) == ["config", "dataset"]
    assert parquet["dataset"] is not None
    assert parquet["config"] is not None
    assert len(parquet["dataset"]) == 1  # Only has one processing step
    assert len(parquet["config"]) == 1  # Only has one processing step

    dataset_info = definition["/info"]
    assert dataset_info is not None
    assert sorted(list(dataset_info)) == ["config", "dataset"]
    assert dataset_info["dataset"] is not None
    assert dataset_info["config"] is not None
    assert len(dataset_info["dataset"]) == 1  # Only has one processing step
    assert len(dataset_info["config"]) == 1  # Only has one processing step

    size = definition["/size"]
    assert size is not None
    assert sorted(list(size)) == ["config", "dataset"]
    assert size["dataset"] is not None
    assert size["config"] is not None
    assert len(size["dataset"]) == 1  # Only has one processing step
    assert len(size["config"]) == 1  # Only has one processing step

    opt_in_out_urls = definition["/opt-in-out-urls"]
    assert opt_in_out_urls is not None
    assert sorted(list(opt_in_out_urls)) == ["config", "dataset", "split"]
    assert opt_in_out_urls["split"] is not None
    assert opt_in_out_urls["config"] is not None
    assert opt_in_out_urls["dataset"] is not None
    assert len(opt_in_out_urls["split"]) == 1  # Only has one processing step
    assert len(opt_in_out_urls["config"]) == 1  # Only has one processing step
    assert len(opt_in_out_urls["dataset"]) == 1  # Only has one processing step

    is_valid = definition["/is-valid"]
    assert is_valid is not None
    assert sorted(list(is_valid)) == ["config", "dataset", "split"]
    assert is_valid["dataset"] is not None
    assert is_valid["config"] is not None
    assert is_valid["split"] is not None
    assert len(is_valid["dataset"]) == 1  # Only has one processing step
    assert len(is_valid["config"]) == 1  # Only has one processing step
    assert len(is_valid["split"]) == 1  # Only has one processing step

    # assert old deleted endpoints don't exist
    with raises(KeyError):
        _ = definition["/dataset-info"]
    with raises(KeyError):
        _ = definition["/parquet-and-dataset-info"]
    with raises(KeyError):
        _ = definition["/config-names"]


def test_get_cache_entry_from_steps() -> None:
    dataset = "dataset"
    revision = "revision"
    config = "config"

    app_config = AppConfig.from_env()
    graph_config = ProcessingGraphConfig()
    processing_graph = ProcessingGraph(graph_config.specification)

    cache_with_error = "config-split-names-from-streaming"
    cache_without_error = "config-split-names-from-info"

    step_with_error = processing_graph.get_processing_step(cache_with_error)
    step_without_error = processing_graph.get_processing_step(cache_without_error)

    upsert_response(
        kind=cache_without_error,
        dataset=dataset,
        config=config,
        content={},
        http_status=HTTPStatus.OK,
    )

    upsert_response(
        kind=cache_with_error,
        dataset=dataset,
        config=config,
        content={},
        http_status=HTTPStatus.INTERNAL_SERVER_ERROR,
    )

    # succeeded result is returned
    result = get_cache_entry_from_steps(
        processing_steps=[step_without_error, step_with_error],
        dataset=dataset,
        config=config,
        split=None,
        processing_graph=processing_graph,
        hf_endpoint=app_config.common.hf_endpoint,
        cache_max_days=CACHE_MAX_DAYS,
    )
    assert result
    assert result["http_status"] == HTTPStatus.OK

    # succeeded result is returned even if first step failed
    result = get_cache_entry_from_steps(
        processing_steps=[step_with_error, step_without_error],
        dataset=dataset,
        config=config,
        split=None,
        processing_graph=processing_graph,
        hf_endpoint=app_config.common.hf_endpoint,
        cache_max_days=CACHE_MAX_DAYS,
    )
    assert result
    assert result["http_status"] == HTTPStatus.OK

    # error result is returned if all steps failed
    result = get_cache_entry_from_steps(
        processing_steps=[step_with_error, step_with_error],
        dataset=dataset,
        config=config,
        split=None,
        processing_graph=processing_graph,
        hf_endpoint=app_config.common.hf_endpoint,
        cache_max_days=CACHE_MAX_DAYS,
    )
    assert result
    assert result["http_status"] == HTTPStatus.INTERNAL_SERVER_ERROR

    # pending job throws exception
    queue = Queue()
    queue.add_job(job_type="dataset-split-names", dataset=dataset, revision=revision, config=config, difficulty=50)
    non_existent_step = processing_graph.get_processing_step("dataset-split-names")
    with patch("libcommon.dataset.get_dataset_git_revision", return_value=revision):
        # ^ the dataset does not exist on the Hub, we don't want to raise an issue here
        with raises(ResponseNotReadyError):
            get_cache_entry_from_steps(
                processing_steps=[non_existent_step],
                dataset=dataset,
                config=None,
                split=None,
                processing_graph=processing_graph,
                hf_endpoint=app_config.common.hf_endpoint,
                cache_max_days=CACHE_MAX_DAYS,
            )
