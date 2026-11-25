import React from 'react';
import {afterEach, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen, waitFor} from '@testing-library/react';
import Worklist from '../../app/worklist';

afterEach(cleanup);

it('pages patients using the submitted search and replaces page-specific reviews',async()=>{
  const api=vi.fn(async(_path:string, init?:RequestInit)=>{
    const request=JSON.parse(String(init?.body));
    const second=Boolean(request.patient_cursor);
    return {json:async()=>({patients:[{patient_id:second?'older':'first',full_name:second?'Older patient':'First patient',scopes:['read_documents']}],recent_uploads:[],pending_notes:[],pending_extractions:[],next_patient_cursor:second?null:'cursor'})} as Response;
  });
  render(<Worklist api={api} onSelect={vi.fn()} onError={vi.fn()}/>);
  fireEvent.change(screen.getByLabelText('Search by name or exact patient ID'),{target:{value:'Synthetic'}});
  fireEvent.click(screen.getByRole('button',{name:'Search and refresh worklist'}));
  await screen.findByText('First patient');
  fireEvent.change(screen.getByLabelText('Search by name or exact patient ID'),{target:{value:'Unsaved'}});
  fireEvent.click(screen.getByRole('button',{name:'Next patient page'}));
  await screen.findByText('Older patient');
  expect(JSON.parse(String(api.mock.calls[1][1]?.body))).toEqual({query:'Synthetic',patient_cursor:'cursor'});
  expect(screen.queryByText('First patient')).toBeNull();
  expect(screen.queryByRole('button',{name:'Next patient page'})).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:'Previous worklist view'}));
  await screen.findByText('First patient');
  expect(JSON.parse(String(api.mock.calls[2][1]?.body))).toEqual({query:'Synthetic'});
});

it('pages review categories independently and resets their cursors for the next patient page',async()=>{
  const api=vi.fn(async(_path:string, init?:RequestInit)=>{
    const request=JSON.parse(String(init?.body));
    const patientPage=Boolean(request.patient_cursor);
    return {json:async()=>({patients:[],recent_uploads:[],pending_notes:[],pending_extractions:[],next_patient_cursor:patientPage?null:'patient-next',next_upload_cursor:request.upload_cursor?null:'upload-next',next_note_cursor:request.note_cursor?null:'note-next',next_extraction_cursor:null})} as Response;
  });
  render(<Worklist api={api} onSelect={vi.fn()} onError={vi.fn()}/>);
  fireEvent.click(screen.getByRole('button',{name:'Search and refresh worklist'}));
  await screen.findByRole('button',{name:'Older recent uploads'});
  fireEvent.click(screen.getByRole('button',{name:'Older recent uploads'}));
  await waitFor(()=>expect(screen.queryByRole('button',{name:'Older recent uploads'})).toBeNull());
  fireEvent.click(screen.getByRole('button',{name:'Older notes awaiting review'}));
  await waitFor(()=>expect(screen.queryByRole('button',{name:'Older notes awaiting review'})).toBeNull());
  expect(JSON.parse(String(api.mock.calls[2][1]?.body))).toEqual({query:'',upload_cursor:'upload-next',note_cursor:'note-next'});
  fireEvent.click(screen.getByRole('button',{name:'Next patient page'}));
  await screen.findByRole('button',{name:'Older recent uploads'});
  expect(JSON.parse(String(api.mock.calls[3][1]?.body))).toEqual({query:'',patient_cursor:'patient-next'});
});
