import unittest

try:
    from kvmagent.plugins import prometheus
except ImportError as e:
    raise unittest.SkipTest(
        "kvmagent package not importable (%s); "
        "run with PYTHONPATH=kvmagent:zstacklib" % e)

import mock


class TestStripPartitionSuffix(unittest.TestCase):

    def test_strip_partition_suffix(self):
        f = prometheus._strip_partition_suffix
        cases = [
            ("sda1", "sda"),
            ("sdb", "sdb"),
            ("sdaa", "sdaa"),
            ("vda1", "vda"),
            ("vdb", "vdb"),
            ("nvme0n1", "nvme0n1"),
            ("nvme0n1p1", "nvme0n1"),
            ("nvme0n1p12", "nvme0n1"),
            ("nvme1n1", "nvme1n1"),
            ("nvme1n1p3", "nvme1n1"),
            ("nvme0n10", "nvme0n10"),
            ("nvme0n10p1", "nvme0n10"),
            ("nvme10n1p2", "nvme10n1"),
        ]
        for raw, expected in cases:
            self.assertEqual(expected, f(raw),
                             "strip(%s) expect %s" % (raw, expected))


class TestSafeGetDeviceFromPath(unittest.TestCase):

    def test_success_returns_device(self):
        fake_dev = object()
        with mock.patch.object(prometheus.pyudev, "Device") as Device:
            Device.from_device_file.return_value = fake_dev
            self.assertIs(fake_dev,
                          prometheus._safe_get_device_from_path(object(), "/dev/sda"))

    def test_oserror_returns_none(self):
        with mock.patch.object(prometheus.pyudev, "Device") as Device, \
                mock.patch.object(prometheus, "logger") as logger:
            Device.from_device_file.side_effect = OSError(2, "No such device")
            ret = prometheus._safe_get_device_from_path(object(), "/dev/nvme9n1")
            self.assertIsNone(ret)
            self.assertEqual(1, logger.warn.call_count)
            msg = logger.warn.call_args[0][0]
            self.assertIn("/dev/nvme9n1", msg)
            self.assertIn("udev error", msg)
            self.assertIn("_safe_get_device_from_path", msg)

    def test_arbitrary_exception_returns_none(self):
        with mock.patch.object(prometheus.pyudev, "Device") as Device, \
                mock.patch.object(prometheus, "logger") as logger:
            Device.from_device_file.side_effect = RuntimeError("device not found by file")
            ret = prometheus._safe_get_device_from_path(object(), "/dev/sdz")
            self.assertIsNone(ret)
            self.assertEqual(1, logger.warn.call_count)
            self.assertIn("device not found by file", logger.warn.call_args[0][0])

    def test_each_failure_logs_independently(self):
        with mock.patch.object(prometheus.pyudev, "Device") as Device, \
                mock.patch.object(prometheus, "logger") as logger:
            Device.from_device_file.side_effect = OSError("nope")
            prometheus._safe_get_device_from_path(object(), "/dev/sda")
            prometheus._safe_get_device_from_path(object(), "/dev/sdb")
            self.assertEqual(2, logger.warn.call_count)


class TestCollectNodeDiskWwidSkipsBadDevices(unittest.TestCase):

    def setUp(self):
        prometheus.collect_node_disk_wwid_last_time = None
        prometheus.collect_node_disk_wwid_last_result = None

    def _fake_device(self, devlinks="", dm_uuid=""):
        dev = mock.MagicMock()
        dev.get.side_effect = lambda k, d=None: {
            "DEVLINKS": devlinks,
            "DM_UUID": dm_uuid,
        }.get(k, d)
        return dev

    def _run(self, pvs_output, from_device_file_side_effect):
        with mock.patch.object(prometheus, "bash_o",
                               return_value="\n".join(pvs_output)), \
                mock.patch.object(prometheus, "pyudev") as pyudev_mod, \
                mock.patch.object(prometheus, "GaugeMetricFamily") as Gauge, \
                mock.patch.object(prometheus, "logger") as logger:
            pyudev_mod.Device.from_device_file.side_effect = from_device_file_side_effect
            pyudev_mod.Context.return_value = object()
            metric = mock.MagicMock()
            Gauge.return_value = metric
            prometheus.collect_node_disk_wwid()
            return metric, logger

    def test_bad_pv_skipped_others_collected(self):
        good = self._fake_device(
            devlinks="/dev/disk/by-id/wwn-good /dev/sda", dm_uuid="")

        def side_effect(ctx, devpath):
            if devpath == "/dev/sdX":
                raise OSError("missing")
            return good

        metric, logger = self._run(
            ["/dev/sdX vgbad", "/dev/sda vggood"], side_effect)
        metric.add_metric.assert_called_once_with(
            ["sda", "wwn-good"], 1)
        self.assertEqual(1, logger.warn.call_count)
        self.assertIn("/dev/sdX", logger.warn.call_args[0][0])

    def test_bad_disk_skipped_loop_continues(self):
        def side_effect(ctx, devpath):
            if devpath == "/dev/nvme0n1":
                raise OSError("disk gone")
            return self._fake_device(devlinks="", dm_uuid="")

        metric, logger = self._run(["/dev/nvme0n1p1 vgnvme"], side_effect)
        metric.add_metric.assert_not_called()
        self.assertEqual(1, logger.warn.call_count)
        self.assertIn("/dev/nvme0n1", logger.warn.call_args[0][0])

    def test_all_pvs_bad_does_not_raise(self):
        def side_effect(ctx, devpath):
            raise OSError("all gone")

        metric, logger = self._run(
            ["/dev/sda vg1", "/dev/sdb vg2", "/dev/nvme0n1p1 vg3"], side_effect)
        metric.add_metric.assert_not_called()
        self.assertEqual(3, logger.warn.call_count)


if __name__ == "__main__":
    #import sys;sys.argv = ['', 'Test.testName']
    unittest.main()
