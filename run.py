"""Entry point for TwitchDVR-to-YouTube (Tkinter desktop UI)."""


def main() -> None:
    from app.gui import main as tk_main
    tk_main()


if __name__ == "__main__":
    main()
