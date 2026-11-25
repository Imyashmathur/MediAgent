'use client';
import {FormEvent, useState} from 'react';

type Relationship = {id:string; patient_id:string; doctor_id:string; status:string};
type Message = {id:string; sender_id:string; content:string; created_at:number};
type API = (path:string, init?:RequestInit) => Promise<Response>;

function Thread({api, relationship, userId, onError}: {api:API; relationship:Relationship; userId:string; onError:(value:string)=>void}) {
  const [messages, setMessages] = useState<Message[]|null>(null);
  const [nextCursor, setNextCursor] = useState<string|null>(null);
  const [busy, setBusy] = useState(false);
  const path = `relationships/${encodeURIComponent(relationship.id)}/messages`;
  async function load(cursor?:string) {try {const result=await api(cursor ? `${path}?cursor=${encodeURIComponent(cursor)}` : path); setMessages(await result.json()); setNextCursor(result.headers.get('x-next-cursor'));} catch(e) {setMessages(null); setNextCursor(null); onError((e as Error).message);}}
  async function page(cursor?:string) {setBusy(true); try {await load(cursor);} finally {setBusy(false);}}
  async function send(event:FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form=event.currentTarget; const data=new FormData(form);
    setBusy(true);
    try {await api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:data.get('content')})}); form.reset(); await load();}
    catch(e) {onError((e as Error).message);} finally {setBusy(false);}
  }
  return <><button onClick={()=>void page()} disabled={busy}>Refresh messages</button>{nextCursor && <button className="secondary" onClick={()=>void page(nextCursor)} disabled={busy}>Older messages</button>}{messages && <div className="conversation" aria-live="polite">{messages.length ? messages.map(message=><div className="note" key={message.id}><strong>{message.sender_id === userId ? 'You' : 'Care team member'}</strong><p className="note-content">{message.content}</p><small>{new Date(message.created_at*1000).toLocaleString()}</small></div>) : <p>No messages yet.</p>}</div>}<form onSubmit={send}><label>Message<textarea name="content" required maxLength={4000} rows={3}/></label><button disabled={busy || relationship.status !== 'active'}>Send message</button></form></>;
}

export default function CareMessages({api, relationships, userId, onError}: {api:API; relationships:Relationship[]; userId:string; onError:(value:string)=>void}) {
  const [selected, setSelected] = useState('');
  const relationship=relationships.find(item=>item.id===selected);
  return <section className="card document-list"><h3>Care team messages</h3><p>Messages are not monitored for emergencies. For urgent help, use local emergency services. Each page shows up to 100 messages; refresh to return to the latest replies.</p><label>Care relationship<select value={selected} onChange={event=>setSelected(event.target.value)}><option value="">Select a care relationship</option>{relationships.map(item=><option key={item.id} value={item.id}>{item.patient_id===userId ? `Doctor ${item.doctor_id}` : `Patient ${item.patient_id}`} · {item.status} · Relationship {item.id}</option>)}</select></label>{relationship && <Thread key={relationship.id} api={api} relationship={relationship} userId={userId} onError={onError}/>}</section>;
}
