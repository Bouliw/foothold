"""foothold research <company>   one account
foothold batch <accounts.csv>  a list of accounts
foothold serve                 a small local web page"""
import argparse
import csv
import os
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__, analyze, fetch, llm, render, sources


COLUMNS = {'company': 'name', 'company name': 'name', 'account': 'name', 'account name': 'name',
           'website': 'domain', 'url': 'domain', 'company domain name': 'domain'}


def parse_company(value):
    """'Datadog', 'datadoghq.com' or 'https://www.datadoghq.com/about' as an account."""
    value = value.strip()
    if re.fullmatch(r'(https?://)?[\w.-]+\.[a-z]{2,}(/\S*)?', value, re.I):
        host = urlsplit(value if '//' in value else '//' + value).hostname or ''
        return {'domain': host.lower().removeprefix('www.')}
    return {'name': value}


def read_accounts(path):
    """A CSV with a header naming at least `name` or `domain` (also read: country, ticker), or one company per line."""
    with open(path, newline='', encoding='utf-8-sig') as f:
        rows = [row for row in csv.reader(f) if any(cell.strip() for cell in row)]
    if not rows:
        return []
    header = [COLUMNS.get(h.strip().lower(), h.strip().lower()) for h in rows[0]]
    if not {'name', 'domain'} & set(header):
        return [parse_company(row[0]) for row in rows]
    accounts = []
    for row in rows[1:]:
        fields = {k: v.strip() for k, v in zip(header, row) if v.strip()}
        account = parse_company(fields['domain']) if fields.get('domain') else {}
        account |= {k: fields[k] for k in ('name', 'country', 'ticker') if k in fields}
        if account.get('name') or account.get('domain'):
            accounts.append(account)
    return accounts


def research(account, offer, cfg, log=lambda message: None):
    """One account, from collection to checked brief."""
    name, domain, evidence, gaps = sources.collect(account, offer, log)
    if not evidence:
        raise sources.Unresolved(f'nothing could be collected for {domain}: ' + '; '.join(gaps))
    log(f'Analysis ({len(evidence)} sources)')
    cfg = dict(cfg)
    prompt = analyze.build_prompt(offer, name, domain, evidence, gaps)
    raw = llm.extract_json(llm.ask(analyze.SYSTEM, prompt, cfg))
    checked = analyze.validate(raw, offer, evidence, gaps)
    return {'account': {'name': name, 'domain': domain}, 'researched': date.today().isoformat(),
            'offer': offer['product']['name'], 'model': f'{cfg["provider"]}/{cfg["model"]}', **checked, 'gaps': gaps,
            'evidence': [{'id': e.id, 'kind': e.kind, 'title': e.title, 'url': e.url, 'published': e.published,
                          'fetched': e.fetched} for e in evidence]}


def save(brief, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir / analyze.slug(brief['account']['domain'])
    base.with_suffix('.md').write_text(render.to_markdown(brief), encoding='utf-8')
    base.with_suffix('.json').write_text(render.to_json(brief), encoding='utf-8')
    return base.with_suffix('.md')


def load_env(path='.env'):
    """KEY=value lines from a .env file in the current folder; the real environment wins. Returns the names
    this file set."""
    used = set()
    if os.path.exists(path):
        for line in Path(path).read_text(encoding='utf-8').splitlines():
            key, sep, value = line.partition('=')
            key = key.strip().removeprefix('export ').strip()
            if sep and key and not key.startswith('#') and key not in os.environ:
                os.environ[key] = value.strip().strip('"\'')
                used.add(key)
    return used


def main(argv=None):
    parser = argparse.ArgumentParser(prog='foothold', description='Account research for SDRs: a sourced, dated '
                                     'brief for every target company, from public data.')
    parser.add_argument('--version', action='version', version=f'foothold {__version__}')
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument('--offer', default='offer.toml', help='what you sell and to whom (default: offer.toml)')
    common.add_argument('--out', default='briefs', type=Path, help='folder for the briefs (default: briefs)')
    common.add_argument('--provider', choices=('anthropic', 'openai', 'local'), help='model provider')
    common.add_argument('--model', help='model name')
    common.add_argument('--base-url', help='address of an OpenAI-compatible API (LM Studio, Ollama, others)')
    common.add_argument('--no-cache', action='store_true', help='fetch every page again')
    sub = parser.add_subparsers(dest='command', required=True)
    one = sub.add_parser('research', parents=[common], help='research one company')
    one.add_argument('company', help='a company name or domain')
    many = sub.add_parser('batch', parents=[common], help='research every company of a CSV file')
    many.add_argument('csv', help='CSV with a name or domain column, or one company per line')
    for p in (one, many):
        p.add_argument('--crm', choices=tuple(render.CRM_COLUMNS), default='hubspot', help='column names of the CSV export')
    web = sub.add_parser('serve', parents=[common], help='open the local web page')
    web.add_argument('--port', type=int, default=8765)
    web.add_argument('--no-open', action='store_true', help='do not open the browser')
    args = parser.parse_args(argv)

    from_file = load_env()
    for flag, variable in (('provider', 'FOOTHOLD_PROVIDER'), ('model', 'FOOTHOLD_MODEL'), ('base_url', 'FOOTHOLD_BASE_URL')):
        if getattr(args, flag):
            os.environ[variable] = getattr(args, flag)
    fetch.use_cache = not args.no_cache
    try:
        offer = analyze.load_offer(args.offer)
        cfg = llm.settings()
        # A .env that came with a shared folder must not send a key from your own environment somewhere else
        key = {'anthropic': 'ANTHROPIC_API_KEY', 'openai': 'OPENAI_API_KEY'}.get(cfg['provider'])
        if key and 'FOOTHOLD_BASE_URL' in from_file and key not in from_file:
            raise ValueError(f'.env sets FOOTHOLD_BASE_URL but {key} comes from your environment: '
                             f'pass --base-url yourself if you trust that address')
        print(f'Model: {cfg["provider"]}' + (f' {cfg["model"]}' if cfg['model'] else '') +
              (f' at {cfg["base_url"]}' if cfg['base_url'] else ''), file=sys.stderr)
        accounts = [parse_company(args.company)] if args.command == 'research' else \
            read_accounts(args.csv) if args.command == 'batch' else []
    except (ValueError, OSError, llm.LLMError) as e:
        sys.exit(f'foothold: {e}')
    if args.command == 'serve':
        from . import serve
        return serve.run(offer, cfg, args.out, args.port, not args.no_open)

    briefs, failed = [], 0
    for account in accounts:
        label = account.get('name') or account.get('domain')
        try:
            brief = research(account, offer, cfg, log=lambda m: print(f'  {label}: {m}', file=sys.stderr))
        except (sources.Unresolved, fetch.FetchError, llm.LLMError) as e:
            failed += 1
            print(f'{label}: failed, {e}', file=sys.stderr)
            continue
        except Exception as e:  # one account never stops the others
            failed += 1
            print(f'{label}: failed, unexpected {type(e).__name__}: {e}', file=sys.stderr)
            continue
        briefs.append(brief)
        print(f'{brief["account"]["name"]}: fit {brief["fit"]["score"]}/100, {len(brief["signals"])} signals, '
              f'{len(brief["gaps"])} gaps -> {save(brief, args.out)}')
    if briefs:
        table = args.out / f'accounts-{args.crm}.csv'
        table.write_text(render.to_csv(briefs, args.crm), encoding='utf-8')
        print(f'CRM import file: {table}')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
