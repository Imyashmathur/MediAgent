'use client';
import {useEffect, useRef, useState} from 'react';

type API=(path:string, init?:RequestInit)=>Promise<Response>;
type Notification={id:string; kind:string; patient_id:string; resource_id:string; processing_job_id?:string; created_at:number; read_at:number|null};
const labels:Record<string,string>={relationship_created:'A patient added you to their care team.',relationship_restored:'A patient restored a care relationship; access requires new permission.',consent_changed:'A patient changed an access permission.',note_review_requested:'A clinical note was submitted for review.',processing_completed:'Document processing completed; results are unverified.',processing_failed:'Document processing could not complete.',care_message_received:'A care team message is available. Refresh the matching message thread.'};

export default function Notifications({api,role,onError,onOpenProcessing}:{api:API; role:string; onError:(message:string)=>void; onOpenProcessing?:(documentId:string,jobId:string)=>void}) {
  const [items,setItems]=useState<Notification[]|null>(null);
  const [cursor,setCursor]=useState<string|null>(null);
  const [busy,setBusy]=useState(false);
  const revision=useRef(0);
  useEffect(()=>()=>{revision.current++;},[]);
  async function load(before?:string) {
    const request=++revision.current; setBusy(true);
    try {
      const result=await api(`notifications${before ? `?cursor=${encodeURIComponent(before)}` : ''}`);
      const page:Notification[]=await result.json();
      if(request!==revision.current) return;
      setItems(previous=>before ? [...(previous ?? []),...page] : page);
      setCursor(result.headers?.get('x-next-cursor') ?? null);
    } catch(e) {if(request===revision.current) {setItems(null); setCursor(null); onError((e as Error).message);}}
    finally {if(request===revision.current) setBusy(false);}
  }
  async function mark(item:Notification) {
    setBusy(true);
    try {await api(`notifications/${encodeURIComponent(item.id)}/read`,{method:'POST'}); await load();} catch(e) {onError((e as Error).message); setBusy(false);}
  }
  return <section className="card document-list"><h3>Notifications</h3><p>In-app only. Refresh to check current access and see the newest updates.</p><button disabled={busy} onClick={()=>void load()}>Refresh notifications</button>{items && (items.length ? items.map(item=><div className="row" key={item.id}><div><strong>{labels[item.kind] ?? 'Care workflow update'}</strong><p>{role === 'doctor' && <>Patient {item.patient_id} · </>}{item.kind === 'care_message_received' && <>Relationship {item.resource_id} · </>}{new Date(item.created_at*1000).toLocaleString()} · {item.read_at ? 'Read' : 'Unread'}</p></div>{role === 'patient' && item.processing_job_id && onOpenProcessing && <button disabled={busy} className="secondary" onClick={()=>onOpenProcessing(item.resource_id,item.processing_job_id!)}>Open processing run</button>}{!item.read_at && <button disabled={busy} className="secondary" onClick={()=>void mark(item)}>Mark read</button>}</div>) : <p>No notifications.</p>)}{cursor && <button disabled={busy} className="secondary" onClick={()=>void load(cursor)}>Load older notifications</button>}</section>;
}
