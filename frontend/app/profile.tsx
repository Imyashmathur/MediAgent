'use client';

import {FormEvent, useEffect, useState} from 'react';

type API = (path:string, init?:RequestInit) => Promise<Response>;
type Profile = Record<string, string | number | boolean | null | {doctor_id:string; relationship_id:string}[]>;
const patientFields = [
  ['date_of_birth', 'Date of birth', 'date', 0], ['demographics', 'Basic demographics', 'text', 2000],
  ['contact_details', 'Contact details', 'text', 2000], ['allergies', 'Allergies', 'text', 4000],
  ['medications', 'Existing medications', 'text', 4000], ['medical_history', 'Relevant medical history', 'text', 8000],
] as const;
const doctorFields = [
  ['specialization', 'Specialization', 'text', 200], ['qualifications', 'Qualifications', 'text', 4000],
  ['registration_identifier', 'Professional registration identifier', 'text', 200], ['experience', 'Experience', 'text', 4000],
  ['organization_affiliation', 'Organization affiliation (self-reported)', 'text', 200],
] as const;

export default function ProfileEditor({api, userId, role, onError, onVerificationChange}: {api:API; userId:string; role:'patient'|'doctor'; onError:(message:string)=>void; onVerificationChange:(status:string)=>void}) {
  const [profile, setProfile] = useState<Profile|null>(null);
  const [message, setMessage] = useState('');
  const path = role === 'patient' ? `patients/${encodeURIComponent(userId)}/profile` : `doctors/${encodeURIComponent(userId)}/profile`;
  useEffect(() => {let current=true; api(path).then(r=>r.json()).then(data=>{if(current) setProfile(data);}).catch(e=>{if(current) onError((e as Error).message);}); return ()=>{current=false;};}, [api, path, onError]);
  async function save(event:FormEvent<HTMLFormElement>) {
    event.preventDefault(); setMessage(''); const form = new FormData(event.currentTarget);
    const fields = role === 'patient' ? patientFields : doctorFields;
    const values = Object.fromEntries(fields.map(([key]) => [key, String(form.get(key) ?? '').trim() || null]));
    try {
      const response = await api(`profile/${role}`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values)});
      const result = await response.json();
      if (role === 'doctor') onVerificationChange(result.verification_status);
      const refreshed = await api(path); setProfile(await refreshed.json());
      setMessage(role === 'doctor' && result.verification_status === 'pending' ? 'Profile saved. Clinician verification is pending.' : 'Profile saved.');
    } catch (e) {onError((e as Error).message);}
  }
  const fields = role === 'patient' ? patientFields : doctorFields;
  return <section className="card document-list"><h3>Your profile</h3><p>Information is self-reported. Leave a field blank if it is unknown.</p>{profile && <><p>{String(profile.full_name)}{role === 'doctor' && <> · verification: {String(profile.verification_status)} · account: {profile.active ? 'active' : 'inactive'}</>}</p><form onSubmit={save} key={JSON.stringify(profile)}>{fields.map(([key,label,type,max])=><label key={key}>{label}{type === 'date' || max <= 200 ? <input name={key} type={type} maxLength={max || undefined} defaultValue={String(profile[key] ?? '')}/> : <textarea name={key} maxLength={max} rows={2} defaultValue={String(profile[key] ?? '')}/>}</label>)}<button>Save profile</button></form>{role === 'patient' && <p>Active care team: {Array.isArray(profile.care_team) && profile.care_team.length ? profile.care_team.map(item=>item.doctor_id).join(', ') : 'None listed'}</p>}</>}{message && <p role="status">{message}</p>}</section>;
}

export function PatientProfileViewer({api, patientId, onError}: {api:API; patientId:string; onError:(message:string)=>void}) {
  const [profile, setProfile] = useState<Profile|null>(null);
  useEffect(()=>{setProfile(null);}, [patientId]);
  async function open() {try {const r=await api(`patients/${encodeURIComponent(patientId)}/profile`); setProfile(await r.json());} catch(e) {setProfile(null); onError((e as Error).message);}}
  return <section className="card document-list"><h3>Patient profile</h3><p>Requires a separate profile consent grant. Information is patient-reported and may be incomplete.</p><button disabled={!patientId} onClick={open}>Open patient profile</button>{profile && <div><p>{String(profile.full_name)}</p>{patientFields.map(([key,label])=><p key={key}><strong>{label}:</strong> {String(profile[key] ?? 'Not provided')}</p>)}</div>}</section>;
}

export function DoctorProfileViewer({api, onError}: {api:API; onError:(message:string)=>void}) {
  const [profile, setProfile] = useState<Profile|null>(null);
  const [message, setMessage] = useState('');
  const [history, setHistory] = useState<{id:string; reviewer_id:string; profile_revision:number; decision:string; created_at:number}[]>([]);
  async function loadHistory(id:string) {const response=await api(`admin/doctors/${encodeURIComponent(id)}/verification-history`); setHistory(await response.json());}
  async function open(event:FormEvent<HTMLFormElement>) {
    event.preventDefault(); const id=String(new FormData(event.currentTarget).get('doctor_id') ?? '');
    setMessage('');
    try {const r=await api(`doctors/${encodeURIComponent(id)}/profile`); const data=await r.json(); await loadHistory(id); setProfile(data);} catch(e) {setProfile(null); setHistory([]); onError((e as Error).message);}
  }
  async function decide(verified:boolean) {
    if (!profile) return;
    try {
      const result=await api(`admin/doctors/${encodeURIComponent(String(profile.user_id))}/verification`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({verified,expected_profile_revision:profile.profile_revision,expected_verification_revision:profile.verification_revision,expected_status:profile.verification_status})});
      const data=await result.json(); setProfile({...profile,verification_status:data.verification_status,verification_revision:data.verification_revision}); await loadHistory(String(profile.user_id)); setMessage('Verification decision recorded.');
    } catch(e) {setProfile(null); setHistory([]); onError((e as Error).message);}
  }
  return <section className="card document-list"><h3>Review clinician profile</h3><p>Profile details are self-reported and must be independently verified before a decision.</p><form onSubmit={open}><label>Doctor ID<input name="doctor_id" required/></label><button>View profile</button></form>{message && <p role="status">{message}</p>}{profile && <div><p>{String(profile.full_name)} · {String(profile.verification_status)} · {profile.active ? 'active' : 'inactive'} · profile revision {String(profile.profile_revision)}</p>{doctorFields.map(([key,label])=><p key={key}><strong>{label}:</strong> {String(profile[key] ?? 'Not provided')}</p>)}<button disabled={!profile.active} onClick={()=>decide(true)}>Mark verified</button>{' '}<button className="secondary" disabled={!profile.active} onClick={()=>decide(false)}>Reject verification</button><h4>Decision history</h4>{history.length ? history.map(item=><p key={item.id}>{item.decision} · profile revision {item.profile_revision} · reviewer {item.reviewer_id} · {new Date(item.created_at*1000).toLocaleString()}</p>) : <p>No decisions recorded.</p>}</div>}</section>;
}
