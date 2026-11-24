"""Text helpers: HTML to plain text, removal of contact details, and the normal form used to check quotes."""
import re
import unicodedata
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin

SKIP = {'script', 'style', 'noscript', 'svg', 'template'}
BLOCK = {'p', 'div', 'br', 'li', 'ul', 'ol', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'tr', 'td', 'th', 'table', 'section',
         'article', 'header', 'footer', 'nav', 'main', 'aside', 'blockquote', 'dt', 'dd'}

# The look-behind keeps the search linear on long runs of letters
EMAIL = re.compile(r'(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
PHONE = re.compile(r'(?:\+\d{1,3}[\s.-]?)?\(?\d{2,4}\)?[\s.-]\d{3,4}[\s.-]\d{3,4}\b'
                   r'|(?:\+33[\s.-]?|\b0)\d(?:[\s.-]?\d{2}){4}\b')
VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}
HIDDEN_STYLE = re.compile(r'display\s*:\s*none|visibility\s*:\s*hidden', re.I)


class Doc(HTMLParser):
    """One pass over a page: visible text, title, meta tags, links and feeds."""

    def __init__(self, base_url=''):
        super().__init__(convert_charrefs=True)
        self.base, self.parts, self.links, self.feeds, self.meta, self.title = base_url, [], [], [], {}, ''
        self._skip, self._in_title, self._href, self._label = 0, False, None, []
        self._hidden = []  # tag and depth of the hidden element being skipped

    def _abs(self, href):
        try:
            return urljoin(self.base, href.strip())
        except ValueError:
            return None

    def handle_starttag(self, tag, attrs):
        a = {k: v or '' for k, v in attrs}
        if self._hidden:
            self._hidden[1] += tag == self._hidden[0]
            return
        # Text a visitor cannot see is not evidence, and is where a page would hide instructions
        if tag not in VOID and ('hidden' in a or a.get('aria-hidden') == 'true' or HIDDEN_STYLE.search(a.get('style', ''))):
            self._hidden = [tag, 1]
            return
        if tag in SKIP:
            self._skip += 1
        elif tag == 'meta':
            key = a.get('property') or a.get('name')
            if key and a.get('content'):
                self.meta.setdefault(key.lower(), a['content'])
        elif tag == 'link' and 'alternate' in a.get('rel', '') and re.search('rss|atom', a.get('type', '')):
            if url := self._abs(a.get('href', '')):
                self.feeds.append(url)
        elif tag == 'a' and a.get('href'):
            self._href, self._label = self._abs(a['href']), []
        elif tag == 'title':
            self._in_title = True
        if tag in BLOCK:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if self._hidden:
            self._hidden[1] -= tag == self._hidden[0]
            if not self._hidden[1]:
                self._hidden = []
            return
        if tag in SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == 'a' and self._href:
            self.links.append((self._href, ' '.join(''.join(self._label).split())))
            self._href = None
        elif tag == 'title':
            self._in_title = False
        if tag in BLOCK:
            self.parts.append('\n')

    def handle_data(self, data):
        if self._skip or self._hidden:
            return
        if self._in_title:
            self.title += data
            return
        self.parts.append(data)
        if self._href:
            self._label.append(data)

    @property
    def text(self):
        lines = (' '.join(line.split()) for line in ''.join(self.parts).split('\n'))
        return '\n'.join(line for line in lines if line)


def parse(html, base_url=''):
    doc = Doc(base_url)
    try:
        doc.feed(html)
        doc.close()
    except Exception:  # a page too broken to parse still gives the text read so far
        pass
    doc.title = ' '.join(doc.title.split())
    return doc


def to_text(html):
    return parse(html).text


def scrub(text):
    """Removes email addresses and phone numbers: the tool never keeps contact details."""
    return PHONE.sub('[phone removed]', EMAIL.sub('[email removed]', text or ''))


def clip(text, limit):
    return text if len(text) <= limit else text[:limit].rsplit(' ', 1)[0] + ' […]'


def norm(text):
    """Lower case, plain quotes and dashes, single spaces: what two copies of the same passage share."""
    text = unicodedata.normalize('NFKC', text or '').lower()
    text = re.sub('[‘’‚′]', "'", text)
    text = re.sub('[“”„″]', '"', text).replace('‹', '<')
    text = re.sub('[‐‑‒–—―]', '-', text)
    return ' '.join(text.replace('[…]', ' ').replace('…', ' ').split())


def iso_date(value):
    """The first valid YYYY-MM-DD in a string, or None."""
    for m in re.finditer(r'(\d{4})-?(\d{2})-?(\d{2})', str(value or '')):
        try:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            continue
    return None
