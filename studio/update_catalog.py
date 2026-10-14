"""モデルの一覧 (catalog.json) を新しくする。

  python update_catalog.py                       # OpenRouter (価格・新しいモデル) と Artificial Analysis (性能) から取る
  python update_catalog.py --offline or.json [aa.json]   # 取ってきた JSON から作る (テスト・ネットワークが無いとき)
  python update_catalog.py --new-days 45         # 「新しいモデル」とみなす日数 (既定 60)
  python update_catalog.py --embed-only          # catalog.json を手で直したあと、index.html の控えだけ合わせる

- 価格: OpenRouter の GET /api/v1/models の pricing (1 トークンあたりの USD) を 100 万トークンあたりに直す。キーは要らない
- 性能: Artificial Analysis の GET /api/v2/data/llms/models の artificial_analysis_coding_index
  (PBI の実装に近いため)。無いモデルは artificial_analysis_intelligence_index で代える。
  環境変数 AA_API_KEY があるときだけ取る。無ければ quality は前の値のまま
- 新しいモデル: catalog の providers に載っている会社の、new-days 日以内に出たモデルで catalog に無いものを
  candidates に入れる。実行先 (runner) への対応は人が決めるので、models には足さない
- catalog.json と、index.html の中の控え (<script id="catalog-fallback">) の両方を書き換える
"""
import argparse
import datetime
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OPENROUTER = 'https://openrouter.ai/api/v1/models'
AA = 'https://artificialanalysis.ai/api/v2/data/llms/models'
QUALITY_METRICS = ('artificial_analysis_coding_index', 'artificial_analysis_intelligence_index')


def fetch(url: str, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, headers={'User-Agent': 'pbi-template-studio', **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def per_million(value) -> float | None:
    try:
        return round(float(value) * 1_000_000, 4)
    except (TypeError, ValueError):
        return None


def merge(catalog: dict, openrouter: dict | None, aa: dict | None, today: datetime.date, new_days: int) -> dict:
    out = json.loads(json.dumps(catalog))
    out['updated'] = today.isoformat()
    or_by_id = {m['id']: m for m in (openrouter or {}).get('data', [])}
    aa_by_slug = {m.get('slug'): m for m in (aa or {}).get('data', [])}
    for m in out['models']:
        o = or_by_id.get(m.get('openrouter_id'))
        if o:
            pin, pout = per_million(o.get('pricing', {}).get('prompt')), per_million(o.get('pricing', {}).get('completion'))
            if pin is not None and pout is not None:
                m['price_in'], m['price_out'] = pin, pout
                m['price_sample'] = False
        a = aa_by_slug.get(m.get('aa_slug'))
        evals = (a or {}).get('evaluations') or {}
        for metric in QUALITY_METRICS:
            if evals.get(metric) is not None:
                m['quality'] = round(float(evals[metric]), 1)
                m['quality_metric'] = metric
                m['quality_sample'] = False
                break
        m['sample'] = not (m.get('price_sample') is False and m.get('quality_sample') is False)
    if openrouter:
        out['sources']['openrouter'] = today.isoformat()
        known = {m.get('openrouter_id') for m in out['models']}
        providers = {p['id'] for p in out['providers']}
        since = datetime.datetime.combine(today - datetime.timedelta(days=new_days), datetime.time())
        out['candidates'] = sorted((
            {'openrouter_id': o['id'], 'provider': o['id'].split('/')[0], 'name': o.get('name', o['id']),
             'created': datetime.datetime.fromtimestamp(o['created'], datetime.timezone.utc).date().isoformat(),
             'price_in': per_million(o.get('pricing', {}).get('prompt')),
             'price_out': per_million(o.get('pricing', {}).get('completion'))}
            for o in or_by_id.values()
            if o['id'].split('/')[0] in providers and o['id'] not in known
            and o.get('created', 0) >= since.replace(tzinfo=datetime.timezone.utc).timestamp()
        ), key=lambda c: c['created'], reverse=True)
    if aa:
        out['sources']['artificial_analysis'] = today.isoformat()
    return out


def embed(html: str, catalog: dict) -> str:
    body = json.dumps(catalog, ensure_ascii=False, indent=2)
    pattern = re.compile(r'(<script type="application/json" id="catalog-fallback">)(.*?)(</script>)', re.S)
    if not pattern.search(html):
        sys.exit('index.html に catalog-fallback の script が無い')
    return pattern.sub(lambda m: m.group(1) + '\n' + body + '\n' + m.group(3), html, count=1)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--offline', nargs='+', metavar='JSON')
    ap.add_argument('--new-days', type=int, default=60)
    ap.add_argument('--today')
    ap.add_argument('--dir', default=str(HERE))
    ap.add_argument('--embed-only', action='store_true')
    args = ap.parse_args()
    base = Path(args.dir)
    if args.embed_only:
        html = base / 'index.html'
        html.write_text(embed(html.read_text(encoding='utf-8'), json.load(open(base / 'catalog.json', encoding='utf-8'))),
                        encoding='utf-8')
        return
    today = datetime.date.fromisoformat(args.today) if args.today else datetime.date.today()

    if args.offline:
        openrouter = json.load(open(args.offline[0], encoding='utf-8'))
        aa = json.load(open(args.offline[1], encoding='utf-8')) if len(args.offline) > 1 else None
    else:
        openrouter = fetch(OPENROUTER)
        key = os.environ.get('AA_API_KEY')
        aa = fetch(AA, {'x-api-key': key}) if key else None
        if not key:
            print('AA_API_KEY が無いので、性能 (quality) は前の値のまま')

    catalog = merge(json.load(open(base / 'catalog.json', encoding='utf-8')), openrouter, aa, today, args.new_days)
    (base / 'catalog.json').write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    html = base / 'index.html'
    if html.exists():
        html.write_text(embed(html.read_text(encoding='utf-8'), catalog), encoding='utf-8')
    sample = [m['name'] for m in catalog['models'] if m.get('sample')]
    print(f"モデル {len(catalog['models'])} 件を更新。新しい候補 {len(catalog.get('candidates', []))} 件。"
          + (f" 仮の値のまま: {', '.join(sample)}" if sample else ''))


if __name__ == '__main__':
    main()
