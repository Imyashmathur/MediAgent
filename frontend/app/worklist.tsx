'use client';
import {FormEvent, useState} from 'react';

type API=(path:string, init?:RequestInit)=>Promise<Response>;
type Item={patient_id:string; created_at:number; filename?:string; note_id?:string; document_id?:string};
type Worklist={patients:{patient_id:string; full_name:string; scopes:string[]}[]; recent_uploads:Item[]; pending_notes:Item[]; pending_extractions:Item[]; next_patient_cursor:string|null; next_upload_cursor:string|null; next_note_cursor:string|null; next_extraction_cursor:string|null};
type Cursors={patient_cursor?:string; upload_cursor?:string; note_cursor?:string; extraction_cursor?:string};

export default function Worklist({api,onSelect,onError}:{api:API; onSelect:(id:string)=>void; onError:(message:string)=>void}) {
  const [data,setData]=useState<Worklist|null>(null);
  const [query,setQuery]=useState('');
  const [cursors,setCursors]=useState<Cursors>({});
  const [history,setHistory]=useState<Cursors[]>([]);
  const [busy,setBusy]=useState(false);
  async function load(query:string, next:Cursors={}, nextHistory:Cursors[] = []) {
    setBusy(true); onError('');
    try {const result=await api('doctor/worklist', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query,...next})}); setData(await result.json()); setCursors(next); setHistory(nextHistory);}
    catch(e) {setData(null); setHistory([]); onError((e as Error).message);}
    finally {setBusy(false);}
  }
  async function search(event:FormEvent<HTMLFormElement>) {
    event.preventDefault(); const value=String(new FormData(event.currentTarget).get('query') ?? '').trim();
    setQuery(value); await load(value);
  }
  function advance(next:Cursors) {void load(query,next,[...history,cursors].slice(-100));}
  function items(title:string,rows:Item[],describe:(row:Item)=>string,cursor:string|null,key:keyof Cursors) {return <section><h4>{title}</h4>{rows.length ? rows.map(row=><div className="row" key={row.note_id ?? row.document_id}><div><strong>{describe(row)}</strong><p>Patient {row.patient_id} · {new Date(row.created_at*1000).toLocaleString()}</p></div><button className="secondary" onClick={()=>onSelect(row.patient_id)}>Select patient</button></div>) : <p>None to show.</p>}{cursor && <button className="secondary" disabled={busy} onClick={()=>advance({...cursors,[key]:cursor})}>Older {title.toLowerCase()}</button>}</section>;}
  return <section className="card document-list"><h3>Authorized patients and reviews</h3><p>Results require an active relationship and current consent. Each patient page has up to 100 patients; each item category has pages of up to 50 items for those patients. Search to narrow the list. Select a patient to open their workspace.</p><form onSubmit={search}><label>Search by name or exact patient ID<input name="query" maxLength={100}/></label><button disabled={busy}>Search and refresh worklist</button></form>{data && <>{history.length > 0 && <button className="secondary" disabled={busy} onClick={()=>void load(query,history[history.length-1],history.slice(0,-1))}>Previous worklist view</button>}<h4>Patients</h4>{data.patients.length ? data.patients.map(patient=><div className="row" key={patient.patient_id}><div><strong>{patient.full_name}</strong><p>{patient.patient_id} · {patient.scopes.join(', ')}</p></div><button className="secondary" onClick={()=>onSelect(patient.patient_id)}>Select patient</button></div>) : <p>No authorized patients found.</p>}{data.next_patient_cursor && <button className="secondary" disabled={busy} onClick={()=>advance({patient_cursor:data.next_patient_cursor!})}>Next patient page</button>}{items('Recent uploads',data.recent_uploads,item=>item.filename ?? 'Document',data.next_upload_cursor,'upload_cursor')}{items('Notes awaiting review',data.pending_notes,()=> 'Clinical note awaiting review',data.next_note_cursor,'note_cursor')}{items('Transcriptions awaiting review',data.pending_extractions,item=>item.filename ?? 'Document',data.next_extraction_cursor,'extraction_cursor')}</>}</section>;
}
