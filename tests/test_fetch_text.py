import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from foothold import fetch, text


class Site(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        routes = {
            '/robots.txt': (200, 'text/plain', b'User-agent: *\nDisallow: /private/\n', {}),
            '/': (200, 'text/html', b'<title>Home</title><p>Hello</p>', {}),
            '/private/x': (200, 'text/html', b'secret', {}),
            '/to-private': (302, 'text/html', b'', {'Location': '/private/x'}),
            '/to-file': (302, 'text/html', b'', {'Location': 'file:///etc/passwd'}),
            '/big': (200, 'text/html', b'x' * 5000, {}),
            '/image': (200, 'image/png', b'\x89PNG', {}),
            '/to-bad': (302, 'text/html', b'', {'Location': 'http://[x'}),
            '/to-inside': (302, 'text/html', b'', {'Location': 'http://127.0.0.2:9/admin'}),
            '/to-other': (302, 'text/html', b'', {'Location': getattr(self.server, 'other', '') + '/echo'}),
            '/echo': (200, 'text/plain', json.dumps(dict(self.headers)).encode(), {}),
            '/bomb': (200, 'text/html', gzip.compress(b'0' * 5_000_000), {'Content-Encoding': 'gzip'}),
        }
        status, ctype, body, extra = routes.get(self.path, (404, 'text/plain', b'', {}))
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def site(monkeypatch, tmp_path):
    server = ThreadingHTTPServer(('127.0.0.1', 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(fetch, 'is_public_ip', lambda ip: True)  # the test server is local
    monkeypatch.setattr(fetch, '_robots', {})
    monkeypatch.setattr(fetch, '_last', {})
    monkeypatch.setenv('FOOTHOLD_CACHE', str(tmp_path))
    yield f'http://127.0.0.1:{server.server_port}'
    server.shutdown()


@pytest.mark.parametrize('ip', ['127.0.0.1', '10.0.0.5', '192.168.1.1', '169.254.169.254', '100.64.0.1', '::1',
                                'fe80::1', '::ffff:127.0.0.1', 'fd00::1', '0.0.0.0', 'not-an-ip'])
def test_private_addresses_are_refused(ip):
    assert not fetch.is_public_ip(ip)


def test_public_address_is_accepted():
    assert fetch.is_public_ip('93.184.216.34')


def test_loopback_is_never_fetched(tmp_path, monkeypatch):
    monkeypatch.setenv('FOOTHOLD_CACHE', str(tmp_path))
    monkeypatch.setattr(fetch, '_robots', {})
    with pytest.raises(fetch.FetchError, match='not a public address'):
        fetch.get('http://127.0.0.1:9/')


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'ftp://example.com/x', 'gopher://example.com'])
def test_only_http_is_fetched(url):
    with pytest.raises(fetch.FetchError, match='not an http'):
        fetch.get(url, robots=False)


def test_page_is_fetched_and_cached(site, monkeypatch):
    assert 'Hello' in fetch.get(site + '/').text
    monkeypatch.setattr(fetch, '_follow', lambda *a: pytest.fail('the second read must come from the cache'))
    assert 'Hello' in fetch.get(site + '/').text


def test_robots_txt_is_obeyed(site):
    with pytest.raises(fetch.FetchError, match='robots.txt'):
        fetch.get(site + '/private/x')


def test_redirects_are_checked_again(site):
    with pytest.raises(fetch.FetchError, match='robots.txt'):
        fetch.get(site + '/to-private')
    with pytest.raises(fetch.FetchError, match='not an http'):
        fetch.get(site + '/to-file')


def test_odd_addresses_are_errors_not_crashes(site):
    for path in ('/caf\u00e9', '/to-bad'):
        with pytest.raises(fetch.FetchError):
            fetch.get(site + path)
    with pytest.raises(fetch.FetchError):
        fetch.get('http://[x/')


def test_a_redirect_to_a_private_address_is_refused(site, monkeypatch):
    monkeypatch.setattr(fetch, 'is_public_ip', lambda ip: ip != '127.0.0.2')
    with pytest.raises(fetch.FetchError, match='not a public address'):
        fetch.get(site + '/to-inside')


def test_keys_do_not_follow_a_redirect_to_another_host(site):
    other = ThreadingHTTPServer(('127.0.0.1', 0), Site)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        fetch.use_cache, saved = False, fetch.use_cache
        import gc
        server = next(o for o in gc.get_objects() if isinstance(o, ThreadingHTTPServer) and site.endswith(str(o.server_port)))
        server.other = f'http://127.0.0.1:{other.server_port}'
        seen = json.loads(fetch.get(site + '/to-other', headers={'Authorization': 'Bearer k', 'X-Api-Key': 'k', 'X-Other': '1'}).text)
        assert 'Authorization' not in seen and 'X-Api-Key' not in seen and seen['X-Other'] == '1'
    finally:
        fetch.use_cache = saved
        other.shutdown()


def test_a_compressed_answer_cannot_exceed_the_size_cap(site):
    with pytest.raises(fetch.FetchError, match='larger than'):
        fetch.get(site + '/bomb', max_bytes=100_000)


def test_robots_rules_follow_rfc_9309():
    rules = fetch.Robots('User-agent: *\nAllow: /\nDisallow: /careers\nDisallow: /*?\nDisallow: /*.pdf$\nAllow: /careers/open\n'
                         'Crawl-delay: 3\n\nUser-agent: otherbot\nDisallow: /\n')
    can = lambda path: rules.can_fetch('https://x.example' + path)
    assert can('/') and can('/about') and can('/careers/open/1') and can('/a.pdf.html')
    assert not can('/careers') and not can('/careers/team') and not can('/search?q=1') and not can('/a/b.pdf')
    assert rules.delay == 3
    named = fetch.Robots('User-agent: *\nDisallow:\n\nUser-agent: Foothold\nDisallow: /private\n')
    assert not named.can_fetch('https://x.example/private') and named.can_fetch('https://x.example/other')


@pytest.mark.parametrize('status,expected', [(404, True), (403, True), (500, False), (429, False)])
def test_missing_robots_allows_and_broken_robots_forbids(monkeypatch, status, expected):
    monkeypatch.setattr(fetch, '_robots', {})
    monkeypatch.setattr(fetch, '_follow', lambda *a: (a[0], status, {}, b''))
    assert fetch.allowed('https://x.example/page') is expected


def test_hidden_text_is_not_evidence():
    page = ('<p>Shown</p><div hidden>secret <div>nested</div> more</div><span style="display: none">gone</span>'
            '<p aria-hidden="true">icon</p><input type="hidden" value="x"><p>Also shown</p>')
    assert text.to_text(page) == 'Shown\nAlso shown'


def test_size_and_type_limits(site):
    with pytest.raises(fetch.FetchError, match='larger than'):
        fetch.get(site + '/big', max_bytes=1000)
    with pytest.raises(fetch.FetchError, match='not text'):
        fetch.get(site + '/image')


def test_html_to_text_skips_scripts_and_keeps_links():
    doc = text.parse('<title> A | B </title><script>var x = "no"</script><p>Hi <a href="/about">About us</a></p>'
                     '<link rel="alternate" type="application/rss+xml" href="/feed.xml">', 'https://x.com/')
    assert doc.text == 'Hi About us' and doc.title == 'A | B'
    assert doc.links == [('https://x.com/about', 'About us')] and doc.feeds == ['https://x.com/feed.xml']


def test_contact_details_are_removed_but_dates_stay():
    out = text.scrub('Write to jane.doe@corp.example or call +1 (415) 555-0100 before 2026-10-12.')
    assert '@' not in out and '555' not in out and '2026-10-12' in out
    assert '45' not in text.scrub('Standard : 01 23 45 67 89 ou +33 1 23 45 67 89')
    import time
    start = time.monotonic()
    text.scrub('a' * 200_000)
    assert time.monotonic() - start < 2


def test_quotes_match_despite_typography():
    assert text.norm('“Cloud Security” – hiring') == '"cloud security" - hiring'
    assert text.iso_date('20261120T120000Z') == '2026-11-20' and text.iso_date('soon') is None
