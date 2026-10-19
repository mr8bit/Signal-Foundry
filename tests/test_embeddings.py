import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import embeddings
import signals
from test_signals import CFG,NOW,post

CONFIG=('https://embed.example/v1','key','embedder-a')


def story(id,title,hours=1,**changes):
    return post(**dict(id=id,title=title,source_id=id,source_name=id.title(),filter_mode='all',article_url=f'https://{id}.example/',
                       url=f'https://{id}.example/',selftext='',links=[],created_utc=NOW-hours*3600)|changes)


class Store(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.TemporaryDirectory();self.store=embeddings.Store(Path(self.dir.name)/'vectors.sqlite3')
        self.calls=[]

    def tearDown(self):self.dir.cleanup()

    def embed(self,texts,config):
        self.calls.append(texts);return [[float(len(t)),*[0.5]*1023] for t in texts]

    def test_embeds_new_stories_once_newest_first_and_forgets_removed_ones(self):
        old,new=story('old','Old story',hours=9),story('new','New story',hours=1)
        model=story('m','owner/model',content_kind='model')
        self.assertEqual(self.store.refresh([old,new,model],CONFIG,self.embed),2)
        self.assertEqual(self.calls,[['New story','Old story']])
        self.assertEqual(self.store.last,dict(model='embedder-a',stories=2,added=2,pending=0,error=''))
        vectors=self.store.load(CONFIG)
        self.assertEqual(len(vectors),2);self.assertEqual(len(next(iter(vectors.values()))[0]),embeddings.DIMS)
        self.assertEqual(self.store.refresh([new],CONFIG,self.embed),0);self.assertEqual(len(self.calls),1)
        self.assertEqual(list(self.store.load(CONFIG)),[embeddings.digest('New story')])
        # Vectors of another model are not comparable: they are dropped and the stories embedded again.
        other=CONFIG[:2]+('embedder-b',)
        self.assertEqual(self.store.refresh([new],other,self.embed),1);self.assertEqual(self.store.load(CONFIG),{})

    def test_failures_and_the_per_refresh_limit_only_delay_vectors(self):
        posts=[story(f's{i}',f'Story {i}',hours=i) for i in range(5)]
        with patch.object(embeddings,'PER_REFRESH',3),patch.object(embeddings,'BATCH',2):
            self.assertEqual(self.store.refresh(posts,CONFIG,self.embed),3)
        def broken(texts,config):raise OSError('down')
        self.assertEqual(self.store.refresh(posts,CONFIG,broken),0)
        self.assertEqual((self.store.last['error'],self.store.last['pending']),('OSError',2))
        self.assertEqual(self.store.refresh(posts,CONFIG,self.embed),2);self.assertEqual(len(self.store.load(CONFIG)),5)

    def test_without_settings_nothing_is_called_or_loaded(self):
        with patch.dict(os.environ,{'SIGNAL_EMBEDDINGS_URL':'','SIGNAL_EMBEDDINGS_MODEL':''}):
            self.assertEqual(self.store.refresh([story('a','A story')],embed=self.embed),0)
            self.assertEqual(self.store.load(),{});self.assertEqual(self.calls,[])


class Matching(unittest.TestCase):
    def test_vectors_decide_retold_stories_and_tfidf_covers_the_rest(self):
        verge=story('verge','ChatGPT can peek at your Chrome tabs now',hours=20,selftext='OpenAI is letting ChatGPT read your open Chrome tabs.')
        decoder=story('decoder','ChatGPT becomes a full Chrome browser assistant that reads and acts on your open tabs',hours=4,
                      selftext='The ChatGPT extension for Chrome now reads open tabs and acts on them.')
        filler=[story(f'w{i}',f'Unrelated story number {i} about chips and courts',hours=30+i) for i in range(6)]
        posts=[verge,decoder,*filler]
        def vectors(pairs):
            return {embeddings.digest(embeddings.story_text(p)):(v,sum(x*x for x in v)**0.5)
                    for p,v in pairs for v in [embeddings.quantize(v)]}
        axis=lambda i:[1.0 if j==i else 0.0 for j in range(embeddings.DIMS)]
        chrome=lambda vec:[x for x in signals.build_items(posts,CFG,NOW,vectors=vec) if 'Chrome' in x['title']]
        # The same event: one card, as the TF-IDF matcher already found.
        self.assertEqual(len(chrome(vectors([(verge,axis(0)),(decoder,axis(0))]))),1)
        # Orthogonal vectors overrule the shared words.
        self.assertEqual(len(chrome(vectors([(verge,axis(0)),(decoder,axis(1))]))),2)
        # A story without a vector falls back to TF-IDF.
        self.assertEqual(len(chrome(vectors([(verge,axis(0))]))),1)


if __name__=='__main__':unittest.main()
