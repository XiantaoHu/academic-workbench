import json, os, re, threading, time, urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
import xml.etree.ElementTree as ET
import fetchers
ROOT=os.path.join(os.path.dirname(__file__),'data')
FILE=os.path.join(ROOT,'trending_papers.json')
_lock=threading.Lock()
_running=False

def read():
    try:
        with open(FILE,encoding='utf-8') as f: return json.load(f)
    except (OSError,ValueError): return {'items':[], 'history':{}, 'metadata':{}}

def save(data):
    temp=FILE+'.tmp'
    with open(temp,'w',encoding='utf-8') as f: json.dump(data,f,ensure_ascii=False)
    os.replace(temp,FILE)

def dates():
    today=datetime.now(ZoneInfo('Asia/Shanghai')).date()
    return [(today-timedelta(days=i)).isoformat() for i in range(7)]

def source_day(value):
    if not value: return ''
    try:
        parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if parsed.tzinfo: parsed=parsed.astimezone(ZoneInfo('Asia/Shanghai'))
        return parsed.date().isoformat()
    except ValueError: return ''

def norm(t): return re.sub(r'[^a-z0-9]','',t.lower())

def reduct_public():
    import workbench_settings
    authorization = workbench_settings.read('reduct_config.json').get('authorization', '')
    if authorization:
        import urllib.request
        req=urllib.request.Request('https://app.reduct.cn/Web/V1/Science/hot_list', headers={'Authorization': authorization, 'Accept': 'application/json', 'User-Agent':'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=25) as response:
            payload=json.load(response)
        if payload.get('code') != 200:
            raise RuntimeError('减论认证失败，请更新 Authorization')
        return [{'title':p.get('title',''),'publishDate':p.get('publish_date',''),'heatValue':p.get('popularity',0)} for p in payload.get('data',{}).get('list',[])]

    html=fetchers._http_get('https://www.reduct.cn/')
    for m in re.finditer(r'self\.__next_f\.push\((.*?)\)</script>',html):
        try:
            chunk=json.loads(m.group(1))[1]
            if '"initialPapers":' in chunk:
                start=chunk.index('"initialPapers":')+len('"initialPapers":')
                return json.JSONDecoder().raw_decode(chunk[start:])[0]
        except (ValueError,IndexError,TypeError): pass
    raise RuntimeError('减论公开页面未提供热点名单')

def arxiv_batch(ids):
    url='https://export.arxiv.org/api/query?'+urllib.parse.urlencode({'id_list':','.join(ids),'max_results':len(ids)})
    root=ET.fromstring(fetchers._http_get(url,prefer_direct=True))
    ns={'a':'http://www.w3.org/2005/Atom'}
    result={}
    for e in root.findall('a:entry',ns):
        link=e.findtext('a:id','',ns)
        pid=re.sub(r'v\d+$','',link.rsplit('/',1)[-1])
        if pid not in ids: continue
        result[pid]={'title':fetchers._clean(e.findtext('a:title','',ns)), 'summary':fetchers._clean(e.findtext('a:summary','',ns)), 'authors':'、'.join(a.findtext('a:name','',ns) for a in e.findall('a:author',ns)), 'date':e.findtext('a:published','',ns)[:10], 'link':'https://arxiv.org/abs/'+pid, **fetchers.arxiv_publication(e)}
    return result

def refresh():
    global _running
    data=read(); ds=dates(); today=ds[0]; warnings=[]; candidates={}
    history=data.get('history',{}); metadata=data.get('metadata',{})
    hf_days=data.get('hf_days',{})
    def hf(day):
        try:
            if day != today and day in hf_days: return day,hf_days[day],None
            return day,json.loads(fetchers._http_get('https://huggingface.co/api/daily_papers?date='+day)),None
        except Exception: return day,[],day+' 的 Hugging Face 名单暂时获取失败'
    with ThreadPoolExecutor(max_workers=4) as pool:
        for day, entries, error in pool.map(hf,ds):
            if error: warnings.append(error)
            else: hf_days[day]=entries
            for e in entries:
                p=e.get('paper',{}); pid=p.get('id','')
                if not re.fullmatch(r'\d{4}\.\d{4,5}',pid): continue
                recommended=source_day(e.get('publishedAt'))
                item=candidates.setdefault(pid,{'sources':[], 'listed_date':recommended,'heat':0,'title':p.get('title','')})
                if 'Hugging Face' not in item['sources']: item['sources'].append('Hugging Face')
                item['listed_date']=max(item['listed_date'],recommended)
                item['heat']=max(item['heat'],p.get('upvotes',0) or 0)
    try: history[today]=reduct_public()
    except Exception: warnings.append('减论热点获取失败；若已配置令牌，请检查 Authorization 是否过期。使用已有缓存')
    history={day:items for day,items in history.items() if day in ds}
    title_ids={norm(it['title']):pid for pid,it in candidates.items()}
    unresolved=0
    for day, entries in history.items():
        for e in entries:
            recommended=source_day(e.get('publishDate'))
            if recommended and recommended not in ds: continue
            title=e.get('title',''); pid=title_ids.get(norm(title))
            if not pid:
                match=data.get('title_ids',{}).get(norm(title))
                if match: pid=match
                elif data.get('unresolved_titles',{}).get(norm(title)) == today: continue
                else:
                    try:
                        found=fetchers.fetch_arxiv('ti:"'+title.replace('"','')+'"',3)
                        exact=next((p for p in found if norm(p['title'])==norm(title)),None)
                        if exact: pid=re.sub(r'v\d+$','',exact['link'].rsplit('/',1)[-1]); metadata[pid]=exact
                    except Exception: pass
            if not pid:
                unresolved+=1
                data.setdefault('unresolved_titles',{})[norm(title)]=today
                continue
            title_ids[norm(title)]=pid
            item=candidates.setdefault(pid,{'sources':[],'listed_date':recommended,'heat':0,'title':title})
            if '减论' not in item['sources']: item['sources'].append('减论')
            item['listed_date']=max(item['listed_date'],recommended)
    ids=[pid for pid in candidates if pid not in metadata]
    def details(batch):
        try: return arxiv_batch(batch)
        except Exception: return {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        for result in pool.map(details,[ids[start:start+50] for start in range(0,len(ids),50)]):
            if result: metadata.update(result)
            else: warnings.append('部分 arXiv 详情暂未获取成功，下次更新会重试')
    translations=fetchers._load_trans_cache()
    items=[]
    for pid, hot in candidates.items():
        if pid not in metadata: continue
        if metadata[pid].get('date') not in ds: continue
        p=dict(metadata[pid]); p.update(hot); p['title']=metadata[pid]['title']; p['title_zh']=translations.get(p['title'],''); items.append(p)
    items.sort(key=lambda p:(p['date'],len(p['sources']),p['heat']),reverse=True)
    data.update(items=items,hf_days={d:hf_days[d] for d in ds if d in hf_days},history=history,metadata=metadata,title_ids=title_ids,warnings=list(dict.fromkeys(warnings)),unmatched=unresolved,updated_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds'),timestamp=time.time(),dates=ds,date_rule_version=3)
    save(data)

def worker():
    global _running
    try:
        refresh()
        data=read()
        pending=[p for p in data.get('items',[]) if not p.get('title_zh')]
        for attempt in range(2):
            pending=[p for p in data.get('items',[]) if not p.get('title_zh')]
            if not pending: break
            for start in range(0,len(pending),8):
                fetchers.translate_titles(pending[start:start+8])
                save(data)
        if any(not p.get('title_zh') for p in data.get('items',[])):
            data.setdefault('warnings',[]).append('部分标题翻译未完成，请检查标题翻译模型配置后刷新重试')
            save(data)
    except Exception:
        data=read(); data['warnings']=['热点刷新失败，保留已有缓存']; save(data)
    finally:
        with _lock: _running=False

def get(force=False):
    global _running
    data=read()
    with _lock:
        if not _running and force:
            _running=True; threading.Thread(target=worker,daemon=True).start()
    ds=dates()
    return {'ok':True,'items':sorted([p for p in data.get('items',[]) if p.get('date') in ds], key=lambda p:p.get('date',''), reverse=True), 'loading':_running,'warnings':data.get('warnings',[]),'updated_at':data.get('updated_at',''),'unmatched':data.get('unmatched',0),'dates':ds}
