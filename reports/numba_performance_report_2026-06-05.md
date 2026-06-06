# Numba Performance Report - 2026-06-05

## Scope

This benchmark tested an optional Numba accelerator against the existing mypyc-compiled package. The acceleration target is intentionally narrow: TMX status classification in `lokit.parsers.tmx.props.TmxProps.status_from_values`.

Numba is not applied to the XML iterator, exporter IO, or dataclass construction paths because those paths are dominated by `lxml`, filesystem writes, and Python object creation. Applying JIT there would either fail nopython compilation or add overhead without moving the hot path.

## Implementation

- Added `lokit._accelerators.STATUS_CODE`, which uses the pure Python classifier by default.
- Added opt-in Numba compilation with `LOKIT_ENABLE_NUMBA=1`.
- Added optional extras:
  - `accelerators`: installs `numba>=0.63`
  - `perf`: installs `numba>=0.63` and `psutil>=5.9`
- Kept the rest of the package mypyc compiled; the benchmark confirmed 50 compiled extensions loaded for every measurement.
- Excluded `_accelerators.py` from mypyc so the Numba dispatcher wraps a normal Python function and does not interfere with compiled extension imports.

## Method

Input file:

`/Users/ciaran/code/lokit/lokit/test_data/tmx/en_US-de_DE-2026-01-01.tmx`

Source size:

612 MB

Benchmark command shape:

```bash
cd /tmp
LOKIT_ENABLE_NUMBA=0 uv --project /Users/ciaran/code/lokit/lokit run --extra perf --no-editable \
  python -X noadaptive /Users/ciaran/code/lokit/lokit/benchmarks/numba_tmx_conversion.py \
  --source /Users/ciaran/code/lokit/lokit/test_data/tmx/en_US-de_DE-2026-01-01.tmx \
  --output /Users/ciaran/code/lokit/lokit/.benchmarks/numba_20260605 \
  --operation jsonl_text
```

Controls:

- Each operation ran in a fresh process.
- CPython adaptive specialization was disabled with `-X noadaptive`.
- Numba compilation was warmed before timing.
- Benchmarks used a non-editable package install so the imported package came from the built wheel rather than `src`.
- Each result recorded compiled extension count, wall time, CPU time, peak RSS, Python version, and interpreter `xoptions`.

## Results

| Operation | Wall Off | Wall On | Wall Delta | CPU Off | CPU On | CPU Delta | RSS Off | RSS On | RSS Delta |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `jsonl_text` | 16.934s | 15.435s | +8.85% | 16.653s | 15.034s | +9.72% | 36.8 MB | 113.4 MB | +76.6 MB |
| `jsonl` | 76.423s | 74.791s | +2.14% | 75.582s | 73.816s | +2.34% | 37.0 MB | 116.1 MB | +79.1 MB |
| `csv` | 19.288s | 20.904s | -8.38% | 19.003s | 20.715s | -9.01% | 36.0 MB | 114.8 MB | +78.8 MB |
| `xliff` | 22.306s | 23.268s | -4.31% | 22.115s | 23.022s | -4.11% | 36.4 MB | 114.4 MB | +78.0 MB |
| `tmx` | 26.197s | 25.124s | +4.09% | 25.460s | 24.889s | +2.25% | 36.4 MB | 115.2 MB | +78.9 MB |
| `po` | 34.119s | 33.009s | +3.25% | 33.562s | 32.782s | +2.33% | 1109.4 MB | 1151.5 MB | +42.0 MB |
| `xlsx` | 21.107s | 22.792s | -7.99% | 20.866s | 22.567s | -8.15% | 37.9 MB | 115.5 MB | +77.6 MB |

## Findings

Numba is not a clear default win for this repository right now.

It improved wall time in four measured operations: `jsonl_text`, `jsonl`, `tmx`, and `po`. The gains ranged from 2.14% to 8.85%.

It slowed three measured operations: `csv`, `xliff`, and `xlsx`. The regressions ranged from 4.31% to 8.38%.

It consistently increased memory usage. Streaming formats gained roughly 76-79 MB of peak RSS from importing and using the Numba runtime. The PO path was already memory-heavy because `polib.POFile` accumulates entries; Numba added another 42 MB there.

## Recommendation

Keep Numba optional and disabled by default.

The current parser/exporter architecture is already faster from mypyc plus C/Rust-backed libraries (`lxml`, `python-calamine`, `rustpy-xlsxwriter`) than it is from JITing tiny string branches. Numba can remain available for users who explicitly prefer the small gains in status-heavy TMX workflows and can afford the memory/runtime dependency.

Do not make `numba` a required dependency unless a future refactor introduces numeric/vectorizable workloads or larger pure-Python loops that Numba can compile without increasing memory enough to offset speed.

