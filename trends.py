"""Public GitHub Trending and Hugging Face model/paper adapters."""
from hashlib import sha256
from html.parser import HTMLParser
import json
import math
import re
import time
from urllib.parse import urljoin, urlsplit
from sources import timestamp,Content


def number(value):
    try:return int(str(value).replace(',','').replace(' ','').strip())
    except (ValueError,TypeError):return None


def metric(kind,value,label):
    value=number(value)
    return dict(kind=kind,value=value,label=label) if value is not None and value>=0 else None


def entry(source,identifier,title,url,summary,rank,observed,kind,metrics,original_date=None,links=None):
    date=None
    if original_date:
        try:date=timestamp(original_date)
        except (ValueError,TypeError,AttributeError):pass
    return dict(id=sha256(identifier.encode()).hexdigest()[:24],source='site',source_id=source['id'],
                source_name=source['name'],category=source['category'],filter_mode=source.get('filter_mode','all'),
                content_kind=kind,title=title,url=url,article_url=url,selftext=summary,
                feed_rank=rank,created_utc=observed,observed_at=observed,date_kind='trend',
                original_published_at=date,links=links or [],score=None,
                metrics=[m for m in metrics if m is not None])


class GitHubTrending(HTMLParser):
    VOID={'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows=[];self.row=None;self.stack=[]

    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='article' and 'Box-row' in attrs.get('class','').split():
            self.row={'title':[],'summary':[],'stars':[],'forks':[],'period':[],'language':[],'url':''}
            self.stack=[]
        if self.row is None:return
        inherited=self.stack[-1][1] if self.stack else ''
        field=inherited
        if tag=='h2':field='title'
        elif tag=='p':field='summary'
        elif attrs.get('itemprop')=='programmingLanguage':field='language'
        elif tag=='a' and attrs.get('href','').endswith('/stargazers'):field='stars'
        elif tag=='a' and attrs.get('href','').endswith('/forks'):field='forks'
        elif tag=='span' and ('float-sm-right' in attrs.get('class','') or 'float-right' in attrs.get('class','')):field='period'
        if tag=='a' and inherited=='title':self.row['url']=urljoin('https://github.com',attrs.get('href',''))
        if tag in ('svg','script','style'):field=''
        if tag not in self.VOID:self.stack.append((tag,field))

    def handle_data(self,data):
        if self.row is not None and self.stack and self.stack[-1][1]:
            self.row[self.stack[-1][1]].append(data)

    def handle_endtag(self,tag):
        if self.row is None:return
        if tag=='article':
            if self.row['url']:self.rows.append(self.row)
            self.row=None;self.stack=[];return
        for i in range(len(self.stack)-1,-1,-1):
            if self.stack[i][0]==tag:self.stack=self.stack[:i];break


def parse_github(data,source,now):
    parser=GitHubTrending();parser.feed(data.decode('utf-8'))
    if not parser.rows:raise RuntimeError('GitHub Trending returned no repository rows')
    posts=[]
    for rank,row in enumerate(parser.rows):
        title=re.sub(r'\s+','',''.join(row['title']))
        if not re.fullmatch(r'[\w.-]+/[\w.-]+',title):continue
        summary=re.sub(r'\s+',' ',''.join(row['summary'])).strip()
        period=''.join(row['period']);match=re.search(r'([\d,]+)\s+stars?\s+(today|this week|this month)',period)
        window=match[2] if match else ''
        period_kind={'today':'stars_day','this week':'stars_week','this month':'stars_month'}.get(window)
        label={'today':'звёзд за сутки','this week':'звёзд за неделю','this month':'звёзд за месяц'}.get(window,'')
        metrics=[metric('stars', ''.join(row['stars']),'звёзд всего'),metric('forks',''.join(row['forks']),'форков')]
        if match:metrics.insert(0,metric(period_kind,match[1],label))
        p=entry(source,title,title,row['url'],summary,rank,now,'repository',metrics)
        p['language']=''.join(row['language']).strip();posts.append(p)
    return posts


def parse_models(data,source,now):
    rows=json.loads(data)
    if not isinstance(rows,list):raise RuntimeError('Expected Hugging Face models array')
    posts=[]
    for rank,row in enumerate(rows):
        identifier=row.get('id') or row.get('modelId')
        if not identifier or row.get('private'):continue
        card=row.get('cardData') or {}
        task=row.get('pipeline_tag') or card.get('pipeline_tag')
        license=card.get('license')
        if isinstance(license,list):license=', '.join(str(x) for x in license)
        fragments=[str(x) for x in [task,row.get('library_name'),('Лицензия: '+str(license)) if license else None] if x]
        parameters=(row.get('safetensors') or {}).get('total')
        if isinstance(parameters,(int,float)):fragments.append(f'Параметров: {parameters:,}')
        if not fragments:fragments=['Модель в трендах Hugging Face. Подробности — в model card.']
        p=entry(source,identifier,identifier,'https://huggingface.co/'+identifier,' · '.join(fragments),rank,now,'model',[
            metric('likes',row.get('likes'),'лайков'),metric('downloads_month',row.get('downloads'),'скачиваний / мес.')],row.get('createdAt'))
        p['provider_trending_score']=row.get('trendingScore');p['pipeline_tag']=task;p['license']=license
        posts.append(p)
    return posts


def parse_papers(data,source,now):
    rows=json.loads(data)
    if not isinstance(rows,list):raise RuntimeError('Expected Hugging Face papers array')
    posts=[]
    for rank,row in enumerate(rows):
        paper=row.get('paper') or row
        identifier=paper.get('id');title=paper.get('title') or row.get('title')
        if not identifier or not title:continue
        summary=(paper.get('summary') or row.get('summary') or '').replace('\\n',' ')
        repo=paper.get('githubRepo')
        links=['https://arxiv.org/abs/'+identifier]
        if repo and urlsplit(repo).scheme in ('https','http'):links.append(repo)
        p=entry(source,identifier,title,'https://huggingface.co/papers/'+identifier,summary,rank,now,'paper',[
            metric('upvotes',paper.get('upvotes'),'голосов'),metric('comments',row.get('numComments'),'комментариев')],paper.get('publishedAt') or row.get('publishedAt'),links)
        p['discussion_url']=p['url'];p['arxiv_id']=identifier
        posts.append(p)
    return posts


def parse_hype(data,source,now):
    payload=json.loads(data)
    if not isinstance(payload,dict) or payload.get('success') is not True or not isinstance(payload.get('result'),list):
        raise RuntimeError('Expected successful Replicate Hype posts response')
    posts=[];seen=set()
    for rank,row in enumerate(payload['result']):
        if not isinstance(row,dict):continue
        origin=row.get('source');url=row.get('url')
        hosts={'github':'github.com','huggingface':'huggingface.co','replicate':'replicate.com','reddit':'www.reddit.com'}
        if origin not in hosts or not isinstance(url,str):continue
        parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.netloc not in {hosts[origin],hosts[origin].removeprefix('www.')}:continue
        parts=parsed.path.strip('/').split('/')
        if origin=='reddit':
            if len(parts)<4 or parts[0]!='r' or parts[2]!='comments':continue
            identifier=parts[3];title=row.get('name');subreddit=parts[1]
        else:
            if len(parts)!=2 or not all(re.fullmatch(r'[\w.-]+',x) for x in parts):continue
            identifier='/'.join(parts);title=identifier
        if not isinstance(title,str) or not title.strip() or url in seen:continue
        try:stamp=timestamp(row.get('created_at'))
        except (ValueError,TypeError,AttributeError,OverflowError):continue
        if not math.isfinite(stamp) or stamp>now+300:continue
        value=row.get('stars')
        value=value if type(value) is int and value>=0 else None
        unit,label={'github':('stars','звёзд всего'), 'huggingface':('likes','лайков'),
                    'replicate':('runs','запусков всего'), 'reddit':('upvotes','апвоутов')}[origin]
        kind='repository' if origin=='github' else 'model' if origin in ('huggingface','replicate') else None
        category='repository' if origin=='github' else 'model' if kind=='model' else 'community'
        spec=dict(source,category=category,filter_mode='all' if kind=='model' else 'keywords')
        description=Content(url);description.feed(row.get('description') if isinstance(row.get('description'),str) else '')
        p=entry(spec,origin+':'+identifier,title.strip(),url,description.text(),rank,now,kind,[metric(unit,value,label)])
        # Hype's HF date is lastModified; Replicate's is latest_version.created_at.
        # Neither is a model creation date. Keep the real update date, never the fetch time.
        p.update(created_utc=stamp,date_kind='updated' if kind=='model' else 'published',
                 hype_origin=origin,aggregator_url='https://hype.replicate.dev/')
        if origin=='reddit':p.update(subreddit=subreddit,score=value,link_flair_text=None,reddit_post_id=identifier)
        seen.add(url);posts.append(p)
    return posts


def parse_trends(data,source,now=None):
    now=time.time() if now is None else now
    parser={'github_trending':parse_github,'hf_models':parse_models,'hf_papers':parse_papers,'replicate_hype':parse_hype}[source['format']]
    return parser(data,source,now)
