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
        label={'today':'stars / day','this week':'stars / week','this month':'stars / month'}.get(window,'')
        metrics=[metric('stars', ''.join(row['stars']),'total stars'),metric('forks',''.join(row['forks']),'forks')]
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
        fragments=[str(x) for x in [task,row.get('library_name'),('License: '+str(license)) if license else None] if x]
        parameters=(row.get('safetensors') or {}).get('total')
        if isinstance(parameters,(int,float)):fragments.append(f'Parameters: {parameters:,}')
        if not fragments:fragments=['Trending on Hugging Face. See the model card for details.']
        p=entry(source,identifier,identifier,'https://huggingface.co/'+identifier,' · '.join(fragments),rank,now,'model',[
            metric('likes',row.get('likes'),'likes'),metric('downloads_month',row.get('downloads'),'downloads / month')],row.get('createdAt'))
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
            metric('upvotes',paper.get('upvotes'),'upvotes'),metric('comments',row.get('numComments'),'comments')],paper.get('publishedAt') or row.get('publishedAt'),links)
        p['discussion_url']=p['url'];p['arxiv_id']=identifier
        posts.append(p)
    return posts


def parse_trends(data,source,now=None):
    now=time.time() if now is None else now
    parser={'github_trending':parse_github,'hf_models':parse_models,'hf_papers':parse_papers}[source['format']]
    return parser(data,source,now)
