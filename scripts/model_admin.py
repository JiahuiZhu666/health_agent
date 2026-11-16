#!/usr/bin/env python3
"""Local operator controls. Never reads .env or sends a model request."""
import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from nhis_agent.budget import ModelBudget
from nhis_agent.telemetry import Telemetry


def nonnegative(value):
    if not re.fullmatch(r'[0-9]{1,12}',value):
        raise argparse.ArgumentTypeError('Use a nonnegative bounded integer.')
    return int(value)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default=os.environ.get('NHIS_STATE_DIR') or str(Path(__file__).resolve().parents[1]))
    commands=parser.add_subparsers(dest='command',required=True)
    for name in ('status','disable','enable','reservations','events','metrics'):
        command=commands.add_parser(name)
        if name in ('reservations','events'):command.add_argument('--limit',type=int,default=100)
        if name=='reservations':command.add_argument('--state',choices=('held','reserved','reconciled','released','all'),default='held')
    reconcile=commands.add_parser('reconcile',help='Explicitly resolve held usage using independent provider evidence.')
    reconcile.add_argument('--reservation-id',required=True)
    reconcile.add_argument('--input-tokens',type=nonnegative)
    reconcile.add_argument('--output-tokens',type=nonnegative)
    reconcile.add_argument('--confirmed-not-sent',action='store_true',help='Release only after independently confirming that the request was never sent.')
    reconcile.add_argument('--evidence-reference',required=True,help='An opaque local/provider audit reference; do not enter a prompt or credential.')
    args=parser.parse_args(argv)
    if args.command=='reconcile':
        if not re.fullmatch(r'[a-f0-9]{32}',args.reservation_id):parser.error('Use a valid reservation ID.')
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}',args.evidence_reference) or args.evidence_reference.lower().startswith(('sk-','bearer')):
            parser.error('Use a bounded opaque evidence reference without credentials.')
        if args.confirmed_not_sent:
            if args.input_tokens is not None or args.output_tokens is not None:parser.error('Choose confirmed-not-sent or complete usage counts.')
        elif args.input_tokens is None or args.output_tokens is None:
            parser.error('Both observed input and output token counts are required.')
    try:
        if args.command=='metrics':
            from nhis_agent.metrics import snapshot
            print(json.dumps(snapshot(args.root),indent=2,sort_keys=True,allow_nan=False))
            return 0
        telemetry=Telemetry(args.root)
        budget=ModelBudget(args.root)
        if args.command=='status':result=budget.status()
        elif args.command in ('enable','disable'):
            result=budget.set_enabled(args.command=='enable')
            telemetry.emit('operator.model_'+args.command,{'status':args.command})
        elif args.command=='reservations':result=budget.reservations(args.state,args.limit)
        elif args.command=='events':result=telemetry.recent(args.limit)
        else:
            usage=None if args.confirmed_not_sent else {'input_tokens':args.input_tokens,'output_tokens':args.output_tokens}
            row=budget.reconcile(args.reservation_id,usage,not_sent=args.confirmed_not_sent)
            telemetry.emit('operator.budget_reconciled',{'reservation_id':args.reservation_id,
                'status':row['state'],'request_id':args.evidence_reference})
            result={key:row[key] for key in ('id','state','calls','input_tokens','output_tokens','cost_micros')}
        print(json.dumps(result,indent=2,sort_keys=True,allow_nan=False))
        return 0
    except (OSError,ValueError,RuntimeError,sqlite3.Error):
        print('{"status":"failed","error_code":"operator_action_failed"}',file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())
