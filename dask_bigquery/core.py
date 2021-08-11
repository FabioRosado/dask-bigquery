from __future__ import annotations

import logging
from collections.abc import Iterable
from contextlib import contextmanager
from functools import partial

import dask
import dask.dataframe as dd
import pandas as pd
import pyarrow
from google.cloud import bigquery, bigquery_storage


@contextmanager
def bigquery_client(project_id=None, with_storage_api=False):
    """This context manager is a temporary solution until there is an
    upstream solution to handle this.
    See  googleapis/google-cloud-python#9457
    and googleapis/gapic-generator-python#575 for reference.
    """

    bq_storage_client = None
    bq_client = bigquery.Client(project_id)
    try:
        if with_storage_api:
            bq_storage_client = bigquery_storage.BigQueryReadClient(
                credentials=bq_client._credentials
            )
            yield bq_client, bq_storage_client
        else:
            yield bq_client
    finally:
        bq_client.close()


def _stream_to_dfs(bqs_client, stream_name, schema, timeout):
    """Given a Storage API client and a stream name, yield all dataframes."""
    return [
        pyarrow.ipc.read_record_batch(
            pyarrow.py_buffer(message.arrow_record_batch.serialized_record_batch),
            schema,
        ).to_pandas()
        for message in bqs_client.read_rows(name=stream_name, offset=0, timeout=timeout)
    ]


@dask.delayed
def _read_rows_arrow(
    *,
    make_create_read_session_request: callable,
    partition_field: str = None,
    project_id: str,
    stream_name: str = None,
    timeout: int,
) -> pd.DataFrame:
    """Read a single batch of rows via BQ Storage API, in Arrow binary format.
    Args:
        project_id: BigQuery project
        create_read_session_request: kwargs to pass to `bqs_client.create_read_session`
        as `request`
        partition_field: BigQuery field for partitions, to be used as Dask index col for
        divisions
            NOTE: Please set if specifying `row_restriction` filters in TableReadOptions.
        stream_name: BigQuery Storage API Stream "name".
            NOTE: Please set if reading from Storage API without any `row_restriction`.
            https://cloud.google.com/bigquery/docs/reference/storage/rpc/google.cloud.bigquery.storage.v1beta1#stream
    NOTE: `partition_field` and `stream_name` kwargs are mutually exclusive.
    Adapted from
    https://github.com/googleapis/python-bigquery-storage/blob/a0fc0af5b4447ce8b50c365d4d081b9443b8490e/google/cloud/bigquery_storage_v1/reader.py.
    """
    with bigquery_client(project_id, with_storage_api=True) as (bq_client, bqs_client):
        session = bqs_client.create_read_session(make_create_read_session_request())
        schema = pyarrow.ipc.read_schema(
            pyarrow.py_buffer(session.arrow_schema.serialized_schema)
        )

        if (partition_field is not None) and (stream_name is not None):
            raise ValueError(
                "The kwargs `partition_field` and `stream_name` are mutually exclusive."
            )

        elif partition_field is not None:
            shards = [
                df
                for stream in session.streams
                for df in _stream_to_dfs(
                    bqs_client, stream.name, schema, timeout=timeout
                )
            ]
            # NOTE: if no rows satisfying the row_restriction, then `shards` will be empty list
            if len(shards) == 0:
                shards = [schema.empty_table().to_pandas()]
            shards = [shard.set_index(partition_field, drop=True) for shard in shards]

        elif stream_name is not None:
            shards = _stream_to_dfs(bqs_client, stream_name, schema, timeout=timeout)
            # NOTE: BQ Storage API can return empty streams
            if len(shards) == 0:
                shards = [schema.empty_table().to_pandas()]

        else:
            raise NotImplementedError(
                "Please specify either `partition_field` or `stream_name`."
            )

    return pd.concat(shards)


