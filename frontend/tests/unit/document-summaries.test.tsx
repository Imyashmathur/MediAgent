import React from 'react';
import {afterEach, describe, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import DocumentSummaries from '../../app/document-summaries';

afterEach(()=>{cleanup(); sessionStorage.clear();});

describe('document summary retry', ()=>{
  it('reuses the request ID after a failed attempt and labels the returned source', async()=>{
    const requestIds:string[]=[];
    const api=vi.fn(async (_path:string, init?:RequestInit)=>{
      if (init?.method === 'POST') {
        requestIds.push(JSON.parse(String(init.body)).request_id);
        if(requestIds.length===1) throw new Error('Synthetic network failure');
        return {} as Response;
      }
      return {json:async()=>[{id:'synthetic', content:'Synthetic summary', citations:[{id:'source',page:1,method:'ocr',layer:'supplemental_ocr'}], truncated:true, model_name:'synthetic-model', created_at:0, review_status:'unverified'}]} as Response;
    });
    const onError=vi.fn();
    const onPageSelect=vi.fn();
    render(<DocumentSummaries api={api} documentId="document" jobId="job" onError={onError} onPageSelect={onPageSelect}/>);
    fireEvent.click(screen.getByRole('button',{name:'Generate unverified summary'}));
    await waitFor(()=>expect(onError).toHaveBeenCalledWith('Synthetic network failure'));
    expect(sessionStorage.getItem('mediagent-document-summary:document:job')).toBe(requestIds[0]);
    fireEvent.click(screen.getByRole('button',{name:'Generate unverified summary'}));
    await screen.findByText('Synthetic summary');
    expect(requestIds).toHaveLength(2);
    expect(requestIds[1]).toBe(requestIds[0]);
    expect(sessionStorage.getItem('mediagent-document-summary:document:job')).toBeNull();
    expect(screen.getByText('AI summary · unverified')).toBeTruthy();
    expect(screen.getByText('Only part of the extracted text was sent to the model.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button',{name:'page 1'}));
    expect(onPageSelect).toHaveBeenCalledWith(1);
  });
  it('shows historical summaries without a generation control', async()=>{
    const api=vi.fn(async()=>({json:async()=>[{id:'old',content:'Historical synthetic summary',citations:[],truncated:false,model_name:'synthetic',created_at:0,review_status:'unverified'}]} as Response));
    render(<DocumentSummaries api={api} documentId="document" jobId="old" readOnly onError={vi.fn()} onPageSelect={vi.fn()}/>);
    expect(screen.queryByRole('button',{name:'Generate unverified summary'})).toBeNull();
    fireEvent.click(screen.getByRole('button',{name:'View summaries'}));
    await screen.findByText('Historical synthetic summary');
    expect(api).toHaveBeenCalledWith('documents/document/processing/old/summaries');
  });
});
