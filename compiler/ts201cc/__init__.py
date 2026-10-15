"""Freestanding word-addressed C compiler for the ADSP-TS201."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'vendor'))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'linker'))
from .compiler import CompileError, compile_source
