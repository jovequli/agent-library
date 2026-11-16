#!/usr/bin/env python3
"""Generate fictional source archives. Run after installing Agent Library."""
from pathlib import Path
import argparse
from agent_library.importers import create_demo_source

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('directory', type=Path)
args = parser.parse_args()
print(create_demo_source(args.directory))
