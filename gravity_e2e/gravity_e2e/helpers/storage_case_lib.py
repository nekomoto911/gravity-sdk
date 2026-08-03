"""
Pure helpers shared by the storage-v2 cluster cases
(storage_v2_baseline / storage_v2_upgrade / storage_v2_fresh_sync).

Everything here is deterministic and unit-tested in
gravity_e2e/tests/unit/test_storage_case_lib.py. The pytest cases keep
orchestration and assertions; this module keeps the derivable facts:

- OnChainHistory / build_anchor_spec: turn the tx receipts recorded while
  building history into the H1 AnchorSpec, covering all six anchor kinds
  (balance, storage, transaction, receipt, logs, block_hash).
- derive_offline_env: encode the on-disk layout that cluster/deploy.sh
  materializes for a node, so the H2 offline db commands look at exactly
  what the node ran with.
- encode_set_call: calldata for AnchorTarget.set(uint256) without going
  through web3 contract ABI codecs (stable across web3 v6/v7).
- assert_history_is_anchorable: positive controls on freshly collected
  anchors — the history must have produced real, distinguishable facts,
  otherwise a later replay would "pass" on trivially empty anchors.
- assert_sf_layout / assert_upgraded_legacy_layout: offline layout probes
  shared by TC4 and TC9.
- classify_changeset_prune / ChangesetPruneEffect: map a prune node's
  on-disk changeset segment coverage onto a named effect (used by TC9's
  sf_prune1 guardrail).

History: extracted from storage_v2_baseline's case-local
storage_baseline_lib.py when storage_v2_upgrade needed the same logic
(BaselineHistory renamed to OnChainHistory).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Sequence, Union

from gravity_e2e.helpers.offline_db import (
    ACCOUNT_CHANGESETS_TABLE,
    STORAGE_CHANGESETS_TABLE,
    OfflineDbEnv,
    SettingsState,
    count_table_entries,
    inspect_changeset_static_files,
    read_storage_settings,
)
from gravity_e2e.helpers.storage_anchors import AnchorSet, AnchorSpec

LOG = logging.getLogger(__name__)

# keccak256("set(uint256)")[:4]; also visible in the tracked
# prague/contracts/Counter.json bytecode (same signature).
SET_SELECTOR = "60fe47b1"

# AnchorTarget.value lives in storage slot 0 (first declared variable).
VALUE_SLOT = 0


@dataclass(frozen=True)
class TxPoint:
    """One confirmed transaction: its hash and inclusion block."""

    tx_hash: str
    block_number: int


@dataclass(frozen=True)
class OnChainHistory:
    """What a case did on-chain, as recorded from receipts.

    Attributes:
        faucet: sender address funding the transfers.
        recipient: fresh account receiving the transfers.
        contract: deployed AnchorTarget address.
        transfers: confirmed faucet->recipient transfers (>= 1 required;
            the recipient balance differs at each inclusion block).
        deploy: the AnchorTarget deployment transaction.
        sets: confirmed AnchorTarget.set() calls (>= 1 required; slot 0
            differs at each inclusion block).
    """

    faucet: str
    recipient: str
    contract: str
    transfers: List[TxPoint]
    deploy: TxPoint
    sets: List[TxPoint]


def encode_set_call(value: int) -> str:
    """Calldata for AnchorTarget.set(value) as a 0x-hex string."""
    if not 0 <= value < 2**256:
        raise ValueError(f"set() argument out of uint256 range: {value}")
    return "0x" + SET_SELECTOR + format(value, "064x")


def build_anchor_spec(history: OnChainHistory) -> AnchorSpec:
    """Build the H1 collection spec from the recorded history.

    Covers all six anchor kinds:
    - balance: recipient at every transfer block (different value each
      time) plus the faucet at the last transfer block;
    - storage: contract slot 0 at every set() block (different value each
      time) — the changeset-backed historical reads these cases exist for;
    - transaction + receipt: every recorded tx hash;
    - block_hash: every block touched by the history;
    - logs: one address-filtered range spanning the contract's lifetime
      (deploy through last set), expected to hold one ValueSet per set().
    """
    if not history.transfers:
        raise ValueError("history has no transfers — balance anchors need one")
    if not history.sets:
        raise ValueError("history has no set() calls — storage anchors need one")

    balances = [(history.recipient, t.block_number) for t in history.transfers]
    balances.append((history.faucet, history.transfers[-1].block_number))

    storage_slots = [
        (history.contract, VALUE_SLOT, s.block_number) for s in history.sets
    ]

    all_points = [*history.transfers, history.deploy, *history.sets]
    tx_hashes: List[str] = []
    for point in all_points:
        if point.tx_hash not in tx_hashes:
            tx_hashes.append(point.tx_hash)

    block_numbers = sorted({point.block_number for point in all_points})

    contract_blocks = [history.deploy.block_number] + [
        s.block_number for s in history.sets
    ]
    log_ranges = [(min(contract_blocks), max(contract_blocks), history.contract)]

    return AnchorSpec(
        balances=balances,
        storage_slots=storage_slots,
        tx_hashes=tx_hashes,
        block_numbers=block_numbers,
        log_ranges=log_ranges,
    )


def assert_history_is_anchorable(
    anchors: AnchorSet,
    history: OnChainHistory,
    transfer_amounts_wei: Sequence[int],
    set_values: Sequence[int],
) -> None:
    """Positive controls on freshly collected anchors.

    The history must have produced real, distinguishable facts — all six
    anchor kinds present, slot 0 holding each set() value at its block,
    the recipient balance accruing transfer by transfer, one event log per
    set() — otherwise a later replay would "pass" on trivially empty
    anchors. ``transfer_amounts_wei`` / ``set_values`` are the values the
    case actually sent, in history order.
    """
    by_kind = {}
    for anchor in anchors.anchors:
        by_kind.setdefault(anchor.kind, []).append(anchor)

    for kind in ("balance", "storage", "transaction", "receipt", "logs", "block_hash"):
        assert by_kind.get(kind), f"no {kind} anchors collected"

    # Storage history: slot 0 at each set() block holds that set's value.
    for point, value in zip(history.sets, set_values):
        anchor = next(
            a
            for a in by_kind["storage"]
            if a.params["block_number"] == point.block_number
        )
        got = int(anchor.expected, 16)
        assert got == value, (
            f"slot 0 at block {point.block_number}: expected {value}, "
            f"collected {got}"
        )

    # Balance history: recipient accrues the transfers cumulatively.
    cumulative = 0
    for point, amount_wei in zip(history.transfers, transfer_amounts_wei):
        cumulative += amount_wei
        anchor = next(
            a
            for a in by_kind["balance"]
            if a.params["address"] == history.recipient.lower()
            and a.params["block_number"] == point.block_number
        )
        assert anchor.expected == cumulative, (
            f"recipient balance at block {point.block_number}: expected "
            f"{cumulative}, collected {anchor.expected}"
        )

    # Log history: one ValueSet event per set() call inside the range.
    (logs_anchor,) = by_kind["logs"]
    assert len(logs_anchor.expected) == len(history.sets), (
        f"expected {len(history.sets)} ValueSet logs in "
        f"{logs_anchor.anchor_id}, collected {len(logs_anchor.expected)}"
    )


def assert_sf_layout(
    env: OfflineDbEnv,
    stage: str,
    *,
    read_settings=read_storage_settings,
    inspect_sf=inspect_changeset_static_files,
    count_entries=count_table_entries,
) -> Dict[str, int]:
    """The static-file layout facts on a STOPPED node's datadir: settings
    ratchet flipped to PRESENT_STATIC_FILES, both changeset kinds present
    as SF segments with .csoff sidecars, both DB tables empty
    (db list --count, exact). Returns the table counts (all zero).

    Shared by storage_v2_upgrade (post-migrate-changesets) and
    storage_v2_fresh_sync (SF-enabled fresh nodes). The probe callables
    are injectable for unit tests only.
    """
    probe = read_settings(env)
    assert probe.error is None, f"[{stage}] {probe.summary()}"
    assert probe.state is SettingsState.PRESENT_STATIC_FILES, (
        f"[{stage}] settings are not PRESENT_STATIC_FILES "
        f"(state={probe.state}) — the SF ratchet must be set.\n"
        + probe.summary()
    )

    layout = inspect_sf(env.datadir, env.static_files_dir)
    assert layout.exists, f"static-files dir missing: {layout.static_files_dir}"
    assert layout.account_segments, (
        f"[{stage}] no account-change-sets static-file segments"
    )
    assert layout.storage_segments, (
        f"[{stage}] no storage-change-sets static-file segments"
    )
    assert layout.account_sidecars, (
        f"[{stage}] no account-change-sets .csoff sidecars"
    )
    assert layout.storage_sidecars, (
        f"[{stage}] no storage-change-sets .csoff sidecars"
    )
    LOG.info(
        "[%s] SF layout: %d account segs, %d storage segs, %d+%d .csoff",
        stage,
        len(layout.account_segments),
        len(layout.storage_segments),
        len(layout.account_sidecars),
        len(layout.storage_sidecars),
    )

    counts: Dict[str, int] = {}
    for table in (ACCOUNT_CHANGESETS_TABLE, STORAGE_CHANGESETS_TABLE):
        count = count_entries(env, table)
        assert count.error is None, f"[{stage}] {count.summary()}"
        assert count.count == 0, (
            f"[{stage}] {table} still has {count.count} entries under the "
            f"SF layout — the tables must be empty.\n" + count.summary()
        )
        counts[table] = count.count
    LOG.info("[%s] both changeset tables empty (db list --count == 0)", stage)
    return counts


def assert_upgraded_legacy_layout(
    env: OfflineDbEnv,
    stage: str,
    *,
    read_settings=read_storage_settings,
    inspect_sf=inspect_changeset_static_files,
    count_entries=count_table_entries,
) -> Dict[str, int]:
    """The upgraded-legacy layout facts on a STOPPED node's datadir
    (a v1.7.5-born datadir now run by v2.3.0): settings MISSING (only a
    fresh v2.3.0 init_genesis writes one — TC2's criterion), no changeset
    segments/sidecars, both changeset tables populated (positive control).
    Returns the table counts (all positive).
    """
    probe = read_settings(env)
    assert probe.error is None, f"[{stage}] {probe.summary()}"
    assert probe.state is SettingsState.MISSING, (
        f"[{stage}] upgraded datadir has a gravity_storage_settings entry "
        f"(state={probe.state}) — the upgrade path must not write settings; "
        "only fresh init_genesis does. Product bug unless proven "
        "otherwise.\n" + probe.summary()
    )
    LOG.info("[%s] storage settings: MISSING (as required)", stage)

    layout = inspect_sf(env.datadir, env.static_files_dir)
    assert layout.exists, f"static-files dir missing: {layout.static_files_dir}"
    assert not layout.has_segment_files, (
        f"[{stage}] upgraded legacy node has changeset segment files: "
        f"{layout.account_segments + layout.storage_segments}"
    )
    assert not layout.has_sidecar_files, (
        f"[{stage}] upgraded legacy node has .csoff sidecars: "
        f"{layout.account_sidecars + layout.storage_sidecars}"
    )

    counts: Dict[str, int] = {}
    for table in (ACCOUNT_CHANGESETS_TABLE, STORAGE_CHANGESETS_TABLE):
        count = count_entries(env, table)
        assert count.error is None, f"[{stage}] {count.summary()}"
        assert count.count > 0, (
            f"[{stage}] {table} is empty on an upgraded legacy-layout node:\n"
            + count.summary()
        )
        counts[table] = count.count
        LOG.info("[%s] %s entries: %d", stage, table, count.count)
    return counts


class ChangesetPruneEffect(Enum):
    """What a prune node's on-disk changeset segment coverage reveals about
    account/storage-history pruning under the storage-v2 layout.

    Distance-based history pruning keeps the last ``distance`` blocks and
    removes everything older, so on a FULLY working prune the segments'
    lowest block advances to about ``tip - distance``. These outcomes map
    the measured lowest block onto the correctness rule so the case asserts
    on a named effect instead of re-deriving the arithmetic inline.

    Disk-only caveat (2026-07-23 empirical, design doc §12.1): the on-disk
    segment lowest block alone CANNOT tell logical pruning apart from
    physical reclamation. The single-node empirical run proved that the
    merge-v2.3.0 binary's LOGICAL prune works — the AccountHistory /
    StorageHistory prune checkpoint advances to ``tip - distance`` and
    historical reads below it correctly return ``StateAtBlockPruned`` — while
    the static-file changeset segments are NEVER physically reclaimed (the
    missing ``prune_static_files``, doc §11). So a lowest block stuck at 0
    while ``tip > distance`` is the RECLAMATION LEAK, not a no-op; the
    ``StateAtBlockPruned`` online probe (or a PruneCheckpoints read) is what
    confirms the logical half. See :attr:`NOT_RECLAIMED`.
    """

    NOT_YET_EXPECTED = "not_yet_expected"
    """``tip <= distance`` and segments still reach genesis: a lowest block
    of 0 is correct here and proves nothing either way."""

    PRUNED = "pruned"
    """Lowest block advanced into ``(0, tip - distance + tolerance]`` — the
    static-file changesets were front-truncated as configured (BOTH the
    logical prune AND physical reclamation happened)."""

    NOT_RECLAIMED = "not_reclaimed"
    """``tip > distance`` yet segments still start at block 0 — the static
    files were never physically reclaimed. Per the empirical run (doc §12.1)
    the logical prune HAS advanced (read boundary at ``tip - distance``,
    below-horizon reads return ``StateAtBlockPruned``), so this is the
    storage-v2 static-file RECLAMATION LEAK (missing ``prune_static_files``,
    merge-gap doc §11) — disk grows unbounded. NOTE: a hypothetical true
    no-op (checkpoint never advanced either) has the SAME disk signature;
    the online ``StateAtBlockPruned`` probe distinguishes them. Supersedes
    the earlier ``NO_OP`` label, which wrongly implied nothing was pruned."""

    OVER_PRUNED = "over_pruned"
    """Lowest block advanced past ``tip - distance + tolerance`` (or advanced
    at all while ``tip <= distance``) — blocks inside the retained window
    were deleted, so history that should still be queryable is gone."""


def classify_changeset_prune(
    lowest_block: int,
    tip_block: int,
    distance: int,
    *,
    tolerance_blocks: int = 0,
) -> ChangesetPruneEffect:
    """Map a changeset segment's lowest block onto a :class:`ChangesetPruneEffect`.

    ``lowest_block`` is the start block of the oldest changeset segment
    (offline_db.ChangesetStaticFiles.lowest_account_block /
    lowest_storage_block; the caller checks segments exist first — a prune
    node that synced must have them). ``tip_block`` is the node's current
    height, ``distance`` the configured
    ``prune.{account,storage}history.distance``. ``tolerance_blocks`` absorbs
    the pruner's batch/lag jitter around the ``tip - distance`` boundary
    (the retained set is roughly ``[tip - distance, tip]``); keep it small so
    genuine over-pruning still surfaces. Pure arithmetic on the disk fact —
    the case owns the pass/fail policy (assert ``PRUNED`` to prove full
    pruning works, or assert ``!= OVER_PRUNED`` to prove no corruption and
    record ``NOT_RECLAIMED`` as the static-file reclamation leak, doc §11).
    Disk coverage only: ``NOT_RECLAIMED`` is the leak per the §12.1 empirical
    run — a true no-op shares the signature and needs the online
    ``StateAtBlockPruned`` probe to tell apart (see the enum's caveat).
    """
    boundary = tip_block - distance
    if lowest_block <= 0:
        # Segments still reach genesis.
        return (
            ChangesetPruneEffect.NOT_YET_EXPECTED
            if boundary <= 0
            else ChangesetPruneEffect.NOT_RECLAIMED
        )
    # Segments have been front-truncated to some positive block.
    if boundary <= 0 or lowest_block > boundary + tolerance_blocks:
        return ChangesetPruneEffect.OVER_PRUNED
    return ChangesetPruneEffect.PRUNED


def derive_offline_env(
    base_dir: Union[str, Path], node_id: str = "node1"
) -> OfflineDbEnv:
    """OfflineDbEnv for a node deployed by cluster/deploy.sh.

    Layout facts (all from cluster/deploy.sh and
    cluster/templates/reth_config.json.tpl):
    - node dir: <base_dir>/<node_id> with bin/ config/ data/ logs/ script/;
    - node binary hardlinked to <node dir>/bin/gravity_node — using it here
      guarantees the offline commands run the exact binary the node ran
      (in storage_v2_upgrade: the post-upgrade binary after the swap);
    - STORAGE_DIR = <node dir>/data, reth datadir = ${STORAGE_DIR}/reth;
    - --datadir.static-files is ALSO ${STORAGE_DIR}/reth (the datadir root,
      not the reth default <datadir>/static_files), so it must be passed
      explicitly or the offline commands would look in the wrong place;
    - chain spec: the node runs --chain <base_dir>/genesis.json (deploy.sh
      copies the suite genesis there).
    """
    base = Path(base_dir)
    datadir = base / node_id / "data" / "reth"
    return OfflineDbEnv(
        binary=base / node_id / "bin" / "gravity_node",
        datadir=datadir,
        chain=base / "genesis.json",
        static_files_dir=datadir,
    )
