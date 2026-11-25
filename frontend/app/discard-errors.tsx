'use client';
import {useState} from 'react';

type API = (path:string, init?:RequestInit)=>Promise<Response>;
type ErrorItem = {id:string; category:string; attempts:number; checked_at:number|null; next_check_at:number};
const labels:Record<string,string>={storage_configuration:'Storage configuration',integrity_conflict:'Object integrity or association conflict',provider_unavailable:'Storage provider unavailable',unexpected_error:'Unexpected failure',unclassified:'Unclassified'};

export default function DiscardErrors({api,onError}:{api:API; onError:(message:string)=>void}) {
  const [items,setItems]=useState<ErrorItem[]|null>(null);
  const [cursor,setCursor]=useState<string|null>(null);
  const [busy,setBusy]=useState(false);
  const [message,setMessage]=useState('');
  async function load(before?:string) {
    const result=await api(`admin/operations/discard-errors${before ? `?cursor=${encodeURIComponent(before)}` : ''}`);
    setItems(await result.json());
    setCursor(result.headers.get('x-next-cursor'));
  }
  async function refresh(before?:string) {
    setBusy(true); onError(''); setMessage('');
    try {await load(before);} catch(e) {setItems(null);setCursor(null);onError((e as Error).message);} finally {setBusy(false);}
  }
  async function retry(item:ErrorItem) {
    setBusy(true); onError(''); setMessage('');
    try {await api(`admin/operations/discard-errors/${encodeURIComponent(item.id)}/retry`,{method:'POST'}); await load(); setMessage('Cleanup check scheduled. The result remains uncertain until the purger checks it.');}
    catch(e) {onError((e as Error).message);} finally {setBusy(false);}
  }
  return <section><h3>Discard cleanup errors</h3><p>Operational metadata only. Retry schedules another check; it does not establish that the object was removed.</p><button className="secondary" disabled={busy} onClick={()=>void refresh()}>Review discard errors</button>{message && <p role="status">{message}</p>}{items && (items.length ? items.map(item=><div className="row" key={item.id}><div><strong>{labels[item.category] ?? 'Unclassified'}</strong><p>Check ID: {item.id} · Attempts: {item.attempts}</p><p>Last check: {item.checked_at == null ? 'unknown' : new Date(item.checked_at*1000).toLocaleString()} · Next check after {new Date(item.next_check_at*1000).toLocaleString()}</p></div><button className="secondary" disabled={busy} onClick={()=>void retry(item)}>Retry cleanup check</button></div>) : <p>No discard cleanup errors.</p>)}{cursor && <button className="secondary" disabled={busy} onClick={()=>void refresh(cursor)}>Older discard errors</button>}</section>;
}
