"""PyInstaller-friendly entry point: `python run.py` also works."""

from app.main import main

if __name__ == "__main__":
    raise SystemExit(main())
