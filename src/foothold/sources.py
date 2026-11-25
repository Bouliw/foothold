"""Where the evidence comes from. Each source returns Evidence items (address, dates, text) or raises FetchError;
what a source could not give ends up in `gaps`, never in a guess.

Company pages are read like a polite crawler would (robots.txt applies). The other sources are documented public
APIs, called under their own terms: Wikidata, Greenhouse, Lever, Ashby, GDELT, SEC EDGAR, the French company
register, and Apollo when a key is set."""
import html
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from . import fetch
from .fetch import FetchError
from .text import clip, iso_date, parse, scrub, to_text


@dataclass
class Evidence:
    kind: str       # site, stack, jobs, job, news, press, registry, filing, apollo
    url: str
    title: str
    text: str
    published: str | None = None
    fetched: str = ''
    id: str = ''


URL_SAFE = ":/?#[]@!$&'*+,;=%~.-_"


def _today():
    return date.today().isoformat()


def _ev(kind, url, title, body, published=None):
    # No "<" survives, so a page cannot close its evidence block or open a fake one; no address can carry a
    # line break or markup into a brief
    def plain(text):
        return scrub(text).replace('<', '‹')
    url = quote(str(url), safe=URL_SAFE)
    return Evidence(kind, url, ' '.join(plain(str(title)[:400]).split())[:200], plain(body), published, _today())


def same_site(host, domain):
    host = (host or '').lower().removeprefix('www.')
    return host == domain or host.endswith('.' + domain)


SUFFIXES = {'inc', 'incorporated', 'corp', 'corporation', 'ltd', 'limited', 'plc', 'nv', 'bv', 'sa', 'sas', 'se',
            'ag', 'gmbh', 'llc', 'lp', 'co', 'company', 'holdings', 'holding', 'group', 'the'}


def name_key(name):
    words = re.sub(r'[^\w\s]', ' ', (name or '').lower()).split()
    return ' '.join(w for w in words if w not in SUFFIXES)


# ---------------------------------------------------------------- company site

SECTIONS = ('about', 'pricing', 'customers', 'careers', 'press', 'security', 'blog')
SECTION_WORDS = {'about': 'about|about-us|company', 'pricing': 'pricing|plans', 'customers': 'customers|case-studies',
                 'careers': 'careers|jobs|join-us', 'press': 'press|newsroom|news|media',
                 'security': 'security|trust|compliance', 'blog': 'blog'}
SECTION_RE = {name: re.compile(rf'/(?:{words})/?$', re.IGNORECASE) for name, words in SECTION_WORDS.items()}

# Third-party tools that leave a recognisable trace in the HTML or the response headers
TECH = {
    'Google Tag Manager': r'googletagmanager\.com', 'Google Analytics': r'google-analytics\.com',
    'Segment': r'cdn\.segment\.com', 'HubSpot': r'js\.hs-scripts\.com|js\.hsforms\.net|hs-analytics\.net',
    'Marketo': r'munchkin\.marketo\.net|mktoForms2|mktoweb\.com', 'Pardot': r'pi\.pardot\.com|pardot\.com/pd\.js',
    'Intercom': r'widget\.intercom\.io|intercomcdn\.com', 'Drift': r'js\.driftt\.com',
    'Zendesk': r'static\.zdassets\.com', 'Qualified': r'js\.qualified\.com', '6sense': r'j\.6sc\.co',
    'Demandbase': r'tag\.demandbase\.com', 'Clearbit': r'tag\.clearbitscripts\.com',
    'Optimizely': r'cdn\.optimizely\.com', 'Hotjar': r'static\.hotjar\.com', 'FullStory': r'fullstory\.com/s/fs\.js',
    'Amplitude': r'cdn\.amplitude\.com', 'Mixpanel': r'cdn\.mxpnl\.com', 'Heap': r'cdn\.heapanalytics\.com',
    'Sentry': r'browser\.sentry-cdn\.com|ingest\.sentry\.io', 'Datadog RUM': r'datadoghq-browser-agent\.com',
    'New Relic': r'js-agent\.newrelic\.com|bam\.nr-data\.net', 'Next.js': r'/_next/static/|__NEXT_DATA__',
    'Nuxt': r'/_nuxt/', 'Gatsby': r'id="___gatsby"', 'WordPress': r'/wp-content/|/wp-includes/',
    'Webflow': r'assets\.website-files\.com|data-wf-site', 'Contentful': r'ctfassets\.net',
    'Shopify': r'cdn\.shopify\.com', 'Stripe': r'js\.stripe\.com', 'OneTrust': r'cdn\.cookielaw\.org|onetrust\.com',
    'Cookiebot': r'consent\.cookiebot\.com', 'TrustArc': r'consent\.trustarc\.com', 'Okta': r'oktacdn\.com',
    'Auth0': r'cdn\.auth0\.com', 'reCAPTCHA': r'google\.com/recaptcha', 'Cloudflare': r'\x00header server: cloudflare',
    'Vercel': r'\x00header x-vercel-id:', 'Fastly': r'\x00header (?:x-served-by: cache-|x-fastly)',
    'Amazon CloudFront': r'\x00header x-amz-cf-id:', 'Akamai': r'\x00header server: akamai',
}

