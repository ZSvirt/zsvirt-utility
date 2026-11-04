import os
import pipes
import re
import shutil
import tempfile

from kvmagent import kvmagent
from zstacklib.utils import http
from zstacklib.utils import jsonobject
from zstacklib.utils import log
from zstacklib.utils import bash
from zstacklib.utils import linux

log.configure_log('/var/log/zstack/zstack-kvmagent.log')
logger = log.get_logger(__name__)

USAGE_MIGRATION = 'migration'
ROLE_SERVER = 'server'
ROLE_CLIENT = 'client'

QEMU_PKI_DIR = '/etc/pki/qemu'
PENDING_DIR = os.path.join(QEMU_PKI_DIR, '.zstack-pending')
QEMU_CONF = '/etc/libvirt/qemu.conf'

SAFE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')


def _validate_name(kind, name):
    if not name or name in ('.', '..') or '/' in name or os.sep in name:
        raise Exception('invalid %s[%s]' % (kind, name))
    if not SAFE_NAME.match(name):
        raise Exception('invalid %s[%s]' % (kind, name))
    return name


def _role_filename(role, suffix):
    if role == ROLE_SERVER:
        return 'server-%s.pem' % suffix
    if role == ROLE_CLIENT:
        return 'client-%s.pem' % suffix
    raise Exception('unsupported certificate role[%s]' % role)


def _usage_pending_dir(usage):
    usage = _validate_name('usage', usage)
    pending_dir = os.path.normpath(os.path.join(PENDING_DIR, usage))
    base = os.path.normpath(PENDING_DIR)
    if not pending_dir.startswith(base + os.sep):
        raise Exception('invalid usage path[%s]' % usage)
    return pending_dir


def _pending_key_path(usage, role):
    return os.path.join(_usage_pending_dir(usage), _role_filename(role, 'key'))


def _pending_csr_path(usage, role):
    return os.path.join(_usage_pending_dir(usage), '%s.csr' % _validate_name('role', role))


def _installed_key_path(role):
    return os.path.join(QEMU_PKI_DIR, _role_filename(role, 'key'))


def _installed_cert_path(role):
    return os.path.join(QEMU_PKI_DIR, _role_filename(role, 'cert'))


def _installed_ca_path():
    return os.path.join(QEMU_PKI_DIR, 'ca-cert.pem')


def _installed_crl_path():
    return os.path.join(QEMU_PKI_DIR, 'crl.pem')


def _quote(path):
    return pipes.quote(path)


def _as_list(value):
    if not value:
        return []
    return list(value)


def _as_dict(value):
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    return value.__dict__


