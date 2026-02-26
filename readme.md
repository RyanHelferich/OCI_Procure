# OCI Procure – OCI VM Provisioning with Retry Logic

** In Development & Testing ** 

Provision Oracle Cloud Infrastructure (OCI) Compute instances using the **OCI Python SDK**, with built-in **retry/backoff** when OCI returns host capacity errors (e.g., *“out of host capacity”*).

This repo is intentionally small and focused:

- `main.py` – launch an instance using values from `config.json` (or a custom config path)
- `config.json` – your environment-specific configuration (OCIDs, shape, subnet, AD, etc.)

## What this tool does

- Loads OCI credentials from your local `~/.oci/config` profile
- Reads instance launch parameters from `config.json`
- Attempts to launch an instance via the OCI Compute API
- If OCI reports a capacity error, retries with exponential backoff until success or max attempts

## What this tool does NOT do

- It **does not** create networking (VCN/subnet/route tables/NSGs)
- It **does not** auto-fix your config (Availability Domain, compartment OCIDs, SSH keys)
- It **does not** manage lifecycle (terminate instances automatically, etc.)

If you want fully automated networking creation/cleanup, see Oracle’s official SDK examples.

---

## Prerequisites

1. **Python 3.9+** (3.11/3.12 works fine)
2. OCI credentials configured locally in:
   - `C:\Users\<you>\.oci\config` on Windows
   - `~/.oci/config` on Linux/macOS
3. The **OCI Python SDK** installed:

```bash
pip install oci
```

4. A VCN + Subnet already created
5. An **OpenSSH public key** (starts with `ssh-...`) for SSH access

---

## Configuration

The script reads configuration from a JSON file (default: `config.json`).

Notes:

- The config file must be **valid JSON** (no trailing commas, no comments).
- This repo keeps `config.json` as a **blank template**. Fill in your values locally before running.

### config.json example

Fill in `config.json` based on your environment:

```json
{
  "oci_profile": "DEFAULT",
  "vm_config": {
    "display_name": "my-instance",
    "compartment_id": "ocid1.compartment.oc1..REPLACE_ME",
    "image_id": "ocid1.image.oc1.REGION.REPLACE_ME",
    "shape": "VM.Standard.E5.Flex",
    "shape_config": {
      "ocpus": 2,
      "memory_in_gbs": 16
    },
    "subnet_id": "ocid1.subnet.oc1.REGION.REPLACE_ME",
    "region": "ap-singapore-1",
    "availability_domain": "<TENANCY_PREFIX>:AP-SINGAPORE-1-AD-1",
    "ssh_public_key": "ssh-ed25519 AAAA...",
    "assign_public_ip": true,
    "boot_volume_size_in_gbs": 50
  },
  "retry_config": {
    "max_attempts": 30,
    "initial_delay_seconds": 5,
    "max_delay_seconds": 300,
    "backoff_multiplier": 1.5
  }
}
```

### Notes on key fields

- `availability_domain` must be the **full AD name** returned by OCI (often includes a tenancy prefix like `xOnw:...`).
- `ssh_public_key` must be an **OpenSSH public key** (not an OCI API key PEM). It should start with `ssh-`.
- `shape_config` is required for most `Flex` shapes.

---

## Usage

### Dry run (recommended)

Validates your config and prints the request payload **without** launching an instance:

```bash
python main.py --profile DEFAULT --config config.json --dry-run
```

### Launch

```bash
python main.py --profile DEFAULT --config config.json
```

### Enable debug logging

```bash
python main.py --profile DEFAULT --config config.json --log-level DEBUG
```

---

## Retry behavior (capacity errors)

The script treats the following as capacity errors and will retry:

- `out of host capacity`
- `no sufficient compute capacity`
- `insufficient capacity`
- `capacity exceeded`
- `OutOfCapacity`
- `capacity.exceeded`

All other OCI errors are treated as non-retryable and the script exits with a non-zero code.

---


