"""App version.

Release builds get this file overwritten by CI with the exact version.
Source / Docker deployments fall back to the repo's VERSION file, so the
UI shows the real release version instead of 0.0.0-dev.
"""
__version__ = "0.0.0-dev"

if __version__.startswith("0.0.0"):
    try:
        import sys
        from pathlib import Path
        _root = Path(getattr(sys, "_MEIPASS",
                             Path(__file__).resolve().parent.parent))
        _version_file = _root / "VERSION"
        if _version_file.exists():
            __version__ = _version_file.read_text(encoding="utf-8").strip() \
                or __version__
    except Exception:
        pass
