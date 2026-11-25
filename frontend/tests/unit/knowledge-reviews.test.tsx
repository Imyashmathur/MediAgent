import React from 'react';
import {afterEach, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen} from '@testing-library/react';
import {KnowledgeReviewAdmin, KnowledgeReviewDoctor} from '../../app/knowledge-reviews';

afterEach(cleanup);

it('assigns a doctor and displays the recommendation state',async()=>{
  const api=vi.fn(async(path:string,init?:RequestInit)=>{
    if(init?.method==='POST') {expect(JSON.parse(String(init.body)).doctor_id).toBe('00000000-0000-4000-8000-000000000001');return {} as Response;}
    return {json:async()=>[{id:'review',doctor_id:'synthetic-doctor',status:'pending',comment:null}]} as Response;
  });
  render(<KnowledgeReviewAdmin api={api} sourceId="source" onError={vi.fn()}/>);
  fireEvent.click(screen.getByText('Clinical review assignments'));
  fireEvent.change(screen.getByLabelText('Doctor ID for clinical review'),{target:{value:'00000000-0000-4000-8000-000000000001'}});
  fireEvent.click(screen.getByRole('button',{name:'Assign reviewer'}));
  await screen.findByText(/synthetic-doctor · pending/);
});

it('allows an assigned doctor to submit a decline',async()=>{
  let submitted=false;
  const api=vi.fn(async(path:string,init?:RequestInit)=>{
    if(init?.method==='POST') {expect(JSON.parse(String(init.body))).toEqual({decision:'declined',comment:'Synthetic concern'});submitted=true;return {} as Response;}
    return {json:async()=>submitted?[]:[{id:'review',source_id:'source',doctor_id:'doctor',status:'pending',comment:null,source:{title:'Synthetic reference',version:'1',publisher:'Test',publication_date:'2025-01-01',specialty:'general',provenance:'synthetic',content:'Synthetic text'}}]} as Response;
  });
  render(<KnowledgeReviewDoctor api={api} onError={vi.fn()}/>);
  fireEvent.click(screen.getByRole('button',{name:'Refresh assigned reviews'}));
  await screen.findByText('Synthetic reference · 1');
  fireEvent.change(screen.getByLabelText('Recommendation'),{target:{value:'declined'}});
  fireEvent.change(screen.getByLabelText('Review comment (required when declining)'),{target:{value:'Synthetic concern'}});
  fireEvent.click(screen.getByRole('button',{name:'Submit clinical review'}));
  await screen.findByText('No pending assignments.');
});

it('prevents review submission when the assigned source changed', async()=>{
  const api=vi.fn(async()=>({json:async()=>[{id:'review',source_id:'source',doctor_id:'doctor',status:'pending',comment:null,source_matches:false,source:{title:'Changed source',version:'1',publisher:'Test',publication_date:'2025-01-01',specialty:'general',provenance:'synthetic',content:'Changed synthetic text'}}]} as Response));
  render(<KnowledgeReviewDoctor api={api} onError={vi.fn()}/>);
  fireEvent.click(screen.getByRole('button',{name:'Refresh assigned reviews'}));
  await screen.findByText(/assignment is no longer bound/);
  expect((screen.getByRole('button',{name:'Submit clinical review'}) as HTMLButtonElement).disabled).toBe(true);
});
