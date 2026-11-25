from datetime import date, timedelta

import pytest

from foothold import fetch, sources

RECENT = (date.today() - timedelta(days=20)).isoformat()
OLD = (date.today() - timedelta(days=400)).isoformat()
OFFER = {'keywords': ['IAM', 'cloud security'], 'competitors': ['Okta']}


def page(url, html):
    from foothold.text import parse
    p = fetch.Page(url, 200, {'server': 'cloudflare'}, html, date.today().isoformat())
    return ('home', p, parse(html, url))


def test_job_boards_are_found_in_page_links():
    pages = [page('https://acme.com/careers', '<a href="https://boards.greenhouse.io/acme/jobs/1">x</a>'
                  '<a href="https://jobs.lever.co/acme-inc">y</a> <a href="https://jobs.ashbyhq.com/acme">z</a>'
                  '<a href="https://acme.wd5.myworkdayjobs.com/en-US/External">w</a>')]
    assert sources.find_boards(pages) == [('greenhouse', 'acme'), ('lever', 'acme-inc'), ('ashby', 'acme'),
                                          ('workday', 'acme', 'wd5', 'External')]


def test_stack_reads_html_and_headers():
    found = sources.stack('acme.com', [page('https://acme.com/', '<script src="https://js.hs-scripts.com/1.js">')])
    assert 'HubSpot' in found.text and 'Cloudflare' in found.text and 'Marketo' not in found.text


def test_jobs_summary_and_matching_postings(monkeypatch):
    listed = [{'title': 'Senior IAM Engineer', 'dept': 'Security', 'location': 'Paris', 'date': RECENT,
               'url': 'https://acme.com/jobs/1', 'text': 'Own least privilege. Reach us at jobs@acme.com', 'company': 'Acme'},
              {'title': 'Account Executive', 'dept': 'Sales', 'location': 'NYC', 'date': RECENT,
               'url': 'https://acme.com/jobs/2', 'text': 'Sell things.', 'company': 'Acme'}]
    monkeypatch.setattr(sources, '_greenhouse', lambda slug: (listed, 'https://boards.greenhouse.io/acme'))
    gaps = []
    out = sources.jobs('Acme', 'acme.com', [page('https://acme.com/', 'boards.greenhouse.io/acme')], OFFER, gaps)
    assert [e.kind for e in out] == ['jobs', 'job'] and not gaps
    assert '2 open roles' in out[0].text and 'Senior IAM Engineer (posted' in out[0].text
    assert out[1].published == RECENT and '@' not in out[1].text


def test_a_guessed_board_must_name_the_company(monkeypatch):
    other = [{'title': 'Baker', 'dept': None, 'location': None, 'date': None, 'url': 'https://other.io/1',
              'text': 'Bread', 'company': 'Acme Bakery'}]
    monkeypatch.setattr(sources, '_greenhouse', lambda slug: (other, 'u'))
    for name in ('_lever', '_ashby'):
        monkeypatch.setattr(sources, name, lambda slug: (_ for _ in ()).throw(fetch.FetchError('HTTP 404', 404)))
    gaps = []
    assert sources.jobs('Acme', 'acme.com', [], OFFER, gaps) == [] and 'no public' in gaps[0]


def test_an_accented_name_only_tries_plain_board_names(monkeypatch):
    tried = []

    def load(slug):
        tried.append(slug)
        raise fetch.FetchError('HTTP 404', 404)
    for name in ('_greenhouse', '_lever', '_ashby'):
        monkeypatch.setattr(sources, name, load)
    assert sources.jobs("L'Oréal", 'loreal.com', [page('https://loreal.com/', 'boards.greenhouse.io/é')], OFFER, []) == []
    assert tried and all(slug.isascii() for slug in tried)


def test_unlocker_only_answers_a_403_and_obeys_robots(monkeypatch):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        if 'brightdata' in url:
            return fetch.Page(url, 200, {}, 'unlocked', RECENT)
        raise fetch.FetchError('refused', status['code'])
    status = {'code': 429}
    monkeypatch.setattr(fetch, 'get', get)
    monkeypatch.setenv('BRIGHTDATA_API_KEY', 'k'), monkeypatch.setenv('BRIGHTDATA_ZONE', 'z')
    monkeypatch.setattr(fetch, 'allowed', lambda url: False)
    for code in (429, 503, 403):  # slow-down answers are respected; a 403 is not retried where robots.txt forbids
        status['code'] = code
        with pytest.raises(fetch.FetchError):
            sources._page('https://acme.com/')
    assert not any('brightdata' in c for c in calls)
    monkeypatch.setattr(fetch, 'allowed', lambda url: True)
    assert sources._page('https://acme.com/').text == 'unlocked'


def test_evidence_addresses_carry_no_markup():
    item = sources._ev('news', 'https://x.example/a\n<b>|"', 'T', 'body')
    assert item.url == 'https://x.example/a%0A%3Cb%3E%7C%22'


