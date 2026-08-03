# storage_v2_fresh_sync (storage-v2 TC9 + prune guardrail)

SF-enabled **fresh nodes sync from block 0** on a rolling-upgraded
network, an **SF validator votes**, and an **SF pfn with reth's
production `--full` prune profile** survives the config path — with the
SF × non-SF upstream/downstream matrix fully covered. Design doc:
`_local/drafts/storage-v2-e2e/sf-fresh-sync-design.md` (repo-external).

## Topology (10 nodes)

| node | role | layout | upstream | purpose |
|---|---|---|---|---|
| node1, node2 | genesis validator | legacy (upgraded) | — | consensus core, born v1.7.5 |
| vfn1 | vfn | legacy (upgraded) | node1 | control + sf_pfn2/pfn1 upstream |
| pfn1 | pfn | legacy (upgraded) | vfn1 | **tx entry** + control |
| sf_val1 | validator | **SF** | — (joins) | G3: SF validator votes |
| sf_vfn1 | vfn | **SF** | node2 | SF vfn ← legacy validator |
| sf_vfn2 | vfn | **SF** | sf_val1 | SF vfn ← SF validator |
| sf_pfn1 | pfn | **SF** | sf_vfn1 | SF pfn ← SF vfn (archive twin of sf_prune1) |
| sf_pfn2 | pfn | **SF** | vfn1 | SF pfn ← legacy vfn |
| sf_prune1 | pfn | **SF + --full** | sf_vfn1 | config-path prune guardrail |

## Phases

1. legacy core live on v1.7.5; SF nodes stopped + data-wiped back to
   fresh (the runner auto-starts everything);
2. v1.7.5-era history + anchor batch A via pfn1; cross ≥1 epoch;
3. rolling upgrade → merge v2.3.0 (sender paused per swap window — with
   2 validators every swap freezes the chain, expected), Alpha crossing,
   anchor batch B;
4. sf_vfn1 / sf_pfn2 / sf_pfn1 from-0 sync (SF-enable hook at first
   start);
5. offline layout probes: SF mirror of TC1 (settings
   PRESENT_STATIC_FILES, segments+sidecars, tables == 0) vs upgraded
   legacy controls (settings MISSING, no SF files, tables > 0);
6. A+B anchor replays on the SF fullnodes (v1.7.5 history served by the
   SF read path — the core from-0 evidence);
7. sf_val1 from-0 sync → governance-enabled permissionless join with
   **equal stake** (2 ETH = genesis stake → 3-validator quorum = ALL
   votes) → L1 active, L2 healthy epoch;
8. sf_vfn2 (SF ← SF validator) closes the matrix;
9. **prune guardrail** (`sf_prune1`): born-SF from-0 sync under
   production `--full --prune.transactionlookup.distance 10064` → no
   pruner/persistence crash → offline changeset classify
   `NOT_YET_EXPECTED` (tip ≪ 10064 floor) → online reads match archive
   twin `sf_pfn1` (no wrong values);
10. **L3 necessity probe**: stop sf_val1 → chain MUST freeze → restart →
    chain resumes, still active (its offline SF probe rides the window);
11. load floors, final A+B replay on all non-prune nodes, log scan
    (sf_prune1 is exempt from whole-history anchor replay — `--full`
    drops old receipts/logs).

## SF enable

Both forms are executable; they yield the same on-disk product:

| mode | form | how | when to use |
|---|---|---|---|
| `"flag"` | B (recommended) | phase 1 injects bare `--storage.v2` into each SF node's `reth_config.json`; fresh init is born on SF (genesis alloc as entity rows) | greth with feat/sf-fresh-init (`--storage.v2` → genesis init) |
| `"migrate"` | D (compatibility) | first start → stable-runtime wait → stop → `db migrate-changesets` → restart | any binary with the #391 preflight fix; also the **code default** when `[sf].mode` is omitted |

`test_params.toml.example` pins `mode = "flag"`. Override at run time
with `GRAVITY_SF_MODE=migrate|flag`.

## Fund flow (three accounts — do not "just use the faucet")

| account | role | why |
|---|---|---|
| genesis faucet | **gas-only** governance wallet (owner + pool[0] voter) | `[faucet_init]` **sweeps** its on-chain balance at suite init: `cluster/faucet.sh:44-56` writes `eth_balance` into the bench config, but gravity_bench overrides it — `main.rs:384-411` scales the cascade up to (on-chain − 1%) and `src/txn_plan/constructor/faucet.rs:64-104` splits it all among the bench accounts. Leftover ≈ 0.5 ETH (live attempt3 died funding 1 ETH from 0.488). |
| bench[0] (accounts.csv) | sf_val1's join/staking signer | `manager._ensure_evm_account` assigns csv rows to VALIDATOR-role nodes in order; the 2 ETH equal-power stake comes from its init balance. |
| bench[1] (accounts.csv) | the **bank**: all foreground value transfers (A/B history, bg-sender funding) | disjoint from the background TxSender's dedicated account (funded once by the bank in phase 2 — nonce-race lock from live attempt2). |

