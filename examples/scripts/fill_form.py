"""Fill a sign-in form: the email field and the "Remember me" checkbox, in one call.

    python examples/scripts/fill_form.py --fake --email ada@example.com

Password fields are off limits to AXPilot by design (the person types passwords), so this script
fills everything else and leaves the password to you.
"""

from __future__ import annotations

import argparse
import sys

from axpilot import Desktop
from axpilot.errors import AXPilotError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fake", action="store_true", help="use the simulated desktop")
    parser.add_argument("--app", default="Login Demo", help="the app showing the form")
    parser.add_argument("--email", default="ada@example.com")
    args = parser.parse_args(argv)
    mac = Desktop.fake() if args.fake else Desktop.connect()
    try:
        with mac:
            mac.app(args.app).activate()
            form = mac.snapshot(args.app)
            email = form.find("Email", role="field")
            remember = form.find("Remember me", role="checkbox")
            if email is None or remember is None:
                print("This app shows no email field and Remember me checkbox.", file=sys.stderr)
                return 1
            mac.fill_form({email.ref: args.email, remember.ref: True}, snapshot_id=form.snapshot_id)
            check = mac.snapshot(args.app)
    except AXPilotError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    filled = check.find("Email", role="field")
    ticked = check.find("Remember me", role="checkbox")
    ok = (
        filled is not None
        and filled.element is not None
        and filled.element.value == args.email
        and ticked is not None
        and ticked.element is not None
        and ticked.element.checked
    )
    print("form filled; type the password yourself" if ok else "the form does not show the values")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
