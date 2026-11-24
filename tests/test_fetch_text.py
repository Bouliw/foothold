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


def test_quotes_match_despite_typography():
    assert text.norm('“Cloud Security” – hiring') == '"cloud security" - hiring'
    assert text.iso_date('20261120T120000Z') == '2026-11-20' and text.iso_date('soon') is None
