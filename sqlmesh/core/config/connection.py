from __future__ import annotations

import abc
import os
import sys
import typing as t
from enum import Enum

from pydantic import Field, root_validator

from sqlmesh.core import engine_adapter
from sqlmesh.core.config.base import BaseConfig
from sqlmesh.core.config.common import (
    concurrent_tasks_validator,
    http_headers_validator,
)
from sqlmesh.core.engine_adapter import EngineAdapter
from sqlmesh.utils.errors import ConfigError

if sys.version_info >= (3, 9):
    from typing import Annotated, Literal
else:
    from typing_extensions import Annotated, Literal


class _ConnectionConfig(abc.ABC, BaseConfig):
    concurrent_tasks: int

    @property
    @abc.abstractmethod
    def _connection_kwargs_keys(self) -> t.Set[str]:
        """keywords that should be passed into the connection"""

    @property
    @abc.abstractmethod
    def _engine_adapter(self) -> t.Type[EngineAdapter]:
        """The engine adapter for this connection"""

    @property
    @abc.abstractmethod
    def _connection_factory(self) -> t.Callable:
        """A function that is called to return a connection object for the given Engine Adapter"""

    @property
    def _static_connection_kwargs(self) -> t.Dict[str, t.Any]:
        """The static connection kwargs for this connection"""
        return {}

    @property
    def _extra_engine_config(self) -> t.Dict[str, t.Any]:
        """kwargs that are for execution config only"""
        return {}

    def create_engine_adapter(self) -> EngineAdapter:
        """Returns a new instance of the Engine Adapter."""
        return self._engine_adapter(
            lambda: self._connection_factory(
                **{
                    **self._static_connection_kwargs,
                    **{k: v for k, v in self.dict().items() if k in self._connection_kwargs_keys},
                }
            ),
            multithreaded=self.concurrent_tasks > 1,
            **self._extra_engine_config,
        )


class DuckDBConnectionConfig(_ConnectionConfig):
    """Configuration for the DuckDB connection.

    Args:
        database: The optional database name. If not specified, the in-memory database will be used.
        concurrent_tasks: The maximum number of tasks that can use this connection concurrently.
    """

    database: t.Optional[str]

    concurrent_tasks: Literal[1] = 1

    type_: Literal["duckdb"] = Field(alias="type", default="duckdb")

    @property
    def _connection_kwargs_keys(self) -> t.Set[str]:
        return {"database"}

    @property
    def _engine_adapter(self) -> t.Type[EngineAdapter]:
        return engine_adapter.DuckDBEngineAdapter

    @property
    def _connection_factory(self) -> t.Callable:
        import duckdb

        return duckdb.connect


class SnowflakeConnectionConfig(_ConnectionConfig):
    """Configuration for the Snowflake connection.

    Args:
        account: The Snowflake account name.
        user: The Snowflake username.
        password: The Snowflake password.
        warehouse: The optional warehouse name.
        database: The optional database name.
        role: The optional role name.
        concurrent_tasks: The maximum number of tasks that can use this connection concurrently.
        authenticator: The optional authenticator name. Defaults to username/password authentication ("snowflake").
                       Options: https://github.com/snowflakedb/snowflake-connector-python/blob/e937591356c067a77f34a0a42328907fda792c23/src/snowflake/connector/network.py#L178-L183
    """

    account: str
    user: t.Optional[str]
    password: t.Optional[str]
    warehouse: t.Optional[str]
    database: t.Optional[str]
    role: t.Optional[str]
    authenticator: t.Optional[str]

    concurrent_tasks: int = 4

    type_: Literal["snowflake"] = Field(alias="type", default="snowflake")

    _concurrent_tasks_validator = concurrent_tasks_validator

    @root_validator(pre=True)
    def _validate_authenticator(
        cls, values: t.Dict[str, t.Optional[str]]
    ) -> t.Dict[str, t.Optional[str]]:
        if "type" in values and values["type"] != "snowflake":
            return values
        auth = values.get("authenticator")
        user = values.get("user")
        password = values.get("password")
        if not auth and (not user or not password):
            raise ConfigError("User and password must be provided if using default authentication")
        return values

    @property
    def _connection_kwargs_keys(self) -> t.Set[str]:
        return {"user", "password", "account", "warehouse", "database", "role", "authenticator"}

    @property
    def _engine_adapter(self) -> t.Type[EngineAdapter]:
        return engine_adapter.SnowflakeEngineAdapter

    @property
    def _connection_factory(self) -> t.Callable:
        from snowflake import connector

        return connector.connect


