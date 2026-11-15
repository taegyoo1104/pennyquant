"""Audit stored backfill minutes without provider calls or rewriting observations."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from .bars import session_bounds
from .history import HistoryStore, signature


def ranges(minutes):
    result=[]
    for minute in sorted(minutes):
        if result and minute==result[-1][1]+1:result[-1][1]=minute
        else:result.append([minute,minute])
    return result


def diagnose_history(repo, backfill):
    symbols=backfill.get('symbols',[])
    dates=backfill.get('session_dates',[])
    if not symbols or not dates:
        return {'status':'NO_BACKFILL_PLAN','summary':{},'symbols':[],'incomplete_sessions':[]}
    store=HistoryStore(repo)
    incomplete=[]
    symbol_rows=[]
    counts=Counter({key:0 for key in ('required_symbol_days','complete_symbol_days','expected_minutes',
        'received_unique_minutes','missing_minutes','conflicting_minutes','out_of_session_or_off_boundary_rows','unreadable_sessions')})
    offsets=Counter()
    for symbol in symbols:
        stats={'symbol':symbol,'required_sessions':len(dates),'complete_sessions':0,
               'missing_minutes':0,'conflicting_minutes':0,'unreadable_sessions':0,
               'complete_prefix_sessions':{str(n):0 for n in (5,75,120,240,390)}}
        for day in dates:
            bounds=session_bounds(datetime.fromisoformat(day+'T12:00:00-05:00'))
            if bounds is None:raise ValueError('저장된 수집 계획의 거래일을 확인하세요.')
            opened,closed=bounds
            expected=int((closed-opened).total_seconds()/60)
            counts['required_symbol_days']+=1
            counts['expected_minutes']+=expected
            try:
                row,bars=store.get(symbol,day)
            except (ValueError,TypeError,KeyError,AttributeError,OverflowError) as exc:
                stats['unreadable_sessions']+=1
                counts['unreadable_sessions']+=1
                incomplete.append({'symbol':symbol,'session':day,'error_type':type(exc).__name__,
                    'status':'STORED_DATA_INVALID'})
                continue
            versions=defaultdict(set)
            invalid=0
            for bar in bars:
                elapsed=(bar.timestamp-opened).total_seconds()/60
                if not elapsed.is_integer() or not 1<=elapsed<=expected:
                    invalid+=1
                    continue
                versions[int(elapsed)].add(signature(bar)[1:])
            missing=set(range(1,expected+1))-versions.keys()
            conflicts={i for i,values in versions.items() if len(values)>1}
            bad=missing|conflicts
            complete=not bad and not invalid
            counts['complete_symbol_days']+=int(complete)
            counts['received_unique_minutes']+=len(versions)
            counts['missing_minutes']+=len(missing)
            counts['conflicting_minutes']+=len(conflicts)
            counts['out_of_session_or_off_boundary_rows']+=invalid
            stats['complete_sessions']+=int(complete)
            stats['missing_minutes']+=len(missing)
            stats['conflicting_minutes']+=len(conflicts)
            for cutoff in stats['complete_prefix_sessions']:
                n=int(cutoff)
                stats['complete_prefix_sessions'][cutoff]+=int(expected>=n and not invalid and not any(i<=n for i in bad))
            offsets.update(missing)
            if not complete:
                progress=(row or {}).get('progress',{})
                incomplete.append({'symbol':symbol,'session':day,'status':'INCOMPLETE',
                    'expected_minutes':expected,'received_minutes':len(versions),
                    'missing_minutes':len(missing),'conflicting_minutes':len(conflicts),
                    'missing_elapsed_minute_ranges':ranges(missing),
                    'conflicting_elapsed_minute_ranges':ranges(conflicts),
                    'first_problem_elapsed_minute':min(bad) if bad else None,
                    'first_missing_bar_utc':(opened+timedelta(minutes=min(missing))).isoformat() if missing else None,
                    'session_open_utc':opened.isoformat(),'session_close_utc':closed.isoformat(),
                    'out_of_session_or_off_boundary_rows':invalid,
                    'stop_reason':progress.get('status','NO_RECORD'),'pages':progress.get('pages',0),
                    'next_cursor':progress.get('cursor'),'stored_received_at':(row or {}).get('received_at')})
        symbol_rows.append(stats)
        store.cache.clear()
    summary={**counts,'incomplete_symbol_days':counts['required_symbol_days']-counts['complete_symbol_days'],
             'fully_complete_symbols':sum(s['complete_sessions']==s['required_sessions'] for s in symbol_rows),
             'required_symbols':len(symbols),'reported_complete_symbol_days':backfill.get('complete_symbol_days'),
             'missing_offset_frequencies':[{'elapsed_minute':i,'symbol_days':n} for i,n in offsets.most_common(15)]}
    return {'status':'COMPLETE' if not incomplete else 'INCOMPLETE','summary':summary,
            'scope':'last_backfill_plan','session_dates':dates,'symbols':symbol_rows,
            'incomplete_sessions':incomplete,'provider_calls':0,'records_modified':False,
            'minute_offsets':'1 means the first bar ending one minute after regular-session open',
            'note':'Complete prefixes describe stored inputs only; they do not verify volume comparability or authorize entries.'}
