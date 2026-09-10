"""Application entry point."""

from .app import App

def main():
    App().mainloop()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
