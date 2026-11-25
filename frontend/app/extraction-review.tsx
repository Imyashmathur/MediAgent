'use client';

import {useEffect, useState} from 'react';

export type FieldValue = {value:string; origin:'source'|'clinician'; evidence:{page:number; quote:string; location_type?:'page'|'text_segment'; layer?:'primary'|'supplemental_ocr'} | null};
type Row = Record<string, FieldValue | null>;
export type ExtractionData = {schema_version:'1.0'|'1.1'|'1.2'; laboratory_tests:Row[]; prescription_items:Row[]; warnings:string[]};
export type ExtractionVersion = {id:string; version:number; review_status:'unverified'|'reviewed'; reviewer_id:string|null; reviewed_at:number|null; data:ExtractionData};

const labFields = [['test_name','Test name'], ['observed_value','Observed value'], ['unit','Unit'], ['reference_range','Reference range'], ['abnormality_flag','Reported abnormality flag'], ['report_date','Report date'], ['laboratory_name','Laboratory name'], ['laboratory_details','Laboratory details']];
const medicineFields = [['medicine_name','Medicine name'], ['dosage','Dosage'], ['frequency','Frequency'], ['duration','Duration'], ['instructions','Instructions'], ['prescribing_doctor','Prescribing doctor']];

export default function ExtractionReview({versions, canReview, onReview, onPageSelect, selectedVersion}:{versions:ExtractionVersion[]; selectedVersion?:number|null; canReview:boolean; onReview:(version:number, data:ExtractionData)=>Promise<void>; onPageSelect:(page:number)=>void}) {
  const latest = selectedVersion == null ? versions[0] : versions.find(version=>version.version === selectedVersion);
  const [draft, setDraft] = useState<ExtractionData | null>(null);
  const [saving, setSaving] = useState(false);
  useEffect(()=>{setDraft(latest ? structuredClone(latest.data) : null);}, [latest]);
  if (!latest) return selectedVersion == null ? null : <p>Cited transcription version is unavailable.</p>;
  if (!draft) return null;
  const groups: {key:'laboratory_tests'|'prescription_items'; title:string; fields:string[][]}[] = [
    {key:'laboratory_tests', title:'Laboratory tests', fields:labFields},
    {key:'prescription_items', title:'Prescription items', fields:medicineFields},
  ];

  function update(key:'laboratory_tests'|'prescription_items', index:number, name:string, value:string) {
    setDraft(previous=>{
      if (!previous) return previous;
      const next = structuredClone(previous);
      const original = next[key][index][name];
      if (original?.value !== value) next[key][index][name] = value ? {value, origin:'clinician', evidence:null} : null;
      if (key === 'laboratory_tests' && name === 'laboratory_details' && value) next.schema_version = '1.2';
      return next;
    });
  }
  return <div className="note"><h4>Structured transcription · {latest.review_status}{selectedVersion != null && ` · cited version ${selectedVersion}`}</h4><p>Only explicit field labels and supported pipe-delimited laboratory and prescription tables are automatically transcribed. Check every field against the source; missing values are not inferred. A reviewed transcription is not an approved clinical note.</p>
    {latest.reviewer_id && <p>Reviewed by {latest.reviewer_id} · version {latest.version}</p>}
    {draft.warnings.map((warning,i)=><p key={i}>{warning}</p>)}
    {groups.map(group=><section key={group.key}><h5>{group.title}</h5>{draft[group.key].length === 0 && <p>No labeled items found.</p>}{draft[group.key].map((row,index)=><div className="extraction-item" key={index}><strong>Item {index+1}</strong>{group.fields.map(([name,label])=>{const field=row[name]; return <div key={name} className="extraction-field"><label>{label}{canReview ? <input value={field?.value ?? ''} maxLength={500} onChange={event=>update(group.key,index,name,event.target.value)}/> : <span>{field?.value ?? 'Not provided'}</span>}</label>{field?.evidence && <small>{field.evidence.location_type === 'text_segment' ? <>Text segment {field.evidence.page}</> : <button className="link" type="button" onClick={()=>onPageSelect(field.evidence!.page)}>Page {field.evidence.page}</button>}{field.evidence.layer === 'supplemental_ocr' && <> · supplemental OCR</>}: “{field.evidence.quote}”</small>}{field?.origin === 'clinician' && <small>Clinician-entered value</small>}</div>;})}{canReview && <button className="secondary" type="button" onClick={()=>setDraft(previous=>{if(!previous)return previous; const next=structuredClone(previous); next[group.key].splice(index,1); return next;})}>Remove item</button>}</div>)}{canReview && <button className="secondary" type="button" onClick={()=>setDraft(previous=>{if(!previous)return previous; const next=structuredClone(previous); next[group.key].push({}); return next;})}>Add {group.key === 'laboratory_tests' ? 'laboratory test' : 'prescription item'}</button>}</section>)}
    {canReview && <p><button disabled={saving} onClick={async()=>{setSaving(true);try {await onReview(latest.version,draft);} finally {setSaving(false);}}}>Save clinician review</button></p>}
    {versions.length > 1 && <details><summary>Version history ({versions.length})</summary>{versions.map(version=><div className="note" key={version.id}><strong>Version {version.version} · {version.review_status}</strong>{version.reviewer_id && <p>Reviewed by {version.reviewer_id}</p>}{groups.map(group=><div key={group.key}><p>{group.title}</p>{version.data[group.key].map((row,i)=><ul key={i}>{group.fields.map(([name,label])=>row[name] && <li key={name}>{label}: {row[name]?.value} {row[name]?.evidence && `(${row[name]?.evidence?.location_type === 'text_segment' ? 'text segment' : 'page'} ${row[name]?.evidence?.page}${row[name]?.evidence?.layer === 'supplemental_ocr' ? ' · supplemental OCR' : ''})`}</li>)}</ul>)}</div>)}</div>)}</details>}
  </div>;
}
