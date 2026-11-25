'use client';

import {FormEvent, useCallback, useEffect, useRef, useState} from 'react';
import AssistantPreferences from './assistant-preferences';

type Api = (path:string, init?:RequestInit)=>Promise<Response>;
type Conversation = {id:string; patient_id:string; created_at:number};
type Citation = {id:string; kind:string; title:string; consultation_id?:string; truncated?:boolean; page?:number; text_segment?:number; provenance?:string; version?:string; layer?:string};
type Message = {id:string; role:'user'|'assistant'; content:string; citations:Citation[]; review_status:string|null};
type Event = {type:string; message?:string; text?:string; citations?:Citation[]};
const json = (data:object):RequestInit => ({method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
function citationKind(kind:string):string {
  if(kind==='approved_reference') return 'reference approved when this response was generated; verify current status';
  if(kind==='doctor_approved_note') return 'note approved when this response was generated; verify current status';
  return kind.replace(/_/g,' ');
}

export default function Assistant({api, patientId, canDraft, onError}:{api:Api; patientId:string; canDraft:boolean; onError:(message:string)=>void}) {
  const [conversations,setConversations] = useState<Conversation[]>([]);
  const [conversationCursor,setConversationCursor] = useState<string|null>(null);
  const [listBusy,setListBusy] = useState(false);
  const [selected,setSelected] = useState('');
  const [messages,setMessages] = useState<Message[]>([]);
  const [historyCursor,setHistoryCursor] = useState<string|null>(null);
  const [historyLoading,setHistoryLoading] = useState(false);
  const [ready,setReady] = useState(false);
  const [busy,setBusy] = useState(false);
  const [status,setStatus] = useState('');
  const [partial,setPartial] = useState('');
  const [draftMessage,setDraftMessage] = useState('');
  const [draftStatus,setDraftStatus] = useState('');
  const [generating,setGenerating] = useState(false);
  const socket = useRef<WebSocket|null>(null);
  const mounted = useRef(false);
  const selection = useRef(0);
  const historyRequest = useRef(0);
  const listRequest = useRef(0);
  const loadList = useCallback(async(cursor?:string)=>{
    const sequence=++listRequest.current;
    if(!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(patientId)) {setConversations([]); setConversationCursor(null); setListBusy(false); return;}
    setListBusy(true);
    const query=new URLSearchParams({patient_id:patientId}); if(cursor) query.set('cursor',cursor);
    try {const response=await api(`conversations?${query}`); const data:Conversation[]=await response.json(); if(mounted.current && sequence===listRequest.current) {setConversations(previous=>cursor ? [...previous,...data.filter(item=>!previous.some(existing=>existing.id===item.id))] : data);setConversationCursor(response.headers.get('x-next-cursor'));}}
    catch(e) {if(mounted.current && sequence===listRequest.current) {setConversations([]);setConversationCursor(null);onError((e as Error).message);}}
    finally {if(mounted.current && sequence===listRequest.current) setListBusy(false);}
  },[api,onError,patientId]);
  const loadMessages = useCallback(async(id:string, request:number, cursor?:string)=>{
    const sequence=++historyRequest.current; setHistoryLoading(true);
    try {const response=await api(`conversations/${encodeURIComponent(id)}/messages${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`); const data=await response.json(); if (mounted.current && selection.current===request && historyRequest.current===sequence) {setMessages(data);setHistoryCursor(response.headers.get('x-next-cursor'));}}
    catch(e) {if (mounted.current && selection.current===request && historyRequest.current===sequence) {setMessages([]);setHistoryCursor(null);onError((e as Error).message);}}
    finally {if (mounted.current && selection.current===request && historyRequest.current===sequence) setHistoryLoading(false);}
  },[api,onError]);
  useEffect(()=>{mounted.current=true; void loadList(); return ()=>{mounted.current=false; listRequest.current++; selection.current++; socket.current?.close(); socket.current=null;};},[loadList]);

  async function open(id:string) {
    const request=++selection.current;
    socket.current?.close(); socket.current=null; setSelected(id); setMessages([]); setHistoryCursor(null); setPartial(''); setDraftMessage(''); setDraftStatus(''); setReady(false); setBusy(false); setStatus('Connecting…');
    try {
      await loadMessages(id,request);
      if (!mounted.current || selection.current !== request) return;
      const ticket = (await (await api(`conversations/${id}/ticket`,{method:'POST'})).json()).ticket;
      if (!mounted.current || selection.current !== request) return;
      const configured = process.env.NEXT_PUBLIC_ASSISTANT_WS_URL;
      const base = configured || `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.hostname}:8100`;
      const connection = new WebSocket(`${base}/ws/conversations/${id}`); socket.current=connection;
      connection.onopen=()=>connection.send(JSON.stringify({ticket}));
      connection.onmessage=event=>{
        if(socket.current!==connection) return;
        const data:Event=JSON.parse(event.data);
        if(data.type==='ready') {setReady(true); setStatus('Connected');}
        else if(data.type==='status') setStatus(data.message ?? 'Working');
        else if(data.type==='chunk') setPartial(value=>value+(data.text ?? ''));
        else if(data.type==='complete') {setBusy(false); setPartial(''); setStatus('Draft complete'); void loadMessages(id,request);}
        else if(data.type==='cancelled') {setBusy(false); setPartial(''); setStatus('Turn cancelled'); void loadMessages(id,request);}
        else if(data.type==='error') {setBusy(false);setPartial('');setStatus(data.message ?? 'Turn failed'); void loadMessages(id,request);}
      };
      connection.onclose=()=>{if(socket.current===connection){setReady(false);setBusy(false);setStatus('Disconnected. Reconnect to continue.');}};
      connection.onerror=()=>{if(socket.current===connection) setStatus('Connection failed.');};
    } catch(e) {if (mounted.current && selection.current===request) {setStatus('Could not connect');onError((e as Error).message);}}
  }
  async function create() {
    if(!patientId) {onError('Enter a patient ID before creating a conversation.');return;}
    try {const response=await api('conversations',json({patient_id:patientId})); const result:Conversation=await response.json(); await loadList(); await open(result.id);} catch(e) {onError((e as Error).message);}
  }
  function send(event:FormEvent<HTMLFormElement>) {
    event.preventDefault(); if(!socket.current || !ready || busy) return;
    const form=event.currentTarget; const data=new FormData(form);
    const consultationId=String(data.get('consultation_id') ?? '').trim();
    if(consultationId && (!data.has('notes') || !data.has('consultations'))) {onError('Select consultation history and approved notes when choosing a consultation.'); return;}
    socket.current.send(JSON.stringify({type:'turn',question:data.get('question'),include_documents:data.has('documents'),include_notes:data.has('notes'),include_profile:data.has('profile'),include_consultations:data.has('consultations'),include_knowledge:data.has('knowledge'),consultation_id:consultationId || null}));
    setBusy(true); setPartial(''); setStatus('Starting turn'); form.reset();
  }
  async function generateNote(messageId:string) {
    if (generating) return;
    setGenerating(true); setDraftStatus('Generating an unreviewed note draft…');
    const storageKey=`mediagent-note-generation:${selected}:${messageId}`;
    let requestId:string;
    try {requestId=sessionStorage.getItem(storageKey) ?? crypto.randomUUID(); sessionStorage.setItem(storageKey,requestId);}
    catch {setGenerating(false); setDraftStatus('Draft generation requires browser session storage.'); return;}
    try {const response=await api(`conversations/${selected}/messages/${messageId}/generate-note-draft`,json({request_id:requestId})); const note=await response.json(); sessionStorage.removeItem(storageKey); setDraftStatus(`AI-assisted draft created for patient ${note.patient_id}. Open clinical notes to revise it and submit it for review.`);}
    catch(e) {setDraftStatus('Draft generation failed.'); onError((e as Error).message);}
    finally {setGenerating(false);}
  }
  async function createNote(event:FormEvent<HTMLFormElement>, messageId:string) {
    event.preventDefault(); const form=event.currentTarget; const values=new FormData(form);
    try {const result=await api(`conversations/${selected}/messages/${messageId}/note-draft`,json({content:values.get('content'),consultation_id:String(values.get('consultation_id') ?? '').trim() || null})); const note=await result.json(); setDraftMessage(''); setDraftStatus(`Draft created for patient ${note.patient_id}. Open clinical notes to submit it for review.`);}
    catch(e) {onError((e as Error).message);}
  }
  return <section className="card document-list"><h3>Medical assistant</h3><p>Educational support only. The assistant cannot diagnose, prescribe, or make treatment decisions. Generated answers are unreviewed; discuss them with a clinician. Conversations are private to their creator.</p><p>Questions and selected information are sent to the configured model service. Generating a note sends the unreviewed response to that service again. Only use this development build with synthetic data.</p>
    <AssistantPreferences api={api} onError={onError}/>
    <button disabled={!patientId} onClick={create}>New conversation</button>{!patientId && <p>Enter a patient ID in the workspace to start.</p>}
    {conversations.some(c=>c.patient_id===patientId) && <label>Conversation<select value={selected} onChange={e=>{if(e.target.value) void open(e.target.value);}}><option value="">Choose a conversation</option>{conversations.filter(c=>c.patient_id===patientId).map(c=><option key={c.id} value={c.id}>{new Date(c.created_at*1000).toLocaleString()} · patient {c.patient_id}</option>)}</select></label>}{conversationCursor && <button className="secondary" disabled={listBusy} onClick={()=>void loadList(conversationCursor)}>Load older conversations</button>}
    {selected && <><p role="status">{status}</p>{draftStatus && <p role="status">{draftStatus}</p>}{!ready && <button className="secondary" onClick={()=>open(selected)}>Reconnect</button>}<p>History shows up to 100 messages per page. Latest messages are shown after a turn or reconnect.</p><button className="secondary" disabled={historyLoading || busy} onClick={()=>void loadMessages(selected,selection.current)}>Latest messages</button>{historyCursor && <button className="secondary" disabled={historyLoading || busy} onClick={()=>void loadMessages(selected,selection.current,historyCursor)}>Older messages</button>}<div className="conversation">{messages.map(message=><article className="note" key={message.id}><strong>{message.role === 'user' ? 'You' : 'Assistant · unreviewed'}</strong><p className="note-content">{message.content}</p>{message.citations.map(c=><p key={c.id}>Source: {c.title}{c.page ? `, page ${c.page}` : ''}{c.text_segment ? `, text segment ${c.text_segment}` : ''} · {citationKind(c.kind)}{c.version ? ` · version ${c.version}` : ''}{c.layer==='supplemental_ocr' ? ' · supplemental OCR' : ''}{c.provenance ? ` · ${c.provenance}` : ''}{c.consultation_id ? ` · consultation ${c.consultation_id}` : ''}{c.truncated ? ' · source truncated' : ''}</p>)}{canDraft && message.role==='assistant' && message.citations.length>0 && <>{message.citations.some(c=>c.kind==='unverified_patient_document' || c.kind==='doctor_approved_note' || c.kind==='self_reported_patient_profile') && <button className="secondary" disabled={generating} onClick={()=>generateNote(message.id)}>Generate AI-assisted note draft</button>}<button className="secondary" onClick={()=>{setDraftMessage(message.id); setDraftStatus('');}}>Write a note using this response as reference</button>{draftMessage===message.id && <form onSubmit={e=>createNote(e,message.id)}><p>Patient {conversations.find(c=>c.id===selected)?.patient_id}. Verify every clinical statement and write the draft yourself. The assistant response is unreviewed and will be linked as a reference.</p><label>Clinician-written draft<textarea name="content" required maxLength={20000} rows={5}/></label><label>Consultation ID (optional)<input name="consultation_id" maxLength={36}/></label><button>Create unapproved draft</button></form>}</>}</article>)}{partial && <article className="note"><strong>Assistant · unreviewed</strong><p className="note-content">{partial}</p></article>}</div>
      <form onSubmit={send}><label>Question<textarea name="question" rows={3} maxLength={4000} required/></label><fieldset><legend>Include authorized context</legend><label className="check"><input type="checkbox" name="documents"/> Extracted document text</label><label className="check"><input type="checkbox" name="notes"/> Doctor-approved notes</label><label className="check"><input type="checkbox" name="profile"/> Patient-reported allergies, medications and medical history</label><label className="check"><input type="checkbox" name="consultations"/> Recent consultation titles and times (not clinical findings)</label><label className="check"><input type="checkbox" name="knowledge" defaultChecked/> Approved medical references</label><label>Specific consultation ID (optional)<input name="consultation_id" maxLength={36}/></label><p>Select both consultation history and approved notes to use only that consultation and its linked approved notes. IDs are listed in consultation history.</p></fieldset><button disabled={!ready || busy}>Send</button></form>{busy && <button className="secondary" onClick={()=>socket.current?.send(JSON.stringify({type:'cancel'}))}>Cancel turn</button>}</>}
  </section>;
}
