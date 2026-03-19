#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Global user queue management for handling concurrent translation requests.
Ensures fair scheduling and prevents API rate limit violations. 
"""

import asyncio
import threading
from typing import Optional, List, Dict, Any
from datetime import datetime


class UserQueue:
    """
    Manages a queue of translation requests from multiple users.
    Limits concurrent users to prevent API rate limit violations. 
    """
    
    def __init__(self, max_concurrent_users: int = 10):
        """
        Args: 
            max_concurrent_users: Maximum number of users processing simultaneously
        """
        self.max_concurrent = max_concurrent_users
        self._semaphore = None
        self.active_users: List[str] = []
        self._lock = threading.Lock()

    @property
    def semaphore(self):
        """Lazily create the semaphore inside the running event loop (Python 3.9 compat)."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrent)
        return self._semaphore

    async def acquire(self, user_id: str):
        """
        Acquire a slot for this user.
        Blocks if max concurrent users already active.
        
        Args:
            user_id:  Identifier for the user making the request
        """
        await self.semaphore.acquire()
        with self._lock:
            self.active_users.append(user_id)
            print(f"[QUEUE] User {user_id} started. Active users: {len(self.active_users)}/{self.max_concurrent}")
        
    def release(self, user_id: str):
        """
        Release the slot for this user.
        
        Args:
            user_id: Identifier for the user
        """
        with self._lock:
            if user_id in self.active_users:
                self.active_users.remove(user_id)
            user_count = len(self.active_users)
        
        self.semaphore.release()
        print(f"[QUEUE] User {user_id} completed. Active users: {user_count}/{self.max_concurrent}")
    
    def get_queue_position(self, user_id: str) -> Optional[int]:
        """
        Get the position of a user in the queue. 
        
        Args:
            user_id: Identifier for the user
            
        Returns:
            Position in queue (0-indexed), or None if user is active
        """
        with self._lock:
            if user_id in self.active_users:
                return None  # User is already processing
        
        # Note: This is a simplified version. A full implementation would
        # track waiting users and their order.
        return 0


# Global queue instance
_global_queue: Optional[UserQueue] = None

def get_user_queue(max_concurrent: int = 10) -> UserQueue:
    """Get or create the global user queue instance."""
    global _global_queue
    if _global_queue is None:
        _global_queue = UserQueue(max_concurrent)
    return _global_queue
