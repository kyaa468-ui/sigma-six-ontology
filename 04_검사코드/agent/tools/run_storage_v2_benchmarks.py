"""Reproducible isolated RSS/dedup benchmarks; generates synthetic local data only."""
from pathlib import Path
import argparse,json,resource,subprocess,sys,time,random,string
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import common as C,storage_objects as O,log_store as L,record_store as R


def worker(root,mode,n=0):
    root=Path(root);root.mkdir(parents=True,exist_ok=True);C.WORK=str(root)
    start=time.perf_counter();rows=0;path=root/'large.jsonl';extra={}
    if mode=='generate':
        payload=b'x'*(1024*1024)
        with path.open('wb') as out:
            out.write(b'{"seq":0,"raw":"')
            for _ in range(80):out.write(payload)
            out.write(b'"}\n');i=1
            while out.tell()<1_900_000_000:
                out.write(('{"seq":'+str(i)+',"raw":"').encode());out.write(payload);out.write(b'"}\n');i+=1
        rows=i
    elif mode=='project':
        for row in L.iter_rows(path,fields=('seq',)):
            assert row=={'seq':rows};rows+=1
    elif mode in ('convert80','convertlarge'):
        base=root/mode/'ontology'
        with O.scope(base):
            with path.open('rb') as stream:
                source=L.PrefixReader(stream,80*1024*1024+18) if mode=='convert80' else stream
                for ref in L.store_json_stream(source,base=base,multiple=mode=='convertlarge'):
                    value=O.open_object(ref,base=base);assert value['seq']==rows
                    if not rows:assert O.field_size(value,'raw')==80*1024*1024+2
                    O.verify_closure(ref,base=base)
                    R.publish(base/'events'/f'{rows:08}.json',value,base=base)
                    rows+=1
        extra['physical_bytes']=sum(p.stat().st_size for p in base.rglob('*') if p.is_file())
    elif mode=='archive':
        folder=root/('archive-'+str(n));C.WORK=str(folder);folder.mkdir(exist_ok=True)
        random.seed(7)
        with O.scope(folder/'ontology'):
            payload=O.freeze({'text':''.join(random.choices(string.ascii_letters+string.digits,k=1024*1024))})
            payload_ref=O.put_object(payload);dependencies=O.verify_closure(payload_ref)
            for i in range(n):R.publish(folder/'ontology/events'/f'{i:08}.json',{'event_id':str(i),'run_attempt_id':'wanted' if i<10 else 'old','payload':payload})
            extra.update(payload_ref=payload_ref,payload_closure_bytes=sum(Path(p).stat().st_size for p in dependencies),
                         physical_bytes=sum(p.stat().st_size for p in folder.rglob('*') if p.is_file()))
        rows=n
    elif mode=='query':
        folder=root/('archive-'+str(n));C.WORK=str(folder)
        rows=sum(1 for _ in R.iter_records(folder/'ontology/events',filters={'run_attempt_id':'wanted'},fields=['event_id']))
        assert rows==10
    report={'mode':mode,'n':n,'rows':rows,'seconds':time.perf_counter()-start,
        'peak_rss_bytes':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*(1 if sys.platform=='darwin' else 1024),
        'metrics':O.METRICS,'input_bytes':path.stat().st_size if path.exists() else None,**extra}
    return report


def run(root):
    root=Path(root);root.mkdir(parents=True,exist_ok=True);results=[]
    tasks=[('generate',0),('project',0),('convert80',0),('convertlarge',0),
           ('archive',10),('archive',100),('archive',1000),('query',100),('query',1000)]
    for mode,n in tasks:
        output=root/f'{mode}-{n}.json'
        if output.exists():result=json.loads(output.read_text())
        else:
            completed=subprocess.run([sys.executable,__file__,str(root),'--worker',mode,'--n',str(n)],capture_output=True,text=True,check=True)
            result=json.loads(completed.stdout);output.write_text(json.dumps(result,indent=2)+'\n')
        results.append(result);print(json.dumps(result),flush=True)
    by={(x['mode'],x['n']):x for x in results}
    small,big=by[('query',100)],by[('query',1000)]
    archives=[by[('archive',n)] for n in (10,100,1000)]
    checks={'log_rss_under_256MiB':all(by[(mode,0)]['peak_rss_bytes']<=256*1024**2 for mode in ('project','convert80','convertlarge')),
            'query_growth_bounded':big['peak_rss_bytes']-small['peak_rss_bytes']<=max(small['peak_rss_bytes']*.2,32*1024**2),
            'payload_single_copy':len({x['payload_ref'][O.REF]['sha256'] for x in archives})==1 and len({x['payload_closure_bytes'] for x in archives})==1,
            'full_log_rows_converted':by[('generate',0)]['rows']==by[('convertlarge',0)]['rows']}
    report={'schema_version':2,'status':'passed' if all(checks.values()) else 'failed','checks':checks,'results':results,
            'rss_platform':sys.platform,'rss_scope':'fresh child process per workload; ru_maxrss is process lifetime peak',
            'source_hashes':{str(p.relative_to(Path(__file__).resolve().parents[1])):O.file_fingerprint(p)[0] for p in (Path(__file__).resolve().parents[1]/'src').glob('*.py')}}
    (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('root',type=Path)
    parser.add_argument('--worker',choices=('generate','project','convert80','convertlarge','archive','query'));parser.add_argument('--n',type=int,default=0)
    args=parser.parse_args()
    result=worker(args.root,args.worker,args.n) if args.worker else run(args.root)
    if args.worker:print(json.dumps(result))
    elif result['status']!='passed':raise SystemExit(1)
