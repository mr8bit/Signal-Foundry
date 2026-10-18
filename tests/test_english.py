import json
from pathlib import Path
import re
import unittest

import dashboard
import digest
from presentation import english_post
import signals

CFG=json.loads(Path('config.json').read_text())
NOW=1792324800


class EnglishPresentation(unittest.TestCase):
    def test_archived_metrics_and_generated_metadata_are_translated_without_mutation(self):
        original=dict(source_id='hf_models',selftext='text-generation · Лицензия: mit · Параметров: 123',
                      metrics=[dict(kind='likes',value=0,label='лайков')],title='Модель Qwen')
        normalized=english_post(original)
        self.assertEqual(normalized['metrics'][0],dict(kind='likes',value=0,label='likes'))
        self.assertEqual(normalized['selftext'],'text-generation · License: mit · Parameters: 123')
        self.assertEqual(normalized['title'],original['title'])
        self.assertEqual(original['metrics'][0]['label'],'лайков')
        story=dict(original,source_id='techcrunch')
        self.assertEqual(english_post(story)['selftext'],original['selftext'])

    def test_english_labels_are_rebuilt_from_an_old_archive(self):
        post=dict(id='test-model',source='site',source_id='hf_models',source_name='Hugging Face Models',
                  category='model',content_kind='model',filter_mode='all',title='Qwen 3.5',
                  url='https://huggingface.co/example/Qwen3.5',article_url='https://huggingface.co/example/Qwen3.5',
                  selftext='text-generation · Лицензия: mit',created_utc=NOW-3600,links=[],score=None,
                  metrics=[dict(kind='likes',label='лайков',value=100)])
        item=signals.build_items([post],CFG,NOW)[0]
        generated=dict(topic=item['topic'],reasons=item['reasons'],components=item['components'],
                       metrics=item['metrics'],description_kind=item['description_kind'],summary=item['summary'])
        self.assertIsNone(re.search('[А-Яа-яЁё]',json.dumps(generated,ensure_ascii=False)))
        data=dict(items=[item],generated_at=NOW,scoring_version=signals.SCORING_VERSION,signal_rankings={})
        self.assertIsNone(re.search('[А-Яа-яЁё]',digest.markdown(data,now=NOW)))
        self.assertIsNone(re.search('[А-Яа-яЁё]',digest.llms_txt(data)))

    def test_static_interface_and_readme_are_english(self):
        for path in ('README.md','web/dashboard.html','web/dashboard.js','web/signal-ranking.js'):
            self.assertIsNone(re.search('[А-Яа-яЁё]',Path(path).read_text()),path)
        self.assertIn('lang="en"',Path('web/dashboard.html').read_text())

    def test_russian_source_matching_is_preserved(self):
        p=dict(title='Корпоративный AI для бизнеса',selftext='',category='news')
        self.assertIn('Enterprise use',signals.enterprise_matches(p))


if __name__=='__main__':unittest.main()