def read_gbq(
    project_id: str,
    dataset_id: str,
    table_id: str,
    partition_field: str = None,
    partitions: Iterable[str] = None,
    row_filter="",
    fields: list[str] = (),
    read_timeout: int = 3600,
):
    """Read table as dask dataframe using BigQuery Storage API via Arrow format.
    If `partition_field` and `partitions` are specified, then the resulting dask dataframe
    will be partitioned along the same boundaries. Otherwise, partitions will be approximately
    balanced according to BigQuery stream allocation logic.
    If `partition_field` is specified but not included in `fields` (either implicitly by requesting
    all fields, or explicitly by inclusion in the list `fields`), then it will still be included
    in the query in order to have it available for dask dataframe indexing.
    Args:
        project_id: BigQuery project
        dataset_id: BigQuery dataset within project
        table_id: BigQuery table within dataset
        partition_field: to specify filters of form "WHERE {partition_field} = ..."
        partitions: all values to select of `partition_field`
        fields: names of the fields (columns) to select (default None to "SELECT *")
        read_timeout: # of seconds an individual read request has before timing out
    Returns:
        dask dataframe
    See https://github.com/dask/dask/issues/3121 for additional context.
    """
    if (partition_field is None) and (partitions is not None):
        raise ValueError("Specified `partitions` without `partition_field`.")

    # If `partition_field` is not part of the `fields` filter, fetch it anyway to be able
    # to set it as dask dataframe index. We want this to be able to have consistent:
    # BQ partitioning + dask divisions + pandas index values
    if (partition_field is not None) and fields and (partition_field not in fields):
        fields = (partition_field, *fields)

    # These read tasks seems to cause deadlocks (or at least long stuck workers out of touch with
    # the scheduler), particularly when mixed with other tasks that execute C code. Anecdotally
    # annotating the tasks with a higher priority seems to help (but not fully solve) the issue at
    # the expense of higher cluster memory usage.
    with bigquery_client(project_id, with_storage_api=True) as (
        bq_client,
        bqs_client,
    ), dask.annotate(priority=1):
        table_ref = bq_client.get_table(".".join((dataset_id, table_id)))
        if table_ref.table_type == "VIEW":
            raise TypeError("Table type VIEW not supported")

        # The protobuf types can't be pickled (may be able to tweak w/ copyreg), so instead use a
        # generator func.
        def make_create_read_session_request(row_filter=""):
            return bigquery_storage.types.CreateReadSessionRequest(
                max_stream_count=100,  # 0 -> use as many streams as BQ Storage will provide
                parent=f"projects/{project_id}",
                read_session=bigquery_storage.types.ReadSession(
                    data_format=bigquery_storage.types.DataFormat.ARROW,
                    read_options=bigquery_storage.types.ReadSession.TableReadOptions(
                        row_restriction=row_filter,
                        selected_fields=fields,
                    ),
                    table=table_ref.to_bqstorage(),
                ),
            )

        # Create a read session in order to detect the schema.
        # Read sessions are light weight and will be auto-deleted after 24 hours.
        session = bqs_client.create_read_session(
            make_create_read_session_request(row_filter=row_filter)
        )
        schema = pyarrow.ipc.read_schema(
            pyarrow.py_buffer(session.arrow_schema.serialized_schema)
        )
        meta = schema.empty_table().to_pandas()
        delayed_kwargs = dict(prefix=f"{dataset_id}.{table_id}-")

        if partition_field is not None:
            if row_filter:
                raise ValueError("Cannot pass both `partition_field` and `row_filter`")
            delayed_kwargs["meta"] = meta.set_index(partition_field, drop=True)

            if partitions is None:
                logging.info(
                    "Specified `partition_field` without `partitions`; reading full table."
                )
                partitions = [
                    p
                    for p in bq_client.list_partitions(f"{dataset_id}.{table_id}")
                    if p != "__NULL__"
                ]
                # TODO generalize to ranges (as opposed to discrete values)

            partitions = sorted(partitions)
            delayed_kwargs["divisions"] = (*partitions, partitions[-1])
            row_filters = [
                f'{partition_field} = "{partition_value}"'
                for partition_value in partitions
            ]
            delayed_dfs = [
                _read_rows_arrow(
                    make_create_read_session_request=partial(
                        make_create_read_session_request, row_filter=row_filter
                    ),
                    partition_field=partition_field,
                    project_id=project_id,
                    timeout=read_timeout,
                )
                for row_filter in row_filters
            ]
        else:
            delayed_kwargs["meta"] = meta
            delayed_dfs = [
                _read_rows_arrow(
                    make_create_read_session_request=make_create_read_session_request,
                    project_id=project_id,
                    stream_name=stream.name,
                    timeout=read_timeout,
                )
                for stream in session.streams
            ]

        return dd.from_delayed(dfs=delayed_dfs, **delayed_kwargs)
