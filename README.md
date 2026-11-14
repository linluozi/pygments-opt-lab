# pygments-opt-lab

Pygments (the Python syntax highlighter) tries **every** regex of a lexer state at **every** token position. This repo adds a conservative first-character dispatch table to `RegexLexer`: rules whose regex cannot start with the current character are skipped, while rules that may match the empty string or whose first-set cannot be proven stay on the slow path. On 2.0 MB of real CPython 3.12 stdlib source the median speedup is **1.63x**, worst file **1.40x**, with **13,812 independently de-duplicated inputs byte-identical**.

> **Adversarial audit (2026-11-14).** This dispatch is **not safe as a general-purpose `RegexLexer` optimisation**. On 295 real Pygments lexers outside the stdlib corpus it is up to **102x slower** than upstream (worst 0.0098x), and a prototype runtime fallback only raised the worst case to 0.198x. The stdlib speedup below is real, but it is a corpus-specific result; read the audit section before using this patch.

Upstream: https://github.com/pygments/pygments

Base commit: `156d85fdd18d34427fe893527e71da91dcfb0eb5` (Pygments 2.21.0).

## Benchmark

`benchmark/bench.py` lexes the 20 largest `*.py` files of the running CPython stdlib (2,021,262 bytes total; names and sizes are recorded in `benchmark/baseline.json`). Warmup 3, timed 5, `time.perf_counter()`, every sample written to `benchmark/raw/`. Same machine, same interpreter, same corpus for both sides; the only difference is `patch.diff`.

| scale | bytes | baseline median (s) | optimized median (s) | speedup |
|---|---|---|---|---|
| small (1 file) | 229,220 | 0.2832 | 0.1463 | **1.94x** |
| medium (4 files) | 618,429 | 0.7932 | 0.4535 | **1.75x** |
| large (20 files) | 2,021,262 | 2.9567 | 1.7266 | **1.71x** |

Quartiles of the timed samples (baseline p25/p75 → optimized p25/p75, seconds): small 0.2741/0.3010 → 0.1454/0.1547; medium 0.7886/0.8481 → 0.4460/0.4645; large 2.9409/3.0947 → 1.7179/1.7935.

Per-file medians (20 real files): median **1.63x**, worst **1.40x** (`pickletools.py`), best **1.87x** (`_pydecimal.py`). Relative standard deviation is < 5.4% for every scale in this committed run (raw samples are kept, so this is re-computable). No synthetic repetitions were used; input bytes are exactly the files on disk.

## Differential test and quality gates

`tests/test_diff.py` takes every 4-line window of the same 20 files, the whole files, and 20 edge cases (empty, non-ASCII/emoji, BOM, CRLF, NUL, unterminated strings, 20 KB comment line, 5,000-term expression, 100-deep nesting, `match`/PEP 695 syntax). Inputs are de-duplicated by the sha256 of their UTF-8 bytes **before** lexing; the token stream (index + token type + value) is serialized and hashed.

| gate | result | command |
|---|---|---|
| G1 upstream tests | **5329 passed, 16 skipped, 0 failed** (89.35 s) | `cd pygments && python -m pytest tests --ignore=tests/contrast -q` |
| G2 differential | **13,812 independent inputs, 0 mismatches** | `BASE_PYGMENTS=/tmp/base OPT_PYGMENTS=/tmp/opt python -m pytest tests -q -s` |
| G3 median speedup (3 scales) | **1.75x** (1.94 / 1.75 / 1.71) | `python benchmark/bench.py --base-root /tmp/base --opt-root /tmp/opt` |
| G3b real-corpus speedup | **1.63x** on 2,021,262 bytes of real stdlib source | same |
| G4 largest scale | **1.71x** (≥ 0.95x) | same |
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

# 2. benchmark (per-sample JSON into benchmark/raw/, aggregates into benchmark/baseline.json and benchmark/optimized.json)
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

**B 档：把正则表达式的保守首字符集分析用于 Pygments 的规则分派，并在真实语料上量化收益与逐文件下限。** 此前 Pygments 的 `RegexLexer` 在每个 token 位置线性尝试该状态的全部规则；本仓库首次用 `re._parser` 的语法树证明「哪些规则不可能以当前字符开头」，对不可证明（含 `(?i)`、否定字符类、未知 opcode）的规则一律回退全表，并给出 20 个真实文件的整档加速与最差文件 1.40x 的下限，而不是只报一个挑出来的合成负载数字。与台账里已有的「调用点归因 / 标度证书 / 回退分支」不同：这里的新结论是**分派表本身能在通用 Python 源码上带来 1.40–1.94x**，且正确性由 13,812 个去重输入逐字节证明。

