"""Background publication verification against public bibliographic APIs."""
import json
import os
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from difflib import SequenceMatcher


def normalized(value):
    value = unicodedata.normalize('NFKD', str(value)).lower()
    return ''.join(c for c in value if c.isalnum())


def key_for(paper):
    url = paper.get('url') or paper.get('link') or ''
    return re.sub(r'v\d+$', '', url.split('/abs/')[-1])


def author_names(value):
    return {normalized(n.strip().split()[-1]) for n in re.split(r'[、;,]', value or '') if n.strip()}


def matches(paper, title, authors, year):
    left, right = normalized(paper.get('title', '')), normalized(title)
    if not left or SequenceMatcher(None, left, right).ratio() < .97:
        return False
    expected = author_names(paper.get('authors', ''))
    actual = author_names('、'.join(authors))
    if not expected or not actual or not expected.intersection(actual):
        return False
    try:
        if abs(int(year) - int(paper.get('date', '')[:4])) > 5:
            return False
    except (ValueError, TypeError):
        pass
    return True


def get_json(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'AcademicWorkbench/1.0 publication lookup', 'Accept': 'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=15) as response:
        return json.load(response)


def candidates(paper):
    found, errors = [], []
    try:
        url = 'https://dblp.org/search/publ/api?' + urllib.parse.urlencode({'q': paper['title'], 'format': 'json', 'h': 5})
        hits = get_json(url).get('result', {}).get('hits', {}).get('hit', [])
        if isinstance(hits, dict):
            hits = [hits]
        for hit in hits:
            info = hit.get('info', {})
            authors = info.get('authors', {}).get('author', [])
            if isinstance(authors, (dict, str)):
                authors = [authors]
            authors = [a.get('text', '') if isinstance(a, dict) else a for a in authors]
            venue, year = info.get('venue', ''), info.get('year', '')
            if isinstance(venue, list):
                venue = ' / '.join(venue)
            if not venue or re.search(r'arxiv|corr', str(venue), re.I):
                continue
            if info.get('type') not in ('Journal Articles', 'Conference and Workshop Papers'):
                continue
            if matches(paper, info.get('title', ''), authors, year):
                found.append({'venue': str(venue), 'year': str(year), 'source': 'DBLP', 'url': info.get('url', ''), 'doi': ''})
    except Exception as error:
        errors.append('DBLP: ' + str(error)[:100])
    try:
        url = 'https://api.crossref.org/works?' + urllib.parse.urlencode({'query.bibliographic': paper['title'], 'rows': 5})
        for item in get_json(url).get('message', {}).get('items', []):
            if item.get('type') not in ('journal-article', 'proceedings-article'):
                continue
            venue = (item.get('container-title') or [''])[0]
            if not venue or re.search(r'arxiv|preprint|ssrn|biorxiv', venue, re.I):
                continue
            year = (item.get('issued', {}).get('date-parts') or [['']])[0][0]
            title = (item.get('title') or [''])[0]
            authors = [a.get('family', '') for a in item.get('author', [])]
            if matches(paper, title, authors, year):
                doi = item.get('DOI', '')
                found.append({'venue': venue, 'year': str(year), 'source': 'Crossref', 'url': 'https://doi.org/' + doi if doi else item.get('URL', ''), 'doi': doi})
    except Exception as error:
        errors.append('Crossref: ' + str(error)[:100])
    return found, errors


def venue_key(value):
    aliases = {'AAAI': r'aaai|association for the advancement of artificial intelligence', 'CVPR': r'cvpr|computer vision and pattern recognition', 'ICCV': r'iccv|international conference on computer vision', 'ECCV': r'eccv|european conference on computer vision', 'TPAMI': r'tpami|t-pami|transactions on pattern analysis', 'TIP': r'\btip\b|transactions on image processing', 'IJCV': r'ijcv|international journal of computer vision', 'ACMMM': r'acm mm|acm international conference on multimedia', 'NEURIPS': r'neurips|neural information processing systems'}
    for name, pattern in aliases.items():
        if re.search(pattern, value, re.I):
            return name + ('-workshop' if re.search('workshop', value, re.I) else '')
    return normalized(value)


def resolve(paper):
    hits, errors = candidates(paper)
    result = {'publication_checked_at': time.strftime('%Y-%m-%d %H:%M'), 'publication_evidence': hits, 'publication_lookup_errors': errors}
    if not hits:
        result['publication_status'] = 'retry' if errors else 'not_found'
        return result
    # Different confirmed venues or years require review, never overwrite silently.
    identities = {(venue_key(h['venue']), h['year']) for h in hits}
    if len(identities) > 1:
        result['publication_status'] = 'conflict'
        return result
    preferred = next((hit for hit in hits if hit['source'] == 'DBLP'), hits[0])
    result.update(publication=preferred['venue'] + ' ' + preferred['year'], publication_source=' / '.join(dict.fromkeys(h['source'] for h in hits)), publication_status='verified', publication_url=preferred['url'])
    doi = next((h['doi'] for h in hits if h['doi']), '')
    if doi:
        result['doi'] = doi
    return result


class PublicationLookup:
    def __init__(self, path, library_getter):
        self.path, self.library_getter = path, library_getter
        self.lock = threading.RLock()
        self.wake = threading.Event()
        try:
            with open(path, encoding='utf-8') as stream:
                self.records = json.load(stream)
        except (OSError, ValueError):
            self.records = {}
        for record in self.records.values():
            record['pending'] = not bool(record.get('result'))
            if record.get('running'):
                record['running'] = False
                record['pending'] = True

    def persist(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        temporary = self.path + '.tmp'
        with open(temporary, 'w', encoding='utf-8') as stream:
            json.dump(self.records, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, self.path)

    def request(self, papers):
        with self.lock:
            changed = False
            for paper in papers:
                key = key_for(paper)
                if not paper.get('title') or not re.fullmatch(r'(?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/\d{7})', key):
                    continue
                record = self.records.setdefault(key, {'result': {}, 'pending': True})
                new_paper = {k: paper.get(k, '') for k in ('title', 'authors', 'date', 'publication', 'publication_source', 'url', 'link')}
                if record.get('paper') != new_paper:
                    record['paper'] = {k: paper.get(k, '') for k in ('title', 'authors', 'date', 'publication', 'publication_source', 'url', 'link')}
                    changed = True
            if changed:
                self.persist()
        self.wake.set()

    def merge(self, paper):
        with self.lock:
            record = self.records.get(key_for(paper), {})
            result = dict(record.get('result', {}))
            if record and not result:
                result['publication_status'] = 'checking' if record.get('running') else 'queued'
            return dict(paper, **result)

    def snapshot(self):
        with self.lock:
            return {key: dict(r.get('result') or {'publication_status': 'checking' if r.get('running') else 'queued'}) for key, r in self.records.items()}

    def loop(self):
        while True:
            try:
                with self.lock:
                    due = next(((key, dict(r['paper'])) for key, r in self.records.items() if r.get('paper') and r.get('pending', False) and not r.get('running')), None)
                    if due:
                        self.records[due[0]]['running'] = True
                if due:
                    result = resolve(due[1])
                    with self.lock:
                        record = self.records[due[0]]
                        # Preserve earlier confirmed evidence if an API is temporarily down.
                        if record.get('result', {}).get('publication_status') == 'verified' and result['publication_status'] == 'retry':
                            record['result']['publication_lookup_errors'] = result['publication_lookup_errors']
                        else:
                            record['result'] = result
                        record.update(running=False, pending=False)
                        self.persist()
                    time.sleep(3)
                    continue
            except Exception as error:
                print('[publication] background lookup error:', error, flush=True)
                if 'due' in locals() and due:
                    with self.lock:
                        self.records[due[0]].update(running=False, pending=False, result={'publication_status': 'retry', 'publication_lookup_errors': [str(error)[:120]]})
                        self.persist()
            self.wake.clear()
            with self.lock:
                has_pending = any(r.get('pending') and not r.get('running') for r in self.records.values())
            if not has_pending:
                self.wake.wait()

    def start(self):
        threading.Thread(target=self.loop, name='publication-lookup', daemon=True).start()
