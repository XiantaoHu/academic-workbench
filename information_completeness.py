"""Repair missing source fields and title translations without replacing valid data."""
import re
import fetchers
import trending_papers

PAPER_FIELDS = ('title', 'summary', 'authors', 'date', 'link')
NEWS_FIELDS = ('title', 'summary', 'date', 'link')

def paper_id(item):
    match = re.search(r'arxiv.org/abs/(\d{4}\.\d{4,5})', item.get('link', ''))
    return match.group(1) if match else None

def repair_papers(items):
    missing_ids = list(dict.fromkeys(paper_id(p) for p in items
        if any(not p.get(f) for f in PAPER_FIELDS) and paper_id(p)))
    metadata = {}
    for start in range(0, len(missing_ids), 50):
        try:
            metadata.update(trending_papers.arxiv_batch(missing_ids[start:start+50]))
        except Exception:
            continue
    for item in items:
        source = metadata.get(paper_id(item), {})
        for field in PAPER_FIELDS:
            if not item.get(field) and source.get(field):
                item[field] = source[field]
    fetchers.translate_titles(items)
    return sum(any(not p.get(f) for f in PAPER_FIELDS) or
               (not re.search(r'[\u4e00-\u9fff]', p.get('title', '')) and not p.get('title_zh'))
               for p in items)

def repair_news(news):
    remaining = 0
    sources = {s['key']: s for s in fetchers.SOURCES}
    for key, result in news.items():
        items = result.get('items', [])
        missing = [p for p in items if any(not p.get(f) for f in NEWS_FIELDS)]
        if missing and key in sources and sources[key].get('kind') != 'weekly':
            try:
                source = sources[key]
                fresh = fetchers.fetch_rss(source['url'], source.get('limit', 8))
                by_link = {p.get('link'): p for p in fresh if p.get('link')}
                for item in missing:
                    for field in NEWS_FIELDS:
                        value = by_link.get(item.get('link'), {}).get(field)
                        if not item.get(field) and value: item[field] = value
            except Exception:
                pass
        if key in fetchers.TRANSLATE_KEYS:
            fetchers.translate_titles(items)
        remaining += sum(any(not p.get(f) for f in NEWS_FIELDS) for p in items)
    return remaining
