'use client';

import {useEffect, useState} from 'react';

export type Consent = {id:string; relationship_id:string; doctor_id:string; relationship_status:'active'|'ended'; purpose:string; scope:string; status:'active'|'withdrawn'; effective_at:number; expires_at:number; withdrawn_at:number|null};
const labels:Record<string,string> = {clinical_notes:'Clinical notes', care_messages:'Care team messages', consultations:'Consultations', read_profile:'Patient profile', review_documents:'Document transcription review', assistant:'Patient-scoped assistant', integration_access:'Document integrations', profile_integration:'Profile integrations', consultations_integration:'Consultation integrations', read_documents:'Documents'};

export function grantState(c:Consent, now:number):string {
  if (c.status === 'withdrawn') return 'withdrawn';
  if (c.relationship_status !== 'active') return 'relationship ended';
  if (c.expires_at <= now) return 'expired';
  if (c.effective_at > now) return 'not yet effective';
  return 'active';
}

export default function ConsentHistory({consents, onWithdraw, onRefresh}:{consents:Consent[]; onWithdraw:(id:string)=>void; onRefresh:()=>void}) {
  const [now, setNow] = useState(()=>Date.now()/1000);
  useEffect(()=>{const timer = setInterval(()=>setNow(Date.now()/1000), 30_000); return ()=>clearInterval(timer);},[]);
  return <section className="card"><h3>Access permissions</h3><p>Grant status also depends on the care relationship and dates. Access requires an active, verified doctor; refresh to check for relationship changes.</p><button type="button" className="secondary" onClick={onRefresh}>Refresh permissions</button>{consents.length ? consents.map(c=><div className="row" key={c.id}><div><strong>{c.purpose}</strong><p>{labels[c.scope] ?? c.scope} · {grantState(c,now)} · expires {new Date(c.expires_at*1000).toLocaleString()}</p><p>Doctor ID: {c.doctor_id} · effective {new Date(c.effective_at*1000).toLocaleString()}{c.withdrawn_at != null && <> · withdrawn {new Date(c.withdrawn_at*1000).toLocaleString()}</>}</p></div>{c.status==='active' && <button className="secondary" onClick={()=>onWithdraw(c.id)}>Withdraw</button>}</div>) : <p>No consent grants yet.</p>}</section>;
}
