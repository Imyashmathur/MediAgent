'use client';
import {useEffect, useState} from 'react';

export default function VerifyEmailPage() {
  const [token,setToken]=useState('');
  const [message,setMessage]=useState('');
  const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);
  useEffect(()=>{
    const value=new URLSearchParams(window.location.hash.slice(1)).get('token');
    if(value) {setToken(value); window.history.replaceState(null,'',window.location.pathname);}
  },[]);
  async function confirm() {
    setBusy(true); setError('');
    try {
      const response=await fetch('/api/auth/email-verification/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})});
      const data=await response.json();
      if(!response.ok) throw new Error(typeof data.detail==='string' ? data.detail : 'Unable to verify email address.');
      setToken(''); setMessage(data.detail);
    } catch(reason) {setError((reason as Error).message);}
    finally {setBusy(false);}
  }
  return <main><header><a className="brand" href="/">✚ <span>MediAgent</span></a></header><section className="card auth"><h1>Verify email address</h1>{message && <p role="status">{message}</p>}{error && <p role="alert" className="notice error">{error}</p>}{token ? <><p>Confirm the email address associated with this link.</p><button disabled={busy} onClick={()=>void confirm()}>Verify email address</button></> : !message && <p>No verification link is available. Request a new link from your account.</p>}<p><a href="/">Return to your account</a></p></section></main>;
}
