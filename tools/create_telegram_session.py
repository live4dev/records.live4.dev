#!/usr/bin/env python3
"""Create the Telethon StringSession used by the import workflow."""

from __future__ import annotations

import asyncio
import getpass
import os


async def create_session() -> None:
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError as error:
        raise SystemExit("Install dependencies first: pip install -r requirements.txt") from error

    api_id = os.getenv("TELEGRAM_API_ID") or input("Telegram API ID: ").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH") or getpass.getpass("Telegram API hash: ").strip()
    if not api_id or not api_hash:
        raise SystemExit("API ID and API hash are required")

    client = TelegramClient(StringSession(), int(api_id), api_hash)
    await client.start(
        phone=lambda: input("Phone number: ").strip(),
        code_callback=lambda: input("Login code: ").strip(),
        password=lambda: getpass.getpass("2FA password (if enabled): "),
    )
    try:
        print("\nSave this value as the TELEGRAM_SESSION GitHub Actions secret:\n")
        print(client.session.save())
        print("\nTreat it like a password. Do not commit it to the repository.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(create_session())
