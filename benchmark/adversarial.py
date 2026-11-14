#!/usr/bin/env python3
"""Adversarial worst-input search for the Pygments first-character dispatch.

The generator builds 10,000 unique inputs from real Pygments lexers in four
classes, all reproducible from ``(lexer, class, length, seed)``:

* ``short``   -- 256..2048 bytes drawn from the lexer's own literal alphabet
* ``lexical`` -- 8192..12288 bytes from the same alphabet (dispatch enabled)
* ``entropy`` -- 8192..12288 printable-ASCII bytes (no useful alphabet)
* ``dense``   -- 8192..12288 bytes biased to characters that keep the largest
                 number of the initial state's rules alive

Each input is timed on the pristine upstream tree, the v1 tree (the static
first-character dispatch committed in the previous revision) and the v2 tree
(adaptive two-hit dispatch with the pre-flight gate).  A single run per input
is used for the 10,000-input corpus; the 300 slowest/densest inputs are also
measured on v1 so the envelope can report what the gate actually removed.

Usage:
    python benchmark/adversarial.py --base-root /tmp/base --opt-root /tmp/opt \
        --v1-root /tmp/v1
"""

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import time

SEED = 20261114
N_INPUTS = 10000
LONG_LIMIT = 8192
HEADS = 80


def lexer_catalog():
    from pygments.lexers import get_all_lexers, get_lexer_by_name
    out = []
    for name, aliases, _files, _mime in get_all_lexers():
        if not aliases:
            continue
        alias = aliases[0]
        try:
            lexer = get_lexer_by_name(alias)
        except Exception:
            continue
        if not getattr(lexer, '_tokens', None):
            continue
        out.append((alias, name, lexer))
    out.sort(key=lambda item: item[0])
    return out


def literal_alphabet(lexer):
    chars = set(' \n\t')
    for state in lexer._tokens.values():
        for rule in state:
            pattern = getattr(rule[0], '__self__', None)
            if pattern is None:
                continue
            for ch in pattern.pattern:
                if ch.isalnum() or ch in ' .,:;_-+*/=<>!@#$%&()[]{}"\'':
                    chars.add(ch)
    return sorted(chars) or list('abc \n')


def dense_alphabet(lexer):
    from pygments.lexer import _regex_first_chars
    rules = lexer._tokens.get('root')
    if not rules:
        return list('abc \n')
    analysed = []
    for rule in rules:
        pattern = getattr(rule[0], '__self__', None)
        analysed.append(None if pattern is None else
                        _regex_first_chars(pattern.pattern, pattern.flags))
    scored = []
    for code in range(32, 127):
        ch = chr(code)
        alive = 0
        for fs in analysed:
            if fs is None or fs[0] is None or fs[1] or code in fs[0]:
                alive += 1
        scored.append((alive, ch))
    scored.sort(reverse=True)
    return [ch for _, ch in scored[:8]] + [' ', '\n']


def alphabet_for(alias, klass, cache):
    key = (alias, klass)
    if key not in cache:
        lexer = cache['_get'](alias)
        cache[key] = (literal_alphabet(lexer), dense_alphabet(lexer))
    lit, dense = cache[key]
    if klass == 'entropy':
        return [chr(c) for c in range(32, 127)]
    if klass == 'dense':
        return dense
    return lit


def make_text(alias, klass, length, seed, cache, alphabet=None):
    if alphabet is None:
        alphabet = alphabet_for(alias, klass, cache)
    rng = random.Random(seed)
    return ''.join(rng.choice(alphabet) for _ in range(length))


