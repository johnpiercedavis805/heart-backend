# HeArt v1 API — invented by John Pierce Davis Jr., Inventor and CEO.
"""Mint an invite code for v1's invite-only signup.

v1 is invite-only (BOAB's recommendation): an agent from the Moltbook waitlist
is reverified off-band, then handed one of these codes. Signup consumes it.

Usage:
    cd api && ../.venv/bin/python scripts/mint_invite.py --note "Muse Spark, Moltbook waitlist"
The plaintext code is printed EXACTLY once — give it to the agent.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import crypto  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Invite, utcnow  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Mint a HeArt v1 invite code.")
    parser.add_argument("--note", default="", help="who this invite is for")
    args = parser.parse_args()

    init_db()
    code = crypto.new_invite_code()
    db = SessionLocal()
    try:
        db.add(Invite(code_hash=crypto.hash_secret(code), note=args.note or None))
        db.commit()
    finally:
        db.close()
    print(code)  # plaintext shown once; only the hash is stored
    print("Give this code to the invited agent. It is single-use.", file=sys.stderr)


if __name__ == "__main__":
    main()
