import logging
from collections import defaultdict
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import psycopg2
import pytz
from psycopg2 import sql
from psycopg2.extras import execute_values
from pydantic.schema import Literal

from feast import Entity, FeatureTable
from feast.feature_view import FeatureView
from feast.infra.key_encoding_utils import serialize_entity_key
from feast.infra.online_stores.online_store import OnlineStore
from feast.protos.feast.types.EntityKey_pb2 import EntityKey as EntityKeyProto
from feast.protos.feast.types.Value_pb2 import Value as ValueProto
from feast.repo_config import RepoConfig

from ..postgres_config import PostgreSQLConfig


class PostgreSQLOnlineStoreConfig(PostgreSQLConfig):
    type: Literal[
        "feast_postgres.PostgreSQLOnlineStore"
    ] = "feast_postgres.PostgreSQLOnlineStore"


class PostgreSQLOnlineStore(OnlineStore):
    _conn: Optional[psycopg2._psycopg.connection] = None

    def _get_conn(self, config: RepoConfig):
        if not self._conn:
            assert config.online_store.type == "feast_postgres.PostgreSQLOnlineStore"
            self._conn = psycopg2.connect(
                dbname=config.online_store.database,
                host=config.online_store.host,
                port=int(config.online_store.port),
                user=config.online_store.user,
                password=config.online_store.password,
                options="-c search_path={}".format(
                    config.online_store.db_schema
                    if config.online_store.db_schema
                    else config.online_store.user
                ),
            )
        return self._conn

    def online_write_batch(
        self,
        config: RepoConfig,
        table: Union[FeatureTable, FeatureView],
        data: List[
            Tuple[EntityKeyProto, Dict[str, ValueProto], datetime, Optional[datetime]]
        ],
        progress: Optional[Callable[[int], Any]],
    ) -> None:
        project = config.project

        with self._get_conn(config) as conn, conn.cursor() as cur:
            insert_values = []
            for entity_key, values, timestamp, created_ts in data:
                entity_key_bin = serialize_entity_key(entity_key)
                timestamp = _to_naive_utc(timestamp)
                if created_ts is not None:
                    created_ts = _to_naive_utc(created_ts)

                for feature_name, val in values.items():
                    insert_values.append(
                        (
                            entity_key_bin,
                            feature_name,
                            val.SerializeToString(),
                            timestamp,
                            created_ts,
                        )
                    )
            # Controll the batch so that we can update the progress
            batch_size = 5000
            for i in range(0, len(insert_values), batch_size):
                cur_batch = insert_values[i : i + batch_size]
                execute_values(
                    cur,
                    sql.SQL(
                        """
                        INSERT INTO {}
                        (entity_key, feature_name, value, event_ts, created_ts)
                        VALUES %s
                        ON CONFLICT (entity_key, feature_name) DO
                        UPDATE SET
                            value = EXCLUDED.value,
                            event_ts = EXCLUDED.event_ts,
                            created_ts = EXCLUDED.created_ts;
                        """,
                    ).format(sql.Identifier(_table_id(project, table))),
                    cur_batch,
                    page_size=batch_size,
                )
                if progress:
                    progress(len(cur_batch))

    def online_read(
        self,
        config: RepoConfig,
        table: Union[FeatureTable, FeatureView],
        entity_keys: List[EntityKeyProto],
        requested_features: Optional[List[str]] = None,
    ) -> List[Tuple[Optional[datetime], Optional[Dict[str, ValueProto]]]]:
        result: List[Tuple[Optional[datetime], Optional[Dict[str, ValueProto]]]] = []

        project = config.project
        with self._get_conn(config) as conn, conn.cursor() as cur:
            # Collecting all the keys to a list allows us to make fewer round trips
            # to PostgreSQL
            keys = []
            for entity_key in entity_keys:
                keys.append(serialize_entity_key(entity_key))

            cur.execute(
                sql.SQL(
                    """
                    SELECT entity_key, feature_name, value, event_ts
                    FROM {} WHERE entity_key = ANY(%s);
                    """
                ).format(sql.Identifier(_table_id(project, table)),),
                (keys,),
            )

            rows = cur.fetchall()

            # Since we don't know the order returned from PostgreSQL we'll need
            # to construct a dict to be able to quickly look up the correct row
            # when we iterate through the keys since they are in the correct order
            values_dict = defaultdict(list)
            for row in rows if rows is not None else []:
                values_dict[row[0].tobytes()].append(row[1:])

            for key in keys:
                if key in values_dict:
                    value = values_dict[key]
                    res = {}
                    for feature_name, value_bin, event_ts in value:
                        val = ValueProto()
                        val.ParseFromString(value_bin)
                        res[feature_name] = val
                    result.append((event_ts, res))
                else:
                    result.append((None, None))

        return result

    def update(
        self,
        config: RepoConfig,
        tables_to_delete: Sequence[Union[FeatureTable, FeatureView]],
        tables_to_keep: Sequence[Union[FeatureTable, FeatureView]],
        entities_to_delete: Sequence[Entity],
        entities_to_keep: Sequence[Entity],
        partial: bool,
    ):
        project = config.project
        with self._get_conn(config) as conn, conn.cursor() as cur:
            # If a db_schema is provided, then that schema gets created if it doesn't
            # exist. Else a schema is created for the feature store user.
            if config.online_store.db_schema:
                create_schema_sql = sql.SQL(
                    "CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION {}"
                ).format(
                    sql.Identifier(config.online_store.db_schema),
                    sql.Identifier(config.online_store.user),
                )
            else:
                create_schema_sql = sql.SQL(
                    "CREATE SCHEMA IF NOT EXISTS AUTHORIZATION {}"
                ).format(sql.Identifier(config.online_store.user),)
            cur.execute(create_schema_sql)

            for table in tables_to_delete:
                cur.execute(
                    sql.SQL(
                        """
                        DROP TABLE IF EXISTS {};
                        """
                    ).format(sql.Identifier(_table_id(project, table)),)
                )

            for table in tables_to_keep:
                cur.execute(
                    sql.SQL(
                        """
                        CREATE TABLE IF NOT EXISTS {}
                        (
                            entity_key BYTEA,
                            feature_name TEXT,
                            value BYTEA,
                            event_ts TIMESTAMPTZ,
                            created_ts TIMESTAMPTZ,
                            PRIMARY KEY(entity_key, feature_name)
                        );
                        """
                    ).format(sql.Identifier(_table_id(project, table)),)
                )
                cur.execute(
                    sql.SQL(
                        """
                        CREATE INDEX IF NOT EXISTS {}
                        ON {} (entity_key);
                        """
                    ).format(
                        sql.Identifier(f"{_table_id(project, table)}_ek"),
                        sql.Identifier(_table_id(project, table)),
                    )
                )

            conn.commit()

    def teardown(
        self,
        config: RepoConfig,
        tables: Sequence[Union[FeatureTable, FeatureView]],
        entities: Sequence[Entity],
    ):
        try:
            with self._get_conn(config) as conn, conn.cursor() as cur:
                cur.execute(
                    sql.SQL(
                        """
                        DROP SCHEMA IF EXISTS {} CASCADE;
                        """
                    ).format(sql.Identifier(config.online_store.user),)
                )
        except Exception:
            logging.exception("Teardown failed")


def _table_id(project: str, table: Union[FeatureTable, FeatureView]) -> str:
    return f"{project}_{table.name}"


def _to_naive_utc(ts: datetime):
    if ts.tzinfo is None:
        return ts
    else:
        return ts.astimezone(pytz.utc).replace(tzinfo=None)
