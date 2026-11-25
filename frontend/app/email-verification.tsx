'use client';
import {useState} from 'react';

type Api = (path:string, init?:RequestInit)=>Promise<Response>;
export default function EmailVerification({verified, api, onRefresh, onError}:{verified:boolean; api:Api; onRefresh:()=>Promise<void>; onError:(message:string)=>void}) {
  const [message,setMessage]=useState('');
  const [busy,setBusy]=useState(false);
  async function request() {
    setBusy(true); setMessage(''); onError('');
    try {await api('auth/email-verification/request',{method:'POST'}); setMessage('Verification link queued. Check the configured mail inbox.');}
    catch(e) {onError((e as Error).message);}
    finally {setBusy(false);}
  }
  return <section className="card"><h3>Email address</h3><p>{verified ? 'Email address verified.' : 'Email address not verified. Email verification is separate from clinician verification.'}</p>{!verified && <><button disabled={busy} onClick={()=>void request()}>Send verification link</button> <button className="secondary" disabled={busy} onClick={()=>void onRefresh()}>Refresh email status</button></>}{message && <p role="status">{message}</p>}</section>;
}
