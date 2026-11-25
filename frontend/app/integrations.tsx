'use client';

import {FormEvent, useCallback, useEffect, useState} from 'react';

type Api = (path:string, init?:RequestInit)=>Promise<Response>;
type Credential = {id:string; label:string; patient_id:string|null; scopes:string[]; expires_at:number; revoked_at:number|null};
const json = (body:object):RequestInit => ({method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});

export default function Integrations({api, patientId, onError}:{api:Api; patientId:string; onError:(message:string)=>void}) {
  const [credentials,setCredentials] = useState<Credential[]>([]);
  const [secret,setSecret] = useState('');
  const [busy,setBusy] = useState(false);
  const load = useCallback(async()=>{try {setCredentials(await (await api('integration-credentials')).json());} catch(e) {onError((e as Error).message);}},[api,onError]);
  useEffect(()=>{void load();},[load]);
  async function create(event:FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form=event.currentTarget; const data=new FormData(form); const scopes = data.getAll('scope').map(String);
    setSecret(''); onError('');
    if(!scopes.length) {onError('Select at least one integration permission.');return;}
    const patientScoped=scopes.some(scope=>['documents:read','profile:read','consultations:read'].includes(scope));
    if(patientScoped && !patientId) {onError('Select a patient in the workspace before granting patient-data access.');return;}
    setBusy(true);
    try {const response=await api('integration-credentials',json({label:data.get('label'),scopes,patient_id:patientScoped ? patientId : null,expires_at:new Date(String(data.get('expires_at'))).getTime()/1000})); const result=await response.json(); setSecret(result.token); form.reset(); await load();} catch(e) {onError((e as Error).message);} finally {setBusy(false);}
  }
  async function revoke(id:string) {setBusy(true); try {await api(`integration-credentials/${id}/revoke`,{method:'POST'}); await load();} catch(e){onError((e as Error).message);} finally{setBusy(false);}}
  return <section className="card document-list"><h3>Local integrations</h3><p>Create a separate credential for each MCP client. Each doctor patient-data permission requires both its corresponding viewing consent and separate integration consent from the patient. Credentials expire within 30 days and can be revoked here.</p><form onSubmit={create}><label>Client label<input name="label" maxLength={100} required/></label><fieldset><legend>Permissions</legend><label className="check"><input type="checkbox" name="scope" value="documents:read"/> Read selected patient documents</label><label className="check"><input type="checkbox" name="scope" value="profile:read"/> Read selected patient profile</label><label className="check"><input type="checkbox" name="scope" value="consultations:read"/> Read selected patient consultations</label><label className="check"><input type="checkbox" name="scope" value="knowledge:search"/> Search approved knowledge</label></fieldset><label>Expires on<input type="datetime-local" name="expires_at" required/></label><button disabled={busy}>Create credential</button></form>
    {secret && <div className="notice" role="status"><strong>Copy this credential now. It will not be shown again.</strong><code className="secret">{secret}</code><button className="secondary" onClick={()=>setSecret('')}>Hide credential</button></div>}
    {credentials.map(c=><div className="row" key={c.id}><div><strong>{c.label}</strong><p>{c.scopes.join(', ')} · {c.revoked_at ? 'revoked' : `expires ${new Date(c.expires_at*1000).toLocaleString()}`}</p>{c.patient_id && <p>Patient: {c.patient_id}</p>}</div>{!c.revoked_at && <button className="secondary" disabled={busy} onClick={()=>revoke(c.id)}>Revoke</button>}</div>)}
  </section>;
}
