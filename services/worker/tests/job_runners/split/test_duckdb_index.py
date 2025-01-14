# SPDX-License-Identifier: Apache-2.0
# Copyright 2023 The HuggingFace Authors.

import os
from dataclasses import replace
from http import HTTPStatus
from typing import Callable, Optional

import duckdb
import pytest
import requests
from libcommon.processing_graph import ProcessingGraph
from libcommon.resources import CacheMongoResource, QueueMongoResource
from libcommon.simple_cache import upsert_response
from libcommon.storage import StrPath
from libcommon.utils import Priority

from worker.config import AppConfig
from worker.job_runners.config.parquet_and_info import ConfigParquetAndInfoJobRunner
from worker.job_runners.split.duckdb_index import SplitDuckDbIndexJobRunner
from worker.resources import LibrariesResource

from ...fixtures.hub import HubDatasetTest

GetJobRunner = Callable[[str, str, str, AppConfig], SplitDuckDbIndexJobRunner]

GetParquetJobRunner = Callable[[str, str, AppConfig], ConfigParquetAndInfoJobRunner]


@pytest.fixture
def get_job_runner(
    duckdb_index_cache_directory: StrPath,
    cache_mongo_resource: CacheMongoResource,
    queue_mongo_resource: QueueMongoResource,
) -> GetJobRunner:
    def _get_job_runner(
        dataset: str,
        config: str,
        split: str,
        app_config: AppConfig,
    ) -> SplitDuckDbIndexJobRunner:
        processing_step_name = SplitDuckDbIndexJobRunner.get_job_type()
        processing_graph = ProcessingGraph(
            {
                "dataset-step": {"input_type": "dataset"},
                "config-parquet": {
                    "input_type": "config",
                    "triggered_by": "dataset-step",
                    "provides_config_parquet": True,
                },
                "config-split-names-from-streaming": {
                    "input_type": "config",
                    "triggered_by": "dataset-step",
                },
                processing_step_name: {
                    "input_type": "dataset",
                    "job_runner_version": SplitDuckDbIndexJobRunner.get_job_runner_version(),
                    "triggered_by": ["config-parquet", "config-split-names-from-streaming"],
                },
            }
        )
        return SplitDuckDbIndexJobRunner(
            job_info={
                "type": SplitDuckDbIndexJobRunner.get_job_type(),
                "params": {
                    "dataset": dataset,
                    "revision": "revision",
                    "config": config,
                    "split": split,
                },
                "job_id": "job_id",
                "priority": Priority.NORMAL,
                "difficulty": 50,
            },
            app_config=app_config,
            processing_step=processing_graph.get_processing_step(processing_step_name),
            duckdb_index_cache_directory=duckdb_index_cache_directory,
        )

    return _get_job_runner


@pytest.fixture
def get_parquet_job_runner(
    libraries_resource: LibrariesResource,
    cache_mongo_resource: CacheMongoResource,
    queue_mongo_resource: QueueMongoResource,
) -> GetParquetJobRunner:
    def _get_job_runner(
        dataset: str,
        config: str,
        app_config: AppConfig,
    ) -> ConfigParquetAndInfoJobRunner:
        processing_step_name = ConfigParquetAndInfoJobRunner.get_job_type()
        processing_graph = ProcessingGraph(
            {
                "dataset-level": {"input_type": "dataset"},
                processing_step_name: {
                    "input_type": "config",
                    "job_runner_version": ConfigParquetAndInfoJobRunner.get_job_runner_version(),
                    "triggered_by": "dataset-level",
                },
            }
        )
        return ConfigParquetAndInfoJobRunner(
            job_info={
                "type": ConfigParquetAndInfoJobRunner.get_job_type(),
                "params": {
                    "dataset": dataset,
                    "revision": "revision",
                    "config": config,
                    "split": None,
                },
                "job_id": "job_id",
                "priority": Priority.NORMAL,
                "difficulty": 50,
            },
            app_config=app_config,
            processing_step=processing_graph.get_processing_step(processing_step_name),
            hf_datasets_cache=libraries_resource.hf_datasets_cache,
        )

    return _get_job_runner


