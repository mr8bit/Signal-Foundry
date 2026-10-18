"""Cloud-specific editorial gates and model availability annotations. No network calls."""
from dataclasses import dataclass
import re
from urllib.parse import urlsplit

ANNOUNCE=r'\b(?:launch\w*|introduc\w*|announc\w*|releas\w*|unveil\w*|now available|generally available|general availability|new|GA)\b'
EXCLUDE=r'\b(?:how (?!to\b).+? (?:built|builds|uses|using)|customer stor\w*|customer success|case stud\w*|webinars?|govcloud|certifications?|certified|compliance certificates?|weekly roundup|new regions?|additional regions?|more regions?|expands? to .+ regions?)\b'
REGION=r'\b(?:now |newly )?available in (?:the )?(?:aws |azure )?(?:us |u\.s\.|europe|asia|canada|japan|australia|south america|africa|middle east|[\w -]+ region\b)'
SERVICES=r'\b(?:bedrock|sagemaker|agentcore|amazon q|nova|trainium\w*|inferentia\w*|tpu\w*|vertex ai|gemini|openai|foundry|ai gateway|ai sdk|workers ai|v0|clef|ai|agents?|llm)\b'
PLATFORMS=r'\b(?:agentcore|gemini enterprise|copilot studio|foundry|workers ai|ai sdk|v0|agent(?:ic)? platform|agent runtime)\b'
FAMILIES=r'gemini|claude|gpt|llama|qwen|deepseek|gemma|mistral|kimi|glm|nemotron|nova|clef|flux|phi|grok|jamba'
VARIANTS=r'opus|sonnet|haiku|flash|pro|lite|ultra|mini|nano|instant|premier|micro|small|medium|large|instruct|reasoning|vision|codex'
MODEL=re.compile(r'\b('+FAMILIES+r')[- ./]*(?:('+VARIANTS+r')[- ]*)?[vVrR]?(\d+(?:[.-]\d{1,2}(?![\dbB]))*)(?:[- ]+('+VARIANTS+r'))?(?:[- ]+('+VARIANTS+r'))?',re.I)
MODEL_STOP={'now','available','availability','released','release','releases','launched','launch','on','in','is','are',
            'and','with','for','from','to','via','api','model','models','new','open','weights','benchmark','scores','score',
            'support','supports','built','introducing','introduces','announcing','announces','at','as','by','the'}


def model_keys(text):
    result=set()
    for match in MODEL.finditer(text):
        family,variant,version,tail,tail2=match.groups()
        variants={v.lower() for v in (variant,tail,tail2) if v}
        suffix=text[match.end():]
        # Preserve unfamiliar proper names / hyphenated artifact variants instead of
        # guessing that two differently named models of the same version are identical.
        for _ in range(4):
            extra=re.match(r'([- ]+)([A-Za-z][A-Za-z0-9]*|\d+[bB]|\d{4,8}(?!\d))',suffix)
            if not extra or extra[2].lower() in MODEL_STOP or not ('-' in extra[1] or extra[2][0].isupper() or extra[2][-1:] in ('B','b') or extra[2].isdigit()):break
            variants.add(extra[2].lower());suffix=suffix[extra.end():]
        variants=sorted(variants)
        result.add(family.lower()+':'+re.sub(r'\D','',version)+':'+','.join(variants))
    for match in re.finditer(r'\bo([134])(?:[- ](mini|pro))?\b',text,re.I):
        result.add('openai-o:'+match[1]+':'+(match[2] or '').lower())
    return result


def model_links(p):
    keys=set()
    for url in [p.get('url','')]+p.get('links',[]):
        parsed=urlsplit(url);parts=[s for s in parsed.path.split('/') if s]
        if parsed.hostname=='huggingface.co' and len(parts)>=2 and parts[0] not in ('blog','docs','papers','spaces','datasets','collections','learn','posts'):
            keys.add('/'.join(parts[:2]).lower())
    return keys


@dataclass(frozen=True)
class Decision:
    mode:str
    reason:str
    kind:str=''


def normalize_note(p):
    if p.get('cloud_rule') in ('gemini_enterprise','vertex_notes') and re.fullmatch(r'[A-Za-z]+ \d{1,2}, \d{4}',p['title'].strip()):
        headline=next((line.strip() for line in p.get('selftext','').splitlines() if len(line.strip())>=25),'')
        if headline:return dict(p,original_title=p['title'],title=(p.get('cloud_label') or p['source_name'])+' · '+headline[:220])
    return p