def test_feed_items_without_an_xml_parser():
    xml = ('<rss><item><title><![CDATA[Acme raises $50M]]></title><link>https://acme.com/a</link>'
           '<pubDate>Tue, 03 Nov 2026 10:00:00 GMT</pubDate><description>&lt;p&gt;Series C&lt;/p&gt;</description></item>'
           '<entry><title>Atom one</title><link href="https://acme.com/b"/><updated>2026-10-01T00:00:00Z</updated></entry></rss>')
    assert list(sources._feed_items(xml)) == [('Acme raises $50M', 'https://acme.com/a', '2026-11-03', 'Series C'),
                                              ('Atom one', 'https://acme.com/b', '2026-10-01', '')]


def test_news_keeps_recent_headlines_that_name_the_company(monkeypatch):
    stamp = RECENT.replace('-', '') + 'T080000Z'
    monkeypatch.setattr(fetch, 'get_json', lambda url, **k: {'articles': [
        {'title': 'Acme buys a rival', 'url': 'https://news.example/1', 'seendate': stamp, 'domain': 'news.example'},
        {'title': 'Acme buys a rival', 'url': 'https://copy.example/1', 'seendate': stamp, 'domain': 'copy.example'},
        {'title': 'Unrelated story', 'url': 'https://news.example/2', 'seendate': stamp, 'domain': 'news.example'}]})
    out = sources.news('Acme', 'acme.com', [], 180, [])
    assert len(out) == 1 and out[0].published == RECENT and out[0].kind == 'news'


def test_news_falls_back_to_a_feed_and_drops_old_headlines(monkeypatch):
    def refused(url, **kwargs):
        raise fetch.FetchError('api.gdeltproject.org: HTTP 429', 429)
    stamp = date.fromisoformat(RECENT).strftime('%a, %d %b %Y 08:00:00 GMT')
    rss = (f'<rss><item><title>Acme opens a Paris office</title><link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;'
           f'url=https%3a%2f%2fnews.example%2facme-paris&amp;c=1</link><pubDate>{stamp}</pubDate></item>'
           f'<item><title>Acme founded</title><link>https://news.example/old</link><pubDate>Sun, 27 May 2018 08:00:00 GMT</pubDate></item></rss>')
    monkeypatch.setattr(fetch, 'get_json', refused)
    monkeypatch.setattr(fetch, 'get', lambda url, **k: fetch.Page(url, 200, {}, rss, RECENT))
    gaps = []
    out = sources.news('Acme', 'acme.com', [], 180, gaps)
    assert [(e.url, e.published) for e in out] == [('https://news.example/acme-paris', RECENT)] and not gaps


def test_edgar_reports_events_by_item_and_never_reads_the_filing(monkeypatch):
    def fake(url, **kwargs):
        assert 'foothold sdr@corp.example' == kwargs['headers']['User-Agent']
        if 'company_tickers' in url:
            return {'0': {'cik_str': 42, 'ticker': 'ACME', 'title': 'Acme, Inc.'}}
        if 'submissions' in url:
            return {'name': 'Acme, Inc.', 'sic': '7372', 'sicDescription': 'Prepackaged Software', 'filings': {'recent': {
                'form': ['8-K', '10-K', '8-K'], 'filingDate': [RECENT, OLD, OLD], 'accessionNumber': ['0001-26-1', '0001-25-9', '0001-25-2'],
                'primaryDocument': ['a.htm', 'b.htm', 'c.htm'], 'items': ['5.02,9.01', '', '2.02']}}}
        return {'units': {'USD': [{'fp': 'FY', 'form': '10-K', 'start': '2025-01-01', 'end': '2025-12-31',
                                   'val': 2500000000, 'filed': OLD}]}}
    monkeypatch.setattr(fetch, 'get_json', fake)
    out = sources.edgar('Acme', None, 180, 'sdr@corp.example')
    assert len(out) == 2 and 'USD 2,500,000,000' in out[0].text and 'Prepackaged Software' in out[0].text
    assert out[1].published == RECENT and 'Departure or appointment of directors or officers' in out[1].text
    assert out[1].url == 'https://www.sec.gov/Archives/edgar/data/42/0001261/a.htm'


def test_french_register_ignores_directors(monkeypatch):
    monkeypatch.setattr(fetch, 'get_json', lambda url, **k: {'results': [
        {'siren': '123456789', 'nom_complet': 'ACME', 'nom_raison_sociale': 'ACME', 'date_creation': '2013-07-15',
         'activite_principale': '62.01Z', 'categorie_entreprise': 'ETI', 'tranche_effectif_salarie': '42',
         'annee_tranche_effectif_salarie': '2024', 'dirigeants': [{'nom': 'DUPONT', 'prenoms': 'JEANNE'}]}]})
    out = sources.france('Acme SAS')
    monkeypatch.setattr(fetch, 'get_json', lambda url, **k: {'results': [
        {'siren': '1', 'nom_complet': 'JEANNE DUPONT', 'nom_raison_sociale': 'JEANNE DUPONT', 'nature_juridique': '1000'}]})
    assert sources.france('Jeanne Dupont') is None
    assert '1,000-1,999' in out.text and 'DUPONT' not in out.text and 'JEANNE' not in out.text


