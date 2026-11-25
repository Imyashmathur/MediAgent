'use client';
import {useEffect, useState} from 'react';

type API = (path:string, init?:RequestInit) => Promise<Response>;

export default function DocumentPreview({api, documentId, page, setPage}: {api:API; documentId:string; page:number; setPage:(page:number)=>void}) {
  const [url,setUrl]=useState<string|null>(null);
  const [error,setError]=useState('');
  useEffect(()=>{
    let current=true; let created:string|null=null; const controller=new AbortController();
    setUrl(null); setError('');
    api(`documents/${encodeURIComponent(documentId)}/preview/${page}`, {signal:controller.signal}).then(r=>r.blob()).then(blob=>{
      if (!current) return;
      created=URL.createObjectURL(blob); setUrl(created);
    }).catch(e=>{if(current) setError((e as Error).message);});
    return ()=>{current=false; controller.abort(); if(created) URL.revokeObjectURL(created);};
  },[api,documentId,page]);
  return <section className="preview-panel"><h4>Original document · page {page}</h4><p>Rendered preview. Compare carefully with the original download; some interactive content may not appear.</p><div><button className="secondary" disabled={page <= 1} onClick={()=>setPage(page-1)}>Previous page</button>{' '}<button className="secondary" disabled={page >= 30} onClick={()=>setPage(page+1)}>Next page</button></div>{error && <p role="alert">{error}</p>}{url && <img className="preview-image" src={url} alt={`Rendered original document, page ${page}`}/>}</section>;
}