def cloud_decision(p,cfg=None):
    rule=p.get('cloud_rule')
    if not rule:return None
    p=normalize_note(p)
    title=p['title'];body=p.get('selftext','')[:5000];text=title+' '+body
    has=lambda pattern,value=text:bool(re.search(pattern,value,re.I))
    if has(EXCLUDE,title) or has(REGION,title) or has(r'^how to\b|\b(?:ISO[- ]?\d+|SOC[- ]?[12]|HIPAA|FedRAMP)\b',title):return Decision('drop','Customer story, tutorial, region or administrative announcement')
    if rule=='gemini_enterprise' and (has(r'\bpreview\b') or not (has(r'\b(?:GA|generally available|general availability)\b') or model_keys(text))):
        return Decision('drop','GA only; previews excluded')
    if rule=='vertex_notes' and not model_keys(text):return Decision('drop','No versioned model name')
    if rule=='azure_updates' and (not has(r'^\[Launched\]',title) or not has(r'\b(?:OpenAI|Foundry|models?|agents?)\b')):
        return Decision('drop','Only Launched updates about AI')
    if rule=='aws_whats_new' and not has(r'\b(?:Bedrock|SageMaker|Nova|Amazon Q|Q Developer|Q Business|AgentCore|Trainium)\b',title):
        return Decision('drop','Not an AWS AI service')
    if rule=='vercel_sdk':
        return Decision('standalone','AI SDK major release','agent_platform') if re.fullmatch(r'ai@[1-9]\d*\.0\.0',title.strip()) else Decision('drop','Not a stable ai@N.0.0 major release')
    if rule=='foundry' and has(r"\bwhat['’]?s new\b",title):return Decision('standalone','Microsoft Foundry updates','agent_platform')
    if rule=='vercel' and not has(r'\b(?:AI Gateway|v0|AI SDK|agents?)\b',title):return Decision('drop','Not a Vercel AI product')
    if rule=='gcloud_blog' and not has(ANNOUNCE+r'|\bGemini\b',title):return Decision('drop','Not a Google Cloud announcement')
    if rule=='aws_ml' and not has(r'\b(?:is now available|Bedrock|AgentCore|Nova|open[- ]weights?)\b',title):return Decision('drop','Not an AWS GenAI announcement')
    if rule=='azure_blog' and (not has(ANNOUNCE,title) or has(r'\b(?:strategy|strategies|thought leadership|future of|vision for)\b',title)):
        return Decision('drop','Not an Azure announcement')
    if rule=='aws_news':
        words=(cfg or {}).get('include',[])
        if not any(re.search(r'(?<!\w)'+re.escape(word)+r'(?!\w)',title,re.I) for word in words) and not has(r'\b(?:Bedrock|SageMaker|AgentCore|Nova|Trainium\w*|Inferentia\w*)\b',title):
            return Decision('drop','No AI topic in the headline')
    if not has(SERVICES):return Decision('drop','No AI topic')
    # A quantified reduction in cloud AI pricing is an independent product event.
    price=has(r'\b(?:pric\w*|cost\w*|cheaper|discount\w*)\b',title) and has(r'\b(?:reduc\w*|cut\w*|lower\w*|drop\w*|cheaper|discount\w*)\b',title)
    price_scope=title+' '+ ' '.join(s for s in re.split(r'\n|[!?]|\.\s',body) if re.search(r'\b(?:pric\w*|cost\w*|cheaper|discount\w*|savings)\b',s,re.I))
    reductions=[float(v) for v in re.findall(r'(\d+(?:\.\d+)?)\s*%',price_scope)]
    multiples=[float(v) for v in re.findall(r'(\d+(?:\.\d+)?)\s*[x×]\s*(?:cheaper|lower)',price_scope,re.I)]
    if price and (any(v>=(cfg or {}).get('cloud',{}).get('price_drop_min_percent',20) for v in reductions) or any(v>=2 for v in multiples)):
        return Decision('standalone','Significant AI service price reduction','pricing')
    if price:return Decision('drop','Price reduction without sufficient figures')
    owned={'aws':r'\b(?:Amazon )?Nova\b','cloudflare':r'\bClef\b'}
    if has(r'\bNova (?:Act|Forge)\b',title) and has(ANNOUNCE,title):
        return Decision('standalone','AWS Nova platform','agent_platform')
    if has(owned.get(p.get('cloud_provider'),'(?!)'),title) and has(ANNOUNCE,title):
        return Decision('standalone','Cloud provider model','model')
    hardware=re.search(r'\b(?:Trainium\w*|Inferentia\w*|TPU\w*|new .*(?:GPU|AI) instances?)\b',title,re.I)
    model=MODEL.search(title) or re.search(r'\bmodels?\b',title,re.I)
    if hardware and (not model or hardware.start()<model.start()) and has(ANNOUNCE,title):
        return Decision('standalone','New AI hardware','hardware')
    availability=(bool(model_keys(title)) or bool(model_links(p)) or has(r'\bmodels?\b(?!\s+(?:context protocol|router|gateway|deployment platform))',title)) and has(r'\b(?:available|availability|support\w*|access|adds?|bring\w*|introduc\w*|launch\w*|releas\w*)\b')
    # Date-only release-note titles carry the model announcement in their content.
    if rule in ('vertex_notes','gemini_enterprise') and model_keys(text):availability=True
    if availability:return Decision('availability','Cloud model availability','model')
    if has(PLATFORMS,title) and has(ANNOUNCE,title):return Decision('standalone','Cloud platform for AI and agents','agent_platform')
    return Decision('drop','No first-party release, platform, price reduction or new hardware')


