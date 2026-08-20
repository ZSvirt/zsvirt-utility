import re
import socket

from zstacklib.utils.linux import contains_path_traversal

try:
    string_types = basestring  # Python 2
except NameError:
    string_types = str

IPV4_PATTERN = re.compile(r'^((25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(25[0-5]|2[0-4]\d|[01]?\d\d?)$')
# Allows trailing $ for Samba-style machine accounts; Unicode usernames intentionally disallowed.
SSH_USERNAME_PATTERN = re.compile(r'^[a-zA-Z0-9._-]+\$?$')
# Shell metacharacters (;|&$`\) disallowed to prevent command injection in SSH/SCP commands.
VALID_PATH_CHAR_PATTERN = re.compile(r'^[a-zA-Z0-9/._@~+\-]+$')
VALID_SCRIPT_SUFFIXES = ('.sh', '.bash')


class SSHValidationError(Exception):
    pass


def _is_valid_ipv6(ip):
    try:
        socket.inet_pton(socket.AF_INET6, ip)
        return True
    except (socket.error, OSError):
        return False


def validate_ssh_host_ip(ip):
    """Validate SSH target host IP (IPv4 / IPv6)."""
    if not ip:
        raise SSHValidationError("SSH target host IP cannot be empty or None")

    if not isinstance(ip, string_types):
        raise SSHValidationError(
            "SSH target host IP must be a string (current type: {0})".format(type(ip).__name__)
        )

    if IPV4_PATTERN.match(ip):
        return

    if _is_valid_ipv6(ip):
        return

    raise SSHValidationError(
        "SSH target host IP has invalid format (current value: {0})".format(ip)
    )


# Linux useradd allows up to 32 characters; use 256 as a generous upper bound
# to accommodate non-Linux systems while still preventing abuse.
_MAX_SSH_USERNAME_LENGTH = 256


def validate_ssh_username(username):
    if not username:
        raise SSHValidationError("SSH login username cannot be empty")
    if not isinstance(username, string_types):
        raise SSHValidationError(
            "SSH username must be a string (current type: {0})".format(type(username).__name__)
        )
    if len(username) > _MAX_SSH_USERNAME_LENGTH:
        raise SSHValidationError(
            "SSH username exceeds maximum length ({0} characters, max: {1})".format(
                len(username), _MAX_SSH_USERNAME_LENGTH)
        )
    if not SSH_USERNAME_PATTERN.match(username):
        raise SSHValidationError(
            "SSH username format is invalid (only letters, digits, underscores(_), dots(.), hyphens(-) and trailing dollar($) allowed), current value: {0}".format(
                username)
        )


def validate_ssh_port(port):
    if port is None:
        raise SSHValidationError("SSH port number cannot be empty")
    if isinstance(port, bool):
        raise SSHValidationError("SSH port number must be an integer, not bool")
    try:
        port_int = int(port)
    except (ValueError, TypeError):
        raise SSHValidationError(
            "SSH port number must be an integer, current value: {0} (type: {1})".format(port, type(port).__name__)
        )
    if not (1 <= port_int <= 65535):
        raise SSHValidationError(
            "SSH port number must be in the range 1-65535, current value: {0}".format(port)
        )


def validate_ssh_path(path, param_name="path", allow_absolute=True, allow_relative=True):
    if not path:
        raise SSHValidationError("{0} cannot be empty or None".format(param_name))

    if not isinstance(path, string_types):
        raise SSHValidationError(
            "{0} must be string type (current type: {1})".format(
                param_name, type(path).__name__
            )
        )

    if '\x00' in path:
        raise SSHValidationError(
            "{0} contains illegal null byte character".format(param_name)
        )

    if contains_path_traversal(path):
        raise SSHValidationError(
            "{0} contains illegal path traversal sequence '..' (current value: {1})".format(
                param_name, path
            )
        )

    if not VALID_PATH_CHAR_PATTERN.match(path):
        raise SSHValidationError(
            "{0} contains illegal characters (only letters, digits, / . _ - @ ~ + are allowed) (current value: {1})".format(
                param_name, path
            )
        )

    if path.startswith('/') and not allow_absolute:
        raise SSHValidationError(
            "{0} does not allow absolute paths (current value: {1})".format(
                param_name, path
            )
        )

    if not path.startswith('/') and not allow_relative:
        raise SSHValidationError(
            "{0} must be an absolute path (current value: {1})".format(
                param_name, path
            )
        )

    if len(path) > 4096:
        raise SSHValidationError(
            "{0} exceeds maximum length (4096 characters) (current length: {1})".format(
                param_name, len(path)
            )
        )


def validate_ssh_script_path(script_path, param_name="upgradeScriptPath", allowed_suffixes=None,
                             allow_relative=True):
    if allowed_suffixes is None:
        allowed_suffixes = VALID_SCRIPT_SUFFIXES

    validate_ssh_path(script_path, param_name=param_name, allow_relative=allow_relative)
    if not script_path.endswith(tuple(allowed_suffixes)):
        raise SSHValidationError(
            "{0} must point to a shell script file (allowed suffixes: {1}) (current value: {2})".format(
                param_name, ', '.join(allowed_suffixes), script_path
            )
        )
