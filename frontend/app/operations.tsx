'use client';
import {FormEvent, useState} from 'react';
import DiscardErrors from './discard-errors';

type API = (path:string, init?:RequestInit) => Promise<Response>;
type Operations = {database:string; redis:string; document_jobs:Record<string,number>; knowledge_import_jobs:Record<string,number>; knowledge_index_jobs:Record<string,number>; possibly_stale_index_jobs:number; job_dispatch:Record<string,number>; reset_mail:Record<string,number>; document_purges:Record<string,number>; incomplete_object_uploads:Record<string,number>; old_incomplete_object_uploads:number; discarded_object_uploads:Record<string,number>; discard_error_categories:Record<string,number>; overdue_discard_checks:number; oldest_discard_check_lag_seconds:number; api_metrics:{responses:Record<string,number>; latency_buckets:Record<string,number>; provider_failures:Record<string,number>}; possibly_stale_jobs:number; stale_after_seconds:number};
const discardErrors:Record<string,string>={storage_configuration:'storage configuration',integrity_conflict:'object integrity or association conflict',provider_unavailable:'storage provider unavailable',unexpected_error:'unexpected failure',unclassified:'unclassified'};
type Audit = {id:string; actor_id:string|null; patient_id:string|null; action:string; resource_type:string; outcome:string; correlation_id:string; created_at:number};

export default function Operations({api,onError}: {api:API; onError:(value:string)=>void}) {
  const [status,setStatus]=useState<Operations|null>(null);
  const [events,setEvents]=useState<Audit[]|null>(null);
  const [nextCursor,setNextCursor]=useState<string|null>(null);
  const [filters,setFilters]=useState('');
  const [loading,setLoading]=useState(false);
  async function loadStatus() {try {const response=await api('admin/operations'); setStatus(await response.json());} catch(e) {setStatus(null); onError((e as Error).message);}}
  async function loadAudit(event:FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form=new FormData(event.currentTarget); const query=new URLSearchParams();
    for (const key of ['action','outcome']) {const value=String(form.get(key) ?? '').trim(); if(value) query.set(key,value);}
    setFilters(query.toString()); await fetchAudit(query);
  }
  async function fetchAudit(query:URLSearchParams) {
    setLoading(true);
    try {const response=await api(`admin/audit?${query}`); setEvents(await response.json()); setNextCursor(response.headers.get('x-next-cursor'));}
    catch(e) {setEvents(null); setNextCursor(null); onError((e as Error).message);}
    finally {setLoading(false);}
  }
  function loadOlder() {if(!nextCursor)return; const query=new URLSearchParams(filters); query.set('cursor',nextCursor); void fetchAudit(query);}
  return <section className="card document-list"><h3>System operations</h3><button onClick={loadStatus}>Refresh status</button>{status && <div><p>Database: {status.database} · Redis: {status.redis}</p><p>Document jobs: {Object.entries(status.document_jobs).map(([state,count])=>`${state}: ${count}`).join(' · ') || 'none'}</p><p>Knowledge import jobs: {Object.entries(status.knowledge_import_jobs).map(([state,count])=>`${state}: ${count}`).join(' · ') || 'none'}</p><p>Knowledge indexing jobs: {Object.entries(status.knowledge_index_jobs).map(([state,count])=>`${state}: ${count}`).join(' · ') || 'none'}</p><p>Knowledge indexing jobs queued or running for over {status.stale_after_seconds/60} minutes: {status.possibly_stale_index_jobs}</p><p>Job dispatch: {Object.entries(status.job_dispatch).map(([state,count])=>`${state}: ${count}`).join(' · ') || 'none'}</p><p>Password-reset mail: {Object.entries(status.reset_mail).map(([state,count])=>`${state}: ${count}`).join(' · ') || 'none'}</p><p>API responses (this process): {Object.entries(status.api_metrics.responses).map(([state,count])=>`${state}: ${count}`).join(' · ')}</p><p>Response duration buckets: {Object.entries(status.api_metrics.latency_buckets).map(([state,count])=>`${state}: ${count}`).join(' · ')}</p><p>Provider failures (this process): {Object.entries(status.api_metrics.provider_failures).map(([state,count])=>`${state}: ${count}`).join(' · ')}</p><p>Incomplete object uploads: {Object.entries(status.incomplete_object_uploads).map(([state,count])=>`${state}: ${count}`).join(' · ') || 'none'}</p><p>Incomplete object uploads older than 24 hours: {status.old_incomplete_object_uploads}</p><p>Discarded upload cleanup checks: {Object.entries(status.discarded_object_uploads).map(([state,count])=>`${state}: ${count}`).join(' · ') || 'none'}</p><p>Discard cleanup errors: {Object.entries(status.discard_error_categories).map(([code,count])=>`${discardErrors[code] ?? 'unclassified'}: ${count}`).join(' · ') || 'none'}</p><p>Discard checks more than 5 minutes overdue: {status.overdue_discard_checks} · oldest lag: {status.oldest_discard_check_lag_seconds} seconds</p><p>Document purges: {Object.entries(status.document_purges).map(([state,count])=>`${state}: ${count}`).join(' · ') || 'none'}</p><p>Document jobs queued or running for over {status.stale_after_seconds/60} minutes: {status.possibly_stale_jobs}</p></div>}
    <DiscardErrors api={api} onError={onError}/>
    <h3>Audit events</h3><p>Operational metadata only; purpose text and patient content are omitted. Pages contain up to 100 matching events, newest first.</p><form onSubmit={loadAudit}><label>Action (optional)<input name="action" maxLength={80} pattern="[A-Za-z0-9_]+"/></label><label>Outcome<select name="outcome"><option value="">All outcomes</option><option value="success">Success</option><option value="denied">Denied</option><option value="failed">Failed</option><option value="cancelled">Cancelled</option></select></label><button disabled={loading}>Review audit events</button></form>{events && (events.length ? events.map(item=><div className="row" key={item.id}><div><strong>{item.action} · {item.outcome}</strong><p>{new Date(item.created_at*1000).toLocaleString()} · {item.resource_type}</p><p>Actor: {item.actor_id ?? 'unknown'} · Patient: {item.patient_id ?? 'none'} · Correlation: {item.correlation_id}</p></div></div>) : <p>No matching events.</p>)}{nextCursor && <button className="secondary" disabled={loading} onClick={loadOlder}>Older audit events</button>}
  </section>;
}
