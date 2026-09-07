"""Roll four validators onto TestnetOwnerFix; assert pendingOwner after activation."""

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import time

import pytest
from web3 import Web3

from gravity_e2e.cluster.manager import Cluster
from gravity_e2e.utils.hardfork import (
    block_timestamp,
    wait_for_activation_block,
)


LOG = logging.getLogger(__name__)
SUITE_DIR = Path(__file__).resolve().parent
METADATA_FILE = "testnet_owner_fix_rolling_metadata.json"
SUMMARY_FILE = "testnet_owner_fix_rolling_summary.json"
CONFIRMATION_BLOCKS = 8
DEFAULT_MIN_SECONDS_PER_REMAINING_NODE = 120
DEFAULT_BLOCK_WAIT_TIMEOUT_SECONDS = 15 * 60
RECONFIGURATION_ADDRESS = Web3.to_checksum_address(
    "0x00000000000000000000000000000001625F2003"
)
SEL_CURRENT_EPOCH = Web3.keccak(text="currentEpoch()")[:4]
SEL_REMAINING_TIME = Web3.keccak(text="getRemainingTimeSeconds()")[:4]
SEL_OWNER = Web3.keccak(text="owner()")[:4]
SEL_PENDING_OWNER = Web3.keccak(text="pendingOwner()")[:4]
MIN_ROLLING_EPOCH_HEADROOM_SECONDS = 10 * 60


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _metadata() -> dict:
    return json.loads((SUITE_DIR / "artifacts" / METADATA_FILE).read_text())


def _write_summary(payload: dict) -> None:
    path = SUITE_DIR / "artifacts" / SUMMARY_FILE
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _node_binary(cluster: Cluster, node_id: str) -> Path:
    return cluster.base_dir / node_id / "bin" / "gravity_node"


def _running_binary_hash(cluster: Cluster, node_id: str) -> str:
    node = cluster.get_node(node_id)
    assert node is not None and node.pid_file.exists()
    pid = int(node.pid_file.read_text().strip())
    return _sha256(Path(f"/proc/{pid}/exe"))


def _assert_binary_matrix(
    cluster: Cluster, upgraded: set[str], old_hash: str, new_hash: str
) -> None:
    for node_id in sorted(cluster.nodes):
        expected = new_hash if node_id in upgraded else old_hash
        assert _sha256(_node_binary(cluster, node_id)) == expected
        assert _running_binary_hash(cluster, node_id) == expected


