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
    expect(screen.getByText(/This older summary may omit important results and must not be treated as a complete report/)).toBeTruthy();
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

it('shows deterministic range comparisons separately from the AI summary', ()=>{
  const field=(value:string)=>({value,origin:'clinician' as const,evidence:null});
  const transcription={id:'v1',version:1,review_status:'reviewed' as const,reviewer_id:null,reviewed_at:null,
    data:{schema_version:'1.0' as const,laboratory_tests:[{test_name:field('Synthetic analyte'),observed_value:field('12'),unit:field('mg/L'),reference_range:field('1 - 10')}],prescription_items:[],warnings:[]},
    range_comparisons:['above' as const]};
  render(<DocumentSummaries api={vi.fn()} documentId="document" jobId="job" transcription={transcription} onError={vi.fn()} onPageSelect={vi.fn()}/>);
  expect(screen.getByText('Above stated range')).toBeTruthy();
  expect(screen.getByText(/Calculated from saved structured transcription, not the AI summary/)).toBeTruthy();
});

it('collapses older summaries when a new summary is created and allows reopening them', async()=>{
  let generated = false;
  const old = {id:'old',content:'Earlier synthetic summary',citations:[],truncated:false,model_name:'synthetic',created_at:1,review_status:'unverified'};
  const recent = {id:'new',content:'New synthetic summary',citations:[],truncated:false,model_name:'synthetic',created_at:2,review_status:'unverified'};
  const api=vi.fn(async (_path:string, init?:RequestInit)=>{
    if (init?.method === 'POST') {generated=true; return {} as Response;}
    return {json:async()=>generated ? [recent,old] : [old]} as Response;
  });
  render(<DocumentSummaries api={api} documentId="document" jobId="job" onError={vi.fn()} onPageSelect={vi.fn()}/>);
  fireEvent.click(screen.getByRole('button',{name:'View summaries'}));
  await screen.findByText('Earlier synthetic summary');
  fireEvent.click(screen.getByRole('button',{name:'Generate unverified summary'}));
  await screen.findByText('New synthetic summary');
  expect(screen.queryByText('Earlier synthetic summary')).toBeNull();
  const summaries=screen.getAllByRole('button',{name:/AI summary · unverified/});
  expect(summaries[0].getAttribute('aria-expanded')).toBe('true');
  expect(summaries[1].getAttribute('aria-expanded')).toBe('false');
  fireEvent.click(summaries[1]);
  expect(screen.getByText('Earlier synthetic summary')).toBeTruthy();
  expect(screen.queryByText('New synthetic summary')).toBeNull();
});
