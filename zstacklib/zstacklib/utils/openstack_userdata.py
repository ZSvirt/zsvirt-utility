"""Translate the legacy root-password userdata for OpenStack consumers.

Cloudbase-init reads the default Windows user's password from admin_pass;
it does not implement cloud-init's chpasswd module. EC2 userdata is left to
the caller unchanged. Never execute, log, or deserialize arbitrary objects
from the supplied userdata.
"""
import email
import re
import textwrap
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import yaml

try:
    string_types = (basestring,)
    text_type = unicode
except NameError:
    string_types = (str,)
    text_type = str

_WINDOWS_SECTIONS = re.compile(
    r'\A\s*(?:(?:<script>.*?</script>|<powershell>.*?</powershell>)\s*)+',
    re.I | re.S)

# Conversion is optional. Bound the parser work without restricting the
# original userdata passed through to cloud-init.
_MAX_YAML_SIZE = 64 * 1024
_MAX_YAML_DEPTH = 32


def _load_config(content):
    if len(content) > _MAX_YAML_SIZE:
        return None
    try:
        depth = 0
        for event in yaml.parse(content, Loader=yaml.SafeLoader):
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
                if depth > _MAX_YAML_DEPTH:
                    return None
            elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
        return yaml.safe_load(content)
    except (yaml.YAMLError, RuntimeError, MemoryError, OverflowError):
        # RuntimeError also covers Python 2's recursion-limit exception.
        return None


def _root_password(config):
    change = config.get('chpasswd')
    if not isinstance(change, dict) or change.get('encrypted'):
        return None
    entries = change.get('list')
    if not isinstance(entries, string_types):
        return None
    password = None
    for entry in entries.splitlines():
        account, sep, value = entry.partition(':')
        if sep and account.strip() == 'root' and value:
            password = value
    return password


def _translate_content(content):
    """Return (root password, typed userdata parts), or (None, None)."""
    script = None
    if content.startswith('#cloud-config'):
        config_text = content[len('#cloud-config'):]
    else:
        match = _WINDOWS_SECTIONS.match(content)
        if not match:
            return None, None
        script = match.group()
        config_text = textwrap.dedent(content[match.end():]).strip()
        if config_text.startswith('#cloud-config'):
            config_text = config_text[len('#cloud-config'):]
    config = _load_config(config_text)
    if not isinstance(config, dict):
        return None, None
    password = _root_password(config)
    if password is None:
        return None, None
    # Keep cloud-config intact: Linux OpenStack consumers still need chpasswd.
    # Cloudbase's unsupported module is nonfatal; its password plugin uses the
    # native admin_pass field after the userdata plugin has finished.
    parts = []
    if script:
        parts.append(('text/x-shellscript', script))
        parts.append(('text/cloud-config', '#cloud-config\n' + config_text.lstrip('\r\n')))
    else:
        parts.append(('text/cloud-config', content))
    return password, parts


def prepare(userdata_list):
    """Return (admin_pass or None, valid OpenStack userdata).

    The legacy root alias addresses Cloudbase-init's configured default user;
    it does not create a Windows root account or guess a localized username.
    Later list entries take precedence, matching the builder's password tag.
    """
    password = None
    parts = []
    changed = False
    for content in userdata_list:
        if text_type is not str and isinstance(content, text_type):
            content = content.encode('utf-8')
        message = email.message_from_string(content)
        messages = [part for part in message.walk() if not part.is_multipart()]
        for part in messages:
            if part.get_content_type() not in ('text/plain', 'text/cloud-config', 'text/x-shellscript'):
                parts.append(part)
                continue
            body = part.get_payload(decode=True) if part.get('Content-Transfer-Encoding') else part.get_payload()
            if not isinstance(body, str):
                try:
                    body = body.decode(part.get_content_charset() or 'utf-8')
                except (UnicodeError, LookupError):
                    parts.append(part)
                    continue
            value, translated = _translate_content(body)
            if value is not None:
                password = value
                if part.keys():
                    # Explicit MIME parts retain all headers and their payload.
                    parts.append(part)
                else:
                    changed = True
                    parts.extend(translated)
                continue
            content_type = part.get_content_type()
            if content_type == 'text/plain':
                if body.startswith('#cloud-config'):
                    content_type = 'text/cloud-config'
                elif body.lstrip().startswith(('<script>', '<powershell>', '#ps1', 'rem cmd', '#!')):
                    content_type = 'text/x-shellscript'
            if part.get('Content-Type'):
                parts.append(part)
            else:
                parts.append((content_type, body))
    if len(userdata_list) == 1 and not changed:
        return password, userdata_list[0]
    if not parts:
        return password, ''
    if len(parts) == 1 and isinstance(parts[0], tuple) and parts[0][0] in ('text/x-shellscript', 'text/cloud-config'):
        return password, parts[0][1]
    combined = MIMEMultipart()
    for index, entry in enumerate(parts):
        if not isinstance(entry, tuple):
            combined.attach(entry)
            continue
        content_type, body = entry
        # A filename without an extension lets Cloudbase identify XML and PS1
        # by their contents, instead of executing XML as a cmd file.
        part = MIMEText(body, _subtype=content_type.split('/', 1)[1], _charset='utf-8')
        part.replace_header('Content-Type', content_type + '; charset="utf-8"')
        part.add_header('Content-Disposition', 'attachment', filename='part-%03d' % index)
        combined.attach(part)
    return password, combined.as_string()
