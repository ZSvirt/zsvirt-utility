#!/usr/bin/env python
# encoding: utf-8

import glob
import os
import re
import shutil
import tarfile
import tempfile
import zlib
from collections import namedtuple


SQL_MIGRATION_PATTERN = re.compile(r'^V([0-9][0-9._]*)__([^.]+)\.sql$')
OPEN_SOURCE_UPGRADE = 'open-source-to-open-source'
OPEN_SOURCE_TO_EE = 'open-source-to-ee'
COMMERCIAL_UPGRADE = 'commercial-to-ee'
FLYWAY_SQL_CALLBACKS = (
    'beforeMigrate.sql',
    'beforeValidate.sql',
)

Migration = namedtuple('Migration', [
    'version',
    'description',
    'script',
    'checksum',
])

AppliedMigration = namedtuple('AppliedMigration', [
    'installed_rank',
    'version',
    'script',
    'checksum',
    'migration_type',
    'success',
])

SchemaCheckpoint = namedtuple('SchemaCheckpoint', [
    'source',
    'target',
])

SchemaCheckpointMap = namedtuple('SchemaCheckpointMap', [
    'opensource_migrations',
    'checkpoints',
])

SchemaHistoryBackfillPlan = namedtuple('SchemaHistoryBackfillPlan', [
    'upgrade_path',
    'current_source',
    'checkpoint',
    'ee_history_to_backfill',
    'opensource_history_to_delete',
])


class SchemaHistoryBackfillError(Exception):
    pass


def signed_crc32(data):
    checksum = zlib.crc32(data) & 0xffffffff
    if checksum >= 0x80000000:
        checksum -= 0x100000000
    return checksum


def flyway_version_key(version):
    parts = [int(part) for part in version.replace('_', '.').split('.')]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def parse_sql_migration_script(script):
    match = SQL_MIGRATION_PATTERN.match(script)
    if not match:
        return None

    version = match.group(1).replace('_', '.')
    description = match.group(2).replace('_', ' ')
    return version, description


def scan_sql_migrations(schema_dir):
    migrations = []
    for path in glob.glob(os.path.join(schema_dir, 'V*.sql')):
        script = os.path.basename(path)
        parsed = parse_sql_migration_script(script)
        if not parsed:
            continue

        with open(path, 'rb') as fd:
            checksum = signed_crc32(fd.read())

        version, description = parsed
        migrations.append(Migration(version, description, script, checksum))

    migrations.sort(key=lambda migration: flyway_version_key(migration.version))
    return migrations


def scan_open_source_migrations(db_dir):
    migrations = scan_sql_migrations(os.path.join(db_dir, 'upgrade'))
    migrations.extend(
        migration for migration in scan_sql_migrations(db_dir)
        if migration.script == 'V0.5.1__schema.sql'
    )
    migrations.sort(key=lambda migration: flyway_version_key(migration.version))
    return migrations


def copy_schema_callbacks_to_upgrade_dir(db_dir, upgrade_dir):
    if not os.path.isdir(upgrade_dir):
        os.makedirs(upgrade_dir)

    for callback in FLYWAY_SQL_CALLBACKS:
        source_path = os.path.join(db_dir, callback)
        if not os.path.isfile(source_path):
            continue

        try:
            shutil.copy2(source_path, os.path.join(upgrade_dir, callback))
        except EnvironmentError as e:
            raise SchemaHistoryBackfillError(
                'failed to copy ZSvirt schema callback %s to %s: %s' %
                (source_path, upgrade_dir, e)
            )


def prepare_zsvirt_ee_schema_upgrade_dir(upgrade_dir, ee_history_dir, schema_map_archive, ee_config_dir):
    """Prepare commercial SQL files and return the package's checkpoint map.

    Return None for a non-EE target. For an EE target, return:
        SchemaCheckpointMap(
            opensource_migrations=[Migration(version, description, script, checksum), ...],
            checkpoints=[
                SchemaCheckpoint(source=Migration(...), target=Migration(...)), ...
            ],
        )

    opensource_migrations contains packaged open-source SQL, including V0.5.1.
    Each checkpoint maps an open-source source to a commercial target.
    This describes the package, not migrations applied in the database.
    """
    if not os.path.isdir(ee_config_dir):
        return None

    archive_exists = schema_map_archive and os.path.exists(schema_map_archive)
    history_exists = ee_history_dir and os.path.isdir(ee_history_dir)

    if not archive_exists:
        raise SchemaHistoryBackfillError(
            'missing ZSvirt EE schema map archive %s' % schema_map_archive
        )

    if not history_exists:
        raise SchemaHistoryBackfillError(
            'missing ZSvirt EE schema history directory %s' % ee_history_dir
        )

    tmpdir = tempfile.mkdtemp()
    try:
        tar = tarfile.open(schema_map_archive, 'r:gz')
        try:
            _safe_extract_tar(tar, tmpdir)
        finally:
            tar.close()

        _replace_schema_upgrade_migrations(upgrade_dir, ee_history_dir)
        _copy_missing_checkpoint_migrations(tmpdir, upgrade_dir)

        return scan_schema_checkpoint_map(tmpdir, scan_sql_migrations(upgrade_dir))
    finally:
        shutil.rmtree(tmpdir)