ATS = {
    'greenhouse': re.compile(r'(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([\w-]+)'
                             r'|boards-api\.greenhouse\.io/v1/boards/([\w-]+)', re.ASCII),
    'lever': re.compile(r'jobs\.(?:eu\.)?lever\.co/([\w-]+)', re.ASCII),
    'ashby': re.compile(r'jobs\.ashbyhq\.com/([\w.-]+)', re.ASCII),
    'workday': re.compile(r'([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)', re.ASCII),
}


def _page(url):
    """A page, through Bright Data's unlocker when the site answers 403, robots.txt allows the page and the user
    has set a key and a zone. "Slow down" answers (429, 503) are respected, not worked around."""
    try:
        return fetch.get(url)
    except FetchError as e:
        key, zone = os.environ.get('BRIGHTDATA_API_KEY'), os.environ.get('BRIGHTDATA_ZONE')
        if not (key and zone and e.status == 403 and fetch.allowed(url)):
            raise
        body = json.dumps({'zone': zone, 'url': url, 'format': 'raw'}).encode()
        page = fetch.get('https://api.brightdata.com/request', data=body, robots=False,
                         headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
        return fetch.Page(url, 200, {}, page.text, page.fetched)


def crawl_site(domain, gaps):
    """The home page and one page per useful section. Returns the evidence, the raw pages and a company name."""
    home, error = None, None
    for url in (f'https://{domain}/', f'https://www.{domain}/'):
        try:
            home = _page(url)
            break
        except FetchError as e:
            error = error or e
    if home is None:
        raise error or FetchError(f'{domain}: unreachable')
    doc = parse(home.text, home.url)
    picks = {}
    for href, _ in doc.links:
        parts = urlsplit(href)
        if parts.scheme not in ('http', 'https') or not same_site(parts.hostname, domain):
            continue
        sub = (parts.hostname or '').split('.')[0]
        for name in SECTIONS:
            hit = SECTION_RE[name].search(parts.path) or (parts.path in ('', '/') and re.fullmatch(SECTION_WORDS[name], sub))
            clean = href.split('#')[0].split('?')[0]
            if hit and len(clean) < len(picks.get(name, 'x' * 999)):
                picks[name] = clean
    pages = [('home', home, doc)]
    for name in SECTIONS:
        if name in picks:
            try:
                page = _page(picks[name])
                pages.append((name, page, parse(page.text, page.url)))
            except FetchError as e:
                gaps.append(f'Company site, {name} page: {e}')
    evidence = []
    for name, page, d in pages:
        body = '\n'.join(x for x in (d.meta.get('description') or d.meta.get('og:description'), d.text) if x)
        if len(d.text) < 200:
            gaps.append(f'Company site, {name} page: almost no text without JavaScript ({page.url})')
        if body:
            # No date: the publication date in a page's meta tags is rarely kept up to date
            evidence.append(_ev('site', page.url, d.title or f'{domain} {name}', clip(body, 6000)))
    return evidence, pages, site_name(domain, doc)


def site_name(domain, doc):
    """The company name as its own home page writes it, or None. Only a name that matches the domain is trusted:
    a page title can be a slogan, a country edition ("Klarna FR") or a bot challenge."""
    label = domain.split('.')[0]
    names = [n for n in (doc.meta.get('og:site_name'), *re.split(r'\s[|–—:·-]\s|: ', doc.title)) if n and n.strip()]
    keys = [(n.strip(), name_key(n).replace(' ', '')) for n in names]
    for test in (lambda k: k == label, lambda k: len(k) > 3 and label.startswith(k)):  # "Datadog" for datadoghq
        if hit := next((n for n, k in keys if test(k)), None):
            return hit
    word = next((w for n in names for w in re.findall(r"[^\W_]+", n) if w.lower() == label), None)
    return word and (word if word != word.lower() else word.capitalize())


def stack(domain, pages):
    # Headers get a marker no page text can contain, so a page cannot fake one
    hay = '\n'.join('\n'.join(f'\x00header {k}: {v}' for k, v in page.headers.items()) + '\n' + page.text.replace('\x00', '')
                    for _, page, _ in pages).lower()
    found = [name for name, pattern in TECH.items() if re.search(pattern, hay, re.MULTILINE)]
    if not found:
        return None
    return _ev('stack', pages[0][1].url, f'Technologies visible on {domain}',
               f'Third-party technologies detected in the public HTML and response headers of {domain}: '
               f'{", ".join(found)}. Detection covers front-end and marketing tools only, '
               f'not the internal stack.')


# ---------------------------------------------------------------- job boards

def _greenhouse(slug):
    data = fetch.get_json(f'https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true', max_bytes=30_000_000)
    jobs = [{'title': j.get('title', ''), 'dept': (j.get('departments') or [{}])[0].get('name'),
             'location': (j.get('location') or {}).get('name'), 'date': iso_date(j.get('first_published') or j.get('updated_at')),
             'url': j.get('absolute_url', ''), 'text': to_text(html.unescape(j.get('content') or '')),
             'company': j.get('company_name')} for j in data.get('jobs', [])]
    return jobs, f'https://boards.greenhouse.io/{slug}'


def _lever(slug):
    data = fetch.get_json(f'https://api.lever.co/v0/postings/{slug}?mode=json', max_bytes=30_000_000)
    jobs = [{'title': j.get('text', ''), 'dept': (j.get('categories') or {}).get('team'),
             'location': (j.get('categories') or {}).get('location'),
             'date': datetime.fromtimestamp(j['createdAt'] / 1000, UTC).date().isoformat() if j.get('createdAt') else None,
             'url': j.get('hostedUrl', ''),
             'text': '\n'.join([j.get('descriptionPlain') or ''] + [to_text(x.get('content', '')) for x in j.get('lists', [])])}
            for j in data if isinstance(j, dict)]
    return jobs, f'https://jobs.lever.co/{slug}'


def _ashby(slug):
    data = fetch.get_json(f'https://api.ashbyhq.com/posting-api/job-board/{slug}', max_bytes=30_000_000)
    jobs = [{'title': j.get('title', ''), 'dept': j.get('department') or j.get('team'), 'location': j.get('location'),
             'date': iso_date(j.get('publishedAt')), 'url': j.get('jobUrl', ''), 'text': j.get('descriptionPlain') or ''}
            for j in data.get('jobs', [])]
    return jobs, f'https://jobs.ashbyhq.com/{slug}'


def _workday(tenant, shard, site):
    base = f'https://{tenant}.{shard}.myworkdayjobs.com'
    body = json.dumps({'appliedFacets': {}, 'limit': 20, 'offset': 0, 'searchText': ''}).encode()
    # Not a documented API: treated as a page, so robots.txt decides
    page = fetch.get(f'{base}/wday/cxs/{tenant}/{site}/jobs', data=body, robots=True,
                     headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
    try:
        data = json.loads(page.text)
    except ValueError:
        raise FetchError(f'{base}: the answer is not JSON')
    jobs = [{'title': j.get('title', ''), 'dept': None, 'location': j.get('locationsText'), 'date': None,
             'url': f'{base}/{site}{j.get("externalPath", "")}', 'text': ''} for j in data.get('jobPostings', [])]
    return jobs, f'{base}/{site}', data.get('total')


def find_boards(pages):
    hay = '\n'.join(page.url + '\n' + page.text for _, page, _ in pages)
    boards = []
    for ats, pattern in ATS.items():
        for m in pattern.finditer(hay):
            key = (ats, *[g for g in m.groups() if g])
            if key not in boards and key[1] not in ('embed', 'api', 'v1'):
                boards.append(key)
    return boards


def jobs(name, domain, pages, offer, gaps):
    """The public job board: one summary and the few postings that match the offer's keywords."""
    found, total, where, label = None, None, None, None
    for board in find_boards(pages)[:4]:
        try:
            if board[0] == 'workday':
                found, where, total = _workday(*board[1:])
            else:
                found, where = {'greenhouse': _greenhouse, 'lever': _lever, 'ashby': _ashby}[board[0]](board[1])
            label = board[0].capitalize()
            break
        except FetchError as e:
            gaps.append(f'Job board ({board[0]}): {e}')
    if found is None:
        # No link on the site: try the usual board names, and keep one only if it says it is this company
        guesses = [re.sub(r'[^a-z0-9-]', '', g) for g in (domain.split('.')[0], name_key(name).replace(' ', ''))]
        for slug in dict.fromkeys(g for g in guesses if g):
            for ats, load in (('Greenhouse', _greenhouse), ('Lever', _lever), ('Ashby', _ashby)):
                try:
                    listed, board_url = load(slug)
                except FetchError:
                    continue
                proof = ' '.join((j.get('company') or '') + ' ' + j['url'] + ' ' + j['text'][:3000] for j in listed[:5]).lower()
                if listed and (domain in proof or any(name_key(j.get('company')) == name_key(name) for j in listed[:5])):
                    found, where, label = listed, board_url, ats
                    break
            if found:
                break
    if not found:
        gaps.append('Job board: no public Greenhouse, Lever, Ashby or Workday board found for this company')
        return []
    words = [re.compile(rf'\b{re.escape(w.lower())}\b') for w in offer.get('keywords', []) + offer.get('competitors', [])]

    def score(job):
        title, body = job['title'].lower(), job['text'].lower()
        return 5 * sum(bool(w.search(title)) for w in words) + sum(bool(w.search(body)) for w in words)

    ranked = sorted(found, key=score, reverse=True)
    matches = [j for j in ranked if score(j) >= 5]  # a keyword in the title, or five different ones in the text
    teams = Counter(j['dept'] or 'Other' for j in found).most_common(8)
    lines = [f'{total or len(found)} open roles on the public {label} job board on {_today()}.']
    if len(teams) > 1:
        lines.append('Open roles by team: ' + ', '.join(f'{team} {n}' for team, n in teams) + '.')
    if matches:
        lines.append('Open roles that mention the offer keywords: ' + '; '.join(
            f'{j["title"]}' + (f' (posted {j["date"]})' if j['date'] else '') for j in matches[:15]) + '.')
    lines.append('Other open roles: ' + '; '.join(j['title'] for j in ranked[len(matches[:15]):][:25]) + '.')
    evidence = [_ev('jobs', where, f'{name} job board ({label})', '\n'.join(lines))]
    for job in matches[:4]:
        if job['text']:
            place = f' ({job["location"]})' if job['location'] else ''
            evidence.append(_ev('job', job['url'] or where, f'Job posting: {job["title"]}{place}',
                                clip(job['text'], 2500), job['date']))
    return evidence


# ---------------------------------------------------------------- news

def _feed_items(xml):
    """Title, link, date and summary of each RSS or Atom entry, read with patterns rather than an XML parser
    (feeds are untrusted and often malformed)."""
    def field(block, tag):
        m = re.search(rf'<{tag}\b[^>]*>(.*?)</{tag}>', block, re.DOTALL | re.IGNORECASE)
        value = re.sub(r'^<!\[CDATA\[|\]\]>$', '', m[1].strip()) if m else ''
        return html.unescape(value)
    for block in re.findall(r'<(?:item|entry)\b.*?</(?:item|entry)>', xml[:500_000], re.DOTALL | re.IGNORECASE)[:40]:
        href = re.search(r'<link\b[^>]*href="([^"]+)"', block)
        link = field(block, 'link') or (href[1] if href else '')
        when = field(block, 'pubDate') or field(block, 'published') or field(block, 'updated')
        try:
            day = parsedate_to_datetime(when).date().isoformat()
        except (TypeError, ValueError):
            day = iso_date(when)
        yield field(block, 'title'), link.strip(), day, to_text(field(block, 'description') or field(block, 'summary'))


def news(name, domain, pages, window, gaps):
    since = (date.today() - timedelta(days=window)).isoformat()
    evidence = []
    feeds = [f for _, _, d in pages for f in d.feeds if same_site(urlsplit(f).hostname, domain)]
    if feeds:
        try:
            for title, link, day, summary in _feed_items(fetch.get(feeds[0]).text):
                if title and link.startswith('http') and day and day >= since and len(evidence) < 8:
                    evidence.append(_ev('press', link, title, clip(f'{title}\n{summary}', 800), day))
        except FetchError as e:
            gaps.append(f'Company feed: {e}')
    headlines, errors = [], []
    try:
        query = urlencode({'query': f'"{name}"', 'mode': 'ArtList', 'format': 'json', 'maxrecords': 50,
                           'timespan': f'{window}d', 'sort': 'DateDesc'})
        data = fetch.get_json('https://api.gdeltproject.org/api/v2/doc/doc?' + query, delay=6)
        headlines = [(str(a.get('title', '')), str(a.get('url', '')), iso_date(a.get('seendate')), str(a.get('domain', '')))
                     for a in data.get('articles', [])]
    except FetchError as e:
        errors.append(f'GDELT: {e}')
    if not headlines:
        # Second choice: the RSS output of Bing News, read like any page (robots.txt decides)
        try:
            feed = fetch.get('https://www.bing.com/news/search?' + urlencode({'q': f'"{name}"', 'format': 'rss', 'mkt': 'en-US'}))
            for title, link, day, _ in _feed_items(feed.text):
                target = parse_qs(urlsplit(link).query).get('url', [link])[0]
                headlines.append((title, target, day, urlsplit(target).hostname or ''))
        except FetchError as e:
            errors.append(f'Bing News: {e}')
    seen = set()
    for title, url, day, publisher in sorted(headlines, key=lambda h: h[2] or '', reverse=True):
        title = ' '.join(title.split())
        if name.lower() not in title.lower() or title.lower() in seen or not url.startswith('http') or not day or day < since:
            continue
        seen.add(title.lower())
        evidence.append(_ev('news', url, title, f'Headline: {title}\nPublisher: {publisher}', day))
        if len(seen) == 12:
            break
    if not seen:
        gaps.append(f'News: no headline naming {name} in the last {window} days' + (f' ({"; ".join(errors)})' if errors else ''))
    return evidence


# ---------------------------------------------------------------- public registers

WIKIDATA = 'https://www.wikidata.org/w/api.php?format=json&'


def _claims(entity, prop):
    out = []
    for claim in entity.get('claims', {}).get(prop, []):
        snak = claim.get('mainsnak', {})
        if claim.get('rank') != 'deprecated' and snak.get('snaktype') == 'value':
            out.append((claim.get('rank') == 'preferred', snak['datavalue']['value'], claim.get('qualifiers', {})))
    return [(value, quals) for _, value, quals in sorted(out, key=lambda c: not c[0])]


def _label(entity):
    names = entity.get('labels', {})  # Wikidata keeps a name shared by all languages under "mul"
    return (names.get('en') or names.get('mul') or {}).get('value')


def _when(qualifiers):
    snaks = qualifiers.get('P585', [])
    return iso_date(snaks[0]['datavalue']['value']['time'].replace('-00', '-01')) if snaks and 'datavalue' in snaks[0] else None


# industry, staff, exchange, CEO, revenue, legal form, founder, parent, owner
BUSINESS_PROPS = ('P452', 'P1128', 'P414', 'P169', 'P2139', 'P1454', 'P112', 'P749', 'P127')


def wikidata(name, domain):
    """The Wikidata entity whose official website is this domain (or, with no domain yet, the first company-like
    match that has one). Returns (evidence, domain) or None."""
    hits = fetch.get_json(WIKIDATA + urlencode({'action': 'wbsearchentities', 'search': name, 'language': 'en',
                                                 'type': 'item', 'limit': 5})).get('search', [])
    if not hits:
        return None
    entities = fetch.get_json(WIKIDATA + urlencode({'action': 'wbgetentities', 'ids': '|'.join(h['id'] for h in hits),
                                                     'props': 'claims|labels|descriptions', 'languages': 'en|mul'}))['entities']
    # The entry named exactly like the request before "Acme GmbH", a subsidiary that name_key also accepts
    for hit in sorted(hits, key=lambda h: (h.get('label') or '').lower() != name.lower()):
        entity = entities.get(hit['id'], {})
        hosts = [(urlsplit(str(v)).hostname or '').removeprefix('www.') for v, _ in _claims(entity, 'P856')]
        if not hosts or (domain and not any(same_site(h, domain) or same_site(domain, h) for h in hosts)):
            continue
        label = _label(entity) or hit.get('label') or ''
        # Resolving a bare name: the entry must carry that name and look like a company, or we do not guess
        if not domain and (name_key(label) != name_key(name) or not any(_claims(entity, p) for p in BUSINESS_PROPS)):
            continue
        refs = {prop: [v['id'] for v, _ in _claims(entity, prop) if isinstance(v, dict) and 'id' in v][:3]
                for prop in ('P452', 'P17', 'P159', 'P414')}
        ids = [i for group in refs.values() for i in group]
        labels = {}
        if ids:
            got = fetch.get_json(WIKIDATA + urlencode({'action': 'wbgetentities', 'ids': '|'.join(ids),
                                                       'props': 'labels', 'languages': 'en|mul'}))['entities']
            labels = {i: _label(e) or i for i, e in got.items()}
        label = label or name
        lines = [f'{label}: {entity.get("descriptions", {}).get("en", {}).get("value", "no description")}.',
                 f'Official website: {hosts[0]}.']
        for prop, text in (('P452', 'Industry'), ('P17', 'Country'), ('P159', 'Headquarters'), ('P414', 'Listed on')):
            if refs[prop]:
                lines.append(f'{text}: {", ".join(labels.get(i, i) for i in refs[prop])}.')
        if founded := _claims(entity, 'P571'):
            lines.append(f'Founded: {founded[0][0]["time"][1:5]}.')
        staff = sorted(((_when(q) or '', v) for v, q in _claims(entity, 'P1128')), reverse=True)
        if staff:
            count = int(float(staff[0][1]['amount']))
            lines.append(f'Employees: {count:,}' + (f' (as of {staff[0][0]})' if staff[0][0] else ' (no date given)') + '.')
        return _ev('registry', f'https://www.wikidata.org/wiki/{hit["id"]}', f'Wikidata: {label}', '\n'.join(lines)), hosts[0]
    return None


SEC_ITEMS = {'1.01': 'Entry into a material agreement', '1.02': 'Termination of a material agreement',
             '1.05': 'Material cybersecurity incident', '2.01': 'Completion of an acquisition or disposal',
             '2.02': 'Results of operations (earnings release)', '2.03': 'New direct financial obligation',
             '2.05': 'Costs of an exit or restructuring plan', '2.06': 'Material impairment',
             '3.02': 'Unregistered sale of equity securities', '5.02': 'Departure or appointment of directors or officers',
             '5.03': 'Change to bylaws or fiscal year', '5.07': 'Shareholder vote results',
             '7.01': 'Regulation FD disclosure', '8.01': 'Other events'}


def edgar(name, ticker, window, contact):
    """SEC EDGAR: registration, recent 8-K events (by item number, never the body, which names people) and the last
    annual revenue. The SEC asks every client to give a contact in its User-Agent."""
    headers = {'User-Agent': f'foothold {contact}', 'Accept-Encoding': 'gzip'}
    listed = fetch.get_json('https://www.sec.gov/files/company_tickers.json', headers=headers).values()
    match = next((c for c in listed if ticker and c['ticker'].upper() == ticker.upper()), None) \
        or next((c for c in listed if name_key(c['title']) == name_key(name)), None)
    if not match:
        return None
    cik = int(match['cik_str'])
    sub = fetch.get_json(f'https://data.sec.gov/submissions/CIK{cik:010d}.json', headers=headers)
    page = f'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik:010d}'
    recent = sub.get('filings', {}).get('recent', {})
    rows = list(zip(recent.get('form', []), recent.get('filingDate', []), recent.get('accessionNumber', []),
                    recent.get('primaryDocument', []), recent.get('items', [])))
    annual = next((r for r in rows if r[0] in ('10-K', '20-F', '40-F')), None)
    lines = [(f'{sub.get("name", match["title"])} is registered with the US Securities and Exchange Commission '
              f'(CIK {cik}, ticker {match["ticker"]}).'),
             f'SIC industry code: {sub.get("sic", "?")} {sub.get("sicDescription", "")}.']
    if annual:
        lines.append(f'Latest annual report: form {annual[0]} filed {annual[1]}.')
    for concept in ('RevenueFromContractWithCustomerExcludingAssessedTax', 'Revenues'):
        try:
            facts = fetch.get_json(f'https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/{concept}.json',
                                   headers=headers, max_bytes=10_000_000)
        except FetchError:
            continue
        years = [f for f in facts.get('units', {}).get('USD', []) if f.get('fp') == 'FY' and f.get('form') == '10-K'
                 and f.get('start') and (date.fromisoformat(f['end']) - date.fromisoformat(f['start'])).days > 350]
        if years:
            last = max(years, key=lambda f: (f['end'], f['filed']))
            lines.append(f'Annual revenue for the fiscal year ending {last["end"]}: USD {last["val"]:,} '
                         f'(form 10-K filed {last["filed"]}).')
            break
    evidence = [_ev('registry', page, f'SEC EDGAR: {sub.get("name", match["title"])}', '\n'.join(lines))]
    since = (date.today() - timedelta(days=window)).isoformat()
    for form, filed, accession, document, items in rows:
        if form == '8-K' and filed >= since and len(evidence) < 9:
            what = '; '.join(f'Item {i} {SEC_ITEMS[i]}' for i in items.split(',') if i in SEC_ITEMS and i != '9.01')
            if what:
                url = f'https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace("-", "")}/{document}'
                evidence.append(_ev('filing', url, f'SEC form 8-K filed {filed}',
                                    f'Form 8-K filed with the SEC on {filed}. Reported events: {what}.', filed))
    return evidence


FR_STAFF = {'00': '0', '01': '1-2', '02': '3-5', '03': '6-9', '11': '10-19', '12': '20-49', '21': '50-99',
            '22': '100-199', '31': '200-249', '32': '250-499', '41': '500-999', '42': '1,000-1,999',
            '51': '2,000-4,999', '52': '5,000-9,999', '53': '10,000 or more'}


def france(name):
    """The French company register (recherche-entreprises). Only company fields are read: the answer also lists
    directors, which are personal data and are ignored."""
    data = fetch.get_json('https://recherche-entreprises.api.gouv.fr/search?' + urlencode({'q': name, 'per_page': 5}))
    for c in data.get('results', []):
        if name_key(c.get('nom_raison_sociale') or c.get('nom_complet')) != name_key(name):
            continue
        if str(c.get('nature_juridique')) == '1000':  # a sole trader is registered under a person's name
            continue
        lines = [f'{c.get("nom_complet", name)} is registered in France (SIREN {c["siren"]}), created {c.get("date_creation", "?")}.',
                 f'Activity code (NAF): {c.get("activite_principale", "?")}. Size category: {c.get("categorie_entreprise") or "?"}.']
        if c.get('tranche_effectif_salarie') in FR_STAFF:
            lines.append(f'Employees in France: {FR_STAFF[c["tranche_effectif_salarie"]]} '
                         f'(bracket for {c.get("annee_tranche_effectif_salarie") or "an unknown year"}).')
        return _ev('registry', f'https://annuaire-entreprises.data.gouv.fr/entreprise/{quote(str(c["siren"]))}',
                   f'French company register: {c.get("nom_complet", name)}', '\n'.join(lines))
    return None


def apollo(domain, key):
    """Company fields from Apollo's organisation enrichment. People endpoints are never called."""
    org = fetch.get_json('https://api.apollo.io/api/v1/organizations/enrich?' + urlencode({'domain': domain}),
                         headers={'X-Api-Key': key, 'Accept': 'application/json'}).get('organization') or {}
    fields = (('Industry', 'industry'), ('Estimated employees', 'estimated_num_employees'), ('Founded', 'founded_year'),
              ('Annual revenue', 'annual_revenue_printed'), ('Total funding', 'total_funding_printed'),
              ('Latest funding stage', 'latest_funding_stage'), ('Latest funding date', 'latest_funding_round_date'),
              ('Description', 'short_description'))
    lines = [f'{label}: {org[field]}.' for label, field in fields if org.get(field)]
    if org.get('technology_names'):
        lines.append('Technologies: ' + ', '.join(org['technology_names'][:40]) + '.')
    if not lines:
        return None
    return _ev('apollo', org.get('website_url') or f'https://{domain}', f'Apollo: {org.get("name", domain)}',
               '\n'.join(lines), iso_date(org.get('latest_funding_round_date')))


# ---------------------------------------------------------------- everything for one account

class Unresolved(Exception):
    """The company could not be tied to a website."""


def collect(account, offer, log=lambda message: None):
    """All the evidence for one account: (name, domain, evidence, gaps)."""
    name, domain = account.get('name'), account.get('domain')
    window = offer.get('signal_window_days', 180)
    evidence, gaps, registry = [], [], None

    def attempt(label, source, *args):
        log(label)
        try:
            return source(*args)
        except FetchError as e:
            gaps.append(f'{label}: {e}')
            return None

    if not domain:
        registry = attempt('Wikidata', wikidata, name, None)
        if not registry:
            raise Unresolved(f'no official website found for "{name}": give its domain instead')
        domain = registry[1]
    pages = []
    site = attempt('Company site', crawl_site, domain, gaps)
    if site:
        evidence += site[0]
        pages = site[1]
        name = name or site[2]
    label = domain.split('.')[0]
    registry = registry or attempt('Wikidata', wikidata, name or label, domain)
    if registry:
        evidence.append(registry[0])
        name = name or registry[0].title.removeprefix('Wikidata: ')
    else:
        gaps.append('Wikidata: no entry whose official website is this domain')
    name = name or label.capitalize()
    if pages and (found := stack(domain, pages)):
        evidence.append(found)
    evidence += attempt('Job board', jobs, name, domain, pages, offer, gaps) or []
    evidence += attempt('News', news, name, domain, pages, window, gaps) or []
    if contact := os.environ.get('FOOTHOLD_CONTACT'):
        filings = attempt('SEC EDGAR', edgar, name, account.get('ticker'), window, contact)
        if filings:
            evidence += filings
        elif filings is None and not any(g.startswith('SEC EDGAR') for g in gaps):
            gaps.append('SEC EDGAR: no US-listed company with this name or ticker')
    else:
        gaps.append('SEC EDGAR: skipped, set FOOTHOLD_CONTACT (the SEC asks for a contact address)')
    if ((account.get('country') or '').upper() == 'FR' or domain.endswith('.fr')) and (
            found := attempt('French company register', france, name)):
        evidence.append(found)
    if (key := os.environ.get('APOLLO_API_KEY')) and (found := attempt('Apollo', apollo, domain, key)):
        evidence.append(found)
    for number, item in enumerate(evidence, 1):
        item.id = f'E{number}'
    return name, domain, evidence, gaps
