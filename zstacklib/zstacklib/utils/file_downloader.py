import os
import traceback
import urlparse
from enum import Enum

from zstacklib.utils import traceable_shell, linux, shell, plugin, log
from zstacklib.utils.bash import bash_roe

logger = log.get_logger(__name__)


class UrlScheme(Enum):
    """Supported URL schemes"""
    HTTP = 'http'
    HTTPS = 'https'
    FILE = 'file'
    SFTP = 'sftp'
    FTP = 'ftp'
    NFS = 'nfs'


class DownloadStrategy(object):
    """Abstract base class for download strategies"""

    def __init__(self, downloader):
        self.downloader = downloader

    def download(self):
        """Execute download operation, returns (success: bool, error_msg: str or None)"""
        raise NotImplementedError()


class HttpDownloadStrategy(DownloadStrategy):
    """Download strategy for HTTP/HTTPS/FTP protocols"""

    def download(self):
        install_path = self.downloader.install_path
        download_cancelled = [False]

        class HttpDownloadDaemon(plugin.TaskDaemon):
            def _cancel(self):
                download_cancelled[0] = True
                linux.rm_file_force(install_path)

        daemon = HttpDownloadDaemon(self.downloader.cmd, "DownloadImage")
        if not daemon.start():
            return False, 'download canceled before start'

        def cancellation_checker():
            return download_cancelled[0] or self.downloader.cancellation_pending()

        try:
            cmd = self.downloader.cmd
            cmd.url = linux.shellquote(cmd.url)
            ret = self.downloader.use_wget(
                cmd.url,
                self.downloader.file_name,
                self.downloader.path,
                self.downloader.timeout,
                cancellation_checker
            )
            if ret != 0:
                linux.rm_file_force(self.downloader.install_path)
                return False, 'http/https/ftp download failed, [wget -O %s %s] returns value %s' % (
                    self.downloader.file_name, cmd.url, ret)
            return True, None
        except linux.LinuxError as e:
            linux.rm_file_force(self.downloader.install_path)
            logger.warning("HTTP download error traceback: %s" % traceback.format_exc())
            return False, str(e)
        finally:
            daemon.close()


class SftpDownloadStrategy(DownloadStrategy):
    """Download strategy for SFTP protocol"""

    def download(self):
        ssh_pass_file = None
        url = urlparse.urlparse(self.downloader.cmd.url)
        port = (url.port, 22)[url.port is None]
        install_path = self.downloader.install_path

        class SftpDownloadDaemon(plugin.TaskDaemon):
            def _cancel(self):
                pass

            def _get_percent(self):
                return os.stat(install_path).st_size / (total_size / 100) if os.path.exists(install_path) else 0

            def _exit(self, exc_type, exc_val, exc_tb):
                if ssh_pass_file:
                    linux.rm_file_force(ssh_pass_file)
                if exc_val is not None:
                    linux.rm_file_force(install_path)
                    logger.warning("SFTP download error traceback: %s" % traceback.format_exc())

        try:
            with SftpDownloadDaemon(self.downloader.cmd, "DownloadImage"):
                sftp_cmd = "sftp -P %d -o BatchMode=no -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -b /dev/stdin %s@%s " \
                           "<<EOF\n%%s\nEOF\n" % (port, url.username, url.hostname)
                if url.password is not None:
                    ssh_pass_file = linux.write_to_temp_file(url.password)
                    sftp_cmd = 'sshpass -f %s %s' % (ssh_pass_file, sftp_cmd)

                total_size = int(shell.call(sftp_cmd % ("ls -l " + url.path)).splitlines()[1].split()[4])
                self.downloader.t_shell.call(sftp_cmd % ("reget %s %s" % (url.path, install_path)))
            return True, None
        except Exception as e:
            return False, "SFTP download failed: %s" % str(e)


