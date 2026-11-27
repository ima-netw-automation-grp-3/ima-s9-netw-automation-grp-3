"""Tests du projet : la collection locale est importée depuis collections/ (sans installation)."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "collections"))
