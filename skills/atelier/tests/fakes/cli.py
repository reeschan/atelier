import json
import os
import sys
from pathlib import Path

if '-o' in sys.argv:
    Path(sys.argv[sys.argv.index('-o') + 1]).write_text('done', encoding='utf-8')
print(os.environ.get('FAKE_OUTPUT', json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 7, 'output_tokens': 3}})))
