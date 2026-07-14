"""Entry point for TwitchDVR-to-YouTube.

Default interface is Studio (the modern web UI in a native window);
`--classic` or Settings → ui_mode=classic starts the Tkinter interface.
"""
import sys


def main() -> None:
    from app import config
    cfg = config.load_config()
    if "--classic" in sys.argv:
        mode = "classic"
    elif "--studio" in sys.argv:
        mode = "studio"
    else:
        mode = cfg.get("ui_mode", "studio")

    if mode == "studio":
        try:
            from app.studio import main as studio_main
            studio_main()
            return
        except Exception as exc:            # missing WebView2 runtime etc.
            print(f"Studio mode unavailable ({exc}); falling back to classic UI.")

    from app.gui import main as tk_main
    tk_main()


if __name__ == "__main__":
    main()
