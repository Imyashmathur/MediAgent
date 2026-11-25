import React from 'react';
import {afterEach, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen} from '@testing-library/react';
import ExtractedRecords from '../../app/extracted-records';

afterEach(cleanup);

it('loads older extracted records without replacing the first page', async()=>{
  const api=vi.fn(async(path:string)=>({json:async()=>({documents:[{document_id:path.includes('cursor=')?'old':'new',filename:path.includes('cursor=')?'old.pdf':'new.pdf',version:1,review_status:'unverified',reviewer_id:null,data:{schema_version:'1.0',laboratory_tests:[{test_name:{value:'Synthetic test',origin:'clinician',evidence:null}}],prescription_items:[],warnings:[]}}],truncated:!path.includes('cursor='),next_cursor:path.includes('cursor=')?null:'new'})} as Response));
  render(<ExtractedRecords api={api} patientId="patient" onOpenDocument={vi.fn()} onError={vi.fn()}/>);
  fireEvent.click(screen.getByRole('button',{name:'View extracted records'}));
  fireEvent.click(await screen.findByRole('button',{name:'Load older extracted records'}));
  await screen.findByText(/old.pdf/);
  expect(screen.getByText(/new.pdf/)).toBeTruthy();
  expect(api).toHaveBeenCalledWith('patients/patient/extracted-records?cursor=new');
});
