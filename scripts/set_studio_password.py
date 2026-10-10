"""Set (or change) the shared studio password used to sign in and to create accounts.

    .venv/Scripts/python scripts/set_studio_password.py            # asks for it (not shown on screen)

Only a PBKDF2 hash is stored, in the app_settings table of DATABASE_URL (Neon). The password itself is never saved
in the code, the repo or .env. Changing it doesn't sign anyone out; personal accounts keep their own passwords.
"""
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import auth, config  # noqa: E402
from backend.store import default_store  # noqa: E402


def main():
    if not config.DATABASE_URL:
        sys.exit("DATABASE_URL is empty in .env")
    pw = sys.argv[1] if len(sys.argv) > 1 else getpass.getpass("New studio password: ")
    if len(pw) < 8:
        sys.exit("Use at least 8 characters.")
    store = default_store()
    store.set_setting("studio_password_hash", auth.hash_password(pw))
    assert auth.studio_password_ok(store, pw)
    print("Studio password set.")


if __name__ == "__main__":
    main()