def test_wikidata_needs_a_matching_website(monkeypatch):
    def claim(value):
        return [{'rank': 'normal', 'mainsnak': {'snaktype': 'value', 'datavalue': {'value': value}}}]

    def fake(url, **kwargs):
        if 'wbsearchentities' in url:
            return {'search': [{'id': 'Q1'}, {'id': 'Q2'}]}
        if 'Q1%7CQ2' in url:
            return {'entities': {
                'Q1': {'labels': {'en': {'value': 'Acme (band)'}}, 'claims': {'P856': claim('https://acmeband.example')}},
                'Q2': {'labels': {'mul': {'value': 'Acme'}}, 'descriptions': {'en': {'value': 'software company'}}, 'claims': {
                    'P856': claim('https://www.acme.com/'), 'P17': claim({'id': 'Q30'}),
                    'P571': claim({'time': '+2010-00-00T00:00:00Z'}),
                    'P1128': [{'rank': 'normal', 'mainsnak': {'snaktype': 'value', 'datavalue': {'value': {'amount': '+5200'}}},
                               'qualifiers': {'P585': [{'datavalue': {'value': {'time': '+2025-12-31T00:00:00Z'}}}]}}]}}}}
        return {'entities': {'Q30': {'labels': {'en': {'value': 'United States'}}}}}
    monkeypatch.setattr(fetch, 'get_json', fake)
    found, domain = sources.wikidata('Acme', 'acme.com')
    assert domain == 'acme.com' and found.url.endswith('/Q2')
    assert 'Employees: 5,200 (as of 2025-12-31)' in found.text and 'Country: United States' in found.text and 'Founded: 2010' in found.text
    assert sources.wikidata('Acme', 'other.org') is None
    # A bare name resolves only to an entry with that name that looks like a company (Q1 is a band)
    assert sources.wikidata('Acme', None)[1] == 'acme.com'
    assert sources.wikidata('Acme (band)', None) is None and sources.wikidata('Acme Rockets', None) is None


def test_wikidata_prefers_the_exact_name_over_a_subsidiary(monkeypatch):
    def entry(site):
        return {'claims': {'P856': [{'rank': 'normal', 'mainsnak': {'snaktype': 'value', 'datavalue': {'value': site}}}],
                           'P112': [{'rank': 'normal', 'mainsnak': {'snaktype': 'value', 'datavalue': {'value': {'id': 'Q9'}}}}]}}

    def fake(url, **kwargs):
        if 'wbsearchentities' in url:
            return {'search': [{'id': 'Q1', 'label': 'Acme GmbH'}, {'id': 'Q2', 'label': 'Acme'}]}
        return {'entities': {'Q1': entry('https://acme.de') | {'labels': {'en': {'value': 'Acme GmbH'}}},
                             'Q2': entry('https://acme.fr') | {'labels': {'en': {'value': 'Acme'}}}}}
    monkeypatch.setattr(fetch, 'get_json', fake)
    assert sources.wikidata('Acme', None)[1] == 'acme.fr'


@pytest.mark.parametrize('domain,head,expected', [
    ('datadoghq.com', '<meta property="og:site_name" content="Datadog"><title>Cloud Monitoring as a Service | Datadog</title>', 'Datadog'),
    ('klarna.com', '<meta property="og:site_name" content="Klarna FR"><title>Payez plus tard</title>', 'Klarna'),
    ('snowflake.com', '<title>Snowflake AI Data Cloud</title>', 'Snowflake'),
    ('crowdstrike.com', '<title>CrowdStrike.com: Stop breaches</title>', 'CrowdStrike'),
    ('bbc.co.uk', '<title>BBC - Home</title>', 'BBC'),
    ('linkedin.com', '<title>Checking your browser</title>', None),
])
def test_only_a_name_that_matches_the_domain_is_read_from_the_home_page(domain, head, expected):
    from foothold.text import parse
    assert sources.site_name(domain, parse(f'<html><head>{head}</head><body></body></html>', f'https://{domain}/')) == expected


def test_collect_turns_failures_into_gaps(monkeypatch):
    def down(*args, **kwargs):
        raise fetch.FetchError('example.org: HTTP 503', 503)
    monkeypatch.setattr(fetch, 'get', down)
    monkeypatch.setattr(fetch, 'get_json', down)
    monkeypatch.delenv('FOOTHOLD_CONTACT', raising=False)
    monkeypatch.delenv('APOLLO_API_KEY', raising=False)
    name, domain, evidence, gaps = sources.collect({'domain': 'acme.com'}, {'keywords': [], 'competitors': []})
    assert (name, domain, evidence) == ('Acme', 'acme.com', [])
    assert any(g.startswith('Company site') for g in gaps) and any('FOOTHOLD_CONTACT' in g for g in gaps)
    with pytest.raises(sources.Unresolved):
        sources.collect({'name': 'Acme'}, {})
