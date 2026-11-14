"""Finish an interrupted storage rehearsal on a copied archive after a view-only fix.

Business source/config hashes must match the completed execution/research phases.
The report retains their provenance; this does not pretend to rerun those phases.
"""
from pathlib import Path
import argparse,contextlib,copy,json,os,resource,shutil,socket,subprocess,sys,tempfile,time,traceback
from datetime import datetime
import pytest
import run_ontology_rehearsal as H


def run(source,output,baseline):
    parent=json.loads((source/'report.json').read_text())
    for phase in ('1-3_execution','4-7_research'):
        assert parent['phase_timings'][phase]['status']=='completed',phase
    output.mkdir(parents=True,exist_ok=False)
    runtime,hashes=H.copy_runtime(output)
    changed={name for name in hashes.keys()|parent['runtime_source_sha256'].keys()
             if name.startswith(('src/','config/')) and hashes.get(name)!=parent['runtime_source_sha256'].get(name)}
    assert changed <= {'src/trace_query.py','src/trace_pages.py'},changed
    work=runtime/'work'
    shutil.copytree(Path(parent['work']),work)
    execution=json.loads((work/'rehearsal_execution.json').read_text())
    stages=parent['steps']['4-7']
    research={'review_id':stages['5']['review_id'],'proposal_id':stages['6']['proposal_id'],
              'result_id':stages['7']['portfolio']['result_id']}
    report=copy.deepcopy(parent)
    report.update(status='running',output=str(output),work=str(work),runtime_agent=str(runtime),
        runtime_source_sha256=hashes,phase_sources={'1-3_execution':str(source),'4-7_research':str(source),
                                                 '8_view':str(output),'fault_scenarios':str(output)},
        continuation={'parent_report':str(source/'report.json'),'changed_business_sources':[],
                      'changed_view_sources':sorted(changed),'copied_records':True,
                      'interrupted_view_timing':parent['phase_timings'].get('8_view'),
                      'metrics_scope':'Cumulative counters/RSS belong to the process indicated by phase_sources.'})
    report['phase_timings'].pop('8_view',None)
    protected=[H.AGENT/'config',H.AGENT/'work',H.AGENT.parent/'docs/data',H.AGENT.parent/'live']
    original=H.digest_files(protected,output)
    guard={'active':False,'attempts':[]};allowed=output.resolve()
    def check_write(raw):
        if isinstance(raw,int) or raw is None:return
        path=Path(os.fsdecode(raw)).absolute().resolve()
        if path==Path(os.devnull) or path.is_relative_to(allowed):return
        guard['attempts'].append({'kind':'write_outside_rehearsal','path':str(path)})
        raise PermissionError('Write outside isolated continuation')
    def audit(event,args):
        if not guard['active']:return
        if event in ('socket.connect','socket.getaddrinfo','subprocess.Popen','os.system','os.posix_spawn'):
            guard['attempts'].append({'kind':event});raise PermissionError('External I/O forbidden')
        if event=='open':
            path,mode,flags=args
            if (isinstance(mode,str) and any(x in mode for x in 'wax+')) or flags&(os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC):check_write(path)
        elif event in ('os.mkdir','os.remove','os.rmdir','os.chmod','os.truncate'):check_write(args[0])
        elif event in ('os.rename','os.link','os.symlink'):
            check_write(args[0]);check_write(args[1])
        elif event=='sqlite3.connect':
            from urllib.parse import urlsplit,unquote
            check_write(unquote(urlsplit(args[0]).path) if str(args[0]).startswith('file:') else args[0])
    sys.addaudithook(audit)
    clock={'now':datetime.fromisoformat('2026-11-14T10:00:00+09:00')}
    with pytest.MonkeyPatch.context() as mp:
        temporary=output/'tmp';temporary.mkdir()
        mp.setattr(tempfile,'tempdir',str(temporary))
        for name,value in {'TMPDIR':str(temporary),'MPLCONFIGDIR':str(temporary/'matplotlib'),
                           'MPL_IGNORE_SYSTEM_FONTS':'1','XDG_CACHE_HOME':str(temporary/'cache')}.items():mp.setenv(name,value)
        for name in list(os.environ):
            if name.startswith(('KIWOOM_','KRX_','NAVER_','ANTHROPIC_','OPENAI_','RUN_','PROPOSAL_','CASE_REVIEW_')) or name in ('ACCOUNT_NAME','ONTOLOGY_ACCOUNT_SCOPE'):mp.delenv(name,raising=False)
        mp.setenv('MODE','mock');mp.setenv('RUN_ID','local-rehearsal')
        C=H.load_runtime(mp,runtime,clock)
        def forbidden(*args,**kwargs):
            guard['attempts'].append({'kind':'network_or_process_boundary'})
            raise AssertionError('External call forbidden')
        class BlockedProcess(subprocess.Popen):
            def __init__(self,*args,**kwargs):forbidden()
        mp.setattr(socket.socket,'connect',forbidden);mp.setattr(socket,'create_connection',forbidden)
        mp.setattr(subprocess,'Popen',BlockedProcess);mp.setattr(subprocess,'run',forbidden)
        @contextlib.contextmanager
        def phase(name):
            start=time.perf_counter();state={'status':'running'};report['phase_timings'][name]=state
            try:yield
            except BaseException:state['status']='failed';raise
            else:state['status']='completed'
            finally:
                import storage_objects as O
                state.update(seconds=round(time.perf_counter()-start,3),
                    process_peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024),
                    storage_metrics=dict(O.METRICS))
                H.save(output/'report.json',report);print(name,state['status'],state['seconds'],flush=True)
        guard['active']=True
        try:
            with phase('8_view'):report['steps']['8']=H.run_view(mp,execution,research,clock)
            from rehearsal_faults import run_faults
            with phase('fault_scenarios'):report['fault_scenarios']=run_faults(mp,clock)
            assert not guard['attempts'],guard['attempts']
            report['status']='passed_with_explicit_limits'
        except Exception as exc:
            report.update(status='failed',failure={'type':type(exc).__name__,'message':str(exc),'traceback':traceback.format_exc()})
            print(report['failure']['traceback'],flush=True)
        finally:guard['active']=False
    after=H.digest_files(protected,output)
    report['isolation']={'forbidden_attempts':guard['attempts'],'operating_files_unchanged':original==after,
                         'operating_file_count':len(original),'changed_paths':sorted(k for k in original.keys()|after.keys() if original.get(k)!=after.get(k))}
    from rehearsal_storage import summarize_storage,compare_rehearsals
    report['storage']=summarize_storage(work)
    report['comparison_with_previous_rehearsal']=compare_rehearsals(report,baseline)
    if original!=after or report['comparison_with_previous_rehearsal']['status']!='matched':report['status']='failed'
    report['completed_at']=datetime.now(H.C.KST).isoformat()
    H.save(output/'report.json',report)
    print(json.dumps({'status':report['status'],'report':str(output/'report.json'),'operating_files_unchanged':original==after}),flush=True)
    return 0 if report['status']=='passed_with_explicit_limits' else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path);parser.add_argument('output',type=Path)
    parser.add_argument('--baseline-report',required=True,type=Path)
    args=parser.parse_args();raise SystemExit(run(args.source.resolve(),args.output.resolve(),args.baseline_report.resolve()))
