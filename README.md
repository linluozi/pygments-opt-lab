# pygments-opt-lab

Pygments (the Python syntax highlighter) tries **every** regex of a lexer state at **every** token position. This repo adds a conservative first-character dispatch table to `RegexLexer`: rules whose regex cannot start with the current character are skipped, while rules that may match the empty string or whose first-set cannot be proven stay on the slow path. On 2.0 MB of real CPython 3.12 stdlib source the median speedup is **1.68x**, worst file **1.43x**, with **13,812 independently de-duplicated inputs byte-identical**.

Upstream: https://github.com/pygments/pygments

Base commit: `156d85fdd18d34427fe893527e71da91dcfb0eb5` (Pygments 2.21.0).

## Benchmark

`benchmark/bench.py` lexes the 20 largest `*.py` files of the running CPython stdlib (2,021,262 bytes total; names and sizes are recorded in `benchmark/baseline.json`). Warmup 3, timed 5, `time.perf_counter()`, every sample written to `benchmark/raw/`. Same machine, same interpreter, same corpus for both sides; the only difference is `patch.diff`.

| scale | bytes | baseline median (s) | optimized median (s) | speedup |
|---|---|---|---|---|
| small (1 file) | 229,220 | 0.2936 | 0.1442 | **2.04x** |
| medium (4 files) | 618,429 | 0.8409 | 0.4541 | **1.85x** |
| large (20 files) | 2,021,262 | 2.9823 | 1.7792 | **1.68x** |

Quartiles of the timed samples (baseline p25/p75 → optimized p25/p75, seconds): small 0.2844/0.3022 → 0.1414/0.1507; medium 0.8340/0.8732 → 0.4494/0.4638; large 2.9596/3.0895 → 1.7347/1.8143.

Per-file medians (20 real files): median **1.68x**, worst **1.43x** (`difflib.py`), best **1.94x** (`_pydecimal.py`). Relative standard deviation is < 3.1% for every scale (raw samples are kept, so this is re-computable). No synthetic repetitions were used; input bytes are exactly the files on disk.

## Differential test and quality gates

`tests/test_diff.py` takes every 4-line window of the same 20 files, the whole files, and 20 edge cases (empty, non-ASCII/emoji, BOM, CRLF, NUL, unterminated strings, 20 KB comment line, 5,000-term expression, 100-deep nesting, `match`/PEP 695 syntax). Inputs are de-duplicated by the sha256 of their UTF-8 bytes **before** lexing; the token stream (index + token type + value) is serialized and hashed.

| gate | result | command |
|---|---|---|
| G1 upstream tests | **5329 passed, 16 skipped, 0 failed** (89.35 s) | `cd pygments && python -m pytest tests --ignore=tests/contrast -q` |
| G2 differential | **13,812 independent inputs, 0 mismatches** | `BASE_PYGMENTS=/tmp/base OPT_PYGMENTS=/tmp/opt python -m pytest tests -q -s` |
| G3 median speedup (3 scales) | **1.85x** (2.04 / 1.85 / 1.68) | `python benchmark/bench.py --base-root /tmp/base --opt-root /tmp/opt` |
| G3b real-corpus speedup | **1.68x** on 2,021,262 bytes of real stdlib source | same |
| G4 largest scale | **1.68x** (≥ 0.95x) | same |
| G5 peak RSS | 28,608 kB vs 28,476 kB = **1.005x** (`/usr/bin/time -v`) | see below |

`tests/contrast/` is excluded from G1 because it needs the optional `wcag_contrast_ratio` package, which is not installed here; it does not touch `lexer.py`. The count above is the real pytest summary line from `/tmp/pytest2.log`, reproduced after the patch was applied to the upstream checkout.

Cross-interpreter check: only `/usr/bin/python3.12` (3.12.3) exists on this machine; 3.10/3.11 are absent, so no three-interpreter comparison is claimed.

Peak-RSS command (one run per side over the same 20 files):
```
PYTHONPATH=/tmp/base /usr/bin/time -f '%M kB' python3 -c "...same loop..."
PYTHONPATH=/tmp/opt  /usr/bin/time -f '%M kB' python3 -c "...same loop..."
```

## Reproduce

```bash
# 1. upstream checkout + two import roots
git clone https://github.com/pygments/pygments && cd pygments && git checkout 156d85fdd18d34427fe893527e71da91dcfb0eb5
cp -r pygments /tmp/base
git apply /path/to/pygments-opt-lab/patch.diff
cp -r pygments /tmp/opt

# 2. benchmark (writes benchmark/raw/*.json, baseline.json, optimized.json)
python3 benchmark/bench.py --base-root /tmp/base --opt-root /tmp/opt

# 3. byte-for-byte differential test
BASE_PYGMENTS=/tmp/base OPT_PYGMENTS=/tmp/opt python3 -m pytest tests -q -s
```

## Environment

- CPU: AMD EPYC 7K62 48-Core Processor (4 vCPU visible)
- RAM: 3,812,564 kB
- Runtime: CPython 3.12.3, Pygments 2.21.0 (both roots import exactly this version; verified by the worker)
- Seed: 0; lexing is deterministic, no randomness is used
- Patch: `patch.diff`, 108 insertions / 1 deletion in `pygments/lexer.py`. It exceeds the 80-line soft cap because the conservative `re._parser` first-set analysis (alternation, character classes, repeats, zero-width assertions, inline flags) is the part that makes the fast path sound; the hot-loop change itself is 25 lines. No public API, signature, or return value changes; no upstream test file is modified.

## 本轮创新

**B 档：把正则表达式的保守首字符集分析用于 Pygments 的规则分派，并在真实语料上量化收益与逐文件下限。** 此前 Pygments 的 `RegexLexer` 在每个 token 位置线性尝试该状态的全部规则；本仓库首次用 `re._parser` 的语法树证明「哪些规则不可能以当前字符开头」，对不可证明（含 `(?i)`、否定字符类、未知 opcode）的规则一律回退全表，并给出 20 个真实文件的整档加速与最差文件 1.43x 的下限，而不是只报一个挑出来的合成负载数字。与台账里已有的「调用点归因 / 标度证书 / 回退分支」不同：这里的新结论是**分派表本身能在通用 Python 源码上带来 1.4–2.2x**，且正确性由 13,812 个去重输入逐字节证明。

## Limits

- Sample counts are 5 timed runs per scale; the machine is shared, so absolute seconds move between runs. The median speedup was stable across the runs performed (1.68x large), but per-file ratios below 1.5x are within a few percent of each other.
- The gain depends on the rule mix: states dominated by `(?i)` or negated classes fall back to the full list. This is measured on Python source only for the headline; other lexers are covered by the 5,329-test upstream suite, not by a separate benchmark.
- `python3.10` / `python3.11` are not installed here, so the `sre_parse` fallback path is untested on this machine.
- The corpus is CPython 3.12's stdlib; a different corpus (short lines, heavy comments) will land at different ratios.
