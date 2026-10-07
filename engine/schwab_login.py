"""Run on your own computer to log in to Schwab and print the token for GitHub.

    pip install schwab-py
    python -m engine.schwab_login

Paste the printed JSON into the SCHWAB_TOKEN_JSON secret of your GitHub repo.
Schwab makes you redo this every 7 days.
"""
import getpass
from pathlib import Path


def main():
    from schwab.auth import client_from_manual_flow

    key = input("Schwab app key: ").strip()
    secret = getpass.getpass("Schwab app secret: ").strip()
    callback = input("Callback URL [https://127.0.0.1:8182]: ").strip() or "https://127.0.0.1:8182"
    token_path = Path("schwab_token.json")
    client_from_manual_flow(key, secret, callback, str(token_path))
    print("\nCopy everything between the lines into the SCHWAB_TOKEN_JSON secret:\n" + "-" * 60)
    print(token_path.read_text())
    print("-" * 60)
    token_path.unlink()


if __name__ == "__main__":
    main()
