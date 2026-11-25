'use client';

import {useState} from 'react';

type API = (path:string, init?:RequestInit)=>Promise<Response>;
type Citation = {id:string; title:string; page?:number; text_segment?:number; method:string; layer?:string};
type Summary = {id:string; content:string; citations:Citation[]; truncated:boolean; model_name:string; created_at:number; review_status:'unverified'};

export default function DocumentSummaries({api, documentId, jobId, onError, onPageSelect, readOnly=false}:{api:API; documentId:string; jobId:string; onError:(message:string)=>void; onPageSelect:(page:number)=>void; readOnly?:boolean}) {
  const [summaries,setSummaries] = useState<Summary[]|null>(null);
  const [busy,setBusy] = useState(false);
  const path = `documents/${documentId}/processing/${jobId}/summaries`;
  async function load() {
    try {setSummaries(await (await api(path)).json());} catch(error) {setSummaries(null); onError((error as Error).message);}
  }
  async function generate() {
    const storageKey=`mediagent-document-summary:${documentId}:${jobId}`;
    let requestId:string;
    try {requestId=sessionStorage.getItem(storageKey) ?? crypto.randomUUID(); sessionStorage.setItem(storageKey,requestId);}
    catch {onError('Summary generation requires browser session storage.'); return;}
    setBusy(true);
    try {
      await api(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:requestId})});
      sessionStorage.removeItem(storageKey);
      await load();
    } catch(error) {onError((error as Error).message);}
    finally {setBusy(false);}
  }
  return <section className="note"><h4>AI document summaries</h4><p>Unverified summaries can omit or invent details and are not clinical records. {!readOnly && <>Generating one sends up to five extracted text sections to the configured model; OCR can be wrong. </>} Check the original with a clinician.</p>
    <button className="secondary" onClick={load} disabled={busy}>View summaries</button>{!readOnly && <>{' '}<button onClick={generate} disabled={busy}>{busy ? 'Generating…' : 'Generate unverified summary'}</button></>}
    {summaries && (summaries.length ? summaries.map(summary=><article className="note" key={summary.id}><strong>AI summary · unverified</strong><p className="note-content">{summary.content}</p><p>Model: {summary.model_name} · {new Date(summary.created_at*1000).toLocaleString()}</p>{summary.truncated && <p>Only part of the extracted text was sent to the model.</p>}{summary.citations.map(citation=><p key={citation.id}>Source: {citation.text_segment ? `text segment ${citation.text_segment}` : <button className="link" onClick={()=>onPageSelect(citation.page!)}>page {citation.page}</button>} · {citation.layer === 'supplemental_ocr' ? 'supplemental OCR' : citation.method === 'ocr' ? 'OCR' : citation.method === 'docx' ? 'DOCX text' : 'native text'}</p>)}</article>) : <p>No summaries for this processing job.</p>)}
  </section>;
}
