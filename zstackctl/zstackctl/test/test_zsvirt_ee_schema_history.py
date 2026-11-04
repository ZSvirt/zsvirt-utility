#!/usr/bin/env python
# encoding: utf-8

import os
import shutil
import sys
import tarfile
import tempfile
import unittest

curr_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(os.path.dirname(curr_dir)))

import zstackctl.zsvirt_ee_schema_history as ee_history


class ZSvirtEESchemaHistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.resolved_dir = os.path.join(self.tmpdir, 'resolved')
        self.map_dir = os.path.join(self.tmpdir, 'map')
        self.map_upgrade_dir = os.path.join(self.map_dir, 'upgrade')
        self.map_ee_dir = os.path.join(self.map_dir, 'ee')
        self.ee_config_dir = os.path.join(self.tmpdir, 'WEB-INF', 'classes', 'springConfigXml', 'ee')
        os.makedirs(self.resolved_dir)
        os.makedirs(self.map_upgrade_dir)
        os.makedirs(self.map_ee_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def write_sql(self, directory, name, body):
        path = os.path.join(directory, name)
        with open(path, 'wb') as fd:
            fd.write(body)
        return path

    def write_resolved_sql(self, name, body):
        return self.write_sql(self.resolved_dir, name, body)

    def write_source_sql(self, name, body):
        return self.write_sql(self.map_upgrade_dir, name, body)

    def link_checkpoint(self, target_script, source_script):
        os.symlink(
            os.path.join('..', 'upgrade', source_script),
            os.path.join(self.map_ee_dir, target_script)
        )

    def make_map_archive(self):
        archive_path = os.path.join(self.tmpdir, 'zsvirt-ee-schema-map.tgz')
        tar = tarfile.open(archive_path, 'w:gz')
        try:
            tar.add(self.map_dir, arcname='.')
        finally:
            tar.close()
        return archive_path

    def scan_map(self):
        return ee_history.scan_schema_checkpoint_map(
            self.map_dir,
            ee_history.scan_sql_migrations(self.resolved_dir)
        )

    def test_flyway_version_key_sorts_like_flyway(self):
        versions = ['1.10', '1.0.0', '1.2', '1.0', '5.1.1']

        self.assertEqual(
            sorted(versions, key=ee_history.flyway_version_key),
            ['1.0.0', '1.0', '1.2', '1.10', '5.1.1']
        )
        self.assertEqual(ee_history.flyway_version_key('1.0'), ee_history.flyway_version_key('1.0.0'))

    def test_open_source_schema_backfills_full_history_only_through_checkpoint(self):
        self.write_resolved_sql('V1.0__schema.sql', b'commercial 1.0')
        self.write_resolved_sql('V5.0.0__schema.sql', b'commercial 5.0.0')
        self.write_resolved_sql('V5.1.0__schema.sql', b'commercial 5.1.0')
        self.write_resolved_sql('V5.1.1__schema.sql', b'commercial 5.1.1')
        self.write_resolved_sql('V5.2.0__schema.sql', b'pending commercial migration')
        self.write_source_sql('V1.0.0__schema.sql', b'open source base')
        self.write_sql(self.map_dir, 'V0.5.1__schema.sql', b'open source initial schema')
        self.write_source_sql('V1.0.1.3__schema.sql', b'open source checkpoint')
        self.link_checkpoint('V5.1.1__schema.sql', 'V1.0.1.3__schema.sql')

        migrations = ee_history.scan_sql_migrations(self.resolved_dir)
        source_by_script = dict((m.script, m) for m in self.scan_map().opensource_migrations)
        applied = [
            ee_history.AppliedMigration(0, '0.5.1', 'V0.5.1__schema.sql', source_by_script['V0.5.1__schema.sql'].checksum, 'SQL', 1),
            ee_history.AppliedMigration(
                1, '1.0.0', 'V1.0.0__schema.sql',
                source_by_script['V1.0.0__schema.sql'].checksum, 'SQL', 1
            ),
            ee_history.AppliedMigration(
                2, '1.0.1.3', 'V1.0.1.3__schema.sql',
                source_by_script['V1.0.1.3__schema.sql'].checksum, 'SQL', 1
            ),
        ]

        plan = ee_history.plan_zsvirt_ee_schema_history_backfill(applied, migrations, self.scan_map())

        self.assertEqual(plan.current_source.script, 'V1.0.1.3__schema.sql')
        self.assertEqual(plan.upgrade_path, ee_history.OPEN_SOURCE_TO_EE)
        self.assertEqual(plan.checkpoint.script, 'V5.1.1__schema.sql')
        self.assertEqual(
            [migration.script for migration in plan.ee_history_to_backfill],
            ['V1.0__schema.sql', 'V5.0.0__schema.sql', 'V5.1.0__schema.sql', 'V5.1.1__schema.sql']
        )
        self.assertEqual(
            [migration.script for migration in plan.opensource_history_to_delete],
            ['V0.5.1__schema.sql', 'V1.0.0__schema.sql', 'V1.0.1.3__schema.sql']
        )
        sql = ee_history.build_schema_history_backfill_sql(plan, 'zstack')
        self.assertIn('V0.5.1__schema.sql', sql)
        self.assertNotIn('V5.2.0__schema.sql', sql)
        for migration in plan.ee_history_to_backfill:
            self.assertIn("'%s', %s" % (migration.script, migration.checksum), sql)

        converted = [
            ee_history.AppliedMigration(
                index, migration.version, migration.script,
                migration.checksum, 'SQL', 1
            )
            for index, migration in enumerate(plan.ee_history_to_backfill, 1)
        ]
        retry = ee_history.plan_zsvirt_ee_schema_history_backfill(converted, migrations, self.scan_map())
        self.assertEqual(retry.upgrade_path, ee_history.COMMERCIAL_UPGRADE)
        self.assertIsNone(retry.checkpoint)
        self.assertEqual(retry.ee_history_to_backfill, [])
        self.assertEqual(retry.opensource_history_to_delete, [])
        self.assertIsNone(ee_history.build_schema_history_backfill_sql(retry, 'zstack'))

        pending = migrations[-1]
        converted.append(ee_history.AppliedMigration(
            20, pending.version, pending.script, pending.checksum, 'SQL', 1
        ))
        next_upgrade = ee_history.plan_zsvirt_ee_schema_history_backfill(converted, migrations, self.scan_map())
        self.assertEqual(next_upgrade.upgrade_path, ee_history.COMMERCIAL_UPGRADE)
        self.assertIsNone(next_upgrade.checkpoint)
        self.assertEqual(next_upgrade.ee_history_to_backfill, [])
        self.assertEqual(next_upgrade.opensource_history_to_delete, [])

        with self.assertRaises(ee_history.SchemaHistoryBackfillError) as raised:
            ee_history.plan_zsvirt_ee_schema_history_backfill(
                converted, self.scan_map().opensource_migrations, None)
        self.assertIn('commercial-to-open-source upgrade is not supported', str(raised.exception))

    def test_open_source_1_0_0_uses_legacy_5_0_2_checkpoint_without_symlink(self):
        self.write_resolved_sql('V5.0.0__schema.sql', b'commercial 5.0.0')
        self.write_resolved_sql('V5.0.2__schema.sql', b'')
        self.write_resolved_sql('V5.1.0__schema.sql', b'commercial 5.1.0')
        self.write_source_sql('V1.0.0__schema.sql', b'open source 1.0.0')

        migrations = ee_history.scan_sql_migrations(self.resolved_dir)
        opensource_migrations = ee_history.scan_sql_migrations(self.map_upgrade_dir)
        applied = [
            ee_history.AppliedMigration(
                1, '1.0.0', 'V1.0.0__schema.sql',
                opensource_migrations[0].checksum, 'SQL', 1
            ),
        ]

        plan = ee_history.plan_zsvirt_ee_schema_history_backfill(applied, migrations, self.scan_map())

        self.assertEqual(plan.current_source.script, 'V1.0.0__schema.sql')
        self.assertEqual(plan.checkpoint.script, 'V5.0.2__schema.sql')
        self.assertEqual(
            [migration.script for migration in plan.ee_history_to_backfill],
            ['V5.0.0__schema.sql', 'V5.0.2__schema.sql']
        )
        self.assertEqual(
            [migration.script for migration in plan.opensource_history_to_delete],
            ['V1.0.0__schema.sql']
        )

    def test_open_source_upgrade_without_ee_package_never_converts_history(self):
        self.write_resolved_sql('V1.0.0__schema.sql', b'open source base')
        self.write_resolved_sql('V1.0.1__schema.sql', b'new open source SQL')
        base = ee_history.scan_sql_migrations(self.resolved_dir)[0]
        applied = [ee_history.AppliedMigration(1, base.version, base.script, base.checksum, 'SQL', 1)]
        plan = ee_history.plan_zsvirt_ee_schema_history_backfill(
            applied, ee_history.scan_sql_migrations(self.resolved_dir), None)
        self.assertEqual(plan.upgrade_path, ee_history.OPEN_SOURCE_UPGRADE)
        self.assertIsNone(ee_history.build_schema_history_backfill_sql(plan, 'zstack'))

    def test_open_source_target_rejects_old_and_new_commercial_history(self):
        self.write_resolved_sql('V1.0.0__schema.sql', b'open source base')
        migrations = ee_history.scan_sql_migrations(self.resolved_dir)
        source = migrations[0]
        recognized = ee_history.AppliedMigration(1, source.version, source.script, source.checksum, 'SQL', 1)
        commercial_rows = [
            ee_history.AppliedMigration(2, '4.8.0', 'V4.8.0__schema.sql', 42, 'SQL', 1),
            ee_history.AppliedMigration(2, '5.1.1', 'V5.1.1__schema.sql', 42, 'SQL', 1),
            ee_history.AppliedMigration(2, source.version, source.script, source.checksum + 1, 'SQL', 1),
        ]
        for commercial in commercial_rows:
            for applied in ([commercial], [recognized, commercial]):
                before = list(applied)
                for retry in range(2):
                    with self.assertRaises(ee_history.SchemaHistoryBackfillError) as raised:
                        ee_history.plan_zsvirt_ee_schema_history_backfill(applied, migrations, None)
                    self.assertIn('commercial-to-open-source upgrade is not supported', str(raised.exception))
                    self.assertIn(commercial.script, str(raised.exception))
                    self.assertEqual(applied, before)

    def test_open_source_target_rejects_failed_or_unidentifiable_history(self):
        self.write_resolved_sql('V1.0.0__schema.sql', b'open source base')
        migrations = ee_history.scan_sql_migrations(self.resolved_dir)
        source = migrations[0]
        histories = [[], [ee_history.AppliedMigration(1, source.version, source.script, source.checksum, 'SQL', 0)]]
        for applied in histories:
            with self.assertRaises(ee_history.SchemaHistoryBackfillError):
                ee_history.plan_zsvirt_ee_schema_history_backfill(applied, migrations, None)

    def test_open_source_target_includes_root_initializer_without_executing_it_again(self):
        self.write_sql(self.map_dir, 'V0.5.1__schema.sql', b'open source initial schema')
        self.write_source_sql('V1.0.0__schema.sql', b'open source base')
        self.write_source_sql('V1.0.1__schema.sql', b'pending migration')
        migrations = ee_history.scan_open_source_migrations(self.map_dir)
        applied = [ee_history.AppliedMigration(rank, m.version, m.script, m.checksum, 'SQL', 1)
                   for rank, m in enumerate(migrations[:2], 1)]
        self.assertEqual([m.script for m in applied], ['V0.5.1__schema.sql', 'V1.0.0__schema.sql'])
        plan = ee_history.plan_zsvirt_ee_schema_history_backfill(applied, migrations, None)
        self.assertEqual(plan.upgrade_path, ee_history.OPEN_SOURCE_UPGRADE)
        self.assertIsNone(ee_history.build_schema_history_backfill_sql(plan, 'zstack'))
        self.assertFalse(os.path.exists(os.path.join(self.map_upgrade_dir, 'V0.5.1__schema.sql')))

    def test_both_open_source_upgrade_paths_reject_pending_migrations_below_database_maximum(self):
        self.write_sql(self.map_dir, 'V0.5.1__schema.sql', b'initial')
        self.write_source_sql('V1.0.0__schema.sql', b'base')
        self.write_source_sql('V1.0.1__schema.sql', b'middle')
        self.write_source_sql('V1.0.2__schema.sql', b'current')
        self.write_resolved_sql('V5.1.1__schema.sql', b'commercial checkpoint')
        self.link_checkpoint('V5.1.1__schema.sql', 'V1.0.2__schema.sql')
        schema_map = self.scan_map()
        sources = schema_map.opensource_migrations
        commercial = ee_history.scan_sql_migrations(self.resolved_dir)
        for missing_index in (0, 2):
            applied = [ee_history.AppliedMigration(rank, m.version, m.script, m.checksum, 'SQL', 1)
                       for rank, m in enumerate(sources, 1) if rank - 1 != missing_index]
            before = list(applied)
            for resolved, mapping in ((sources, None), (commercial, schema_map)):
                for retry in range(2):
                    with self.assertRaises(ee_history.SchemaHistoryBackfillError) as raised:
                        ee_history.plan_zsvirt_ee_schema_history_backfill(applied, resolved, mapping)
                    self.assertIn('pending open-source migration', str(raised.exception))
                    self.assertIn(sources[missing_index].script, str(raised.exception))
                    self.assertIn('1.0.2', str(raised.exception))
                    self.assertEqual(applied, before)

    def test_both_open_source_upgrade_paths_reject_pending_equivalent_versions(self):
        self.write_source_sql('V1.0__schema.sql', b'already applied')
        self.write_source_sql('V1.0.0__extra.sql', b'unapplied equivalent version')
        self.write_resolved_sql('V5.1.1__schema.sql', b'commercial checkpoint')
        self.link_checkpoint('V5.1.1__schema.sql', 'V1.0__schema.sql')
        schema_map = self.scan_map()
        source = next(m for m in schema_map.opensource_migrations if m.script == 'V1.0__schema.sql')
        applied = [ee_history.AppliedMigration(1, source.version, source.script, source.checksum, 'SQL', 1)]
        for resolved, mapping in ((schema_map.opensource_migrations, None),
                                  (ee_history.scan_sql_migrations(self.resolved_dir), schema_map)):
            with self.assertRaises(ee_history.SchemaHistoryBackfillError) as raised:
                ee_history.plan_zsvirt_ee_schema_history_backfill(applied, resolved, mapping)
            self.assertIn('pending open-source migration', str(raised.exception))
            self.assertIn('V1.0.0__extra.sql', str(raised.exception))

    def test_pending_source_versions_use_database_maximum_not_rank_or_commercial_checkpoint(self):
        for version in ('1.0', '1.9', '1.10'):
            self.write_source_sql('V%s__schema.sql' % version, version.encode('ascii'))
        self.write_resolved_sql('V5.1.1__schema.sql', b'commercial current')
        self.write_resolved_sql('V5.2.0__schema.sql', b'commercial pending')
        self.link_checkpoint('V5.1.1__schema.sql', 'V1.9__schema.sql')
        self.link_checkpoint('V5.2.0__schema.sql', 'V1.10__schema.sql')
        schema_map = self.scan_map()
        sources = schema_map.opensource_migrations
        for applied_count in (2, 3):
            applied = [ee_history.AppliedMigration(rank, m.version, m.script, m.checksum, 'SQL', 1)
                       for rank, m in enumerate(reversed(sources[:applied_count]), 1)]
            for resolved, mapping, expected_path in (
                    (sources, None, ee_history.OPEN_SOURCE_UPGRADE),
                    (ee_history.scan_sql_migrations(self.resolved_dir), schema_map, ee_history.OPEN_SOURCE_TO_EE)):
                plan = ee_history.plan_zsvirt_ee_schema_history_backfill(applied, resolved, mapping)
                self.assertEqual(plan.upgrade_path, expected_path)
                if mapping is not None:
                    self.assertEqual(plan.checkpoint.version, '5.1.1' if applied_count == 2 else '5.2.0')

    def test_commercial_upgrade_does_not_validate_pending_open_source_versions(self):
        self.write_source_sql('V1.0.0__schema.sql', b'open source history')
        self.write_resolved_sql('V5.1.1__schema.sql', b'commercial history')
        commercial = ee_history.scan_sql_migrations(self.resolved_dir)
        m = commercial[0]
        applied = [ee_history.AppliedMigration(1, m.version, m.script, m.checksum, 'SQL', 1)]
        plan = ee_history.plan_zsvirt_ee_schema_history_backfill(applied, commercial, self.scan_map())
        self.assertEqual(plan.upgrade_path, ee_history.COMMERCIAL_UPGRADE)
        self.assertIsNone(ee_history.build_schema_history_backfill_sql(plan, 'zstack'))

    def test_conversion_rejects_unknown_older_sql_instead_of_leaving_open_source_history(self):
        self.write_resolved_sql('V5.1.1__schema.sql', b'commercial checkpoint')
        self.write_source_sql('V1.0.1.3__schema.sql', b'open source checkpoint')
        self.link_checkpoint('V5.1.1__schema.sql', 'V1.0.1.3__schema.sql')
        source = ee_history.scan_sql_migrations(self.map_upgrade_dir)[0]
        applied = [
            ee_history.AppliedMigration(1, '1.0.0.9', 'V1.0.0.9__schema.sql', 42, 'SQL', 1),
            ee_history.AppliedMigration(2, source.version, source.script, source.checksum, 'SQL', 1),
        ]
        with self.assertRaises(ee_history.SchemaHistoryBackfillError):
            ee_history.plan_zsvirt_ee_schema_history_backfill(
                applied, ee_history.scan_sql_migrations(self.resolved_dir), self.scan_map())

    def test_conversion_cannot_preserve_initial_history_matching_only_commercial_sql(self):
        self.write_sql(self.map_dir, 'V0.5.1__schema.sql', b'open source initial schema')
        self.write_resolved_sql('V0.5.1__schema.sql', b'different commercial initial schema')
        self.write_resolved_sql('V5.1.1__schema.sql', b'commercial checkpoint')
        self.write_source_sql('V1.0.1.3__schema.sql', b'open source checkpoint')
        self.link_checkpoint('V5.1.1__schema.sql', 'V1.0.1.3__schema.sql')
        commercial = ee_history.scan_sql_migrations(self.resolved_dir)
        initial = commercial[0]
        source = ee_history.scan_sql_migrations(self.map_upgrade_dir)[0]
        applied = [ee_history.AppliedMigration(rank, m.version, m.script, m.checksum, 'SQL', 1)
                   for rank, m in enumerate((initial, source), 1)]
        with self.assertRaises(ee_history.SchemaHistoryBackfillError) as raised:
            ee_history.plan_zsvirt_ee_schema_history_backfill(applied, commercial, self.scan_map())
        self.assertIn('mixed open-source and commercial schema history', str(raised.exception))

    def test_conversion_rejects_failed_sql_before_rewriting_successful_history(self):
        self.write_resolved_sql('V5.1.1__schema.sql', b'commercial checkpoint')
        self.write_source_sql('V1.0.1.3__schema.sql', b'open source checkpoint')
        self.link_checkpoint('V5.1.1__schema.sql', 'V1.0.1.3__schema.sql')
        source = ee_history.scan_sql_migrations(self.map_upgrade_dir)[0]
        applied = [
            ee_history.AppliedMigration(1, source.version, source.script, source.checksum, 'SQL', 1),
            ee_history.AppliedMigration(2, '1.0.2', 'V1.0.2__schema.sql', 42, 'SQL', 0),
        ]
        with self.assertRaises(ee_history.SchemaHistoryBackfillError):
            ee_history.plan_zsvirt_ee_schema_history_backfill(
                applied, ee_history.scan_sql_migrations(self.resolved_dir), self.scan_map())

    def test_out_of_order_source_history_uses_highest_version_and_removes_all_source_rows(self):
        self.write_resolved_sql('V5.1.0__schema.sql', b'commercial base')
        self.write_resolved_sql('V5.1.1__schema.sql', b'commercial checkpoint')
        self.write_source_sql('V1.0.1__schema.sql', b'older source')
        self.write_source_sql('V1.0.1.3__schema.sql', b'newer source')
        self.link_checkpoint('V5.1.0__schema.sql', 'V1.0.1__schema.sql')
        self.link_checkpoint('V5.1.1__schema.sql', 'V1.0.1.3__schema.sql')
        sources = ee_history.scan_sql_migrations(self.map_upgrade_dir)
        applied = [ee_history.AppliedMigration(
            rank, source.version, source.script, source.checksum, 'SQL', 1
        ) for rank, source in enumerate(reversed(sources), 1)]
        plan = ee_history.plan_zsvirt_ee_schema_history_backfill(
            applied, ee_history.scan_sql_migrations(self.resolved_dir), self.scan_map())
        self.assertEqual(plan.checkpoint.version, '5.1.1')
        self.assertEqual(plan.opensource_history_to_delete, applied)

    def test_open_source_initial_history_cannot_be_ignored_with_commercial_history(self):
        self.write_source_sql('V0.5.1__schema.sql', b'open source initial schema')
        self.write_resolved_sql('V4.8.0__schema.sql', b'commercial 4.8.0')
        self.write_resolved_sql('V5.1.0__schema.sql', b'commercial 5.1.0')
        source = ee_history.scan_sql_migrations(self.map_upgrade_dir)[0]
        commercial = ee_history.scan_sql_migrations(self.resolved_dir)
        applied = [ee_history.AppliedMigration(1, source.version, source.script, source.checksum, 'SQL', 1)]
        applied.extend(ee_history.AppliedMigration(
            rank, m.version, m.script, m.checksum, 'SQL', 1
        ) for rank, m in enumerate(commercial, 2))
        with self.assertRaises(ee_history.SchemaHistoryBackfillError) as raised:
            ee_history.plan_zsvirt_ee_schema_history_backfill(applied, commercial, self.scan_map())
        self.assertIn('mixed open-source and commercial schema history', str(raised.exception))

    def test_same_version_with_different_content_can_still_be_open_source(self):
        self.write_source_sql('V5.1.0__schema.sql', b'open source 5.1.0')
        self.write_resolved_sql('V5.1.0__schema.sql', b'old commercial 5.1.0')
        self.write_resolved_sql('V9.0.0__schema.sql', b'new commercial checkpoint')
        self.link_checkpoint('V9.0.0__schema.sql', 'V5.1.0__schema.sql')
        source = ee_history.scan_sql_migrations(self.map_upgrade_dir)[0]
        applied = [ee_history.AppliedMigration(1, source.version, source.script, source.checksum, 'SQL', 1)]
        plan = ee_history.plan_zsvirt_ee_schema_history_backfill(
            applied, ee_history.scan_sql_migrations(self.resolved_dir), self.scan_map())
        self.assertEqual(plan.upgrade_path, ee_history.OPEN_SOURCE_TO_EE)
        self.assertEqual(plan.checkpoint.version, '9.0.0')
        self.assertEqual(plan.opensource_history_to_delete, applied)

    def test_identical_source_and_commercial_identity_is_rejected_as_ambiguous(self):
        self.write_source_sql('V5.0.1__schema.sql', b'')
        self.write_resolved_sql('V5.0.1__schema.sql', b'')
        self.write_resolved_sql('V9.0.0__schema.sql', b'checkpoint')
        self.link_checkpoint('V9.0.0__schema.sql', 'V5.0.1__schema.sql')
        applied = [ee_history.AppliedMigration(1, '5.0.1', 'V5.0.1__schema.sql', 0, 'SQL', 1)]
        with self.assertRaises(ee_history.SchemaHistoryBackfillError):
            ee_history.plan_zsvirt_ee_schema_history_backfill(
                applied, ee_history.scan_sql_migrations(self.resolved_dir), self.scan_map())

    def test_copy_schema_callbacks_to_upgrade_dir_copies_root_callbacks_into_flyway_location(self):
        db_dir = os.path.join(self.tmpdir, 'runtime-db')
        upgrade_dir = os.path.join(db_dir, 'upgrade')
        os.makedirs(upgrade_dir)
        self.write_sql(db_dir, 'beforeMigrate.sql', b'before migrate from root')
        self.write_sql(db_dir, 'beforeValidate.sql', b'before validate from root')

        ee_history.copy_schema_callbacks_to_upgrade_dir(db_dir, upgrade_dir)

        with open(os.path.join(upgrade_dir, 'beforeMigrate.sql'), 'rb') as fd:
            before_migrate = fd.read()
        with open(os.path.join(upgrade_dir, 'beforeValidate.sql'), 'rb') as fd:
            before_validate = fd.read()
        self.assertEqual(before_migrate, b'before migrate from root')
        self.assertEqual(before_validate, b'before validate from root')

    def test_ee_config_requires_map_even_when_both_schema_payloads_are_missing(self):
        os.makedirs(self.ee_config_dir)
        with self.assertRaises(ee_history.SchemaHistoryBackfillError) as raised:
            ee_history.prepare_zsvirt_ee_schema_upgrade_dir(
                self.resolved_dir, os.path.join(self.tmpdir, 'missing-history'),
                os.path.join(self.tmpdir, 'missing-map.tgz'), self.ee_config_dir)
        self.assertIn('missing ZSvirt EE schema map archive', str(raised.exception))

    def test_ee_config_requires_history_when_map_exists(self):
        os.makedirs(self.ee_config_dir)
        with self.assertRaises(ee_history.SchemaHistoryBackfillError) as raised:
            ee_history.prepare_zsvirt_ee_schema_upgrade_dir(
                self.resolved_dir, os.path.join(self.tmpdir, 'missing-history'),
                self.make_map_archive(), self.ee_config_dir)
        self.assertIn('missing ZSvirt EE schema history directory', str(raised.exception))

    def test_open_source_package_does_not_convert_sql_even_with_ee_schema_payloads(self):
        self.write_resolved_sql('V1.0.0__schema.sql', b'open source')
        history_dir = os.path.join(self.tmpdir, 'ee-history')
        os.makedirs(history_dir)
        self.write_sql(history_dir, 'V5.0.2__schema.sql', b'commercial')
        schema_map = ee_history.prepare_zsvirt_ee_schema_upgrade_dir(
            self.resolved_dir, history_dir, self.make_map_archive(), self.ee_config_dir)
        self.assertIsNone(schema_map)
        self.assertEqual(os.listdir(self.resolved_dir), ['V1.0.0__schema.sql'])
        with open(os.path.join(self.resolved_dir, 'V1.0.0__schema.sql'), 'rb') as fd:
            self.assertEqual(fd.read(), b'open source')

    def test_prepare_ee_schema_upgrade_dir_uses_history_and_symlink_map(self):
        os.makedirs(self.ee_config_dir)
        db_dir = os.path.join(self.tmpdir, 'runtime-db-for-prepare')
        upgrade_dir = os.path.join(db_dir, 'upgrade')
        history_dir = os.path.join(self.tmpdir, 'ee-history')
        os.makedirs(upgrade_dir)
        os.makedirs(history_dir)
        self.write_sql(db_dir, 'beforeMigrate.sql', b'before migrate')
        self.write_sql(db_dir, 'beforeValidate.sql', b'before validate')
        self.write_sql(upgrade_dir, 'V1.0.0__schema.sql', b'stale open source')
        self.write_sql(history_dir, 'V5.0.0__schema.sql', b'history 5.0.0')
        self.write_sql(history_dir, 'V5.0.2__schema.sql', b'history 5.0.2')
        self.write_sql(self.map_dir, 'V0.5.1__schema.sql', b'initial open source schema')
        self.write_source_sql('V1.0.0__schema.sql', b'open source 1.0.0')
        self.write_source_sql('V1.0.1.3__schema.sql', b'open source 1.0.1.3')
        self.link_checkpoint('V5.1.1__schema.sql', 'V1.0.1.3__schema.sql')

        ee_history.copy_schema_callbacks_to_upgrade_dir(db_dir, upgrade_dir)
        schema_map = ee_history.prepare_zsvirt_ee_schema_upgrade_dir(
            upgrade_dir,
            history_dir,
            self.make_map_archive(),
            self.ee_config_dir,
        )

        self.assertTrue(os.path.exists(os.path.join(upgrade_dir, 'beforeMigrate.sql')))
        self.assertTrue(os.path.exists(os.path.join(upgrade_dir, 'beforeValidate.sql')))
        self.assertFalse(os.path.exists(os.path.join(upgrade_dir, 'V1.0.0__schema.sql')))
        self.assertFalse(os.path.exists(os.path.join(upgrade_dir, 'V0.5.1__schema.sql')))
        self.assertIn('V0.5.1__schema.sql', [m.script for m in schema_map.opensource_migrations])
        with open(os.path.join(upgrade_dir, 'V5.0.2__schema.sql'), 'rb') as fd:
            v5_0_2_body = fd.read()
        with open(os.path.join(upgrade_dir, 'V5.1.1__schema.sql'), 'rb') as fd:
            v5_1_1_body = fd.read()
        self.assertEqual(
            v5_0_2_body,
            b'history 5.0.2'
        )
        self.assertEqual(
            v5_1_1_body,
            b'open source 1.0.1.3'
        )
        self.assertEqual(
            [(checkpoint.source.script, checkpoint.target.script) for checkpoint in schema_map.checkpoints],
            [
                ('V1.0.0__schema.sql', 'V5.0.2__schema.sql'),
                ('V1.0.1.3__schema.sql', 'V5.1.1__schema.sql'),
            ]
        )

    def test_commercial_schema_needs_no_backfill_when_latest_history_is_resolved(self):
        self.write_resolved_sql('V5.0.0__schema.sql', b'commercial 5.0.0')
        self.write_resolved_sql('V5.1.0__schema.sql', b'commercial 5.1.0')
        self.write_source_sql('V1.0.1.3__schema.sql', b'open source checkpoint')
        self.link_checkpoint('V5.1.0__schema.sql', 'V1.0.1.3__schema.sql')

        migrations = ee_history.scan_sql_migrations(self.resolved_dir)
        applied = [
            ee_history.AppliedMigration(1, '5.0.0', 'V5.0.0__schema.sql', migrations[0].checksum, 'SQL', 1),
            ee_history.AppliedMigration(2, '5.1.0', 'V5.1.0__schema.sql', migrations[1].checksum, 'SQL', 1),
        ]

        plan = ee_history.plan_zsvirt_ee_schema_history_backfill(applied, migrations, self.scan_map())

        self.assertEqual(plan.ee_history_to_backfill, [])
        self.assertEqual(plan.opensource_history_to_delete, [])
        self.assertIsNone(plan.current_source)
        self.assertIsNone(plan.checkpoint)

    def test_commercial_schema_ignores_pruned_cloud_history_after_retained_history_is_applied(self):
        self.write_resolved_sql('V4.8.0__schema.sql', b'commercial 4.8.0')
        self.write_resolved_sql('V5.1.0__schema.sql', b'commercial 5.1.0')

        migrations = ee_history.scan_sql_migrations(self.resolved_dir)
        target_by_script = dict((m.script, m) for m in migrations)
        applied = [
            ee_history.AppliedMigration(1, '3.10.0', 'V3.10.0__schema.sql', 42, 'SQL', 1),
            ee_history.AppliedMigration(
                2, '4.8.0', 'V4.8.0__schema.sql',
                target_by_script['V4.8.0__schema.sql'].checksum, 'SQL', 1
            ),
            ee_history.AppliedMigration(
                3, '5.1.0', 'V5.1.0__schema.sql',
                target_by_script['V5.1.0__schema.sql'].checksum, 'SQL', 1
            ),
        ]

        plan = ee_history.plan_zsvirt_ee_schema_history_backfill(applied, migrations, self.scan_map())

        self.assertEqual(plan.ee_history_to_backfill, [])
        self.assertEqual(plan.opensource_history_to_delete, [])
        self.assertIsNone(plan.current_source)
        self.assertIsNone(plan.checkpoint)

    def test_commercial_schema_with_only_pruned_cloud_history_still_fails(self):
        self.write_resolved_sql('V4.8.0__schema.sql', b'commercial 4.8.0')
        self.write_resolved_sql('V5.1.0__schema.sql', b'commercial 5.1.0')
        applied = [
            ee_history.AppliedMigration(1, '3.10.0', 'V3.10.0__schema.sql', 42, 'SQL', 1),
        ]

        with self.assertRaises(ee_history.SchemaHistoryBackfillError):
            ee_history.plan_zsvirt_ee_schema_history_backfill(applied, ee_history.scan_sql_migrations(self.resolved_dir), self.scan_map())

    def test_checkpoint_mapping_uses_symlink_instead_of_equal_checksum(self):
        self.write_resolved_sql('V5.2.0__schema.sql', b'commercial 5.2.0')
        self.write_resolved_sql('V5.3.0__schema.sql', b'commercial checkpoint body')
        self.write_source_sql('V789.0.0__schema.sql', b'open source checkpoint body')
        self.link_checkpoint('V5.3.0__schema.sql', 'V789.0.0__schema.sql')

        migrations = ee_history.scan_sql_migrations(self.resolved_dir)
        opensource_migrations = ee_history.scan_sql_migrations(self.map_upgrade_dir)
        source_by_script = dict((m.script, m) for m in opensource_migrations)
        applied = [
            ee_history.AppliedMigration(
                1, '789.0.0', 'V789.0.0__schema.sql',
                source_by_script['V789.0.0__schema.sql'].checksum, 'SQL', 1
            ),
        ]

        plan = ee_history.plan_zsvirt_ee_schema_history_backfill(applied, migrations, self.scan_map())

        self.assertEqual(plan.current_source.script, 'V789.0.0__schema.sql')
        self.assertEqual(plan.checkpoint.script, 'V5.3.0__schema.sql')
        self.assertEqual(
            [migration.script for migration in plan.ee_history_to_backfill],
            ['V5.2.0__schema.sql', 'V5.3.0__schema.sql']
        )
        self.assertEqual(
            [migration.script for migration in plan.opensource_history_to_delete],
            ['V789.0.0__schema.sql']
        )

    def test_open_source_schema_without_checkpoint_fails_loudly(self):
        self.write_resolved_sql('V5.0.0__schema.sql', b'commercial 5.0.0')
        self.write_source_sql('V1.0.1.3__schema.sql', b'open source checkpoint')
        migrations = ee_history.scan_sql_migrations(self.resolved_dir)
        opensource_migrations = ee_history.scan_sql_migrations(self.map_upgrade_dir)
        applied = [
            ee_history.AppliedMigration(
                1, '1.0.1.3', 'V1.0.1.3__schema.sql',
                opensource_migrations[0].checksum, 'SQL', 1
            ),
        ]

        with self.assertRaises(ee_history.SchemaHistoryBackfillError):
            ee_history.plan_zsvirt_ee_schema_history_backfill(applied, migrations, self.scan_map())

    def test_parse_schema_version_output_keeps_null_checksum(self):
        output = '1\t1.0.1.3\tV1.0.1.3__schema.sql\tNULL\tSQL\t1\n'

        migrations = ee_history.parse_applied_migrations(output)

        self.assertEqual(
            migrations,
            [ee_history.AppliedMigration(1, '1.0.1.3', 'V1.0.1.3__schema.sql', None, 'SQL', 1)]
        )

    def test_build_backfill_sql_restarts_both_ranks_and_escapes_installed_by(self):
        self.write_resolved_sql('V5.0.0__schema.sql', b'commercial base')
        self.write_resolved_sql('V5.0.2__schema.sql', b'')
        self.write_resolved_sql('V5.1.1__schema.sql', b'commercial checkpoint')
        self.write_source_sql('V1.0.1.3__schema.sql', b'open source checkpoint')
        self.link_checkpoint('V5.1.1__schema.sql', 'V1.0.1.3__schema.sql')

        migrations = ee_history.scan_sql_migrations(self.resolved_dir)
        opensource_migrations = ee_history.scan_sql_migrations(self.map_upgrade_dir)
        applied = [
            ee_history.AppliedMigration(
                7, '1.0.1.3', 'V1.0.1.3__schema.sql',
                opensource_migrations[0].checksum, 'SQL', 1
            ),
        ]
        plan = ee_history.plan_zsvirt_ee_schema_history_backfill(applied, migrations, self.scan_map())

        sql = ee_history.build_schema_history_backfill_sql(plan, "z'stack")

        self.assertIn("START TRANSACTION;\n", sql)
        self.assertIn("(1, 1, '5.0.0', 'schema', 'SQL', 'V5.0.0__schema.sql'", sql)
        self.assertIn("(2, 2, '5.0.2', 'schema', 'SQL', 'V5.0.2__schema.sql'", sql)
        self.assertIn("(3, 3, '5.1.1', 'schema', 'SQL', 'V5.1.1__schema.sql'", sql)
        self.assertIn("DELETE FROM `schema_version`", sql)
        self.assertIn("'V1.0.1.3__schema.sql'", sql)
        self.assertIn("'z''stack'", sql)
        self.assertTrue(sql.index("DELETE FROM `schema_version`") < sql.index("INSERT INTO `schema_version`"))
        self.assertTrue(sql.endswith('COMMIT;'))

    def test_commercial_history_does_not_infer_a_conversion_checkpoint(self):
        self.write_resolved_sql('V5.0.0__schema.sql', b'commercial 5.0.0')
        self.write_resolved_sql('V5.1.1__schema.sql', b'commercial checkpoint')
        self.write_resolved_sql('V5.2.0__schema.sql', b'commercial 5.2.0')
        self.write_source_sql('V1.0.1.3__schema.sql', b'open source checkpoint')
        self.link_checkpoint('V5.1.1__schema.sql', 'V1.0.1.3__schema.sql')

        migrations = ee_history.scan_sql_migrations(self.resolved_dir)
        target_by_script = dict((m.script, m) for m in migrations)
        applied = [
            ee_history.AppliedMigration(
                7, '5.1.1', 'V5.1.1__schema.sql',
                target_by_script['V5.1.1__schema.sql'].checksum, 'SQL', 1
            ),
        ]

        plan = ee_history.plan_zsvirt_ee_schema_history_backfill(applied, migrations, self.scan_map())

        self.assertIsNone(plan.current_source)
        self.assertIsNone(plan.checkpoint)
        self.assertEqual(plan.ee_history_to_backfill, [])
        self.assertEqual(plan.opensource_history_to_delete, [])

    def test_mixed_exclusive_histories_are_rejected_regardless_of_install_order(self):
        self.write_resolved_sql('V5.0.0__schema.sql', b'commercial base')
        self.write_resolved_sql('V5.1.1__schema.sql', b'commercial checkpoint')
        self.write_source_sql('V1.0.1.3__schema.sql', b'open source checkpoint')
        self.link_checkpoint('V5.1.1__schema.sql', 'V1.0.1.3__schema.sql')
        migrations = ee_history.scan_sql_migrations(self.resolved_dir)
        source = ee_history.scan_sql_migrations(self.map_upgrade_dir)[0]
        base = migrations[0]
        applied = [
            ee_history.AppliedMigration(1, base.version, base.script, base.checksum, 'SQL', 1),
            ee_history.AppliedMigration(2, source.version, source.script, source.checksum, 'SQL', 1),
        ]
        for history in (applied, list(reversed(applied))):
            history = [m._replace(installed_rank=rank) for rank, m in enumerate(history, 1)]
            with self.assertRaises(ee_history.SchemaHistoryBackfillError):
                ee_history.plan_zsvirt_ee_schema_history_backfill(history, migrations, self.scan_map())

    def test_commercial_checksum_mismatch_is_not_accepted_by_filename(self):
        self.write_resolved_sql('V5.1.0__schema.sql', b'commercial')
        migrations = ee_history.scan_sql_migrations(self.resolved_dir)
        m = migrations[0]
        applied = [ee_history.AppliedMigration(1, m.version, m.script, m.checksum + 1, 'SQL', 1)]
        with self.assertRaises(ee_history.SchemaHistoryBackfillError):
            ee_history.plan_zsvirt_ee_schema_history_backfill(applied, migrations, self.scan_map())

    def test_identified_commercial_history_leaves_unmatched_sql_to_flyway(self):
        self.write_resolved_sql('V4.8.0__schema.sql', b'commercial base')
        self.write_resolved_sql('V5.1.0__schema.sql', b'commercial current')
        migrations = ee_history.scan_sql_migrations(self.resolved_dir)
        base, current = migrations
        recognized = ee_history.AppliedMigration(1, base.version, base.script, base.checksum, 'SQL', 1)
        unmatched = [
            ee_history.AppliedMigration(2, '3.10.0', 'V3.10.0__schema.sql', 42, 'SQL', 1),
            ee_history.AppliedMigration(2, '4.9.0', 'V4.9.0__schema.sql', 42, 'SQL', 1),
            ee_history.AppliedMigration(2, current.version, current.script, current.checksum + 1, 'SQL', 1),
        ]
        for migration in unmatched:
            applied = [recognized, migration]
            plan = ee_history.plan_zsvirt_ee_schema_history_backfill(applied, migrations, self.scan_map())
            self.assertEqual(plan.upgrade_path, ee_history.COMMERCIAL_UPGRADE)
            self.assertEqual(plan.ee_history_to_backfill, [])
            self.assertEqual(plan.opensource_history_to_delete, [])
            self.assertIsNone(ee_history.build_schema_history_backfill_sql(plan, 'zstack'))
            self.assertEqual(applied, [recognized, migration])

    def test_identified_open_source_history_still_requires_every_row_before_conversion(self):
        self.write_source_sql('V1.0.0__schema.sql', b'open source base')
        self.write_source_sql('V1.0.1.3__schema.sql', b'open source current')
        self.write_resolved_sql('V5.1.1__schema.sql', b'commercial checkpoint')
        self.link_checkpoint('V5.1.1__schema.sql', 'V1.0.1.3__schema.sql')
        schema_map = self.scan_map()
        base, current = schema_map.opensource_migrations
        source_by_key = dict(((m.script, m.checksum), m) for m in schema_map.opensource_migrations)
        migrations = ee_history.scan_sql_migrations(self.resolved_dir)
        unmatched = [
            ee_history.AppliedMigration(1, '0.9.9', 'V0.9.9__schema.sql', 42, 'SQL', 1),
            ee_history.AppliedMigration(1, base.version, base.script, base.checksum + 1, 'SQL', 1),
        ]
        for migration in unmatched:
            applied = [migration, ee_history.AppliedMigration(
                2, current.version, current.script, current.checksum, 'SQL', 1)]
            self.assertEqual(ee_history._classify_schema_history(applied, source_by_key, migrations), 'open-source')
            with self.assertRaises(ee_history.SchemaHistoryBackfillError) as raised:
                ee_history.plan_zsvirt_ee_schema_history_backfill(applied, migrations, schema_map)
            self.assertIn('cannot convert all open-source history', str(raised.exception))

    def test_open_source_initial_sql_identifies_open_source_history(self):
        self.write_sql(self.map_dir, 'V0.5.1__schema.sql', b'open source initial schema')
        self.write_resolved_sql('V5.1.1__schema.sql', b'commercial')
        source = self.scan_map().opensource_migrations[0]
        applied = [ee_history.AppliedMigration(
            1, source.version, source.script, source.checksum, 'SQL', 1)]
        source_by_key = {(source.script, source.checksum): source}
        self.assertEqual(
            ee_history._classify_schema_history(
                applied, source_by_key, ee_history.scan_sql_migrations(self.resolved_dir)),
            'open-source')

    def test_history_without_edition_evidence_is_rejected(self):
        self.write_source_sql('V5.0.1__schema.sql', b'')
        self.write_resolved_sql('V5.0.1__schema.sql', b'')
        self.write_resolved_sql('V5.1.1__schema.sql', b'commercial')
        histories = [[]]
        for m in ee_history.scan_sql_migrations(self.map_upgrade_dir):
            histories.append([ee_history.AppliedMigration(1, m.version, m.script, m.checksum, 'SQL', 1)])
        for history in histories:
            with self.assertRaises(ee_history.SchemaHistoryBackfillError):
                ee_history.plan_zsvirt_ee_schema_history_backfill(
                    history, ee_history.scan_sql_migrations(self.resolved_dir), self.scan_map())

    def test_ee_upgrade_rejects_baseline_even_with_recognized_sql_history(self):
        self.write_source_sql('V1.0.1.3__schema.sql', b'open source')
        self.write_resolved_sql('V5.1.1__schema.sql', b'commercial')
        self.link_checkpoint('V5.1.1__schema.sql', 'V1.0.1.3__schema.sql')
        source = ee_history.scan_sql_migrations(self.map_upgrade_dir)[0]
        commercial = ee_history.scan_sql_migrations(self.resolved_dir)
        baseline = ee_history.AppliedMigration(1, '0.6', '0.6 version', None, 'BASELINE', 1)
        histories = []
        for m in (source, commercial[0]):
            histories.append([baseline, ee_history.AppliedMigration(
                2, m.version, m.script, m.checksum, 'SQL', 1)])
        histories.append([baseline])
        for history in histories:
            with self.assertRaises(ee_history.SchemaHistoryBackfillError) as raised:
                ee_history.plan_zsvirt_ee_schema_history_backfill(history, commercial, self.scan_map())
            self.assertIn('unsupported schema history type BASELINE', str(raised.exception))

    def test_open_source_target_rejects_baseline_without_proven_open_source_history(self):
        applied = [ee_history.AppliedMigration(1, '0.6', '0.6 version', None, 'BASELINE', 1)]
        with self.assertRaises(ee_history.SchemaHistoryBackfillError):
            ee_history.plan_zsvirt_ee_schema_history_backfill(applied, [], None)

    def test_shared_sql_does_not_override_commercial_evidence(self):
        self.write_source_sql('V5.0.1__schema.sql', b'')
        self.write_resolved_sql('V5.0.1__schema.sql', b'')
        self.write_resolved_sql('V5.1.1__schema.sql', b'commercial')
        self.link_checkpoint('V5.1.1__schema.sql', 'V5.0.1__schema.sql')
        migrations = ee_history.scan_sql_migrations(self.resolved_dir)
        for order in (migrations, list(reversed(migrations))):
            applied = [ee_history.AppliedMigration(rank, m.version, m.script, m.checksum, 'SQL', 1)
                       for rank, m in enumerate(order, 1)]
            plan = ee_history.plan_zsvirt_ee_schema_history_backfill(applied, migrations, self.scan_map())
            self.assertEqual(plan.upgrade_path, ee_history.COMMERCIAL_UPGRADE)
            self.assertIsNone(plan.current_source)


if __name__ == "__main__":
    unittest.main()
