import datetime
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import update_catalog  # noqa: E402

TODAY = datetime.date(2026, 10, 14)
DAY = 86400
NOW = datetime.datetime(2026, 10, 14, tzinfo=datetime.timezone.utc).timestamp()

CATALOG = {
    'updated': '2026-01-01', 'sources': {'openrouter': None, 'artificial_analysis': None},
    'providers': [{'id': 'anthropic'}, {'id': 'deepseek'}],
    'models': [
        {'id': 'a', 'provider': 'anthropic', 'openrouter_id': 'anthropic/a', 'aa_slug': 'a',
         'quality': 50, 'price_in': 1, 'price_out': 1, 'sample': True, 'options': []},
        {'id': 'd', 'provider': 'deepseek', 'openrouter_id': 'deepseek/d', 'aa_slug': 'd',
         'quality': 40, 'price_in': 1, 'price_out': 1, 'sample': True, 'options': []},
    ],
}
OPENROUTER = {'data': [
    {'id': 'anthropic/a', 'name': 'A', 'created': NOW - 200 * DAY,
     'pricing': {'prompt': '0.000003', 'completion': '0.000015'}},
    {'id': 'anthropic/new', 'name': 'New', 'created': NOW - 5 * DAY,
     'pricing': {'prompt': '0.000001', 'completion': '0.000002'}},
    {'id': 'anthropic/old', 'name': 'Old', 'created': NOW - 300 * DAY, 'pricing': {}},
    {'id': 'someone/else', 'name': 'Else', 'created': NOW - 1 * DAY, 'pricing': {}},
]}
AA = {'data': [
    {'slug': 'a', 'evaluations': {'artificial_analysis_intelligence_index': 71.24}},
    {'slug': 'd', 'evaluations': {'artificial_analysis_coding_index': 55.0, 'artificial_analysis_intelligence_index': 60.0}},
]}


class MergeTest(unittest.TestCase):
    def test_prices_quality_and_candidates(self):
        out = update_catalog.merge(CATALOG, OPENROUTER, AA, TODAY, 60)
        a, d = out['models']
        self.assertEqual((a['price_in'], a['price_out']), (3.0, 15.0))
        self.assertEqual(a['quality'], 71.2)
        self.assertFalse(a['sample'])
        self.assertEqual(a['quality_metric'], 'artificial_analysis_intelligence_index')  # コーディング指標が無いので総合で代える
        self.assertEqual((d['quality'], d['quality_metric']), (55.0, 'artificial_analysis_coding_index'))
        self.assertTrue(d['sample'])  # 価格が OpenRouter に無いので仮の値のまま
        self.assertEqual([c['openrouter_id'] for c in out['candidates']], ['anthropic/new'])
        self.assertEqual(out['sources'], {'openrouter': '2026-10-14', 'artificial_analysis': '2026-10-14'})

    def test_without_aa_keeps_quality(self):
        out = update_catalog.merge(CATALOG, OPENROUTER, None, TODAY, 60)
        self.assertEqual(out['models'][0]['quality'], 50)
        self.assertTrue(out['models'][0]['sample'])
        self.assertIsNone(out['sources']['artificial_analysis'])

    def test_embed_replaces_fallback(self):
        html = '<p></p><script type="application/json" id="catalog-fallback">{}</script><script>x</script>'
        out = update_catalog.embed(html, {'updated': 'z'})
        self.assertIn('"updated": "z"', out)
        self.assertIn('<script>x</script>', out)

    def test_embed_escapes_script_close(self):
        html = '<script type="application/json" id="catalog-fallback">{}</script>'
        out = update_catalog.embed(html, {'name': '</script><b>x'})
        self.assertEqual(out.count('</script>'), 1)
        body = out[out.index('>') + 1:out.rindex('</script>')]
        self.assertEqual(json.loads(body), {'name': '</script><b>x'})

    def test_page_fallback_matches_catalog(self):
        with open(os.path.join(HERE, 'index.html'), encoding='utf-8') as f:
            html = f.read()
        start = html.index('id="catalog-fallback">') + len('id="catalog-fallback">')
        embedded = json.loads(html[start:html.index('</script>', start)])
        with open(os.path.join(HERE, 'catalog.json'), encoding='utf-8') as f:
            self.assertEqual(embedded, json.load(f))


if __name__ == '__main__':
    unittest.main()
