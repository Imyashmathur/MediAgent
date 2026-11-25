'use client';
import {FormEvent, useState} from 'react';

type API=(path:string, init?:RequestInit)=>Promise<Response>;
type Version={version_id:string; version:number};
type Comparison={from_version:number; to_version:number; lines:{kind:'context'|'added'|'removed';text:string}[]};

export default function NoteComparison({api,noteId,current,previous,onError}:{api:API; noteId:string; current:Version; previous:Version[]; onError:(message:string)=>void}) {
  const [result,setResult]=useState<Comparison|null>(null);
  if (!previous.length) return null;
  async function compare(event:FormEvent<HTMLFormElement>) {
    event.preventDefault(); const source=String(new FormData(event.currentTarget).get('source') ?? '');
    try {const response=await api(`notes/${encodeURIComponent(noteId)}/compare`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({from_version_id:source,to_version_id:current.version_id})}); setResult(await response.json());}
    catch(e) {setResult(null); onError((e as Error).message);}
  }
  return <details><summary>Compare with an earlier version</summary><form onSubmit={compare}><label>Earlier version<select name="source">{previous.map(version=><option key={version.version_id} value={version.version_id}>Version {version.version}</option>)}</select></label><button className="secondary">Compare</button></form>{result && <div className="note-diff"><p>Version {result.from_version} → {result.to_version}. Lines marked + are added; lines marked − are removed.</p>{result.lines.map((line,index)=><div className={`diff-${line.kind}`} key={index}><span aria-hidden="true">{line.kind === 'added' ? '+ ' : line.kind === 'removed' ? '− ' : '  '}</span>{line.text || '\u00a0'}</div>)}</div>}</details>;
}