def _write_atomic(path, content, mode):
    directory = os.path.dirname(path)
    linux.mkdir(directory, 0o700)
    fd, tmp = tempfile.mkstemp(prefix='.tmp-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(str(content))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.rename(tmp, path)
    except:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _qemu_runtime_owner():
    user, group = 'root', 'root'
    if os.path.exists(QEMU_CONF):
        content = _read_file(QEMU_CONF)
        m = re.search(r'(?m)^\s*user\s*=\s*"([^"]+)"\s*$', content)
        if m:
            user = m.group(1)
        m = re.search(r'(?m)^\s*group\s*=\s*"([^"]+)"\s*$', content)
        if m:
            group = m.group(1)
    return user, group


def _set_key_permissions(path):
    os.chmod(path, 0o600)
    try:
        pwd = __import__('pwd')
        grp = __import__('grp')
        user, group = _qemu_runtime_owner()
        pw = pwd.getpwnam(user)
        gr = grp.getgrnam(group)
        os.chown(path, pw.pw_uid, gr.gr_gid)
    except Exception as ex:
        logger.debug('unable to set runtime owner for key file[%s]: %s' % (path, ex))
        try:
            pwd = __import__('pwd')
            for fallback_user in ('root', 'qemu', 'libvirt-qemu'):
                try:
                    pw = pwd.getpwnam(fallback_user)
                    os.chown(path, pw.pw_uid, pw.pw_gid)
                    return
                except KeyError:
                    continue
        except Exception as ex2:
            logger.debug('fallback owner for key file[%s] also failed: %s' % (path, ex2))


def _run(cmd, error):
    r, o, e = bash.bash_roe(cmd)
    if r != 0:
        raise Exception('%s: %s' % (error, e))
    return o


def _fingerprint(cert_pem_path):
    o = _run("openssl x509 -noout -fingerprint -sha256 -in %s" % _quote(cert_pem_path),
             "failed to get certificate fingerprint")
    return o.strip().split('=', 1)[-1]


def _public_key_fingerprint(key_path):
    o = _run("openssl pkey -in %s -pubout -outform DER 2>/dev/null | openssl dgst -sha256" % _quote(key_path),
             "failed to get public key fingerprint")
    return o.strip().split('=', 1)[-1].replace(' ', '')


def _cert_public_key_fingerprint(cert_path):
    o = _run("openssl x509 -in %s -pubkey -noout | openssl pkey -pubin -outform DER 2>/dev/null | openssl dgst -sha256" %
             _quote(cert_path), "failed to get certificate public key fingerprint")
    return o.strip().split('=', 1)[-1].replace(' ', '')


def _is_key_pair(cert_path, key_path):
    return _cert_public_key_fingerprint(cert_path).lower() == _public_key_fingerprint(key_path).lower()


def _san_list(cert_pem_path):
    r, o, _ = bash.bash_roe("openssl x509 -noout -ext subjectAltName -in %s" % _quote(cert_pem_path))
    if r != 0 or not o.strip():
        return []
    sans = []
    for part in o.replace('\n', ',').split(','):
        part = part.strip()
        for prefix in ('IP Address:', 'DNS:', 'IP:'):
            if part.startswith(prefix):
                sans.append(part[len(prefix):].strip())
                break
    return sans


def _not_after(cert_pem_path):
    o = _run("openssl x509 -noout -enddate -in %s" % _quote(cert_pem_path),
             "failed to get certificate notAfter")
    return o.strip().split('=', 1)[-1]


def _is_cert_valid(cert_pem_path, ca_pem_path):
    r, _, _ = bash.bash_roe("openssl verify -CAfile %s %s" % (_quote(ca_pem_path), _quote(cert_pem_path)))
    return r == 0


def _subject_cn(subject, usage):
    if not subject:
        return usage
    for part in subject.split(','):
        part = part.strip()
        if part.startswith('CN='):
            return part[3:].replace('/', '_')
    return subject.replace('/', '_')


def _is_ip_san(value):
    return value.replace('.', '').isdigit() or ':' in value


def _openssl_req_config(subject, san_list, usage):
    cn = _subject_cn(subject, usage)
    lines = [
        '[ req ]',
        'prompt = no',
        'distinguished_name = dn',
        'req_extensions = req_ext',
        '[ dn ]',
        'CN = %s' % cn,
        '[ req_ext ]',
    ]
    if san_list:
        lines.append('subjectAltName = @alt_names')
        lines.append('[ alt_names ]')
        ip_idx = 1
        dns_idx = 1
        for san in san_list:
            if _is_ip_san(san):
                lines.append('IP.%d = %s' % (ip_idx, san))
                ip_idx += 1
            else:
                lines.append('DNS.%d = %s' % (dns_idx, san))
                dns_idx += 1
    return '\n'.join(lines) + '\n'


def _generate_key(key_file, key_algorithm):
    algo = (key_algorithm or 'RSA_2048').upper()
    if algo.startswith('ECDSA') or algo.startswith('EC_'):
        curve = 'prime256v1'
        if '384' in algo:
            curve = 'secp384r1'
        _run("openssl ecparam -name %s -genkey -noout -out %s" % (curve, _quote(key_file)),
             "failed to generate EC private key")
    else:
        key_bits = 4096 if '4096' in algo else 2048
        _run("openssl genrsa -out %s %d" % (_quote(key_file), key_bits),
             "failed to generate RSA private key")
    os.chmod(key_file, 0o600)


def _read_file(path):
    with open(path, 'r') as f:
        return f.read()


def _qemu_conf_has_setting(content, key):
    return re.search(r'(?m)^\s*' + re.escape(key) + r'\s*=', content) is not None


def _replace_or_append_qemu_conf_line(content, key, line):
    pattern = r'(?m)^\s*' + re.escape(key) + r'\s*=.*$'
    if re.search(pattern, content):
        return re.sub(pattern, line.rstrip('\n'), content, count=1)
    if content and not content.endswith('\n'):
        content += '\n'
    return content + line


def _update_qemu_conf():
    if _qemu_conf_ready():
        return False

    desired_lines = (
        ('migrate_tls_x509_verify', 'migrate_tls_x509_verify = 1\n'),
        ('migrate_tls_x509_cert_dir', 'migrate_tls_x509_cert_dir = "%s"\n' % QEMU_PKI_DIR),
    )

    if not os.path.exists(QEMU_CONF):
        _write_atomic(QEMU_CONF, ''.join(line for _, line in desired_lines), 0o644)
        return True

    content = _read_file(QEMU_CONF)
    updated = False
    for key, line in desired_lines:
        new_content = _replace_or_append_qemu_conf_line(content, key, line)
        if new_content != content:
            content = new_content
            updated = True

    if not updated:
        return False

    if not content.endswith('\n'):
        content += '\n'
    _write_atomic(QEMU_CONF, content, 0o644)
    return True


def _reload_libvirtd_after_qemu_conf_change():
    r, _, e = bash.bash_roe('systemctl try-reload-or-restart libvirtd')
    if r == 0:
        return
    r, _, e = bash.bash_roe('service libvirtd restart')
    if r != 0:
        logger.warn('failed to reload/restart libvirtd after updating qemu.conf: %s' % e)


def _qemu_conf_ready():
    if not os.path.exists(QEMU_CONF):
        return False
    content = _read_file(QEMU_CONF)
    return (
        re.search(r'(?m)^\s*migrate_tls_x509_verify\s*=\s*1\s*$', content) is not None and
        re.search(r'(?m)^\s*migrate_tls_x509_cert_dir\s*=\s*"%s"\s*$' % re.escape(QEMU_PKI_DIR), content) is not None
    )


class AgentRsp(object):
    def __init__(self):
        self.success = True
        self.error = None


class HostPkiPlugin(kvmagent.KvmAgent):
    GENERATE_CSR_PATH = '/host/pki/generate-csr'
    INSTALL_PATH = '/host/pki/install'
    STATUS_PATH = '/host/pki/status'
    REVOKE_LOCAL_PATH = '/host/pki/revoke-local'

    def configure(self, config):
        self.config = config

    def start(self):
        http_server = kvmagent.get_http_server()
        http_server.register_async_uri(self.GENERATE_CSR_PATH, self.generate_csr)
        http_server.register_async_uri(self.INSTALL_PATH, self.install)
        http_server.register_async_uri(self.STATUS_PATH, self.status)
        http_server.register_async_uri(self.REVOKE_LOCAL_PATH, self.revoke_local)

    def stop(self):
        pass

    @kvmagent.replyerror
    def generate_csr(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        usage = _validate_name('usage', cmd.usage)
        if usage != USAGE_MIGRATION:
            raise Exception('unsupported Host PKI usage[%s]' % usage)
        roles = _as_list(cmd.roles) or [ROLE_SERVER, ROLE_CLIENT]
        san_list = _as_list(cmd.sanList)
        subject = cmd.subject or ('CN=%s' % usage)
        key_algorithm = cmd.keyAlgorithm or 'RSA_2048'

        pending_dir = _usage_pending_dir(usage)
        if os.path.isdir(pending_dir):
            shutil.rmtree(pending_dir)
        linux.mkdir(pending_dir, 0o700)

        config_file = os.path.join(pending_dir, 'openssl-req.cnf')
        _write_atomic(config_file, _openssl_req_config(subject, san_list, usage), 0o600)

        csr_pem_by_role = {}
        public_key_fp_by_role = {}
        for role in roles:
            role = _validate_name('role', role)
            if role not in (ROLE_SERVER, ROLE_CLIENT):
                raise Exception('unsupported certificate role[%s]' % role)

            key_file = _pending_key_path(usage, role)
            csr_file = _pending_csr_path(usage, role)
            _generate_key(key_file, key_algorithm)
            _run("openssl req -new -key %s -out %s -config %s" %
                 (_quote(key_file), _quote(csr_file), _quote(config_file)),
                 "failed to generate CSR for role[%s]" % role)

            csr_pem_by_role[role] = _read_file(csr_file)
            public_key_fp_by_role[role] = _public_key_fingerprint(key_file)

        rsp = AgentRsp()
        rsp.csrPemByRole = csr_pem_by_role
        rsp.publicKeyFingerprintByRole = public_key_fp_by_role
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    def install(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        usage = _validate_name('usage', cmd.usage)
        if usage != USAGE_MIGRATION:
            raise Exception('unsupported Host PKI usage[%s]' % usage)

        cert_pem_by_role = _as_dict(cmd.certPemByRole)
        expected_fp_by_role = _as_dict(cmd.expectedFingerprintByRole)
        ca_chain_pem = cmd.caChainPem
        crl_pem = cmd.crlPem

        if not cert_pem_by_role:
            raise Exception('certPemByRole is empty')
        if not ca_chain_pem:
            raise Exception('caChainPem is empty')

        pending_dir = _usage_pending_dir(usage)
        install_dir = os.path.join(pending_dir, 'install')
        if os.path.isdir(install_dir):
            shutil.rmtree(install_dir)
        linux.mkdir(install_dir, 0o700)

        final_files = {}
        ca_file = os.path.join(install_dir, 'ca-cert.pem')
        _write_atomic(ca_file, ca_chain_pem, 0o644)
        final_files[ca_file] = _installed_ca_path()

        if crl_pem:
            crl_file = os.path.join(install_dir, 'crl.pem')
            _write_atomic(crl_file, crl_pem, 0o644)
            final_files[crl_file] = _installed_crl_path()

        for role, cert_pem in cert_pem_by_role.items():
            role = _validate_name('role', role)
            if role not in (ROLE_SERVER, ROLE_CLIENT):
                raise Exception('unsupported certificate role[%s]' % role)
            key_file = _pending_key_path(usage, role)
            if not os.path.exists(key_file):
                raise Exception('private key for role[%s] is missing; generate CSR first' % role)

            staged_key = os.path.join(install_dir, _role_filename(role, 'key'))
            staged_cert = os.path.join(install_dir, _role_filename(role, 'cert'))
            shutil.copy2(key_file, staged_key)
            os.chmod(staged_key, 0o600)
            _write_atomic(staged_cert, cert_pem, 0o644)

            actual_fp = _fingerprint(staged_cert)
            expected_fp = expected_fp_by_role.get(role)
            if expected_fp and actual_fp.upper().replace(':', '') != expected_fp.upper().replace(':', ''):
                raise Exception('certificate fingerprint mismatch for role[%s]: expected %s, got %s' %
                                (role, expected_fp, actual_fp))
            if not _is_key_pair(staged_cert, staged_key):
                raise Exception('certificate does not match local private key for role[%s]' % role)
            if not _is_cert_valid(staged_cert, ca_file):
                raise Exception('certificate for role[%s] cannot be verified by supplied CA chain' % role)

            final_files[staged_key] = _installed_key_path(role)
            final_files[staged_cert] = _installed_cert_path(role)

        linux.mkdir(QEMU_PKI_DIR, 0o755)

        config_updated = _update_qemu_conf()
        if config_updated:
            _reload_libvirtd_after_qemu_conf_change()

        for src, dst in final_files.items():
            os.rename(src, dst)
            if dst.endswith('-key.pem'):
                _set_key_permissions(dst)

        if not _qemu_conf_ready():
            raise Exception('migration TLS libvirt config is not ready after install; check %s' % QEMU_CONF)

        rsp = AgentRsp()
        rsp.path = QEMU_PKI_DIR
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    def status(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        usage = _validate_name('usage', cmd.usage)
        if usage != USAGE_MIGRATION:
            raise Exception('unsupported Host PKI usage[%s]' % usage)

        rsp = AgentRsp()
        rsp.ready = False
        rsp.fingerprint = None
        rsp.notAfter = None
        rsp.sanList = []
        rsp.path = QEMU_PKI_DIR

        ca_file = _installed_ca_path()
        required = [
            ca_file,
            _installed_cert_path(ROLE_SERVER),
            _installed_key_path(ROLE_SERVER),
            _installed_cert_path(ROLE_CLIENT),
            _installed_key_path(ROLE_CLIENT),
        ]
        if any(not os.path.exists(path) for path in required):
            return jsonobject.dumps(rsp)

        try:
            server_cert = _installed_cert_path(ROLE_SERVER)
            client_cert = _installed_cert_path(ROLE_CLIENT)
            ready = (
                _is_cert_valid(server_cert, ca_file) and
                _is_cert_valid(client_cert, ca_file) and
                _is_key_pair(server_cert, _installed_key_path(ROLE_SERVER)) and
                _is_key_pair(client_cert, _installed_key_path(ROLE_CLIENT)) and
                _qemu_conf_ready()
            )
            rsp.ready = ready
            rsp.fingerprint = _fingerprint(server_cert)
            rsp.notAfter = _not_after(server_cert)
            rsp.sanList = _san_list(server_cert)
        except Exception as ex:
            rsp.success = False
            rsp.error = str(ex)

        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    def revoke_local(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        usage = _validate_name('usage', cmd.usage)
        if usage != USAGE_MIGRATION:
            raise Exception('unsupported Host PKI usage[%s]' % usage)

        for path in (
                _installed_cert_path(ROLE_SERVER),
                _installed_key_path(ROLE_SERVER),
                _installed_cert_path(ROLE_CLIENT),
                _installed_key_path(ROLE_CLIENT),
                _installed_ca_path(),
                _installed_crl_path()):
            if os.path.exists(path):
                os.remove(path)

        pending_dir = _usage_pending_dir(usage)
        if os.path.isdir(pending_dir):
            shutil.rmtree(pending_dir)

        return jsonobject.dumps(AgentRsp())
