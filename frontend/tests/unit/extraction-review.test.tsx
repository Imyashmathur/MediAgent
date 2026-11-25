import React from 'react';
import {afterEach, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import ExtractionReview, {ExtractionData, ExtractionVersion} from '../../app/extraction-review';

afterEach(cleanup);

it('retains source evidence for unchanged fields and marks corrections as clinician-entered', async()=>{
  const source = {value:'Synthetic test',origin:'source' as const,evidence:{page:1,quote:'Synthetic test',location_type:'page' as const}};
  const value = {value:'4',origin:'source' as const,evidence:{page:1,quote:'4',location_type:'page' as const}};
  const version:ExtractionVersion = {id:'synthetic',version:1,review_status:'unverified',reviewer_id:null,reviewed_at:null,data:{schema_version:'1.0',laboratory_tests:[{test_name:source,observed_value:value}],prescription_items:[],warnings:[]}};
  const onReview=vi.fn(async(_version:number,_data:ExtractionData)=>{});
  render(<ExtractionReview versions={[version]} canReview onReview={onReview} onPageSelect={()=>{}}/>);
  fireEvent.change(screen.getByLabelText('Observed value'),{target:{value:'5'}});
  fireEvent.click(screen.getByRole('button',{name:'Save clinician review'}));
  await waitFor(()=>expect(onReview).toHaveBeenCalledTimes(1));
  const [expectedVersion, data]=onReview.mock.calls[0];
  expect(expectedVersion).toBe(1);
  expect(data.laboratory_tests[0].test_name).toEqual(source);
  expect(data.laboratory_tests[0].observed_value).toEqual({value:'5',origin:'clinician',evidence:null});
});


it('upgrades an older transcription when a clinician adds laboratory details', async()=>{
  const data:ExtractionData = {schema_version:'1.0',laboratory_tests:[{}],prescription_items:[],warnings:[]};
  const version:ExtractionVersion = {id:'synthetic',version:1,review_status:'unverified',reviewer_id:null,reviewed_at:null,data};
  const onReview=vi.fn(async(_version:number,_data:ExtractionData)=>{});
  render(<ExtractionReview versions={[version]} canReview onReview={onReview} onPageSelect={()=>{}}/>);
  fireEvent.change(screen.getByLabelText('Laboratory details'),{target:{value:'Synthetic branch'}});
  fireEvent.click(screen.getByRole('button',{name:'Save clinician review'}));
  await waitFor(()=>expect(onReview).toHaveBeenCalledTimes(1));
  const reviewed=onReview.mock.calls[0][1];
  expect(reviewed.schema_version).toBe('1.2');
  expect(reviewed.laboratory_tests[0].laboratory_details).toEqual({value:'Synthetic branch',origin:'clinician',evidence:null});
});


it('identifies supplemental OCR evidence in the review and version history', ()=>{
  const evidence = {page:1,quote:'OCR test',location_type:'page' as const,layer:'supplemental_ocr' as const};
  const data:ExtractionData = {schema_version:'1.1',laboratory_tests:[{test_name:{value:'OCR test',origin:'source',evidence}}],prescription_items:[],warnings:[]};
  const version:ExtractionVersion = {id:'v2',version:2,review_status:'reviewed',reviewer_id:null,reviewed_at:null,data};
  const onPageSelect=vi.fn();
  render(<ExtractionReview versions={[version,{...version,id:'v1',version:1}]} canReview={false} onReview={async()=>{}} onPageSelect={onPageSelect}/>);
  expect(screen.getAllByText(/supplemental OCR/)).toHaveLength(3);
  fireEvent.click(screen.getByRole('button',{name:'Page 1'}));
  expect(onPageSelect).toHaveBeenCalledWith(1);
});

it('shows the cited historical transcription rather than the newest review', ()=>{
  const version=(number:number, value:string):ExtractionVersion=>({id:`v${number}`,version:number,review_status:number===1?'unverified':'reviewed',reviewer_id:null,reviewed_at:null,data:{schema_version:'1.0',laboratory_tests:[{observed_value:{value,origin:'clinician',evidence:null}}],prescription_items:[],warnings:[]}});
  render(<ExtractionReview versions={[version(2,'newer'),version(1,'cited')]} selectedVersion={1} canReview={false} onReview={async()=>{}} onPageSelect={()=>{}}/>);
  expect(screen.getByText(/cited version 1/)).toBeTruthy();
  expect(screen.getByText('cited',{selector:'span'})).toBeTruthy();
  expect(screen.queryByRole('button',{name:'Save clinician review'})).toBeNull();
});
