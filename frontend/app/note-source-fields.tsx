'use client';
import {ExtractionData} from './extraction-review';

export type SourceField={group:'laboratory_tests'|'prescription_items'; index:number; field:string; note_quote?:string};

export default function NoteSourceFields({data}:{data:ExtractionData}) {
  const groups = [{key:'laboratory_tests' as const,label:'Laboratory test'}, {key:'prescription_items' as const,label:'Prescription item'}];
  return <fieldset><legend>Optional fields from the saved transcription</legend><p>Select fields actually used. Optionally copy the exact note statement linked to each field; it must occur once in the note. These refer to the saved version, not unsaved edits. Linking fields does not verify them.</p>{groups.flatMap(group=>data[group.key].flatMap((row,index)=>Object.entries(row).filter(([,value])=>value).map(([field,value])=>{const key=JSON.stringify({group:group.key,index,field}); const title=`${group.label} ${index+1} · ${field.replace(/_/g,' ')}`; return <div key={key}><label className="check"><input type="checkbox" name="source_field" value={key}/>{title}: {value?.value}</label><label>Exact note statement for {title} (optional)<input name={`note_quote:${key}`} maxLength={500}/></label></div>;})))}</fieldset>;
}
