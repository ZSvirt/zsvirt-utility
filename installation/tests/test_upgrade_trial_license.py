"""Check trial installation when converting an open-source management node."""

from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest


INSTALLER = (Path(__file__).resolve().parents[1] / "install.sh").read_text()


def installer_function(name):
    return re.search(r"(?ms)^" + name + r"\(\)\s*\{.*?^\}", INSTALLER).group()


class UpgradeTrialLicenseTest(unittest.TestCase):
    def run_upgrade(self, source="community", target_ee=True,
                    install_result="installed", repeat=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_config = root / "source/classes/springConfigXml"
            source_config.mkdir(parents=True)
            if source == "ee":
                (source_config / "ee").mkdir()
            elif source == "legacy":
                (source_config / "license.xml").touch()
            if target_ee:
                (root / "target/zstack/WEB-INF/classes/springConfigXml/ee").mkdir(parents=True)
            (root / "license").mkdir()
            (root / "license/license_paid").write_text("paid license")
            (root / "trial").write_text("ISO trial")
            (root / "custom-license").write_text("custom license")

            upgrade = installer_function("upgrade_zstack")
            entry = upgrade[upgrade.index("    uz_install_trial_license\n"):
                            upgrade.index("    show_spinner uz_upgrade_zstack\n")]
            functions = "\n".join(installer_function(name) for name in (
                "uz_has_installed_trial_license", "uz_install_trial_license",
                "show_spinner", "install_license", "il_install_license"))
            script = """
test_root=%s
ZSTACK_INSTALL_ROOT="$test_root/source"
CATALINA_ZSTACK_CLASSES=classes
upgrade_folder="$test_root/target"
LICENSE_FOLDER="$test_root/license"
ZSTACK_TRIAL_LICENSE="$test_root/trial"
LICENSE_PATH="$test_root/custom-license"
ZSTACK_INSTALL_LOG="$test_root/install.log"
INSTALLATION_FAILURE="$test_root/failure"
DEBUG=y
ZSV_INSTALL=y
fail() { echo "$*" >&2; echo "$*" > "$INSTALLATION_FAILURE"; exit 1; }
echo_title() { :; }
echo_subtitle() { :; }
traplogger() { :; }
post_scripts_to_restore_iptables_rules() { :; }
chown() { :; }
pass() { :; }
zstack-ctl() {
    echo "$*" >> "$test_root/calls"
    case %s in
        installed) cp "$3" "$LICENSE_FOLDER/license_imported" ;;
        different) echo "wrong license" > "$LICENSE_FOLDER/license_imported" ;;
        failed) return 23 ;;
    esac
}
%s
%s
[ "$LICENSE_PATH" = "$test_root/custom-license" ] || fail "Supplied license path changed"
[ "$(cat "$LICENSE_FOLDER/license_paid")" = "paid license" ] || fail "Existing license changed"
touch "$test_root/upgraded"
""" % (shlex.quote(directory), shlex.quote(install_result), functions,
       entry * (2 if repeat else 1))
            result = subprocess.run(["bash", "--noprofile", "--norc", "-s"],
                                    input=script, text=True, capture_output=True)
            calls = root / "calls"
            return (result, calls.read_text().splitlines() if calls.exists() else [],
                    (root / "upgraded").exists())

    def test_conversion_installs_bundled_trial_instead_of_supplied_license(self):
        result, calls, upgraded = self.run_upgrade()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(upgraded)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith("install_license --license "))
        self.assertTrue(calls[0].endswith("/trial"))

    def test_other_upgrade_paths_preserve_existing_licenses(self):
        for source, target_ee in (("community", False), ("ee", True), ("legacy", True)):
            with self.subTest(source=source, target_ee=target_ee):
                result, calls, upgraded = self.run_upgrade(source, target_ee)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(calls, [])
                self.assertTrue(upgraded)

    def test_retry_does_not_import_the_same_trial_twice(self):
        result, calls, upgraded = self.run_upgrade(repeat=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 1)
        self.assertTrue(upgraded)

    def test_failed_import_stops_before_package_replacement(self):
        for outcome in ("failed", "missing", "different"):
            with self.subTest(outcome=outcome):
                result, calls, upgraded = self.run_upgrade(install_result=outcome)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("trial license", result.stderr)
                self.assertEqual(len(calls), 1)
                self.assertFalse(upgraded)


if __name__ == "__main__":
    unittest.main()
