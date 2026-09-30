# Probe.EXE pre-registration

Pre-registered predictions for the validation of the Probe.EXE prediction model on GenLayer Bradbury
(September 28-30, 2026). Every `window-<w>.json` was committed and pushed before its window was run;
GitHub keeps the push time.

- `window-v2.json` to `window-v6.json`: predictions of first-attempt acceptance per contract (primary
  model C, judged; secondary model B and, for v3, a local-only estimate, reference only), the naive
  prediction, the network snapshot used, the simulator seed and sample count, and the SHA-256 of the
  fit data and of the model code.
- `model/`: the model code (version c-1, frozen from v2 to v6) and the validation script
  (`model/scripts/evaluate_validation.py`, committed before window v2 was run).

## Verifying the hashes

The fit data are published with the Probe.EXE project, complete and unedited. From the project root:

    python scripts/verify_preregistration.py --prereg-dir <path to this repository>

It checks every SHA-256 in the `window-*.json` files: fit data, model code (against `model/` here),
local-module code and data, and the network snapshot row. Result on 2026-09-30: **95 hashes match
exactly and 1 matches a leading block of its file.**

The one prefix match is `results/bradbury/execution-recheck-v1.jsonl`, listed in `window-v2.json`.
That file is append-only. On 2026-09-28 at 20:48 UTC, after `window-v2.json` was committed, a test run
of the per-window pipeline re-read the execution results of window v1 again and appended 13 lines,
identical in transactions and results to the first 13. The hash in `window-v2.json` is that of the
first 13 lines; the hashes in `window-v3.json` onwards are of the whole file. No prediction or result
changes, and the file is published as it is. The script reports this case as a prefix match.
