"""
SQLite database for storing forecasts and evidence.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


class Database:
    """
    SQLite database for storing runs, forecasts, and evidence items.
    
    Schema:
    - runs: Run metadata
    - forecasts: Forecast outputs with parsed fields
    - evidence_items: Evidence items used in forecasts
    """
    
    def __init__(self, db_path: str = "runs/forecasts.db"):
        """
        Initialize database.
        
        Args:
            db_path: Path to SQLite database
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        self._init_schema()
        
    def _init_schema(self):
        """Initialize database schema."""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        # Runs table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                campaign TEXT NOT NULL,
                run_time_utc TEXT NOT NULL,
                mode TEXT NOT NULL,
                config_json TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        
        # Oracle markets table (replaces forecasts for v0.2)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS oracle_markets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                p_event REAL NOT NULL,
                bid REAL NOT NULL,
                ask REAL NOT NULL,
                spread_cents REAL NOT NULL,
                volume_24h INTEGER NOT NULL,
                asof_utc TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            )
        """)
        
        # Structures table (option structures)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS structures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                underlier TEXT NOT NULL,
                expiry TEXT NOT NULL,
                template TEXT NOT NULL,
                legs_json TEXT NOT NULL,
                premium REAL NOT NULL,
                max_loss REAL NOT NULL,
                max_gain REAL NOT NULL,
                ev REAL NOT NULL,
                ev_std REAL NOT NULL,
                prob_profit REAL NOT NULL,
                greeks_json TEXT,
                constraints_json TEXT,
                notes TEXT,
                rank INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            )
        """)
        
        # Evidence items table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS evidence_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                source TEXT NOT NULL,
                url TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                extracted_text TEXT,
                is_primary INTEGER NOT NULL,
                fetched_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            )
        """)
        
        # Create indices
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_oracle_markets_run_id 
            ON oracle_markets(run_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_oracle_markets_market_id 
            ON oracle_markets(market_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_structures_run_id 
            ON structures(run_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_structures_underlier 
            ON structures(underlier)
        """)
        
        conn.commit()
        conn.close()
        
    def insert_run(
        self,
        run_id: str,
        campaign: str,
        run_time_utc: str,
        mode: str,
        config: Dict,
        manifest: Dict
    ):
        """
        Insert run record.
        
        Args:
            run_id: Run identifier
            campaign: Campaign name
            run_time_utc: Run timestamp
            mode: Run mode
            config: Configuration dict
            manifest: Manifest dict
        """
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT OR REPLACE INTO runs
            (run_id, campaign, run_time_utc, mode, config_json, manifest_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id,
            campaign,
            run_time_utc,
            mode,
            json.dumps(config),
            json.dumps(manifest),
            datetime.now(timezone.utc).isoformat()
        ))
        
        conn.commit()
        conn.close()
        
    def insert_oracle_market(self, run_id: str, oracle_data: Dict):
        """
        Insert oracle market record.
        
        Args:
            run_id: Run identifier
            oracle_data: Oracle data dict
        """
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO oracle_markets
            (run_id, market_id, p_event, bid, ask, spread_cents, volume_24h,
             asof_utc, raw_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id,
            oracle_data.get("market_id"),
            oracle_data.get("p_event"),
            oracle_data.get("bid"),
            oracle_data.get("ask"),
            oracle_data.get("spread_cents"),
            oracle_data.get("volume_24h"),
            oracle_data.get("asof_utc"),
            json.dumps(oracle_data),
            datetime.now(timezone.utc).isoformat()
        ))
        
        conn.commit()
        conn.close()
    
    def insert_structure(self, run_id: str, structure: Dict):
        """
        Insert option structure record.
        
        Args:
            run_id: Run identifier
            structure: Structure evaluation dict
        """
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO structures
            (run_id, underlier, expiry, template, legs_json, premium,
             max_loss, max_gain, ev, ev_std, prob_profit, greeks_json,
             constraints_json, notes, rank, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id,
            structure.get("underlier"),
            structure.get("expiry"),
            structure.get("template_name"),
            json.dumps(structure.get("legs", [])),
            structure.get("premium", 0),
            structure.get("max_loss"),
            structure.get("max_gain"),
            structure.get("ev"),
            structure.get("std"),
            structure.get("prob_profit"),
            json.dumps(structure.get("greeks", {})),
            json.dumps(structure.get("constraints", {})),
            structure.get("notes", ""),
            structure.get("rank"),
            datetime.now(timezone.utc).isoformat()
        ))
        
        conn.commit()
        conn.close()
        
        
    def get_oracle_markets_by_run(self, run_id: str) -> List[Dict]:
        """
        Get all oracle markets for a run.
        
        Args:
            run_id: Run identifier
            
        Returns:
            List of oracle data dicts
        """
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT raw_json
            FROM oracle_markets
            WHERE run_id = ?
        """, (run_id,))
        
        rows = cursor.fetchall()
        conn.close()
        
        return [json.loads(row[0]) for row in rows]
    
    def get_structures_by_run(self, run_id: str) -> List[Dict]:
        """
        Get all structures for a run.
        
        Args:
            run_id: Run identifier
            
        Returns:
            List of structure dicts
        """
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT underlier, expiry, template, legs_json, premium,
                   max_loss, max_gain, ev, ev_std, prob_profit, rank
            FROM structures
            WHERE run_id = ?
            ORDER BY rank
        """, (run_id,))
        
        rows = cursor.fetchall()
        conn.close()
        
        structures = []
        for row in rows:
            structures.append({
                "underlier": row[0],
                "expiry": row[1],
                "template_name": row[2],
                "legs": json.loads(row[3]),
                "premium": row[4],
                "max_loss": row[5],
                "max_gain": row[6],
                "ev": row[7],
                "std": row[8],
                "prob_profit": row[9],
                "rank": row[10]
            })
        
        return structures
