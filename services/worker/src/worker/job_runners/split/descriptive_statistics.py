# SPDX-License-Identifier: Apache-2.0
# Copyright 2023 The HuggingFace Authors.
import enum
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple, TypedDict, Union

import duckdb
import numpy as np
import pandas as pd
from libcommon.constants import PROCESSING_STEP_SPLIT_DESCRIPTIVE_STATISTICS_VERSION
from libcommon.exceptions import (
    CacheDirectoryNotInitializedError,
    NoSupportedFeaturesError,
    ParquetResponseEmptyError,
    PreviousStepFormatError,
    SplitWithTooBigParquetError,
    StatisticsComputationError,
)
from libcommon.processing_graph import ProcessingStep
from libcommon.simple_cache import get_previous_step_or_raise
from libcommon.storage import StrPath
from libcommon.utils import JobInfo
from tqdm import tqdm

from worker.config import AppConfig, DescriptiveStatisticsConfig
from worker.dtos import CompleteJobResult
from worker.job_runners.split.split_job_runner import SplitJobRunnerWithCache
from worker.utils import check_split_exists

PARQUET_FILENAME = "dataset.parquet"

DECIMALS = 5

INTEGER_DTYPES = ["int8", "int16", "int32", "int64", "uint8", "uint16", "uint32", "uint64"]
FLOAT_DTYPES = ["float16", "float32", "float64"]
NUMERICAL_DTYPES = INTEGER_DTYPES + FLOAT_DTYPES


COPY_PARQUET_DATA_COMMAND = """
COPY (SELECT * FROM read_parquet({parquet_files_urls})) TO '{local_parquet_path}' (FORMAT PARQUET);
"""
COMPUTE_NAN_COUNTS_COMMAND = """
    SELECT COUNT(*) FROM read_parquet('{parquet_filename}') WHERE {column_name} IS NULL;
"""
COMPUTE_CATEGORIES_COUNTS_COMMAND = """
    SELECT {column_name}, COUNT(*) FROM read_parquet('{parquet_filename}') GROUP BY {column_name};
"""
COMPUTE_MIN_MAX_MEAN_MEDIAN_STD_COMMAND = """
    SELECT min({column_name}), max({column_name}), mean({column_name}),
    median({column_name}), stddev_samp({column_name}) FROM read_parquet('{parquet_filename}');
"""
COMPUTE_HIST_COMMAND = """
    SELECT bin_id, COUNT(*) as count FROM read_parquet('{parquet_filename}')
        JOIN bins ON ({column_name} >= bin_min AND {column_name} < bin_max) GROUP BY bin_id;
"""  # `bins` is the name of preinjected table with bin edges data


class ColumnType(str, enum.Enum):
    FLOAT = "float"
    INT = "int"
    CLASS_LABEL = "class_label"


class Histogram(TypedDict):
    hist: List[int]
    bin_edges: List[float]


class NumericalStatisticsItem(TypedDict):
    nan_count: int
    nan_proportion: float
    min: float
    max: float
    mean: float
    median: float
    std: float
    histogram: Histogram


class CategoricalStatisticsItem(TypedDict):
    nan_count: int
    nan_proportion: float
    n_unique: int
    frequencies: Dict[str, int]


class StatisticsPerColumnItem(TypedDict):
    column_name: str
    column_type: ColumnType
    column_statistics: Union[NumericalStatisticsItem, CategoricalStatisticsItem]


class SplitDescriptiveStatisticsResponse(TypedDict):
    num_examples: int
    statistics: List[StatisticsPerColumnItem]