class DatabricksConnectionConfig(_ConnectionConfig):
    """
    Databricks connection that uses the SQL connector for SQL models and then Databricks Connect for Dataframe operations

    Arg Source: https://github.com/databricks/databricks-sql-python/blob/main/src/databricks/sql/client.py#L39
    Args:
        server_hostname: Databricks instance host name.
        http_path: Http path either to a DBSQL endpoint (e.g. /sql/1.0/endpoints/1234567890abcdef)
                   or to a DBR interactive cluster (e.g. /sql/protocolv1/o/1234567890123456/1234-123456-slid123)
        access_token: Http Bearer access token, e.g. Databricks Personal Access Token.
        catalog: Default catalog to use for SQL models. Defaults to None which means it will use the default set in
                 the Databricks cluster (most likely `hive_metastore`).
        http_headers: An optional list of (k, v) pairs that will be set as Http headers on every request
        databricks_connect_server_hostname: The hostname to use when establishing a connecting using Databricks Connect.
                   Defaults to the `server_hostname` value.
        databricks_connect_access_token: The access token to use when establishing a connecting using Databricks Connect.
                   Defaults to the `access_token` value.
        databricks_connect_cluster_id: The cluster id to use when establishing a connecting using Databricks Connect.
                   Defaults to deriving the cluster id from the `http_path` value.
        force_databricks_connect: Force all queries to run using Databricks Connect instead of the SQL connector.
        disable_databricks_connect: Even if databricks connect is installed, do not use it.
    """

    server_hostname: t.Optional[str]
    http_path: t.Optional[str]
    access_token: t.Optional[str]
    catalog: t.Optional[str]
    http_headers: t.Optional[t.List[t.Tuple[str, str]]]
    databricks_connect_server_hostname: t.Optional[str]
    databricks_connect_access_token: t.Optional[str]
    databricks_connect_cluster_id: t.Optional[str]
    force_databricks_connect: bool = False
    disable_databricks_connect: bool = False

    concurrent_tasks: int = 1

    type_: Literal["databricks"] = Field(alias="type", default="databricks")

    _concurrent_tasks_validator = concurrent_tasks_validator
    _http_headers_validator = http_headers_validator

    @root_validator(pre=True)
    def _databricks_connect_validator(cls, values: t.Dict[str, t.Any]) -> t.Dict[str, t.Any]:
        from sqlmesh import runtime_env
        from sqlmesh.core.engine_adapter.databricks import DatabricksEngineAdapter

        if values["type"] != "databricks" or runtime_env.is_databricks:
            return values
        server_hostname, http_path, access_token = (
            values.get("server_hostname"),
            values.get("http_path"),
            values.get("access_token"),
        )
        if not server_hostname or not http_path or not access_token:
            raise ValueError(
                "`server_hostname`, `http_path`, and `access_token` are required for Databricks connections when not running in a notebook"
            )
        if DatabricksEngineAdapter.can_access_spark_session:
            if not values.get("databricks_connect_server_hostname"):
                values["databricks_connect_server_hostname"] = f"https://{server_hostname}"
            if not values.get("databricks_connect_access_token"):
                values["databricks_connect_access_token"] = access_token
            if not values.get("databricks_connect_cluster_id"):
                values["databricks_connect_cluster_id"] = http_path.split("/")[-1]
        return values

    @property
    def _connection_kwargs_keys(self) -> t.Set[str]:
        if self.use_spark_session_only:
            return set()
        return {
            "server_hostname",
            "http_path",
            "access_token",
            "http_headers",
            "session_configuration",
            "catalog",
        }

    @property
    def _engine_adapter(self) -> t.Type[engine_adapter.DatabricksEngineAdapter]:
        return engine_adapter.DatabricksEngineAdapter

    @property
    def _extra_engine_config(self) -> t.Dict[str, t.Any]:
        return {
            k: v
            for k, v in self.dict().items()
            if k.startswith("databricks_connect_") or k in ("catalog", "disable_databricks_connect")
        }

    @property
    def use_spark_session_only(self) -> bool:
        from sqlmesh import runtime_env

        return runtime_env.is_databricks or self.force_databricks_connect

    @property
    def _connection_factory(self) -> t.Callable:
        if self.use_spark_session_only:
            from sqlmesh.engines.spark.db_api.spark_session import connection

            return connection

        from databricks import sql

        return sql.connect

    @property
    def _static_connection_kwargs(self) -> t.Dict[str, t.Any]:
        from sqlmesh import runtime_env

        if not self.use_spark_session_only:
            return {}

        if runtime_env.is_databricks:
            from pyspark.sql import SparkSession

            return dict(
                spark=SparkSession.getActiveSession(),
                catalog=self.catalog,
            )

        from databricks.connect import DatabricksSession

        return dict(
            spark=DatabricksSession.builder.remote(
                host=self.databricks_connect_server_hostname,
                token=self.databricks_connect_access_token,
                cluster_id=self.databricks_connect_cluster_id,
            ).getOrCreate(),
            catalog=self.catalog,
        )


