"""Offline operator commands; never imports or starts the Telegram application."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3

from dotenv import load_dotenv

from campaigns import CampaignStore


def utc_time(text):
    try:
        value = datetime.fromisoformat(text.replace('Z', '+00:00'))
        if value.tzinfo is None or value.utcoffset().total_seconds() != 0:
            raise ValueError()
    except ValueError:
        raise argparse.ArgumentTypeError('Use an explicit UTC timestamp, e.g. 2026-12-01T09:00:00Z') from None
    return value.timestamp()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['preview', 'activate'])
    parser.add_argument('--id', default='materials-immediate-v1')
    timing = parser.add_mutually_exclusive_group()
    timing.add_argument('--immediate', action='store_true', help='Due immediately on activation (default)')
    timing.add_argument('--at', type=utc_time, help='Optional future UTC date instead of immediate delivery')
    parser.add_argument('--replace-active', action='store_true', help='Explicitly supersede an unfinished campaign; preserve its history')
    parser.add_argument('--confirm', action='store_true', help='Required to activate')
    args = parser.parse_args(argv)
    base = Path(__file__).resolve().parent
    load_dotenv(base / '.env')
    path = Path(os.getenv('DATABASE_PATH', 'data/bot.sqlite3'))
    if not path.is_absolute():
        path = base / path
    store = CampaignStore(path)
    print(f'Database: {path}')
    try:
        if args.command == 'activate':
            if not args.confirm:
                parser.error('Activation requires --confirm; use preview to inspect first')
            due = store.activate(args.id, args.at, replace_active=args.replace_active)
            print('Activated; scheduled UTC:', datetime.fromtimestamp(due, timezone.utc).isoformat())
        result = store.preview()
        for campaign in result['campaigns']:
            for key in ('activated_at', 'scheduled_at', 'completed_at'):
                if campaign[key] is not None:
                    campaign[key] = datetime.fromtimestamp(campaign[key], timezone.utc).isoformat()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result['campaigns']:
            print('Not activated. Scheduled time: none. Pending deliveries: 0.')
        print('Eligible = previously verified and not delivered/uncertain; all FIVE current channels are rechecked at send time.')
    except (ValueError, sqlite3.Error) as exc:
        parser.exit(1, f'{exc}\nDeploy and start the updated bot once before activation.\n')


if __name__ == '__main__':
    main()
