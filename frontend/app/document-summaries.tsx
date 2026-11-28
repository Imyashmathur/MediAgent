'use client';

import {useState} from 'react';
import type {ExtractionVersion} from './extraction-review';

type API = (path:string, init?:RequestInit)=>Promise<Response>;
type Citation = {id:string; title:string; page?:number; text_segment?:number; method:string; layer?:string};
type Summary = {id:string; content:string; citations:Citation[]; truncated:boolean; model_name:string; created_at:number; review_status:'unverified'};

export default function DocumentSummaries({api, documentId, jobId, onError, onPageSelect, readOnly=false, transcription}:{api:API; documentId:string; jobId:string; onError:(message:string)=>void; onPageSelect:(page:number)=>void; readOnly?:boolean; transcription?:ExtractionVersion}) {
  const [summaries,setSummaries] = useState<Summary[]|null>(null);
  const [busy,setBusy] = useState(false);
  const [openSummaryId,setOpenSummaryId] = useState<string|null>(null);
  const path = `documents/${documentId}/processing/${jobId}/summaries`;
  async function load(openNewest=false) {
    try {
      const items:Summary[] = await (await api(path)).json();
      setSummaries(items);
      setOpenSummaryId(current => openNewest || !current || !items.some(item => item.id === current) ? items[0]?.id ?? null : current);
    } catch(error) {setSummaries(null); onError((error as Error).message);}
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
      await load(true);
    } catch(error) {onError((error as Error).message);}
    finally {setBusy(false);}
  }
  return <section className="note"><h4>AI document summaries</h4><p>Unverified summaries can omit or invent details and are not clinical records. {!readOnly && <>Generation sends the full extracted text to the configured model in separate batches and may take several minutes. Summaries can miss or misstate results; extraction can be wrong. </>} Check the original with a clinician.</p>
    <section aria-label="Comparison with stated reference ranges"><h5>Comparison with stated reference ranges</h5>
      <p>Calculated from saved structured transcription, not the AI summary. Check each value, unit, range and source against the original with a clinician. This is not a diagnosis.</p>
      {transcription?.data.laboratory_tests.length ? <><p>Transcription version {transcription.version} · {transcription.review_status}</p><ul>{transcription.data.laboratory_tests.map((row, index)=>{
        const labels = {below:'Below stated range', within:'Within stated range', above:'Above stated range', unable_to_determine:'Unable to determine'};
        const status = transcription.range_comparisons?.[index] ?? 'unable_to_determine';
        return <li key={index}>{row.test_name?.value ?? `Test ${index+1}`} · result: {row.observed_value?.value ?? 'not provided'}{row.unit?.value ? ` ${row.unit.value}` : ''} · stated range: {row.reference_range?.value ?? 'not provided'}{row.abnormality_flag?.value ? ` · reported flag: ${row.abnormality_flag.value}` : ''} · <strong>{labels[status]}</strong></li>;
      })}</ul></> : <p>No structured laboratory items are available for comparison. A clinician can transcribe and review them against the original.</p>}
    </section>
    <button className="secondary" onClick={()=>void load()} disabled={busy}>View summaries</button>{!readOnly && <>{' '}<button onClick={generate} disabled={busy}>{busy ? 'Generating…' : 'Generate unverified summary'}</button></>}
    {summaries && (summaries.length ? summaries.map(summary=><article className="note" key={summary.id}><button type="button" className="secondary" aria-expanded={openSummaryId === summary.id} aria-controls={`summary-${summary.id}`} onClick={()=>setOpenSummaryId(current=>current === summary.id ? null : summary.id)}><span>AI summary · unverified</span> · {new Date(summary.created_at*1000).toLocaleString()}</button>{openSummaryId === summary.id && <div id={`summary-${summary.id}`}><p className="note-content">{summary.content}</p><p>Model: {summary.model_name} · {new Date(summary.created_at*1000).toLocaleString()}</p>{summary.truncated && <p>Only part of the extracted text was sent to the model. This older summary may omit important results and must not be treated as a complete report.</p>}{summary.citations.map(citation=><p key={citation.id}>Source: {citation.text_segment ? `text segment ${citation.text_segment}` : <button className="link" onClick={()=>onPageSelect(citation.page!)}>page {citation.page}</button>} · {citation.layer === 'supplemental_ocr' ? 'supplemental OCR' : citation.method === 'ocr' ? 'OCR' : citation.method === 'docx' ? 'DOCX text' : 'native text'}</p>)}</div>}</article>) : <p>No summaries for this processing job.</p>)}
  </section>;
}