def scan_schema_checkpoint_map(schema_map_dir, resolved_migrations):
    source_dir = os.path.join(schema_map_dir, 'upgrade')
    target_dir = os.path.join(schema_map_dir, 'ee')
    if not os.path.isdir(source_dir) or not os.path.isdir(target_dir):
        return SchemaCheckpointMap([], [])

    opensource_migrations = scan_open_source_migrations(schema_map_dir)
    source_by_script = dict((migration.script, migration) for migration in opensource_migrations)
    target_by_script = dict((migration.script, migration) for migration in resolved_migrations)

    checkpoints = []
    for path in glob.glob(os.path.join(target_dir, 'V*.sql')):
        if not os.path.islink(path):
            continue

        target_script = os.path.basename(path)
        source_script = os.path.basename(os.readlink(path))
        source = source_by_script.get(source_script)
        target = target_by_script.get(target_script)

        if source is None:
            raise SchemaHistoryBackfillError(
                'missing open-source schema migration %s referenced by %s' %
                (source_script, target_script)
            )

        if target is None:
            raise SchemaHistoryBackfillError(
                'missing ZSvirt EE schema migration %s referenced by open-source migration %s' %
                (target_script, source_script)
            )

        checkpoints.append(SchemaCheckpoint(source, target))

    _append_legacy_open_source_checkpoints(checkpoints, source_by_script, target_by_script)
    checkpoints.sort(key=lambda checkpoint: flyway_version_key(checkpoint.target.version))
    return SchemaCheckpointMap(opensource_migrations, checkpoints)


def parse_applied_migrations(output):
    if isinstance(output, bytes):
        output = output.decode('utf-8')

    migrations = []
    for line in output.splitlines():
        if not line.strip():
            continue

        fields = line.split('\t')
        if len(fields) != 6:
            raise SchemaHistoryBackfillError(
                'unexpected schema_version query output: %s' % line
            )

        installed_rank, version, script, checksum, migration_type, success = fields
        migrations.append(AppliedMigration(
            int(installed_rank),
            version,
            script,
            _parse_checksum(checksum),
            migration_type,
            int(success),
        ))

    return migrations


def plan_zsvirt_ee_schema_history_backfill(applied_migrations, resolved_migrations, schema_checkpoint_map=None):
    if schema_checkpoint_map is None:
        if not applied_migrations:
            raise SchemaHistoryBackfillError('cannot verify open-source schema history: database history is empty')
        opensource_by_key = _migrations_by_script_checksum(resolved_migrations)
        # Applied database history ⊆ packaged open-source SQL (script, checksum; success=1).
        for migration in applied_migrations:
            if (migration.migration_type != 'SQL' or migration.success != 1
                    or _script_checksum_key(migration) not in opensource_by_key):
                raise SchemaHistoryBackfillError(
                    'cannot verify open-source schema history for %s: expected successful SQL matching '
                    'the open-source package; commercial-to-open-source upgrade is not supported. '
                    'Manual intervention is required before retrying' % migration.script)
        _validate_unapplied_open_source_migrations(applied_migrations, resolved_migrations)
        return SchemaHistoryBackfillPlan(OPEN_SOURCE_UPGRADE, None, None, [], [])

    if not resolved_migrations:
        raise SchemaHistoryBackfillError('the EE package contains no commercial SQL migrations')
    for migration in applied_migrations:
        if migration.success != 1:
            raise SchemaHistoryBackfillError(
                'failed schema history must be resolved before EE upgrade: %s' % migration.script)

    source_by_key = _migrations_by_script_checksum(schema_checkpoint_map.opensource_migrations)
    current_edition = _classify_schema_history(applied_migrations, source_by_key, resolved_migrations)
    if current_edition == 'commercial':
        return SchemaHistoryBackfillPlan(
            COMMERCIAL_UPGRADE, None, None, [], [])

    current_source = _find_current_source_migration(applied_migrations, source_by_key)
    return _plan_open_source_to_ee_upgrade(
        applied_migrations, resolved_migrations, schema_checkpoint_map, current_source)


