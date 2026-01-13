#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redis-based caching for translations.
Dramatically reduces API calls and costs.
"""

import json
import hashlib
from typing import Optional, Dict, Any
import redis
from datetime import timedelta
from urllib.parse import urlparse


class CacheManager:
    """
    Manages Redis cache for translations.
    Cache key format: "translation:{hash(text+lang)}"
    """
    
    def __init__(
        self,
        redis_host: str = "localhost",
        redis_port: int = 6379,
        redis_db:  int = 0,
        ttl_hours: int = 24,
        redis_url: Optional[str] = None,
    ):
        """
        Args:
            redis_host: Redis server hostname
            redis_port:  Redis server port
            redis_db: Redis database number
            ttl_hours: Cache time-to-live in hours
        """
        display_host = f"{redis_host}:{redis_port}"
        if redis_url:
            parsed = urlparse(redis_url)
            display_host = parsed.hostname or display_host
            if parsed.port:
                display_host = f"{display_host}:{parsed.port}"
            self.client = redis.Redis.from_url(
                redis_url,
                decode_responses=True,
                max_connections=100,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
        else:
            self.client = redis.Redis(
                host=redis_host,
                port=redis_port,
                db=redis_db,
                decode_responses=True,
                max_connections=100,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
        self.ttl = timedelta(hours=ttl_hours)
        
        # Test connection
        try:
            self.client.ping()
            print(f"✅ Redis connected:  {display_host}")
        except redis.ConnectionError as e:
            print(f"⚠️  Redis connection failed:  {e}")
            print("   Caching will be disabled")
            self.client = None
    
    def _make_key(self, text: str, lang: str, translation_type: str = "headline") -> str:
        """
        Generate cache key from text + language. 
        Uses SHA256 hash to handle special characters.
        """
        raw = f"{translation_type}:{text}:{lang}"
        hash_val = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return f"trans:{translation_type}:{lang}:{hash_val}"
    
    def get_translation(
        self,
        text: str,
        lang: str,
        translation_type: str = "headline"
    ) -> Optional[Dict[str, Any]]:
        """
        Get cached translation if it exists.
        
        Returns:
            Cached result dict, or None if not found
        """
        if not self.client:
            return None
        
        try:
            key = self._make_key(text, lang, translation_type)
            cached = self.client.get(key)
            
            if cached: 
                result = json.loads(cached)
                print(f"✅ Cache HIT: {lang} ({translation_type})")
                return result
            else:
                print(f"❌ Cache MISS: {lang} ({translation_type})")
                return None
                
        except Exception as e:
            print(f"⚠️  Cache get error: {e}")
            return None
    
    def set_translation(
        self,
        text: str,
        lang: str,
        result: Dict[str, Any],
        translation_type: str = "headline"
    ):
        """
        Store translation result in cache.
        
        Args:
            text: Original text
            lang: Target language
            result: Translation result dict
            translation_type: "headline" or "description"
        """
        if not self.client:
            return
        
        try:
            key = self._make_key(text, lang, translation_type)
            value = json.dumps(result, ensure_ascii=False)
            
            self.client.setex(
                key,
                self.ttl,
                value
            )
            print(f"💾 Cached: {lang} ({translation_type})")
            
        except Exception as e:
            print(f"⚠️  Cache set error: {e}")
    
    def invalidate_translation(self, text: str, lang: str, translation_type: str = "headline"):
        """Manually invalidate a cached translation."""
        if not self. client:
            return
        
        try:
            key = self._make_key(text, lang, translation_type)
            self.client.delete(key)
            print(f"🗑️  Cache invalidated: {lang} ({translation_type})")
        except Exception as e:
            print(f"⚠️  Cache invalidate error: {e}")

    def clear_all_translations(self):
        """Clear ALL cached translations."""
        if not self. client:
            return
        
        try:
            # Find all translation cache keys
            keys = self.client.keys("trans:*")
            if keys:
                self.client.delete(*keys)
                print(f"🗑️  Cleared {len(keys)} cached translations")
            else:
                print("ℹ️  No cached translations to clear")
        except Exception as e:
            print(f"⚠️  Cache clear error: {e}")
            
    
    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        if not self.client:
            return {"enabled": False}
        
        try:
            info = self.client.info("stats")
            return {
                "enabled": True,
                "keys":  self.client.dbsize(),
                "hits": info.get("keyspace_hits", 0),
                "misses": info.get("keyspace_misses", 0),
                "hit_rate": info.get("keyspace_hits", 0) / max(1, info.get("keyspace_hits", 0) + info.get("keyspace_misses", 0))
            }
        except Exception as e: 
            return {"enabled": False, "error": str(e)}


# Global instance
_global_cache: Optional[CacheManager] = None

def get_cache_manager() -> CacheManager:
    """Get or create global cache manager."""
    global _global_cache
    if _global_cache is None:
        import os
        redis_url = os.getenv("REDIS_URL")
        _global_cache = CacheManager(
            redis_host=os.getenv("REDIS_HOST", "localhost"),
            redis_port=int(os.getenv("REDIS_PORT", 6379)),
            redis_db=int(os.getenv("REDIS_DB", 0)),
            ttl_hours=int(os.getenv("CACHE_TTL_HOURS", 24)),
            redis_url=redis_url,
        )
    return _global_cache
