#!/usr/bin/env python3
"""
OCI VM Provisioning Automation Script

Automates VM provisioning on OCI with intelligent retry logic for capacity constraints.
Handles "out of host capacity" errors by retrying with exponential backoff.
Uses OCI Python SDK for direct API calls, avoiding CLI parsing issues.
"""

import json
import logging
import sys
import time
import argparse
import os
from pathlib import Path
from typing import Any, Dict, NoReturn, Optional

# OCI SDK imports
import oci
from oci.config import from_file
from oci.core import ComputeClient
from oci.core.models import (
    CreateVnicDetails,
    InstanceSourceViaImageDetails,
    LaunchInstanceDetails,
    LaunchInstanceShapeConfigDetails,
)
from oci.exceptions import ServiceError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('oci_provisioning.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


def _fail(msg: str) -> NoReturn:
    logger.error(msg)
    raise SystemExit(1)


class OCIProvisioner:
    """Handles OCI VM provisioning with retry logic using OCI SDK."""
    
    def __init__(self, config_path: str = 'config.json', profile_override: Optional[str] = None):
        """
        Initialize the provisioner.
        
        Args:
            config_path: Path to the config.json file
            profile_override: Override OCI profile from command line
        """
        self.config = self._load_config(config_path)
        self.profile = profile_override or os.getenv('OCI_PROFILE') or self.config.get('oci_profile', 'DEFAULT')
        self.vm_config = self.config.get('vm_config', {})
        self.retry_config = self.config.get('retry_config', {})
        
        # Set defaults for retry config
        self.max_attempts = self.retry_config.get('max_attempts', 30)
        self.initial_delay = self.retry_config.get('initial_delay_seconds', 5)
        self.max_delay = self.retry_config.get('max_delay_seconds', 300)
        self.backoff_multiplier = self.retry_config.get('backoff_multiplier', 1.5)
        
        # Load OCI SDK config once and reuse across clients
        self.oci_config = self._load_oci_sdk_config()

        # Initialize OCI SDK clients
        self.compute_client = self._init_compute_client()
        
        logger.info(f"Initialized provisioner with profile: {self.profile}")
        logger.info(f"Max retry attempts: {self.max_attempts}")

    def _load_oci_sdk_config(self) -> Dict[str, Any]:
        """Load OCI SDK config (~/.oci/config) for the selected profile and apply region override."""
        try:
            cfg = from_file(profile_name=self.profile)
            if 'region' in self.vm_config:
                cfg['region'] = self.vm_config['region']
            return cfg
        except Exception as e:
            logger.error(f"Failed to load OCI SDK config: {e}")
            sys.exit(1)
    
    def _init_compute_client(self) -> ComputeClient:
        """Initialize OCI Compute Client using the specified profile."""
        try:
            if 'region' in self.vm_config:
                logger.info(f"Using region from config: {self.vm_config['region']}")

            client = ComputeClient(self.oci_config)
            logger.info(f"OCI Compute Client initialized with profile: {self.profile}")
            return client
        except Exception as e:
            logger.error(f"Failed to initialize OCI Compute Client: {e}")
            sys.exit(1)

    def _maybe_enable_oci_sdk_debug_logging(self) -> None:
        """Enable OCI SDK HTTP request/response logging when our log level is DEBUG."""
        try:
            if logger.getEffectiveLevel() <= logging.DEBUG:
                # OCI Python SDK uses the standard logging framework.
                # These loggers are verbose but invaluable when chasing 400 CannotParseRequest.
                logging.getLogger('oci').setLevel(logging.DEBUG)
                logging.getLogger('oci.base_client').setLevel(logging.DEBUG)
                logger.debug("Enabled OCI SDK debug logging")
        except Exception:
            # Never fail a provision run because logging couldn't be configured
            pass
    
    def _load_config(self, config_path: str) -> Dict[str, Any]:
        """Load configuration from JSON file."""
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
            logger.info(f"Configuration loaded from {config_path}")
            
            # Validate required fields
            vm_config = config.get('vm_config', {})
            required_fields = ['compartment_id', 'image_id', 'shape', 'subnet_id']
            
            for field in required_fields:
                if not vm_config.get(field):
                    logger.error(f"Missing required field in vm_config: {field}")
                    sys.exit(1)
                # Validate OCID format (should start with ocid1.)
                if field in ['compartment_id', 'image_id', 'subnet_id']:
                    if not vm_config[field].startswith('ocid1.'):
                        logger.error(f"Invalid OCID format for {field}: {vm_config[field]}")
                        sys.exit(1)

            if not vm_config.get('availability_domain'):
                logger.error("Missing required field in vm_config: availability_domain")
                sys.exit(1)

            # Flex shapes generally require a shape_config payload (OCPUs/memory).
            shape = vm_config.get('shape', '')
            if 'Flex' in shape:
                sc = vm_config.get('shape_config') or {}
                if sc.get('ocpus') is None or sc.get('memory_in_gbs') is None:
                    logger.error(
                        "Flex shape requires vm_config.shape_config.ocpus and vm_config.shape_config.memory_in_gbs"
                    )
                    sys.exit(1)
            
            return config
        except FileNotFoundError:
            logger.error(f"Config file not found: {config_path}")
            sys.exit(1)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in config file: {e}")
            sys.exit(1)

    def _load_ssh_authorized_keys(self) -> Optional[str]:
        """Load SSH public key content from config.
        """
        key_value = self.vm_config.get('ssh_public_key')
        if not key_value:
            return None

        key_path = Path(str(key_value))
        if key_path.exists() and key_path.is_file():
            try:
                key_text = key_path.read_text(encoding='utf-8').strip()
            except Exception as e:
                _fail(f"Failed reading ssh_public_key file '{key_path}': {e}")
        else:
            key_text = str(key_value).strip()

        if not key_text:
            return None

        if not key_text.startswith('ssh-'):
            _fail(
                "ssh_public_key must be an OpenSSH public key (starts with 'ssh-'), "
                f"got: {key_text[:40]!r}..."
            )

        return key_text
    
    def _is_capacity_error(self, error_message: str) -> bool:
        """Check if the error is due to host capacity."""
        capacity_indicators = [
            'out of host capacity',
            'no sufficient compute capacity',
            'insufficient capacity',
            'capacity exceeded',
            'OutOfCapacity',
            'capacity.exceeded'
        ]
        return any(indicator.lower() in error_message.lower() for indicator in capacity_indicators)
    
    def _build_launch_instance_details(self) -> LaunchInstanceDetails:
        """Build LaunchInstanceDetails object from config."""

        ad_name = self.vm_config.get('availability_domain')
        compartment_id = self.vm_config.get('compartment_id')
        
        # Build source details from image
        source_details = InstanceSourceViaImageDetails(
            image_id=self.vm_config.get('image_id'),
            boot_volume_size_in_gbs=self.vm_config.get('boot_volume_size_in_gbs'),
        )

        # Build primary VNIC details with subnet
        primary_vnic = CreateVnicDetails(
            subnet_id=self.vm_config.get('subnet_id'),
            assign_public_ip=self.vm_config.get('assign_public_ip'),
        )

        # Flex shapes typically require a shape_config payload.
        shape_config = None
        if self.vm_config.get('shape_config'):
            shape_config = LaunchInstanceShapeConfigDetails(
                ocpus=self.vm_config['shape_config'].get('ocpus'),
                memory_in_gbs=self.vm_config['shape_config'].get('memory_in_gbs'),
            )

        ssh_keys = self._load_ssh_authorized_keys()
        metadata = {"ssh_authorized_keys": ssh_keys} if ssh_keys else None

        # Build launch instance details
        launch_details = LaunchInstanceDetails(
            compartment_id=compartment_id,
            display_name=self.vm_config.get('display_name', 'compute-instance'),
            availability_domain=ad_name,
            source_details=source_details,
            shape=self.vm_config.get('shape'),
            shape_config=shape_config,
            create_vnic_details=primary_vnic,
            metadata=metadata,
        )
        
        # Only add optional fields if they're actually needed
        # Start simple - users can add more advanced features later
        
        logger.debug(f"Launch instance details: {launch_details}")
        return launch_details
    
    def _launch_instance(self, launch_details: LaunchInstanceDetails) -> Optional[str]:
        """
        Attempt to launch an instance.
        
        Returns:
            Instance ID if successful, None if failed
        """
        try:
            response = self.compute_client.launch_instance(launch_details)
            instance_id = response.data.id
            logger.info(f"[SUCCESS] Instance launched successfully!")
            logger.info(f"Instance ID: {instance_id}")
            return instance_id
        except ServiceError as e:
            error_msg = str(e)
            logger.debug(f"OCI ServiceError: {e}")
            
            # Check if it's a capacity error
            if self._is_capacity_error(error_msg):
                logger.warning(f"[RETRY] Capacity error: {e.message if hasattr(e, 'message') else error_msg}")
                return None
            else:
                # Non-capacity error - print full details for debugging
                logger.error(f"[FAILED] Non-retryable error: {e.message if hasattr(e, 'message') else error_msg}")
                if hasattr(e, '__dict__'):
                    logger.debug(f"Full error details: {e.__dict__}")

                # When debugging 400 CannotParseRequest, seeing the exact payload helps.
                try:
                    logger.debug("Launch payload (sanitized for serialization):")
                    logger.debug(json.dumps(oci.util.sanitize_for_serialization(launch_details), indent=2))
                except Exception:
                    logger.debug(f"Launch payload (str): {launch_details}")
                raise
        except Exception as e:
            logger.error(f"[FAILED] Unexpected error: {e}")
            import traceback
            logger.debug(f"Traceback: {traceback.format_exc()}")
            raise
    
    def provision_vm_with_retry(self) -> bool:
        """
        Provision a VM with retry logic for capacity errors.
        
        Returns:
            True if VM was successfully provisioned, False otherwise
        """
        launch_details = self._build_launch_instance_details()
        attempt = 0
        current_delay = self.initial_delay
        
        logger.info("=" * 70)
        logger.info("Starting OCI VM provisioning process")
        logger.info(f"Instance name: {self.vm_config.get('display_name')}")
        logger.info(f"Shape: {self.vm_config.get('shape')}")
        logger.info(f"Region: {self.vm_config.get('region')}")
        logger.info("=" * 70)
        
        while attempt < self.max_attempts:
            attempt += 1
            logger.info(f"\n[Attempt {attempt}/{self.max_attempts}] Launching instance...")
            
            try:
                instance_id = self._launch_instance(launch_details)
                
                if instance_id:
                    # Success
                    logger.info("=" * 70)
                    logger.info("VM PROVISIONING COMPLETED SUCCESSFULLY")
                    logger.info("=" * 70)
                    return True
                else:
                    # Capacity error - retry
                    if attempt < self.max_attempts:
                        next_delay = min(current_delay * self.backoff_multiplier, self.max_delay)
                        logger.info(f"  Waiting {current_delay:.1f}s before retry... (next delay: {next_delay:.1f}s)")
                        time.sleep(current_delay)
                        current_delay = next_delay
                    else:
                        logger.error(f"[FAILED] Max retry attempts ({self.max_attempts}) reached. Giving up.")
                        return False
            
            except ServiceError as e:
                # Non-retryable error
                return False
            except Exception as e:
                logger.error(f"[FAILED] Unexpected error: {e}")
                return False
        
        logger.error(f"[FAILED] Failed to provision VM after {self.max_attempts} attempts")
        return False


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='OCI VM Provisioning with Automatic Retry Logic',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  python main.py                                    # Use DEFAULT profile
  python main.py --profile PRODUCTION               # Use PRODUCTION profile
  python main.py --config staging-config.json       # Use custom config file
  python main.py --profile PROD --config prod.json  # Both custom profile and config
        '''
    )
    parser.add_argument(
        '--profile',
        type=str,
        help='OCI profile to use (overrides config.json and OCI_PROFILE env var)'
    )
    parser.add_argument(
        '--config',
        type=str,
        default='config.json',
        help='Path to configuration file (default: config.json)'
    )
    parser.add_argument(
        '--log-level',
        type=str,
        default='INFO',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        help='Logging level (default: INFO)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Validate config and print the LaunchInstanceDetails payload without launching an instance'
    )
    
    args = parser.parse_args()
    
    # Set log level
    logger.setLevel(getattr(logging, args.log_level))
    
    # Validate config file exists
    if not Path(args.config).exists():
        logger.error(f"Configuration file not found: {args.config}")
        sys.exit(1)
    
    # Initialize provisioner and launch VM
    provisioner = OCIProvisioner(config_path=args.config, profile_override=args.profile)
    provisioner._maybe_enable_oci_sdk_debug_logging()

    if args.dry_run:
        launch_details = provisioner._build_launch_instance_details()
        logger.info("DRY RUN: launch payload below (no instance will be created)")
        try:
            logger.info(json.dumps(oci.util.sanitize_for_serialization(launch_details), indent=2))
        except Exception:
            # Fallback: this is *not* the wire format, just a readable representation
            logger.info(str(launch_details))
        sys.exit(0)

    success = provisioner.provision_vm_with_retry()
    
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
