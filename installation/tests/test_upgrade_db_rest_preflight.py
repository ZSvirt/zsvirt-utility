"""Exercise UpgradeDbCmd preflight and Flyway arguments without database access."""

from pathlib import Path
import os
import posixpath
import shutil
import subprocess
from types import SimpleNamespace
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]


class CtlError(Exception):
    pass


def upgrade_db_preflight():
    source = (ROOT / "zstackctl/zstackctl/ctl.py").read_text(encoding="utf-8")
    body = source.split("class UpgradeDbCmd(Command):", 1)[1]
    start = body.index("    def run(self, args):")
    end = body.index("        def update_db_config():")
    namespace = {}
    exec(compile(textwrap.dedent(body[start:end]), "UpgradeDbCmd.run preflight", "exec"), namespace)
    return namespace["run"]


def upgrade_db_migrate():
    source = (ROOT / "zstackctl/zstackctl/ctl.py").read_text(encoding="utf-8")
    body = source.split("class UpgradeDbCmd(Command):", 1)[1]
    start = body.index("        def migrate():")
    end = body.index("        update_db_config()", start)
    quote_start = source.index("def shell_quote(s):")
    quote_end = source.index("\nclass SpinnerInfo", quote_start)
    namespace = {}
    exec(compile(source[quote_start:quote_end], "ctl.py shell_quote", "exec"), namespace)
    exec(compile(textwrap.dedent(body[start:end]), "UpgradeDbCmd.migrate", "exec"), namespace)
    return namespace["migrate"]


class UpgradeDbRestPreflightTest(unittest.TestCase):
    def run_preflight(self, dry_run, force=False):
        checked_paths = []

        def exists(path):
            checked_paths.append(path)
            return not path.endswith("/db/zstack_rest/upgrade/")

        ctl = SimpleNamespace(
            zstack_home="/installed/old-zstack",
            get_db_url=lambda: "jdbc:mysql://localhost:3306/zstack",
            read_property=lambda key: None,
            get_live_mysql_portal=lambda: ("localhost", 3306, "root", ""),
            check_if_management_node_has_stopped=lambda value: None,
        )
        namespace = upgrade_db_preflight().__globals__
        namespace.update(
            ctl=ctl,
            os=SimpleNamespace(path=SimpleNamespace(join=posixpath.join, exists=exists)),
            CtlError=CtlError,
            error_if_tool_is_missing=lambda tool: None,
            info=lambda message: None,
        )
        return namespace["run"](None, SimpleNamespace(dry_run=dry_run, force=force)), checked_paths

    def test_dry_run_allows_old_install_without_new_rest_directory(self):
        for force in (False, True):
            with self.subTest(force=force):
                result, checked_paths = self.run_preflight(dry_run=True, force=force)
                self.assertTrue(result)
                self.assertNotIn(
                    "/installed/old-zstack/WEB-INF/classes/db/zstack_rest/upgrade/",
                    checked_paths,
                )

    def test_actual_migration_requires_new_rest_directory(self):
        with self.assertRaisesRegex(CtlError, "db/zstack_rest/upgrade"):
            self.run_preflight(dry_run=False)


class UpgradeDbRestUrlTest(unittest.TestCase):
    def test_jdbc_url_and_location_reach_one_foreground_flyway_command(self):
        bash = os.environ.get("MYSQL_PACKET_TEST_BASH")
        if not bash and os.name != "nt":
            bash = shutil.which("bash")
        if not bash or not Path(bash).is_absolute():
            self.skipTest("Set MYSQL_PACKET_TEST_BASH to an absolute Bash path")

        urls = (
            "jdbc:mysql://localhost:3306/zstack_rest",
            "jdbc:mysql://localhost:3306/zstack_rest?useUnicode=true&characterEncoding=UTF-8",
        )
        for password in ("", "fixture-password"):
            for url in urls:
                with self.subTest(password=bool(password), url=url):
                    migrate = upgrade_db_migrate()
                    commands = []
                    migrate.__globals__.update(
                        upgrading_schema_dirs=[],
                        rest_upgrading_schema_dir="/safe/db/zstack_rest/upgrade/",
                        flyway_path="/safe/flyway",
                        db_user="zstack",
                        db_password=password,
                        rest_db_url=url,
                        shell_no_pipe=commands.append,
                        info=lambda message: None,
                    )
                    migrate()
                    self.assertEqual(len(commands), 1)
                    script = 'bash() { printf "ARG:<%s>\\n" "$@"; return 23; }\n' + commands[0] + "\n"
                    result = subprocess.run(
                        [bash, "--noprofile", "--norc", "-s"],
                        input=script, text=True, capture_output=True,
                    )
                    self.assertEqual(result.returncode, 23, result.stderr)
                    self.assertIn("ARG:<-url=%s>" % url, result.stdout)
                    self.assertIn(
                        "ARG:<-locations=filesystem:/safe/db/zstack_rest/upgrade/>",
                        result.stdout,
                    )


if __name__ == "__main__":
    unittest.main()
