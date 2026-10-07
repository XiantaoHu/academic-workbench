import json, os, re
import workbench_settings
ROOT=os.path.join(os.path.dirname(__file__),'data')

def load(name):
    try:
        with open(os.path.join(ROOT,name),encoding='utf-8') as f: return json.load(f)
    except (OSError,ValueError): return {}

def public():
    related=set()
    caches=load('arxiv_tracking_cache.json')
    for value in caches.values():
        for p in value.get('items',[]):
            from trending_papers import dates
            if p.get('date') not in dates(): continue
            link=p.get('link','')
            if link: related.add(re.sub(r'v\d+$', '', link.replace('http://', 'https://')))
    from trending_papers import get
    hot={p['link'] for p in get().get('items',[]) if p.get('link')}
    news=load('cache.json')
    ai=news.get('news',{}).get('aihot',{}).get('items',[])
    import fetchers
    ai=fetchers.recent_news(ai)
    news_ids={p.get('link') or p.get('title') for p in ai if p.get('link') or p.get('title')}
    saved=load('information_counts.json')
    result={}
    for key, values in [('related',related),('hot',hot),('news',news_ids)]:
        previous=saved.get(key)
        ids=sorted(values)
        if previous is None: delta=0
        elif previous.get('ids')==ids: delta=previous.get('added',0)
        else: delta=len(values-set(previous.get('ids',[])))
        saved[key]={'ids':ids,'added':delta}
        result[key]={'count':len(values),'added':delta}
    workbench_settings.write('information_counts.json',saved)
    return result
