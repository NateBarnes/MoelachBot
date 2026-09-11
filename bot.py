"""MoelachBot entry point.

Kept at the repo root so `python bot.py` keeps working and so load_dotenv()
resolves .env from this directory. find_dotenv walks up from the *calling
frame's* file and silently switches to os.getcwd() whenever a trace function is
set (which coverage does), so it must not be called from inside the package.
"""

from pathlib import Path

from dotenv import load_dotenv

from moelachbot.cli import main

if __name__ == "__main__":
    load_dotenv()
    # No .resolve(): the original anchored on Path(__file__).parent as-is.
    main(base_dir=Path(__file__).parent)
