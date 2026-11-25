'use client';
import {FormEvent, useState} from 'react';

type API=(path:string,init?:RequestInit)=>Promise<Response>;
type Account={id:string; email:string; full_name:string; role:'patient'|'doctor'|'admin'; active:boolean; verification_status:string};
type Filters={query:string;role:string|null;active:boolean|null};
type Page={users:Account[];next_cursor:string|null};

export default function AdminUsers({api,onError}:{api:API;onError:(message:string)=>void}) {
  const [page,setPage]=useState<Page|null>(null);
  const [filters,setFilters]=useState<Filters|null>(null);
  const [selected,setSelected]=useState<Account|null>(null);
  const [status,setStatus]=useState('');
  async function fetchPage(input:Filters,after:string|null=null) {
    try {const result=await api('admin/users/search',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...input,after})}); setPage(await result.json()); setSelected(null);}
    catch(e) {setPage(null); setSelected(null); onError((e as Error).message);}
  }
  async function search(event:FormEvent<HTMLFormElement>) {
    event.preventDefault(); const form=new FormData(event.currentTarget); const active=String(form.get('active') ?? ''); const role=String(form.get('role') ?? '');
    const input:Filters={query:String(form.get('query') ?? '').trim(),role:role || null,active:active ? active === 'active' : null}; setFilters(input); setStatus(''); await fetchPage(input);
  }
  async function deactivate(event:FormEvent<HTMLFormElement>) {
    event.preventDefault(); if(!selected)return; const form=new FormData(event.currentTarget);
    try {await api(`admin/users/${encodeURIComponent(selected.id)}/deactivate`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reason:form.get('reason')})}); setPage(current=>current && {...current,users:current.users.map(item=>item.id===selected.id ? {...item,active:false}:item)}); setStatus('Account deactivated.'); setSelected(null);}
    catch(e) {onError((e as Error).message);}
  }
  return <section className="card document-list"><h3>User management</h3><p>Search account identity and status. Medical records are not included.</p><form onSubmit={search}><label>Name, email or exact ID<input name="query" maxLength={100}/></label><label>Role<select name="role"><option value="">All</option><option value="patient">Patient</option><option value="doctor">Doctor</option><option value="admin">Administrator</option></select></label><label>Status<select name="active"><option value="">All</option><option value="active">Active</option><option value="inactive">Inactive</option></select></label><button>Search accounts</button></form>{status && <p role="status">{status}</p>}{page && <>{page.users.length ? page.users.map(item=><div className="row" key={item.id}><div><strong>{item.full_name}</strong><p>{item.email} · {item.role} · {item.active ? 'active' : 'inactive'}{item.role==='doctor' && ` · ${item.verification_status}`}</p><p>ID: {item.id}</p></div>{item.active && item.role !== 'admin' && <button className="secondary" onClick={()=>{setSelected(item); setStatus('');}}>Deactivate</button>}</div>) : <p>No accounts found.</p>}{page.next_cursor && filters && <button className="secondary" onClick={()=>fetchPage(filters,page.next_cursor)}>Next page</button>}</>}{selected && <form onSubmit={deactivate}><h4>Deactivate {selected.full_name}</h4><p>{selected.email} · {selected.id}</p><p>This ends account access and revokes sessions and integration credentials. Records are retained; reactivation is not available in this build.</p><label>Reason<select name="reason" required defaultValue=""><option value="" disabled>Select a reason</option><option value="security">Security</option><option value="user_request">User request</option><option value="administrative">Administrative</option></select></label><button>Deactivate account</button>{' '}<button type="button" className="secondary" onClick={()=>setSelected(null)}>Cancel</button></form>}</section>;
}
