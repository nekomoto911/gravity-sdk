"""
Pure helpers for the storage_v2_fresh_sync case (TC9 + prune guardrail).

Deterministic and unit-tested in
gravity_e2e/tests/unit/test_storage_fresh_sync_case.py (loaded by file
path; this directory is not a package). The pytest case keeps
orchestration and assertions; this module keeps the derivable facts:

- topology constants (legacy / SF node sets, pinned upstreams for the
  SF x non-SF matrix, plus sf_prune1 as the SF prune-profile pfn);
- resolve_sf_mode: [sf] mode knob + GRAVITY_SF_MODE override
  ("flag" = form B primary via --storage.v2; "migrate" = form D);
- inject_sf_v2_flag_reth_args / inject_prune_reth_args: pure rewrites of
  a node's reth_config.json dict (the case owns the file I/O);
- sf_start_extra_args: CLI encoding of the form-B flag (clap contract
  lock for unit tests; production injects via reth_config, not Node.start).
"""

from __future__ import annotations

from typing import Mapping, Optional, Sequence, Tuple

# Legacy core: born on v1.7.5, rolling-upgraded, stays on the legacy
# layout (upgraded datadirs never flip — TC2's criterion).
LEGACY_NODE_IDS: Tuple[str, ...] = ("node1", "node2", "vfn1", "pfn1")

# SF nodes: born on merge v2.3.0, SF layout from (effectively) block 0.
SF_NODE_IDS: Tuple[str, ...] = (
    "sf_val1",
    "sf_vfn1",
    "sf_vfn2",
    "sf_pfn1",
    "sf_pfn2",
    "sf_prune1",  # SF pfn that ALSO runs reth's full-node prune profile
)

# Every fullnode's pinned upstream (static seeds + discovery none in
# cluster.toml.tpl) — the coverage matrix's edges. Validators discover
# each other via consensus and have no pinned upstream.
PINNED_UPSTREAMS: Mapping[str, str] = {
    "vfn1": "node1",      # legacy vfn  <- legacy validator
    "pfn1": "vfn1",       # legacy pfn  <- legacy vfn (the tx entry)
    "sf_vfn1": "node2",   # SF vfn      <- legacy validator
    "sf_vfn2": "sf_val1", # SF vfn      <- SF validator
    "sf_pfn1": "sf_vfn1", # SF pfn      <- SF vfn
    "sf_pfn2": "vfn1",    # SF pfn      <- legacy vfn
    "sf_prune1": "sf_vfn1", # SF prune pfn <- SF vfn (sf_pfn1 is its archive twin)
}

# SF fullnodes whose upstream exists from phase 1 (first batch); sf_pfn1
# must wait for sf_vfn1, sf_vfn2 must wait for sf_val1.
SF_FIRST_BATCH: Tuple[str, ...] = ("sf_vfn1", "sf_pfn2")

# All txs enter here (the production pfn -> vfn -> validator path).
TX_ENTRY_NODE = "pfn1"

# Cross-checked against genesis.toml.tpl by the unit tests: the case's
# waits must match the rendered epoch, and the join stake must equal the
# genesis validators' stake (the L3 all-votes-quorum prerequisite).
EPOCH_INTERVAL_S = 120
GENESIS_STAKE_WEI = 2 * 10**18
JOIN_STAKE_ETH = "2.0"

# The three-account fund-flow model (test module constraint (4)): the
# [faucet_init] cascade sweeps the faucet's on-chain balance at suite
# init, so the faucet is a gas-only governance wallet afterwards;
# bench[0] signs sf_val1's join/staking (manager._ensure_evm_account
# assigns accounts.csv rows to VALIDATOR-role nodes in order); bench[1]
# is the bank for every foreground value transfer. Must match
# [faucet_init] num_accounts (unit-test enforced).
JOIN_BENCH_INDEX = 0
BANK_BENCH_INDEX = 1
FAUCET_INIT_NUM_ACCOUNTS = 2