def generate_bins(
    min_value: Union[int, float],
    max_value: Union[int, float],
    column_type: ColumnType,
    n_bins: int,
) -> pd.DataFrame:
    """
    Returns:
        pandas.DataFrame with bin edges to insert into database to perform histogram computation with duckdb
    """
    if column_type is ColumnType.FLOAT:
        bin_size = (max_value - min_value) / n_bins
        bin_edges = np.arange(min_value, max_value, bin_size).astype(float).tolist()
        if len(bin_edges) != n_bins:
            raise StatisticsComputationError(
                f"Incorrect number of bins generated, expected {n_bins}, got {len(bin_edges)}."
            )
    elif column_type is ColumnType.INT:
        bin_size = np.ceil((max_value - min_value + 1) / n_bins)
        bin_edges = np.arange(min_value, max_value + 1, bin_size).astype(int).tolist()
        if len(bin_edges) > n_bins:
            raise StatisticsComputationError(
                f"Incorrect number of bins generated, expected {n_bins}, got {len(bin_edges)}."
            )
    else:
        raise ValueError(f"Incorrect column type {column_type}. ")
    bin_max_edges = bin_edges[1:] + [max_value + 1]  # add 1 to include exact max values in the last bin
    return pd.DataFrame.from_dict(
        {"bin_id": list(range(len(bin_edges))), "bin_min": bin_edges, "bin_max": bin_max_edges}
    )


def compute_histogram(
    con: duckdb.DuckDBPyConnection,
    column_name: str,
    parquet_filename: Path,
    column_type: ColumnType,
    min_value: Union[int, float],
    max_value: Union[int, float],
    n_bins: int,
    n_samples: Optional[int] = None,
) -> Histogram:
    bins_df = generate_bins(min_value=min_value, max_value=max_value, column_type=column_type, n_bins=n_bins)
    n_bins = bins_df.shape[0]
    con.sql("CREATE OR REPLACE TEMPORARY TABLE bins AS SELECT * from bins_df")
    compute_hist_command = COMPUTE_HIST_COMMAND.format(parquet_filename=parquet_filename, column_name=column_name)
    logging.debug(f"Compute histogram for {column_name}")
    # query returns list of tuples (bin_id, bin_max, n_count):
    hist_query_result = dict(con.sql(compute_hist_command).fetchall())  # dict bin_id -> n_samples
    if len(hist_query_result) > n_bins + 1:
        raise StatisticsComputationError(
            "Got unexpected result during histogram computation: returned more bins than requested. "
            f"{n_bins=} {hist_query_result=}. "
        )
    hist = []
    for bin_idx in range(n_bins):
        # no key in query result = no examples in this range, so we put 0
        hist.append(hist_query_result.get(bin_idx, 0))
    if n_samples and sum(hist) != n_samples:
        raise StatisticsComputationError(
            "Got unexpected result during histogram computation: histogram sum and number of non-null samples don't"
            f" match. histogram sum={sum(hist)}, {n_samples=}"
        )
    bins = bins_df["bin_min"].round(DECIMALS).tolist()
    bins = bins + [np.round(max_value, DECIMALS).item()]  # put exact max value back to bins
    return Histogram(hist=hist, bin_edges=bins)


def compute_numerical_statistics(
    con: duckdb.DuckDBPyConnection,
    column_name: str,
    parquet_filename: Path,
    n_bins: int,
    n_samples: int,
    column_type: ColumnType,
) -> NumericalStatisticsItem:
    logging.debug(f"Compute min, max, mean, median, std and proportion of null values for {column_name}")
    min_max_mean_median_std_command = COMPUTE_MIN_MAX_MEAN_MEDIAN_STD_COMMAND.format(
        column_name=column_name, parquet_filename=parquet_filename
    )
    minimum, maximum, mean, median, std = con.sql(min_max_mean_median_std_command).fetchall()[0]
    logging.debug(f"{minimum=}, {maximum=}, {mean=}, {median=}, {std=}")

    nan_count_command = COMPUTE_NAN_COUNTS_COMMAND.format(column_name=column_name, parquet_filename=parquet_filename)
    nan_count = con.sql(nan_count_command).fetchall()[0][0]
    nan_proportion = np.round(nan_count / n_samples, DECIMALS).item() if nan_count else 0.0
    logging.debug(f"{nan_count=} {nan_proportion=}")
    histogram = compute_histogram(
        con,
        column_name,
        parquet_filename,
        min_value=minimum,
        max_value=maximum,
        column_type=column_type,
        n_bins=n_bins,
        n_samples=n_samples - nan_count,
    )
    if column_type == ColumnType.FLOAT:
        minimum, maximum, mean, median, std = np.round([minimum, maximum, mean, median, std], DECIMALS).tolist()
    elif column_type == ColumnType.INT:
        mean, median, std = np.round([mean, median, std], DECIMALS).tolist()
    else:
        raise ValueError(f"Incorrect column type {column_type}")
    return NumericalStatisticsItem(
        nan_count=nan_count,
        nan_proportion=nan_proportion,
        min=minimum,
        max=maximum,
        mean=mean,
        median=median,
        std=std,
        histogram=histogram,
    )


