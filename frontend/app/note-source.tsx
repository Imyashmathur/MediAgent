'use client';
import {useState} from 'react';
type API=(path:string,init?:RequestInit)=>Promise<Response>;
type Source={content:string; citations:{id:string; title:string; kind:string; page?:number; text_segment?:number}[]};

export default function NoteSource({api,noteId,versionId,onError}:{api:API; noteId:string; versionId:string; onError:(message:string)=>void}) {
  const [source,setSource]=useState<Source|null>(null);
  async function open() {try {const response=await api(`notes/${encodeURIComponent(noteId)}/versions/${encodeURIComponent(versionId)}/assistant-source`); setSource(await response.json());} catch(e) {setSource(null); onError((e as Error).message);}}
  return <div><p>Assistant response used as a reference; the response remains unreviewed.</p><button className="secondary" onClick={open}>Open assistant reference</button>{source && <div className="note"><p className="note-content">{source.content}</p>{source.citations.map(item=><p key={item.id}>Source: {item.title}{item.page ? `, page ${item.page}` : ''}{item.text_segment ? `, text segment ${item.text_segment}` : ''} · {item.kind.replace(/_/g,' ')}</p>)}</div>}</div>;
}
