"""Trinetra Capital AI — entrypoint.

    python main.py

Starts the interactive multi-agent trading CLI. Trading mode (paper/live),
Groww credentials and safety limits are all read from your .env via
trinetra.config. Run `python connect_groww.py` first to connect Groww.
"""

import os

# Trinetra never uses PyTorch, but langchain_core probes `transformers` at import
# time, and transformers drags in the whole torch/CUDA DLL stack if it is present
# in the environment — ~20s of startup for nothing. Telling transformers to skip
# its torch backend keeps startup fast when torch happens to be installed
# alongside (e.g. for an unrelated tool). Must be set before langchain imports.
os.environ.setdefault("USE_TORCH", "0")

from trinetra.cli import run  # noqa: E402

if __name__ == "__main__":
    run()