## Adversarial audit (2026-11-14)

`benchmark/adversarial.py` builds **10,000 unique inputs** from 160+ real Pygments lexers in four
classes, each reproducible from `(lexer, class, length, seed)` (seed `20261114`): `short`
(256-2048 bytes), `lexical`/`entropy`/`dense` (8192 bytes from the lexer alphabet, printable
ASCII, or the characters that keep the most initial-state rules alive). Every input is regenerated
from its stored alphabet + seed, so each witness is byte-reproducible; `adversarial.json` keeps the
per-input base/optimized times and the ten worst witnesses (`sha256`, lexer, class, length, seed,
first 80 bytes).

The shipped patch (v1, static per-`(state, character)` cache) was measured on the **295 inputs most
likely to expose it** (the 150 lowest-scoring inputs plus 150 `dense`/`entropy` inputs; this is a
targeted subset, not a random sample):

| audit metric | shipped v1 | prototype v2 (probe + two-hit, **not shipped**) |
|---|---|---|
| inputs measured | 295 | 10,000 |
| worst speedup | **0.0098x** (102x slower) | 0.198x (5.1x slower) |
| inputs < 0.90x | 189 / 295 | - |
| inputs < 1.00x | 216 / 295 | 4871 / 10000 |

Prototype v2 adds a 64-position runtime probe, a selectivity gate and two-hit filter construction.
It improves the worst case from 0.0098x to 0.198x but does **not** close it: a lexer that emits many
short tokens in its first 64 positions and then switches to a few huge tokens fools the
remaining-position estimate, enables the filter, and never amortises its construction cost.
Cross-class medians for v2 were dense 1.261x, lexical 1.191x, entropy 1.002x, short 0.988x.

**Conclusion (negative result).** First-character dispatch is only defensible inside a corpus whose
token shape has been measured. `patch.diff` is unchanged (v1); v2 was evaluated and rejected. The
reusable deliverable here is the audit harness, not the speedup table above.

Reproduce (v1 is `patch.diff`; the v1-vs-base audit needs a v1 root):

```bash
cp -r /tmp/base /tmp/v1 && cd /tmp/v1 && git apply /path/to/pygments-opt-lab/patch.diff
cd pygments-opt-lab && python3 benchmark/adversarial.py --base-root /tmp/base --opt-root /tmp/v1
```

## Limits

- Sample counts are 5 timed runs per scale; the machine is shared, so absolute seconds move between runs. Across the runs performed here the large-scale speedup was 1.68–1.71x and the worst file 1.40–1.51x; per-file ratios below 1.5x sit a few percent apart. One rerun on a loaded machine showed 5.4% relative stddev on the small baseline, so treat single-digit differences as noise.
- The gain depends on the rule mix: states dominated by `(?i)` or negated classes fall back to the full list. This is measured on Python source only for the headline; other lexers are covered by the 5,329-test upstream suite, not by a separate benchmark.
- `python3.10` / `python3.11` are not installed here, so the `sre_parse` fallback path is untested on this machine.
- The corpus is CPython 3.12's stdlib; a different corpus (short lines, heavy comments) will land at different ratios.

## 本轮创新（2026-11-14 审计）

**B 档：把「对抗性最坏输入搜索」作为发布门槛，并接受它给出的否定结论。** 之前所有
`-opt-lab` 只证明真实语料的中位数；本轮从 160+ 个真实 Pygments lexer 里构造 10,000 个
对抗输入（高熵 / 规则稠密 / 一次性字符 / 短输入），先证伪再谈收益：shipped v1 在目标子集上
最坏 **0.0098x**（189/295 低于 0.90x）。我随后实现了「64 位置运行时探针 + 选择性闸门 + 双命中
滤表」的原型 v2，把最坏提到 **0.198x**，但**仍未封住**——「先碎后整」的 lexer 会让剩余位置
估计失效。与台账里「逐 hunk 消融 / 输入类下限 / 静态回退分支」不同：本轮的新结论是
**首字符分派不能作为通用 RegexLexer 优化发布**，只能作为特定语料的实验性加速；把负面结论
连同可复算的 10,000 个 witness 一起发布，比再报一个 1.6x 更有用。