# ── Chain-rate assumptions: the SINGLE source of truth ──────────────────
#
# TC9 slows the CHAIN's block production instead of touching the nodes
# under test: deep sync is tick-limited on the sync side (~1 block per
# 200 ms sync round ⇒ ~4.4 blk/s ceiling — possibly a driver defect, see
# the README's greth question) while the chain produces at ~3.8-3.9
# blk/s, leaving near-zero net convergence. Slowing production to ~1
# blk/s is an ENVIRONMENT parameter (e2e owns the chain's pace) and
# gives even the untouched sync driver an overwhelming advantage.
#
# The pacing knob, verified in source:
# - round_manager.rs:389-396 `process_new_round_event`: the proposer
#   sleeps APTOS_PROPOSER_SLEEP_MS (env, safe-parsed, default 200 ms)
#   UNCONDITIONALLY at the top of every new round — before the payload
#   pull — so it floors the per-block interval regardless of load (load
#   only makes blocks bigger, never faster). Measured: 200 ms default +
#   ~60 ms round overhead = the observed ~260 ms/block (~3.8-3.9 blk/s).
# - quorum_store_poll_time_ms is NOT a pacing knob in this fork:
#   quorum_store_client.rs:124 hardcodes `let done = true;`, so the poll
#   loop returns after a single pull and the `!done` wait branch (:138)
#   is unreachable — the config value is dead code.
#
# Injection: the case-local reth_config.json.tpl (validator-role
# template, auto-picked by runner.py's RETH_CONFIG_TPL override) bakes
# APTOS_PROPOSER_SLEEP_MS into .env_vars, so ALL validator-role nodes —
# node1, node2 AND sf_val1 — carry it from first boot (chain speed is a
# network property, and every validator rotates as proposer).
# vfn/pfn templates stay default (fullnodes never propose).
#
# ALL rate-derived constants below flow from these; a future re-pacing
# is a one-line change plus the unit-locked derivations.
PROPOSER_SLEEP_MS = 1000
ROUND_OVERHEAD_MS = 60  # measured: 260 ms observed at the 200 ms default
CHAIN_BLOCK_RATE_BPS = 1000.0 / (PROPOSER_SLEEP_MS + ROUND_OVERHEAD_MS)  # ~0.94
# The sync driver's own ceiling (~1 block per 200 ms sync round;
# measured 4.3-4.5 blk/s across attempts 5-7), floored conservatively.
SYNC_RATE_FLOOR_BPS = 4.0
# Net convergence floor for the catch-up budget: sync floor minus chain
# production (~3.06 blk/s — pending live calibration via the per-minute
# net_rate diagnostics).
NET_CATCHUP_FLOOR_BPS = SYNC_RATE_FLOOR_BPS - CHAIN_BLOCK_RATE_BPS
# Epoch geometry at the slowed rate (epoch_interval 120 s x ~0.94 blk/s).
BLOCKS_PER_EPOCH = int(EPOCH_INTERVAL_S * CHAIN_BLOCK_RATE_BPS)  # ~113
# Stall window for the progress-based waiter: one epoch-staircase step
# now takes <= BLOCKS_PER_EPOCH / SYNC_RATE_FLOOR_BPS (~28 s); 3x that
# for jitter, but never below two full epochs — epoch transitions (DKG
# etc.) can legitimately flatten progress around boundaries.
CATCHUP_STALL_WINDOW_S = max(
    2 * EPOCH_INTERVAL_S, 3 * BLOCKS_PER_EPOCH / SYNC_RATE_FLOOR_BPS
)
# gammaBlock for test_params.toml.example: must activate only after the
# fleet finishes upgrading (~render+19 min; the chain starts at
# ~render+5 min, so ~14 min of chain time ≈ 790 blocks at the slowed
# rate) yet be crossed comfortably within the run — 1200 is crossed at
# ~21 min of chain time (~render+26 min). Unit-locked against the
# example file.
GAMMA_BLOCK = 1200

# First-stop safety for the D-form SF enable (attempt8): a ~12s-old
# gravity_node IGNORED a direct SIGTERM for 109s (early-init
# graceful-shutdown windows are a suspected product defect, under a
# separate investigation), while the earlier 1s-age "successful" first
# stops most likely predated signal-handler installation entirely
# (default disposition = instant death) — i.e. they never exercised
# graceful shutdown at all. TC1/TC2/TC3 prove minute-age nodes stop
# gracefully and reliably, so the D-form first stop waits for a
# demonstrably stable runtime: a hard minimum uptime, then "synced
# enough OR waited long enough" — whichever comes first.
FIRST_STOP_MIN_UPTIME_S = 30
FIRST_STOP_READY_HEIGHT = 50
FIRST_STOP_MAX_WAIT_S = 60


