#!/usr/bin/env python3
"""Universal Agent Orchestrator — CLI entry point.

Usage:
    python scripts/orchestrator-cli.py build "My feature"
    python scripts/orchestrator-cli.py status
    python scripts/orchestrator-cli.py tick
"""
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.cli import main

if __name__ == "__main__":
    main()