class BigQueryConnectionMethod(str, Enum):
    OAUTH = "oauth"
    OAUTH_SECRETS = "oauth-secrets"
    SERVICE_ACCOUNT = "service-account"
    SERVICE_ACCOUNT_JSON = "service-account-json"


class BigQueryPriority(str, Enum):
    BATCH = "batch"
    INTERACTIVE = "interactive"

    @property
    def is_batch(self) -> bool:
        return self == self.BATCH

    @property
    def is_interactive(self) -> bool:
        return self == self.INTERACTIVE

    @property
    def bigquery_constant(self) -> str:
        from google.cloud.bigquery import QueryPriority

        if self.is_batch:
            return QueryPriority.BATCH
        return QueryPriority.INTERACTIVE


class BigQueryConnectionConfig(_ConnectionConfig):
    """
    BigQuery Connection Configuration.
    """

    method: BigQueryConnectionMethod = BigQueryConnectionMethod.OAUTH

    project: t.Optional[str] = None
    location: t.Optional[str] = None
    # Keyfile Auth
    keyfile: t.Optional[str] = None
    keyfile_json: t.Optional[t.Dict[str, t.Any]] = None
    # Oath Secret Auth
    token: t.Optional[str] = None
    refresh_token: t.Optional[str] = None
    client_id: t.Optional[str] = None
    client_secret: t.Optional[str] = None
    token_uri: t.Optional[str] = None
    scopes: t.Tuple[str, ...] = ("https://www.googleapis.com/auth/bigquery",)
    job_creation_timeout_seconds: t.Optional[int] = None
    # Extra Engine Config
    job_execution_timeout_seconds: t.Optional[int] = None
    job_retries: t.Optional[int] = 1
    job_retry_deadline_seconds: t.Optional[int] = None
    priority: t.Optional[BigQueryPriority] = None
    maximum_bytes_billed: t.Optional[int] = None

    concurrent_tasks: int = 1

    type_: Literal["bigquery"] = Field(alias="type", default="bigquery")

    @property
    def _connection_kwargs_keys(self) -> t.Set[str]:
        return set()

    @property
    def _engine_adapter(self) -> t.Type[EngineAdapter]:
        return engine_adapter.BigQueryEngineAdapter

    @property
    def _static_connection_kwargs(self) -> t.Dict[str, t.Any]:
        """The static connection kwargs for this connection"""
        import google.auth
        from google.api_core import client_info
        from google.oauth2 import credentials, service_account

        if self.method == BigQueryConnectionMethod.OAUTH:
            creds, _ = google.auth.default(scopes=self.scopes)
        elif self.method == BigQueryConnectionMethod.SERVICE_ACCOUNT:
            creds = service_account.Credentials.from_service_account_file(
                self.keyfile, scopes=self.scopes
            )
        elif self.method == BigQueryConnectionMethod.SERVICE_ACCOUNT_JSON:
            creds = service_account.Credentials.from_service_account_info(
                self.keyfile_json, scopes=self.scopes
            )
        elif self.method == BigQueryConnectionMethod.OAUTH_SECRETS:
            creds = credentials.Credentials(
                token=self.token,
                refresh_token=self.refresh_token,
                client_id=self.client_id,
                client_secret=self.client_secret,
                token_uri=self.token_uri,
                scopes=self.scopes,
            )
        else:
            raise ConfigError("Invalid BigQuery Connection Method")
        client = google.cloud.bigquery.Client(
            project=self.project,
            credentials=creds,
            location=self.location,
            client_info=client_info.ClientInfo(user_agent="sqlmesh"),
        )

        return {
            "client": client,
        }

    @property
    def _extra_engine_config(self) -> t.Dict[str, t.Any]:
        return {
            k: v
            for k, v in self.dict().items()
            if k
            in {
                "job_creation_timeout_seconds",
                "job_execution_timeout_seconds",
                "job_retries",
                "job_retry_deadline_seconds",
                "priority",
                "maximum_bytes_billed",
            }
        }

    @property
    def _connection_factory(self) -> t.Callable:
        from google.cloud.bigquery.dbapi import connect

        return connect