def _plan_open_source_to_ee_upgrade(applied_migrations, resolved_migrations, schema_checkpoint_map, current_source):
    source_by_key = _migrations_by_script_checksum(schema_checkpoint_map.opensource_migrations)
    opensource_history_to_delete = []
    # Applied database history ⊆ packaged open-source SQL (script, checksum; success=1).
    for applied in applied_migrations:
        if applied.migration_type != 'SQL':
            raise SchemaHistoryBackfillError(
                'unsupported history type %s during open-source conversion: %s' %
                (applied.migration_type, applied.script))
        key = _script_checksum_key(applied)
        if key not in source_by_key:
            raise SchemaHistoryBackfillError(
                'cannot convert all open-source history: unknown SQL or checksum mismatch for %s' %
                applied.script)
        opensource_history_to_delete.append(applied)

    _validate_unapplied_open_source_migrations(
        applied_migrations, schema_checkpoint_map.opensource_migrations)

    checkpoint_by_source_key = dict(
        (_script_checksum_key(checkpoint.source), checkpoint)
        for checkpoint in schema_checkpoint_map.checkpoints
    )
    checkpoint = checkpoint_by_source_key.get(_script_checksum_key(current_source))
    if checkpoint is None:
        raise SchemaHistoryBackfillError(
            'missing ZSvirt EE schema checkpoint for current open-source migration %s' %
            current_source.script
        )

    ee_history_to_backfill = []
    checkpoint_version = flyway_version_key(checkpoint.target.version)
    for migration in sorted(resolved_migrations, key=lambda item: flyway_version_key(item.version)):
        # Leave migrations above the checkpoint for Flyway to execute, without backfilling their history.
        if flyway_version_key(migration.version) > checkpoint_version:
            continue
        ee_history_to_backfill.append(migration)

    return SchemaHistoryBackfillPlan(
        OPEN_SOURCE_TO_EE,
        current_source,
        checkpoint.target,
        ee_history_to_backfill,
        opensource_history_to_delete,
    )


def _validate_unapplied_open_source_migrations(applied_migrations, opensource_migrations):
    """Require unexecuted open-source SQL to be newer than the database maximum.

    Used for both open-source-to-open-source and open-source-to-EE upgrades,
    after validating that nonempty applied history is a subset of packaged
    open-source SQL. Exclude applied SQL by script and checksum, then require
    every remaining version to exceed the highest applied open-source version
    in Flyway version order, not the commercial checkpoint. Raise
    SchemaHistoryBackfillError on an older or equal version; do not modify
    database history or execute SQL.
    """
    applied_keys = set(_script_checksum_key(migration) for migration in applied_migrations)
    latest_applied = max(applied_migrations, key=lambda migration: flyway_version_key(migration.version))
    latest_version = flyway_version_key(latest_applied.version)
    for migration in opensource_migrations:
        if _script_checksum_key(migration) in applied_keys:
            continue
        if flyway_version_key(migration.version) <= latest_version:
            raise SchemaHistoryBackfillError(
                'pending open-source migration %s must be newer than database maximum version %s; '
                'manual intervention is required before retrying upgrade' %
                (migration.script, latest_applied.version))


def build_schema_history_backfill_sql(plan, installed_by):
    if not plan.ee_history_to_backfill and not plan.opensource_history_to_delete:
        return None

    statements = ['START TRANSACTION']
    if plan.opensource_history_to_delete:
        statements.append(
            'DELETE FROM `schema_version` WHERE\n%s' %
            '\nOR '.join([
                '(`success` = 1 AND `type` = \'SQL\' AND `version` = %s AND `script` = %s AND %s)' %
                (
                    sql_string(migration.version),
                    sql_string(migration.script),
                    _checksum_predicate(migration.checksum),
                )
                for migration in plan.opensource_history_to_delete
            ])
        )

    if plan.ee_history_to_backfill:
        values = []
        for rank, migration in enumerate(plan.ee_history_to_backfill, 1):
            values.append('(%d, %d, %s, %s, %s, %s, %s, %s, NOW(), 0, 1)' % (
                rank,
                rank,
                sql_string(migration.version),
                sql_string(migration.description),
                sql_string('SQL'),
                sql_string(migration.script),
                'NULL' if migration.checksum is None else str(migration.checksum),
                sql_string(installed_by),
            ))

        statements.append(
            'INSERT INTO `schema_version` '
            '(`version_rank`, `installed_rank`, `version`, `description`, `type`, '
            '`script`, `checksum`, `installed_by`, `installed_on`, `execution_time`, `success`) '
            'VALUES\n%s' % ',\n'.join(values)
        )

    statements.append('COMMIT')
    return ';\n'.join(statements) + ';'


def sql_string(value):
    return "'" + str(value).replace("'", "''") + "'"


