"""English labels for generated metadata, including records from older archives.

Original source titles and excerpts retain their language.
"""
METRIC_LABELS={
    'stars_day':'stars / day','stars_week':'stars / week','stars_month':'stars / month',
    'stars':'total stars','forks':'forks','likes':'likes','downloads_month':'downloads / month',
    'upvotes':'upvotes','comments':'comments','runs':'total runs','trending':'HF trendingScore',
}


def english_post(post):
    result=dict(post)
    if 'metrics' in post:
        result['metrics']=[dict(m,label=METRIC_LABELS.get(m.get('kind'),m.get('label',''))) for m in post['metrics']]
    # These are generated HF API metadata, not original article text or model README excerpts.
    if post.get('source_id')=='hf_models':
        text=post.get('selftext','')
        text=text.replace('Лицензия:','License:').replace('Параметров:','Parameters:')
        if text=='Модель в трендах Hugging Face. Подробности — в model card.':
            text='Trending on Hugging Face. See the model card for details.'
        result['selftext']=text
    return result
