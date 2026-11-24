"""HTTP for addresses we do not control: public IPs only, robots.txt, a pause per host, a size cap and a disk
cache. Every redirect goes through the same checks as the first request."""
import hashlib
import http.client
import ipaddress
import json
import os
import socket
import ssl
import threading
import time
import urllib.error
import re
import urllib.request
import zlib
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from . import __version__

AGENT = 'foothold'
USER_AGENT = f'{AGENT}/{__version__} (+https://github.com/Bouliw/foothold)'
MAX_BYTES = 3_000_000
TIMEOUT = 20       # seconds without a byte
DEADLINE = 90      # seconds for one whole answer
CACHE_TTL = 24 * 3600
TEXT_TYPES = ('text/', 'application/json', 'application/xml', 'application/xhtml', 'application/rss',
              'application/atom', 'application/ld+json')
CHARSETS = {'utf-8', 'utf8', 'us-ascii', 'ascii', 'iso-8859-1', 'latin-1', 'latin1', 'iso-8859-15', 'iso-8859-2',
            'windows-1250', 'windows-1251', 'windows-1252', 'cp1252', 'utf-16', 'shift_jis', 'euc-jp', 'euc-kr',
            'gb2312', 'gbk', 'gb18030', 'big5'}
SECRET_HEADERS = {'authorization', 'x-api-key', 'cookie'}
use_cache = True


class FetchError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


@dataclass
class Page:
    url: str
    status: int
    headers: dict
    text: str
    fetched: str


def is_public_ip(ip):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    addr = getattr(addr, 'ipv4_mapped', None) or addr
    if addr.version == 6 and (int(addr) < 2 ** 32 or addr in _NAT64):  # IPv4 hidden inside an IPv6 address
        return False
    return addr.is_global


_NAT64 = ipaddress.ip_network('64:ff9b::/96')