Phase 1 fail-fasts when the post-sweep faucet gas budget or the bank
balance is missing. Unit locks: `TxSender` may never be fed
`cluster.faucet`; `TransactionBuilder` may never be fed the faucet;
`[faucet_init] num_accounts` must equal the bench partition.

## Run

```
cp test_params.toml.example test_params.toml   # edit binaries if needed
python render_config.py                        # re-render before EVERY run
cd ../.. && python3 gravity_e2e/runner.py --force-init storage_v2_fresh_sync
```

Old binary: v1.7.5 (see storage_v2_upgrade/README for the release-asset
channel and the source-build fallback). New binary: `[sf_source]` and
`GRAVITY_NEW_BINARY` must point at the same merge v2.3.0 build. Without
a rendered cluster.toml the runner skips the suite (binary opt-in,
CI-neutral).

## Duration & the from-0 sync budget

From-0 sync runs against the **live, loaded chain** with the SF nodes'
sync path **completely untouched** — the original design semantics.
What makes that feasible: the case slows the CHAIN, an environment
parameter e2e rightfully owns. The chain's pace comes from the
proposer's unconditional per-round sleep (`round_manager.rs:389-396`,
`APTOS_PROPOSER_SLEEP_MS`, default 200 ms + ~60 ms round overhead = the
measured ~260 ms/block); the case-local `reth_config.json.tpl`
(validator-role template, auto-picked by the runner) bakes
`APTOS_PROPOSER_SLEEP_MS=1000` into every validator's env — node1,
node2 and sf_val1 alike — pacing production to **~0.94 blk/s under any
load** (the sleep precedes the payload pull each round; load makes
blocks bigger, never faster). Against that, even the sync driver's
observed ~4.4 blk/s ceiling converges at ≥3 blk/s net. Note:
`quorum_store_poll_time_ms` is NOT a usable knob in this fork —
`quorum_store_client.rs:124` hardcodes `done = true`, making the config
dead code.

All rate-derived constants (gammaBlock, stall window, budget floor,
expected gaps) flow from the central chain-rate block in `sf_lib.py`;
re-pacing the chain is a one-line change plus unit-locked derivations.
Catch-up waits stay progress-based (`helpers/catchup.py`), never fixed
deadlines.

### Prune guardrail scope

`sf_prune1` asserts the **config path** is safe under the production
shape (`--full` + txlookup distance at reth's
`MINIMUM_UNWIND_SAFE_DISTANCE = 10064`). This harness's chain only
reaches ~1800 blocks, well under that floor, so:

- changeset pruning correctly stays `NOT_YET_EXPECTED` (lowest segment
  block 0 is correct while tip < floor);
- an explicit sub-floor `accounthistory`/`storagehistory` distance would
  **crash** the persistence service (live 2026-07-23 finding) — the
  phase scans reth logs for those markers;
- the static-file reclamation leak (`NOT_RECLAIMED` once tip > 10064)
  and below-horizon `StateAtBlockPruned` reads are covered by unit tests
  + the single-node empirical run (design doc §11/§12.1), not this short
  chain.

### Open question for greth (from the attempt5-7 investigations)

**Why does fast sync net only ~1 block per sync round?** The fullnode
sync driver polls on a 200 ms tick and each round advances ~1 block
(≈5 blk/s hard ceiling), while burst replay demonstrably executes at
~4 ms/block — and a 10x tick experiment scaled throughput 5.4x
(4.4 → 23.6 blk/s), proving the driver, not execution, is the limiter.
A fast-forward sync should advance in BATCHES and outrun production by
orders of magnitude; is the per-round net progress capped by chunk
size/windowing, or is this a driver logic defect? Data:
`tc9-catchup-freeze-investigation.md` + live-run5/6/7 logs.

Historical note: attempts 6-7 briefly carried `quiet_chain`/`frozen_tip`
machinery (pause the load / halt node1 to freeze the tip) and a brief
sync-tick injection — all retired: the first two were built on refuted
capacity readings, the last modified the nodes under test. Git history
and the investigation archive keep them; every realism concession
recorded then is withdrawn.

The Alpha schedule stays compressed to keep the chain young at phase 4.
Expected end-to-end: **~55-80 min** (at ~1 blk/s the phase-4 gap is only
~1400 blocks ⇒ minutes of syncing; the rolling-upgrade front section,
epoch waits, the prune guardrail and the L3 probe dominate).
