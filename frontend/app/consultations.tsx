'use client';
import {FormEvent, useEffect, useRef, useState} from 'react';

type Consultation = {id:string; title:string; doctor_id:string; occurred_at:number};
type API = (path:string, init?:RequestInit) => Promise<Response>;

export default function Consultations({api, patientId, isDoctor, onError}: {api:API; patientId:string; isDoctor:boolean; onError:(message:string)=>void}) {
  const [items, setItems] = useState<Consultation[]|null>(null);
  const [message, setMessage] = useState('');
  const [cursor, setCursor] = useState<string|null>(null);
  const [busy, setBusy] = useState(false);
  const revision = useRef(0);
  useEffect(()=>{setItems(null); setCursor(null); setMessage(''); return ()=>{revision.current++;};},[patientId]);
  async function load(before?:string) {
    const request=++revision.current; setBusy(true);
    try {
      const result=await api(`patients/${encodeURIComponent(patientId)}/consultations${before ? `?cursor=${encodeURIComponent(before)}` : ''}`);
      const page:Consultation[]=await result.json();
      if(request!==revision.current) return;
      setItems(page); setCursor(result.headers?.get('x-next-cursor') ?? null);
    }
    catch(e) {if(request===revision.current) {setItems(null); setCursor(null); onError((e as Error).message);}}
    finally {if(request===revision.current) setBusy(false);}
  }
  async function create(event:FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form=event.currentTarget; const data=new FormData(form);
    const occurred_at=new Date(String(data.get('occurred_at'))).getTime()/1000;
    if (!Number.isFinite(occurred_at)) {onError('Enter a valid consultation time.'); return;}
    try {await api(`patients/${encodeURIComponent(patientId)}/consultations`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:data.get('title'),occurred_at})}); form.reset(); await load(); setMessage('Consultation recorded.');}
    catch(e) {onError((e as Error).message);}
  }
  return <section className="card document-list"><h3>Consultation history</h3><p>Records of completed consultations; this is not an appointment scheduler. Clinician access requires consultation consent. Each page contains up to 500 consultations.</p><button disabled={!patientId || busy} onClick={()=>void load()}>Open consultation history</button>{isDoctor && <form onSubmit={create}><label>Consultation title<input name="title" required maxLength={200}/></label><label>Consultation date and time<input name="occurred_at" type="datetime-local" required/></label><button disabled={!patientId}>Record consultation</button></form>}{message && <p role="status">{message}</p>}{items && (items.length ? items.map(item=><div className="row" key={item.id}><div><strong>{item.title}</strong><p>{new Date(item.occurred_at*1000).toLocaleString()} · Doctor {item.doctor_id}</p><p>Consultation ID: {item.id}</p></div></div>) : <p>No consultations recorded.</p>)}{cursor && <button className="secondary" disabled={busy} onClick={()=>void load(cursor)}>Older consultations</button>}</section>;
}