class FileDownloadStrategy(DownloadStrategy):
    """Download strategy for local file system"""

    def download(self):
        url = self.downloader.cmd.url
        if url.startswith('file://'):
            src_path = url[len('file://'):]
        elif url.startswith('file:'):
            src_path = url[len('file:'):]
        else:
            src_path = url
        if linux.contains_path_traversal(src_path):
            return False, 'file url contains illegal path traversal: %s' % src_path
        src_path = os.path.normpath(src_path)
        if not os.path.isabs(src_path):
            return False, 'file url must use absolute path: %s' % src_path
        if not os.path.isfile(src_path):
            return False, 'cannot find the file[%s]' % src_path

        logger.debug("src_path is: %s" % src_path)
        try:
            self.downloader.t_shell.call('yes | cp %s %s' % (linux.shellquote(src_path), linux.shellquote(self.downloader.install_path)))
            return True, None
        except shell.ShellError as e:
            linux.rm_file_force(self.downloader.install_path)
            return False, str(e)


class FileDownloader:
    """File Downloader - using Strategy Pattern"""

    @staticmethod
    def _redact_url(url):
        parsed = urlparse.urlparse(url)
        if parsed.username is None and parsed.password is None:
            return url
        host = parsed.hostname or ""
        if parsed.port:
            host = "%s:%s" % (host, parsed.port)
        netloc = "%s:***@%s" % (parsed.username or "", host)
        return urlparse.urlunparse((
            parsed.scheme, netloc, parsed.path,
            parsed.params, parsed.query, parsed.fragment
        ))

    # Strategy mapping table
    STRATEGY_MAP = {
        UrlScheme.HTTP: HttpDownloadStrategy,
        UrlScheme.HTTPS: HttpDownloadStrategy,
        UrlScheme.FTP: HttpDownloadStrategy,
        UrlScheme.SFTP: SftpDownloadStrategy,
        UrlScheme.FILE: FileDownloadStrategy,
    }

    def __init__(self, reporter, cmd):
        self.reporter = reporter
        self.cmd = cmd
        self.t_shell = traceable_shell.get_shell(cmd)
        self.path = os.path.dirname(self.cmd.installPath)
        self.file_name = os.path.basename(self.cmd.installPath)
        self.install_path = self.cmd.installPath
        self.timeout = self.cmd.timeout if self.cmd.timeout else 7200

        # Convert string to enum
        try:
            self.urlScheme = UrlScheme(self.cmd.urlScheme)
        except ValueError:
            self.urlScheme = None

    def use_wget(self, url, name, workdir, timeout, cancellation_checker=None):
        if cancellation_checker is None:
            cancellation_checker = self.cancellation_pending

        return linux.wget(url, workdir=workdir, rename=name, timeout=timeout, interval=2,
                          callback=self.reporter.progress_report, callback_data="report",
                          cmd_wrapper=self.t_shell.wrap_cmd,
                          cancellation_checker=cancellation_checker)

    def cancellation_pending(self):
        return plugin.TaskManager.cancellation_pending(plugin.get_api_id(self.cmd))

    def get_url_file_size(self, url):
        # Only allow HTTP/HTTPS/FTP to prevent SSRF via unexpected protocols.
        parsed = urlparse.urlparse(url)
        if parsed.scheme not in ('http', 'https', 'ftp'):
            logger.debug("get_url_file_size: unsupported scheme %s, skipping" % parsed.scheme)
            return False, 0

        # curl -L follows redirects; iterate all headers and keep the last
        # Content-Length which belongs to the final response.
        try:
            output = shell.call("curl -sSL --head --connect-timeout 10 --max-time 30 %s" % linux.shellquote(url))
        except Exception as e:
            logger.debug("curl HEAD request failed for %s: %s" % (self._redact_url(url), str(e)))
            return False, 0

        content_length = None
        for line in output.split('\n'):
            line_lower = line.strip().lower()
            if 'content-length:' in line_lower:
                parts = line.split(':', 1)
                if len(parts) >= 2:
                    try:
                        content_length = int(parts[1].strip())
                    except ValueError:
                        pass
        if content_length is not None:
            return True, content_length
        return False, 0

    def get_local_file_size(self, local_file_path):
        parsed = urlparse.urlparse(local_file_path)
        if parsed.scheme == 'file':
            path = parsed.path
        else:
            path = local_file_path

        # Only allow absolute paths to avoid ambiguity with urlparse.
        if not os.path.isabs(path):
            logger.debug("get_local_file_size: ignoring non-absolute path %s" % path)
            return 0

        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    def get_url_file_size_by_wget(self, url):
        """Fallback: use wget --spider to get file size when curl HEAD fails."""
        parsed = urlparse.urlparse(url)
        if parsed.scheme not in ('http', 'https', 'ftp'):
            return False, 0
        try:
            # wget --spider prints size info to stderr; capture it via
            # bash_roe instead of 2>&1 so output is available even on
            # non-zero exit codes.
            _, _, stderr = bash_roe("wget --spider --timeout=10 --tries=1 %s" % linux.shellquote(url))
            output = stderr if stderr else ''
            for line in output.split('\n'):
                line_lower = line.strip().lower()
                if 'length:' in line_lower:
                    # wget output: "Length: 12345 (12K) [type]"
                    parts = line.split()
                    for i, part in enumerate(parts):
                        if part.lower() == 'length:' and i + 1 < len(parts):
                            try:
                                return True, int(parts[i + 1])
                            except ValueError:
                                pass
        except Exception as e:
            logger.debug("wget --spider failed for %s: %s" % (self._redact_url(url), str(e)))
        return False, 0

    def check_capacity(self):
        if self.urlScheme == UrlScheme.FILE:
            file_size = self.get_local_file_size(self.cmd.url)
            is_support_file_size = file_size > 0
        else:
            is_support_file_size, file_size = self.get_url_file_size(self.cmd.url)
            if not is_support_file_size:
                is_support_file_size, file_size = self.get_url_file_size_by_wget(self.cmd.url)

        if is_support_file_size:
            try:
                _, availableCapacity = linux.get_disk_capacity_by_df(os.path.dirname(self.install_path))
            except Exception:
                # Parent directory may not exist yet; find the nearest existing ancestor.
                ancestor = os.path.dirname(self.install_path)
                while ancestor and ancestor != '/' and not os.path.exists(ancestor):
                    ancestor = os.path.dirname(ancestor)
                if not ancestor or not os.path.exists(ancestor):
                    logger.warning("cannot determine capacity for %s, skipping check", self.install_path)
                    return True, None
                _, availableCapacity = linux.get_disk_capacity_by_df(ancestor)
            # 10% margin to mitigate TOCTOU race between check and download.
            required = int(file_size * 1.1)
            if availableCapacity < required:
                return False, 'disk capacity[%s] is not enough to download file[%s] of size[%s] (with 10%% margin)' % (
                    availableCapacity, self._redact_url(self.cmd.url), file_size)
        else:
            logger.warning("unable to determine file size for %s (Content-Length header missing or local file not found), "
                        "skipping capacity check", self._redact_url(self.cmd.url))
            # Even when the exact file size is unknown, reject downloads when
            # available disk space is critically low (< 1 GiB).
            MIN_SAFE_CAPACITY = 1024 * 1024 * 1024  # 1 GiB
            try:
                ancestor = os.path.dirname(self.install_path)
                while ancestor and ancestor != '/' and not os.path.exists(ancestor):
                    ancestor = os.path.dirname(ancestor)
                if ancestor and os.path.exists(ancestor):
                    _, availableCapacity = linux.get_disk_capacity_by_df(ancestor)
                    if availableCapacity < MIN_SAFE_CAPACITY:
                        return False, ('disk available capacity [%s] is below minimum safe threshold [%s]'
                                       % (availableCapacity, MIN_SAFE_CAPACITY))
            except Exception:
                logger.debug("failed to check minimum capacity for %s" % self.install_path)

        return True, None

    def download(self):
        """Execute download using appropriate strategy"""

        api_id = plugin.get_api_id(self.cmd)
        if plugin.TaskManager.cancellation_pending(api_id):
            return False, 'download canceled before start'

        # Validate URL scheme
        if self.urlScheme is None or self.urlScheme not in self.STRATEGY_MAP:
            supported = [scheme.value for scheme in self.STRATEGY_MAP.keys()]
            return False, 'unsupported url scheme[%s], only supports %s' % (self.cmd.urlScheme, supported)

        success, err = self.check_capacity()
        if not success:
            return False, err

        if not os.path.exists(self.path):
            os.makedirs(self.path, 0o755)

        # Execute appropriate download strategy
        strategy_class = self.STRATEGY_MAP[self.urlScheme]
        strategy = strategy_class(self)
        return strategy.download()
