#!/usr/bin/env python3
"""Benchmark Pygments' RegexLexer first-character dispatch.

Two roots are compared (each a directory containing a ``pygments`` package):
the unmodified upstream tree and the tree with patch.diff applied.  Every
sample is timed with ``time.perf_counter`` and written to ``raw/``; only
aggregates are derived from those files.

Usage:
    python benchmark/bench.py --base-root /tmp/base --opt-root /tmp/opt

The corpus is the 20 largest ``*.py`` files of the running CPython's standard
library (2,021,262 bytes on CPython 3.12.3).  It is real code, not generated
text, and the exact names/sizes are recorded in baseline.json.
"""

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time

WARMUP = 3
REPEATS = 5
CORPUS_SIZE = 20


def corpus_files(stdlib):
    files = [os.path.join(stdlib, n) for n in os.listdir(stdlib)
             if n.endswith('.py')]
    files = [f for f in files if os.path.isfile(f)]
    files.sort(key=os.path.getsize, reverse=True)
    return files[:CORPUS_SIZE]


def slug(path):
    return os.path.basename(path)[:-3]


def worker(root, outdir, corpus):
    sys.path.insert(0, root)
    import pygments
    if not os.path.realpath(pygments.__file__).startswith(os.path.realpath(root)):
        raise SystemExit('imported wrong pygments: %s' % pygments.__file__)
    from pygments.lexers import PythonLexer

    impl = os.path.basename(root.rstrip('/'))
    with open(os.path.join(outdir, impl + '-env.json'), 'w') as f:
        json.dump({'root': root, 'pygments': pygments.__file__,
                   'version': pygments.__version__,
                   'python': sys.version, 'platform': platform.platform()}, f,
                  indent=1)

    lexer = PythonLexer()
    texts = {p: open(p, encoding='utf-8').read() for p in corpus}

    def run(paths):
        out = []
        for p in paths:
            out.extend(lexer.get_tokens_unprocessed(texts[p]))
        return out

    def timed(paths, label):
        for i in range(WARMUP):
            run(paths)
        for i in range(REPEATS):
            t0 = time.perf_counter()
            toks = run(paths)
            dt = time.perf_counter() - t0
            rec = {'impl': impl, 'scale': label, 'i': i, 'seconds': dt,
                   'bytes': sum(os.path.getsize(p) for p in paths),
                   'tokens': len(toks), 'files': [os.path.basename(p) for p in paths]}
            with open(os.path.join(outdir, '%s-%s-%d.json' % (impl, label, i)), 'w') as f:
                json.dump(rec, f)
        return [json.load(open(os.path.join(outdir, '%s-%s-%d.json' % (impl, label, i))))
                for i in range(REPEATS)]

    # Three scales: 1 file (101 KB), 4 files (475 KB), all 20 files (2.0 MB).
    small = corpus[:1]
    typed_scales = {'small': small, 'medium': corpus[:4], 'large': corpus}
    for label, paths in typed_scales.items():
        timed(paths, label)

    # Per-file samples, for the worst-file floor.  One warmup is enough here:
    # the same lexer object and text have already been exercised above.
    per_file = {}
    for p in corpus:
        for _ in range(1):
            run([p])
        samples = []
        for i in range(REPEATS):
            t0 = time.perf_counter()
            toks = run([p])
            dt = time.perf_counter() - t0
            rec = {'impl': impl, 'scale': 'file-' + slug(p), 'i': i,
                   'seconds': dt, 'bytes': os.path.getsize(p), 'tokens': len(toks),
                   'files': [os.path.basename(p)]}
            with open(os.path.join(outdir, '%s-file-%s-%d.json' % (impl, slug(p), i)), 'w') as f:
                json.dump(rec, f)
            samples.append(dt)
        per_file[slug(p)] = samples

    return typed_scales, per_file


def summarize(samples):
    s = sorted(samples)
    med = statistics.median(s)
    p25 = statistics.quantiles(s, n=4)[0]
    p75 = statistics.quantiles(s, n=4)[2]
    mean = statistics.mean(s)
    rsd = statistics.pstdev(s) / mean if mean else 0.0
    return {'median': med, 'p25': p25, 'p75': p75, 'runs': len(s), 'rsd': rsd}


