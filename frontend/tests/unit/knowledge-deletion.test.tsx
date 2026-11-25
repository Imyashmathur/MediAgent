import React from 'react';
import {afterEach, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import Knowledge from '../../app/knowledge';

afterEach(cleanup);

it('requests deletion and displays the staged-file purge status', async()=>{
  let deleted=false;
  const api=vi.fn(async(path:string, init?:RequestInit)=>{
    if(path==='admin/knowledge/review-policy') return {json:async()=>({knowledge_review_policy:'self_or_independent',administrator_id:'admin'})} as Response;
    if(path==='admin/knowledge') return {json:async()=>[]} as Response;
    if(path==='admin/knowledge/imports') return {json:async()=>[{id:'import-id', filename:'synthetic.pdf', sha256:'synthetic', media_type:'application/pdf', scan_status:'not_scanned', status:'completed', error_code:null, deleted_at:deleted?1:null, purge_status:deleted?'pending':'not_requested'}]} as Response;
    if(path==='admin/knowledge/imports/import-id/delete') {expect(JSON.parse(String(init?.body))).toEqual({confirmation:'DELETE'}); deleted=true; return {} as Response;}
    throw new Error('Unexpected request');
  });
  render(<Knowledge admin api={api} onError={vi.fn()}/>);
  const input=await screen.findByLabelText('Type DELETE to remove staged file and extracted text');
  fireEvent.change(input,{target:{value:'DELETE'}});
  fireEvent.click(screen.getByRole('button',{name:'Delete staged import'}));
  await screen.findByText('Deletion requested · file purge: pending');
  await waitFor(()=>expect(screen.queryByText('Download original')).toBeNull());
});

it('retries a failed staged import and refreshes its status', async()=>{
  let status='failed';
  const api=vi.fn(async(path:string, init?:RequestInit)=>{
    if(path==='admin/knowledge/review-policy') return {json:async()=>({knowledge_review_policy:'self_or_independent',administrator_id:'admin'})} as Response;
    if(path==='admin/knowledge') return {json:async()=>[]} as Response;
    if(path==='admin/knowledge/imports') return {json:async()=>[{id:'failed-id', filename:'synthetic.pdf', sha256:'synthetic', media_type:'application/pdf', scan_status:'not_scanned', status, error_code:status==='failed'?'import_failed':null, deleted_at:null, purge_status:'not_requested'}]} as Response;
    if(path==='admin/knowledge/imports/failed-id/retry') {expect(init?.method).toBe('POST'); status='queued'; return {} as Response;}
    throw new Error('Unexpected request');
  });
  render(<Knowledge admin api={api} onError={vi.fn()}/>);
  fireEvent.click(await screen.findByRole('button',{name:'Retry parsing'}));
  await screen.findByText('Reference parsing queued again. Refresh imports to check status.');
  expect(screen.queryByRole('button',{name:'Retry parsing'})).toBeNull();
});

it('disables independent approval for the creator', async()=>{
  const api=vi.fn(async(path:string)=>{
    if(path==='admin/knowledge/review-policy') return {json:async()=>({knowledge_review_policy:'independent',administrator_id:'creator'})} as Response;
    if(path==='admin/knowledge/imports') return {json:async()=>[]} as Response;
    if(path==='admin/knowledge') return {json:async()=>[{id:'source',title:'Synthetic reference',publisher:'Synthetic',provenance:'synthetic',publication_date:'2025-01-01',specialty:'general',version:'1',status:'draft',index_status:'ready',embedding_digest:'a',created_by:'creator',content:'Synthetic text'}]} as Response;
    throw new Error('Unexpected request');
  });
  render(<Knowledge admin api={api} onError={vi.fn()}/>);
  await screen.findByText(/A different administrator must approve each source/);
  expect((await screen.findByRole('button',{name:'Approve source'}) as HTMLButtonElement).disabled).toBe(true);
});

it('queues indexing and displays the refreshed job state', async()=>{
  let indexed=false;
  const api=vi.fn(async(path:string, init?:RequestInit)=>{
    if(path==='admin/knowledge/review-policy') return {json:async()=>({knowledge_review_policy:'self_or_independent',administrator_id:'admin'})} as Response;
    if(path==='admin/knowledge/imports') return {json:async()=>[]} as Response;
    if(path==='admin/knowledge') return {json:async()=>[{id:'source',title:'Synthetic reference',publisher:'Synthetic',provenance:'synthetic',publication_date:'2025-01-01',specialty:'general',version:'1',status:'draft',index_status:indexed?'ready':'not_indexed',embedding_digest:indexed?'a':null,created_by:'admin',content:'Synthetic text'}]} as Response;
    if(path==='admin/knowledge/source/index-jobs' && init?.method==='POST') return {json:async()=>({status:'queued'})} as Response;
    if(path==='admin/knowledge/source/index-jobs') {indexed=true; return {json:async()=>[{status:'completed',error_code:null}]} as Response;}
    throw new Error(`Unexpected ${path}`);
  });
  render(<Knowledge admin api={api} onError={()=>{}}/>);
  fireEvent.click(await screen.findByRole('button',{name:'Index'}));
  expect(await screen.findByText('Latest indexing job: completed')).toBeTruthy();
  expect(await screen.findByRole('button',{name:'Reindex'})).toBeTruthy();
  expect(api).toHaveBeenCalledWith('admin/knowledge/source/index-jobs',{method:'POST'});
});
