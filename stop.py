"""Stop minicode using the same project environment as start.py."""

from start import main

if __name__ == "__main__":
    raise SystemExit(main(stop=True))