def _find_current_source_migration(applied_migrations, source_by_key):
    sql_success_migrations = [
        migration for migration in applied_migrations
        if migration.success == 1
        and migration.migration_type == 'SQL'
        and migration.script.startswith('V')
        and migration.script.endswith('.sql')
    ]

    sql_success_migrations.sort(
        key=lambda migration: (flyway_version_key(migration.version), migration.installed_rank), reverse=True)
    for migration in sql_success_migrations:
        if _script_checksum_key(migration) in source_by_key:
            return migration

    return None


def _classify_schema_history(applied_migrations, source_by_key, resolved_migrations):
    commercial_by_key = _migrations_by_script_checksum(resolved_migrations)
    has_open_source = False
    has_commercial = False
    for migration in applied_migrations:
        if migration.migration_type != 'SQL':
            raise SchemaHistoryBackfillError(
                'unsupported schema history type %s: %s' % (migration.migration_type, migration.script))
        key = _script_checksum_key(migration)
        in_source = key in source_by_key
        in_commercial = key in commercial_by_key
        # Only SQL matching exactly one edition can identify it.
        if in_source == in_commercial:
            continue
        has_open_source = has_open_source or in_source
        has_commercial = has_commercial or in_commercial

    if has_open_source and has_commercial:
        raise SchemaHistoryBackfillError('mixed open-source and commercial schema history')
    if not has_open_source and not has_commercial:
        raise SchemaHistoryBackfillError('cannot identify schema edition from shared or empty history')

    return 'commercial' if has_commercial else 'open-source'


def _migrations_by_script_checksum(migrations):
    return dict((_script_checksum_key(migration), migration) for migration in migrations)


def _script_checksum_key(migration):
    return migration.script, migration.checksum


def _checksum_predicate(checksum):
    if checksum is None:
        return '`checksum` IS NULL'
    return '`checksum` = %s' % checksum


def _safe_extract_tar(tar, target_dir):
    target_dir = os.path.abspath(target_dir)
    members = tar.getmembers()
    for member in members:
        member_path = os.path.abspath(os.path.join(target_dir, member.name))
        if member_path != target_dir and not member_path.startswith(target_dir + os.sep):
            raise SchemaHistoryBackfillError(
                'unsafe schema map archive path: %s' % member.name
            )
    for member in members:
        try:
            tar.extract(member, target_dir, filter='fully_trusted')
        except TypeError:
            tar.extract(member, target_dir)


def _replace_schema_upgrade_migrations(upgrade_dir, ee_history_dir):
    if not os.path.isdir(upgrade_dir):
        os.makedirs(upgrade_dir)

    for path in glob.glob(os.path.join(upgrade_dir, 'V*.sql')):
        if os.path.isfile(path) or os.path.islink(path):
            os.remove(path)

    for path in glob.glob(os.path.join(ee_history_dir, 'V*.sql')):
        if os.path.isfile(path):
            shutil.copy2(path, os.path.join(upgrade_dir, os.path.basename(path)))


def _copy_missing_checkpoint_migrations(schema_map_dir, upgrade_dir):
    target_dir = os.path.join(schema_map_dir, 'ee')
    if not os.path.isdir(target_dir):
        return

    for path in glob.glob(os.path.join(target_dir, 'V*.sql')):
        if not os.path.islink(path):
            continue

        target_script = os.path.basename(path)
        source_path = _resolve_schema_map_link(schema_map_dir, path)
        if not os.path.isfile(source_path):
            raise SchemaHistoryBackfillError(
                'missing open-source schema migration file referenced by %s' %
                target_script
            )

        target_path = os.path.join(upgrade_dir, target_script)
        if os.path.exists(target_path):
            continue

        shutil.copy2(source_path, target_path)


def _resolve_schema_map_link(schema_map_dir, link_path):
    schema_map_dir = os.path.realpath(schema_map_dir)
    source_path = os.path.realpath(link_path)
    if source_path != schema_map_dir and not source_path.startswith(schema_map_dir + os.sep):
        raise SchemaHistoryBackfillError(
            'unsafe ZSvirt EE schema map link: %s' % os.path.basename(link_path)
        )
    return source_path


def _append_legacy_open_source_checkpoints(checkpoints, source_by_script, target_by_script):
    legacy_pairs = [
        ('V1.0.0__schema.sql', 'V5.0.2__schema.sql'),
    ]
    existing_sources = set([checkpoint.source.script for checkpoint in checkpoints])
    for source_script, target_script in legacy_pairs:
        if source_script in existing_sources:
            continue

        source = source_by_script.get(source_script)
        target = target_by_script.get(target_script)
        if source is None or target is None:
            continue

        checkpoints.append(SchemaCheckpoint(source, target))


def _parse_checksum(checksum):
    if checksum is None:
        return None
    if checksum.upper() == 'NULL' or checksum == '':
        return None
    return int(checksum)
