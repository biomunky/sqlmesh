from __future__ import annotations

import abc
import importlib
import logging
import pkgutil
import typing as t

from sqlglot import __version__ as SQLGLOT_VERSION

from sqlmesh import migrations
from sqlmesh.core import scheduler
from sqlmesh.core.environment import Environment
from sqlmesh.core.snapshot import (
    Intervals,
    Snapshot,
    SnapshotId,
    SnapshotIdLike,
    SnapshotInfoLike,
    SnapshotIntervals,
    SnapshotNameVersionLike,
    SnapshotTableInfo,
)
from sqlmesh.utils import major_minor
from sqlmesh.utils.date import TimeLike, now, to_datetime
from sqlmesh.utils.errors import SQLMeshError
from sqlmesh.utils.pydantic import PydanticModel

logger = logging.getLogger(__name__)


class Versions(PydanticModel):
    """Represents the various versions of dependencies in the state sync."""

    schema_version: int
    sqlglot_version: str

    @property
    def minor_sqlglot_version(self) -> t.Tuple[int, int]:
        return major_minor(self.sqlglot_version)


MIGRATIONS = [
    importlib.import_module(f"sqlmesh.migrations.{migration}")
    for migration in sorted(info.name for info in pkgutil.iter_modules(migrations.__path__))
]
SCHEMA_VERSION: int = len(MIGRATIONS)


