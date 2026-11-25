import React from 'react';
import {afterEach, expect, it, vi} from 'vitest';
import {cleanup, fireEvent, render, screen} from '@testing-library/react';
import Notifications from '../../app/notifications';

afterEach(cleanup);

it('identifies the relationship for a content-free care message notification', async()=>{
  const api=vi.fn(async()=>({json:async()=>[{id:'notification', kind:'care_message_received', patient_id:'patient', resource_id:'relationship-id', created_at:1000, read_at:null}]} as Response));
  render(<Notifications api={api} role="patient" onError={vi.fn()}/>);
  fireEvent.click(screen.getByRole('button',{name:'Refresh notifications'}));
  await screen.findByText(/A care team message is available/);
  expect(screen.getByText(/Relationship relationship-id/)).toBeTruthy();
});

it('loads older notifications and keeps the current page', async()=>{
  const api=vi.fn(async(path:string)=>({headers:new Headers(path.includes('cursor=') ? {} : {'x-next-cursor':'first'}),json:async()=>[{id:path.includes('cursor=')?'older':'first',kind:path.includes('cursor=')?'processing_failed':'processing_completed',patient_id:'patient',resource_id:'doc',created_at:1000,read_at:null}]} as Response));
  render(<Notifications api={api} role="patient" onError={vi.fn()}/>);
  fireEvent.click(screen.getByRole('button',{name:'Refresh notifications'}));
  fireEvent.click(await screen.findByRole('button',{name:'Load older notifications'}));
  await screen.findByText('Document processing could not complete.');
  expect(screen.getByText('Document processing completed; results are unverified.')).toBeTruthy();
  expect(api).toHaveBeenCalledWith('notifications?cursor=first');
});

it('opens the exact processing run referenced by a notification',async()=>{
  const api=vi.fn(async()=>({headers:new Headers(),json:async()=>[{id:'notification',kind:'processing_completed',patient_id:'patient',resource_id:'document',processing_job_id:'older-job',created_at:1000,read_at:null}]} as Response));
  const open=vi.fn();
  render(<Notifications api={api} role="patient" onError={vi.fn()} onOpenProcessing={open}/>);
  fireEvent.click(screen.getByRole('button',{name:'Refresh notifications'}));
  fireEvent.click(await screen.findByRole('button',{name:'Open processing run'}));
  expect(open).toHaveBeenCalledWith('document','older-job');
});
