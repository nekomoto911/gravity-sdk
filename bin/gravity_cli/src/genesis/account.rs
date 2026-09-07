use clap::Parser;

use k256::ecdsa::SigningKey;
use rand_core::OsRng;
use sha3::{Digest, Keccak256};

use std::{fs, path::PathBuf};

use crate::command::Executable;

use serde::Serialize;

#[derive(Debug, Serialize)]
struct Account {
    private_key: String,
    public_key: String,
    address: String,
}

#[derive(Debug, Parser)]
pub struct GenerateAccount {
    /// Output file path
    #[clap(long, value_parser)]
    pub output_file: PathBuf,
}

/// Derive `(private_key_hex, uncompressed_pubkey_hex, address)` for an EVM account.
///
/// Address formula (Ethereum):
/// `keccak256(uncompressed_sec1_pubkey_without_0x04_prefix)[12..]`.
///
/// IMPORTANT: `VerifyingKey::to_sec1_bytes()` returns a *compressed* point in k256.
/// Always use `to_encoded_point(false)` for address derivation.
fn account_from_signing_key(
    signing_key: &SigningKey,
) -> Result<(String, String, String), anyhow::Error> {
    let private_key_hex = hex::encode(signing_key.to_bytes());

    let verifying_key = signing_key.verifying_key();
    let point = verifying_key.to_encoded_point(/* compress = */ false);
    let bytes = point.as_bytes();
    anyhow::ensure!(
        bytes.len() == 65 && bytes[0] == 0x04,
        "expected uncompressed SEC1 public key (0x04 || X || Y), got {} bytes prefix={:02x}",
        bytes.len(),
        bytes.first().copied().unwrap_or(0)
    );
    let public_key_for_hashing = &bytes[1..];

    let public_key_hash = Keccak256::digest(public_key_for_hashing);
    let account_address = format!("0x{}", hex::encode(&public_key_hash[12..]));

    Ok((private_key_hex, hex::encode(public_key_for_hashing), account_address))
}

fn generate_eth_account() -> Result<(String, String, String), anyhow::Error> {
    let signing_key = SigningKey::random(&mut OsRng);
    account_from_signing_key(&signing_key)
}

impl Executable for GenerateAccount {
    fn execute(self) -> Result<(), anyhow::Error> {
        let (private_key, public_key, address) = generate_eth_account()?;
        let account = Account { private_key, public_key, address };
        let yaml_string = serde_yaml::to_string(&account)?;
        fs::write(self.output_file, yaml_string)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Anvil/Hardhat default account #0.
    const ANVIL0_PK: &str = "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80";
    const ANVIL0_ADDR: &str = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266";

    fn anvil0_signing_key() -> SigningKey {
        SigningKey::from_slice(&hex::decode(ANVIL0_PK).unwrap()).unwrap()
    }

    #[test]
    fn anvil0_matches_standard_eth_address() {
        let (pk_hex, pub_hex, addr) = account_from_signing_key(&anvil0_signing_key()).unwrap();
        assert_eq!(pk_hex, ANVIL0_PK);
        assert_eq!(pub_hex.len(), 128, "uncompressed pubkey without 0x04 is 64 bytes");
        assert_eq!(addr.to_lowercase(), ANVIL0_ADDR);
    }

    #[test]
    fn address_is_not_compressed_pubkey_hash() {
        // Regression: old bug hashed compressed_sec1[1..] (32 bytes) instead of
        // uncompressed[1..] (64 bytes). For Anvil #0 those diverge.
        let sk = anvil0_signing_key();
        let compressed = sk.verifying_key().to_encoded_point(true);
        let cbytes = compressed.as_bytes();
        assert!(cbytes[0] == 0x02 || cbytes[0] == 0x03);
        let bogus = Keccak256::digest(&cbytes[1..]);
        let bogus_addr = format!("0x{}", hex::encode(&bogus[12..]));

        let (_, _, addr) = account_from_signing_key(&sk).unwrap();
        assert_ne!(
            addr.to_lowercase(),
            bogus_addr.to_lowercase(),
            "must not reproduce the compressed-pubkey address bug"
        );
        assert_eq!(addr.to_lowercase(), ANVIL0_ADDR);
    }

    #[test]
    fn random_account_roundtrip_pubkey_len() {
        let (pk, pub_hex, addr) = generate_eth_account().unwrap();
        assert_eq!(pk.len(), 64);
        assert_eq!(pub_hex.len(), 128);
        assert!(addr.starts_with("0x") && addr.len() == 42);
    }
}
