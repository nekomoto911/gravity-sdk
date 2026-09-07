"""Prepare old/new binaries and TestnetOwnerFix genesis (Longevity migration table)."""

import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import time

from web3 import Web3

try:
    import tomllib
except ImportError:
    import tomli as tomllib


LOG = logging.getLogger(__name__)
METADATA_FILE = "testnet_owner_fix_rolling_metadata.json"
SUMMARY_FILE = "testnet_owner_fix_rolling_summary.json"
DEFAULT_ACTIVATION_DELAY_SECONDS = 900

# Must match gravity-reth `hardfork::testnet_owner_fix::MIGRATION_TABLE`
# (Longevity production table). Local node4 maps to Longevity node5.
MIGRATION_TABLE = (
    {
        "label": "node1",
        "local_node": "node1",
        "stake_pool": "0x743d93845745e01a23f9afbb990bbc7c87aae6c8",
        "old_owner": "0xCE128222Bd84D67672f863424a03D114CD1253C5",
        "new_owner": "0x91a59bae639a3cef0c41e4c61268aa54c71de1ba",
    },
    {
        "label": "node2",
        "local_node": "node2",
        "stake_pool": "0x419ad62f796a0f3971bd1212f208942c3c435b99",
        "old_owner": "0x78F595Fb25D03a742338Fb32AcfD544BdC63D814",
        "new_owner": "0x6a0da8def2ccd134119c0293ad470d1aa1d6129a",
    },
    {
        "label": "node3",
        "local_node": "node3",
        "stake_pool": "0x93e5acbcdd50767f7fd19ab4a2efc259d9a8bdd1",
        "old_owner": "0x891299fE364088ead65ABa911ea17DD5d968Cd81",
        "new_owner": "0x5a1ba49d261e1e58dd1b8cf0aeeb1976d04ac6bd",
    },
    {
        "label": "node5",
        "local_node": "node4",
        "stake_pool": "0x298136ce84d442d2c0c594f5734a20afc60de244",
        "old_owner": "0xB99AA922Eb5CaE399b79ADC87621E72f66d5A976",
        "new_owner": "0x2326795e2033d209ea12b1022c50ae592ac2b720",
    },
)

