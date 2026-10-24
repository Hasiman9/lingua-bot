"""Offline preview/status/explicit activation for the temporary new-subscriber reward."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3

from dotenv import load_dotenv

from rewards import RewardStore


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['preview', 'status', 'activate'])
    parser.add_argument('--confirm', action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'activate' and not args.confirm:
        parser.error('Activation requires --confirm; use preview first')
    base = Path(__file__).resolve().parent
    load_dotenv(base / '.env')
    path = Path(os.getenv('DATABASE_PATH', 'data/bot.sqlite3'))
    if not path.is_absolute():
        path = base / path
    print(f'Database: {path}')
    store = RewardStore(path)
    try:
        if args.command == 'activate':
            store.activate()
        result = store.preview()
        for key in ('activated_at', 'expires_at'):
            if result[key] is not None:
                result[key] = datetime.fromtimestamp(result[key], timezone.utc).isoformat()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print('Only first successful verifications during the 72-hour window enroll. No existing-user broadcast.')
    except (ValueError, sqlite3.Error) as exc:
        parser.exit(1, f'{exc}\nStart the updated bot once to initialize the reward tables before activation.\n')


if __name__ == '__main__':
    main()