def _replace_binary(source: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.rolling.tmp")
    temporary.unlink(missing_ok=True)
    shutil.copy2(source, temporary)
    temporary.chmod(0o755)
    os.replace(temporary, destination)


async def _wait_for_block(
    node_id: str, w3: Web3, target: int, timeout: int | None = None
) -> None:
    if timeout is None:
        timeout = int(
            os.environ.get(
                "OWNER_FIX_ROLLING_BLOCK_TIMEOUT_SECONDS",
                str(DEFAULT_BLOCK_WAIT_TIMEOUT_SECONDS),
            )
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if w3.eth.block_number >= target:
                return
        except Exception:
            pass
        await asyncio.sleep(1)
    last_height = None
    try:
        last_height = w3.eth.block_number
    except Exception:
        pass
    raise TimeoutError(
        f"{node_id} did not reach block {target} within {timeout}s; "
        f"last height was {last_height}"
    )


async def _canonical_checkpoint(cluster: Cluster) -> dict:
    heights = {
        node_id: node.w3.eth.block_number
        for node_id, node in cluster.nodes.items()
    }
    target = max(1, min(heights.values()) - 2)
    hashes = {
        node_id: Web3.to_hex(node.w3.eth.get_block(target)["hash"])
        for node_id, node in cluster.nodes.items()
    }
    assert len(set(hashes.values())) == 1, hashes
    return {"block": target, "blockHash": next(iter(hashes.values()))}


def _uint_call(w3: Web3, selector: bytes) -> int:
    return int.from_bytes(
        w3.eth.call({"to": RECONFIGURATION_ADDRESS, "data": selector}),
        "big",
    )


def _address_call(w3: Web3, to: str, selector: bytes) -> str:
    raw = w3.eth.call({"to": Web3.to_checksum_address(to), "data": selector})
    return Web3.to_checksum_address("0x" + raw.hex()[-40:])


def _epoch_snapshot(w3: Web3) -> dict:
    return {
        "epoch": _uint_call(w3, SEL_CURRENT_EPOCH),
        "remainingSeconds": _uint_call(w3, SEL_REMAINING_TIME),
    }


def _pool_ownership(w3: Web3, pool: str) -> dict:
    return {
        "owner": _address_call(w3, pool, SEL_OWNER),
        "pendingOwner": _address_call(w3, pool, SEL_PENDING_OWNER),
    }


async def _upgrade_node(
    cluster: Cluster,
    node_id: str,
    new_binary: Path,
    rollout_epoch: int,
) -> dict:
    node = cluster.get_node(node_id)
    assert node is not None
    peer_tip = max(
        peer.w3.eth.block_number
        for peer_id, peer in cluster.nodes.items()
        if peer_id != node_id
    )
    epoch_before = _epoch_snapshot(node.w3)
    assert epoch_before["epoch"] == rollout_epoch
    assert (
        epoch_before["remainingSeconds"]
        > MIN_ROLLING_EPOCH_HEADROOM_SECONDS
    ), f"unsafe epoch window before replacing {node_id}: {epoch_before}"
    started = time.monotonic()
    assert await node.stop(), f"failed to stop {node_id}"
    _replace_binary(new_binary, _node_binary(cluster, node_id))
    assert await node.start(rpc_timeout=120), f"failed to start {node_id}"
    await _wait_for_block(node_id, node.w3, peer_tip)
    assert await cluster.check_block_increasing(timeout=60)
    checkpoint = await _canonical_checkpoint(cluster)
    epoch_after = _epoch_snapshot(node.w3)
    assert epoch_after["epoch"] == rollout_epoch, (
        f"epoch changed while replacing {node_id}: "
        f"before={epoch_before} after={epoch_after}"
    )
    return {
        "node": node_id,
        "peerTipBeforeStop": peer_tip,
        "caughtUpHeight": node.w3.eth.block_number,
        "durationSeconds": round(time.monotonic() - started, 3),
        "canonicalCheckpoint": checkpoint,
        "epochBefore": epoch_before,
        "epochAfter": epoch_after,
    }


def _migration_table() -> list[dict]:
    return list(_metadata()["testnetOwnerFix"]["migrationTable"])


def _assert_pre_fork_ownership(w3: Web3) -> list[dict]:
    rows = []
    for row in _migration_table():
        pool = Web3.to_checksum_address(row["stake_pool"])
        state = _pool_ownership(w3, pool)
        assert state["owner"] == Web3.to_checksum_address(row["old_owner"]), row
        assert state["pendingOwner"] == Web3.to_checksum_address(
            "0x0000000000000000000000000000000000000000"
        ), row
        rows.append({"pool": pool, **state})
    return rows


def _assert_post_fork_ownership(w3: Web3) -> list[dict]:
    rows = []
    for row in _migration_table():
        pool = Web3.to_checksum_address(row["stake_pool"])
        state = _pool_ownership(w3, pool)
        assert state["pendingOwner"] == Web3.to_checksum_address(
            row["new_owner"]
        ), (row, state)
        assert state["owner"] == Web3.to_checksum_address(row["old_owner"]), (
            row,
            state,
        )
        rows.append({"pool": pool, **state, "label": row["label"]})
    return rows


async def _verify_hardfork(cluster: Cluster, activation_time: int) -> dict:
    node1 = cluster.get_node("node1")
    assert node1 is not None
    activation_block = await wait_for_activation_block(
        "node1",
        node1.w3,
        activation_time,
        int(
            os.environ.get(
                "OWNER_FIX_ROLLING_BLOCK_TIMEOUT_SECONDS",
                str(DEFAULT_BLOCK_WAIT_TIMEOUT_SECONDS),
            )
        ),
    )
    await asyncio.gather(
        *(
            _wait_for_block(
                node_id,
                node.w3,
                activation_block + CONFIRMATION_BLOCKS,
            )
            for node_id, node in cluster.nodes.items()
        )
    )
    # All replicas must agree on pendingOwner after activation.
    ownership_by_node = {}
    for node_id, node in cluster.nodes.items():
        ownership_by_node[node_id] = _assert_post_fork_ownership(node.w3)
    serialized = {
        node_id: json.dumps(rows, sort_keys=True)
        for node_id, rows in ownership_by_node.items()
    }
    assert len(set(serialized.values())) == 1, ownership_by_node
    return {
        "activationTime": activation_time,
        "activationBlock": activation_block,
        "ownership": ownership_by_node["node1"],
    }


@pytest.mark.asyncio
async def test_testnet_owner_fix_rolling_binary_upgrade(cluster: Cluster):
    metadata = _metadata()
    old_hash = metadata["oldBinarySha256"]
    new_hash = metadata["newBinarySha256"]
    activation_time = int(metadata["testnetOwnerFix"]["activationTime"])
    assert int(metadata["testnetOwnerFix"]["chainId"]) == 7771625
    new_binary = Path(os.environ["GRAVITY_NEW_BINARY"]).resolve()
    min_seconds = int(
        os.environ.get(
            "OWNER_FIX_ROLLING_MIN_SECONDS_PER_NODE",
            str(DEFAULT_MIN_SECONDS_PER_REMAINING_NODE),
        )
    )
    evidence = []
    upgraded: set[str] = set()

    try:
        assert len(cluster.nodes) == 4
        assert await cluster.set_full_live(timeout=180)
        assert await cluster.check_block_increasing(timeout=60)
        _assert_binary_matrix(cluster, upgraded, old_hash, new_hash)
        node1 = cluster.get_node("node1")
        assert node1 is not None
        assert node1.w3.eth.chain_id == 7771625
        pre_fork = _assert_pre_fork_ownership(node1.w3)
        rollout_epoch = _epoch_snapshot(node1.w3)["epoch"]

        for node_id in sorted(cluster.nodes):
            current_timestamp = max(
                block_timestamp(node.w3) for node in cluster.nodes.values()
            )
            remaining = len(cluster.nodes) - len(upgraded)
            required_headroom = min_seconds * remaining + 30
            assert activation_time - current_timestamp > required_headroom, (
                f"insufficient pre-activation headroom before {node_id}: "
                f"timestamp={current_timestamp} activation={activation_time} "
                f"required={required_headroom}"
            )
            evidence.append(
                await _upgrade_node(
                    cluster, node_id, new_binary, rollout_epoch
                )
            )
            upgraded.add(node_id)
            _assert_binary_matrix(cluster, upgraded, old_hash, new_hash)

        assert upgraded == set(cluster.nodes)
        final_upgrade_timestamp = max(
            block_timestamp(node.w3) for node in cluster.nodes.values()
        )
        assert final_upgrade_timestamp < activation_time
        hardfork = await _verify_hardfork(cluster, activation_time)
        assert _epoch_snapshot(cluster.get_node("node1").w3)["epoch"] == (
            rollout_epoch
        ), "epoch changed before TestnetOwnerFix activation completed"

        restart_node = cluster.get_node("node4")
        assert restart_node is not None
        restart_target = max(
            node.w3.eth.block_number for node in cluster.nodes.values()
        )
        restart_epoch_before = _epoch_snapshot(restart_node.w3)
        assert restart_epoch_before["epoch"] == rollout_epoch
        assert (
            restart_epoch_before["remainingSeconds"]
            > MIN_ROLLING_EPOCH_HEADROOM_SECONDS
        )
        assert await restart_node.restart(rpc_timeout=120)
        await _wait_for_block("node4", restart_node.w3, restart_target)
        assert await cluster.check_block_increasing(timeout=60)
        # Ownership still holds after restart replay.
        _assert_post_fork_ownership(restart_node.w3)
        checkpoint = await _canonical_checkpoint(cluster)

        summary = {
            "result": "PASS",
            "preForkOwnership": pre_fork,
            "rollout": evidence,
            "hardfork": hardfork,
            "postRestartCheckpoint": checkpoint,
            # acceptOwnership is intentionally out of scope (Safe e2e G1).
            "acceptOwnership": "skipped_covered_by_safe_e2e_g1",
        }
        _write_summary(summary)
        LOG.info("TestnetOwnerFix rolling upgrade summary: %s", summary)
    except Exception as exc:
        _write_summary({"result": "FAIL", "error": str(exc), "rollout": evidence})
        raise
