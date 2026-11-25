'use client';
import {useState} from 'react';

type Api = (path:string, init?:RequestInit)=>Promise<Response>;
type Upload = {id:string; filename:string; status:'pending'|'stored'|'discarding'; created_at:number; cleanup_result?:'pending'|'absent'|'removed'|'error'|null; cleanup_next_check_at?:number|null};

export default function IncompleteUploads({api,onError,onCompleted}:{api:Api; onError:(message:string)=>void; onCompleted:()=>Promise<void>}) {
  const [uploads,setUploads]=useState<Upload[]|null>(null);
  const [busy,setBusy]=useState(false);
  const [message,setMessage]=useState('');
  const [nextCursor,setNextCursor]=useState<string|null>(null);
  async function load(cursor?:string) {
    const response=await api(`documents/incomplete-uploads${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`);
    setUploads(await response.json());
    setNextCursor(response.headers.get('x-next-cursor'));
  }
  async function older() {
    if(!nextCursor) return;
    setBusy(true); onError('');
    try {await load(nextCursor);} catch(e) {onError((e as Error).message);} finally {setBusy(false);}
  }
  async function refresh() {
    setBusy(true); onError('');
    try {await load();} catch(e) {onError((e as Error).message);} finally {setBusy(false);}
  }
  async function resume(item:Upload) {
    setBusy(true); onError(''); setMessage('');
    try {await api(`documents/incomplete-uploads/${item.id}/resume`,{method:'POST'}); await load(); await onCompleted(); setMessage('Upload completed.');}
    catch(e) {onError((e as Error).message);} finally {setBusy(false);}
  }
  async function discard(item:Upload) {
    setBusy(true); onError(''); setMessage('');
    try {await api(`documents/incomplete-uploads/${item.id}/discard`,{method:'POST'}); await load(); setMessage('Discard requested; object cleanup remains under monitoring.');}
    catch(e) {onError((e as Error).message);} finally {setBusy(false);}
  }
  return <section className="card"><h3>Incomplete uploads</h3><p>If an object-backed upload was interrupted, refresh this list and try to resume it. An object that was never stored cannot be resumed; upload the file again. Discarding prevents resumption and requests ongoing object cleanup; an empty check does not prove a delayed upload cannot arrive. Pages contain up to 50 uploads.</p><button className="secondary" disabled={busy} onClick={()=>void refresh()}>Refresh incomplete uploads</button>{message && <p role="status">{message}</p>}{uploads && (uploads.length ? uploads.map(item=><div className="row" key={item.id}><p>{item.filename} · {item.status}{item.status === 'discarding' && ` · last cleanup check: ${item.cleanup_result ?? 'pending'}`}{item.cleanup_next_check_at != null && ` · next check after ${new Date(item.cleanup_next_check_at*1000).toLocaleString()}`}</p>{item.status !== 'discarding' && <><button className="secondary" disabled={busy} onClick={()=>void resume(item)}>Resume upload</button><button className="secondary" disabled={busy} onClick={()=>void discard(item)}>Discard upload</button></>}</div>) : <p>No incomplete uploads.</p>)}{nextCursor && <button className="secondary" disabled={busy} onClick={()=>void older()}>Older incomplete uploads</button>}</section>;
}
