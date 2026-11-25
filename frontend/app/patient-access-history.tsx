'use client';
import {useEffect, useRef, useState} from 'react';

type Event={id:string; actor_id:string; action:string; created_at:number};
type API=(path:string, init?:RequestInit)=>Promise<Response>;

export default function PatientAccessHistory({api,onError}:{api:API;onError:(message:string)=>void}) {
  const [items,setItems]=useState<Event[]|null>(null);
  const [cursor,setCursor]=useState<string|null>(null);
  const [loading,setLoading]=useState(false);
  const revision=useRef(0);
  useEffect(()=>()=>{revision.current++;},[]);
  async function load(before?:string) {
    const request=++revision.current; setLoading(true);
    try {
      const response=await api(`audit/patient-access${before ? `?cursor=${encodeURIComponent(before)}` : ''}`);
      const page:Event[]=await response.json();
      if(request!==revision.current) return;
      setItems(previous=>before ? [...(previous ?? []),...page] : page);
      setCursor(response.headers.get('x-next-cursor'));
    } catch(error) {if(request===revision.current) {setItems(null);setCursor(null);onError((error as Error).message);}}
    finally {if(request===revision.current) setLoading(false);}
  }
  return <section className="card document-list"><h3>Clinician access activity</h3><p>Selected successful clinician authorization checks and actions are shown. A successful check does not prove information was delivered or read. This is not a complete access log.</p><button className="secondary" disabled={loading} onClick={()=>void load()}>Refresh access activity</button>{items && (items.length ? items.map(item=><div className="row" key={item.id}><div><strong>{item.action.replace(/_/g,' ')}</strong><p>Doctor ID: {item.actor_id} · {new Date(item.created_at*1000).toLocaleString()}</p></div></div>) : <p>No activity to display.</p>)}{cursor && <button className="secondary" disabled={loading} onClick={()=>void load(cursor)}>Load older access activity</button>}</section>;
}