def availability_entry(p):
    label=p.get('cloud_label') or p.get('source_name','Cloud')
    for pattern,name in [(r'\bBedrock\b','Amazon Bedrock'),(r'\bVertex AI\b','Vertex AI'),
                         (r'\bFoundry\b','Microsoft Foundry'),(r'\bAI Gateway\b','AI Gateway'),(r'\bWorkers AI\b','Workers AI')]:
        if re.search(pattern,p['title'],re.I):label=name;break
    title=p['title'];text=title+' '+p.get('selftext','')[:1000]
    ga=bool(re.search(r'\b(?:GA|general availability|generally available)\b',text,re.I))
    preview=bool(re.search(r'\bpreview\b',title,re.I) or not ga and re.search(r'\bpreview\b',text,re.I))
    return dict(provider=p.get('cloud_provider','cloud'),label=label,
                url=p.get('article_url') or p.get('url'),title=p['title'],published_at=p['created_utc'],
                stage='preview' if preview else 'ga' if ga else 'announced')


def prepare_cloud_posts(posts,cfg,now,max_age_hours=8760):
    """Availability never becomes a standalone story, even before a matching release is collected."""
    stories=[];updates=[];pending=[]
    for p in posts:
        p=normalize_note(p)
        decision=cloud_decision(p,cfg)
        if decision and decision.mode=='drop':continue
        if decision and decision.mode=='availability':
            if 0<=now-p['created_utc']<=max_age_hours*3600:updates.append(p)
        else:stories.append(dict(p))
    releases=[]
    for p in stories:
        keys=model_keys(p['title']+' '+(urlsplit(p.get('url','')).path if p.get('content_kind')=='model' else ''))
        links=model_links(p)
        if len(links)>1:links=set()  # A roundup linking many models is not their release card.
        artifact_title=any(p['title'].casefold().startswith(link) for link in links)
        if (len(keys)==1 or links or p.get('content_kind')=='model') and (artifact_title or p.get('content_kind')=='model' or re.search(r'\b(?:introduc\w*|announc\w*|releas\w*|launch\w*|unveil\w*|new model)\b',p['title'],re.I)):
            releases.append((p,keys,links))
    for update in updates:
        keys=model_keys(update['title']) or model_keys(update.get('selftext','')[:5000])
        entry=availability_entry(update);matched=False
        for release,identities,links in releases:
            stamp=release.get('original_published_at') or release['created_utc']
            if (keys&identities or model_links(update)&links) and stamp<=update['created_utc']+48*3600:
                existing=release.get('cloud_availability',[])
                release['cloud_availability']=[a for a in existing if a['url']!=entry['url']]+[entry]
                matched=True
        if not matched:pending.append(entry)
    return stories,pending


def availability_for(members):
    entries={a['url']:a for p in members for a in p.get('cloud_availability',[]) if urlsplit(a.get('url','')).scheme in ('https','http')}
    result={}
    for entry in sorted(entries.values(),key=lambda a:(-a['published_at'],a['provider'])):
        result.setdefault((entry['provider'],entry['label'],entry['stage']),entry)
    return list(result.values())