def aggregate(rawdir, outdir, impl, scales, per_file, corpus):
    bench = {}
    for label, paths in scales.items():
        samples = [json.load(open(os.path.join(rawdir, '%s-%s-%d.json' % (impl, label, i))))['seconds']
                   for i in range(REPEATS)]
        d = summarize(samples)
        d['bytes'] = sum(os.path.getsize(p) for p in paths)
        d['files'] = [os.path.basename(p) for p in paths]
        bench[label] = d
    files = {}
    for p in corpus:
        d = summarize(per_file[slug(p)])
        d['bytes'] = os.path.getsize(p)
        files[slug(p)] = d
    result = {
        'impl': impl,
        'python': sys.version,
        'platform': platform.platform(),
        'seed': 0,
        'randomness': 'none: Pygments lexing is deterministic',
        'warmup': WARMUP,
        'repeats': REPEATS,
        'command': ' '.join(sys.argv),
        'cpu': cpu_model(),
        'memory_kb': memory_kb(),
        'corpus_total_bytes': sum(os.path.getsize(p) for p in corpus),
        'corpus': [{'name': os.path.basename(p), 'bytes': os.path.getsize(p)} for p in corpus],
        'scales': bench,
        'files': files,
    }
    name = 'baseline.json' if impl == 'base' else 'optimized.json'
    with open(os.path.join(outdir, name), 'w') as f:
        json.dump(result, f, indent=1)
    return result


def cpu_model():
    try:
        for line in open('/proc/cpuinfo'):
            if line.startswith('model name'):
                return line.split(':', 1)[1].strip()
    except OSError:
        pass
    return platform.processor()


def memory_kb():
    try:
        for line in open('/proc/meminfo'):
            if line.startswith('MemTotal'):
                return int(line.split()[1])
    except OSError:
        pass
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base-root', required=True)
    ap.add_argument('--opt-root', required=True)
    ap.add_argument('--stdlib', default=os.path.dirname(os.__file__))
    ap.add_argument('--out', default=os.path.join(os.path.dirname(__file__)))
    args = ap.parse_args()
    outdir = args.out
    raw = os.path.join(outdir, 'raw')
    os.makedirs(raw, exist_ok=True)
    corpus = corpus_files(args.stdlib)
    total = sum(os.path.getsize(p) for p in corpus)
    if total < 2_000_000:
        raise SystemExit('corpus too small: %d bytes' % total)
    print('corpus: %d files, %d bytes' % (len(corpus), total))

    results = {}
    for root in (args.base_root, args.opt_root):
        impl = os.path.basename(root.rstrip('/'))
        print('running', impl)
        subprocess.run([sys.executable, __file__, '--worker', root, raw,
                        '--stdlib', args.stdlib], check=True)
        scales = {'small': corpus[:1], 'medium': corpus[:4], 'large': corpus}
        per_file = {}
        for p in corpus:
            per_file[slug(p)] = [json.load(open(os.path.join(
                raw, '%s-file-%s-%d.json' % (impl, slug(p), i))))['seconds']
                for i in range(REPEATS)]
        results[impl] = aggregate(raw, outdir, impl, scales, per_file, corpus)

    base = results['base']
    opt = results['opt']
    lines = ['| scale | bytes | base median (s) | opt median (s) | speedup |',
             '|---|---|---|---|---|']
    for label in ('small', 'medium', 'large'):
        b = base['scales'][label]['median']
        o = opt['scales'][label]['median']
        lines.append('| %s | %d | %.4f | %.4f | %.2fx |' % (
            label, base['scales'][label]['bytes'], b, o, b / o))
    ratios = []
    for p in corpus:
        b = base['files'][slug(p)]['median']
        o = opt['files'][slug(p)]['median']
        ratios.append(b / o)
    ratios.sort()
    lines.append('| **20-file corpus median** | %d | — | — | **%.2fx** |' % (
        base['corpus_total_bytes'], statistics.median(ratios)))
    lines.append('| worst file | — | — | — | %.2fx |' % ratios[0])
    table = '\n'.join(lines)
    print(table)
    with open(os.path.join(outdir, 'summary.md'), 'w') as f:
        f.write(table + '\n')


if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == '--worker':
        worker(sys.argv[2], sys.argv[3], corpus_files(sys.argv[5]))
    else:
        main()
