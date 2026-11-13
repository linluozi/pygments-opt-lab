#!/usr/bin/env python3
"""Byte-for-byte differential test: upstream Pygments vs patched Pygments.

Inputs are (a) every 4-line window of the 20 largest CPython 3.12 stdlib
modules, (b) the whole files, and (c) hand-written edge cases (empty input,
non-ASCII, unterminated strings, CRLF, BOM, NUL, very long lines ...).
Windows are de-duplicated by the sha256 of their UTF-8 bytes, so the reported
sample count is the number of *independent* inputs, not loop iterations.

Run:
    BASE_PYGMENTS=/tmp/base OPT_PYGMENTS=/tmp/opt python -m pytest tests -q -s
"""

import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WINDOW = 4
MIN_UNIQUE = 9000

EDGE_CASES = [
    '',
    'x',
    '\n',
    '\r\n',
    '\ufeffimport os\n',
    '# -*- coding: utf-8 -*-\n# 中文注释\ns = "汉字🙂"\n',
    'x = "unterminated\n',
    "y = '''also unterminated\n",
    'a = f"{b!r:>{width}}"\n',
    'a = 0x1f + 0b1010 + 1_000_000 + 1e-9j\n',
    'def f(x):\n    return x @ y if x else None\n',
    '\x00\x01\x02 text with NUL\n',
    'x = ' + '1 + ' * 5000 + '1\n',
    '#' + 'a' * 20000 + '\n',
    '\\\n'.join('x%d = %d' % (i, i) for i in range(200)),
    'match x:\n    case {"a": [1, 2, *rest]} if rest:\n        pass\n',
    '@decorator(arg=1)\nclass C[T](Base):\n    async def m(self) -> None: ...\n',
    'print("\\N{SNOWMAN} \\u2603")\n',
    '[\n' * 100 + ']\n' * 100,
    'a=1;b=2;c=3\n' * 300,
]


def sha(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode('utf-8')).hexdigest()


def build_inputs():
    import sysconfig
    stdlib = sysconfig.get_paths()['stdlib']
    files = [os.path.join(stdlib, n) for n in os.listdir(stdlib) if n.endswith('.py')]
    files = sorted([f for f in files if os.path.isfile(f)],
                   key=os.path.getsize, reverse=True)[:20]
    items, seen = [], set()

    def add(text, origin):
        digest = sha(text)
        if digest in seen:
            return
        seen.add(digest)
        items.append([digest, text, origin])

    for path in files:
        text = open(path, encoding='utf-8').read()
        add(text, 'file:' + os.path.basename(path))
        lines = text.splitlines(keepends=True)
        for start in range(0, len(lines), WINDOW):
            chunk = ''.join(lines[start:start + WINDOW])
            if chunk:
                add(chunk, 'window:' + os.path.basename(path))
    for i, text in enumerate(EDGE_CASES):
        add(text, 'edge:%d' % i)
    return items, files


def token_digest(text, lexer):
    import pygments
    parts = []
    for index, token, value in lexer.get_tokens_unprocessed(text):
        parts.append('%d\x1f%s\x1f%s' % (index, token, value))
    payload = '\x1e'.join(parts).encode('utf-8', 'surrogatepass')
    return hashlib.sha256(payload).hexdigest()


def worker(root, inputs_path, out_path):
    sys.path.insert(0, root)
    import pygments
    if not os.path.realpath(pygments.__file__).startswith(os.path.realpath(root)):
        raise SystemExit('wrong pygments: %s' % pygments.__file__)
    from pygments.lexers import PythonLexer
    lexer = PythonLexer()
    items = json.load(open(inputs_path, encoding='utf-8'))
    out = {}
    for key, text, origin in items:
        out[key] = token_digest(text, lexer)
    json.dump(out, open(out_path, 'w'))
    print('worker %s: %d inputs, pygments %s' % (root, len(out), pygments.__version__))


def run_worker(root, inputs_path, out_path):
    subprocess.run([sys.executable, os.path.abspath(__file__),
                    '--worker', root, inputs_path, out_path], check=True)


def compare():
    base_root = os.environ.get('BASE_PYGMENTS', '/tmp/base')
    opt_root = os.environ.get('OPT_PYGMENTS', '/tmp/opt')
    items, files = build_inputs()
    if len(items) < MIN_UNIQUE:
        raise AssertionError('only %d independent inputs (need %d)' % (len(items), MIN_UNIQUE))
    tmp = os.path.join('/tmp', 'pygments-diff-%d' % os.getpid())
    os.makedirs(tmp, exist_ok=True)
    inputs_path = os.path.join(tmp, 'inputs.json')
    json.dump(items, open(inputs_path, 'w', encoding='utf-8'))
    base_out = os.path.join(tmp, 'base.json')
    opt_out = os.path.join(tmp, 'opt.json')
    run_worker(base_root, inputs_path, base_out)
    run_worker(opt_root, inputs_path, opt_out)
    base = json.load(open(base_out))
    opt = json.load(open(opt_out))
    assert set(base) == set(opt)
    bad = [k for k in base if base[k] != opt[k]]
    print('independent inputs: %d (sha256-deduped), corpus files: %d, '
          'total bytes: %d, mismatches: %d'
          % (len(items), len(files), sum(os.path.getsize(f) for f in files), len(bad)))
    if bad:
        raise AssertionError('%d mismatching inputs, first=%s' % (len(bad), bad[:3]))
    return len(items)


def test_diff_11000_inputs():
    n = compare()
    assert n >= MIN_UNIQUE


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--worker':
        worker(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        compare()