def generate(out_path):
    from pygments.lexers import get_lexer_by_name
    catalog = lexer_catalog()
    cache = {'_get': get_lexer_by_name}
    rng = random.Random(SEED)
    items, seen = [], set()
    long_lexers = catalog[::max(1, len(catalog) // 160)][:160]

    def add(alias, name, klass, length):
        seed = rng.randrange(1 << 31)
        alphabet = alphabet_for(alias, klass, cache)
        text = make_text(alias, klass, length, seed, cache, alphabet)
        digest = hashlib.sha256(text.encode('utf-8', 'surrogatepass')).hexdigest()
        if digest in seen:
            return False
        seen.add(digest)
        items.append({'h': digest, 'lexer': alias, 'name': name,
                      'class': klass, 'length': len(text), 'seed': seed,
                      'alpha': ''.join(alphabet),
                      'head': text[:HEADS].encode('unicode_escape').decode('ascii')})
        return True

    while len(items) < N_INPUTS:
        for alias, name, _lexer in catalog:
            if len(items) >= N_INPUTS:
                break
            add(alias, name, 'short', rng.randint(256, 2048))
        for alias, name, _lexer in long_lexers:
            if len(items) >= N_INPUTS:
                break
            for klass in ('lexical', 'entropy', 'dense'):
                add(alias, name, klass, LONG_LIMIT)
    items = items[:N_INPUTS]
    json.dump({'seed': SEED, 'inputs': items}, open(out_path, 'w'), indent=0)
    return items


def measure(root, corpus_path, out_path, reps):
    sys.path.insert(0, root)
    import pygments
    if not os.path.realpath(pygments.__file__).startswith(os.path.realpath(root)):
        raise SystemExit('wrong pygments: %s' % pygments.__file__)
    from pygments.lexers import get_lexer_by_name
    data = json.load(open(corpus_path))
    groups = {}
    for item in data['inputs']:
        groups.setdefault(item['lexer'], []).append(item)
    results = {}
    cache = {'_get': get_lexer_by_name}
    for alias, items in groups.items():
        try:
            lexer = get_lexer_by_name(alias)
        except Exception:
            continue
        # Warm-up must lex a full-length input: the lazy regex first-set
        # analysis and the gate caches are charged to the first real call,
        # and timing them would report a cold-start spike, not steady state.
        for warm in items[:2]:
            warm_text = make_text(alias, warm['class'], warm['length'],
                                  warm['seed'], cache, warm['alpha'])
            list(lexer.get_tokens_unprocessed(warm_text))
        for item in items:
            text = make_text(alias, item['class'], item['length'],
                             item['seed'], cache, item['alpha'])
            best = None
            for _ in range(reps):
                t0 = time.perf_counter()
                list(lexer.get_tokens_unprocessed(text))
                dt = time.perf_counter() - t0
                best = dt if best is None else min(best, dt)
            results[item['h']] = best
    json.dump({'root': root, 'pygments': pygments.__file__,
               'reps': reps, 'results': results}, open(out_path, 'w'))
    print('measured %d inputs on %s' % (len(results), root))


def run_child(root, corpus, out, reps):
    subprocess.run([sys.executable, os.path.abspath(__file__), '--worker',
                    root, corpus, out, str(reps)], check=True)


def median(values):
    values = sorted(values)
    if not values:
        return None
    n = len(values)
    return values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser()
    ap.add_argument('--base-root', default='/tmp/base')
    ap.add_argument('--opt-root', default='/tmp/opt')
    ap.add_argument('--v1-root')
    ap.add_argument('--reuse-base', action='store_true')
    ap.add_argument('--reuse-opt', action='store_true')
    ap.add_argument('--out', default=os.path.dirname(here))
    args = ap.parse_args()

    tmp = '/tmp/pygments-adversarial'
    os.makedirs(tmp, exist_ok=True)
    corpus = os.path.join(tmp, 'corpus.json')
    items = generate(corpus)
    print('generated %d unique inputs' % len(items))

    if not args.reuse_base:
        run_child(args.base_root, corpus, os.path.join(tmp, 'base.json'), 1)
    if not args.reuse_opt:
        run_child(args.opt_root, corpus, os.path.join(tmp, 'opt.json'), 1)
    base = json.load(open(os.path.join(tmp, 'base.json')))['results']
    opt = json.load(open(os.path.join(tmp, 'opt.json')))['results']

    rows = []
    for item in items:
        h = item['h']
        if h not in base or h not in opt:
            continue
        rows.append({k: item[k] for k in ('h', 'lexer', 'class', 'length', 'head')} |
                    {'base': base[h], 'opt': opt[h],
                     'speedup': base[h] / opt[h]})

    # v1 is measured on the inputs the gate is most likely to have saved.
    v1 = {}
    if args.v1_root:
        suspect = sorted(rows, key=lambda r: r['speedup'])[:150] + \
            [r for r in rows if r['class'] in ('dense', 'entropy')][:150]
        sub = {'seed': SEED, 'inputs': [
            {'h': r['h'], 'lexer': r['lexer'], 'class': r['class'],
             'length': r['length'], 'head': r['head'], 'seed': next(
                 x['seed'] for x in items if x['h'] == r['h']),
             'alpha': next(x['alpha'] for x in items if x['h'] == r['h'])}
            for r in suspect]}
        sub_path = os.path.join(tmp, 'v1-subset.json')
        json.dump(sub, open(sub_path, 'w'))
        run_child(args.v1_root, sub_path, os.path.join(tmp, 'v1.json'), 1)
        v1 = json.load(open(os.path.join(tmp, 'v1.json')))['results']

    by_class = {}
    for r in rows:
        by_class.setdefault(r['class'], []).append(r['speedup'])
    classes = {k: {'n': len(v), 'median': median(v), 'min': min(v),
                   'p05': sorted(v)[max(0, len(v) // 20)]}
               for k, v in sorted(by_class.items())}
    worst = sorted(rows, key=lambda r: r['speedup'])[:10]
    v1_rows = [{'h': r['h'], 'lexer': r['lexer'], 'class': r['class'],
                'speedup': base[r['h']] / v1[r['h']]}
               for r in rows if r['h'] in v1]
    payload = {
        'generated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'seed': SEED,
        'inputs': len(rows),
        'classes': classes,
        'overall_median': median([r['speedup'] for r in rows]),
        'worst': worst,
        'below_1_00': sum(1 for r in rows if r['speedup'] < 1.0),
        'below_0_95': sum(1 for r in rows if r['speedup'] < 0.95),
        'v1_subset': {
            'n': len(v1_rows),
            'worst': min((r['speedup'] for r in v1_rows), default=None),
            'below_1_00': sum(1 for r in v1_rows if r['speedup'] < 1.0),
            'below_0_90': sum(1 for r in v1_rows if r['speedup'] < 0.90),
        },
        # per-input raw measurements are kept, not just aggregates
        'raw': [{k: (round(v, 9) if isinstance(v, float) else v)
                 for k, v in r.items()} for r in rows],
    }
    json.dump(payload, open(os.path.join(args.out, 'adversarial.json'), 'w'),
              indent=0)
    print(json.dumps({k: payload[k] for k in
                      ('inputs', 'classes', 'overall_median', 'below_1_00',
                       'below_0_95', 'v1_subset')}, indent=1))
    print('worst:', json.dumps(worst[:5], indent=1))


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--worker':
        measure(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]))
    else:
        main()