def first_stop_ready(uptime_s: float, height: int) -> bool:
    """Whether an SF node's first (pre-migration) stop is safe to issue:
    never before FIRST_STOP_MIN_UPTIME_S; after that, having synced
    FIRST_STOP_READY_HEIGHT blocks proves a live runtime, and
    FIRST_STOP_MAX_WAIT_S caps the wait when sync is slow to start."""
    if uptime_s < FIRST_STOP_MIN_UPTIME_S:
        return False
    return (
        height >= FIRST_STOP_READY_HEIGHT or uptime_s >= FIRST_STOP_MAX_WAIT_S
    )


# Both SF-enable forms are live:
# - "flag" (form B, PRIMARY): greth feat/sf-fresh-init
#   (b709e71df8 + 09097fbab3) wires --storage.v2 into genesis init —
#   default false; when passed, a FRESH datadir is born with SF settings
#   and the genesis alloc written as ENTITY rows in the changeset
#   segments (the design-doc Q6 landmine fixed; the product is
#   isomorphic with a migrate-changesets result). Initialized datadirs
#   ignore the flag entirely (persisted settings win), so carrying it
#   across restarts is harmless.
# - "migrate" (form D, compatibility): fresh init -> stable-runtime wait
#   -> stop -> db migrate-changesets (#391 fix) -> restart.
# Both forms are executable. Code default when [sf].mode is omitted is
# "migrate" (works on any binary with the #391 preflight fix). The
# recommended / example pin is "flag" (requires feat/sf-fresh-init).
SF_MODES: Tuple[str, ...] = ("migrate", "flag")
DEFAULT_SF_MODE = "migrate"

# The reth_args entry the flag mode injects into an SF node's
# config/reth_config.json: an EMPTY value makes the generated
# script/start.sh emit the bare `--storage.v2` (deploy.sh's start-script
# heredocs: `[ -z "$value" ] -> reth_args_array+=( "--${key}" )`), and
# clap parses the bare flag as true (storage.rs StorageArgs:
# `num_args = 0..=1, default_missing_value = "true"` on the
# feat/sf-fresh-init branch).
SF_FLAG_RETH_ARG = "storage.v2"


def resolve_sf_mode(params: Mapping, environ: Mapping[str, str]) -> str:
    """The effective SF-enable mode: GRAVITY_SF_MODE overrides the params
    [sf].mode; default DEFAULT_SF_MODE ("migrate"). Raises on unknown modes."""
    mode = environ.get("GRAVITY_SF_MODE") or params.get("sf", {}).get(
        "mode", DEFAULT_SF_MODE
    )
    if mode not in SF_MODES:
        raise ValueError(f"[sf] mode must be one of {SF_MODES}, got {mode!r}")
    return mode


def sf_start_extra_args(mode: str) -> Sequence[str]:
    """CLI encoding of the form-B ``--storage.v2`` flag for the given mode.

    Production form B injects the flag into each SF node's
    ``config/reth_config.json`` (see :func:`inject_sf_v2_flag_reth_args`)
    rather than via ``Node.start`` extra args — deploy.sh's start script
    materializes bare reth_args the same way. This helper remains as the
    pure clap-surface encoding used by unit tests to lock
    ``bare flag == true``. Form D needs no extra args (offline migrate).
    """
    if mode == "migrate":
        return ()
    if mode == "flag":
        return (f"--{SF_FLAG_RETH_ARG}",)
    raise ValueError(f"unknown sf mode {mode!r}")


def inject_sf_v2_flag_reth_args(reth_config: Mapping) -> dict:
    """A copy of a node's reth_config.json dict with the --storage.v2
    opt-in injected into .reth_args (empty value -> the generated
    start.sh emits the bare flag; see SF_FLAG_RETH_ARG). Other entries
    preserved; pure — the case owns the file I/O."""
    config = dict(reth_config)
    reth_args = dict(config.get("reth_args") or {})
    reth_args[SF_FLAG_RETH_ARG] = ""
    config["reth_args"] = reth_args
    return config