@pytest.mark.parametrize(
    "hub_dataset_name,max_parquet_size_bytes,expected_error_code",
    [
        ("duckdb_index", None, None),
        ("duckdb_index", 1_000, "SplitWithTooBigParquetError"),  # parquet size is 2812
        ("public", None, "NoIndexableColumnsError"),  # dataset does not have string columns to index
    ],
)
def test_compute(
    get_parquet_job_runner: GetParquetJobRunner,
    get_job_runner: GetJobRunner,
    app_config: AppConfig,
    hub_responses_public: HubDatasetTest,
    hub_responses_duckdb_index: HubDatasetTest,
    hub_dataset_name: str,
    max_parquet_size_bytes: Optional[int],
    expected_error_code: str,
) -> None:
    hub_datasets = {"public": hub_responses_public, "duckdb_index": hub_responses_duckdb_index}
    dataset = hub_datasets[hub_dataset_name]["name"]
    config_names = hub_datasets[hub_dataset_name]["config_names_response"]
    config = hub_datasets[hub_dataset_name]["config_names_response"]["config_names"][0]["config"]
    splits_response = hub_datasets[hub_dataset_name]["splits_response"]
    split = "train"

    upsert_response(
        "dataset-config-names",
        dataset=dataset,
        http_status=HTTPStatus.OK,
        content=config_names,
    )

    upsert_response(
        "config-split-names-from-streaming",
        dataset=dataset,
        config=config,
        http_status=HTTPStatus.OK,
        content=splits_response,
    )

    app_config = (
        app_config
        if max_parquet_size_bytes is None
        else replace(
            app_config, duckdb_index=replace(app_config.duckdb_index, max_parquet_size_bytes=max_parquet_size_bytes)
        )
    )

    parquet_job_runner = get_parquet_job_runner(dataset, config, app_config)
    parquet_response = parquet_job_runner.compute()
    config_parquet = parquet_response.content

    # simulate more than one parquet file to index
    extra_parquet_file = config_parquet["parquet_files"][0]
    config_parquet["parquet_files"].append(extra_parquet_file)

    upsert_response(
        "config-parquet-and-info",
        dataset=dataset,
        config=config,
        http_status=HTTPStatus.OK,
        content=config_parquet,
    )

    assert parquet_response
    job_runner = get_job_runner(dataset, config, split, app_config)
    job_runner.pre_compute()

    if expected_error_code:
        with pytest.raises(Exception) as e:
            job_runner.compute()
        assert e.typename == expected_error_code
    else:
        job_runner.pre_compute()
        response = job_runner.compute()
        assert response
        content = response.content
        url = content["url"]
        file_name = content["filename"]
        assert url is not None
        assert file_name is not None
        job_runner.post_compute()

        # download locally duckdb index file
        duckdb_file = requests.get(url)
        with open(file_name, "wb") as f:
            f.write(duckdb_file.content)

        duckdb.execute("INSTALL 'fts';")
        duckdb.execute("LOAD 'fts';")
        con = duckdb.connect(file_name)

        # validate number of inserted records
        record_count = con.sql("SELECT COUNT(*) FROM data;").fetchall()
        assert record_count is not None
        assert isinstance(record_count, list)
        assert record_count[0] == (10,)  # dataset has 5 rows but since parquet file was duplicate it is 10

        # perform a search to validate fts feature
        query = "Lord Vader"
        result = con.execute(
            "SELECT __hf_index_id, text FROM data WHERE fts_main_data.match_bm25(__hf_index_id, ?) IS NOT NULL;",
            [query],
        )
        rows = result.df()
        assert rows is not None
        assert (rows["text"].eq("Vader turns round and round in circles as his ship spins into space.")).any()
        assert (rows["text"].eq("The wingman spots the pirateship coming at him and warns the Dark Lord")).any()
        assert (rows["text"].eq("We count thirty Rebel ships, Lord Vader.")).any()
        assert (
            rows["text"].eq(
                "Grand Moff Tarkin and Lord Vader are interrupted in their discussion by the buzz of the comlink"
            )
        ).any()
        assert not (rows["text"].eq("There goes another one.")).any()
        assert (rows["__hf_index_id"].isin([0, 2, 3, 4, 5, 7, 8, 9])).all()

        con.close()
        os.remove(file_name)
    job_runner.post_compute()