def compute_categorical_statistics(
    con: duckdb.DuckDBPyConnection,
    column_name: str,
    parquet_filename: Path,
    class_label_names: List[str],
    n_samples: int,
) -> CategoricalStatisticsItem:
    categorical_counts_query = COMPUTE_CATEGORIES_COUNTS_COMMAND.format(
        column_name=column_name, parquet_filename=parquet_filename
    )
    categories: List[Tuple[int, int]] = con.sql(
        categorical_counts_query
    ).fetchall()  # list of tuples (idx, num_samples)

    logging.debug(f"Statistics for {column_name} computed")
    frequencies, nan_count = {}, 0
    for cat_id, freq in categories:
        if cat_id is not None:
            frequencies[class_label_names[cat_id]] = freq
        else:
            nan_count = freq
    nan_proportion = np.round(nan_count / n_samples, DECIMALS).item() if nan_count != 0 else 0.0
    return CategoricalStatisticsItem(
        nan_count=nan_count,
        nan_proportion=nan_proportion,
        n_unique=len(categories) - 1 if nan_count else len(categories),
        frequencies=frequencies,
    )


def compute_descriptive_statistics_response(
    dataset: str,
    config: str,
    split: str,
    local_parquet_directory: Path,
    histogram_num_bins: int,
    max_parquet_size_bytes: int,
) -> SplitDescriptiveStatisticsResponse:
    logging.info(f"Compute descriptive statistics for {dataset=}, {config=}, {split=}")
    check_split_exists(dataset=dataset, config=config, split=split)

    config_parquet_and_info_step = "config-parquet-and-info"
    parquet_and_info_best_response = get_previous_step_or_raise(
        kinds=[config_parquet_and_info_step],
        dataset=dataset,
        config=config,
    )
    content_parquet_and_info = parquet_and_info_best_response.response["content"]
    try:
        split_parquet_files = [
            parquet_file
            for parquet_file in content_parquet_and_info["parquet_files"]
            if parquet_file["config"] == config and parquet_file["split"] == split
        ]
        dataset_info = content_parquet_and_info["dataset_info"]
    except KeyError as e:
        raise PreviousStepFormatError(
            (
                f"Previous step '{config_parquet_and_info_step}' did not return the expected content: "
                "'parquet_files' or 'dataset_info'. "
            ),
            e,
        ) from e

    if not split_parquet_files:
        raise ParquetResponseEmptyError("No parquet files found.")
    features = dataset_info.get("features")
    if not features:
        raise PreviousStepFormatError(
            f"Previous step '{config_parquet_and_info_step}' did not return the expected content: "
            "no features found in 'dataset_info'. "
        )

    split_parquets_size = sum(parquet_file["size"] for parquet_file in split_parquet_files)
    if split_parquets_size > max_parquet_size_bytes:
        raise SplitWithTooBigParquetError(
            f"Statistics computation is limited to split parquets under {max_parquet_size_bytes} bytes. "
            f"Current size of sum of split parquets is {split_parquets_size} bytes."
        )
    parquet_files_urls = [parquet_file["url"] for parquet_file in split_parquet_files]

    stats: List[StatisticsPerColumnItem] = []
    num_examples = dataset_info["splits"][split]["num_examples"]
    categorical_features = {
        feature_name: feature for feature_name, feature in features.items() if feature.get("_type") == "ClassLabel"
    }
    numerical_features = {
        feature_name: feature
        for feature_name, feature in features.items()
        if feature.get("_type") == "Value" and feature.get("dtype") in NUMERICAL_DTYPES
    }
    if not categorical_features and not numerical_features:
        raise NoSupportedFeaturesError(
            "No columns for statistics computation found. Currently supported feature types are: "
            f"{NUMERICAL_DTYPES} and ClassLabel. "
        )

    con = duckdb.connect(":memory:")  # we don't load data in local db file, use local parquet file instead
    # configure duckdb extensions
    con.sql(f"SET extension_directory='{local_parquet_directory}';")
    con.sql("INSTALL httpfs")
    con.sql("LOAD httpfs")
    con.sql("SET enable_progress_bar=true;")

    # store data as local parquet file for fast querying
    local_parquet_path = (
        Path(local_parquet_directory) / PARQUET_FILENAME if local_parquet_directory else Path(PARQUET_FILENAME)
    )
    logging.info(f"Downloading remote data to a local parquet file {local_parquet_path}. ")
    con.sql(
        COPY_PARQUET_DATA_COMMAND.format(parquet_files_urls=parquet_files_urls, local_parquet_path=local_parquet_path)
    )

    # compute for ClassLabels (we are sure that these are discrete categories)
    if categorical_features:
        logging.info(f"Compute statistics for categorical columns {categorical_features}")
    for feature_name, feature in tqdm(categorical_features.items()):
        logging.debug(f"Compute statistics for ClassLabel feature {feature_name}")
        class_label_names = feature["names"]
        cat_column_stats: CategoricalStatisticsItem = compute_categorical_statistics(
            con,
            feature_name,
            class_label_names=class_label_names,
            n_samples=num_examples,
            parquet_filename=local_parquet_path,
        )
        stats.append(
            StatisticsPerColumnItem(
                column_name=feature_name,
                column_type=ColumnType.CLASS_LABEL,
                column_statistics=cat_column_stats,
            )
        )

    if numerical_features:
        logging.info(f"Compute min, max, mean, median, std, histogram for numerical columns {numerical_features}. ")
    for feature_name, feature in tqdm(numerical_features.items()):
        column_type = ColumnType.FLOAT if feature["dtype"] in FLOAT_DTYPES else ColumnType.INT
        num_column_stats: NumericalStatisticsItem = compute_numerical_statistics(
            con,
            feature_name,
            parquet_filename=local_parquet_path,
            n_bins=histogram_num_bins,
            n_samples=num_examples,
            column_type=column_type,
        )
        stats.append(
            StatisticsPerColumnItem(
                column_name=feature_name,
                column_type=column_type,
                column_statistics=num_column_stats,
            )
        )
    con.close()

    return SplitDescriptiveStatisticsResponse(
        num_examples=num_examples, statistics=sorted(stats, key=lambda x: x["column_name"])
    )