class RedshiftConnectionConfig(_ConnectionConfig):
    """
    Redshift Connection Configuration.

    Arg Source: https://github.com/aws/amazon-redshift-python-driver/blob/master/redshift_connector/__init__.py#L146
    Note: A subset of properties were selected. Please open an issue/PR if you want to see more supported.

    Args:
        user: The username to use for authentication with the Amazon Redshift cluster.
        password: The password to use for authentication with the Amazon Redshift cluster.
        database: The name of the database instance to connect to.
        host: The hostname of the Amazon Redshift cluster.
        port: The port number of the Amazon Redshift cluster. Default value is 5439.
        source_address: No description provided
        unix_sock: No description provided
        ssl: Is SSL enabled. Default value is ``True``. SSL must be enabled when authenticating using IAM.
        sslmode: The security of the connection to the Amazon Redshift cluster. 'verify-ca' and 'verify-full' are supported.
        timeout: The number of seconds before the connection to the server will timeout. By default there is no timeout.
        tcp_keepalive: Is `TCP keepalive <https://en.wikipedia.org/wiki/Keepalive#TCP_keepalive>`_ used. The default value is ``True``.
        application_name: Sets the application name. The default value is None.
        preferred_role: The IAM role preferred for the current connection.
        principal_arn: The ARN of the IAM entity (user or role) for which you are generating a policy.
        credentials_provider: The class name of the IdP that will be used for authenticating with the Amazon Redshift cluster.
        region: The AWS region where the Amazon Redshift cluster is located.
        cluster_identifier: The cluster identifier of the Amazon Redshift cluster.
        iam: If IAM authentication is enabled. Default value is False. IAM must be True when authenticating using an IdP.
        is_serverless: Redshift end-point is serverless or provisional. Default value false.
        serverless_acct_id: The account ID of the serverless. Default value None
        serverless_work_group: The name of work group for serverless end point. Default value None.
    """

    user: t.Optional[str]
    password: t.Optional[str]
    database: t.Optional[str]
    host: t.Optional[str]
    port: t.Optional[int]
    source_address: t.Optional[str]
    unix_sock: t.Optional[str]
    ssl: t.Optional[bool]
    sslmode: t.Optional[str]
    timeout: t.Optional[int]
    tcp_keepalive: t.Optional[bool]
    application_name: t.Optional[str]
    preferred_role: t.Optional[str]
    principal_arn: t.Optional[str]
    credentials_provider: t.Optional[str]
    region: t.Optional[str]
    cluster_identifier: t.Optional[str]
    iam: t.Optional[bool]
    is_serverless: t.Optional[bool]
    serverless_acct_id: t.Optional[str]
    serverless_work_group: t.Optional[str]

    concurrent_tasks: int = 4

    type_: Literal["redshift"] = Field(alias="type", default="redshift")

    @property
    def _connection_kwargs_keys(self) -> t.Set[str]:
        return {
            "user",
            "password",
            "database",
            "host",
            "port",
            "source_address",
            "unix_sock",
            "ssl",
            "sslmode",
            "timeout",
            "tcp_keepalive",
            "application_name",
            "preferred_role",
            "principal_arn",
            "credentials_provider",
            "region",
            "cluster_identifier",
            "iam",
            "is_serverless",
            "serverless_acct_id",
            "serverless_work_group",
        }

    @property
    def _engine_adapter(self) -> t.Type[EngineAdapter]:
        return engine_adapter.RedshiftEngineAdapter

    @property
    def _connection_factory(self) -> t.Callable:
        from redshift_connector import connect

        return connect


