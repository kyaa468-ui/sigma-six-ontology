/* Public, allowlisted status only. Never contacts localhost or substitutes live data. */
(() => {
  'use strict';
  const text = (tag,value) => {const n=document.createElement(tag);n.textContent=value==null?'미확인':String(value);return n;};
  const label = v => ({linked:'정상 연결',not_submitted:'미제출',rejected:'거절',accepted_unlinked:'수락 후 미연결',recording_error:'기록 오류',protected:'검증용 보호',not_recorded:'연결 미보관',refuted:'반증',maintained:'유지',complete:'완료',partial:'부분 완료',unfinished:'시작 후 미완료',not_started:'미시작',reference_available:'참조 있음'}[v]||v||'미확인');
  let loaded=null,error=null,pending=null;
  const dialog=document.createElement('dialog');dialog.id='trace-summary';dialog.style.cssText='max-width:760px;width:calc(100% - 30px);max-height:85vh;background:linear-gradient(180deg,#1c1c1c,#101012);color:#f5f5f5;border:1px solid rgba(218,41,28,.4);border-radius:16px;padding:24px;line-height:1.6;font-family:var(--font,serif)';
  const body=document.createElement('div'),close=text('button','닫기');close.addEventListener('click',()=>dialog.close());dialog.append(close,body);document.body.append(dialog);
  const digest=async bytes=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))).map(x=>x.toString(16).padStart(2,'0')).join('');
  async function load(){
    if(pending)return pending;
    pending=(async()=>{try{
      const m=await fetch('data/ontology/manifest.json',{cache:'no-store'});if(!m.ok)throw Error('공개 연결 요약이 아직 게시되지 않았습니다.');
      const manifest=await m.json();if(manifest.schema_version!=='TRACE_PUBLIC@v1'||manifest.status!=='complete'||!/^[A-Za-z0-9_-]+$/.test(manifest.export_id)||!/^\w{64}$/.test(manifest.files?.['summary.json']||''))throw Error('공개 요약 목록을 확인할 수 없습니다.');
      const response=await fetch('data/ontology/'+manifest.export_id+'/summary.json',{cache:'no-store'});if(!response.ok)throw Error('공개 요약 파일이 없습니다.');
      const bytes=await response.arrayBuffer();if(await digest(bytes)!==manifest.files['summary.json'])throw Error('공개 요약 무결성 확인에 실패했습니다.');
      const value=JSON.parse(new TextDecoder().decode(bytes));if(value.export_id!==manifest.export_id||value.view_id!==manifest.view_id||value.schema_version!=='TRACE_PUBLIC@v1'||!Array.isArray(value.targets)||typeof value.traces!=='object')throw Error('공개 요약 버전이 일치하지 않습니다.');loaded=value;return loaded;
    }catch(e){error=e.message;return null;}})();return pending;
  }
  function showTrace(trace){
    body.append(text('h3',(trace.code||'')+' · 근거 연결 요약'),text('p',trace.server_summary||'상세 근거는 로컬 운영자 화면에서 확인합니다.'));
    const dl=document.createElement('dl');for(const[k,v]of [['판단 기록',label(trace.coverage_status)],['연결 상태',label(trace.connection_status)],['행동',trace.action],['보호 상태',label(trace.visibility)],['지식 기준시각',loaded.knowledge_as_of]]){dl.append(text('dt',k),text('dd',v));}body.append(dl);
    for(const[k,title]of [['premise_states','전제 평가'],['validation_states','시험 상태']]){const values=trace[k]||[];if(values.length)body.append(text('p',title+' · '+values.map(x=>label(x.status)+' '+x.count+'건').join(' / ')));}
    const details=document.createElement('details');details.append(text('summary','연결 ID · 운영자 조회용'));const code=text('code',trace.trace_id);details.append(code);body.append(details);const copy=text('button','연결 ID 복사');copy.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(trace.trace_id);copy.textContent='복사됨';}catch(_){copy.textContent='위 ID를 선택해 복사하세요';}});details.append(copy,text('p','이 ID를 로컬 운영자 화면에서 찾을 수 있습니다. 공개 화면에서 로컬 서버로 접속하지 않습니다.'));
  }
  async function open(row){
    body.replaceChildren(text('p','공개 연결 요약을 확인하고 있습니다.'));if(!dialog.open)dialog.showModal();
    const value=await load();body.replaceChildren();if(!value){body.append(text('p',error||'요약 조회 불가'),text('p','현재 시세나 샘플로 대체하지 않았습니다.'));return;}
    const refs=new Set((row?.origin_refs||[]).map(x=>x.ref));
    const matches=Object.values(value.traces).filter(t=>row?.trace_id===t.trace_id||(t.origin_refs||[]).some(r=>refs.has(r.ref)));
    if(!row){body.append(text('h3','판단 연결 요약'));for(const target of value.targets){const b=text('button',(target.code||'')+' · '+label(target.coverage_status));b.addEventListener('click',()=>open(target));body.append(b,document.createElement('br'));}if(!value.targets.length)body.append(text('p','공개 가능한 판단 요약이 없습니다.'));return;}
    if(matches.length!==1){body.append(text('h3',matches.length?'연결이 여러 개입니다.':'연결 근거가 보관되지 않았습니다.'),text('p','종목이나 시각으로 판단을 추정해 연결하지 않습니다.'));for(const t of matches){const b=text('button',(t.code||'종목 미확인')+' · '+label(t.coverage_status)+' · 연결 '+(matches.indexOf(t)+1));b.addEventListener('click',()=>{body.replaceChildren();showTrace(t);});body.append(b);}return;}
    showTrace(matches[0]);
  }
  window.SigmaTrace={open};
  const anchor=document.getElementById('ch-scatter');if(anchor){const b=text('button','판단·근거 연결 요약');b.addEventListener('click',()=>open(null));anchor.parentElement.append(b);}
})();
