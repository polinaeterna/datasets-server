from typing import List, Union

from datasets import DatasetInfo, Features
from libutils.exceptions import Status400Error

from worker.config import ROWS_MAX_NUMBER
from worker.models.column.audio import AudioColumn
from worker.models.column.bool import BoolColumn
from worker.models.column.class_label import ClassLabelColumn
from worker.models.column.default import (
    Cell,
    CellTypeError,
    Column,
    ColumnDict,
    ColumnInferenceError,
    ColumnType,
    ColumnTypeError,
)
from worker.models.column.float import FloatColumn
from worker.models.column.image import ImageColumn
from worker.models.column.image_array2d import ImageArray2DColumn
from worker.models.column.image_array3d import ImageArray3DColumn
from worker.models.column.image_url import ImageUrlColumn
from worker.models.column.int import IntColumn
from worker.models.column.string import StringColumn
from worker.models.row import Row

column_classes = [
    AudioColumn,
    ClassLabelColumn,
    ImageColumn,
    ImageArray2DColumn,
    ImageArray3DColumn,
    ImageUrlColumn,
    BoolColumn,
    IntColumn,
    FloatColumn,
    StringColumn,
]

FeaturesOrNone = Union[Features, None]

MAX_ROWS_FOR_TYPE_INFERENCE_AND_CHECK = ROWS_MAX_NUMBER


def get_column(column_name: str, features: FeaturesOrNone, rows: List[Row]) -> Column:
    feature = None if features is None else features[column_name]
    try:
        values = [row[column_name] for row in rows[:MAX_ROWS_FOR_TYPE_INFERENCE_AND_CHECK]]
    except KeyError as e:
        raise Status400Error("one column is missing in the dataset rows", e) from e

    # try until one works
    for column_class in column_classes:
        try:
            return column_class(column_name, feature, values)
        except (ColumnTypeError, CellTypeError, ColumnInferenceError):
            pass
    # none has worked
    return Column(column_name, feature, values)


def get_columns(info: DatasetInfo, rows: List[Row]) -> List[Column]:
    if info.features is None:
        if not rows:
            return []
        else:
            column_names = list(rows[0].keys())
    else:
        column_names = list(info.features.keys())
        # check, just in case
        if rows and info.features.keys() != rows[0].keys():
            raise Status400Error("columns from features and first row don't match")
    return [get_column(column_name, info.features, rows) for column_name in column_names]


# explicit re-export
__all__ = ["Column", "Cell", "ColumnType", "ColumnDict", "ClassLabelColumn"]