"""A brief as Markdown, JSON and a CRM import row."""
import csv
import io
import json
import re

MISSING = 'Not found in the public sources checked.'
FIELDS = (('summary', 'What they do'), ('size', 'Size'), ('markets', 'Markets'), ('business_model', 'Business model'))
CHANNEL_NAMES = {'email': 'Email', 'linkedin': 'LinkedIn', 'call': 'Call'}

# First columns are the standard company fields each CRM matches on import; the rest map to custom properties
CRM_COLUMNS = {'hubspot': ('Company name', 'Company Domain Name', 'Description'),
               'salesforce': ('Account Name', 'Website', 'Description')}
EXTRA_COLUMNS = ('Fit score', 'Fit rationale', 'Top signal', 'Top signal date', 'Top signal source', 'Target roles',
                 'Email subject', 'Email opener', 'LinkedIn opener', 'Call opener', 'Research gaps', 'Researched on')


def _md(text):
    """Text from the web, safe inside a Markdown line or table cell."""
    return re.sub(r'([\\`*_\[\]<>|])', r'\\\1', ' '.join(str(text or '').split()))


def _link(label, url):
    if not str(url).startswith(('http://', 'https://')):
        return _md(label)
    return f'[{_md(label)}]({url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")})'


def _dated(item):
    return item.get('published') or f'undated, seen {item.get("collected", "?")}'


def to_markdown(brief):
    a, fit = brief['account'], brief['fit']
    out = [f'# {_md(a["name"])}: account brief', '',
           f'{a["domain"]} · researched {brief["researched"]} · offer: {_md(brief["offer"])} · model: {_md(brief["model"])}', '',
           f'## Fit: {fit["score"]}/100', '']
    if fit['unknown']:
        out += [f'{fit["unknown"]} of {len(fit["criteria"])} criteria could not be checked from public sources; '
                f'they count as not met.', '']
    out += ['| Criterion | Weight | Verdict | Why | Source |', '|---|---|---|---|---|']
    for c in fit['criteria']:
        source = _link(c['source'], c['url']) if c.get('url') else ''
        out.append(f'| {_md(c["name"])} | {c["weight"]} | {c["verdict"]} | {_md(c["why"])} | {source} |')
    out += ['', '## Company', '']
    for field, label in FIELDS:
        fact = brief['company'].get(field)
        if fact:
            out.append(f'- **{label}:** {_md(fact["text"])} ({_link(fact["source"], fact["url"])}, {_dated(fact)})')
        else:
            out.append(f'- **{label}:** {MISSING}')
    out += ['', '## Buying signals', '']
    if not brief['signals']:
        out.append('None found in the period checked.')
    for number, s in enumerate(brief['signals'], 1):
        out += [f'{number}. **{s["date"] or "undated, seen " + s["collected"]} · {s["type"]}** {_md(s["text"])}',
                f'   - Source: {_link(s["source_title"], s["url"])}',
                f'   - Quote: "{_md(s["quote"])}"']
    out += ['', '## Who to contact', '', 'Job titles only: foothold does not look up people.', '']
    out += [f'- **{_md(r["title"])}**: {_md(r["why"])}' for r in brief['roles']] or ['None suggested.']
    out += ['', '## Openers', '']
    if not brief['hooks']:
        out.append('None written: no checked signal to open on.')
    for channel, hook in brief['hooks'].items():
        out += [f'### {CHANNEL_NAMES[channel]} (opens on signal {hook["signal"]})', '']
        if hook.get('subject'):
            out += [f'**Subject:** {_md(hook["subject"])}', '']
        out += ['> ' + _md(line) for line in hook['body'].splitlines() if line.strip()] + ['']
    out += ['## Likely objections', '']
    out += [f'- **"{_md(o["objection"])}"** {_md(o["response"])}' for o in brief['objections']] or ['None suggested.']
    out += ['', '## Gaps', '', 'What this brief could not establish. Nothing here was filled in by guessing.', '']
    out += [f'- {_md(g)}' for g in brief['gaps']] or ['- None.']
    out += ['', '## Sources', '', '| Id | Kind | Published | Collected | Source |', '|---|---|---|---|---|']
    out += [f'| {e["id"]} | {e["kind"]} | {e["published"] or "undated"} | {e["fetched"]} | {_link(e["title"], e["url"])} |'
            for e in brief['evidence']]
    return '\n'.join(out) + '\n'


def to_json(brief):
    return json.dumps(brief, indent=2, ensure_ascii=False) + '\n'


def _cell(value):
    """A spreadsheet must not read a cell from the web as a formula."""
    value = ' '.join(str(value if value is not None else '').split())
    return "'" + value if value[:1] in ('=', '+', '-', '@') else value


def to_csv(briefs, crm='hubspot'):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator='\n')
    writer.writerow(CRM_COLUMNS[crm] + EXTRA_COLUMNS)
    for b in briefs:
        fit, top, hooks = b['fit'], (b['signals'] or [{}])[0], b['hooks']
        summary = b['company'].get('summary') or {}
        rationale = '; '.join(f'{c["name"]}: {c["verdict"]}' for c in fit['criteria'])
        row = (b['account']['name'], b['account']['domain'], summary.get('text', ''), fit['score'], rationale,
               top.get('text', ''), top.get('date') or '', top.get('url', ''), '; '.join(r['title'] for r in b['roles']),
               hooks.get('email', {}).get('subject', ''), hooks.get('email', {}).get('body', ''),
               hooks.get('linkedin', {}).get('body', ''), hooks.get('call', {}).get('body', ''),
               '; '.join(b['gaps']), b['researched'])
        writer.writerow([_cell(v) for v in row])
    return out.getvalue()
