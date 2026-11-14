"""File-backed subprocess output. Preserve raw bytes on success and failure."""
from __future__ import annotations
import base64
import hashlib
import json
from pathlib import Path
import tempfile

import common as C
import log_store
import record_store
import storage_objects as objects


def freeze_bytes(path):
    refs=[];sha=hashlib.sha256();size=0
    with open(path,'rb') as stream:
        for raw in iter(lambda:stream.read(192*1024),b''):
            sha.update(raw);size+=len(raw)
            refs.append(objects.put_object(base64.b64encode(raw).decode('ascii'),kind='raw_bytes'))
    return {'encoding':'base64-chunks','sha256':sha.hexdigest(),'bytes':size,'chunks':refs}


class Spool:
    def __init__(self, identifier):
        root=C.ontology_root()/'spool';root.mkdir(parents=True,exist_ok=True)
        self.folder=Path(tempfile.mkdtemp(prefix='output-',dir=root))
        self.identifier=identifier
        self.stdout=(self.folder/'stdout').open('w+b')
        self.stderr=(self.folder/'stderr').open('w+b')
        self.raw=None

    def accept(self, stdout=None, stderr=None):
        # File-backed Popen returns None; in-process adapters can return bytes.
        for stream,value in ((self.stdout,stdout),(self.stderr,stderr)):
            if value:stream.write(value.encode('utf-8') if isinstance(value,str) else value)
            stream.flush()

    def tail(self, length=2000):
        self.stderr.flush();self.stderr.seek(0,2);end=self.stderr.tell()
        self.stderr.seek(max(0,end-length*4))
        return self.stderr.read().decode('utf-8',errors='replace')[-length:]

    def result(self):
        self.stdout.flush();self.stdout.seek(0)
        if not self.stdout.read(1):return {}
        self.stdout.seek(0)
        refs=iter(log_store.store_json_stream(self.stdout))
        ref=next(refs)
        if next(refs,None) is not None:raise ValueError('MULTIPLE_CLI_RESULTS')
        return objects.open_object(ref)

    def capture(self):
        if self.raw is not None:return self.raw
        self.accept()
        self.raw={'stdout':freeze_bytes(self.folder/'stdout'),'stderr':freeze_bytes(self.folder/'stderr')}
        path=C.ontology_root()/'runtime_inputs'/(self.identifier+'.stdio.json')
        record_store.publish(path,{'run_attempt_id':self.identifier,'created_at':C.now().isoformat(),**self.raw},kind='cli_stdio')
        self.stdout.close();self.stderr.close()
        (self.folder/'stdout').unlink();(self.folder/'stderr').unlink();self.folder.rmdir()
        return self.raw


def raw_text(raw, *, base=None):
    """Explicit compatibility export; callers choose to materialize text."""
    return b''.join(base64.b64decode(objects.open_object(ref,base=base)) for ref in raw['chunks']).decode('utf-8',errors='replace')
