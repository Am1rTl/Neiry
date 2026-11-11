#!/usr/bin/env python3
"""Portable, offline-friendly hash check for the project's active venv."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
REQUIREMENTS=ROOT/'requirements.txt'
STAMP=ROOT/'.runtime'/'requirements.sha256'
MODULES=('fastapi','uvicorn','websockets','numpy','scipy','pandas','reportlab')


def fingerprint() -> str:
    contents=REQUIREMENTS.read_bytes()+sys.version_info[:2].__repr__().encode('ascii')
    return hashlib.sha256(contents).hexdigest()


def ready() -> bool:
    try: recorded=STAMP.read_text().strip()
    except FileNotFoundError:return False
    return recorded==fingerprint() and all(importlib.util.find_spec(m) for m in MODULES)


def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument('--stamp',action='store_true')
    args=parser.parse_args()
    if args.stamp:
        STAMP.parent.mkdir(parents=True,exist_ok=True)
        STAMP.write_text(fingerprint()+'\n')
        return 0
    return 0 if ready() else 1

if __name__=='__main__':raise SystemExit(main())