class SplitDescriptiveStatisticsJobRunner(SplitJobRunnerWithCache):
    descriptive_statistics_config: DescriptiveStatisticsConfig

    def __init__(
        self,
        job_info: JobInfo,
        app_config: AppConfig,
        processing_step: ProcessingStep,
        statistics_cache_directory: StrPath,
    ):
        super().__init__(
            job_info=job_info,
            app_config=app_config,
            processing_step=processing_step,
            cache_directory=Path(statistics_cache_directory),
        )
        self.descriptive_statistics_config = app_config.descriptive_statistics

    @staticmethod
    def get_job_type() -> str:
        return "split-descriptive-statistics"

    @staticmethod
    def get_job_runner_version() -> int:
        return PROCESSING_STEP_SPLIT_DESCRIPTIVE_STATISTICS_VERSION

    def compute(self) -> CompleteJobResult:
        if self.cache_subdirectory is None:
            raise CacheDirectoryNotInitializedError("Cache directory has not been initialized.")
        return CompleteJobResult(
            compute_descriptive_statistics_response(
                dataset=self.dataset,
                config=self.config,
                split=self.split,
                local_parquet_directory=self.cache_subdirectory,
                histogram_num_bins=self.descriptive_statistics_config.histogram_num_bins,
                max_parquet_size_bytes=self.descriptive_statistics_config.max_parquet_size_bytes,
            )
        )
