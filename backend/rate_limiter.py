#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rate limiting and retry logic for OpenAI API calls. 
Ensures we stay under API limits and handle transient failures gracefully.
"""

import time
import threading
from typing import Callable, Any
from functools import wraps


class RateLimiter:  
    """
    Simple rate limiter that ensures minimum time between API calls. 
    Thread-safe for concurrent usage. 
    """
    
    def __init__(self, calls_per_second:  float = 10.0):
        """
        Args:
            calls_per_second: Maximum API calls per second (default: 10)
        """
        self. min_interval = 1.0 / calls_per_second
        self.last_call_time = 0.0
        self._lock = threading.Lock()  # Thread lock for safety

    def wait_if_needed(self):
        """
        Blocks execution if we're calling too fast.
        Ensures minimum time between calls. 
        """
        with self._lock:  # ✅ Protect shared state with lock
            current_time = time.time()
            time_since_last = current_time - self.last_call_time
            
            if time_since_last < self.min_interval:
                sleep_time = self.min_interval - time_since_last
                time.sleep(sleep_time)
            
            self. last_call_time = time. time()


class RetryHandler: 
    """
    Handles retries with exponential backoff for transient API failures.
    """
    
    def __init__(self, max_retries: int = 3, backoff_base:  float = 2.0):
        """
        Args: 
            max_retries: Maximum number of retry attempts
            backoff_base: Base for exponential backoff (seconds)
        """
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        
    def execute(self, func: Callable[[], Any]) -> Any:
        """
        Execute a function with retry logic. 
        
        Args:
            func: Function to execute (should be a callable with no args)
            
        Returns: 
            Result from the function
            
        Raises:
            Exception: If all retries are exhausted
        """
        last_exception = None
        
        for attempt in range(self.max_retries):
            try:
                return func()
            except Exception as e: 
                last_exception = e
                
                # Don't retry on the last attempt
                if attempt == self.max_retries - 1:
                    raise last_exception
                
                # Exponential backoff:  2^0=1s, 2^1=2s, 2^2=4s
                wait_time = self.backoff_base ** attempt
                
                # Log retry attempt
                error_msg = str(e)[:100]
                print(f"[RETRY] Attempt {attempt + 1}/{self.max_retries} failed: {error_msg}")
                print(f"[RETRY] Waiting {wait_time}s before retry...")
                
                time.sleep(wait_time)
        
        # This should never be reached, but just in case
        raise last_exception
    
    async def execute_async(self, func: Callable[[], Any]) -> Any:
        """Execute an async function with retry logic."""
        import asyncio
        last_exception = None
        
        for attempt in range(self.max_retries):
            try:
                return await func()
            except Exception as e: 
                last_exception = e
                if attempt == self.max_retries - 1:
                    raise last_exception
                
                wait_time = self.backoff_base ** attempt
                error_msg = str(e)[:100]
                print(f"[RETRY] Attempt {attempt + 1}/{self.max_retries} failed: {error_msg}")
                print(f"[RETRY] Waiting {wait_time}s before retry...")
                await asyncio.sleep(wait_time)
        
        raise last_exception