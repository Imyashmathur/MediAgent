'use client';

import {useEffect, useRef, useState} from 'react';

type Run = {id:string; status:string; created_at:number};
type Page = {items:Run[]; next_before:string|null};

export default function ProcessingHistory({api, documentId, onSelect, onError}:{api:(path:string)=>Promise<Response>; documentId:string; onSelect:(id:string)=>void; onError:(message:string)=>void}) {
  const [runs, setRuns] = useState<Run[]>([]);
  const [next, setNext] = useState<string|null>(null);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const active = useRef(false);
  const request = useRef(0);
  useEffect(()=>{active.current=true; return ()=>{active.current=false; request.current++;};},[]);
  async function load(before?:string) {
    const revision=++request.current; setLoading(true);
    try {
      const response = await api(`documents/${documentId}/processing/history${before ? `?before=${encodeURIComponent(before)}` : ''}`);
      const page:Page = await response.json();
      if (!active.current || revision !== request.current) return;
      setRuns(previous => before ? [...previous, ...page.items] : page.items);
      setNext(page.next_before); setLoaded(true);
    } catch(error) {if (active.current && revision === request.current) onError((error as Error).message);} finally {if (active.current && revision === request.current) setLoading(false);}
  }
  return <details onToggle={event=>{if(event.currentTarget.open && !loaded && !loading) void load();}}><summary>Processing history</summary><p>Earlier results are historical; only the latest completed run can be reviewed.</p>{runs.map(run=><div className="row" key={run.id}><span>{new Date(run.created_at * 1000).toLocaleString()} · {run.status.replace('_',' ')}</span><button type="button" className="secondary" onClick={()=>onSelect(run.id)}>View run</button></div>)}{loaded && <button type="button" className="secondary" disabled={loading} onClick={()=>void load()}>Refresh history</button>}{loaded && !runs.length && <p>No processing runs.</p>}{next && <button type="button" disabled={loading} onClick={()=>void load(next)}>Load older runs</button>}</details>;
}
