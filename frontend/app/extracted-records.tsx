'use client';

import {useEffect, useRef, useState} from 'react';
import {ExtractionData, FieldValue} from './extraction-review';

type RecordDocument = {document_id:string; filename:string; version:number; review_status:'unverified'|'reviewed'; reviewer_id:string|null; data:ExtractionData};
type Records = {documents:RecordDocument[]; truncated:boolean; next_cursor:string|null};
type API = (path:string, init?:RequestInit)=>Promise<Response>;

const groups = [
  {key:'laboratory_tests', title:'Laboratory tests', labels:{test_name:'Test', observed_value:'Observed value', unit:'Unit', reference_range:'Reported reference range', abnormality_flag:'Reported flag', report_date:'Report date', laboratory_name:'Laboratory', laboratory_details:'Laboratory details'}},
  {key:'prescription_items', title:'Prescription items', labels:{medicine_name:'Medicine', dosage:'Dosage', frequency:'Frequency', duration:'Duration', instructions:'Instructions', prescribing_doctor:'Prescribing doctor'}},
] as const;

export default function ExtractedRecords({api, patientId, onOpenDocument, onError}:{api:API; patientId:string; onOpenDocument:(id:string)=>void; onError:(message:string)=>void}) {
  const [records, setRecords] = useState<Records|null>(null);
  const [loading, setLoading] = useState(false);
  const revision = useRef(0);
  useEffect(()=>()=>{revision.current++;},[patientId]);
  async function load(cursor?:string) {
    const request=++revision.current; setLoading(true);
    try {
      const page:Records = await (await api(`patients/${encodeURIComponent(patientId)}/extracted-records${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`)).json();
      if(request !== revision.current) return;
      setRecords(previous=>cursor ? {...page, documents:[...(previous?.documents ?? []),...page.documents]} : page);
    } catch (error) {if(request === revision.current) {setRecords(null); onError((error as Error).message);}}
    finally {if(request === revision.current) setLoading(false);}
  }
  return <section className="card document-list"><h3>Extracted tests and prescriptions</h3>
    <p>These are transcriptions from uploaded documents, not a confirmed medication list or clinical interpretation. Source text can be incorrect; compare each item with the original.</p>
    <button onClick={()=>void load()} disabled={loading || !patientId}>{loading ? 'Loading…' : records ? 'Refresh extracted records' : 'View extracted records'}</button>
    {records && <>{records.truncated && <p>More documents with completed current processing are available.</p>}
      {groups.map(group=><section key={group.key}><h4>{group.title}</h4>
        {!records.documents.some(document=>document.data[group.key]?.length) && <p>No transcribed items found in the displayed documents.</p>}
        {records.documents.flatMap(document=>(document.data[group.key] ?? []).map((row,index)=><article className="note" key={`${document.document_id}-${group.key}-${index}`}>
          <strong>{document.filename} · {document.review_status === 'reviewed' ? 'Clinician-reviewed transcription' : 'Unverified transcription'}</strong>
          <p>Version {document.version}{document.reviewer_id && <> · Reviewer {document.reviewer_id}</>}</p>
          <dl>{Object.entries(group.labels).map(([key,label])=>{const field:FieldValue|null|undefined = row[key]; return field ? <div key={key}><dt>{label}</dt><dd>{field.value} <small>({field.origin === 'clinician' ? 'clinician-entered' : `source ${field.evidence?.location_type === 'text_segment' ? 'text segment' : 'page'} ${field.evidence?.page}${field.evidence?.layer === 'supplemental_ocr' ? ' · supplemental OCR' : ''}`})</small></dd></div> : null;})}</dl>
          <button type="button" className="secondary" onClick={()=>onOpenDocument(document.document_id)}>Open source document</button>
        </article>))}
      </section>)}
      {records.next_cursor && <button type="button" className="secondary" disabled={loading} onClick={()=>void load(records.next_cursor!)}>Load older extracted records</button>}
    </>}
  </section>;
}