# ── Prune-node knobs (storage-v2 + --full config-path smoke test) ──
#
# sf_prune1 is an SF fullnode (new layout, from block 0) that ALSO runs the
# production prune shape: `--full --prune.transactionlookup.distance 10064`.
# It is a CONFIG-PATH smoke test, not a "watch pruning happen" test.
#
# Why we can't scale the distance down (runtime fact, found live 2026-07-23):
# reth caps the AccountHistory/StorageHistory segments at a hard floor
# MINIMUM_UNWIND_SAFE_DISTANCE = 32*2 + 10_000 = 10064 (greth
# crates/prune/types/src/target.rs:12). prune_target_block_with_min
# (mode.rs:57-68) accepts Distance(d) only when d > tip (nothing-to-prune) OR
# d >= min_blocks; a sub-min explicit distance like 128 hits neither branch
# and falls to `_ => Err(PruneSegmentError::Configuration(segment))`, which
# CRASHES the persistence service ("Persistence service failed
# err=PrunerError(PruneSegment(Configuration(AccountHistory)))" +
# "persistence channel closed", ~16 s after start). So an explicit
# accounthistory/storagehistory distance below 10064 is not "scaled down",
# it is a hard misconfiguration. This harness's chain only reaches ~1800
# blocks, well under 10064, so:
#   - <10064 explicit  -> Configuration crash;
#   - >=10064 explicit -> Distance(d) > tip -> None -> never triggers here;
# i.e. NO distance in this harness can OBSERVE changeset pruning. We
# therefore assert the config PATH is safe (no crash, no corruption) and that
# changeset pruning correctly stays in NOT_YET_EXPECTED (tip < floor).
#
# `--full` itself is safe: for AccountHistory/StorageHistory it behaves like
# Distance(min_blocks=10064) and returns None while tip < 10064 (mode.rs:61),
# so it never crashes. Under --full, receipts (min_blocks 64) DO prune
# (tip-64) and senderrecovery/txlookup (min_blocks 0) prune to tip; the
# explicit txlookup distance 10064 is > tip so tx-lookup stays unpruned here.
PRUNE_NODE_ID = "sf_prune1"

# The AccountHistory/StorageHistory hard floor (greth target.rs:12). Any
# explicit distance below this for those segments crashes the pruner; used
# here as the distance the changeset classifier reasons against (tip < floor
# => NOT_YET_EXPECTED).
MINIMUM_UNWIND_SAFE_DISTANCE = 32 * 2 + 10_000  # 10064

# reth_args = the user's production prune shape. `full` uses an empty value so
# deploy.sh's start-script emits the bare `--full` (same bare-flag encoding as
# SF_FLAG_RETH_ARG); the distance key renders as
# `--prune.transactionlookup.distance 10064`. Deliberately NO explicit
# accounthistory/storagehistory distance — those crash below 10064 (see above)
# and --full handles them safely.
PRUNE_RETH_ARGS: Mapping[str, object] = {
    "full": "",
    "prune.transactionlookup.distance": MINIMUM_UNWIND_SAFE_DISTANCE,
}


def inject_prune_reth_args(reth_config: Mapping) -> dict:
    """A copy of a node's reth_config.json dict with the full-node prune
    profile merged into .reth_args (see PRUNE_RETH_ARGS). Coexists with the
    --storage.v2 opt-in (both are plain reth_args entries); pure — the case
    owns the file I/O. Other entries preserved."""
    config = dict(reth_config)
    reth_args = dict(config.get("reth_args") or {})
    reth_args.update(PRUNE_RETH_ARGS)
    config["reth_args"] = reth_args
    return config


def wipe_targets(node_data_dir: str, node_logs: Optional[Sequence[str]] = None):
    """What must disappear for a runner-started node to become fresh
    again: the whole data dir (reth datadir + consensus/quorumstore DBs).
    Returned as a list so the caller (which owns the fs side effects) can
    log exactly what it removes. Node identity/config/binary live outside
    data/ and must survive."""
    targets = [node_data_dir]
    targets.extend(node_logs or ())
    return targets