class StateReader(abc.ABC):
    """Abstract base class for read-only operations on snapshot and environment state."""

    @abc.abstractmethod
    def get_snapshots(
        self, snapshot_ids: t.Optional[t.Iterable[SnapshotIdLike]], hydrate_seeds: bool = False
    ) -> t.Dict[SnapshotId, Snapshot]:
        """Bulk fetch snapshots given the corresponding snapshot ids.

        Args:
            snapshot_ids: Iterable of snapshot ids to get. If not provided all
                available snapshots will be returned.
            hydrate_seeds: Whether to hydrate seed snapshots with the content.

        Returns:
            A dictionary of snapshot ids to snapshots for ones that could be found.
        """

    @abc.abstractmethod
    def snapshots_exist(self, snapshot_ids: t.Iterable[SnapshotIdLike]) -> t.Set[SnapshotId]:
        """Checks if multiple snapshots exist in the state sync.

        Args:
            snapshot_ids: Iterable of snapshot ids to bulk check.

        Returns:
            A set of all the existing snapshot ids.
        """

    @abc.abstractmethod
    def models_exist(self, names: t.Iterable[str], exclude_external: bool = False) -> t.Set[str]:
        """Returns the model names that exist in the state sync.

        Args:
            names: Iterable of model names to check.
            exclude_external: Whether to exclude external models from the output.

        Returns:
            A set of all the existing model names.
        """

    @abc.abstractmethod
    def get_environment(self, environment: str) -> t.Optional[Environment]:
        """Fetches the environment if it exists.

        Args:
            environment: The environment

        Returns:
            The environment object.
        """

    @abc.abstractmethod
    def get_environments(self) -> t.List[Environment]:
        """Fetches all environments.

        Returns:
            A list of all environments.
        """

    @abc.abstractmethod
    def get_snapshot_intervals(
        self, snapshots: t.Optional[t.Iterable[SnapshotNameVersionLike]]
    ) -> t.List[SnapshotIntervals]:
        """Fetch intervals for given snapshots as well as for snapshots that share a version with the given ones.

        Args:
            snapshots: Target snapshot IDs. If not specified all intervals will be fetched.
            current_only: Whether to only fetch intervals for snapshots provided as input as opposed
                to fetching intervals for all snapshots that share the same version as the input ones.

        Returns:
            The list of snapshot intervals, one per unique version.
        """

    def missing_intervals(
        self,
        env_or_snapshots: str | Environment | t.Iterable[Snapshot],
        start: t.Optional[TimeLike] = None,
        end: t.Optional[TimeLike] = None,
        latest: t.Optional[TimeLike] = None,
        restatements: t.Optional[t.Iterable[str]] = None,
    ) -> t.Dict[Snapshot, Intervals]:
        """Find missing intervals for an environment or a list of snapshots.

        Args:
            env_or_snapshots: The environment or snapshots to find missing intervals for.
            start: The start of the time range to look for.
            end: The end of the time range to look for.
            latest: The latest datetime to use for non-incremental queries.

        Returns:
            A dictionary of SnapshotId to Intervals.
        """

        if isinstance(env_or_snapshots, str):
            env = self.get_environment(env_or_snapshots)
        elif isinstance(env_or_snapshots, Environment):
            env = env_or_snapshots
        else:
            env = None

        if env:
            snapshots_by_id = self.get_snapshots(env.snapshots)
            start = start or env.start_at
            end = end or env.end_at
        elif isinstance(env_or_snapshots, str):
            snapshots_by_id = {}
        elif not isinstance(env_or_snapshots, Environment):
            snapshots_by_id = {snapshot.snapshot_id: snapshot for snapshot in env_or_snapshots}
        else:
            raise SQLMeshError("This shouldn't be possible.")

        if not snapshots_by_id:
            return {}

        unversioned = [snapshot for snapshot in snapshots_by_id.values() if not snapshot.version]

        snapshots_by_id = {
            **snapshots_by_id,
            **(self.get_snapshots(unversioned) if unversioned else {}),
        }

        snapshot_intervals = self.get_snapshot_intervals(snapshots_by_id.values())

        missing = {}
        start_date = to_datetime(start or scheduler.earliest_start_date(snapshots_by_id.values()))
        end_date = end or now()
        restatements = set(restatements or [])

        for snapshot in Snapshot.hydrate_with_intervals_by_version(
            snapshots_by_id.values(), snapshot_intervals
        ):
            if snapshot.name in restatements:
                snapshot.remove_interval(start_date, end_date, latest)
            intervals = snapshot.missing_intervals(
                max(
                    start_date,
                    to_datetime(
                        scheduler.start_date(snapshot, snapshots_by_id.values()) or start_date
                    ),
                ),
                end_date,
                latest=latest,
                restatements=restatements,
            )
            if intervals:
                missing[snapshot] = intervals
        return missing

    def get_versions(self, validate: bool = True) -> Versions:
        """Get the current versions of the SQLMesh schema and libraries.

        Args:
            validate: Whether or not to raise error if the running version is ahead of state.

        Returns:
            The versions object.
        """
        versions = self._get_versions()

        def raise_error(lib: str, local: str | int, remote: str | int, ahead: bool = False) -> None:
            if ahead:
                raise SQLMeshError(
                    f"{lib} (local) is using version '{local}' which is ahead of '{remote}' (remote). Please run a migration."
                )
            raise SQLMeshError(
                f"{lib} (local) is using version '{local}' which is behind '{remote}' (remote). Please upgrade {lib}."
            )

        if SCHEMA_VERSION < versions.schema_version:
            raise_error("SQLMesh", SCHEMA_VERSION, versions.schema_version)

        if major_minor(SQLGLOT_VERSION) < major_minor(versions.sqlglot_version):
            raise_error("SQLGlot", SQLGLOT_VERSION, versions.sqlglot_version)

        if validate:
            if SCHEMA_VERSION > versions.schema_version:
                raise_error("SQLMesh", SCHEMA_VERSION, versions.schema_version, ahead=True)

            if major_minor(SQLGLOT_VERSION) > major_minor(versions.sqlglot_version):
                raise_error("SQLGlot", SQLGLOT_VERSION, versions.sqlglot_version, ahead=True)

        return versions

    @abc.abstractmethod
    def _get_versions(self, lock_for_update: bool = False) -> Versions:
        """Queries the store to get the current versions of SQLMesh and deps.

        Args:
            lock_for_update: Whether or not the usage of this method plans to update the row.

        Returns:
            The versions object.
        """


