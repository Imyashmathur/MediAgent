import React from 'react';
import {afterEach, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen} from '@testing-library/react';
import PatientAccessHistory from '../../app/patient-access-history';

afterEach(cleanup);

it('shows bounded clinician activity and loads older events', async()=>{
  const api=vi.fn(async(path:string)=>({headers:new Headers(path.includes('cursor=')?{}:{'x-next-cursor':'one'}),json:async()=>[{id:path.includes('cursor=')?'two':'one',actor_id:'synthetic-doctor',action:path.includes('cursor=')?'note_list':'document_list',created_at:1000}]} as Response));
  render(<PatientAccessHistory api={api} onError={vi.fn()}/>);
  fireEvent.click(screen.getByRole('button',{name:'Refresh access activity'}));
  fireEvent.click(await screen.findByRole('button',{name:'Load older access activity'}));
  await screen.findByText('note list');
  expect(screen.getByText('document list')).toBeTruthy();
  expect(api).toHaveBeenCalledWith('audit/patient-access?cursor=one');
});
