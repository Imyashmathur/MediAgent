'use client';

import EmailVerification from './email-verification';
import IncompleteUploads from './incomplete-uploads';

import {FormEvent, useCallback, useEffect, useRef, useState} from 'react';
import ExtractionReview, {ExtractionData, ExtractionVersion} from './extraction-review';
import Knowledge from './knowledge';
import {KnowledgeReviewDoctor} from './knowledge-reviews';
import Assistant from './assistant';
import Integrations from './integrations';
import ProfileEditor, {DoctorProfileViewer} from './profile';
import Consultations from './consultations';
import CareMessages from './care-messages';
import Operations from './operations';
import DocumentPreview from './document-preview';
import Worklist from './worklist';
import Notifications from './notifications';
import NoteComparison from './note-comparison';
import NoteSource from './note-source';
import NoteSourceFields, {SourceField} from './note-source-fields';
import AdminUsers from './admin-users';
import ExtractedRecords from './extracted-records';
import DocumentSummaries from './document-summaries';
import ProcessingHistory from './processing-history';
import ConsentHistory, {Consent} from './consent-history';
import CareTeam from './care-team';
import PatientAccessHistory from './patient-access-history';
import './styles.css';

type User = {id: string; full_name: string; email: string; role: 'patient' | 'doctor' | 'admin'; verification_status: string; email_verified_at?: number|null};
type Document = {id: string; filename: string; content_type:string; review_status: string; scan_status:'not_scanned'|'no_threat_detected'};
type Relationship = {id: string; patient_id: string; doctor_id: string; status: string};
type Note = {note_id:string; version_id:string; version:number; content:string; status:string; author_id:string; reviewer_id:string | null; reviewed_at:number | null; review_comment:string | null; consultation_id:string | null; source_document_id:string | null; source_processing_job_id:string | null; source_extraction_version:number | null; source_fields:SourceField[]; assistant_referenced:boolean; ai_assisted:boolean};
type Processing = {document?:Document; job_id?:string; is_latest?:boolean; status:string; review_status:string; processor_version?:string; processor_details?:{pipeline:string; pdf_ocr_mode:string; ocr:{provider:string;version:string;languages:string[]}|null;vision?:{provider:string;model:string;pages_processed:number;truncated:boolean}|null;document_type?:{category:string;reason:string;evidence:{page:number;location_type:string;layer:string;quote:string}|null}}|null; created_at?:number; error_code?:string | null; pages:{page_number:number; method:string; text:string; supplemental_ocr_text?:string;vision_text?:string;vision_warnings?:string[]}[]; structured_versions?:ExtractionVersion[]};

let refreshInFlight: Promise<Response> | null = null;
let signingOut = false;

async function api(path: string, init?: RequestInit, retry = true): Promise<Response> {
  const response = await fetch(`/api/${path}`, {...init, cache: 'no-store'});
  if (response.status === 401 && retry && !signingOut && path !== 'auth/login' && path !== 'auth/refresh') {
    refreshInFlight ??= fetch('/api/auth/refresh', {method: 'POST'}).finally(() => { refreshInFlight = null; });
    const refreshed = await refreshInFlight;
    if (refreshed.ok) return api(path, init, false);
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(typeof data.detail === 'string' ? data.detail : 'Request failed');
  }
  return response;
}

function json(body: object): RequestInit {return {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};}