STAKEPOOL_CODE_HASH = (
    "0x77e0b0dcaa8422c64dd50f39f1c450698fa2ee51c24fb3979e0c0bff59aadfd0"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _required_binary(env: dict, name: str) -> Path:
    value = env.get(name)
    if not value:
        raise RuntimeError(f"{name} must point to an executable gravity_node")
    path = Path(value).expanduser().resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError(f"{name} is not an executable file: {path}")
    return path


def _replace_binary(source: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.rolling.tmp")
    temporary.unlink(missing_ok=True)
    shutil.copy2(source, temporary)
    temporary.chmod(0o755)
    os.replace(temporary, destination)


def _alloc_key(alloc: dict, address: str) -> str:
    normalized = address.removeprefix("0x").lower()
    for key in alloc:
        if key.removeprefix("0x").lower() == normalized:
            return key
    raise RuntimeError(f"genesis alloc is missing account {address}")


def _runtime_hash(code: str) -> str:
    if not code.startswith("0x") or len(code) % 2:
        raise RuntimeError("runtime is not canonical hex")
    return Web3.to_hex(Web3.keccak(hexstr=code)).lower()


def _word_address(storage_value: str) -> str:
    raw = storage_value.removeprefix("0x").rjust(64, "0")
    return Web3.to_checksum_address("0x" + raw[-40:])


def _owner_slot_value(storage: dict) -> str | None:
    for sk, sv in storage.items():
        if int(sk, 16) == 0:
            return sv
    return None


def _find_pool_by_owner(alloc: dict, old_owner: str) -> str:
    """Locate the CREATE2 pool whose Ownable slot0 == old_owner."""
    want = Web3.to_checksum_address(old_owner)
    matches = []
    for addr, account in alloc.items():
        code = account.get("code") or ""
        if len(code) < 10:
            continue
        if _runtime_hash(code) != STAKEPOOL_CODE_HASH:
            continue
        owner_word = _owner_slot_value(account.get("storage") or {})
        if owner_word is None:
            continue
        if _word_address(owner_word) == want:
            matches.append(addr)
    if len(matches) != 1:
        raise RuntimeError(
            f"expected exactly one StakePool for owner {old_owner}, found {matches}"
        )
    return matches[0]


def _normalize_hex(hex_str: str) -> tuple[bool, str]:
    had_prefix = hex_str.startswith("0x") or hex_str.startswith("0X")
    body = hex_str[2:] if had_prefix else hex_str
    return had_prefix, body.lower()


def _is_solidity_address_word(hex_str: str) -> bool:
    """True iff hex is a 32-byte word with a right-aligned 20-byte address."""
    _, body = _normalize_hex(hex_str)
    return len(body) == 64 and body[:24] == ("0" * 24)


def _rewrite_address_word(hex_str: str, old_addr: str, new_addr: str) -> str:
    """
    Rewrite only Solidity address words (32-byte, left-padded).

    Naive substring replace corrupts BCS-encoded ValidatorSet blobs and makes
    consensus fail with `Consensus public key ... DeserializationError`.
    """
    if not _is_solidity_address_word(hex_str):
        return hex_str
    had_prefix, body = _normalize_hex(hex_str)
    old_n = old_addr.removeprefix("0x").removeprefix("0X").lower()
    new_n = new_addr.removeprefix("0x").removeprefix("0X").lower()
    if body[24:] != old_n:
        return hex_str
    out = ("0" * 24) + new_n
    return ("0x" + out) if had_prefix else out


def _relocate_pool(alloc: dict, from_addr: str, to_addr: str) -> None:
    """Move a StakePool account to the Longevity CREATE2 address and rewrite refs."""
    from_key = _alloc_key(alloc, from_addr)
    to_norm = Web3.to_checksum_address(to_addr)
    for key in alloc:
        if key.removeprefix("0x").lower() == to_norm.removeprefix("0x").lower():
            raise RuntimeError(f"target pool address already occupied: {to_norm}")

    account = alloc.pop(from_key)
    alloc[to_norm] = account

    for acc in alloc.values():
        storage = acc.get("storage")
        if not storage:
            continue
        rewritten = {}
        for sk, sv in storage.items():
            rewritten[_rewrite_address_word(sk, from_addr, to_addr)] = _rewrite_address_word(
                sv, from_addr, to_addr
            )
        acc["storage"] = rewritten
def _align_pools_to_production_table(generated: dict) -> list[dict]:
    """
    Genesis-tool CREATE2 salts differ from Longevity, so local pool addresses
    drift. Owner + StakePool codehash still match. Relocate each generated pool
    onto the Longevity address from greth MIGRATION_TABLE and rewrite storage
    references so Staking/ValidatorManagement keep pointing at the same pools.
    """
    alloc = generated.get("alloc", {})
    chain_id = int(generated.get("config", {}).get("chainId", 0))
    if chain_id != 7771625:
        raise RuntimeError(f"expected chainId 7771625, got {chain_id}")

    verified = []
    for row in MIGRATION_TABLE:
        expected_pool = Web3.to_checksum_address(row["stake_pool"])
        expected_owner = Web3.to_checksum_address(row["old_owner"])
        generated_pool = _find_pool_by_owner(alloc, expected_owner)
        generated_cs = Web3.to_checksum_address(generated_pool)
        if generated_cs != expected_pool:
            LOG.info(
                "Relocating %s StakePool %s -> Longevity %s",
                row["label"],
                generated_cs,
                expected_pool,
            )
            _relocate_pool(alloc, generated_cs, expected_pool)
        else:
            LOG.info("%s StakePool already at Longevity address %s", row["label"], expected_pool)

        key = _alloc_key(alloc, expected_pool)
        account = alloc[key]
        code_hash = _runtime_hash(account.get("code", ""))
        if code_hash != STAKEPOOL_CODE_HASH:
            raise RuntimeError(
                f"{row['label']} pool {expected_pool} codehash {code_hash} "
                f"!= {STAKEPOOL_CODE_HASH}"
            )
        owner_word = _owner_slot_value(account.get("storage") or {})
        if owner_word is None:
            raise RuntimeError(f"{row['label']} pool {expected_pool} missing owner slot 0")
        owner = _word_address(owner_word)
        if owner != expected_owner:
            raise RuntimeError(
                f"{row['label']} pool owner {owner} != expected {expected_owner}"
            )
        verified.append(
            {
                **row,
                "stake_pool": expected_pool,
                "old_owner": expected_owner,
                "new_owner": Web3.to_checksum_address(row["new_owner"]),
                "codeHash": code_hash,
                "generatedPoolBeforeRelocate": generated_cs,
            }
        )
    return verified


def _prepare_genesis(test_dir: Path, activation_override: str | None) -> dict:
    genesis_path = test_dir / "artifacts" / "genesis.json"
    generated = json.loads(genesis_path.read_text())

    delay = int(
        os.environ.get(
            "OWNER_FIX_ROLLING_ACTIVATION_DELAY_SECONDS",
            DEFAULT_ACTIVATION_DELAY_SECONDS,
        )
    )
    activation_time = (
        int(activation_override)
        if activation_override is not None
        else int(time.time()) + delay
    )
    if activation_time <= 0:
        raise RuntimeError("config.testnetOwnerFixTime must be a positive Unix timestamp")

    config = generated.setdefault("config", {})
    # Drop unrelated placeholders that could confuse older binaries.
    config.pop("gammaTime", None)
    config.pop("oracleV1Block", None)
    config["alphaTime"] = 0
    config["testnetOwnerFixTime"] = activation_time

    if os.environ.get("OWNER_FIX_SKIP_RELOCATE"):
        # Debug: keep CREATE2 addresses; rewrite metadata table to discovered pools.
        migration = []
        alloc = generated.get("alloc", {})
        for row in MIGRATION_TABLE:
            pool = _find_pool_by_owner(alloc, row["old_owner"])
            migration.append({
                **row,
                "stake_pool": Web3.to_checksum_address(pool),
                "old_owner": Web3.to_checksum_address(row["old_owner"]),
                "new_owner": Web3.to_checksum_address(row["new_owner"]),
                "codeHash": STAKEPOOL_CODE_HASH,
            })
            LOG.warning("SKIP_RELOCATE: %s using discovered pool %s (NOT greth table)", row["label"], pool)
    else:
        migration = _align_pools_to_production_table(generated)

    temporary = genesis_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(generated, indent=2) + "\n")
    os.replace(temporary, genesis_path)
    return {
        "activationTime": activation_time,
        "chainId": int(config["chainId"]),
        "migrationTable": migration,
    }


def pre_deploy(test_dir: Path, env: dict, pytest_args: list[str]):
    old_binary = _required_binary(env, "GRAVITY_OLD_BINARY")
    new_binary = _required_binary(env, "GRAVITY_NEW_BINARY")
    old_hash = _sha256(old_binary)
    new_hash = _sha256(new_binary)
    if old_hash == new_hash and not os.environ.get("OWNER_FIX_ALLOW_SAME_BINARY"):
        raise RuntimeError("old and new gravity_node binaries must differ")

    hardfork = _prepare_genesis(
        test_dir, env.get("OWNER_FIX_ROLLING_ACTIVATION_TIME")
    )
    artifacts = test_dir / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / SUMMARY_FILE).unlink(missing_ok=True)
    (artifacts / METADATA_FILE).write_text(
        json.dumps(
            {
                "oldBinarySha256": old_hash,
                "newBinarySha256": new_hash,
                "testnetOwnerFix": hardfork,
            },
            indent=2,
        )
        + "\n"
    )
    env["GRAVITY_OLD_BINARY"] = str(old_binary)
    env["GRAVITY_NEW_BINARY"] = str(new_binary)
    LOG.info(
        "Prepared TestnetOwnerFix rolling upgrade old=%s new=%s activation=%d chainId=%s",
        old_hash[:12],
        new_hash[:12],
        hardfork["activationTime"],
        hardfork["chainId"],
    )


def pre_start(test_dir: Path, env: dict, pytest_args: list[str]):
    old_binary = _required_binary(env, "GRAVITY_OLD_BINARY")
    with (test_dir / "cluster.toml").open("rb") as source:
        config = tomllib.load(source)
    base_dir = Path(config["cluster"]["base_dir"])
    for node in config["nodes"]:
        destination = base_dir / node["id"] / "bin" / "gravity_node"
        _replace_binary(old_binary, destination)
        LOG.info("Installed old binary for %s", node["id"])


def post_stop(test_dir: Path, env: dict):
    (test_dir / "artifacts" / METADATA_FILE).unlink(missing_ok=True)