class PostgresConnectionConfig(_ConnectionConfig):
    host: str
    user: str
    password: str
    port: int
    database: str
    keepalives_idle: t.Optional[int]
    connect_timeout: int = 10
    role: t.Optional[str] = None
    sslmode: t.Optional[str] = None

    concurrent_tasks: int = 4

    type_: Literal["postgres"] = Field(alias="type", default="postgres")

    @property
    def _connection_kwargs_keys(self) -> t.Set[str]:
        return {
            "host",
            "user",
            "password",
            "port",
            "database",
            "keepalives_idle",
            "connect_timeout",
            "role",
            "sslmode",
        }

    @property
    def _engine_adapter(self) -> t.Type[EngineAdapter]:
        return engine_adapter.PostgresEngineAdapter

    @property
    def _connection_factory(self) -> t.Callable:
        from psycopg2 import connect

        return connect


class SparkConnectionConfig(_ConnectionConfig):
    """
    Vanilla Spark Connection Configuration. Use `DatabricksConnectionConfig` for Databricks.
    """

    config_dir: t.Optional[str] = None
    catalog: t.Optional[str] = None
    config: t.Dict[str, t.Any] = {}

    concurrent_tasks: int = 4

    type_: Literal["spark"] = Field(alias="type", default="spark")

    @property
    def _connection_kwargs_keys(self) -> t.Set[str]:
        return {
            "catalog",
        }

    @property
    def _engine_adapter(self) -> t.Type[EngineAdapter]:
        return engine_adapter.SparkEngineAdapter

    @property
    def _connection_factory(self) -> t.Callable:
        from sqlmesh.engines.spark.db_api.spark_session import connection

        return connection

    @property
    def _static_connection_kwargs(self) -> t.Dict[str, t.Any]:
        from pyspark.conf import SparkConf
        from pyspark.sql import SparkSession

        spark_config = SparkConf()
        if self.config:
            for k, v in self.config.items():
                spark_config.set(k, v)

        if self.config_dir:
            os.environ["SPARK_CONF_DIR"] = self.config_dir
        return {
            "spark": SparkSession.builder.config(conf=spark_config)
            .enableHiveSupport()
            .getOrCreate(),
        }


ConnectionConfig = Annotated[
    t.Union[
        BigQueryConnectionConfig,
        DatabricksConnectionConfig,
        DuckDBConnectionConfig,
        PostgresConnectionConfig,
        RedshiftConnectionConfig,
        SnowflakeConnectionConfig,
        SparkConnectionConfig,
    ],
    Field(discriminator="type_"),
]
