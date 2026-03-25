"""
Manifest writer for deterministic run tracking.
"""

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List


def compute_config_checksum(config: Dict) -> str:
    """
    Compute SHA256 checksum of configuration.
    
    This ensures any change to config creates a new campaign ID.
    
    Args:
        config: Configuration dict
        
    Returns:
        Hex string of SHA256 hash
    """
    # Convert to stable JSON string (sorted keys)
    config_str = json.dumps(config, sort_keys=True, separators=(',', ':'))
    
    # Compute SHA256
    hash_obj = hashlib.sha256(config_str.encode('utf-8'))
    
    return hash_obj.hexdigest()[:16]  # First 16 chars for brevity


class ManifestWriter:
    """
    Write run manifests for deterministic tracking.
    
    Each run creates:
    - runs/<campaign>/<run_id>/manifest.json
    - runs/<campaign>/<run_id>/inputs.json (market snapshots)
    - runs/<campaign>/<run_id>/outputs.json (forecasts and trades)
    - runs/<campaign>/<run_id>/run.log
    """
    
    def __init__(self, campaign: str, run_id: str, base_dir: str = "runs"):
        """
        Initialize manifest writer.
        
        Args:
            campaign: Campaign name
            run_id: Run identifier
            base_dir: Base directory for runs
        """
        self.campaign = campaign
        self.run_id = run_id
        self.run_dir = Path(base_dir) / campaign / run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        
        self.manifest_path = self.run_dir / "manifest.json"
        self.inputs_path = self.run_dir / "inputs.json"
        self.outputs_path = self.run_dir / "outputs.json"
        self.log_path = self.run_dir / "run.log"
        
    def write_manifest(
        self,
        config: Dict,
        run_time_utc: str,
        markets: List[Dict],
        mode: str
    ):
        """
        Write run manifest.
        
        Args:
            config: Configuration dict
            run_time_utc: Run timestamp
            markets: List of market dicts
            mode: Run mode
        """
        manifest = {
            "campaign": self.campaign,
            "run_id": self.run_id,
            "run_time_utc": run_time_utc,
            "mode": mode,
            "config": config,
            "markets": [m.get("ticker", m.get("id")) for m in markets],
            "n_markets": len(markets)
        }
        
        with open(self.manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)
            
    def write_inputs(self, inputs: Dict):
        """
        Write input snapshots (market states at run start).
        
        Args:
            inputs: Dict mapping market_id to market snapshot
        """
        with open(self.inputs_path, "w") as f:
            json.dump(inputs, f, indent=2)
            
    def write_outputs(self, outputs: Dict):
        """
        Write run outputs (forecasts, trades, etc.).
        
        Args:
            outputs: Dict with forecasts and trade decisions
        """
        with open(self.outputs_path, "w") as f:
            json.dump(outputs, f, indent=2)
            
    def append_log(self, message: str):
        """
        Append message to run log.
        
        Args:
            message: Log message
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        with open(self.log_path, "a") as f:
            f.write(f"[{timestamp}] {message}\n")
            
    def get_run_dir(self) -> Path:
        """Get run directory path."""
        return self.run_dir
