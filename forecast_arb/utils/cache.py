"""
Evidence cache for deterministic runs.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional


class EvidenceCache:
    """
    Deterministic cache for evidence items.
    
    Cache key: (market_id, timestamp_utc, evidence_source, url, content_hash)
    """
    
    def __init__(self, cache_path: str = "runs/evidence_cache.db", ttl_hours: int = 24):
        """
        Initialize evidence cache.
        
        Args:
            cache_path: Path to SQLite cache database
            ttl_hours: Time-to-live for cache entries in hours
        """
        self.cache_path = Path(cache_path)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl_hours = ttl_hours
        
        self._init_db()
        
    def _init_db(self):
        """Initialize cache database schema."""
        conn = sqlite3.connect(str(self.cache_path))
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS evidence_cache (
                cache_key TEXT PRIMARY KEY,
                market_id TEXT NOT NULL,
                timestamp_utc TEXT NOT NULL,
                evidence_source TEXT NOT NULL,
                url TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                title TEXT,
                snippet TEXT,
                cached_at TEXT NOT NULL,
                data_json TEXT NOT NULL
            )
        """)
        
        # Create indices for faster lookups
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_market_timestamp 
            ON evidence_cache(market_id, timestamp_utc)
        """)
        
        conn.commit()
        conn.close()
        
    def _make_key(
        self,
        market_id: str,
        timestamp_utc: str,
        evidence_source: str,
        url: str,
        content_hash: str
    ) -> str:
        """Create cache key from components."""
        return f"{market_id}:{timestamp_utc}:{evidence_source}:{url}:{content_hash}"
    
    def get(
        self,
        market_id: str,
        timestamp_utc: str,
        evidence_source: str,
        url: str,
        content_hash: str = ""
    ) -> Optional[Dict]:
        """
        Get cached evidence item.
        
        Args:
            market_id: Market identifier
            timestamp_utc: Timestamp in UTC
            evidence_source: Evidence source name
            url: Source URL
            content_hash: Content hash (optional)
            
        Returns:
            Cached data dict or None if not found/expired
        """
        cache_key = self._make_key(market_id, timestamp_utc, evidence_source, url, content_hash)
        
        conn = sqlite3.connect(str(self.cache_path))
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT data_json, cached_at
            FROM evidence_cache
            WHERE cache_key = ?
        """, (cache_key,))
        
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            return None
        
        data_json, cached_at = row
        
        # Check TTL
        cached_time = datetime.fromisoformat(cached_at)
        now = datetime.now(timezone.utc)
        
        if (now - cached_time).total_seconds() > self.ttl_hours * 3600:
            # Expired
            return None
        
        return json.loads(data_json)
    
    def set(
        self,
        market_id: str,
        timestamp_utc: str,
        evidence_source: str,
        url: str,
        content_hash: str,
        data: Dict
    ):
        """
        Store evidence item in cache.
        
        Args:
            market_id: Market identifier
            timestamp_utc: Timestamp in UTC
            evidence_source: Evidence source name
            url: Source URL
            content_hash: Content hash
            data: Data dict to cache
        """
        cache_key = self._make_key(market_id, timestamp_utc, evidence_source, url, content_hash)
        cached_at = datetime.now(timezone.utc).isoformat()
        
        conn = sqlite3.connect(str(self.cache_path))
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT OR REPLACE INTO evidence_cache
            (cache_key, market_id, timestamp_utc, evidence_source, url, content_hash,
             title, snippet, cached_at, data_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            cache_key,
            market_id,
            timestamp_utc,
            evidence_source,
            url,
            content_hash,
            data.get("title", ""),
            data.get("snippet", ""),
            cached_at,
            json.dumps(data)
        ))
        
        conn.commit()
        conn.close()
    
    def clear_expired(self):
        """Remove expired cache entries."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.ttl_hours)
        cutoff_str = cutoff.isoformat()
        
        conn = sqlite3.connect(str(self.cache_path))
        cursor = conn.cursor()
        
        cursor.execute("""
            DELETE FROM evidence_cache
            WHERE cached_at < ?
        """, (cutoff_str,))
        
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        
        return deleted
