#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Structured error logging for translation failures and validation issues.
Logs to JSONL format for easy parsing and analysis.
"""

import json
import os
from datetime import datetime
from typing import Optional
from pathlib import Path


class ErrorLogger:
    """
    Logs translation errors and validation failures to JSONL files.
    Each log entry is a single JSON object on one line.
    """
    
    def __init__(self, log_file: str = "errors.jsonl"):
        """
        Args: 
            log_file: Path to the JSONL log file
        """
        self. log_file = Path(log_file)
        
        # Create logs directory if it doesn't exist
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        
    def _write_entry(self, entry: dict):
        """Write a single log entry to the file."""
        try:
            with open(self. log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            # Fallback to console if file writing fails
            print(f"[ERROR LOGGER] Failed to write to {self.log_file}: {e}")
            print(f"[ERROR LOGGER] Entry: {entry}")
    
    def log_translation_error(
        self,
        headline: str,
        language: str,
        error_message: str,
        user_id: Optional[str] = None
    ):
        """
        Log a translation error. 
        
        Args:
            headline: Original headline that failed to translate
            language: Target language code
            error_message: Error message from the exception
            user_id: Optional user identifier
        """
        entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "type": "translation_error",
            "headline": headline[: 200],  # Truncate very long headlines
            "language": language,
            "error":  error_message[: 500],  # Truncate very long errors
            "user_id":  user_id or "unknown"
        }
        
        self._write_entry(entry)
    
    def log_validation_failure(
        self,
        language: str,
        initial_translation: str,
        final_translation: str,
        validator1_result: str,
        validator2_result: str
    ):
        """
        Log a validation failure (when naturalization broke the translation).
        
        Args:
            language: Target language code
            initial_translation: Translation before naturalization
            final_translation: Translation after naturalization (that failed)
            validator1_result:  Result from first validator
            validator2_result:  Result from second validator
        """
        entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "type": "validation_failure",
            "language": language,
            "initial":  initial_translation[: 300],
            "final": final_translation[: 300],
            "validator1":  validator1_result,
            "validator2": validator2_result
        }
        
        self._write_entry(entry)
    
    def log_cost_estimate(
        self,
        headline: str,
        total_tokens: int,
        estimated_cost: float,
        user_id: Optional[str] = None
    ):
        """
        Log cost estimates for monitoring budget.
        
        Args:
            headline:  Headline that was translated
            total_tokens: Total tokens used (input + output)
            estimated_cost: Estimated cost in USD
            user_id: Optional user identifier
        """
        entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "type": "cost_estimate",
            "headline": headline[:200],
            "total_tokens": total_tokens,
            "estimated_cost_usd": round(estimated_cost, 4),
            "user_id":  user_id or "unknown"
        }
        
        self._write_entry(entry)


# Global instance for easy access
_global_logger = None

def get_error_logger(log_file: str = "errors.jsonl") -> ErrorLogger:
    """Get or create the global error logger instance."""
    global _global_logger
    if _global_logger is None: 
        _global_logger = ErrorLogger(log_file)
    return _global_logger