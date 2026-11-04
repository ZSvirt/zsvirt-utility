"""Run the production packet-size configuration blocks without service changes.

On Windows, set MYSQL_PACKET_TEST_BASH to the absolute Git Bash executable path.
"""

import ast
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
import warnings


ROOT = Path(__file__).resolve().parents[2]


def packet_blocks():
    installer = (ROOT / "installation/install.sh").read_text(encoding="utf-8")
    ctl = (ROOT / "zstackctl/zstackctl/ctl.py").read_text(encoding="utf-8")
    embedded = ctl.split("mysql_db_config_script='''", 1)[1].split("'''", 1)[0]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        embedded = ast.literal_eval("'''" + embedded + "'''")
    for name, source in [("installer", installer), ("ctl", embedded)]:
        match = re.search(
            r"(?m)^([ \t]*)# Fixes ZSTAC-24460\n[\s\S]*?^\1# end ZSTAC-24460$",
            source,
        )
        if match is None:
            raise AssertionError("packet configuration block missing in " + name)
        yield name, textwrap.dedent(match.group(0))


class MysqlPacketConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bash = os.environ.get("MYSQL_PACKET_TEST_BASH")
        if not cls.bash and os.name != "nt":
            cls.bash = shutil.which("bash")
        if not cls.bash or not Path(cls.bash).is_absolute():
            raise unittest.SkipTest("Set MYSQL_PACKET_TEST_BASH to an absolute Bash path")

    def check_fixture(self, script, before, expected):
        with tempfile.TemporaryDirectory(prefix=".mysql-packet-test-", dir=str(ROOT)) as directory:
            fixture_root = Path(directory).resolve()
            self.assertEqual(fixture_root.parent, ROOT)
            config = fixture_root / "my.cnf"
            config.write_text(before, encoding="utf-8")
            env = os.environ.copy()
            env.update(MYSQL_CONF_FILE="my.cnf", mysql_conf="my.cnf", ZSTACK_INSTALL_LOG="install.log")
            for _ in range(2):
                result = subprocess.run(
                    [self.bash, "--noprofile", "--norc", "-s"], input=script,
                    text=True, cwd=directory, env=env, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(config.read_text(encoding="utf-8"), expected)

    def test_numeric_threshold_and_idempotence(self):
        values = [
            ("0", True), ("1", True), ("2M", True), ("4M", True),
            ("8M", True), ("16m", True), ("31M", True), ("1024K", True),
            ("32767k", True), ("33554431", True),
            ("32M", True), ("32m", True), ("32768K", True),
            ("33554432", True), ("63M", True), ("67108863", True),
            ("64M", False), ("64m", False), ("65536K", False), ("67108864", False),
            ("1G", False), ("1g", False), ("unknown", False),
        ]
        for name, script in packet_blocks():
            for value, raised in values:
                with self.subTest(script=name, value=value):
                    before = "[mysqld]\nmax_allowed_packet=" + value + "\nother=7\n"
                    expected = before.replace("=" + value + "\n", "=64M\n", 1) if raised else before
                    self.check_fixture(script, before, expected)

    def test_absent_comments_whitespace_and_multiple_values(self):
        fixtures = [
            ("[mysqld]\nother=7\n", "[mysqld]\nmax_allowed_packet=64M\nother=7\n"),
            ("# max_allowed_packet=2M\n[mysqld]\n", "# max_allowed_packet=2M\n[mysqld]\nmax_allowed_packet=64M\n"),
            ("[mysqld]\n  max_allowed_packet = 8M  # keep\n", "[mysqld]\n  max_allowed_packet =64M  # keep\n"),
            ("[mysqld]\n  max_allowed_packet = 64M  # keep\n", "[mysqld]\n  max_allowed_packet = 64M  # keep\n"),
            ("[mysqld]\nmax_allowed_packet=8M\nmax_allowed_packet=64M\n", "[mysqld]\nmax_allowed_packet=64M\nmax_allowed_packet=64M\n"),
        ]
        for name, script in packet_blocks():
            for before, expected in fixtures:
                with self.subTest(script=name, before=before):
                    self.check_fixture(script, before, expected)

    def test_only_mysqld_group_controls_server_packet_size(self):
        fixtures = [
            (
                "[mysqld]\n[mysqldump]\nmax_allowed_packet=64M\n",
                "[mysqld]\nmax_allowed_packet=64M\n[mysqldump]\nmax_allowed_packet=64M\n",
            ),
            (
                "[mysqldump]\nmax_allowed_packet=4M\n[mysqld]\nother=7\n",
                "[mysqldump]\nmax_allowed_packet=4M\n[mysqld]\nmax_allowed_packet=64M\nother=7\n",
            ),
            (
                "[mysqld]\nmax_allowed_packet=8M\n[mysqldump]\nmax_allowed_packet=4M\n",
                "[mysqld]\nmax_allowed_packet=64M\n[mysqldump]\nmax_allowed_packet=4M\n",
            ),
            (
                "[client]\nmax_allowed_packet=8M\n[mysqld]\nmax_allowed_packet=64M\n",
                "[client]\nmax_allowed_packet=8M\n[mysqld]\nmax_allowed_packet=64M\n",
            ),
            (
                "[mysqld] # server settings\nother=7\n",
                "[mysqld] # server settings\nmax_allowed_packet=64M\nother=7\n",
            ),
            (
                "[mysqld] ; server settings\n# max_allowed_packet=4M\n",
                "[mysqld] ; server settings\nmax_allowed_packet=64M\n# max_allowed_packet=4M\n",
            ),
        ]
        for name, script in packet_blocks():
            for before, expected in fixtures:
                with self.subTest(script=name, before=before):
                    self.check_fixture(script, before, expected)


if __name__ == "__main__":
    unittest.main()