class StateSync(StateReader, abc.ABC):
    """Abstract base class for snapshot and environment state management."""

    @abc.abstractmethod
    def push_snapshots(self, snapshots: t.Iterable[Snapshot]) -> None:
        """Push snapshots into the state sync.

        This method only allows for pushing new snapshots. If existing snapshots are found,
        this method should raise an error.

        Raises:
            SQLMeshError when existing snapshots are pushed.

        Args:
            snapshots: A list of snapshots to save in the state sync.
        """

    @abc.abstractmethod
    def delete_snapshots(self, snapshot_ids: t.Iterable[SnapshotIdLike]) -> None:
        """Delete snapshots from the state sync.

        Args:
            snapshot_ids: A list of snapshot like objects to delete.
        """

    @abc.abstractmethod
    def delete_expired_snapshots(self) -> t.List[Snapshot]:
        """Removes expired snapshots.

        Expired snapshots are snapshots that have exceeded their time-to-live
        and are no longer in use within an environment.

        Returns:
            The list of removed snapshots.
        """

    @abc.abstractmethod
    def invalidate_environment(self, name: str) -> None:
        """Invalidates the target environment by setting its expiration timestamp to now.

        Args:
            name: The name of the environment to invalidate.
        """

    @abc.abstractmethod
    def add_interval(
        self,
        snapshot: Snapshot,
        start: TimeLike,
        end: TimeLike,
        is_dev: bool = False,
    ) -> None:
        """Add an interval to a snapshot and sync it to the store.

        Snapshots must be pushed before adding intervals to them.

        Args:
            snapshot: The snapshot like object to add an interval to.
            start: The start of the interval to add.
            end: The end of the interval to add.
            is_dev: Indicates whether the given interval is being added while in
                development mode.
        """

    @abc.abstractmethod
    def remove_interval(
        self,
        snapshots: t.Iterable[SnapshotInfoLike],
        start: TimeLike,
        end: TimeLike,
        all_snapshots: t.Optional[t.Iterable[Snapshot]] = None,
    ) -> None:
        """Remove an interval from a list of snapshots and sync it to the store.

        Because multiple snapshots can be pointing to the same version or physical table, this method
        can also grab all snapshots tied to the passed in version.

        Args:
            snapshots: The snapshot info like object to remove intervals from.
            start: The start of the interval to add.
            end: The end of the interval to add.
            all_snapshots: All snapshots can be passed in to skip fetching matching snapshot versions.
        """

    @abc.abstractmethod
    def promote(
        self, environment: Environment, no_gaps: bool = False
    ) -> t.Tuple[t.List[SnapshotTableInfo], t.List[SnapshotTableInfo]]:
        """Update the environment to reflect the current state.

        This method verifies that snapshots have been pushed.

        Args:
            environment: The environment to promote.
            no_gaps:  Whether to ensure that new snapshots for models that are already a
                part of the target environment have no data gaps when compared against previous
                snapshots for same models.

        Returns:
           A tuple of (added snapshot table infos, removed snapshot table infos)
        """

    @abc.abstractmethod
    def finalize(self, environment: Environment) -> None:
        """Finalize the target environment, indicating that this environment has been
        fully promoted and is ready for use.

        Args:
            environment: The target environment to finalize.
        """

    @abc.abstractmethod
    def delete_expired_environments(self) -> t.List[Environment]:
        """Removes expired environments.

        Expired environments are environments that have exceeded their time-to-live value.

        Returns:
            The list of removed environments.
        """

    @abc.abstractmethod
    def unpause_snapshots(
        self, snapshots: t.Iterable[SnapshotInfoLike], unpaused_dt: TimeLike
    ) -> None:
        """Unpauses target snapshots.

        Unpaused snapshots are scheduled for evaluation on a recurring basis.
        Once unpaused a snapshot can't be paused again.

        Args:
            snapshots: Target snapshots.
            unpaused_dt: The datetime object which indicates when target snapshots
                were unpaused.
        """

    @abc.abstractmethod
    def compact_intervals(self) -> None:
        """Compacts intervals for all snapshots.

        Compaction process involves merging of existing interval records into new records and
        then deleting the old ones.
        """

    @abc.abstractmethod
    def migrate(self, skip_backup: bool = False) -> None:
        """Migrate the state sync to the latest SQLMesh / SQLGlot version."""

    @abc.abstractmethod
    def rollback(self) -> None:
        """Rollback to previous backed up state."""
