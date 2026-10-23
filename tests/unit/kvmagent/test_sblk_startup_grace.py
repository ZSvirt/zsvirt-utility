"""Startup grace uses first attempt time without claiming a successful write."""
import types

from test_sblk_health_check import VG, _load_health_check


def _check_startup_record(last_check, last_success, first_check=None, code=-2):
    checker, _ = _load_health_check(0)
    checker.max_failure = 2
    checker.health_check_interval = 5
    checker.storage_timeout = 10
    namespace = checker._do_zsblkagent_heartbeat_check.__globals__
    namespace['linux'].get_current_timestamp = lambda: last_check
    record = types.SimpleNamespace(lastCheck=last_check, lastSuccess=last_success,
                                   code=code, error='write failed')
    if first_check is not None:
        record.firstCheck = first_check
    return checker._do_zsblkagent_heartbeat_check(VG, {VG: record})


def test_never_successful_writer_keeps_existing_startup_grace():
    for code in (-1, -2):
        result = _check_startup_record(1000, 0, 1000, code)
        assert result.failed is False
        assert result.no_way is False


def test_never_successful_writer_fails_after_startup_grace():
    result = _check_startup_record(1031, 0, 1000)
    assert result.failed is True
    assert result.io_timeout is True


def test_restart_gets_new_startup_grace_without_old_success():
    result = _check_startup_record(2000, 0, 2000, -1)
    assert result.failed is False
    assert result.no_way is False


def test_never_successful_record_without_start_is_unknown():
    result = _check_startup_record(1000, 0)
    assert result.no_way is True


def test_real_success_time_takes_precedence_over_startup_time():
    result = _check_startup_record(1031, 1020, 1000)
    assert result.failed is False
    assert result.no_way is False
