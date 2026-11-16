#!/usr/bin/env python3
"""Explicit local state operations. Retention previews by default; no API/R calls."""
import argparse
import json
import os
import sqlite3
from pathlib import Path
import sys
import tarfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nhis_agent.storage_policy import (
    StoragePolicy, create_backup, restore_backup, validate_state,
    plan_retention, apply_retention,
)


def main(argv=None):
    parser=argparse.ArgumentParser(description='Inspect, back up and restore private agent state. Credentials and official survey inputs are excluded.')
    commands=parser.add_subparsers(dest='command',required=True)
    default_root=os.environ.get('NHIS_STATE_DIR') or str(Path(__file__).resolve().parents[1])
    for name,description in [('status','Show managed byte counts and limits.'),
                             ('validate','Validate an independent recovery copy.'),
                             ('backup','Create a new archive using SQLite online snapshots.'),
                             ('retention','Preview retention candidates; deletion requires --apply.')]:
        sub=commands.add_parser(name,help=description)
        sub.add_argument('--root',default=default_root,help='State directory; this never reads .env.')
        if name=='backup':sub.add_argument('--destination',required=True,help='New archive path outside managed conversations/runs/cache.')
        if name=='retention':sub.add_argument('--apply',action='store_true',help='Explicitly apply revalidated candidates. Pause application admission first. Audit evidence is preserved.')
    restore=commands.add_parser('restore',help='Validate and publish a separate new state directory. Never overwrite live state.')
    restore.add_argument('--archive',required=True)
    restore.add_argument('--destination',required=True,help='An absent independent destination directory.')
    args=parser.parse_args(argv)
    try:
        if args.command=='status':result=StoragePolicy(args.root).status()
        elif args.command=='validate':result=validate_state(args.root)
        elif args.command=='backup':result=create_backup(args.root,args.destination)
        elif args.command=='restore':result=restore_backup(args.archive,args.destination)
        else:
            plan=plan_retention(args.root)
            result=apply_retention(args.root,plan) if args.apply else plan
        print(json.dumps(result,indent=2,sort_keys=True,allow_nan=False))
        return 0
    except (OSError,ValueError,RuntimeError,sqlite3.Error,tarfile.TarError,EOFError) as error:
        # Deliberately no traceback, local variables, file bodies or configuration.
        print(json.dumps({'status':'failed','error_code':getattr(error,'code','state_operation_failed'),
                          'message':'The state operation did not complete. Check the destination, state integrity, storage limits, and whether application writes have been paused.'}),file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())
