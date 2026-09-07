# TestnetOwnerFix Rolling Binary Upgrade E2E

Proves the Longevity Testnet deployment order for `TestnetOwnerFix`:

1. Start four equal-power validators on a pre-Fix `gravity_node` (`chainId=7771625`).
2. Genesis StakePool owners are the **Longevity unrecoverable** EOAs (same as
   greth `MIGRATION_TABLE`).
3. Replace validators one at a time while `timestamp < testnetOwnerFixTime`.
4. After all four run the new binary, wait for activation and assert
   `pendingOwner() == ceremony new EOA` on every pool (S1).
5. Restart one upgraded node and require canonical replay.

`acceptOwnership` is **out of scope** here — ops ceremony follow-up.

An old binary must never remain in the validator set at activation: it does not
inject the forced `transferOwnership` txs and would diverge from upgraded
validators.

## Prepare Binaries

Build the baseline from the SDK commit immediately before the TestnetOwnerFix
reth pin and build the candidate from this branch. Keep the resulting files at
stable, different paths. Limit Rust parallelism on memory-constrained hosts:

```bash
export CARGO_BUILD_JOBS=2
export CARGO_INCREMENTAL=0
export MALLOC_ARENA_MAX=2

RUSTFLAGS='--cfg tokio_unstable' \
  cargo build -j2 --profile quick-release -p gravity_node
```

The old baseline for this PR is SDK `c476e79793`, which pins gravity-reth
`556a5fd4f8d7fa246cb53929a8585bbaf9dc6323`. The candidate pins gravity-reth
`93f31550e2bff60f6e2b092467e23d7d11f89bfe` ([Galxe/gravity-reth#434](https://github.com/Galxe/gravity-reth/pull/434)).

## Run

```bash
export GRAVITY_OLD_BINARY=/stable/path/old/gravity_node
export GRAVITY_NEW_BINARY=/stable/path/new/gravity_node

./gravity_e2e/run_test.sh \
  testnet_owner_fix_rolling \
  --force-init \
  --log-cli-level=INFO
```

Optional headroom knobs:

```bash
OWNER_FIX_ROLLING_ACTIVATION_DELAY_SECONDS=1200 \
OWNER_FIX_ROLLING_MIN_SECONDS_PER_NODE=180 \
  ./gravity_e2e/run_test.sh testnet_owner_fix_rolling --force-init --log-cli-level=INFO
```
