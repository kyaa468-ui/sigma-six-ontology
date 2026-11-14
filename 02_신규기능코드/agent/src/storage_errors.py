"""Bounded fallback for errors whose durable write failed; counters never reset."""
from collections import deque, OrderedDict

class ErrorBuffer(deque):
    def __init__(self):
        super().__init__(maxlen=1024)
        self.total=0;self.first_at=None;self.last_at=None
    def append(self,row):
        self.total+=1;self.last_at=row.get('created_at') or row.get('recorded_at')
        if self.first_at is None:self.first_at=self.last_at
        super().append(row)
    def state(self):
        return {'unpersisted_count':self.total,'retained_count':len(self),'overflow':self.total>len(self),
                'first_at':self.first_at,'last_at':self.last_at,'recording_status':'recording_error'}

class ErrorRegistry(OrderedDict):
    def __init__(self):
        super().__init__();self.total=0;self.first_at=None;self.last_at=None
    def state(self):
        retained=sum(len(rows) for rows in self.values())
        return {'unpersisted_count':self.total,'retained_count':retained,'overflow':self.total>retained,
                'first_at':self.first_at,'last_at':self.last_at,
                'recording_status':'recording_error' if self.total else 'recorded'}

def retain(buffers,scope,row):
    if isinstance(buffers,ErrorRegistry):
        buffers.total+=1;buffers.last_at=row.get('created_at') or row.get('recorded_at')
        if buffers.first_at is None:buffers.first_at=buffers.last_at
    if sum(len(rows) for rows in buffers.values())>=1024:
        for key,existing in list(buffers.items()):
            if existing:
                existing.popleft()
                if not existing:del buffers[key]
                break
    buffer=buffers.setdefault(scope,ErrorBuffer());buffer.append(row)
    return buffers.state() if isinstance(buffers,ErrorRegistry) else buffer.state()