def _connect(address, timeout=socket.getdefaulttimeout(), source_address=None, **_):
    """Resolves the host, refuses it unless every address is public, then connects to the address that was
    checked (so a second DNS answer cannot point somewhere else)."""
    host, port = address
    try:
        ips = [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
    except socket.gaierror:
        raise FetchError(f'{host}: host not found')
    if not ips or not all(is_public_ip(ip) for ip in ips):
        raise FetchError(f'{host}: not a public address', status=0)
    return socket.create_connection((ips[0], port), timeout, source_address)


class _Conn(http.client.HTTPConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _connect


class _TLSConn(http.client.HTTPSConnection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._create_connection = _connect


class _HTTP(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_Conn, req)


class _HTTPS(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_TLSConn, req, context=_TLS)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_TLS = ssl.create_default_context()
# Built by hand: no proxy, file, ftp or data handlers, and redirects come back to get() to be checked
_opener = urllib.request.OpenerDirector()
for _handler in (_HTTP(), _HTTPS(), _NoRedirect(), urllib.request.HTTPDefaultErrorHandler(),
                 urllib.request.HTTPErrorProcessor()):
    _opener.add_handler(_handler)

_lock = threading.Lock()
_last = {}    # host -> time of the last request
_delay = {}   # host -> pause asked by its robots.txt
_robots = {}  # origin -> parsed robots.txt


def _wait(host, delay):
    with _lock:
        pause = _last.get(host, 0) + max(delay, _delay.get(host, 0)) - time.monotonic()
        if pause > 0:
            time.sleep(pause)
        _last[host] = time.monotonic()


def _inflate(body, encoding, limit):
    if encoding not in ('gzip', 'deflate'):
        return body
    return zlib.decompressobj(47).decompress(body, limit + 1)  # 47: gzip or zlib header, found automatically


def _once(url, headers, data, limit):
    """One request, no redirect followed: (status, headers, body)."""
    req = urllib.request.Request(url, data=data, headers={'User-Agent': USER_AGENT, 'Accept': '*/*', 'Accept-Language': 'en', **headers})
    try:
        with _opener.open(req, timeout=TIMEOUT) as resp:
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            # Read piece by piece: the socket timeout alone would let a server drip one byte at a time for ever
            deadline, body = time.monotonic() + DEADLINE, b''
            while len(body) <= limit and (chunk := resp.read1(65536)):
                body += chunk
                if time.monotonic() > deadline:
                    raise FetchError(f'{urlsplit(url).hostname}: answer too slow')
            return resp.status, hdrs, _inflate(body[:limit + 1], hdrs.get('content-encoding', ''), limit)
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, b''
    except FetchError:
        raise
    except (urllib.error.URLError, OSError, http.client.HTTPException, zlib.error) as e:
        raise FetchError(f'{urlsplit(url).hostname}: {getattr(e, "reason", e)}')
    except ValueError:  # an address http.client cannot send, such as a path with raw non-ASCII characters
        raise FetchError(f'{url[:100]}: not a valid address')


def _follow(url, headers, data, limit, delay, robots):
    for _ in range(6):
        try:
            parts = urlsplit(url)
        except ValueError:
            raise FetchError(f'{url[:100]}: not a valid address')
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            raise FetchError(f'{url}: not an http(s) address')
        if robots and not allowed(url):
            raise FetchError(f'{url}: disallowed by robots.txt')
        _wait(parts.hostname, delay)
        status, hdrs, body = _once(url, headers, data, limit)
        if status in (301, 302, 303, 307, 308) and hdrs.get('location'):
            try:
                url = urljoin(url, hdrs['location'])
                moved = urlsplit(url).netloc != parts.netloc
            except ValueError:
                raise FetchError(f'{parts.hostname}: redirect to an invalid address')
            if moved:  # keys and request bodies stay with the host they were meant for
                headers = {k: v for k, v in headers.items() if k.lower() not in SECRET_HEADERS}
            if moved or status in (301, 302, 303):
                data = None
            continue
        return url, status, hdrs, body
    raise FetchError(f'{url}: too many redirects')


class Robots:
    """The rules of one robots.txt for our user agent, read as RFC 9309 asks: the group naming us, or else the
    `*` group; `*` and `$` in paths; the longest matching rule wins, and Allow wins a tie."""

    def __init__(self, text='', allow_all=False, disallow_all=False):
        self.allow_all, self.disallow_all, self.delay = allow_all, disallow_all, 0.0
        groups, agents, fresh = {}, [], True
        for line in text.splitlines():
            key, sep, value = line.split('#')[0].partition(':')
            key, value = key.strip().lower(), value.strip()
            if not sep:
                continue
            if key == 'user-agent':
                if not fresh:
                    agents, fresh = [], True
                agents.append(value.lower())
                groups.setdefault(value.lower(), [])
            elif key in ('allow', 'disallow', 'crawl-delay'):
                fresh = False
                for agent in agents:
                    groups[agent].append((key, value))
        named = [rules for agent, rules in groups.items() if agent and agent != '*' and agent in AGENT]
        self.rules = []
        for key, value in (named[0] if named else groups.get('*', [])):
            if key == 'crawl-delay':
                try:
                    self.delay = min(float(value), 10)
                except ValueError:
                    pass
            elif value:
                pattern = '.*'.join(re.escape(piece) for piece in value.rstrip('$').split('*'))
                self.rules.append((len(value), key == 'allow', re.compile(pattern + ('$' if value.endswith('$') else ''))))

    def can_fetch(self, url):
        if self.allow_all or self.disallow_all:
            return self.allow_all
        parts = urlsplit(url)
        path = (parts.path or '/') + ('?' + parts.query if parts.query else '')
        if path == '/robots.txt':
            return True
        hits = [(length, allow) for length, allow, pattern in self.rules if pattern.match(path)]
        return max(hits)[1] if hits else True


def allowed(url):
    """What robots.txt says for our user agent. No robots.txt (4xx) allows; one we cannot read (5xx, network
    error) disallows, as RFC 9309 asks."""
    parts = urlsplit(url)
    origin = f'{parts.scheme}://{parts.netloc}'
    rules = _robots.get(origin)
    if rules is None:
        try:
            _, status, _, body = _follow(origin + '/robots.txt', {}, None, 500_000, 0.5, False)
        except FetchError as e:
            if e.status == 0:  # refused before any request: say so rather than blame robots.txt
                raise
            status, body = 599, b''
        if status == 200:
            rules = Robots(body[:500_000].decode('utf-8', 'replace'))
        else:
            rules = Robots(allow_all=400 <= status < 500 and status != 429, disallow_all=True)
        _delay[parts.hostname] = rules.delay
        _robots[origin] = rules
    return rules.can_fetch(url)


def _cache_dir():
    root = os.environ.get('FOOTHOLD_CACHE') or os.path.join(
        os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache'), 'foothold')
    return Path(root)


def get(url, *, headers=None, data=None, robots=True, max_bytes=MAX_BYTES, delay=1.0):
    """A text page or API answer. Raises FetchError with a reason a person can read."""
    key = hashlib.sha256(url.encode() + b'\n' + (data or b'')).hexdigest()
    path = _cache_dir() / f'{key}.json'
    if use_cache and path.exists() and time.time() - path.stat().st_mtime < CACHE_TTL:
        try:
            return Page(**json.loads(path.read_text(encoding='utf-8')))
        except (ValueError, TypeError):
            pass
    final, status, hdrs, body = _follow(url, headers or {}, data, max_bytes, delay, robots)
    if status != 200:
        raise FetchError(f'{final}: HTTP {status}', status)
    if len(body) > max_bytes:
        raise FetchError(f'{final}: larger than {max_bytes // 1_000_000} MB')
    ctype = hdrs.get('content-type', 'text/html').lower()
    if not ctype.startswith(TEXT_TYPES):
        raise FetchError(f'{final}: not text ({ctype.split(";")[0]})')
    charset = ctype.split('charset=')[-1].split(';')[0].strip(' "\'') if 'charset=' in ctype else 'utf-8'
    text = body.decode(charset if charset in CHARSETS else 'utf-8', 'replace')
    page = Page(final, status, hdrs, text, date.today().isoformat())
    if use_cache:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)  # answers of paid APIs stay private
        path.touch(mode=0o600)
        path.write_text(json.dumps(asdict(page)), encoding='utf-8')
    return page


def get_json(url, **kwargs):
    kwargs.setdefault('robots', False)  # documented public APIs are used under their own terms
    host = urlsplit(url).hostname
    try:
        page = get(url, **kwargs)
    except FetchError as e:  # keep query strings out of messages shown to the user
        raise FetchError(f'{host}: HTTP {e.status}' if e.status else str(e), e.status)
    try:
        return json.loads(page.text)
    except ValueError:
        raise FetchError(f'{host}: the answer is not JSON')
