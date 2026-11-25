'use client';
import {FormEvent, useCallback, useEffect, useState} from 'react';
type API=(path:string, init?:RequestInit)=>Promise<Response>;
type Preference={enabled:boolean; response_style:'concise'|'balanced'|'detailed'|null};

export default function AssistantPreferences({api,onError}:{api:API; onError:(message:string)=>void}) {
  const [preference,setPreference]=useState<Preference|null>(null);
  const [status,setStatus]=useState('');
  const load=useCallback(async()=>{try {const result=await api('assistant/preferences'); setPreference(await result.json());} catch(e) {onError((e as Error).message);}},[api,onError]);
  useEffect(()=>{void load();},[load]);
  async function save(event:FormEvent<HTMLFormElement>) {
    event.preventDefault(); const style=new FormData(event.currentTarget).get('response_style');
    try {const result=await api('assistant/preferences',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({response_style:style})}); setPreference(await result.json()); setStatus('Preference saved.');} catch(e) {onError((e as Error).message);}
  }
  async function clear() {try {await api('assistant/preferences/clear',{method:'POST'}); setPreference({enabled:false,response_style:null}); setStatus('Saved preference deleted.');} catch(e) {onError((e as Error).message);}}
  return <details><summary>Optional assistant preference</summary><p>Save your preferred answer length for future model responses. This is your own setting, not a medical record. No preference is saved unless you choose one. It does not change safety checks.</p>{preference && <><p>Saved setting: {preference.enabled ? preference.response_style : 'none'}</p><form key={preference.response_style ?? 'none'} onSubmit={save}><label>Response style<select name="response_style" required defaultValue={preference.response_style ?? ''}><option value="" disabled>Select a style</option><option value="concise">Concise</option><option value="balanced">Balanced</option><option value="detailed">Detailed when evidence permits</option></select></label><button>Save preference</button></form>{preference.enabled && <button className="secondary" onClick={clear}>Delete saved preference</button>}</>}{status && <p role="status">{status}</p>}</details>;
}
