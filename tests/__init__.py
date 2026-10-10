# tests/__init__.py — Centralized test environment defaults (fail-closed)
import os

# Ensure all tests and subprocesses default to test environment
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("TESTING", "true")