export default function Home() {
  const [user, setUser] = useState<User | null>(null);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [documents, setDocuments] = useState<Document[]>([]);
  const [documentCursor, setDocumentCursor] = useState<string|null>(null);
  const [loadingDocuments, setLoadingDocuments] = useState(false);
  const [relationships, setRelationships] = useState<Relationship[]>([]);
  const [consents, setConsents] = useState<Consent[]>([]);
  const [notes, setNotes] = useState<Note[]>([]);
  const [reviewPolicy, setReviewPolicy] = useState<'self_or_independent'|'independent'|null>(null);
  useEffect(()=>{let current=true; setReviewPolicy(null); if(user?.role === 'doctor') void api('clinical-review-policy').then(r=>r.json()).then(data=>{if(current && (data.note_review_policy === 'independent' || data.note_review_policy === 'self_or_independent')) setReviewPolicy(data.note_review_policy);}).catch(()=>{if(current) setError('Unable to load clinical review policy.');}); return ()=>{current=false;};},[user?.id,user?.role]);
  const [deletionRevision, setDeletionRevision] = useState(0);
  const [deletions, setDeletions] = useState<{id:string; deleted_at:number; purge_status:string; purged_at:number|null; storage_kind:'local'|'object'}[] | null>(null);
  const [selectedDocument, setSelectedDocument] = useState('');
  const [previewPage, setPreviewPage] = useState(1);
  const [processing, setProcessing] = useState<Processing | null>(null);
  const [citedExtractionVersion, setCitedExtractionVersion] = useState<number|null>(null);
  const [activeNotePatient, setActiveNotePatient] = useState('');
  const [patientId, setPatientId] = useState('');
  const [visionRequested, setVisionRequested] = useState(false);
  const [visionCapability, setVisionCapability] = useState<{vision_available:boolean; provider?:string; model?:string; service_url?:string; configuration?:string}|null>(null);
  useEffect(()=>{let current=true; if(user?.role === 'patient') {void api('document-processing/capabilities').then(r=>r.json()).then(data=>{if(current)setVisionCapability(data);}).catch(()=>{if(current)setVisionCapability(null);});} else {setVisionCapability(null); setVisionRequested(false);} return ()=>{current=false;};},[user]);

  const workspaceRevision = useRef(0);
  const selectedPatientRef = useRef('');
  const documentsRevision = useRef(0);
  const notesRevision = useRef(0);
  const processingRevision = useRef(0);
  function invalidateWorkspace() {workspaceRevision.current++; documentsRevision.current++; notesRevision.current++; processingRevision.current++; selectedPatientRef.current='';}
  function selectPatient(id:string) {invalidateWorkspace(); selectedPatientRef.current=id; setPatientId(id); setDocuments([]); setDocumentCursor(null); setNotes([]); setActiveNotePatient(''); setSelectedDocument(''); setProcessing(null);}

  const load = useCallback(async (current: User, selectedPatient = '') => {
    if (current.role === 'admin') return;
    const revision = workspaceRevision.current;
    const documentRequest = ++documentsRevision.current;
    const relationshipsResponse = await api('relationships');
    const relationshipData = await relationshipsResponse.json();
    if (revision !== workspaceRevision.current) return;
    setRelationships(relationshipData);
    if (current.role === 'patient') {
      const [docs, grants, noteResponse] = await Promise.all([api(`patients/${current.id}/documents`), api('consents'), api(`patients/${current.id}/notes`)]);
      const [documentData, consentData, noteData] = await Promise.all([docs.json(), grants.json(), noteResponse.json()]);
      if (revision !== workspaceRevision.current) return;
      if (documentRequest === documentsRevision.current) {setDocuments(documentData); setDocumentCursor(docs.headers.get('x-next-cursor'));}
      setConsents(consentData); setNotes(noteData); setActiveNotePatient(current.id);
    } else if (selectedPatient) {
      const docs = await api(`patients/${encodeURIComponent(selectedPatient)}/documents`);
      const documentData = await docs.json();
      if (revision === workspaceRevision.current && documentRequest === documentsRevision.current && selectedPatient === selectedPatientRef.current) {setDocuments(documentData); setDocumentCursor(docs.headers.get('x-next-cursor'));}
    }
  }, []);

  useEffect(() => {api('me').then(r => r.json()).then((u: User) => {setUser(u); return load(u);}).catch(() => {});}, [load]);

  async function submitAuth(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError(''); setMessage('');
    const form = new FormData(event.currentTarget);
    try {
      const email = String(form.get('email')); const password = String(form.get('password'));
      if (mode === 'register') await api('auth/register', json({email, password, full_name: form.get('full_name'), role: form.get('role')}));
      const response = await api('auth/login', json({email, password}));
      const data = await response.json(); setUser(data.user); await load(data.user);
    } catch (e) {setError((e as Error).message);}
  }

  async function submitUpload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError('');
    const form = event.currentTarget;
    try {await api('documents', {method:'POST', body:new FormData(form)}); form.reset(); await load(user!); setMessage('Document uploaded and marked unverified.');} catch(e) {setError((e as Error).message);}
  }

  async function deactivate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError('');
    const form = new FormData(event.currentTarget);
    try {
      await api('account/deactivate', json({password: form.get('password')}));
      invalidateWorkspace(); setUser(null); setMode('login'); setPatientId(''); setDocuments([]); setDeletions(null); setRelationships([]); setConsents([]); setNotes([]); setSelectedDocument(''); setProcessing(null); setActiveNotePatient('');
      setMessage('Your account has been deactivated. Your records have not been deleted.');
    } catch (e) {setError((e as Error).message);}
  }

  async function signOut() {
    if (signingOut) return;
    signingOut=true; setError(''); setMessage('');
    try {
      // Finish any local refresh before revoking its newest credential.
      if (refreshInFlight) await refreshInFlight.catch(()=>undefined);
      const response = await fetch('/api/auth/logout', {method:'POST', cache:'no-store'});
      invalidateWorkspace(); setUser(null); setMode('login'); setPatientId(''); setDocuments([]); setDeletions(null); setRelationships([]); setConsents([]); setNotes([]); setActiveNotePatient(''); setSelectedDocument(''); setProcessing(null);
      if (!response.ok) setError('Sign-out could not be confirmed; the server session may still be active.');
    } catch {
      setError('Sign-out could not reach the application. Try again when it is available.');
    } finally {signingOut=false;}
  }

  async function addRelationship(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError('');
    const form = event.currentTarget; const data = new FormData(form);
    try {await api('relationships', json({doctor_id: data.get('doctor_id'), purpose: data.get('purpose')})); form.reset(); await load(user!); setMessage('Care relationship created. Grant consent to allow document access.');} catch(e) {setError((e as Error).message);}
  }

  async function changeRelationship(id:string, action:'end'|'restore') {
    try {
      await api(`relationships/${id}/${action}`, {method:'POST'});
      if (user?.role === 'doctor' && relationships.find(r=>r.id===id)?.patient_id === patientId) selectPatient('');
      await load(user!);
      setMessage(action === 'end' ? 'Care relationship ended.' : 'Care relationship restored. Grant new permissions to allow access.');
    } catch(e) {setError((e as Error).message);}
  }

  async function grantConsent(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError(''); const form = event.currentTarget; const data = new FormData(form);
    const expires = new Date(String(data.get('expires_at'))).getTime() / 1000;
    const start = String(data.get('effective_at') ?? '');
    const effectiveAt = start ? new Date(start).getTime() / 1000 : null;
    try {await api('consents', json({relationship_id: data.get('relationship_id'), scope: data.get('scope'), purpose: data.get('purpose'), effective_at: effectiveAt, expires_at: expires})); form.reset(); await load(user!); setMessage('Permission recorded.');} catch(e) {setError((e as Error).message);}
  }

  async function revoke(id: string) {
    try {await api(`consents/${id}/withdraw`, {method:'POST'}); await load(user!); setMessage('Consent withdrawn.');} catch(e) {setError((e as Error).message);}
  }

  async function openNotes() {
    const revision=workspaceRevision.current, request=++notesRevision.current, target=patientId;
    if (target !== selectedPatientRef.current) return;
    try {const response = await api(`patients/${encodeURIComponent(target)}/notes`); const data=await response.json(); if (revision !== workspaceRevision.current || request !== notesRevision.current) return; setNotes(data); setActiveNotePatient(target); setError('');} catch(e) {if (revision !== workspaceRevision.current || request !== notesRevision.current) return; setNotes([]); setActiveNotePatient(''); setError((e as Error).message);}
  }

  async function createNote(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    const source=String(data.get('source_document_id') ?? '').trim() || null;
    if (data.get('link_processing') && (!source || source !== selectedDocument || !processing?.job_id || processing.status !== 'completed')) {setError('Select the open document and a completed processing run before linking it.'); return;}
    const extraction = citedExtractionVersion ?? processing?.structured_versions?.[0]?.version ?? null;
    const sourceFields = data.getAll('source_field').map(value=>{const key=String(value), field=JSON.parse(key) as SourceField, quote=String(data.get(`note_quote:${key}`) ?? ''); return quote ? {...field,note_quote:quote} : field;});
    if (sourceFields.length > 30 || (sourceFields.length && !data.get('link_extraction'))) {setError('Select at most 30 fields and link their transcription version.'); return;}
    if (data.get('link_extraction') && (!data.get('link_processing') || extraction === null)) {setError('Link a completed processing run and an available transcription version.'); return;}
    try {await api(`patients/${encodeURIComponent(activeNotePatient)}/notes`, json({content:data.get('content'), consultation_id:String(data.get('consultation_id') ?? '').trim() || null, source_document_id:source, source_processing_job_id:data.get('link_processing') ? processing?.job_id : null, source_extraction_version:data.get('link_extraction') ? extraction : null, source_fields:sourceFields})); form.reset(); await openNotes(); setMessage('Draft created. Submit it for clinician review.');} catch(e) {setError((e as Error).message);}
  }

  async function noteAction(note: Note, action: 'submit' | 'approve' | 'reject', reason?:string) {
    try {const base = `notes/${note.note_id}/versions/${note.version_id}`; await api(`${base}/${action === 'submit' ? 'submit' : 'review'}`, action === 'submit' ? {method:'POST'} : json({decision:action, reason:reason ?? null})); await openNotes(); setMessage(`Note ${action === 'submit' ? 'submitted for review' : action === 'approve' ? 'approved' : 'rejected'}.`);} catch(e) {setError((e as Error).message);}
  }

  async function reviseNote(event: FormEvent<HTMLFormElement>, note: Note) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    try {await api(`notes/${note.note_id}/versions/${note.version_id}/revise`, json({content:data.get('content'),clear_document_source:data.get('clear_document_source') === 'on'})); await openNotes(); setMessage('Revised draft saved as a new version.');} catch(e) {setError((e as Error).message);}
  }

  async function amendNote(event: FormEvent<HTMLFormElement>, note: Note) {
    event.preventDefault(); const form = event.currentTarget; const data = new FormData(form);
    try {await api(`notes/${note.note_id}/versions`, json({content:data.get('content'),...(data.get('clear_document_source') === 'on' ? {source_document_id:null} : {})})); form.reset(); await openNotes(); setMessage('Amendment saved as a new draft.');} catch(e) {setError((e as Error).message);}
  }

  async function loadDeletions() {
    try {setDeletions(await (await api('documents/deletions')).json());} catch(e) {setDeletions(null); setError((e as Error).message);}
  }

  async function deleteDocument(event:FormEvent<HTMLFormElement>, id:string) {
    event.preventDefault(); const form=event.currentTarget; const data=new FormData(form);
    try {await api(`documents/${id}/delete`,json({confirmation:data.get('confirmation')})); setSelectedDocument(''); setProcessing(null); setDeletionRevision(v=>v+1); await load(user!); await loadDeletions(); setMessage('Document access removed; file deletion is queued.');} catch(e) {setError((e as Error).message);}
  }

  async function loadOlderDocuments() {
    const cursor=documentCursor, target=user?.role === 'patient' ? user.id : patientId;
    if (!cursor || !target || loadingDocuments) return;
    const revision=workspaceRevision.current, request=++documentsRevision.current; setLoadingDocuments(true);
    try {
      const response=await api(`patients/${encodeURIComponent(target)}/documents?cursor=${encodeURIComponent(cursor)}`);
      const page:Document[]=await response.json();
      if (revision !== workspaceRevision.current || request !== documentsRevision.current) return;
      setDocuments(previous=>{const seen=new Set(previous.map(doc=>doc.id)); return [...previous,...page.filter(doc=>!seen.has(doc.id))];});
      setDocumentCursor(response.headers.get('x-next-cursor'));
    } catch(error) {if(revision === workspaceRevision.current && request === documentsRevision.current) {setDocuments([]); setDocumentCursor(null); setError((error as Error).message);}}
    finally {setLoadingDocuments(false);}
  }

  async function viewProcessing(id: string, jobId?:string, extractionVersion?:number|null) {
    const revision=workspaceRevision.current, request=++processingRevision.current;
    if (id !== selectedDocument) {setSelectedDocument(''); setProcessing(null);}
    try {const response = await api(`documents/${id}/processing${jobId ? `?job_id=${encodeURIComponent(jobId)}` : ''}`); const data=await response.json(); if (revision !== workspaceRevision.current || request !== processingRevision.current) return; setProcessing(data); setCitedExtractionVersion(extractionVersion ?? null); if(id !== selectedDocument) {setPreviewPage(1); setVisionRequested(false);} setSelectedDocument(id); setError('');} catch(e) {if (revision !== workspaceRevision.current || request !== processingRevision.current) return; setSelectedDocument(''); setProcessing(null); setError((e as Error).message);}
  }

  async function requestProcessing(id: string) {
    const revision=workspaceRevision.current, request=++processingRevision.current;
    try {await api(`documents/${id}/processing`, json(visionRequested ? {vision_requested:true, vision_config_fingerprint:visionCapability?.configuration} : {vision_requested:false})); if (revision !== workspaceRevision.current || request !== processingRevision.current) return; await viewProcessing(id); setMessage('Processing requested. Refresh the result to check progress.');} catch(e) {setError((e as Error).message);}
  }

  async function reviewExtraction(version:number, data:ExtractionData) {
    if (!processing?.job_id || !selectedDocument) return;
    const revision=workspaceRevision.current, request=++processingRevision.current;
    try {await api(`documents/${selectedDocument}/processing/${processing.job_id}/review`, json({expected_version:version, data})); if (revision !== workspaceRevision.current || request !== processingRevision.current) return; await viewProcessing(selectedDocument); setMessage('Transcription reviewed and a new version saved.');} catch(e) {setError((e as Error).message);}
  }

  const selectedContentType = (documents.find(d=>d.id===selectedDocument) ?? processing?.document)?.content_type;
  return <main><header><a className="brand" href="/">✚ <span>MediAgent</span></a><span className="tag">SYNTHETIC DEVELOPMENT DATA</span>{user && <button className="quiet" onClick={()=>void signOut()}>Sign out</button>}</header>
    <section className="hero"><p className="eyebrow">DOCTOR-SUPERVISED CARE</p><h1>Your health information, shared with care.</h1><p>A private workspace for patients and their care team. Uploaded documents remain unverified until reviewed by a clinician.</p></section>
    {error && <p className="notice error" role="alert">{error}</p>}{message && <p className="notice" role="status">{message}</p>}
    {!user ? <section className="card auth"><h2>{mode === 'login' ? 'Welcome back' : 'Create an account'}</h2><p>Use synthetic information in this development build.</p><form onSubmit={submitAuth}>
      {mode === 'register' && <><label>Full name<input name="full_name" required maxLength={200}/></label><label>Account type<select name="role"><option value="patient">Patient</option><option value="doctor">Doctor</option></select></label></>}
      <label>Email<input name="email" type="email" required autoComplete="email"/></label><label>Password<input name="password" type="password" required minLength={12} autoComplete={mode === 'login' ? 'current-password' : 'new-password'}/></label><button>{mode === 'login' ? 'Sign in' : 'Create account'}</button></form>{mode === 'login' && <p><a href="/reset">Forgot password?</a></p>}<button className="link" onClick={()=>{setMode(mode === 'login' ? 'register' : 'login'); setError('');}}>{mode === 'login' ? 'Create a patient or doctor account' : 'Already have an account? Sign in'}</button></section>
    : <><div className="heading"><div><p className="eyebrow">YOUR WORKSPACE</p><h2>Hello, {user.full_name}</h2><p>{user.role === 'doctor' ? `Clinician verification: ${user.verification_status}` : user.role}</p></div><div className="identity">Your ID <code>{user.id}</code></div></div>
      {user.role === 'patient' && <div className="grid"><section className="card"><h3>Upload a document</h3><p>PDF, DOCX, JPEG or PNG, up to 10 MB.</p><form onSubmit={submitUpload}><label>Medical document<input type="file" name="file" accept=".pdf,.docx,.jpg,.jpeg,.png" required/></label><button>Upload document</button></form></section>
      <section className="card"><h3>Add a care team member</h3><p>Enter a verified doctor’s ID. A relationship alone does not grant access.</p><form onSubmit={addRelationship}><label>Doctor ID<input name="doctor_id" required/></label><label>Purpose<input name="purpose" required minLength={3}/></label><button>Create relationship</button></form></section>
      <CareTeam relationships={relationships} role="patient" onEnd={id=>void changeRelationship(id,'end')} onRestore={id=>void changeRelationship(id,'restore')}/>
      <section className="card"><h3>Grant access</h3><p>Choose the information and when access ends; you can withdraw it at any time. Document review and each integration permission also require the corresponding viewing permission.</p><form onSubmit={grantConsent}><label>Care relationship<select name="relationship_id" required defaultValue=""><option value="" disabled>Select a doctor</option>{relationships.filter(r=>r.status==='active').map(r=><option value={r.id} key={r.id}>{r.doctor_id}</option>)}</select></label><label>Permission<select name="scope"><option value="read_documents">View uploaded documents</option><option value="read_profile">View patient profile</option><option value="consultations">View and record consultations</option><option value="care_messages">Exchange care team messages</option><option value="review_documents">Review document transcriptions</option><option value="clinical_notes">Read, create and review clinical notes</option><option value="assistant">Use patient-scoped assistant</option><option value="integration_access">Allow doctor document integrations</option><option value="profile_integration">Allow doctor profile integrations</option><option value="consultations_integration">Allow doctor consultation integrations</option></select></label><label>Purpose<input name="purpose" required minLength={3}/></label><label>Starts on (optional)<input type="datetime-local" name="effective_at"/></label><p>Leave the start blank for immediate access, or choose a future start time.</p><label>Expires on<input type="datetime-local" name="expires_at" required/></label><button disabled={!relationships.some(r=>r.status==='active')}>Grant access</button></form></section>
      <ConsentHistory consents={consents} onWithdraw={revoke} onRefresh={()=>{void load(user).catch(e=>setError((e as Error).message));}}/></div>}
      {user.role === 'doctor' && <><CareTeam relationships={relationships} role="doctor" onEnd={id=>void changeRelationship(id,'end')} onRestore={()=>{}}/><Worklist api={api} onSelect={id=>{selectPatient(id); void load(user, id).catch(e=>setError((e as Error).message));}} onError={setError}/><section className="card"><h3>Patient workspace</h3><p>Document access and clinical-note access require separate patient consent.</p><label>Patient ID<input value={patientId} onChange={e=>selectPatient(e.target.value)}/></label><button disabled={!patientId} onClick={async()=>{const revision=workspaceRevision.current; try {await load(user, patientId); if (revision === workspaceRevision.current) setError('');} catch(e) {if (revision === workspaceRevision.current) {setDocuments([]); setError((e as Error).message);}}}}>Open patient documents</button>{' '}<button disabled={!patientId} onClick={openNotes}>Open clinical notes</button><p>Your care relationships: {relationships.length}</p></section></>}
      {user.role === 'admin' && <DoctorProfileViewer api={api} onError={setError}/>}
      {user.role === 'admin' && <AdminUsers api={api} onError={setError}/>}
      {user.role === 'admin' && <Operations api={api} onError={setError}/>}
      <EmailVerification verified={user.email_verified_at != null} api={api} onRefresh={async()=>{try {const response=await api('me'); setUser(await response.json());} catch(e) {setError((e as Error).message);}}} onError={setError}/> {user.role !== 'admin' && <ProfileEditor api={api} userId={user.id} role={user.role} onError={setError} onVerificationChange={status=>setUser(current=>current ? {...current, verification_status:status} : null)}/>} 
      {user.role !== 'admin' && <Consultations key={user.role === 'patient' ? user.id : patientId} api={api} patientId={user.role === 'patient' ? user.id : patientId} isDoctor={user.role === 'doctor'} onError={setError}/>}
      {user.role !== 'admin' && <CareMessages api={api} relationships={relationships} userId={user.id} onError={setError}/>}
      {user.role !== 'admin' && <Notifications api={api} role={user.role} onError={setError} onOpenProcessing={(documentId,jobId)=>void viewProcessing(documentId,jobId)}/>}
      {user.role !== 'admin' && (user.role === 'patient' || patientId) && <ExtractedRecords key={`${user.role === 'patient' ? user.id : patientId}:${deletionRevision}`} api={api} patientId={user.role === 'patient' ? user.id : patientId} onOpenDocument={id=>{void viewProcessing(id);}} onError={setError}/>}
      {user.role === 'patient' && <IncompleteUploads api={api} onError={setError} onCompleted={async()=>{await load(user);}}/>} {user.role === 'patient' && <section className="card"><h3>Document deletion status</h3><button className="secondary" onClick={loadDeletions}>Refresh deletion status</button>{deletions && (deletions.length ? deletions.map(item=><p key={item.id}>Document {item.id} · {item.purge_status === 'complete' ? (item.storage_kind === 'object' ? 'Stored object version removed' : 'Local file removed') : 'File removal pending'}</p>) : <p>No deletion requests.</p>)}</section>}
      {user.role !== 'admin' && <section className="card document-list"><h3>Documents</h3>{documents.length ? documents.map(d=><div className="row" key={d.id}><div><strong>{d.filename}</strong><p>{d.review_status} · {d.scan_status === 'not_scanned' ? 'Not malware-scanned' : 'No threat detected by scanner'}</p></div><div><button className="secondary" onClick={()=>viewProcessing(d.id)}>View processing</button>{' '}<a className="button secondary" href={`/api/documents/${d.id}/download`}>Download</a>{user.role === 'patient' && <details><summary>Delete document</summary><p>This removes access and deletes the original and extracted data. Existing notes, conversations, backups, and information already shared may retain copies. File removal is queued and can take time.</p><form onSubmit={event=>deleteDocument(event,d.id)}><label>Type DELETE to confirm<input name="confirmation" required pattern="DELETE" autoComplete="off"/></label><button>Request deletion</button></form></details>}</div></div>) : <p>No documents to display.</p>}{documentCursor && <button disabled={loadingDocuments} className="secondary" onClick={()=>void loadOlderDocuments()}>Load older documents</button>}{selectedDocument && processing && <article className="note"><h4>Document processing{processing.document && ` · ${processing.document.filename}`}</h4><ProcessingHistory key={selectedDocument} api={api} documentId={selectedDocument} onSelect={job=>void viewProcessing(selectedDocument,job)} onError={setError}/>{processing.is_latest === false && <p>Historical processing run · read only. Select refresh to return to the latest run.</p>}<p>Result: {processing.status.replace('_',' ')} · Source text unverified. OCR can omit or misread text; compare with the original.</p>{processing.error_code && <p>Processing could not complete ({processing.error_code}).</p>}{processing.processor_details && <p>Processor: {processing.processor_details.pipeline}{processing.processor_details.ocr && <> · OCR {processing.processor_details.ocr.provider} {processing.processor_details.ocr.version} · {processing.processor_details.ocr.languages.join(' + ')}</>}</p>}{processing.processor_details?.document_type && <p>Document type hint · unverified: {processing.processor_details.document_type.category.replace(/_/g,' ')}{processing.processor_details.document_type.reason === 'conflicting_headings' && ' (conflicting headings)'}{processing.processor_details.document_type.evidence && <> · {processing.processor_details.document_type.evidence.location_type === 'text_segment' ? 'text segment' : 'page'} {processing.processor_details.document_type.evidence.page}{processing.processor_details.document_type.evidence.layer === 'supplemental_ocr' && ' · supplemental OCR'}: “{processing.processor_details.document_type.evidence.quote}”</>}</p>}{processing.processor_details?.vision && <p>Vision model: {processing.processor_details.vision.model} · {processing.processor_details.vision.pages_processed} page(s){processing.processor_details.vision.truncated && ' · remaining pages not sent'}</p>}{processing.is_latest !== false && user.role === 'patient' && visionCapability?.vision_available && selectedContentType !== 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' && selectedContentType !== 'image/jpeg' && selectedContentType !== 'image/png' && <label className="check"><input type="checkbox" checked={visionRequested} onChange={e=>setVisionRequested(e.target.checked)}/> Also send up to the first three page images to {visionCapability.provider} model {visionCapability.model} at {visionCapability.service_url}. Vision text may be invented or wrong and is not clinical interpretation.</label>}{processing.is_latest !== false && user.role === 'patient' && (['not_requested','failed','completed'].includes(processing.status) || (['queued','running'].includes(processing.status) && processing.created_at && Date.now()/1000 - processing.created_at > 300)) && <button onClick={()=>requestProcessing(selectedDocument)}>{processing.status === 'not_requested' ? 'Process document' : 'Process again'}</button>}{' '}<button className="secondary" onClick={()=>viewProcessing(selectedDocument)}>Refresh status</button><div className="review-layout">{(documents.find(d=>d.id===selectedDocument) ?? processing.document)?.content_type === 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' ? <section className="preview-panel"><h4>Original DOCX</h4><p>Visual preview is unavailable. Download the original to inspect its layout; extracted text is shown as logical segments, not pages.</p><a className="button secondary" href={`/api/documents/${selectedDocument}/download`}>Download original</a></section> : <DocumentPreview api={api} documentId={selectedDocument} page={previewPage} setPage={setPreviewPage}/>}<div className="review-detail">{processing.status === 'completed' && processing.job_id && <DocumentSummaries key={`${selectedDocument}:${processing.job_id}`} api={api} documentId={selectedDocument} jobId={processing.job_id} readOnly={processing.is_latest === false} onError={setError} onPageSelect={setPreviewPage}/>}<ExtractionReview key={`${processing.job_id}:${citedExtractionVersion ?? "latest"}`} versions={processing.structured_versions ?? []} selectedVersion={citedExtractionVersion} canReview={user.role === 'doctor' && processing.is_latest !== false && citedExtractionVersion === null} onReview={reviewExtraction} onPageSelect={setPreviewPage}/>{processing.pages.map(page=><section key={page.page_number} className="note"><button className="link" onClick={()=>setPreviewPage(page.page_number)}>{page.method === 'docx' ? 'Text segment' : 'Page'} {page.page_number} · {page.method === 'ocr' ? 'OCR' : page.method === 'docx' ? 'DOCX text' : 'native text'}</button><p className="note-content">{page.text || 'No text recognized.'}</p>{page.supplemental_ocr_text !== undefined && <><strong>Supplemental OCR · unverified</strong><p>May duplicate or misread native text. Labeled values can be transcribed separately; verify both layers against the original.</p><p className="note-content">{page.supplemental_ocr_text || 'No additional text recognized.'}</p></>}{page.vision_text !== undefined && <><strong>Vision transcription · unverified</strong><p>Model-generated text may be wrong or invented. It is not used in automatic structured transcription; compare with the original.</p><p className="note-content">{page.vision_text || 'No text recognized.'}</p>{page.vision_warnings?.map((warning,i)=><p key={i}>Model warning: {warning}</p>)}</>}</section>)}</div></div></article>}</section>}
      {user.role !== 'admin' && activeNotePatient && <section className="card document-list"><h3>Clinical notes</h3>{user.role === 'doctor' && <p>{reviewPolicy === 'independent' ? 'A different authorized doctor must review each note.' : reviewPolicy === 'self_or_independent' ? 'Authorized doctors may review their own notes.' : 'Loading review policy…'}</p>}{user.role === 'doctor' && <form onSubmit={createNote}><label>New note draft<textarea name="content" required maxLength={20000} rows={5}/></label><label>Consultation ID (optional)<input name="consultation_id" maxLength={36}/></label><label>Source document (optional)<select name="source_document_id" defaultValue=""><option value="">No linked document</option>{activeNotePatient === patientId && documents.map(d=><option key={d.id} value={d.id}>{d.filename}</option>)}{activeNotePatient === patientId && processing?.document && !documents.some(d=>d.id===processing.document!.id) && <option value={processing.document.id}>{processing.document.filename}</option>}</select></label><p>Open patient documents to select a source. A linked document remains unverified and access is checked separately.</p><label className="check"><input type="checkbox" name="link_processing" disabled={!selectedDocument || !processing?.job_id || processing.status !== 'completed'}/> Link the currently open completed processing run (select the same source document above).</label><label className="check"><input type="checkbox" name="link_extraction" disabled={!processing?.structured_versions?.length}/> Also link the displayed transcription version; requires the processing run above.</label>{processing?.structured_versions?.find(v=>v.version === (citedExtractionVersion ?? processing.structured_versions?.[0]?.version)) && <NoteSourceFields key={`${processing.job_id}:${citedExtractionVersion ?? processing.structured_versions?.[0]?.version}`} data={processing.structured_versions.find(v=>v.version === (citedExtractionVersion ?? processing.structured_versions?.[0]?.version))!.data}/>}<button>Create draft</button></form>}{notes.length ? notes.map(n=><article className="note" key={n.version_id}><strong>Version {n.version} · {n.status.replace('_',' ')}</strong><p className="note-content">{n.content}</p>{n.ai_assisted && <p>AI-assisted draft. A clinician must verify the content before approval.</p>}{n.assistant_referenced && (user.role === 'doctor' ? <NoteSource api={api} noteId={n.note_id} versionId={n.version_id} onError={setError}/> : <p>Assistant response referenced during drafting.</p>)}<p>Author: {n.author_id}{n.reviewer_id && <> · Reviewer: {n.reviewer_id}</>}{n.consultation_id && <> · Consultation: {n.consultation_id}</>}</p>{n.source_document_id && <p>Linked source document: {n.source_document_id} · <a href={`/api/documents/${n.source_document_id}/download`}>Download source document</a>{n.source_processing_job_id && <> · Processing run {n.source_processing_job_id}{n.source_extraction_version != null && ` · transcription version ${n.source_extraction_version}`}{n.source_fields?.length > 0 && ` · ${n.source_fields.map(f=>`${f.group === 'laboratory_tests' ? 'test' : 'medicine'} ${f.index+1} ${f.field.replace(/_/g,' ')}${f.note_quote ? ` → “${f.note_quote}”` : ''}`).join(', ')}`} · <button className="link" onClick={()=>void viewProcessing(n.source_document_id!, n.source_processing_job_id!, n.source_extraction_version)}>Open cited run</button></>}</p>}{user.role === 'doctor' && n.status === 'draft' && n.author_id === user.id && <form onSubmit={e=>reviseNote(e,n)}><label>Revise draft<textarea name="content" defaultValue={n.content} required maxLength={20000} rows={5}/></label>{n.source_document_id && <label className="check"><input type="checkbox" name="clear_document_source"/> Remove the document and transcription references from the new version</label>}<button className="secondary">Save revised draft</button></form>}{user.role === 'doctor' && n.status === 'draft' && n.author_id === user.id && <button onClick={()=>noteAction(n,'submit')}>Submit for review</button>}{user.role === 'doctor' && n.review_comment && <p>Rejection reason: {n.review_comment}</p>}{user.role === 'doctor' && <NoteComparison api={api} noteId={n.note_id} current={n} previous={notes.filter(v=>v.note_id===n.note_id && v.version<n.version)} onError={setError}/>} {user.role === 'doctor' && n.status === 'pending_review' && reviewPolicy !== null && (reviewPolicy !== 'independent' || n.author_id !== user.id) && <><button onClick={()=>noteAction(n,'approve')}>Approve</button><form onSubmit={e=>{e.preventDefault(); const reason=String(new FormData(e.currentTarget).get('reason') ?? ''); void noteAction(n,'reject',reason);}}><label>Reason for rejection<textarea name="reason" required minLength={3} maxLength={2000} rows={2}/></label><button className="secondary">Reject</button></form></>}{user.role === 'doctor' && ['approved','rejected'].includes(n.status) && notes.filter(x=>x.note_id===n.note_id).every(x=>x.version<=n.version) && <form onSubmit={e=>amendNote(e,n)}><label>New version<textarea name="content" required maxLength={20000} rows={4}/></label>{n.source_document_id && <label className="check"><input type="checkbox" name="clear_document_source"/> Remove the document and transcription references from the new version</label>}<button className="secondary">Create amendment</button></form>}</article>) : <p>No notes to display.</p>}</section>}{user.role !== 'admin' && <><Assistant key={user.role === 'patient' ? user.id : patientId} api={api} patientId={user.role === 'patient' ? user.id : patientId} canDraft={user.role === 'doctor'} onError={setError}/><Integrations api={api} patientId={user.role === 'patient' ? user.id : patientId} onError={setError}/></>}{user.role === 'doctor' && <KnowledgeReviewDoctor api={api} onError={setError}/>}{user.role === 'patient' && <PatientAccessHistory api={api} onError={setError}/>}<Knowledge admin={user.role === 'admin'} api={api} onError={setError}/><section className="card"><h3>Deactivate account</h3><p>This ends your access and revokes your sessions and integration credentials. Your records are retained. Contact an administrator for help with account access.</p><form onSubmit={deactivate}><label>Confirm your password<input name="password" type="password" autoComplete="current-password" required maxLength={256}/></label><button className="secondary">Deactivate account</button></form></section></>}
    <footer>Clinical decisions remain with qualified clinicians. This build is for synthetic data only.</footer></main>;
}
