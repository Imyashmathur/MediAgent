'use client';

import {FormEvent, useEffect, useState} from 'react';

export default function ResetPage() {
  const [token, setToken] = useState('');
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [done, setDone] = useState(false);
  useEffect(() => {
    const value = new URLSearchParams(window.location.hash.slice(1)).get('token');
    if (value) { setToken(value); window.history.replaceState(null, '', window.location.pathname); }
  }, []);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError(''); setMessage('');
    const form = new FormData(event.currentTarget);
    const payload = token ? {token, password: form.get('password')} : {email: form.get('email')};
    try {
      const response = await fetch(`/api/auth/password-reset/${token ? 'confirm' : 'request'}`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Please check your input.');
      setMessage(data.detail); setDone(true); setToken('');
    } catch (reason) { setError((reason as Error).message); }
  }
  return <main><header><a className="brand" href="/">✚ <span>MediAgent</span></a></header><section className="card auth"><h1>Reset password</h1>
    {message && <p role="status">{message}</p>}{error && <p role="alert" className="notice error">{error}</p>}
    {!done && <form onSubmit={submit}>{token ? <label>New password<input name="password" type="password" required minLength={12} maxLength={256} autoComplete="new-password"/></label> : <label>Email<input name="email" type="email" required maxLength={254} autoComplete="email"/></label>}<button>{token ? 'Change password' : 'Send reset link'}</button></form>}
    <p><a href="/">Return to sign in</a></p></section></main>;
}
